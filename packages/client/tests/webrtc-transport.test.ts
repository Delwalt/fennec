import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createWebRTCTransport,
  toFennecConnection,
} from '../src/transports/webrtc-transport.ts';

describe('direct WebRTC transport', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('maps a safe client session to the generic connection contract', () => {
    expect(toFennecConnection({
      sessionId: 'session-1',
      signalingUrl: 'https://fennec.test/v1/sessions/session-1/offer',
      candidatesUrl: 'https://fennec.test/v1/sessions/session-1/candidates',
      accessToken: 'short-lived-token',
      expiresAt: '2026-08-17T16:00:00Z',
    })).toEqual({
      connectionUrl: 'https://fennec.test/v1/sessions/session-1/offer',
      candidatesUrl: 'https://fennec.test/v1/sessions/session-1/candidates',
      accessToken: 'short-lived-token',
      // A gateway on the same host mints no relay, and an empty list is how that arrives.
      iceServers: [],
    });
  });

  it('gives the peer connection the session\'s TURN credentials', async () => {
    // Without them the browser offers only host candidates, which never reach a gateway
    // on another machine — the whole reason the relay exists.
    const iceServers = [{
      urls: 'turn:100.116.8.95:3478?transport=udp',
      username: '1788610000:session-1',
      credential: 'derived-secret',
    }];
    let configuration: RTCConfiguration | undefined;
    const transport = createWebRTCTransport({
      fetch: async () => Response.json({ type: 'answer', sdp: 'answer-sdp' }),
      createPeerConnection: (config) => {
        configuration = config;
        return new FakePeerConnection() as unknown as RTCPeerConnection;
      },
    });

    await transport.connect({
      connectionUrl: 'https://fennec.test/offer',
      accessToken: 'voice-token',
      iceServers,
    });

    expect(configuration?.iceServers).toEqual(iceServers);
  });

  it('carries the ICE servers from a client session into the connection', () => {
    expect(toFennecConnection({
      sessionId: 'session-1',
      signalingUrl: 'https://fennec.test/v1/sessions/session-1/offer',
      accessToken: 'short-lived-token',
      expiresAt: '2026-08-17T16:00:00Z',
      iceServers: [{ urls: 'turn:relay:3478', username: 'u', credential: 'c' }],
    }).iceServers).toEqual([{ urls: 'turn:relay:3478', username: 'u', credential: 'c' }]);
  });

  it('sends the offer before gathering finishes, then trickles each candidate', async () => {
    // Holding the offer until gathering completes spends a TURN allocation's worth of
    // seconds before the gateway has heard anything at all. The offer goes first.
    const peer = new FakePeerConnection();
    peer.iceGatheringState = 'gathering';
    const posts: { url: string; body: unknown }[] = [];
    const transport = createWebRTCTransport({
      fetch: async (url, init) => {
        posts.push({ url: String(url), body: JSON.parse(String(init?.body)) });
        return Response.json({ type: 'answer', sdp: 'answer-sdp' });
      },
      createPeerConnection: () => peer as unknown as RTCPeerConnection,
    });

    await transport.connect({
      connectionUrl: 'https://fennec.test/offer',
      candidatesUrl: 'https://fennec.test/candidates',
      accessToken: 'voice-token',
    });

    // Connected without gathering ever completing.
    expect(peer.iceGatheringState).toBe('gathering');
    expect(posts).toHaveLength(1);
    expect(posts[0]?.url).toBe('https://fennec.test/offer');

    peer.dispatchEvent(
      Object.assign(new Event('icecandidate'), {
        candidate: { candidate: 'candidate:1 1 udp 1 10.0.0.1 1 typ host', sdpMid: '0', sdpMLineIndex: 0 },
      }),
    );
    // The end-of-gathering marker is not a candidate and must not be posted.
    peer.dispatchEvent(Object.assign(new Event('icecandidate'), { candidate: null }));
    await Promise.resolve();

    expect(posts).toHaveLength(2);
    expect(posts[1]).toMatchObject({
      url: 'https://fennec.test/candidates',
      body: { candidate: 'candidate:1 1 udp 1 10.0.0.1 1 typ host', sdpMid: '0', sdpMLineIndex: 0 },
    });
  });

  it('offers what it has rather than waiting on gathering forever', async () => {
    // Gathering completes only once every candidate resolves, and a TURN allocation the
    // network silently drops never does. Unbounded, that leaves a caller connecting with
    // no error and no end — which is a two-minute spinner, not a failure anyone can see.
    vi.useFakeTimers();
    const peer = new FakePeerConnection();
    peer.iceGatheringState = 'gathering';
    const fetch = vi.fn(async () => Response.json({ type: 'answer', sdp: 'answer-sdp' }));
    const transport = createWebRTCTransport({
      fetch,
      createPeerConnection: () => peer as unknown as RTCPeerConnection,
    });

    // No candidates endpoint, so the offer must carry them and the wait is unavoidable.
    const connecting = transport.connect({
      connectionUrl: 'https://fennec.test/offer',
      accessToken: 'voice-token',
    });
    await vi.advanceTimersByTimeAsync(4_000);
    expect(fetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_500);
    await connecting;

    expect(fetch).toHaveBeenCalledOnce();
    expect(peer.remoteDescription).toEqual({ type: 'answer', sdp: 'answer-sdp' });
  });

  it('calls fetch without making it a method of the transport', async () => {
    // A browser's fetch throws "Illegal invocation" when its receiver is anything but the
    // window, so the transport must never invoke it as one of its own methods.
    const receivers: unknown[] = [];
    const transport = createWebRTCTransport({
      fetch: function (this: unknown) {
        receivers.push(this);
        return Promise.resolve(Response.json({ type: 'answer', sdp: 'answer-sdp' }));
      },
      createPeerConnection: () => new FakePeerConnection() as unknown as RTCPeerConnection,
    });

    await transport.connect({
      connectionUrl: 'https://fennec.test/offer',
      accessToken: 'voice-token',
    });

    expect(receivers).toHaveLength(1);
    expect(receivers[0]).toBe(globalThis);
  });

  it('signals directly and builds final user and assistant transcripts', async () => {
    const peer = new FakePeerConnection();
    const fetch = vi.fn(async () => Response.json({ type: 'answer', sdp: 'answer-sdp' }));
    const transport = createWebRTCTransport({
      fetch,
      createPeerConnection: () => peer as unknown as RTCPeerConnection,
    });

    await transport.connect({
      connectionUrl: 'https://fennec.test/offer',
      accessToken: 'voice-token',
    });
    peer.connectionState = 'connected';
    peer.dispatchEvent(new Event('connectionstatechange'));
    peer.control.dispatchEvent(controlEvent({
      type: 'transcript.final',
      turn_id: 'turn-1',
      text: 'Hello Fennec',
    }));
    peer.control.dispatchEvent(controlEvent({
      type: 'assistant.text.delta',
      generation_id: 'generation-1',
      text: 'Hello ',
    }));
    peer.control.dispatchEvent(controlEvent({
      type: 'assistant.text.delta',
      generation_id: 'generation-1',
      text: 'back.',
    }));
    peer.control.dispatchEvent(controlEvent({
      type: 'assistant.done',
      generation_id: 'generation-1',
    }));

    expect(fetch).toHaveBeenCalledWith('https://fennec.test/offer', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ authorization: 'Bearer voice-token' }),
    }));
    expect(peer.remoteDescription).toEqual({ type: 'answer', sdp: 'answer-sdp' });
    expect(transport.getState()).toMatchObject({
      connectionState: 'connected',
      transcripts: [
        { id: 'turn-1', speaker: 'user', text: 'Hello Fennec', isFinal: true },
        { id: 'generation-1', speaker: 'assistant', text: 'Hello back.', isFinal: true },
      ],
    });
  });

  it('stops local playback on cancellation and resumes the next generation', async () => {
    const peer = new FakePeerConnection();
    const audio = new FakeAudioElement();
    const transport = createWebRTCTransport({
      fetch: async () => Response.json({ type: 'answer', sdp: 'answer-sdp' }),
      createPeerConnection: () => peer as unknown as RTCPeerConnection,
      createAudioElement: () => audio as unknown as HTMLAudioElement,
    });
    await transport.connect({
      connectionUrl: 'https://fennec.test/offer',
      accessToken: 'voice-token',
    });
    peer.dispatchEvent(audioTrackEvent());
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());

    peer.control.dispatchEvent(controlEvent({
      type: 'assistant.cancelled',
      generation_id: 'generation-1',
    }));
    expect(audio.pause).toHaveBeenCalledOnce();

    peer.control.dispatchEvent(controlEvent({
      type: 'state.changed',
      state: 'speaking',
    }));
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledTimes(2));
  });

  it('applies configurable browser microphone processing', async () => {
    const peer = new FakePeerConnection();
    const track = {
      kind: 'audio',
      enabled: true,
      stop: vi.fn(),
      getSettings: () => ({ deviceId: 'microphone-1' }),
    };
    const stream = {
      getAudioTracks: () => [track],
      getTracks: () => [track],
    };
    const getUserMedia = vi.fn(async () => stream);
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
    const transport = createWebRTCTransport({
      fetch: async () => Response.json({ type: 'answer', sdp: 'answer-sdp' }),
      createPeerConnection: () => peer as unknown as RTCPeerConnection,
      microphoneProcessing: {
        echoCancellation: false,
        noiseSuppression: true,
        autoGainControl: false,
      },
    });

    try {
      await transport.connect({
        connectionUrl: 'https://fennec.test/offer',
        accessToken: 'voice-token',
      });
      await transport.startMicrophone();

      expect(getUserMedia).toHaveBeenCalledWith({
        audio: {
          echoCancellation: false,
          noiseSuppression: true,
          autoGainControl: false,
          channelCount: 1,
        },
        video: false,
      });
    } finally {
      await transport.dispose();
      vi.unstubAllGlobals();
    }
  });
});

