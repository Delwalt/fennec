export { createFennecClient, FennecClient } from './fennec-client.ts';
export { listMicrophones } from './microphone/list-microphones.ts';
export type { VoiceTransport, VoiceTransportState } from './transports/voice-transport.ts';
export type {
  FennecAudioPlaybackState,
  FennecClientConfig,
  FennecClientState,
  FennecConnection,
  FennecConnectionState,
  FennecMicrophone,
  FennecMicrophoneState,
  FennecStateListener,
  FennecTranscript,
  FennecVoiceState,
} from './types.ts';
