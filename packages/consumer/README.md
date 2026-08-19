# @fennec/consumer

`@fennec/consumer` is the framework-neutral Node.js boundary between an
application backend and a Fennec gateway. It creates safe browser
sessions and adapts finalized voice turns to a cancellable, streaming response.

```ts
import { createFennecConsumer } from '@fennec/consumer';

const fennec = createFennecConsumer({
  gatewayUrl: process.env.FENNEC_GATEWAY_URL!,
  serviceCredential: process.env.FENNEC_SERVICE_TOKEN!,
  consumerToken: process.env.FENNEC_CONSUMER_TOKEN!,
  respond: async function* (turn, signal) {
    yield* myAgent.respond(turn.text, signal);
  },
});

// Return this result from an authenticated browser endpoint in your app.
const session = await fennec.createClientSession({ clientLabel: 'my-app' });

// Adapt this Web-standard handler in Express, Fastify, Hono, or your framework.
const handleFennecTurn = fennec.turnHandler();
```

The service and consumer credentials are backend-only. The browser receives
only `sessionId`, `signalingUrl`, `accessToken`, and `expiresAt`.

An application may pass session-scoped voice settings while creating that safe
browser session. Unspecified values inherit the Fennec deployment defaults:

```ts
await fennec.createClientSession({
  clientLabel: 'my-app',
  configuration: {
    endpointSilenceMs: 1600,
    speechLanguage: 'en-IN',
    ttsVoice: 'af_sky',
  },
});
```

This is the boundary an application settings UI should call through. The browser must
not call Fennec's authenticated internal session endpoint directly.
