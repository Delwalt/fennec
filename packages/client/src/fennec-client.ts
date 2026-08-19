import { listMicrophones } from './microphone/list-microphones.ts';
import type {
  FennecClientConfig,
  FennecClientState,
  FennecStateListener,
} from './types.ts';
import type { VoiceTransportState } from './transports/voice-transport.ts';

export class FennecClient {
  private readonly listeners = new Set<FennecStateListener>();
  private readonly config: FennecClientConfig;
  private state: FennecClientState;
  private unsubscribeTransport: () => void;
  private removeDeviceListener: (() => void) | undefined;
  private destroyed = false;
  private destroyPromise: Promise<void> | undefined;

  constructor(config: FennecClientConfig) {
    this.config = config;
    this.state = clientState(config.transport.getState(), []);
    this.unsubscribeTransport = config.transport.subscribe((transportState) => {
      this.state = clientState(transportState, this.state.microphones);
      this.emit();
    });

    if (typeof navigator !== 'undefined' && navigator.mediaDevices) {
      const refresh = () => void this.refreshMicrophones();
      navigator.mediaDevices.addEventListener('devicechange', refresh);
      this.removeDeviceListener = () =>
        navigator.mediaDevices.removeEventListener('devicechange', refresh);
    }
  }

  getState = (): FennecClientState => this.state;

  subscribe = (listener: FennecStateListener): (() => void) => {
    this.assertActive();
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  async connect(): Promise<void> {
    await this.perform(async () => {
      await this.config.transport.connect(await this.config.connection());
      await this.refreshMicrophones();
    });
  }

  async disconnect(): Promise<void> {
    await this.perform(() => this.config.transport.disconnect());
  }

  async startMicrophone(): Promise<void> {
    await this.perform(async () => {
      await this.config.transport.startMicrophone();
      await this.refreshMicrophones();
    });
  }

  async stopMicrophone(): Promise<void> {
    await this.perform(() => this.config.transport.stopMicrophone());
  }

  async mute(): Promise<void> {
    await this.perform(() => this.config.transport.setMicrophoneMuted(true));
  }

  async unmute(): Promise<void> {
    await this.perform(() => this.config.transport.setMicrophoneMuted(false));
  }

  async listMicrophones(): Promise<readonly FennecClientState['microphones'][number][]> {
    this.assertActive();
    await this.refreshMicrophones();
    return this.state.microphones;
  }

  async selectMicrophone(deviceId: string): Promise<void> {
    await this.perform(async () => {
      await this.config.transport.selectMicrophone(deviceId);
      await this.refreshMicrophones();
    });
  }

  async enableAudioPlayback(): Promise<void> {
    await this.perform(() => this.config.transport.enableAudioPlayback());
  }

  destroy(): Promise<void> {
    if (this.destroyPromise) return this.destroyPromise;
    this.destroyed = true;
    this.unsubscribeTransport();
    this.removeDeviceListener?.();
    this.listeners.clear();
    this.destroyPromise = this.config.transport.dispose();
    return this.destroyPromise;
  }

  private async refreshMicrophones(): Promise<void> {
    const microphones = await listMicrophones();
    if (this.destroyed) return;
    this.state = { ...this.state, microphones };
    this.emit();
  }

  private async perform(action: () => Promise<void>): Promise<void> {
    this.assertActive();
    this.state = withoutError(this.state);
    this.emit();

    try {
      await action();
    } catch (cause) {
      this.state = {
        ...this.state,
        error: cause instanceof Error ? cause.message : 'Fennec could not complete the action.',
      };
      this.emit();
      throw cause;
    }
  }

  private emit(): void {
    for (const listener of this.listeners) listener(this.state);
  }

  private assertActive(): void {
    if (this.destroyed) throw new Error('Fennec client has been destroyed.');
  }
}

export function createFennecClient(config: FennecClientConfig): FennecClient {
  return new FennecClient(config);
}

function clientState(
  transport: VoiceTransportState,
  microphones: FennecClientState['microphones'],
): FennecClientState {
  return {
    connectionState: transport.connectionState,
    voiceState: transport.voiceState,
    microphoneState: transport.microphoneState,
    audioPlaybackState: transport.audioPlaybackState,
    microphones,
    ...(transport.selectedMicrophoneId
      ? { selectedMicrophoneId: transport.selectedMicrophoneId }
      : {}),
    localAudioLevel: transport.localAudioLevel,
    remoteAudioLevel: transport.remoteAudioLevel,
    transcripts: transport.transcripts,
    ...(transport.error ? { error: transport.error } : {}),
  };
}

function withoutError(state: FennecClientState): FennecClientState {
  const { error: _error, ...next } = state;
  return next;
}
