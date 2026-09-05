import { retainTranscript } from '../conversation-transcripts.ts';
import type {
  FennecConnection,
  FennecIceServer,
  FennecTranscript,
  FennecVoiceState,
} from '../types.ts';
import type { VoiceTransport, VoiceTransportState } from './voice-transport.ts';

const MAX_TRANSCRIPTS = 200;

export type FennecClientSession = {
  sessionId: string;
  signalingUrl: string;
  accessToken: string;
  expiresAt: string;
  iceServers?: FennecIceServer[];
};

export type WebRTCTransportOptions = {
  fetch?: typeof fetch;
  /** Receives the session's ICE configuration; ignore it only when supplying a fake. */
  createPeerConnection?: (configuration: RTCConfiguration) => RTCPeerConnection;
  createAudioElement?: () => HTMLAudioElement;
  microphoneProcessing?: FennecMicrophoneProcessing;
};

export type FennecMicrophoneProcessing = {
  echoCancellation?: boolean;
  noiseSuppression?: boolean;
  autoGainControl?: boolean;
};

export function toFennecConnection(session: FennecClientSession): FennecConnection {
  return {
    connectionUrl: session.signalingUrl,
    accessToken: session.accessToken,
    iceServers: session.iceServers ?? [],
  };
}

export function createWebRTCTransport(
  options: WebRTCTransportOptions = {},
): VoiceTransport {
  return new WebRTCTransport(options);
}

class WebRTCTransport implements VoiceTransport {
  private readonly fetch: typeof fetch;
  private readonly createPeerConnection: (configuration: RTCConfiguration) => RTCPeerConnection;
  private readonly createAudioElement: () => HTMLAudioElement;
  private readonly microphoneProcessing: Required<FennecMicrophoneProcessing>;
  private readonly listeners = new Set<(state: VoiceTransportState) => void>();
  private readonly transcripts = new Map<string, FennecTranscript>();
  private peer: RTCPeerConnection | undefined;
  private control: RTCDataChannel | undefined;
  private sender: RTCRtpSender | undefined;
  private microphoneStream: MediaStream | undefined;
  private remoteAudio: HTMLAudioElement | undefined;
  private connectionState: VoiceTransportState['connectionState'] = 'disconnected';
  private voiceState: FennecVoiceState = 'connecting';
  private playbackState: VoiceTransportState['audioPlaybackState'] = 'stopped';
  private selectedMicrophoneId: string | undefined;
  private startingMicrophone = false;
  private error: string | undefined;
  private disposed = false;
  private disposePromise: Promise<void> | undefined;

  constructor(options: WebRTCTransportOptions) {
    // Bound to the global. Held on the instance, `this.fetch(...)` would call it with the
    // transport as its receiver, and a browser rejects that with "Failed to execute
    // 'fetch' on 'Window': Illegal invocation".
    this.fetch = (options.fetch ?? fetch).bind(globalThis);
    this.createPeerConnection =
      options.createPeerConnection ?? ((configuration) => new RTCPeerConnection(configuration));
    this.createAudioElement =
      options.createAudioElement ??
      (() => {
        const element = document.createElement('audio');
        element.autoplay = true;
        element.setAttribute('playsinline', '');
        return element;
      });
    this.microphoneProcessing = {
      echoCancellation: options.microphoneProcessing?.echoCancellation ?? true,
      noiseSuppression: options.microphoneProcessing?.noiseSuppression ?? true,
      autoGainControl: options.microphoneProcessing?.autoGainControl ?? true,
    };
  }

