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
- a noise-floor-relative echo gate and a transcript backstop on the reply's own speech;
- bounded, text-free session latency and queue telemetry;
- liveness and speech-model readiness endpoints; and
- a development-only browser harness.

## Readiness

`/health/live` answers whether the process is up. `/health/ready` answers
whether a conversation can happen, and reports one of three states:

| `status` | Meaning |
| --- | --- |
| `warming` | loading speech models; no sessions yet |
| `retrying` | a dependency was unreachable; retrying with backoff |
| `ready` | sessions accepted |

Readiness depends only on Fennec's own speech models, never on the consumer. An
unreachable consumer is logged at startup and fails individual turns; it does not
stop sessions being created, because the consumer is a separate application that
restarts on its own schedule. Warm-up retries until it succeeds, so a dependency
that is slow or briefly down at boot recovers without a restart.

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

The `fennec-control` data channel accepts `ping`, `audio.check`,
`microphone.settings`, and `session.close`. It emits `speech.started`,
`turn.committed`, `transcript.final`, `turn.ignored`, `turn.deferred`,
`turn.latency`, `assistant.text.delta`, `assistant.speaking`, `assistant.done`,
`assistant.cancelled`, `state.changed`, and bounded `error` events. There is
deliberately no parallel signaling WebSocket.

`microphone.settings` carries the three processing flags the browser actually
negotiated — `echo_cancellation`, `noise_suppression`, `auto_gain_control` — so a
session whose echo cancellation never engaged is visible in the logs. A flag the
browser does not report is `null`: unknown, not disabled. No device identifier is
accepted or logged.

`assistant.done` and the return to `listening` wait for the queued reply to
finish playing, not for its last phrase to be enqueued. Enqueuing runs seconds
ahead of the speaker, and announcing an idle session there meant Fennec's own
tail arrived as a fresh user turn instead of a barge-in.

### Where a slow reply went

`turn.latency` is emitted once per turn, when the first assistant audio is
queued. Its stages sum to `first_audio_queued_ms`, so a slow reply is
attributable to one stage rather than to the pipeline as a whole:

| Field | Time spent |
| --- | --- |
| `endpoint_delay_ms` | silence Silero waited out before committing the turn |
| `stt_ms` | the Whisper request |
| `llm_first_delta_ms` | the consumer's first token |
| `phrase_ms` | first token to the first speakable phrase |
| `tts_ms` | the Kokoro request for that phrase |
| `decode_ms` | WAV to 48 kHz PCM |
| `enqueue_ms` | handing frames to the assistant track |
| `speech_ms` | audio produced; over `tts_ms` it is Kokoro's real-time factor |

`turn_queue_ms` rides along but is deliberately outside that sum — detection to
the turn worker picking the turn up is a wall-clock wait that falls *inside* the
endpointing window, so adding it would count the same time twice. It is worth
watching on its own: a non-trivial value means Silero is running behind the
microphone rather than the pipeline being slow.

### Hearing you and not itself

Browser echo cancellation is the first line of defence, but residual speaker
output still reaches the microphone, and cancelling a reply because Fennec heard
itself is worse than not being interruptible. So Silero detecting speech starts a
*candidate*, and only a confirmed candidate cancels anything.

While assistant audio could physically be reaching the microphone — from the
first queued frame until the queue drains plus `PLAYBACK_TAIL_SECONDS` — a
candidate must clear `BARGE_IN_MARGIN_DB` over the session's measured noise floor
before it is confirmed. That estimate is maintained only while no candidate is
active and no assistant audio is playing, and it rises slowly so the start of
real speech cannot teach it that the speaker is background noise. Outside that
window — including while the consumer or TTS is still working, when there is no
audio to echo — any candidate is confirmed immediately, so a quiet correction
during a slow reply still interrupts.

