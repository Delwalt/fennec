import type {
  FennecAudioPlaybackState,
  FennecConnection,
  FennecConnectionState,
  FennecMicrophoneState,
  FennecTranscript,
  FennecVoiceState,
} from '../types.ts';

export type VoiceTransportState = {
  connectionState: FennecConnectionState;
  voiceState: FennecVoiceState;
  microphoneState: FennecMicrophoneState;
  audioPlaybackState: FennecAudioPlaybackState;
  selectedMicrophoneId?: string;
  localAudioLevel: number;
  remoteAudioLevel: number;
  transcripts: FennecTranscript[];
  error?: string;
};

export interface VoiceTransport {
  connect(connection: FennecConnection): Promise<void>;
  disconnect(): Promise<void>;
  startMicrophone(): Promise<void>;
  stopMicrophone(): Promise<void>;
  setMicrophoneMuted(muted: boolean): Promise<void>;
  selectMicrophone(deviceId: string): Promise<void>;
  enableAudioPlayback(): Promise<void>;
  dispose(): Promise<void>;
  getState(): VoiceTransportState;
  subscribe(listener: (state: VoiceTransportState) => void): () => void;
}
