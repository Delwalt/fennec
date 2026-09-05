import { timingSafeEqual } from 'node:crypto';

const MAX_TURN_BYTES = 65_536;

export type FennecIceServer = {
  urls: string;
  username: string;
  credential: string;
};

export type FennecClientSession = {
  sessionId: string;
  signalingUrl: string;
  accessToken: string;
  expiresAt: string;
  /** Session-scoped TURN credentials, expiring with the session. Empty when the gateway
   *  runs without a relay, which is only ever true when the browser is on its host. */
  iceServers: FennecIceServer[];
};

export type FennecTurn = {
  sessionId: string;
  turnId: string;
  generationId: string;
  text: string;
};

export type FennecVoiceConfiguration = {
  sttModel?: string;
  ttsModel?: string;
  ttsVoice?: string;
  speechLanguage?: string;
  vadThreshold?: number;
  endpointSilenceMs?: number;
  prefixMs?: number;
  minSpeechMs?: number;
  maxTurnSeconds?: number;
};

export type FennecConsumerOptions = {
  gatewayUrl: string;
  serviceCredential: string;
  consumerToken?: string;
  fetch?: typeof fetch;
  respond: (turn: FennecTurn, signal: AbortSignal) => AsyncIterable<string>;
};

export type CreateClientSessionOptions = {
  clientLabel?: string;
  configuration?: FennecVoiceConfiguration;
};

export type FennecConsumer = {
  createClientSession(options?: CreateClientSessionOptions): Promise<FennecClientSession>;
  turnHandler(): (request: Request) => Promise<Response>;
};

