from __future__ import annotations

from io import BytesIO
from math import log10
import wave

from av import AudioResampler, open as open_media
import numpy as np


SILENT_DBFS = -120.0


def rms_power(pcm: bytes) -> float:
    """Mean-square power of PCM16 mono audio normalized to full scale."""
    if len(pcm) < 2:
        return 0.0
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32_768.0
    return float(np.mean(np.square(samples)))


def power_dbfs(power: float) -> float:
    return round(10 * log10(power), 1) if power > 0 else SILENT_DBFS


def rms_dbfs(pcm: bytes) -> float:
    """Loudness of PCM16 mono audio relative to digital full scale.

    Residual echo that survives browser AEC arrives far quieter than the person
    in front of the microphone; the level is the only signal that separates them.
    """
    return power_dbfs(rms_power(pcm))


def pcm16_mono_wav(pcm: bytes, *, sample_rate: int = 16_000) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def decode_audio_to_pcm16_mono(audio: bytes, *, sample_rate: int) -> bytes:
    output = bytearray()
    resampler = AudioResampler(format="s16", layout="mono", rate=sample_rate)
    with open_media(BytesIO(audio), mode="r") as container:
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                output.extend(bytes(converted.planes[0])[: converted.samples * 2])
        for converted in resampler.resample(None):
            output.extend(bytes(converted.planes[0])[: converted.samples * 2])
    return bytes(output)
