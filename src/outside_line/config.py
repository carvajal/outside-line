"""Application settings, loaded from environment / ``.env`` via pydantic-settings.

Secrets never live in code — they come from ``.env`` (local, gitignored) or
Railway variables in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Agent identity ---
    # The name the agent answers to. Referenced by the persona prompt and
    # by the post-call LLM prompts (call summary, memory merge) so the
    # transcript role labels line up with what the caller heard.
    agent_name: str = "Alex"
    # IANA timezone for the agent's date awareness ("today is …" in the
    # session instructions and the search worker's freshness anchor).
    # Env: AGENT_TIMEZONE.
    agent_timezone: str = "America/New_York"

    # --- OpenAI ---
    openai_api_key: str = ""
    # Realtime model alias — tracks the latest GA snapshot. Pin to a
    # dated snapshot (e.g. `gpt-realtime-2025-08-28`) if reproducibility
    # matters more than freshness.
    openai_realtime_model: str = "gpt-realtime-2.1"
    # `cedar` (and its sibling `marin`) shipped with gpt-realtime-2 and are
    # OpenAI's most natural-sounding voices to date — a meaningful step up
    # from the older `ash`/`alloy`/`verse` lineage on phone-call audio.
    openai_realtime_voice: str = "cedar"
    # Linear gain applied to the agent's output PCM before encoding to μ-law.
    # 1.0 = pass-through, 0.8 = -1.9 dB (the current taste-tested level
    # — the agent was a touch hot at full volume over the phone bearer).
    openai_realtime_output_gain: float = 0.8
    # Speech rate multiplier for the agent's TTS (Realtime audio.output.speed).
    # 1.0 = native human pace; >1.0 starts to sound robotic on a phone
    # bearer. Realtime accepts roughly 0.25–4.0.
    openai_realtime_output_speed: float = 1.0
    # Reasoning effort for the realtime model's internal thinking before
    # it speaks. Enum: minimal | low | medium | high | xhigh. Higher =
    # smarter answers, slower first-token. `high` is the default because
    # a call is a small number of high-value turns, not a latency-sensitive
    # scripted menu. Bump to `xhigh` only if the model
    # is still missing the point on multi-step reasoning.
    openai_realtime_reasoning_effort: str = "high"
    # Model for every *text*-intelligence call: the `search_web` and
    # `case_brief_lookup` tool workers (Responses API + hosted
    # web_search) and the post-call memory merge (caller_memory_model
    # below defaults to this same string). gpt-5.4-mini is a GA reasoning
    # model (snapshot gpt-5.4-mini-2026-03-17) that supports the hosted
    # web_search tool and is far stronger than the old gpt-4o-mini at
    # telling a live source from a stale one and synthesizing conflicting
    # hits — the root cause behind search_web's fake-fare / same-headline
    # failures. Fast enough for the live-call budget. Pin to the dated
    # snapshot if you care about reproducibility.
    openai_search_model: str = "gpt-5.4-mini"
    # Model that transcribes the *caller's* speech on the Realtime session
    # (audio.input.transcription). This is a side channel — it does NOT gate
    # the agent's spoken reply (the model answers from the audio directly), so its
    # latency is off the caller's critical path; pick it for transcript /
    # memory / name-capture quality. gpt-4o-transcribe beats the old
    # whisper-1 baseline on noisy, accented speech over the phone bearer.
    # Kill switch: set OPENAI_TRANSCRIPTION_MODEL=whisper-1 in Railway to
    # revert without a redeploy. See ADR 0024.
    openai_transcription_model: str = "gpt-4o-transcribe"
    # ISO-639-1 hint for the transcription model (e.g. `en`). Pinning a
    # language improves accuracy + latency when callers consistently speak
    # it; leave blank to auto-detect per utterance. Side channel only — it
    # never gates the agent's spoken reply (ADR 0024). Env:
    # OPENAI_TRANSCRIPTION_LANGUAGE.
    openai_transcription_language: str = "en"
    # Whole-tool wall-clock budget for `search_web` (Responses API +
    # hosted web_search round trip, which may fire several EN/ES search
    # calls). Mirrors case_brief_lookup_timeout_s. 15 s — wider than the
    # old hard-coded 12 s to give the stronger model + high search-context
    # room — but deliberately not 18-20 s, because there is no mid-wait
    # spoken filler yet (see docs/roadmap.md): every second past the
    # agent's ~2-4 s preamble is dead air. Widen once a filler lands.
    search_web_timeout_s: float = 15.0

    # --- Realtime server-VAD tuning (barge-in) ---
    # Sensitivity for the Realtime server-side voice activity detector.
    # Higher = needs louder/clearer caller speech to trigger a turn —
    # raises the noise floor so coughs, taps, and room noise don't
    # interrupt the agent. Range 0.0–1.0; OpenAI's bare default is 0.5; we
    # hold at 0.7 because the phone bearer is noisier than a desk mic
    # (0.6 still let slight coughs trip a barge-in on a real call).
    realtime_vad_threshold: float = 0.7
    # How much audio (ms) before speech-onset is kept once VAD trips,
    # so the model hears the very start of the utterance.
    realtime_vad_prefix_padding_ms: int = 300
    # How long (ms) silence must persist after a phrase before the
    # server commits the turn. Shorter = snappier, more accidental
    # commits on mid-sentence pauses (the agent runs away with a partial
    # query); longer = slower handoff but the caller can breathe.
    # 1500 ms (3× the OpenAI 500 ms baseline) is tuned for noisy mobile
    # lines where short pauses are real. Bump via
    # REALTIME_VAD_SILENCE_DURATION_MS if the agent still jumps in early.
    realtime_vad_silence_duration_ms: int = 1500

    # --- Playout-gated floor release (ADR 0020) ---
    # How long (s) past the byte-clock's predicted playout end to keep
    # waiting for Twilio's mark echo before declaring it lost and
    # releasing the floor anyway. The echo is the primary release signal
    # (ground truth, fail-closed); this grace is the fail-open backstop.
    # Larger = more deafness per response if marks ever break; smaller =
    # leak risk when the echo is merely late. 1.5 s rides well above
    # observed WS jitter without a caller-noticeable stall.
    playout_mark_grace_s: float = 1.5

    # --- Twilio (API-Key auth: SK + secret + account SID) ---
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    # Enables X-Twilio-Signature validation on both webhooks when set
    # (403 on bad/missing signature). Blank = validation skipped with a
    # boot-time warning; the production preflight requires it outside
    # development. See ADR 0010.
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # When true, every inbound call triggers a Twilio dual-channel recording
    # (caller on ch0, the agent on ch1). Audio lives on Twilio; fetch via
    # `scripts/recordings.py` or browse at
    # https://console.twilio.com/us1/monitor/logs/call-recordings.
    # OFF by default: recording phone calls has jurisdiction-specific
    # consent requirements — turn it on deliberately, and know your local
    # rules (see the README's "Lawful use & consent" section).
    recordings_enabled: bool = False
    # When recordings are on, speak a short "This call may be recorded."
    # notice to each leg before any recording can capture it (caller leg
    # before the recording is kicked; contact leg before the bridge pumps
    # start). Inert while recordings_enabled is False. Disable only if
    # you have consent covered some other way.
    recording_disclosure_enabled: bool = True

    # Lead + trail pause length (seconds) around the DTMF '1' the handler
    # sends to accept the call on carriers that require a digit press
    # before connecting (the account holder consents to accept charges
    # on their own line).
    # The gate XML is rendered for every allowed caller not flagged
    # skip_gate; flagged callers (data/callers.json) skip it entirely. Twilio
    # rejects <Pause length=> outside 1..60. Tune via Railway env var.
    dtmf_gate_seconds: int = 20

    # --- Caller registry ---
    # Single JSON file at this path, keyed by E.164 phone number. Records
    # first_seen / last_seen / call_count / skip_gate / label per number.
    # Lives on the Railway persistent volume; gitignored under `data/*`.
    callers_path: Path = Path("data/callers.json")

    # --- Caller memory ---
    # Per-caller markdown file (``<E.164>.md``) stored under this dir.
    # Loaded at the start of every call and injected into the agent's
    # instructions so it greets/treats the caller with continuity; an
    # async LLM-merge job runs after the call ends and may rewrite the
    # file based on the transcript. Lives on the Railway persistent
    # volume; gitignored under ``data/*``. Operator surface:
    # ``./scripts/callers.py memory show|edit|set|clear <phone>``.
    caller_memory_dir: Path = Path("data/memory")
    # Skip the LLM merge if the call has fewer than this many caller
    # utterances — too little signal to extract a stable fact from.
    caller_memory_min_utterances: int = 3
    # Model used by the post-call LLM merge. Held at the same string as
    # openai_search_model (gpt-5.4-mini) — one model for all text
    # intelligence for now. Runs off the call's critical path, so its
    # latency doesn't matter; the stronger model just follows the merge
    # rules (incl. the "News already discussed" told-topics section) more
    # reliably than gpt-4o-mini did.
    caller_memory_model: str = "gpt-5.4-mini"

    # --- Per-call report email ---
    # Send the operator one informational email per call, fired on the
    # terminal /twilio/voice/status callback (ADR 0022), via the Resend
    # transactional API. Railway blocks outbound SMTP on the Hobby tier,
    # so HTTPS is the only viable transport (ADR 0005).
    # Fail-open: any blank required setting (TO / RESEND_API_KEY) makes
    # the report a silent no-op; the call path is never affected.
    caller_alerts_enabled: bool = True
    caller_alerts_email_to: str = ""  # recipient
    # Resend test-domain default — works without DNS verification, sends
    # only to the email used when signing up for Resend. Swap for a real
    # `Outside Line <agent@yourdomain.com>` once a domain is verified at
    # https://resend.com/domains.
    caller_alerts_email_from: str = "Outside Line <onboarding@resend.dev>"
    caller_alerts_resend_api_key: str = ""  # https://resend.com/api-keys

    # --- Case-brief lookup ---
    # The `case_brief_lookup` tool grounds its answers in an operator-
    # authored brief plus (optionally) a live CourtListener docket Atom
    # feed. The brief lives only under `data/` (gitignored) and on the
    # deployment volume — case material never belongs in git. A sample
    # brief is at docs/examples/case-brief.example.md. See
    # docs/decisions/0007-case-brief-lookup-tool.md.
    case_brief_path: Path = Path("data/case_brief/brief.md")
    # Atom feed URL for the docket (e.g. a CourtListener /docket/<id>/feed/
    # URL). Blank = no feed; the tool answers from the brief alone.
    case_brief_docket_feed_url: str = ""
    # Wall-clock budget for the whole tool call (brief read + feed GET +
    # Responses-API round trip). 10 s leaves headroom over the ~6 s
    # search_web typically takes, since the grounded call is doing less
    # network work but more reasoning over a longer prompt.
    case_brief_lookup_timeout_s: float = 10.0

    # --- Ring-phase audio ---
    # What the caller hears during the RING phase between AGENT hangup and
    # BRIDGE start. "hum" plays a mathematically-synthesised Nokia-tune
    # hum (fundamental + 2 harmonics + 5 Hz vibrato + breath noise, see
    # audio_bridge._build_nokia_hum_loop). "ringback" plays the original
    # major-third arpeggio (A4/C#5/E5 tones, see _build_ringback_loop).
    # Kill switch: set RING_AUDIO_KIND=ringback in Railway to revert
    # without a redeploy if the hum regresses real-call audio quality.
    ring_audio_kind: Literal["hum", "ringback"] = "hum"

    # --- Bridge phase (Twilio <-> Telegram audio pump) ---
    # Number of consecutive 1-second `phase.bridge.latency` windows with
    # NO audio in either direction — no caller frames in AND none arriving
    # from the contact — before the bridge is proactively torn down.
    # Detects the "phantom-bridge tail" where
    # Twilio's Media Stream WS lingers ~13-14 s after the PSTN call has
    # actually ended (a carrier hangup pattern).
    # Fires only when the bridge is silent in BOTH directions (no caller
    # audio in AND none arriving from the contact) — a live contact leg
    # means the call is up even if the caller's inbound media paused (the
    # false-positive teardown seen on a real call). 10 s gives ride-through margin;
    # the ws_closed backstop (~13 s) still catches a real caller hangup.
    # Set to 0 to disable the detector.
    bridge_stall_tear_down_s: int = 10

    # --- Telegram (only needed for the call/message bridge) ---
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_phone_number: str = ""
    telegram_session_path: str = "data/sessions/outside-line.session"
    # --- Telegram bridge sidecar (ADR 0017) ---
    # The crash-prone ntgcalls voice stack runs in a supervised child process;
    # FastAPI talks to it over this Unix-domain socket. Lives on the Railway
    # persistent volume (/app/data) so crash logs sit beside it; gitignored
    # under data/*. A native SIGSEGV in ntgcalls then costs one bridge attempt,
    # not the whole server.
    telegram_sidecar_socket_path: str = "data/telegram_sidecar.sock"

    # --- Directlines (dedicated DIDs → Telegram contact; the agent stays out of the path) ---
    # JSON file mapping each dedicated Twilio DID (E.164) to one Telegram
    # contact's ``@username``. When an inbound call's ``To`` matches an
    # entry here, the voice handler routes straight to the Telegram contact
    # instead of opening a Realtime session — the agent only steps in if the
    # contact doesn't answer in ``directline_ring_timeout_s``. Lives on
    # the Railway persistent volume; gitignored under ``data/*``. See
    # docs/decisions/0013-directline-feature.md.
    directlines_path: Path = Path("data/directline_numbers.json")
    # Total time (seconds) we wait for the Telegram contact to answer
    # before falling back to the agent with an apology. 70 s covers the
    # two gate pauses (2 × DTMF_GATE_SECONDS) plus ~30 s of post-gate hum.
    directline_ring_timeout_s: int = 70
    # Kill switch for the pre-rendered wait phrase played to the
    # Telegram contact when they answer before the gate completes. Set to
    # False to skip straight to the ringback loop.
    directline_wait_message_enabled: bool = True

    # --- Runtime ---
    public_base_url: str = ""
    log_level: str = "INFO"
    environment: str = "development"

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """Treat an empty env value as unset (Telegram creds are optional)."""
        if v in ("", None):
            return None
        return v

    @field_validator("dtmf_gate_seconds")
    @classmethod
    def _dtmf_gate_in_twilio_range(cls, v: int) -> int:
        """Twilio's <Pause length> accepts 1..60. Fail loud on misconfig."""
        if not 1 <= v <= 60:
            raise ValueError(
                f"DTMF_GATE_SECONDS must be in 1..60 (Twilio limit), got {v!r}"
            )
        return v


def require_boot_credentials(s: "Settings") -> None:
    """Fail loudly at boot when a hard-required credential is blank.

    Called from the app lifespan only when ``environment`` is not
    ``development``, so local quickstarts and the test suite (blank env)
    keep booting while a production deploy can never come up half-armed
    (e.g. webhooks unsigned because TWILIO_AUTH_TOKEN was forgotten).
    """
    required = {
        "OPENAI_API_KEY": s.openai_api_key,
        "TWILIO_ACCOUNT_SID": s.twilio_account_sid,
        "TWILIO_API_KEY_SID": s.twilio_api_key_sid,
        "TWILIO_API_KEY_SECRET": s.twilio_api_key_secret,
        "TWILIO_PHONE_NUMBER": s.twilio_phone_number,
        "TWILIO_AUTH_TOKEN": s.twilio_auth_token,
        "PUBLIC_BASE_URL": s.public_base_url,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            f"ENVIRONMENT={s.environment!r} requires these variables to be "
            f"set, but they are blank: {', '.join(missing)}"
        )


settings = Settings()
