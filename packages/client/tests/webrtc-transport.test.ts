import { describe, expect, it, vi } from 'vitest';
import {
  createWebRTCTransport,
  toFennecConnection,
} from '../src/transports/webrtc-transport.ts';

describe('direct WebRTC transport', () => {
  it('maps a safe client session to the generic connection contract', () => {
    expect(toFennecConnection({
      sessionId: 'session-1',
      signalingUrl: 'https://fennec.test/v1/sessions/session-1/offer',
      accessToken: 'short-lived-token',
      expiresAt: '2026-08-17T16:00:00Z',
    })).toEqual({
      connectionUrl: 'https://fennec.test/v1/sessions/session-1/offer',
      accessToken: 'short-lived-token',
    });
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
