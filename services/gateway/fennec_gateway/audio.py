from __future__ import annotations

from io import BytesIO
import wave

from av import AudioResampler, open as open_media


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
