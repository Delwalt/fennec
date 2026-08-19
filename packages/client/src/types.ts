export type FennecConnection = {
  connectionUrl: string;
  accessToken: string;
};

export type FennecConnectionState =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting';

export type FennecVoiceState =
  | 'connecting'
  | 'ready'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'failed';

export type FennecMicrophoneState = 'stopped' | 'starting' | 'active' | 'muted';

export type FennecAudioPlaybackState = 'blocked' | 'stopped' | 'playing';

export type FennecMicrophone = {
  deviceId: string;
  label: string;
};

export type FennecTranscript = {
  id: string;
  speaker: 'user' | 'assistant';
  text: string;
  isFinal: boolean;
};

export type FennecClientState = {
  connectionState: FennecConnectionState;
  voiceState: FennecVoiceState;
  microphoneState: FennecMicrophoneState;
  audioPlaybackState: FennecAudioPlaybackState;
  microphones: FennecMicrophone[];
  selectedMicrophoneId?: string;
  localAudioLevel: number;
  remoteAudioLevel: number;
  transcripts: FennecTranscript[];
  error?: string;
};

export type FennecClientConfig = {
  transport: import('./transports/voice-transport.ts').VoiceTransport;
  connection: () => Promise<FennecConnection>;
};

export type FennecStateListener = (state: FennecClientState) => void;
