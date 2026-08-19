import { describe, expect, it, vi } from 'vitest';
import { createFennecClient } from '../src/fennec-client.ts';
import type { FennecConnection } from '../src/types.ts';
import type {
  VoiceTransport,
  VoiceTransportState,
} from '../src/transports/voice-transport.ts';

describe('Fennec client', () => {
  it('connects through a provider-neutral transport and exposes its state', async () => {
    const transport = new FakeTransport();
    const connection: FennecConnection = {
      connectionUrl: 'wss://voice.example.test',
      accessToken: 'short-lived-token',
    };
    const client = createFennecClient({
      transport,
      connection: async () => connection,
    });

    await client.connect();
    await client.startMicrophone();

    expect(transport.connect).toHaveBeenCalledWith(connection);
    expect(client.getState()).toMatchObject({
      connectionState: 'connected',
      microphoneState: 'active',
    });
  });

  it('keeps provider errors in Fennec-owned state', async () => {
    const transport = new FakeTransport();
    transport.selectMicrophone = vi.fn(async () => {
      throw new Error('Device is unavailable.');
    });
    const client = createFennecClient({
      transport,
      connection: async () => ({ connectionUrl: 'wss://voice.test', accessToken: 'token' }),
    });

    await expect(client.selectMicrophone('missing')).rejects.toThrow('unavailable');
    expect(client.getState().error).toBe('Device is unavailable.');
  });

  it('disposes transport resources when destroyed', async () => {
    const transport = new FakeTransport();
    const client = createFennecClient({
      transport,
      connection: async () => ({ connectionUrl: 'wss://voice.test', accessToken: 'token' }),
    });

    await client.connect();
    const firstDestroy = client.destroy();
    const secondDestroy = client.destroy();
    await firstDestroy;

    expect(transport.dispose).toHaveBeenCalledOnce();
    expect(secondDestroy).toBe(firstDestroy);
    await expect(client.connect()).rejects.toThrow('destroyed');
  });
});

class FakeTransport implements VoiceTransport {
  private state: VoiceTransportState = {
    connectionState: 'disconnected',
    voiceState: 'connecting',
    microphoneState: 'stopped',
    audioPlaybackState: 'stopped',
    localAudioLevel: 0,
    remoteAudioLevel: 0,
    transcripts: [],
  };
  private readonly listeners = new Set<(state: VoiceTransportState) => void>();

  connect = vi.fn(async (_connection: FennecConnection) => {
    this.update({ connectionState: 'connected', voiceState: 'ready' });
  });
  disconnect = vi.fn(async () => this.update({ connectionState: 'disconnected' }));
  startMicrophone = vi.fn(async () => this.update({ microphoneState: 'active' }));
  stopMicrophone = vi.fn(async () => this.update({ microphoneState: 'stopped' }));
  setMicrophoneMuted = vi.fn(async (muted: boolean) =>
    this.update({ microphoneState: muted ? 'muted' : 'active' }),
  );
  selectMicrophone = vi.fn(async (deviceId: string) =>
    this.update({ selectedMicrophoneId: deviceId }),
  );
  enableAudioPlayback = vi.fn(async () =>
    this.update({ audioPlaybackState: 'stopped' }),
  );
  dispose = vi.fn(async () => this.update({ connectionState: 'disconnected' }));

  getState(): VoiceTransportState {
    return this.state;
  }

  subscribe(listener: (state: VoiceTransportState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private update(update: Partial<VoiceTransportState>): void {
    this.state = { ...this.state, ...update };
    for (const listener of this.listeners) listener(this.state);
  }
}