class FakePeerConnection extends EventTarget {
  connectionState: RTCPeerConnectionState = 'new';
  iceGatheringState: RTCIceGatheringState = 'complete';
  localDescription: RTCSessionDescriptionInit | null = null;
  remoteDescription: RTCSessionDescriptionInit | null = null;
  readonly control = new FakeDataChannel();
  readonly sender = { replaceTrack: vi.fn(async () => undefined) };

  createDataChannel(): RTCDataChannel {
    return this.control as unknown as RTCDataChannel;
  }

  addTransceiver(): RTCRtpTransceiver {
    return { sender: this.sender } as unknown as RTCRtpTransceiver;
  }

  async createOffer(): Promise<RTCSessionDescriptionInit> {
    return { type: 'offer', sdp: 'offer-sdp' };
  }

  async setLocalDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.localDescription = description;
  }

  async setRemoteDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.remoteDescription = description;
  }

  close(): void {
    this.connectionState = 'closed';
  }
}

class FakeDataChannel extends EventTarget {
  close(): void {}
}

class FakeAudioElement {
  autoplay = true;
  srcObject: object | null = null;
  play = vi.fn(async () => undefined);
  pause = vi.fn();
  remove = vi.fn();
}

function controlEvent(value: Record<string, string>): MessageEvent<string> {
  return new MessageEvent('message', { data: JSON.stringify(value) });
}

function audioTrackEvent(): Event {
  const event = new Event('track');
  Object.defineProperties(event, {
    track: { value: { kind: 'audio' } },
    streams: { value: [{}] },
  });
  return event;
}
