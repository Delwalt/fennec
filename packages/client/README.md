# @fennec/client

`@fennec/client` is Fennec's framework-free browser SDK. It renders no UI,
ships no CSS, reads no credentials, and exposes no provider SDK objects.

Create a transport, supply a function that obtains short-lived connection
details from your application, and subscribe to state:

```ts
import { createFennecClient } from '@fennec/client';
import { createWebRTCTransport, toFennecConnection } from '@fennec/client/webrtc';

const client = createFennecClient({
  transport: createWebRTCTransport(),
  connection: async () => {
    const response = await fetch('/api/voice/session', { method: 'POST' });
    const value = await response.json();
    return toFennecConnection(value);
  },
});

client.subscribe((state) => renderVoiceUi(state));
await client.connect();
await client.startMicrophone();
```

The client exposes `connect`, `disconnect`, `startMicrophone`,
`stopMicrophone`, `mute`, `unmute`, `listMicrophones`, `selectMicrophone`, and
`enableAudioPlayback`. State includes connection and voice state, microphone
and playback state, local/remote levels, partial/final transcripts, devices,
and errors. The direct transport retains at most 200 transcript segments and
uses browser echo cancellation, noise suppression, and automatic gain control.
An `assistant.cancelled` event pauses received audio immediately; the next
speaking generation resumes the same media element and still honors autoplay
recovery.

Browser microphone processing is configurable when the direct transport is
created. All three protections remain on by default:

```ts
createWebRTCTransport({
  microphoneProcessing: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
});
```

Call and await `client.destroy()` when the client is no longer needed. It
disconnects and disposes the configured transport, timers, listeners, tracks,
and audio elements.

The transport is a separate subpath export, `@fennec/client/webrtc`, behind the
`VoiceTransport` boundary. The core client has no transport-specific code.
