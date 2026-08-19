import { describe, expect, it, vi } from 'vitest';
import { createFennecConsumer } from '../src/index.ts';

describe('@fennec/consumer', () => {
  it('creates a safe client session through the authenticated gateway API', async () => {
    const fetch = vi.fn(async () => Response.json({
      session_id: 'session-1',
      signaling_url: 'https://fennec.test/v1/sessions/session-1/offer',
      access_token: 'voice-token',
      expires_at: '2026-08-17T16:00:00Z',
    }, { status: 201 }));
    const fennec = createFennecConsumer({
      gatewayUrl: 'https://fennec.test/',
      serviceCredential: 'service-secret',
      fetch,
      respond: async function* () {},
    });

    await expect(fennec.createClientSession({
      clientLabel: 'teamx-user',
      configuration: {
        endpointSilenceMs: 1_600,
        vadThreshold: 0.45,
        ttsVoice: 'af_sky',
      },
    })).resolves.toEqual({
      sessionId: 'session-1',
      signalingUrl: 'https://fennec.test/v1/sessions/session-1/offer',
      accessToken: 'voice-token',
      expiresAt: '2026-08-17T16:00:00Z',
    });
    expect(fetch).toHaveBeenCalledWith('https://fennec.test/v1/sessions', expect.objectContaining({
      headers: expect.objectContaining({ authorization: 'Bearer service-secret' }),
      body: JSON.stringify({
        client_label: 'teamx-user',
        configuration: {
          tts_voice: 'af_sky',
          vad_threshold: 0.45,
          endpoint_silence_ms: 1_600,
        },
      }),
    }));
  });

  it('authenticates and streams one finalized turn as generation-bound NDJSON', async () => {
    const respond = vi.fn(async function* () {
      yield 'Hello ';
      yield 'back.';
    });
    const handler = createFennecConsumer({
      gatewayUrl: 'https://fennec.test',
      serviceCredential: 'service-secret',
      consumerToken: 'consumer-secret',
      respond,
    }).turnHandler();
    const response = await handler(turnRequest('consumer-secret'));

    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toBe('application/x-ndjson');
    await expect(response.text()).resolves.toBe([
      '{"type":"text.delta","generation_id":"generation-1","text":"Hello "}',
      '{"type":"text.delta","generation_id":"generation-1","text":"back."}',
      '{"type":"text.done","generation_id":"generation-1"}',
      '',
    ].join('\n'));
    expect(respond).toHaveBeenCalledWith({
      sessionId: 'session-1',
      turnId: 'turn-1',
      generationId: 'generation-1',
      text: 'Can you hear me?',
    }, expect.any(AbortSignal));
  });

  it('rejects invalid credentials and malformed turns before calling the application', async () => {
    const respond = vi.fn(async function* () { yield 'unused'; });
    const handler = createFennecConsumer({
      gatewayUrl: 'https://fennec.test',
      serviceCredential: 'service-secret',
      consumerToken: 'consumer-secret',
      respond,
    }).turnHandler();

    expect((await handler(turnRequest('wrong-secret'))).status).toBe(403);
    expect((await handler(new Request('https://teamx.test/internal/fennec/turns', {
      method: 'POST',
      headers: { authorization: 'Bearer consumer-secret' },
      body: '{}',
    }))).status).toBe(400);
    expect(respond).not.toHaveBeenCalled();
  });

  it('propagates gateway interruption to the application AbortSignal', async () => {
    let applicationSignal: AbortSignal | undefined;
    const requestAbort = new AbortController();
    const handler = createFennecConsumer({
      gatewayUrl: 'https://fennec.test',
      serviceCredential: 'service-secret',
      consumerToken: 'consumer-secret',
      respond: async function* (_turn, signal) {
        applicationSignal = signal;
        yield 'Started';
        await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }));
      },
    }).turnHandler();
    const response = await handler(turnRequest('consumer-secret', requestAbort.signal));
    const reader = response.body!.getReader();
    await reader.read();

    requestAbort.abort('interrupted');
    await vi.waitFor(() => expect(applicationSignal?.aborted).toBe(true));
    await reader.cancel();
  });
});

function turnRequest(token: string, signal?: AbortSignal): Request {
  return new Request('https://teamx.test/internal/fennec/turns', {
    method: 'POST',
    headers: {
      authorization: `Bearer ${token}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      session_id: 'session-1',
      turn_id: 'turn-1',
      generation_id: 'generation-1',
      text: 'Can you hear me?',
    }),
    ...(signal ? { signal } : {}),
  });
}
