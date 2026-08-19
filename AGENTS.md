# Fennec working agreement

Guides every change made in this repository. `README.md` describes what Fennec
is and how to use it; this file describes how to change it.

## The durable boundary

```text
voice in -> final text out
streamed text in -> voice out
```

Fennec is the application's ears and mouth. The consuming application is the
brain and owns reasoning, memory, tools, permissions, and business logic. Do not
grow Fennec across that line.

## Ownership

**`packages/client`** — microphone permission and capture, browser audio
processing, the WebRTC connection, playback and autoplay recovery, observable
connection/voice/transcript state, and immediate local playback cancellation.
Framework-free. Renders no UI, ships no CSS, reads no credentials, and exposes
no transport internals.

**`services/gateway`** — session lifecycle and authentication, audio receipt,
VAD and turn detection, STT, transcript events, speech chunking, TTS,
cancellation, generation safety, and latency telemetry.

**`packages/consumer`** — the backend's boundary onto the gateway: safe browser
session creation and the cancellable turn handler. No application logic.

**The consuming application** — transcript interpretation, response generation,
models, tools, permissions, cancelling its own work when interrupted, provider
credentials, and session authentication. Reusable Fennec code accepts
configuration explicitly and never reads application `.env` files.

## Non-negotiable conversation rules

- Browser acoustic echo cancellation is the default audio contract.
- Only final transcripts trigger consumer work. Partials are observational.
- Start TTS from useful phrases, not raw tokens and not a completed response.
- Every assistant response carries a `generation_id`.
- Cancellation stops local playback, clears queued work, cancels active TTS,
  notifies the consumer, and rejects all late output from that generation.
- Discard queued text and audio on interruption. No unbounded buffering in code
  we own.
- Raw audio is never persisted. Telemetry never contains transcript text.

## How we build

Start each slice with a user-visible outcome and concrete acceptance examples,
then implement the smallest complete vertical path that proves it. Let real
slices reveal shared structure.

- One obvious owner and one domain name per responsibility.
- Keep code that changes together close together. Create a directory only when a
  real responsibility needs an owner.
- Keep entry points thin and orchestration linear.
- Prefer named functions and modules. A class needs meaningful state or
  lifecycle; an interface needs a real boundary or a second implementation.
- Prefer explicit imports. No broad barrels, no service locators.
- No `utils`, `common`, or `shared` dumping grounds.
- Never two names for one concept. Do not duplicate contracts or DTOs.
- Comments explain constraints and reasons, never what the code plainly does and
  never what the code used to do.
- Delete unused indirection before adding more structure.

Names state outcomes in Fennec's vocabulary: `finalizeUserTurn`,
`speakTextStream`, `cancelGeneration` — not `handle`, `process`, or `manage`.

## Testing

Test public behavior, not private layout.

- Test turn, chunking, ordering, and cancellation rules directly.
- Add contract tests at the client, consumer, and gateway serialization edges.
- Test refusal and stale-generation paths, not only success.
- `pnpm check` before calling a change done. The three `pnpm smoke:*` checks
  exercise the real WebRTC loop and must pass before shipping transport, turn,
  or cancellation changes.
- Real microphone and speaker behavior still needs a human session. Exercise
  headphones, laptop speakers, pauses, corrections, interruptions, slow text
  streams, and background noise.

Measured targets, to verify rather than to fake in tests:

- median speech-end to first assistant audio under 1 s; p95 under 2 s;
- confirmed interruption to stopped playback under 200 ms; and
- zero stale audio after cancellation.

## Definition of done

1. The user-visible outcome works end to end.
2. Acceptance examples and boundary contracts are tested.
3. Relevant latency, cancellation, and errors are observable.
4. Provider and protocol specifics stay at their adapter edge.
5. The diff is understandable without architecture lore.
6. Unused layers, names, comments, and configuration are removed.
7. `README.md` and `services/gateway/README.md` reflect any behavior change, in
   the same commit.

When uncertain, favor the common conversation path, the shorter dependency
path, and the design a new maintainer understands fastest.
