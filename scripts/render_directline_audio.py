#!/usr/bin/env -S uv run python
"""One-shot operator script: render a fixed spoken phrase to a raw audio
asset checked into the source tree under ``src/outside_line/assets/``.

Two target formats, one per call leg:

  * ``--format pcm48`` (default) — 48 kHz mono int16 PCM for the *Telegram
    contact* leg. Used for the directline wait phrase ("One second
    please — I'm getting them on the line."), loaded at import as
    ``audio_bridge.WAIT_MESSAGE_PCM48_BYTES``. ``TelegramBridge.send_to_contact``
    expects exactly this format.
  * ``--format ulaw8`` — 8 kHz mono G.711 μ-law for the *Twilio caller*
    leg. Used for the post-crash message ("Sorry — the call dropped…"),
    loaded as ``audio_bridge.CRASH_MESSAGE_ULAW_BYTES`` and streamed to the
    caller at 160 B / 20 ms.

Either way the phrase is fixed and rarely changes, so we pre-render it via
OpenAI TTS and commit the bytes — no TTS calls happen on the call path.

Run from the repo root:

    ./scripts/render_directline_audio.py
    ./scripts/render_directline_audio.py --voice nova
    ./scripts/render_directline_audio.py --format ulaw8 \\
        --text "Sorry — the call dropped on my end. Please wait twenty seconds and call again." \\
        --out src/outside_line/assets/crash_message.raw

Requires ``OPENAI_API_KEY`` in the environment (``.env`` is loaded
automatically via ``scripts/_dotenv.py``).

OpenAI TTS emits 24 kHz native PCM (per the API docs); we resample once
with ``soxr`` (via ``audio_bridge``) to the target rate and write the result.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_PATH = REPO_ROOT / "src" / "outside_line" / "assets" / "directline_wait.raw"
DEFAULT_TEXT = "One second please — I'm getting them on the line."

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from _dotenv import env  # noqa: E402

# OpenAI TTS natively returns 24 kHz mono int16 PCM via the
# ``response_format="pcm"`` option (per their docs); we resample to
# 48 kHz here so the runtime can splat the bytes straight into
# ``TelegramBridge.send_to_contact`` without per-call resampling.
SRC_SAMPLE_RATE = 24000
DST_SAMPLE_RATE = 48000
DEFAULT_VOICE = "nova"
DEFAULT_MODEL = "gpt-4o-mini-tts"


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--text",
        default=DEFAULT_TEXT,
        help=f"phrase to render (default: {DEFAULT_TEXT!r})",
    )
    p.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=(
            "OpenAI TTS voice (alloy / nova / shimmer / echo / fable / onyx); "
            f"default {DEFAULT_VOICE!r}"
        ),
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI TTS model (default: {DEFAULT_MODEL!r})",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help=f"output path (default: {DEFAULT_OUT_PATH})",
    )
    p.add_argument(
        "--format",
        choices=("pcm48", "ulaw8"),
        default="pcm48",
        help=(
            "target audio format: pcm48 = 48 kHz mono int16 for the Telegram "
            "leg (default); ulaw8 = 8 kHz μ-law for the Twilio caller leg"
        ),
    )
    args = p.parse_args()

    merged_env = env()
    api_key = merged_env.get("OPENAI_API_KEY")
    if not api_key:
        sys.stderr.write(
            "error: OPENAI_API_KEY is not set in .env or the environment\n"
        )
        return 2

    # Imports deferred so the --help path doesn't require these deps installed.
    import numpy as np
    import soxr
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    print(
        f"rendering text={args.text!r} via {args.model}/{args.voice} → {args.out}",
        flush=True,
    )

    # ``response_format="pcm"`` returns raw 24 kHz mono int16 little-endian
    # PCM with no container. We stream into a bytearray and decode in one
    # shot to keep the script simple — the phrase is ~3 s = ~144 KB at
    # 24 kHz, comfortably small.
    with client.audio.speech.with_streaming_response.create(
        model=args.model,
        voice=args.voice,
        input=args.text,
        response_format="pcm",
    ) as resp:
        raw24k = bytearray()
        for chunk in resp.iter_bytes():
            raw24k.extend(chunk)
    pcm24 = np.frombuffer(bytes(raw24k), dtype=np.int16)
    print(
        f"  received {len(raw24k)} B @ {SRC_SAMPLE_RATE} Hz "
        f"({len(pcm24) / SRC_SAMPLE_RATE:.2f}s)",
        flush=True,
    )

    if args.format == "ulaw8":
        # Twilio caller leg: 8 kHz G.711 μ-law. Reuse the bridge's
        # resample+encode, then pad to the 160 B (20 ms) frame the clip
        # driver strides in. μ-law silence is 0xFF (it encodes PCM 0).
        from outside_line.audio_bridge import pcm16_to_mulaw

        out_bytes = pcm16_to_mulaw(bytes(raw24k), sr_in=SRC_SAMPLE_RATE, sr_out=8000)
        remainder = len(out_bytes) % 160
        if remainder:
            pad = 160 - remainder
            out_bytes = out_bytes + (b"\xff" * pad)
            print(f"  padded {pad} B of μ-law silence to align to 20 ms frames")
        duration_s = len(out_bytes) / 8000
        fmt_desc = "8 kHz mono μ-law"
    else:  # pcm48
        # Resample 24 → 48 kHz mono int16. soxr's default settings give
        # broadcast-grade quality for a one-shot prompt; no need to tune.
        pcm48 = soxr.resample(pcm24, SRC_SAMPLE_RATE, DST_SAMPLE_RATE).astype(np.int16)
        out_bytes = pcm48.tobytes()
        # Sanity: must be 1920-aligned for the 20 ms-per-chunk pump. soxr's
        # rational resample of N input samples at 24→48 produces 2N output
        # samples (= 4N bytes); since 24 kHz * 20ms = 480 samples → 960
        # samples = 1920 bytes per 20 ms target frame, the input sample
        # count almost always rounds cleanly. Pad to 1920-alignment for
        # safety.
        remainder = len(out_bytes) % 1920
        if remainder:
            pad = 1920 - remainder
            out_bytes = out_bytes + (b"\x00" * pad)
            print(f"  zero-padded {pad} B to align to 20 ms frames")
        duration_s = (len(out_bytes) // 2) / DST_SAMPLE_RATE
        fmt_desc = f"{DST_SAMPLE_RATE} Hz mono int16"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_bytes(out_bytes)
    os.replace(tmp, args.out)

    print(
        f"  wrote {len(out_bytes)} B → {args.out} ({duration_s:.2f}s @ {fmt_desc})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
