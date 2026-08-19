import type { FennecMicrophone } from '../types.ts';

export async function listMicrophones(): Promise<FennecMicrophone[]> {
  if (typeof navigator === 'undefined' || !navigator.mediaDevices) {
    return [];
  }

  return describeMicrophones(await navigator.mediaDevices.enumerateDevices());
}

export function describeMicrophones(devices: MediaDeviceInfo[]): FennecMicrophone[] {
  const microphones = devices.filter((device) => device.kind === 'audioinput');

  return microphones.map((device, index) => ({
    deviceId: device.deviceId,
    label: device.label || `Microphone ${index + 1}`,
  }));
}
