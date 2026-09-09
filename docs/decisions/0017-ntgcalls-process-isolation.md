# 0017 — Process-isolate the ntgcalls voice stack into a supervised sidecar

**Date:** 2026-07-06
**Status:** Accepted

## Context

On 2026-07-06 production went fully dark. Root cause: a native **SIGSEGV inside
ntgcalls** (`wrtc::IncomingAudioChannel` ctor via `addIncomingSmartSource`,
fires the moment a contact answers — [ntgcalls#51](https://github.com/pytgcalls/ntgcalls/issues/51)).
Because it is an **OS signal, not a Python exception**, no `try/except` can
catch it, and the bridge ran in-process with FastAPI — so the crash killed the
**entire server** and every in-flight call, not just the one bridge attempt.
`faulthandler` left a trace but gave no survival.

Isolation could be read as a stopgap pending a migration of the contact leg
to the WhatsApp Business Calling API. **That option is off the table — the
contacts are on Telegram, not WhatsApp.** There is no migration path; ntgcalls
stays, so it must be **contained**. Isolation is the reliability fix, not a
stopgap.

## Decision

Run the crash-prone Telethon + py-tgcalls/ntgcalls stack in a **supervised
sidecar process**. FastAPI drives it over a local socket; a native crash in the
sidecar costs one bridge attempt, the FastAPI process survives, and the sidecar
is respawned.

- **IPC = "mirror the bridge API."** All soxr resampling stays FastAPI-side
  exactly as today; only the *same* PCM48 frames the in-process bridge already
  passed cross the wire. ADR 0009's 10 ms / 960-byte framing invariant is thus
  preserved **by construction** — the sidecar is a thin ntgcalls host that
  forwards frames untouched. (Rejected: "sidecar resamples" — it moves soxr +
  capture-time + the 960 B split into the crash-prone process and puts the
  invariant *at* the boundary instead of guaranteeing it.)
- **Two Unix-domain stream connections**, not one: a CONTROL channel (RPCs +
  async events) and an AUDIO channel (the ~100–200 fps hot path). The split
  means an audio flood can never head-of-line-block a `place_call` reply. Wire
  format is a length-prefixed frame (`[u32 len][u8 type][body]`);
  [`telegram_ipc.py`](../../src/outside_line/telegram_ipc.py).
- **`TelegramBridgeClient`** ([`telegram_bridge_client.py`](../../src/outside_line/telegram_bridge_client.py))
  is a duck-typed drop-in for `TelegramBridge` — same public surface
  (`place_call` / `send_to_contact` / `hangup` / `register_callbacks` /
  `bridge_ended_event` …) — so `twilio_handler` / `directlines` are unchanged.
  Per-call state (callbacks, `bridge_ended_event`, `_current_user_id`) is
  **local** to the proxy; closures never cross the wire.
- **`SidecarSupervisor`** ([`telegram_supervisor.py`](../../src/outside_line/telegram_supervisor.py))
  is the async, in-process analogue of `scripts/dev_serve.py`'s supervisor:
  spawn → connect → monitor (PING/PONG liveness + child exit) → respawn +
  reconnect the *same* client in place. Crash-loop guard (5 respawns / 60 s) →
  **degraded mode**: sidecar stays down, bridge calls raise, FastAPI keeps
  serving the agent. `PR_SET_PDEATHSIG` (Linux) reaps an orphaned sidecar if uvicorn
  dies ungracefully (avoids a stale Telethon session lock).
- **Crash → play a fixed recording, then hang up** (supersedes two earlier
  designs: a spoken yes/no reconnect confirm, and auto-reconnect).
  A native crash drops the connection with no
  `LEFT_CALL`, so the proxy **synthesizes**
  `on_bridge_ended(reason="sidecar_crashed")` and sets `bridge_ended_event`.
  `_run_bridge_phase` surfaces that reason; both the agent phase loop and the
  directline path respond by playing the caller **one pre-rendered
  recording** — *"Sorry — the call dropped on my end. Please wait twenty
  seconds and call again."* (`CRASH_MESSAGE_ULAW_BYTES`, μ-law 8 kHz, rendered
  by `scripts/render_directline_audio.py --format ulaw8` to the committed
  `assets/crash_message.raw`, streamed via `_play_ulaw_clip`) — and
  then **end the call** (exit the loop → `_call_teardown` → `ws.close()`; there
  is no REST hangup). **No re-ring.** *Why not reconnect:* the just-crashed
  MTProto call lingers on the contact's side for several seconds and can't be
  discarded (the call handle died with the crashed process), so a re-ring hits
  `CallBusy` and fired **4–5 missed-call notifications** to the contact before
  the line cleared (measured: 5× busy-retry over ~18 s). Telling the *caller* to
  wait-and-redial lets the contact's dead call clear naturally with zero
  re-ring and zero notification spam. The recording is a **TTS voice, not
  the agent's Realtime `cedar`** — a deliberate distinct "system" message — and the
  crash path **never touches the Realtime session**, so there is nothing for
  the caller to talk over — the mis-parse risk that sank the spoken-confirm
  design is out of the picture entirely.
- **Durable crash capture.** The sidecar routes `faulthandler` to a file on
  the `/app/data` volume (ADR 0004) so the native trace survives the respawn;
  the supervisor tails new bytes into Railway logs on child death, preserving
  the live debuggability that diagnosed the original outage.
- **FastAPI no longer imports the native code at all.** `main.py` drops its
  pytgcalls import; the `TelegramBridge` type hints in `twilio_handler` /
  `message_contact` move under `TYPE_CHECKING`. Verified: neither `pytgcalls`
  nor `telethon` is in the FastAPI process's import graph.

## Consequences

- **Blast radius shrinks from "everything" to "one call."** A contact-answer
  SIGSEGV tears down only the affected bridge; concurrent calls and the web
  process live.
- **A new hot-path hop.** `send_to_contact` is now a buffered UDS write instead
  of an in-process await. Local loopback at ~192 KB/s each way is trivial and
  the two-channel split protects RPC latency; watch `send_to_contact_p95` on
  real calls (mitigation if >~1 ms: batch the two 10 ms halves per IPC message).
- **One metrics-shape change:** `tg_frames_per_callback` collapses to 1 (one
  960 B frame per IPC message) since sub-frame concatenation no longer happens.
  The load-bearing signal — `frames_b` contact-leg liveness for the stall
  detector — is preserved.
- **The Telethon session is now owned solely by the sidecar** (cleaner). A
  respawn must not fight a stale SQLite lock → PDEATHSIG reap + the crash-loop
  guard bound the retries. `discard_outgoing_call` / `hangup`'s pytgcalls
  cache reasoning stays entirely inside the sidecar; the proxy only ever calls
  them as awaited RPCs — never replicate that logic across the wire.
- **Verification, not a mock:** `POST /debug/crash-sidecar` (gated by
  `SIDECAR_DEBUG_CRASH=1`; 404 in prod) SIGSEGVs the sidecar mid-call to
  prove FastAPI survives + the crash recording plays and the call ends against a
  real native crash. The
  CI suite drives the whole path — spawn → real SIGSEGV → synthesized
  `sidecar_crashed` → respawn — through an `--echo` sidecar (no Telegram creds).
- **No new dependency.** All stdlib (`asyncio` UDS, `struct`, `subprocess`,
  `faulthandler`, `ctypes` for PDEATHSIG).

## References

- [ntgcalls#51](https://github.com/pytgcalls/ntgcalls/issues/51) — the upstream
  SIGSEGV report.
- ADR 0008 — Telegram 1:1 P2P modality (unchanged; now hosted in the sidecar).
- ADR 0009 — the 10 ms / 960-byte framing invariant this design preserves, and
  the caller-side μ-law ring/clip driver the crash recording reuses.
- ADR 0004 — the `/app/data` volume where the durable crash trace + socket live.
- `scripts/dev_serve.py` — the supervisor pattern (`_is_crash_loop`,
  SIGTERM→SIGKILL ladder) ported into `SidecarSupervisor`.