export function createFennecConsumer(options: FennecConsumerOptions): FennecConsumer {
  const gatewayUrl = withoutTrailingSlash(required('gatewayUrl', options.gatewayUrl));
  const serviceCredential = required('serviceCredential', options.serviceCredential);
  const request = options.fetch ?? fetch;

  return {
    async createClientSession(
      sessionOptions: CreateClientSessionOptions = {},
    ): Promise<FennecClientSession> {
      const response = await request(`${gatewayUrl}/v1/sessions`, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${serviceCredential}`,
          'content-type': 'application/json',
        },
        body: JSON.stringify({
          ...(sessionOptions.clientLabel ? { client_label: sessionOptions.clientLabel } : {}),
          ...(sessionOptions.configuration
            ? { configuration: serializeVoiceConfiguration(sessionOptions.configuration) }
            : {}),
        }),
      });
      if (!response.ok) {
        // The gateway says why in `detail`, and the difference matters to whoever reads
        // it: a full gateway is nothing like a rejected credential, but an HTTP number
        // alone makes them look the same.
        throw new Error(
          `Fennec session creation failed with HTTP ${response.status}.${await reason(response)}`,
        );
      }
      return parseClientSession(await response.json());
    },

    turnHandler(): (request: Request) => Promise<Response> {
      const consumerToken = options.consumerToken;
      if (!consumerToken) {
        throw new Error('consumerToken is required before creating a Fennec turn handler.');
      }
      return async (incoming: Request): Promise<Response> => {
        if (!authorized(incoming.headers.get('authorization'), consumerToken)) {
          return jsonError(403, 'invalid_credential');
        }
        const declaredLength = Number(incoming.headers.get('content-length'));
        if (Number.isFinite(declaredLength) && declaredLength > MAX_TURN_BYTES) {
          return jsonError(413, 'body_too_large');
        }

        let raw: string;
        try {
          raw = await incoming.text();
        } catch {
          return jsonError(400, 'invalid_body');
        }
        if (new TextEncoder().encode(raw).byteLength > MAX_TURN_BYTES) {
          return jsonError(413, 'body_too_large');
        }

        let turn: FennecTurn;
        try {
          turn = parseTurn(JSON.parse(raw) as unknown);
        } catch {
          return jsonError(400, 'invalid_turn');
        }

        const abort = new AbortController();
        const abortFromRequest = () => abort.abort(incoming.signal.reason);
        if (incoming.signal.aborted) abortFromRequest();
        else incoming.signal.addEventListener('abort', abortFromRequest, { once: true });

        const iterator = options.respond(turn, abort.signal)[Symbol.asyncIterator]();
        const encoder = new TextEncoder();
        const body = new ReadableStream<Uint8Array>({
          async pull(controller) {
            try {
              const next = await iterator.next();
              if (next.done) {
                controller.enqueue(encoder.encode(ndjson({
                  type: 'text.done',
                  generation_id: turn.generationId,
                })));
                controller.close();
                incoming.signal.removeEventListener('abort', abortFromRequest);
                return;
              }
              if (typeof next.value !== 'string') {
                throw new TypeError('Fennec consumer responses must yield strings.');
              }
              controller.enqueue(encoder.encode(ndjson({
                type: 'text.delta',
                generation_id: turn.generationId,
                text: next.value,
              })));
            } catch (error) {
              incoming.signal.removeEventListener('abort', abortFromRequest);
              controller.error(error);
            }
          },
          async cancel(reason) {
            abort.abort(reason);
            incoming.signal.removeEventListener('abort', abortFromRequest);
            await iterator.return?.();
          },
        });

        return new Response(body, {
          status: 200,
          headers: {
            'cache-control': 'no-store',
            'content-type': 'application/x-ndjson',
          },
        });
      };
    },
  };
}

/** The gateway's own explanation, when it gave one. Never throws: this runs on a path
 *  that is already failing, and a body that will not parse must not replace the status. */
async function reason(response: Response): Promise<string> {
  try {
    const detail = (JSON.parse(await response.text()) as { detail?: unknown }).detail;
    return typeof detail === 'string' && detail ? ` ${detail}` : '';
  } catch {
    return '';
  }
}

function parseClientSession(value: unknown): FennecClientSession {
  const object = record(value);
  return {
    sessionId: nonEmptyString(object.session_id),
    signalingUrl: nonEmptyString(object.signaling_url),
    accessToken: nonEmptyString(object.access_token),
    expiresAt: nonEmptyString(object.expires_at),
    iceServers: parseIceServers(object.ice_servers),
  };
}

function parseIceServers(value: unknown): FennecIceServer[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error('fennec: ice_servers must be an array');
  return value.map((entry) => {
    const server = record(entry);
    return {
      urls: nonEmptyString(server.urls),
      username: nonEmptyString(server.username),
      credential: nonEmptyString(server.credential),
    };
  });
}

function serializeVoiceConfiguration(
  configuration: FennecVoiceConfiguration,
): Record<string, string | number> {
  return {
    ...(configuration.sttModel === undefined ? {} : { stt_model: configuration.sttModel }),
    ...(configuration.ttsModel === undefined ? {} : { tts_model: configuration.ttsModel }),
    ...(configuration.ttsVoice === undefined ? {} : { tts_voice: configuration.ttsVoice }),
    ...(configuration.speechLanguage === undefined
      ? {}
      : { speech_language: configuration.speechLanguage }),
    ...(configuration.vadThreshold === undefined
      ? {}
      : { vad_threshold: configuration.vadThreshold }),
    ...(configuration.endpointSilenceMs === undefined
      ? {}
      : { endpoint_silence_ms: configuration.endpointSilenceMs }),
    ...(configuration.prefixMs === undefined ? {} : { prefix_ms: configuration.prefixMs }),
    ...(configuration.minSpeechMs === undefined
      ? {}
      : { min_speech_ms: configuration.minSpeechMs }),
    ...(configuration.maxTurnSeconds === undefined
      ? {}
      : { max_turn_seconds: configuration.maxTurnSeconds }),
  };
}

function parseTurn(value: unknown): FennecTurn {
  const object = record(value);
  return {
    sessionId: nonEmptyString(object.session_id),
    turnId: nonEmptyString(object.turn_id),
    generationId: nonEmptyString(object.generation_id),
    text: nonEmptyString(object.text),
  };
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError();
  return value as Record<string, unknown>;
}

function nonEmptyString(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) throw new TypeError();
  return value;
}

function required(name: string, value: string): string {
  if (!value.trim()) throw new Error(`${name} must not be empty.`);
  return value;
}

function withoutTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '');
}

function authorized(header: string | null, expected: string): boolean {
  const prefix = 'Bearer ';
  if (!header?.startsWith(prefix)) return false;
  const supplied = Buffer.from(header.slice(prefix.length).trim());
  const wanted = Buffer.from(expected);
  return supplied.length === wanted.length && timingSafeEqual(supplied, wanted);
}

function ndjson(value: Record<string, string>): string {
  return `${JSON.stringify(value)}\n`;
}

function jsonError(status: number, error: string): Response {
  return Response.json({ error }, { status });
}
