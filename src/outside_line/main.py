"""FastAPI application entrypoint."""

from __future__ import annotations

import contextlib
import faulthandler
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .config import require_boot_credentials, settings
from .log import get_logger, setup_logging
from .telegram_supervisor import SidecarSupervisor
from .twilio_handler import router as twilio_router

# Dump Python tracebacks of all threads to stderr on SIGSEGV/SIGFPE/SIGABRT.
# The FastAPI process should no longer segfault (ntgcalls now lives in the
# sidecar, ADR 0017), but keep this as a backstop for any other native crash.
faulthandler.enable()

setup_logging(settings.log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown for the Telegram bridge sidecar (ADR 0017).

    The bridge now runs in a supervised child process; ``app.state.telegram_bridge``
    becomes the ``SidecarSupervisor``'s ``TelegramBridgeClient`` — a duck-typed
    drop-in, so ``twilio_handler`` is unchanged. A native ntgcalls crash takes
    down only that child; FastAPI survives and the supervisor respawns it.

    When ``telegram_api_id`` is unset (local-test mode without Telegram creds),
    skip the sidecar entirely so message-only functionality stays intact — the
    call/message tools just won't be registered on the Realtime session, and
    ``twilio_handler.media`` reads ``app.state.telegram_bridge`` as ``None``.
    """
    if settings.environment != "development":
        require_boot_credentials(settings)
    if not settings.twilio_auth_token:
        log.warning(
            "twilio.signature.validation_disabled",
            hint="set TWILIO_AUTH_TOKEN to enable webhook signature checks",
        )
    supervisor: SidecarSupervisor | None = None
    if settings.telegram_api_id is None:
        log.info("telegram.bridge.skipped", reason="telegram_api_id not set")
    else:
        try:
            supervisor = SidecarSupervisor(settings.telegram_sidecar_socket_path)
            await supervisor.start()
        except Exception:
            log.exception("telegram.bridge.start_failed")
            if supervisor is not None:
                with contextlib.suppress(Exception):
                    await supervisor.stop()
            supervisor = None

    app.state.telegram_supervisor = supervisor
    app.state.telegram_bridge = supervisor.client if supervisor is not None else None

    try:
        yield
    finally:
        if supervisor is not None:
            try:
                await supervisor.stop()
            except Exception:
                log.exception("telegram.bridge.stop_failed")


app = FastAPI(title="outside-line", lifespan=lifespan)
app.include_router(twilio_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/debug/crash-sidecar")
async def debug_crash_sidecar() -> dict[str, str]:
    """Verification hook (ADR 0017): SIGSEGV the bridge sidecar mid-call to
    prove FastAPI survives, only the affected call is torn down, and the agent
    apologizes + can retry. Gated by SIDECAR_DEBUG_CRASH=1 — returns 404
    (invisible) in prod, where the flag is unset."""
    if os.environ.get("SIDECAR_DEBUG_CRASH") != "1":
        raise HTTPException(status_code=404)
    bridge = getattr(app.state, "telegram_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="no bridge")
    await bridge.trigger_sidecar_crash()
    return {"status": "sidecar crash triggered"}
