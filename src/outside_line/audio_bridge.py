"""Audio resampling + G.711 μ-law codec used to bridge Twilio (μ-law 8 kHz)
and OpenAI Realtime (PCM16 24 kHz).

Pure functions only — the bridge layers the fan-out + echo state machine on top.

Why hand-rolled μ-law instead of :mod:`audioop`? Python 3.13 removed
``audioop`` from stdlib, and we run 3.14. Rather than pull in
``audioop-lts``, we precompute two small LUTs at module import — small
enough (256 + 65 536 bytes) that the simplicity is worth the memory.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import soxr

__all__ = [
    "CRASH_MESSAGE_ULAW_BYTES",
    "NOKIA_HUM_ULAW_LOOP",
    "PCM48_10MS_FRAME_BYTES",
    "RINGBACK_PCM48_LOOP",
    "RINGBACK_ULAW_FRAME_BYTES",
    "RINGBACK_ULAW_LOOP",
    "WAIT_MESSAGE_PCM48_BYTES",
    "apply_gain",
    "chunk_pcm48_10ms",
    "mulaw_decode",
    "mulaw_encode",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
    "resample_pcm16",
]


def _build_decode_lut() -> np.ndarray:
    lut = np.empty(256, dtype=np.int16)
    for i in range(256):
        b = (~i) & 0xFF
        sign = b & 0x80
        exponent = (b >> 4) & 0x07
        mantissa = b & 0x0F
        magnitude = ((mantissa << 3) + 0x84) << exponent
        magnitude -= 0x84
        lut[i] = -magnitude if sign else magnitude
    return lut


def _encode_single(sample: int) -> int:
    bias = 0x84
    clip = 32635
    sign = 0x80 if sample < 0 else 0
    s = -sample if sample < 0 else sample
    if s > clip:
        s = clip
    s += bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (s & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (s >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _build_encode_lut() -> np.ndarray:
    lut = np.empty(65536, dtype=np.uint8)
    for i in range(65536):
        s = i if i < 32768 else i - 65536
        lut[i] = _encode_single(s)
    return lut


_DECODE_LUT = _build_decode_lut()
_ENCODE_LUT = _build_encode_lut()


def mulaw_decode(buf: bytes) -> np.ndarray:
    """G.711 μ-law bytes → int16 PCM samples (same sample rate)."""
    indices = np.frombuffer(buf, dtype=np.uint8)
    return _DECODE_LUT[indices].copy()


def mulaw_encode(samples: np.ndarray) -> bytes:
    """int16 PCM samples → G.711 μ-law bytes (same sample rate)."""
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    return _ENCODE_LUT[samples.view(np.uint16)].tobytes()


def mulaw_to_pcm16(buf: bytes, sr_in: int = 8000, sr_out: int = 24000) -> bytes:
    """Decode μ-law, resample to ``sr_out``, return int16 little-endian bytes."""
    pcm = mulaw_decode(buf)
    if sr_in != sr_out:
        pcm = soxr.resample(pcm, sr_in, sr_out).astype(np.int16)
    return pcm.tobytes()


def pcm16_to_mulaw(buf: bytes, sr_in: int = 24000, sr_out: int = 8000) -> bytes:
    """Resample int16 PCM down to ``sr_out`` and encode as μ-law."""
    pcm = np.frombuffer(buf, dtype=np.int16)
    if sr_in != sr_out:
        pcm = soxr.resample(pcm, sr_in, sr_out).astype(np.int16)
    return mulaw_encode(pcm)


def resample_pcm16(buf: bytes, sr_in: int, sr_out: int) -> bytes:
    """Resample int16 little-endian PCM between arbitrary sample rates.

    Used by the Telegram bridge to shuttle PCM between the Realtime model
    (24 kHz) and Telegram's WebRTC leg (48 kHz mono int16, per the
    `AudioParameters` we hand to `pytgcalls.play`).
    """
    if sr_in == sr_out:
        return buf
    pcm = np.frombuffer(buf, dtype=np.int16)
    return soxr.resample(pcm, sr_in, sr_out).astype(np.int16).tobytes()


def apply_gain(pcm16_bytes: bytes, gain: float) -> bytes:
    """Linearly scale int16 PCM samples by ``gain`` with clipping."""
    if gain == 1.0:
        return pcm16_bytes
    samples = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) * gain
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Ringback loop — pumped to Twilio during the RING phase of call_contact.
#
# Not a standard PSTN "brrring." A short upward major-third arpeggio
# (A4 → C#5 → E5) at telephone-friendly amplitude, followed by ~3 s of
# silence, the whole pattern looping. Synthesized at module import so the
# RING phase just streams precomputed μ-law frames at 8 kHz / 20 ms cadence.

_RINGBACK_SR = 8000
_RINGBACK_TONE_MS = 120
_RINGBACK_GAP_MS = 80
_RINGBACK_TAIL_SILENCE_MS = 3000
_RINGBACK_AMPLITUDE = 0.30  # peak / int16 max — comfortable on a phone bearer
_RINGBACK_FRAME_MS = 20  # Twilio frame cadence
RINGBACK_ULAW_FRAME_BYTES = _RINGBACK_SR * _RINGBACK_FRAME_MS // 1000  # = 160


def _envelope(n: int, attack: int, release: int) -> np.ndarray:
    """Linear attack-sustain-release in [0, 1] over n samples."""
    env = np.ones(n, dtype=np.float32)
    if attack > 0:
        env[:attack] = np.linspace(0.0, 1.0, attack, endpoint=False)
    if release > 0:
        env[-release:] = np.linspace(1.0, 0.0, release, endpoint=False)
    return env


def _tone(freq_hz: float, ms: int) -> np.ndarray:
    n = _RINGBACK_SR * ms // 1000
    t = np.arange(n, dtype=np.float32) / _RINGBACK_SR
    wave = np.sin(2.0 * np.pi * freq_hz * t)
    env = _envelope(n, attack=_RINGBACK_SR * 20 // 1000, release=_RINGBACK_SR * 20 // 1000)
    pcm = wave * env * _RINGBACK_AMPLITUDE * 32767.0
    return pcm.astype(np.int16)


def _silence(ms: int) -> np.ndarray:
    n = _RINGBACK_SR * ms // 1000
    return np.zeros(n, dtype=np.int16)


def _build_ringback_loop() -> bytes:
    parts = [
        _tone(440.0, _RINGBACK_TONE_MS),   # A4
        _silence(_RINGBACK_GAP_MS),
        _tone(554.37, _RINGBACK_TONE_MS),  # C#5
        _silence(_RINGBACK_GAP_MS),
        _tone(659.25, _RINGBACK_TONE_MS),  # E5
        _silence(_RINGBACK_TAIL_SILENCE_MS),
    ]
    pcm = np.concatenate(parts)
    # Trim/pad so the loop is an exact multiple of the Twilio frame size
    # — the ringback driver can just stride through `RINGBACK_ULAW_LOOP`
    # in 160-byte chunks without worrying about boundaries.
    remainder = len(pcm) % RINGBACK_ULAW_FRAME_BYTES
    if remainder:
        pad = RINGBACK_ULAW_FRAME_BYTES - remainder
        pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
    return mulaw_encode(pcm)


RINGBACK_ULAW_LOOP: bytes = _build_ringback_loop()


# ---------------------------------------------------------------------------
# Nokia-tune hum loop — alternative RING-phase audio that replaces the
# ringback above when ``settings.ring_audio_kind == "hum"``. Same shape
# (μ-law 8 kHz, frame-aligned bytes) so the ring driver swaps between the
# two with a config flag and zero downstream changes.
#
# Why a hum: the previous attempt at humanising the RING phase (commit
# a4e4129, reverted in 5080736) shipped agent-voiced patter rendered via
# OpenAI TTS voice ``ash``, but live the agent runs the Realtime API voice
# ``cedar`` — different voice family, audible mismatch on real calls.
# Humming carries no vowel formants and no language, so a mathematical
# synth can sound "human producing it" without needing to match the agent's
# voice.

# Tárrega "Gran Vals" fragment — the canonical 13-note Nokia tune.
# Stored as ``(semitones from A4, duration_ms)`` so the math
# ``freq = 440 * 2**(semitones/12)`` makes the table compact and obvious.
# Durations: eighth=200 ms / quarter=400 ms / half=800 ms (quarter-note
# ≈ 150 BPM, the canonical Nokia tempo).
_NOKIA_NOTES_SEMITONES_MS: tuple[tuple[int, int], ...] = (
    (+7, 200),  # E5
    (+5, 200),  # D5
    (-3, 400),  # F#4
    (-1, 400),  # G#4
    (+4, 200),  # C#5
    (+2, 200),  # B4
    (-7, 400),  # D4
    (-5, 400),  # E4
    (+2, 200),  # B4
    (0, 200),   # A4
    (-8, 400),  # C#4
    (-5, 400),  # E4
    (0, 800),   # A4
)
# Pitch and tempo shifts on top of the canonical Nokia table above. Kept
# as named knobs so they're visible — taste-tested at -5 semitones
# (perfect fourth down) at 0.75× the 150-BPM baseline (≈200 BPM).
_NOKIA_TRANSPOSE_SEMITONES = -5
_NOKIA_TEMPO_SCALE = 0.75
_NOKIA_NOTE_GAP_MS = 30
_NOKIA_TAIL_MS = 1500
_NOKIA_VIBRATO_HZ = 5.0
_NOKIA_VIBRATO_DEPTH = 0.025
# Peak-normalisation divisor for fundamental + h2 (-6 dB) + h3 (-12 dB).
_NOKIA_HARMONIC_DIVISOR = 1.75
# ~ -30 dB RMS breath-noise floor under the tone.
_NOKIA_NOISE_AMPLITUDE = 0.0316
_NOKIA_ENV_ATTACK_MS = 30
_NOKIA_ENV_RELEASE_MS = 50
_NOKIA_ENV_SUSTAIN = 0.85


def _hum_note(
    freq_hz: float, ms: int, *, rng: np.random.Generator
) -> np.ndarray:
    """One hummed note: fundamental + 2 harmonics + 5 Hz vibrato + breath noise."""
    n = _RINGBACK_SR * ms // 1000
    t = np.arange(n, dtype=np.float32) / _RINGBACK_SR

    # FM with subtle vibrato (±2.5 %, 5 Hz). Integrate the instantaneous
    # frequency via cumsum to get the running phase — gives true frequency
    # modulation, not amplitude modulation.
    f_inst = freq_hz * (
        1.0 + _NOKIA_VIBRATO_DEPTH * np.sin(2.0 * np.pi * _NOKIA_VIBRATO_HZ * t)
    )
    phase = np.cumsum(2.0 * np.pi * f_inst / _RINGBACK_SR)

    # Fundamental + 2nd harmonic (-6 dB) + 3rd harmonic (-12 dB) — the
    # falling harmonic series that gives a vocal-fold buzz character
    # rather than a pure sine "beep".
    wave = (
        np.sin(phase)
        + 0.5 * np.sin(2.0 * phase)
        + 0.25 * np.sin(3.0 * phase)
    )
    wave = wave / _NOKIA_HARMONIC_DIVISOR

    # Low-amplitude breath-noise floor. Shaped by the envelope below so
    # it fades in/out with the note (no audible noise during gaps).
    wave = wave + rng.standard_normal(n).astype(np.float32) * _NOKIA_NOISE_AMPLITUDE

    # Trapezoidal envelope: attack 30 ms → sustain 0.85 → release 50 ms.
    attack = _RINGBACK_SR * _NOKIA_ENV_ATTACK_MS // 1000
    release = _RINGBACK_SR * _NOKIA_ENV_RELEASE_MS // 1000
    env = _envelope(n, attack=attack, release=release) * _NOKIA_ENV_SUSTAIN

    pcm_float = wave * env * _RINGBACK_AMPLITUDE * 32767.0
    return np.clip(pcm_float, -32768, 32767).astype(np.int16)


def _build_nokia_hum_loop() -> bytes:
    # Seeded RNG keeps the loop bit-identical across process boots — the
    # breath noise is the only stochastic input and we want it
    # deterministic so test assertions over the loop bytes are stable.
    rng = np.random.default_rng(seed=42)
    parts: list[np.ndarray] = []
    last = len(_NOKIA_NOTES_SEMITONES_MS) - 1
    for i, (semitones, ms) in enumerate(_NOKIA_NOTES_SEMITONES_MS):
        shifted = semitones + _NOKIA_TRANSPOSE_SEMITONES
        freq_hz = 440.0 * (2.0 ** (shifted / 12.0))
        scaled_ms = int(round(ms * _NOKIA_TEMPO_SCALE))
        parts.append(_hum_note(freq_hz, scaled_ms, rng=rng))
        if i != last:
            parts.append(_silence(_NOKIA_NOTE_GAP_MS))
    parts.append(_silence(_NOKIA_TAIL_MS))
    pcm = np.concatenate(parts)
    # Pad to a multiple of the Twilio frame size so the ring driver can
    # stride in 160-byte chunks (same trick `_build_ringback_loop` uses).
    remainder = len(pcm) % RINGBACK_ULAW_FRAME_BYTES
    if remainder:
        pad = RINGBACK_ULAW_FRAME_BYTES - remainder
        pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
    return mulaw_encode(pcm)


NOKIA_HUM_ULAW_LOOP: bytes = _build_nokia_hum_loop()


# ---------------------------------------------------------------------------
# Directline audio — TG-contact side.
#
# The directline feature plays audio to the Telegram contact's side of the
# call (48 kHz mono int16 PCM, per the AudioParameters the bridge hands to
# pytgcalls). Two pieces:
#
#   * WAIT_MESSAGE_PCM48_BYTES — pre-rendered "One second please — I'm
#     getting them on the line." phrase, rendered once via
#     `scripts/render_directline_audio.py` to a committed `.raw` asset
#     under `src/outside_line/assets/`. Loaded at import; fails soft if the
#     file is missing (logs a warning, exposes empty bytes — the wait
#     phrase is then silently skipped at call time).
#
#   * RINGBACK_PCM48_LOOP — the same A4/C#5/E5 arpeggio + ~3 s silence
#     loop as RINGBACK_ULAW_LOOP, but rendered at 48 kHz mono int16 PCM
#     for the TG side. Synthesized at import time (no asset on disk).
#     Frame-aligned to 960 B = 10 ms so the directline pump can stride
#     in fixed steps without boundary math.
#
# Both pump into TelegramBridge.send_to_contact, which expects 48 kHz
# mono int16. Framing is **10 ms (960 B)** — ntgcalls' send_external_frame
# is 10 ms-granular (WebRTC native Opus tick); a 20 ms submission would
# be reinterpreted as one 10 ms frame and lose half the audio (this is
# the same gotcha the BRIDGE pump in twilio_handler.py solves by
# splitting each Twilio 20 ms frame into two 10 ms halves before
# send_to_contact). The 8 kHz μ-law loops above stay around for the
# caller-side ring driver (Nokia hum / ringback to Twilio).

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_WAIT_MESSAGE_ASSET = _ASSETS_DIR / "directline_wait.raw"
_PCM48_SR = 48000
_PCM48_FRAME_MS = 10
# 48 kHz mono int16 → 480 samples * 2 bytes = 960 B per 10 ms frame.
PCM48_10MS_FRAME_BYTES = _PCM48_SR * _PCM48_FRAME_MS // 1000 * 2


def _load_raw_asset(path: Path, frame_bytes: int, label: str) -> bytes:
    """Read a committed raw-audio asset; fail soft on any problem.

    Returns the raw bytes on success. On any failure (file missing or
    unreadable, byte count not ``frame_bytes``-aligned), logs a warning
    and returns ``b""`` — call sites treat empty as "skip the clip".
    Misaligned bytes would split a sample mid-int16 (PCM) or break the
    fixed-stride clip drivers (μ-law), so we refuse rather than glitch
    on every call.
    """
    log = logging.getLogger(__name__)
    if not path.exists():
        log.warning(
            "%s.asset_missing path=%s — clip will be skipped; re-render "
            "via ./scripts/render_directline_audio.py",
            label, path,
        )
        return b""
    try:
        data = path.read_bytes()
    except OSError as exc:
        log.warning("%s.asset_unreadable path=%s error=%r", label, path, exc)
        return b""
    if len(data) % frame_bytes:
        log.warning(
            "%s.asset_misaligned path=%s bytes=%d (not a multiple of %d) "
            "— clip will be skipped",
            label, path, len(data), frame_bytes,
        )
        return b""
    return data


WAIT_MESSAGE_PCM48_BYTES: bytes = _load_raw_asset(
    _WAIT_MESSAGE_ASSET, PCM48_10MS_FRAME_BYTES, "directline.wait"
)


# ---------------------------------------------------------------------------
# Crash-message asset — Twilio caller side.
#
# On a sidecar SIGSEGV mid-bridge (ADR 0017) the bridge is gone; we play the
# caller one fixed recording ("Sorry — the call dropped on my end. Please
# wait twenty seconds and call again.") and then hang up. Rendered once via
# `scripts/render_directline_audio.py --format ulaw8` to a committed asset —
# 8 kHz mono G.711 μ-law, frame-aligned to 160 B = 20 ms so the caller-side
# clip driver can stride in fixed steps. Loaded at import; fails soft if the
# file is missing (logs a warning, exposes empty bytes — the recording is
# then silently skipped and the call just drops). Deliberately a TTS voice,
# NOT the agent's Realtime `cedar` — a distinct "system" message, and the crash
# path never touches the Realtime session at all.

_CRASH_MESSAGE_ASSET = _ASSETS_DIR / "crash_message.raw"

CRASH_MESSAGE_ULAW_BYTES: bytes = _load_raw_asset(
    _CRASH_MESSAGE_ASSET, RINGBACK_ULAW_FRAME_BYTES, "crash_message"
)


# ---------------------------------------------------------------------------
# Recording-disclosure assets — one per call leg.
#
# When RECORDINGS_ENABLED and RECORDING_DISCLOSURE_ENABLED are both on,
# each leg hears "This call may be recorded." before any recording can
# capture it: the caller leg (μ-law 8 kHz, played by the clip driver in
# twilio_handler before _start_recording is kicked) and the contact leg
# (PCM 48 kHz, played at the top of the bridge phase before the pumps
# start). Rendered via `scripts/render_directline_audio.py`; fail-soft
# like every other committed clip.

_RECORDING_DISCLOSURE_ULAW_ASSET = _ASSETS_DIR / "recording_disclosure_en_ulaw8.raw"
_RECORDING_DISCLOSURE_PCM48_ASSET = _ASSETS_DIR / "recording_disclosure_en_pcm48.raw"

RECORDING_DISCLOSURE_ULAW_BYTES: bytes = _load_raw_asset(
    _RECORDING_DISCLOSURE_ULAW_ASSET, RINGBACK_ULAW_FRAME_BYTES,
    "recording_disclosure_ulaw",
)
RECORDING_DISCLOSURE_PCM48_BYTES: bytes = _load_raw_asset(
    _RECORDING_DISCLOSURE_PCM48_ASSET, PCM48_10MS_FRAME_BYTES,
    "recording_disclosure_pcm48",
)


def _pcm48_tone(freq_hz: float, ms: int) -> np.ndarray:
    """One PCM48 mono int16 tone with 20 ms attack/release linear envelopes."""
    n = _PCM48_SR * ms // 1000
    t = np.arange(n, dtype=np.float32) / _PCM48_SR
    wave = np.sin(2.0 * np.pi * freq_hz * t)
    env = _envelope(
        n,
        attack=_PCM48_SR * 20 // 1000,
        release=_PCM48_SR * 20 // 1000,
    )
    pcm = wave * env * _RINGBACK_AMPLITUDE * 32767.0
    return pcm.astype(np.int16)


def _pcm48_silence(ms: int) -> np.ndarray:
    n = _PCM48_SR * ms // 1000
    return np.zeros(n, dtype=np.int16)


def _build_ringback_pcm48_loop() -> bytes:
    parts = [
        _pcm48_tone(440.0, _RINGBACK_TONE_MS),   # A4
        _pcm48_silence(_RINGBACK_GAP_MS),
        _pcm48_tone(554.37, _RINGBACK_TONE_MS),  # C#5
        _pcm48_silence(_RINGBACK_GAP_MS),
        _pcm48_tone(659.25, _RINGBACK_TONE_MS),  # E5
        _pcm48_silence(_RINGBACK_TAIL_SILENCE_MS),
    ]
    pcm = np.concatenate(parts)
    raw = pcm.tobytes()
    remainder = len(raw) % PCM48_10MS_FRAME_BYTES
    if remainder:
        pad = PCM48_10MS_FRAME_BYTES - remainder
        raw = raw + (b"\x00" * pad)
    return raw


RINGBACK_PCM48_LOOP: bytes = _build_ringback_pcm48_loop()


def chunk_pcm48_10ms(buf: bytes) -> list[bytes]:
    """Split ``buf`` into ``PCM48_10MS_FRAME_BYTES``-sized 10 ms chunks.

    Trailing bytes that don't fill a frame are zero-padded to the boundary
    rather than dropped — the directline pump streams the whole asset
    front-to-back exactly once, so a clipped tail would cut the phrase
    off slightly early on every call.
    """
    if not buf:
        return []
    frame = PCM48_10MS_FRAME_BYTES
    chunks = [buf[i : i + frame] for i in range(0, len(buf), frame)]
    if len(chunks[-1]) < frame:
        chunks[-1] = chunks[-1] + (b"\x00" * (frame - len(chunks[-1])))
    return chunks
