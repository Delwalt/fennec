import { describe, expect, it } from 'vitest';
import { describeMicrophones } from '../src/microphone/list-microphones.ts';

describe('microphone devices', () => {
  it('returns audio inputs with a readable fallback label', () => {
    const devices = [
      device('audioinput', 'built-in', 'MacBook Microphone'),
      device('videoinput', 'camera', 'FaceTime Camera'),
      device('audioinput', 'usb', ''),
    ];

    expect(describeMicrophones(devices)).toEqual([
      { deviceId: 'built-in', label: 'MacBook Microphone' },
      { deviceId: 'usb', label: 'Microphone 2' },
    ]);
  });
});

function device(kind: MediaDeviceKind, deviceId: string, label: string): MediaDeviceInfo {
  return { kind, deviceId, label, groupId: '', toJSON: () => ({}) };
}