An unconfirmed candidate is not discarded. It keeps accumulating and is
re-evaluated on every Silero tick, so speech that grows louder confirms with its
onset audio intact. If it never clears the margin it still becomes a turn, and
the turn worker is serial, so it is answered after the current reply finishes
rather than lost. Before that turn reaches the consumer its transcript is
compared against what the assistant actually said during the window it began in;
a strong overlap is dropped as `turn.ignored` with reason `assistant_echo`, which
is what stops one false cancellation from becoming a conversation with itself.
Neither side of that comparison is ever logged.

`turn.ignored` carries `empty_transcript` or `assistant_echo`. `speech.started`
carries `level_dbfs` and `noise_floor_dbfs` for the confirmed candidate plus
`confirmation_ms`, the wait between the candidate starting and being confirmed;
`assistant.cancelled` carries `level_dbfs` and its own `latency_ms`. Those two
latencies are deliberately separate — a fast cancellation cannot hide a
slow-feeling barge-in.

On normal session close the channel also emits `telemetry.session.summary`.
It contains counts, queue peaks, rejected-frame counts, and bounded
median/p95/max distributions for every `turn.latency` stage plus `vad_feed_ms`
(one Silero evaluation), `llm_total_ms`, and confirmed interruption
cancellation. It contains no transcript or audio content. `continued_segments`
counts speech fragments that arrived while an earlier fragment was still being
transcribed and were safely joined before dispatch to the consumer.

The summary also reports the echo gate: `echo_candidates_deferred` and
`echo_candidates_confirmed`, `assistant_echo_turns`,
`empty_unconfirmed_candidates`, `echo_reference_misses` and
`echo_reference_evictions` (assistant text aged out before its candidate was
classified), and `dropped_unconfirmed_turns` split by `capacity` and
`generation_cancelled`. Unconfirmed candidates are dropped rather than allowed to
exhaust the turn queue, because backpressure there ends the session; confirmed
speech keeps the original fatal behaviour. `possible_false_interruptions` counts
only turns where Fennec actually cancelled a generation and the result was empty
or was identified as its own echo. A `measurements` block carries the same
bounded distributions for candidate level and noise-floor margin in dBFS.

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

## Tenants

One gateway can serve several application backends. A tenant is identified by
its service token, and that token alone decides which backend receives the
session's turns - never `client_label` or anything else the caller asserts.
Callback URLs come from this configuration only, so no caller can aim the
gateway at a host of its choosing.

```bash
export FENNEC_TENANTS='[
  {"id": "dex",   "service_token": "...", "consumer_url": "http://dex.internal/v1/turns",
   "consumer_token": "...", "public_base_url": "https://dex.example/fennec"},
  {"id": "teamx", "service_token": "...", "consumer_url": "http://teamx.internal/v1/turns",
   "consumer_token": "...", "public_base_url": "https://teamx.example/fennec"}
]'
```

Each entry may also carry `consumer_health_url` and `public_base_url`. Give a tenant
its own `public_base_url` when its browser is served from its own hostname: the
signaling URL handed to that browser is built from it, the gateway sends no CORS
headers, and a shared origin would fail preflight for every tenant but one. Unset,
a tenant falls back to the gateway-wide `FENNEC_PUBLIC_BASE_URL`, which is what a
one-hostname deployment wants. Setting `FENNEC_TENANTS`
replaces `FENNEC_SERVICE_TOKEN`, `FENNEC_CONSUMER_URL`,
`FENNEC_CONSUMER_HEALTH_URL`, and `FENNEC_CONSUMER_TOKEN`; leave it unset and
those four describe a single implicit tenant, which is what a one-application
deployment wants. Ids and service tokens must be unique.

The speech stack, TURN credentials, and `FENNEC_MAX_SESSIONS` stay shared
across tenants - capacity is global, so one busy tenant can exhaust it.

## Application boundary

- An application backend uses `@fennec/consumer` to create browser-safe sessions
  and expose an authenticated Web-standard turn handler.
- A browser uses `@fennec/client/webrtc` for signaling, microphone lifecycle,
  assistant playback, conversation state, and transcripts.
- Fennec service and consumer credentials never enter browser code.
- The mock consumer is a deployment smoke fixture, not application logic.
