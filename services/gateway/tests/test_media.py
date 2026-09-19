from array import array

import numpy as np

from fennec_gateway.media import AssistantAudioTrack, DUCK_GAIN_DB


def _constant_pcm(value: int, count: int) -> bytes:
    return array("h", [value] * count).tobytes()


async def _read_frames(track: AssistantAudioTrack, count: int) -> list[int]:
    samples: list[int] = []
    for _ in range(count):
        pcm = track._apply_gain(track._next_pcm(0.0))
        samples.extend(np.frombuffer(pcm, dtype="<i2").tolist())
    return samples


async def test_duck_ramps_gain_down_without_a_discontinuity() -> None:
    track = AssistantAudioTrack()
    track.begin_generation("g1")
    frame = _constant_pcm(20_000, track.samples_per_frame)
    await track.enqueue_pcm(generation_id="g1", pcm=frame * 6)

    track.duck()
    samples = await _read_frames(track, 4)

    diffs = [abs(b - a) for a, b in zip(samples, samples[1:])]
    # A step change in gain would jump by thousands in a single sample; the ramp
    # keeps every step small, which is what makes it inaudible as a click.
    assert max(diffs) < 50
    # Monotonically settling toward the duck target - not a single jump partway
    # through, and not overshooting past it.
    assert samples == sorted(samples, reverse=True)
    target_gain = 10 ** (DUCK_GAIN_DB / 20)
    assert samples[-1] == int(20_000 * target_gain)


async def test_restore_ramps_gain_back_to_unity_without_a_discontinuity() -> None:
    track = AssistantAudioTrack()
    track.begin_generation("g1")
    frame = _constant_pcm(20_000, track.samples_per_frame)
    await track.enqueue_pcm(generation_id="g1", pcm=frame * 12)

    track.duck()
    await _read_frames(track, 4)  # let the duck ramp fully settle first
    track.restore()
    samples = await _read_frames(track, 4)

    diffs = [abs(b - a) for a, b in zip(samples, samples[1:])]
    assert max(diffs) < 50
    assert samples == sorted(samples)
    assert samples[-1] == 20_000


async def test_new_generation_starts_undamped_even_after_a_duck() -> None:
    track = AssistantAudioTrack()
    track.begin_generation("g1")
    frame = _constant_pcm(20_000, track.samples_per_frame)
    await track.enqueue_pcm(generation_id="g1", pcm=frame * 6)
    track.duck()
    await _read_frames(track, 4)
    assert track.gain < 1.0

    track.begin_generation("g2")
    await track.enqueue_pcm(generation_id="g2", pcm=frame * 2)
    samples = await _read_frames(track, 1)

    assert track.gain == 1.0
    assert samples[0] == 20_000


async def test_recv_applies_duck_gain_to_the_frames_it_actually_returns() -> None:
    """The tests above call _apply_gain(track._next_pcm(...)) directly, which
    proves the ramp arithmetic but not that recv() - the method something is
    actually pulling frames from over the wire - applies it. If _apply_gain
    were dropped from recv() in a later refactor, ducking would silently stop
    working end to end and the tests above would still pass. This one reads
    samples off the AudioFrame recv() actually returns instead."""
    track = AssistantAudioTrack()
    track.begin_generation("g1")
    frame = _constant_pcm(20_000, track.samples_per_frame)
    await track.enqueue_pcm(generation_id="g1", pcm=frame * 8)

    # recv() paces itself against wall-clock time via _started_at, so each call
    # after the first genuinely sleeps for one frame's duration (20ms) - keep
    # the frame count small rather than disabling that pacing.
    before_duck = await track.recv()
    track.duck()
    after_duck = await track.recv()

    before_samples = np.frombuffer(bytes(before_duck.planes[0]), dtype="<i2")
    after_samples = np.frombuffer(bytes(after_duck.planes[0]), dtype="<i2")

    assert before_samples[0] == 20_000
    # The ramp starts attenuating from the very first sample after duck() is
    # called, so even the start of this frame is already below the untouched
    # input level, and it keeps falling within the frame.
    assert after_samples[0] < 20_000
    assert after_samples[-1] < after_samples[0]
