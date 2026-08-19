<img src="fennec.svg" alt="Fennec" width="360">

Fennec gives a web app a realtime voice interface. Speech in, final text out;
streamed text in, speech out. Your app stays the brain — Fennec is its ears and
mouth.

Everything runs locally: Whisper for transcription, Kokoro for speech, Silero
for turn detection, direct WebRTC for transport. No cloud voice platform, no
per-minute bill.

## Run it

Needs Docker with host networking on (Docker Desktop: *Settings → Resources →
Network → Enable host networking*).

```sh
cp deploy/local/.env.example deploy/local/.env
pnpm stack:up
```

First run pulls the speech models, which takes a few minutes. `pnpm stack:down`
stops it; the weights stay cached.

## Test it

Open **<http://127.0.0.1:8080/dev>**, connect your microphone, and talk. You get
live transcripts, the assistant's streamed reply spoken back, and a settings
panel to tune the conversation. It replies through a mock backend — that is the
part you swap for your app.

No microphone handy? These run the full loop synthetically:

```sh
pnpm smoke:conversation   # speak -> transcribe -> respond -> speak back
pnpm smoke:pause          # a thinking pause stays one turn
pnpm smoke:interruption   # barge-in cancels in under 200 ms
```

## Change the models

Edit `deploy/local/.env` and restart with `pnpm stack:up`:

```sh
FENNEC_STT_MODEL=Systran/faster-distil-whisper-small.en   # larger = more accurate, slower
FENNEC_TTS_MODEL=speaches-ai/Kokoro-82M-v1.0-ONNX
FENNEC_TTS_VOICE=af_heart                                 # af_sky, am_adam, bf_emma, …
FENNEC_SPEECH_LANGUAGE=en
```

Kokoro ships 54 voices across English, Japanese, Chinese, French, Hindi,
Italian, Portuguese, and Spanish. List them from the running stack:

```sh
curl -s http://127.0.0.1:8000/v1/models | jq '.data[] | select(.task=="text-to-speech").voices[].id'
```

Any Whisper or Kokoro model that [Speaches](https://speaches.ai) serves works.
The `/dev` settings panel changes the same values per session, so try them there
before committing to one.

Turn detection is tunable the same way — `FENNEC_ENDPOINT_SILENCE_MS` (default
`1200`) is the one to reach for if replies feel too eager or too slow. See
[services/gateway/README.md](services/gateway/README.md) for the full list and
valid ranges.

## Integrate it

**Backend** — mint browser sessions and answer voice turns:

```ts
import { createFennecConsumer } from '@fennec/consumer';

const fennec = createFennecConsumer({
  gatewayUrl: 'http://127.0.0.1:8080',
  serviceCredential: process.env.FENNEC_SERVICE_TOKEN!,
  consumerToken: process.env.FENNEC_CONSUMER_TOKEN!,
  respond: async function* (turn, signal) {
    yield* myAgent.respond(turn.text, signal);   // yield phrases as they're ready
  },
});

app.post('/api/voice/session', requireLogin, async (c) =>
  c.json(await fennec.createClientSession()));   // safe to send to the browser

app.post('/internal/fennec/turns', fennec.turnHandler());  // called by the gateway
```

Point `FENNEC_CONSUMER_URL` in your `.env` at that second route and drop the
`mock-consumer` service from the compose file.

**Browser** — Fennec ships no UI, just observable state:

```ts
import { createFennecClient } from '@fennec/client';
import { createWebRTCTransport, toFennecConnection } from '@fennec/client/webrtc';

const client = createFennecClient({
  transport: createWebRTCTransport(),
  connection: async () => toFennecConnection(
    await (await fetch('/api/voice/session', { method: 'POST' })).json()),
});

client.subscribe((state) => render(state));   // React: useSyncExternalStore
await client.connect();
await client.startMicrophone();
```

State gives you `connectionState`, `voiceState` (`listening` / `thinking` /
`speaking`), `transcripts`, audio levels, and `error`. Methods cover `mute`,
`stopMicrophone`, `listMicrophones`, `selectMicrophone`, `enableAudioPlayback`,
`disconnect`, and `destroy`. The `/dev` page is a complete working UI over this
same API if you want code to copy.

The packages aren't on npm — add your app to this pnpm workspace and depend on
them with `"workspace:*"`.

## How it works

```text
browser mic ──WebRTC──> gateway ──> Silero VAD ──> Whisper ──> final transcript
                           │                                         │
browser speaker <──WebRTC──┴── Kokoro <── streamed text <──── your backend
```

Silero watches the audio and decides when you've actually finished a sentence
rather than just paused. Whisper transcribes that turn, the gateway POSTs it to
your backend, and your streamed reply is cut into phrases and synthesised as it
arrives — so speech starts before your answer is finished.

Every response carries a generation id. Start talking over the assistant and
Fennec stops playback, aborts the HTTP request to your backend, and throws away
any audio still in flight from the cancelled generation. That's what makes
interrupting feel like interrupting a person.

Credentials never reach the browser: your backend authenticates to the gateway
with `FENNEC_SERVICE_TOKEN`, the gateway authenticates back with
`FENNEC_CONSUMER_TOKEN`, and the browser only ever gets a two-minute,
single-session token. Audio is never stored, and session telemetry carries no
transcript text.

**Layout** — `services/gateway/` is the gateway and the `/dev` UI,
`deploy/local/` the Docker stack, `packages/client/` the browser SDK,
`packages/consumer/` the backend SDK, `examples/mock-consumer/` the stand-in
backend.

**Limits** — one client per session, English by default, no reconnect recovery
(a dropped connection means a new session), no provider fallback or telephony.

**Development** — `pnpm install`, then `pnpm check` for typecheck and the client,
consumer, and gateway test suites.
