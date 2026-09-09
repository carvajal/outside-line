"""Round-trip sanity check for the μ-law/PCM resampling math.

Coverage is deliberately narrow: ``audio_bridge`` is the one non-obvious,
self-contained piece of logic here. Everything else is validated by a
real call.
"""

from __future__ import annotations

import numpy as np

from outside_line.audio_bridge import (
    NOKIA_HUM_ULAW_LOOP,
    RINGBACK_ULAW_FRAME_BYTES,
    RINGBACK_ULAW_LOOP,
    mulaw_decode,
    mulaw_encode,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_pcm16,
)


def test_roundtrip_440hz_sine_preserves_signal() -> None:
    sr_telephony = 8000
    sr_realtime = 24000
    duration_s = 0.1
    freq = 440.0
    n = int(sr_telephony * duration_s)
    t = np.arange(n) / sr_telephony
    amplitude = 8000  # well above μ-law quantization floor, well below clip
    sine_int16 = (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)

    # Twilio → us: int16 8 kHz → μ-law 8 kHz → PCM16 24 kHz
    mulaw_in = mulaw_encode(sine_int16)
    assert len(mulaw_in) == n

    pcm_24k = mulaw_to_pcm16(mulaw_in, sr_in=sr_telephony, sr_out=sr_realtime)
    assert len(pcm_24k) // 2 == 3 * n  # 8 kHz → 24 kHz is exactly 3×

    # us → Twilio: PCM16 24 kHz → μ-law 8 kHz
    mulaw_out = pcm16_to_mulaw(pcm_24k, sr_in=sr_realtime, sr_out=sr_telephony)
    assert abs(len(mulaw_out) - n) <= 1, (
        "resample edge effects shouldn't drop > 1 sample"
    )

    # Compare the two μ-law frames once decoded back to int16. μ-law
    # quantization + soxr resample are both lossy, so we just bound the
    # mean absolute error generously.
    decoded_in = mulaw_decode(mulaw_in).astype(np.float64)
    decoded_out = mulaw_decode(mulaw_out[:n]).astype(np.float64)
    mae = float(np.mean(np.abs(decoded_in - decoded_out)))
    assert mae < 500, f"round-trip MAE {mae:.1f} suggests codec/resample drift"


