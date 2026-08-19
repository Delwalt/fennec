<img src="fennec.svg" alt="Fennec" width="360">

I got tired of paying per minute to give an app a voice. I had a homeserver
sitting there doing nothing, so I wondered how far I could get with local models
only. This is where I landed.

Fennec is the ears and mouth for a web app. Speech goes in, you get final text
out. You stream text back, it comes out as speech. Your app stays the brain —
Fennec never touches your LLM, your prompts, or your data.

It runs entirely on your own machine. Whisper listens, Kokoro speaks, Silero
works out when you've actually finished talking, and WebRTC carries the audio.
Nothing leaves the box, and nothing bills you by the minute.

It works well enough that I use it. It is not a finished product — see
[what's rough](#whats-rough) at the bottom. Take it, break it, make it yours.

## Run it

You need Docker with host networking on. On Docker Desktop that's
*Settings → Resources → Network → Enable host networking* — WebRTC grabs
dynamic UDP ports and won't work without it.

```sh
cp deploy/local/.env.example deploy/local/.env
pnpm stack:up
```

First run pulls the speech models, which takes a few minutes and is the only
slow part. They're cached after that. `pnpm stack:down` when you're done.

## Play with it

Open **<http://127.0.0.1:8080/dev>**, connect your mic, and start talking.

You'll see your words appear as you speak, and hear a reply come back. There's
a settings panel to change voices and tune how long it waits before deciding
you've finished a sentence — that setting matters more than you'd expect, so
it's worth fiddling with.

Out of the box it replies through a dumb mock backend that just echoes. That's
the bit you replace with your actual app.

No mic handy? These run the whole loop without one:

```sh
pnpm smoke:conversation   # speak -> transcribe -> reply -> speak back
pnpm smoke:pause          # pausing mid-thought doesn't cut you off
pnpm smoke:interruption   # talking over it shuts it up fast
```

## Swap the models

Edit `deploy/local/.env`, run `pnpm stack:up` again:

```sh
FENNEC_STT_MODEL=Systran/faster-distil-whisper-small.en   # bigger = better, slower
FENNEC_TTS_MODEL=speaches-ai/Kokoro-82M-v1.0-ONNX
FENNEC_TTS_VOICE=af_heart                                 # af_sky, am_adam, bf_emma, …
FENNEC_SPEECH_LANGUAGE=en
```

Kokoro has 54 voices across English, Japanese, Chinese, French, Hindi, Italian,
Portuguese, and Spanish. To see them all while the stack is up:

```sh
curl -s http://127.0.0.1:8000/v1/models | jq '.data[] | select(.task=="text-to-speech").voices[].id'
```

Anything [Speaches](https://speaches.ai) can serve will work. The `/dev`
settings panel changes the same things per session, so try there first before
you commit to one.

If replies feel too eager or too slow, `FENNEC_ENDPOINT_SILENCE_MS` (default
`1200`) is the dial you want. [services/gateway/README.md](services/gateway/README.md)
has the rest.

## Wire it into your app

Two halves. Your backend answers questions, your frontend shows the
conversation.

**Backend** — hand out sessions, answer turns:

```ts
import { createFennecConsumer } from '@fennec/consumer';

const fennec = createFennecConsumer({
  gatewayUrl: 'http://127.0.0.1:8080',
  serviceCredential: process.env.FENNEC_SERVICE_TOKEN!,
  consumerToken: process.env.FENNEC_CONSUMER_TOKEN!,
  respond: async function* (turn, signal) {
    yield* myAgent.respond(turn.text, signal);   // yield phrases, don't wait for the full answer
  },
});

app.post('/api/voice/session', requireLogin, async (c) =>
  c.json(await fennec.createClientSession()));   // safe to hand to the browser

app.post('/internal/fennec/turns', fennec.turnHandler());  // the gateway calls this
```

Then point `FENNEC_CONSUMER_URL` at that second route and delete the
`mock-consumer` service from the compose file.

**Frontend** — there's no UI in here, just state you render however you like:

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

You get `connectionState`, `voiceState` (`listening` / `thinking` / `speaking`),
`transcripts`, audio levels, and `error`. Plus `mute`, `stopMicrophone`,
`listMicrophones`, `selectMicrophone`, `enableAudioPlayback`, `disconnect`, and
`destroy`. The `/dev` page is a full working UI built on exactly this — steal
from it.

Nothing's published to npm. Drop your app into this pnpm workspace and use
`"workspace:*"`.

## How it actually works

```text
your mic ──WebRTC──> gateway ──> Silero ──> Whisper ──> text
                        │                                │
your speakers <─WebRTC──┴── Kokoro <── streamed text <── your backend
```

Silero watches the audio and decides whether you've finished a sentence or just
paused to think. That distinction is most of what makes a voice app feel human
or infuriating. Whisper transcribes the turn, the gateway POSTs it to your
backend, and your reply gets chopped into phrases and spoken as it streams in —
so it starts talking before your answer is done.

Every reply carries a generation id. Talk over it and Fennec stops playback,
kills the HTTP request to your backend, and bins any audio still in flight from
the reply it just cancelled. That last part is what makes interrupting feel like
interrupting a person instead of waiting for a robot to finish.

Credentials never reach the browser. Your backend talks to the gateway with one
token, the gateway talks back with another, and the browser only ever gets a
two-minute token good for a single session. No audio is stored anywhere.

**Where things live** — `services/gateway/` is the gateway and the `/dev` UI,
`deploy/local/` the Docker stack, `packages/client/` the browser SDK,
`packages/consumer/` the backend SDK, `examples/mock-consumer/` the fake backend.

**Poking at it** — `pnpm install`, then `pnpm check` runs typechecks and all
three test suites.

## What's rough

- One client per session. It's not built for many people at once.
- English by default. Other languages are configured, not tested.
- If the connection drops you start a new session. No reconnect.
- Turn detection defaults come from my own voice and my own room. Yours will
  probably want different numbers.

## The name

Fennec — isn't that a cool name? 🦊 A fennec fox has ears bigger than its head,
which is a fitting accident for something whose entire job is listening.

The logo is AI-generated. I described roughly what I wanted and it drew it. I'm
still not over what these tools can do for us now.

## License

[MIT](LICENSE) — do whatever you like. The speech models are downloaded at
runtime and have their own terms: Whisper and Silero are MIT, Kokoro-82M is
Apache 2.0. Worth a look yourself if money is involved.