  async connect(connection: FennecConnection): Promise<void> {
    this.assertActive();
    if (this.peer) await this.disconnect();
    this.error = undefined;
    this.connectionState = 'connecting';
    this.voiceState = 'connecting';
    this.emit();

    // The gateway mints a TURN credential per session; without it the browser offers only
    // host candidates and never reaches a gateway on another machine.
    const peer = this.createPeerConnection({ iceServers: connection.iceServers ?? [] });
    const control = peer.createDataChannel('fennec-control', { ordered: true });
    const transceiver = peer.addTransceiver('audio', { direction: 'sendrecv' });
    this.peer = peer;
    this.control = control;
    this.sender = transceiver.sender;
    this.bindPeer(peer, control);

    try {
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      await waitForIceGathering(peer);
      if (!peer.localDescription) throw new Error('Fennec could not create a WebRTC offer.');
      const response = await this.fetch(connection.connectionUrl, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${connection.accessToken}`,
          'content-type': 'application/json',
        },
        body: JSON.stringify(peer.localDescription),
      });
      if (!response.ok) {
        throw new Error(`Fennec signaling failed with HTTP ${response.status}.`);
      }
      const answer = await response.json() as RTCSessionDescriptionInit;
      await peer.setRemoteDescription(answer);
    } catch (cause) {
      this.error = cause instanceof Error ? cause.message : 'Fennec WebRTC connection failed.';
      await this.disconnect();
      throw cause;
    }
  }

  async disconnect(): Promise<void> {
    this.control?.close();
    this.peer?.close();
    this.stopMicrophoneTracks();
    if (this.remoteAudio) {
      this.remoteAudio.pause();
      this.remoteAudio.srcObject = null;
      this.remoteAudio.remove();
    }
    this.peer = undefined;
    this.control = undefined;
    this.sender = undefined;
    this.remoteAudio = undefined;
    this.connectionState = 'disconnected';
    this.voiceState = 'connecting';
    this.playbackState = 'stopped';
    this.emit();
  }

  async startMicrophone(): Promise<void> {
    this.assertConnected();
    this.startingMicrophone = true;
    this.emit();
    try {
      await this.replaceMicrophone(this.selectedMicrophoneId);
    } finally {
      this.startingMicrophone = false;
      this.emit();
    }
  }

  async stopMicrophone(): Promise<void> {
    await this.sender?.replaceTrack(null);
    this.stopMicrophoneTracks();
    this.emit();
  }

  async setMicrophoneMuted(muted: boolean): Promise<void> {
    const track = this.microphoneStream?.getAudioTracks()[0];
    if (!track && !muted) {
      await this.startMicrophone();
      return;
    }
    if (track) track.enabled = !muted;
    this.emit();
  }

  async selectMicrophone(deviceId: string): Promise<void> {
    this.selectedMicrophoneId = deviceId;
    if (this.microphoneStream) await this.replaceMicrophone(deviceId);
    this.emit();
  }

  async enableAudioPlayback(): Promise<void> {
    if (!this.remoteAudio) return;
    await this.remoteAudio.play();
    this.playbackState = this.voiceState === 'speaking' ? 'playing' : 'stopped';
    this.emit();
  }

  dispose(): Promise<void> {
    if (this.disposePromise) return this.disposePromise;
    this.disposed = true;
    this.disposePromise = this.disconnect().finally(() => this.listeners.clear());
    return this.disposePromise;
  }

  getState(): VoiceTransportState {
    const microphone = this.microphoneStream?.getAudioTracks()[0];
    return {
      connectionState: this.connectionState,
      voiceState: this.voiceState,
      microphoneState: this.startingMicrophone
        ? 'starting'
        : !microphone
          ? 'stopped'
          : microphone.enabled
            ? 'active'
            : 'muted',
      audioPlaybackState: this.playbackState,
      ...(this.selectedMicrophoneId ? { selectedMicrophoneId: this.selectedMicrophoneId } : {}),
      localAudioLevel: 0,
      remoteAudioLevel: 0,
      transcripts: [...this.transcripts.values()],
      ...(this.error ? { error: this.error } : {}),
    };
  }

  subscribe(listener: (state: VoiceTransportState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private bindPeer(peer: RTCPeerConnection, control: RTCDataChannel): void {
    peer.addEventListener('connectionstatechange', () => {
      this.connectionState = mapConnectionState(peer.connectionState);
      if (peer.connectionState === 'failed') {
        this.error = 'The direct WebRTC connection failed.';
        this.voiceState = 'failed';
      }
      this.emit();
    });
    peer.addEventListener('track', (event) => {
      if (event.track.kind !== 'audio') return;
      const audio = this.createAudioElement();
      audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
      this.remoteAudio?.remove();
      this.remoteAudio = audio;
      void audio.play().then(
        () => {
          this.playbackState = this.voiceState === 'speaking' ? 'playing' : 'stopped';
          this.emit();
        },
        () => {
          this.playbackState = 'blocked';
          this.emit();
        },
      );
    });
    control.addEventListener('message', (event) => this.applyControlEvent(event.data));
  }

  private applyControlEvent(raw: unknown): void {
    if (typeof raw !== 'string') return;
    let event: Record<string, unknown>;
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return;
      event = parsed as Record<string, unknown>;
    } catch {
      return;
    }

    if (event.type === 'state.changed' && typeof event.state === 'string') {
      this.voiceState = mapVoiceState(event.state);
      this.playbackState = this.voiceState === 'speaking' ? 'playing' : 'stopped';
      if (this.voiceState === 'speaking') this.tryAudioPlayback();
    } else if (
      event.type === 'transcript.final' &&
      typeof event.turn_id === 'string' &&
      typeof event.text === 'string'
    ) {
      retainTranscript(
        this.transcripts,
        { id: event.turn_id, speaker: 'user', text: event.text, isFinal: true },
        MAX_TRANSCRIPTS,
      );
    } else if (
      event.type === 'assistant.text.delta' &&
      typeof event.generation_id === 'string' &&
      typeof event.text === 'string'
    ) {
      const existing = this.transcripts.get(event.generation_id);
      retainTranscript(
        this.transcripts,
        {
          id: event.generation_id,
          speaker: 'assistant',
          text: `${existing?.text ?? ''}${event.text}`,
          isFinal: false,
        },
        MAX_TRANSCRIPTS,
      );
    } else if (event.type === 'assistant.done' && typeof event.generation_id === 'string') {
      const existing = this.transcripts.get(event.generation_id);
      if (existing) this.transcripts.set(event.generation_id, { ...existing, isFinal: true });
    } else if (event.type === 'assistant.cancelled' && typeof event.generation_id === 'string') {
      this.transcripts.delete(event.generation_id);
      this.remoteAudio?.pause();
      this.playbackState = 'stopped';
    } else if (event.type === 'error') {
      this.error = `Fennec conversation error: ${String(event.component ?? event.code ?? 'unknown')}`;
      this.voiceState = 'failed';
    }
    this.emit();
  }

  private async replaceMicrophone(deviceId?: string): Promise<void> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        ...this.microphoneProcessing,
        channelCount: 1,
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
      video: false,
    });
    const track = stream.getAudioTracks()[0];
    if (!track) {
      stream.getTracks().forEach((candidate) => candidate.stop());
      throw new Error('The selected microphone did not provide an audio track.');
    }
    await this.sender?.replaceTrack(track);
    this.stopMicrophoneTracks();
    this.microphoneStream = stream;
    this.selectedMicrophoneId = track.getSettings().deviceId ?? deviceId;
  }

  private tryAudioPlayback(): void {
    if (!this.remoteAudio) return;
    void this.remoteAudio.play().then(
      () => {
        this.playbackState = this.voiceState === 'speaking' ? 'playing' : 'stopped';
        this.emit();
      },
      () => {
        this.playbackState = 'blocked';
        this.emit();
      },
    );
  }

  private stopMicrophoneTracks(): void {
    this.microphoneStream?.getTracks().forEach((track) => track.stop());
    this.microphoneStream = undefined;
  }

  private assertConnected(): void {
    this.assertActive();
    if (!this.peer || !this.sender) throw new Error('Connect Fennec before starting a microphone.');
  }

  private assertActive(): void {
    if (this.disposed) throw new Error('WebRTC transport has been disposed.');
  }

  private emit(): void {
    const state = this.getState();
    for (const listener of this.listeners) listener(state);
  }
}

/** Long enough for a relay allocation on a healthy network, short enough that a stalled
 *  one is not mistaken for progress. */
const ICE_GATHERING_TIMEOUT_MS = 5_000;

/** Waits for the candidates, but never forever. Gathering only completes when every
 *  candidate has resolved or timed out, and configuring a TURN server adds an allocation
 *  that can hang — on a network that silently drops it, an unbounded wait leaves the
 *  caller connecting with no error and no end. Whatever was gathered is offered instead. */
function waitForIceGathering(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      peer.removeEventListener('icegatheringstatechange', listener);
      resolve();
    };
    const listener = () => {
      if (peer.iceGatheringState === 'complete') done();
    };
    const timer = setTimeout(done, ICE_GATHERING_TIMEOUT_MS);
    peer.addEventListener('icegatheringstatechange', listener);
  });
}

function mapConnectionState(state: RTCPeerConnectionState): VoiceTransportState['connectionState'] {
  if (state === 'connected') return 'connected';
  if (state === 'connecting' || state === 'new') return 'connecting';
  if (state === 'disconnected') return 'reconnecting';
  return 'disconnected';
}

function mapVoiceState(state: string): FennecVoiceState {
  if (state === 'listening') return 'listening';
  if (state === 'transcribing' || state === 'waiting') return 'thinking';
  if (state === 'speaking') return 'speaking';
  if (state === 'error') return 'failed';
  return 'ready';
}
