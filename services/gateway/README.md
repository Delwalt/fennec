# Fennec direct WebRTC gateway

This gateway runs Fennec's complete conversation loop locally, with no paid
speech provider and no third-party realtime platform.

It provides:

- authenticated, short-lived browser sessions;
- one HTTP SDP offer/answer exchange per session;
- one direct WebRTC microphone track and one paced assistant audio track;
- one `fennec-control` WebRTC data channel;
- Silero VAD with prefix audio, silence endpointing, and a maximum turn length;
- continuation-safe turn queuing that never discards transcription already in flight;
- final transcription through local Whisper;
- an authenticated, cancellable NDJSON consumer connection;
- phrase-bounded synthesis through local Kokoro;
- generation-aware cancellation and stale-audio rejection;
- confirmed-speech barge-in that aborts the active consumer and TTS work;
- bounded, text-free session latency and queue telemetry;
- liveness and model/consumer readiness endpoints; and
- a development-only browser harness.

## Run through Docker

From the repository root:

```sh
cp deploy/local/.env.example deploy/local/.env
pnpm stack:up
```

Open <http://127.0.0.1:8080/dev>, connect the microphone, and speak. The page
shows final user transcripts, streamed assistant text, conversation state, and
the actual capture constraints reported by the browser. Its session settings
panel exposes speech models, voice, language, turn detection, and browser audio
processing. Settings are validated and apply to the next connection; **Reset to
defaults** restores the active server defaults.

Run the reproducible synthetic conversation check after all three containers
are healthy:

```sh
pnpm smoke:conversation
pnpm smoke:pause
pnpm smoke:interruption
```

The first command synthesizes a prompt with Kokoro, sends it as a real WebRTC
microphone track, waits for Silero to commit the turn, transcribes it with
Whisper, streams the mock consumer response, and verifies non-silent Kokoro audio
on the assistant track. The pause check inserts an 850 ms thinking pause between
two spoken phrases and requires one complete user turn. The interruption check
injects another utterance during non-silent assistant audio and requires an
under-200 ms confirmed cancellation, a disconnected consumer stream, and zero
locally playable stale frames. None replaces a human microphone and speaker
check.

The gateway uses host networking because aiortc creates dynamic UDP ICE sockets.
That is the intended Linux homelab deployment. Docker Desktop development also
requires **Settings > Resources > Network > Enable host networking**. If that
option is unavailable, run the same locked service natively for the transport
check; do not add a fake TCP-only container result.

Stop it without deleting any caches or user data:

```sh
pnpm stack:down
```

## Native development

```sh
export FENNEC_SERVICE_TOKEN=local-development-service-token
export FENNEC_SESSION_SECRET=local-development-session-secret-change-me
export FENNEC_CONSUMER_TOKEN=local-development-consumer-token-change-me
export FENNEC_DEV_MODE=true
export FENNEC_CONVERSATION_ENABLED=false
uv run --project services/gateway --locked uvicorn fennec_gateway.app:app \
  --app-dir services/gateway --host 127.0.0.1 --port 8080
```

## Protocol

Backend session creation:

```text
POST /v1/sessions
Authorization: Bearer <backend-only-service-token>
```

Browser signaling:

```text
POST /v1/sessions/{session_id}/offer
Authorization: Bearer <short-lived-session-token>
Content-Type: application/json

{"type":"offer","sdp":"..."}
```

The offer is sent as soon as it is available; remaining ICE candidates trickle
in afterwards:

```text
POST /v1/sessions/{session_id}/candidates
Authorization: Bearer <short-lived-session-token>
```

Session tokens expire after two minutes by default and are scoped to exactly
one session. The SDP endpoint is single-use; reconnect means creating a new
session.

The authenticated session-creation request may include a bounded
`configuration` object. Omitted fields inherit the deployment defaults:

```json
{
  "client_label": "my-app",
  "configuration": {
    "endpoint_silence_ms": 1600,
    "vad_threshold": 0.45,
    "speech_language": "en-IN",
    "tts_voice": "af_sky"
  }
}
```

Model identifiers, language, voice, and all five turn settings are scoped to
the created session. Requested local models are prepared before a session is
returned. Only the authenticated backend API and the development-only harness
can request overrides; service URLs, credentials, capacity, timeouts, and
developer switches remain server-owned.

The `fennec-control` data channel accepts `ping`, `audio.check`, and
`session.close`. It emits `speech.started`, `turn.committed`,
`transcript.final`, `assistant.text.delta`, `assistant.speaking`,
`assistant.done`, `assistant.cancelled`, `state.changed`, and bounded `error`
events. There is deliberately no parallel signaling WebSocket.

On normal session close the channel also emits `telemetry.session.summary`.
It contains counts, queue peaks, rejected-frame counts, and bounded
median/p95/max distributions for endpointing, final transcription, first audio
queued at the gateway, and confirmed interruption cancellation. It contains no
transcript or audio content. `continued_segments` counts speech fragments that
arrived while an earlier fragment was still being transcribed and were safely
joined before dispatch to the consumer.

## Turn settings

The container exposes bounded settings for measured tuning:

| Environment variable | Default | Valid range |
| --- | ---: | ---: |
| `FENNEC_VAD_THRESHOLD` | `0.5` | `0.1`–`0.9` |
| `FENNEC_ENDPOINT_SILENCE_MS` | `1200` | `300`–`3000` |
| `FENNEC_PREFIX_MS` | `320` | `100`–`1000` |
| `FENNEC_MIN_SPEECH_MS` | `160` | `80`–`1000` |
| `FENNEC_MAX_TURN_SECONDS` | `30` | `5`–`120` |

Keep the defaults until the same pause, clipping, or false-interruption pattern
repeats in real sessions. Configuration outside these bounds fails at startup.
The 1200 ms endpoint default favors natural thinking pauses over the shortest
possible response latency; lower it only if repeated sessions feel too slow.

## Application boundary

- An application backend uses `@fennec/consumer` to create browser-safe sessions
  and expose an authenticated Web-standard turn handler.
- A browser uses `@fennec/client/webrtc` for signaling, microphone lifecycle,
  assistant playback, conversation state, and transcripts.
- Fennec service and consumer credentials never enter browser code.
- The mock consumer is a deployment smoke fixture, not application logic.