def test_resample_pcm16_roundtrip_24k_48k_24k() -> None:
    """Bridge rates: 24 kHz (OpenAI Realtime) ↔ 48 kHz (Telegram WebRTC)."""
    sr_a = 24000
    sr_b = 48000
    duration_s = 0.1
    freq = 440.0
    n = int(sr_a * duration_s)
    t = np.arange(n) / sr_a
    sine_int16 = (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    pcm_24k_in = sine_int16.tobytes()

    pcm_48k = resample_pcm16(pcm_24k_in, sr_a, sr_b)
    assert len(pcm_48k) // 2 == 2 * n, "24 kHz → 48 kHz is exactly 2×"

    pcm_24k_out = resample_pcm16(pcm_48k, sr_b, sr_a)
    # Up-then-down can drift by a sample at the edges.
    assert abs((len(pcm_24k_out) // 2) - n) <= 2

    out = np.frombuffer(pcm_24k_out, dtype=np.int16)[: min(n, len(pcm_24k_out) // 2)]
    inp = sine_int16[: len(out)]
    mae = float(np.mean(np.abs(inp.astype(np.float64) - out.astype(np.float64))))
    assert mae < 150, f"24k↔48k round-trip MAE {mae:.1f} suggests resample drift"


def test_mulaw_to_pcm16_direct_8_to_48() -> None:
    """BRIDGE pump path: μ-law 8 kHz → PCM16 48 kHz in one soxr step
    (no 24 kHz intermediate). Round-trip MAE must stay within the same
    bound as the 8 ↔ 24 path, since dropping a resample stage only
    reduces accumulated drift.
    """
    sr_telephony = 8000
    sr_telegram = 48000
    duration_s = 0.1
    freq = 440.0
    n = int(sr_telephony * duration_s)
    t = np.arange(n) / sr_telephony
    sine_int16 = (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)

    mulaw_in = mulaw_encode(sine_int16)
    pcm_48k = mulaw_to_pcm16(mulaw_in, sr_in=sr_telephony, sr_out=sr_telegram)
    assert len(pcm_48k) // 2 == 6 * n  # 8 kHz → 48 kHz is exactly 6×

    mulaw_out = pcm16_to_mulaw(pcm_48k, sr_in=sr_telegram, sr_out=sr_telephony)
    assert abs(len(mulaw_out) - n) <= 1

    decoded_in = mulaw_decode(mulaw_in).astype(np.float64)
    decoded_out = mulaw_decode(mulaw_out[:n]).astype(np.float64)
    mae = float(np.mean(np.abs(decoded_in - decoded_out)))
    assert mae < 500, f"8↔48 round-trip MAE {mae:.1f} suggests resample drift"


def test_resample_pcm16_passthrough_when_rates_equal() -> None:
    buf = (np.arange(64, dtype=np.int16) * 100).tobytes()
    assert resample_pcm16(buf, 48000, 48000) is buf


def test_outbound_caller_to_contact_frame_is_1920_bytes() -> None:
    """``pump_to_telegram`` divides each Twilio 20 ms frame's upsampled
    output in half (``len(pcm48) // 2``) on the assumption that the
    result is exactly 1920 bytes — two 960 B (10 ms at 48 kHz mono int16)
    halves matching ntgcalls's 10 ms granule.

    A previous attempt to put a stateful ``soxr.ResampleStream`` on this
    direction (commit ``c966992``, reverted in ``c2e6c8c``) broke this
    invariant: ``ResampleStream`` startup transients produce variable-
    length output for the first few 10 ms input chunks, which made
    ``len // 2`` no longer correspond to a 10 ms ntgcalls granule.
    ntgcalls pad / truncate / PLC-fill produced the "helicopter noise"
    symptom on the contact.

    This test locks the byte-exact stateless ``mulaw_to_pcm16(_, sr_out=48000)``
    invariant so a future "unify the resamplers" refactor can't silently
    reintroduce the regression. ADR 0014 carries the rejected-approach
    record and the evidence chain.
    """
    for _trial in range(10):
        # Random μ-law 20 ms frame — content-independent invariant.
        mulaw_20ms = np.random.randint(0, 256, 160, dtype=np.uint8).tobytes()
        pcm48 = mulaw_to_pcm16(mulaw_20ms, sr_in=8000, sr_out=48000)
        assert len(pcm48) == 1920, (
            f"expected 1920 B (two 10 ms halves at 48 kHz mono int16), "
            f"got {len(pcm48)} — the pump_to_telegram granule invariant "
            "is broken"
        )


def test_ringback_loop_is_frame_aligned() -> None:
    # Twilio frames are 20 ms at 8 kHz = 160 μ-law bytes. The ringback
    # driver strides through the loop in 160-byte chunks, so the loop
    # length must be a clean multiple of the frame size.
    assert RINGBACK_ULAW_FRAME_BYTES == 160
    assert len(RINGBACK_ULAW_LOOP) % RINGBACK_ULAW_FRAME_BYTES == 0
    # Plan target: ~3.5 s. 3.5 s × 8000 = 28000 samples; allow some slack
    # for the frame-alignment pad.
    assert 27000 <= len(RINGBACK_ULAW_LOOP) <= 30000


def test_ringback_loop_has_audible_peaks_and_quiet_tail() -> None:
    # The first ~600 ms (3 tones + 2 gaps) should contain real audio;
    # the last ~2 s should be effectively silence (0 in PCM16).
    pcm = mulaw_decode(RINGBACK_ULAW_LOOP)
    head = pcm[: 8000 * 600 // 1000]  # first 600 ms
    tail = pcm[-8000 * 2 :]  # last 2 s
    head_peak = int(np.abs(head).max())
    tail_peak = int(np.abs(tail).max())
    # Audible — well above μ-law quantization floor.
    assert head_peak > 3000
    # ... but well below clip — we ship at amplitude ~0.30.
    assert head_peak < 20000
    # Silence tail is bit-exact zero before encode; μ-law encodes 0 as a
    # nonzero byte that decodes back to a tiny value, not literally 0.
    assert tail_peak < 100


def test_nokia_hum_loop_is_frame_aligned() -> None:
    # Same Twilio frame-cadence invariant as the ringback above. The
    # driver strides through the loop in 160-byte chunks regardless of
    # which loop is active, so any RING-phase audio must be a clean
    # multiple of the frame size.
    assert len(NOKIA_HUM_ULAW_LOOP) % RINGBACK_ULAW_FRAME_BYTES == 0
    # Plan target: total loop in the 4–6 s range — long enough for the
    # melody to read as a discrete phrase, short enough that the silence
    # tail doesn't read as "did the line drop?".
    seconds = len(NOKIA_HUM_ULAW_LOOP) / 8000
    assert 4.0 <= seconds <= 6.0


def test_nokia_hum_loop_has_audible_melody_and_quiet_tail() -> None:
    # Melody body should have real audio (well above quantization noise,
    # well below clip); tail should be near silence so successive loop
    # iterations read as discrete phrases.
    pcm = mulaw_decode(NOKIA_HUM_ULAW_LOOP)
    # Last ~1 s — sized to match the 1500 ms silence tail with slack.
    tail = pcm[-8000:]
    # Everything before the tail is the melody body.
    melody = pcm[: len(pcm) - 8000]
    melody_peak = int(np.abs(melody).max())
    tail_peak = int(np.abs(tail).max())
    # Audible at the same amplitude as the ringback (peak ~0.30 × int16
    # max), allowing for the +6 dB headroom the harmonic stack consumes.
    assert melody_peak > 3000
    assert melody_peak < 20000
    # Tail is silence-padded before encode; the breath noise lives inside
    # the per-note envelopes and shouldn't leak past them.
    assert tail_peak < 100
