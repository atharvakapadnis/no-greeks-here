"""ASGI app: startup checks, static/template wiring, guest router.

Every route handler in this app is a plain `def`, never `async def` — see
the note at the top of app/routers/guest.py for why.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.auth import GuestLoginRequired, OperatorLoginRequired
from app.config import get_settings
from app.routers import guest as guest_router
from app.routers import operator as operator_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Un-cached on purpose (see app/config.py): fails loudly here, with the
    # specific exception, rather than bootstrapping anything silently.
    get_settings()
    db.verify_ready()
    yield


app = FastAPI(title="no-greeks-here", lifespan=lifespan)

app.mount(
    "/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"
)

app.include_router(guest_router.router)
app.include_router(operator_router.router)


@app.exception_handler(GuestLoginRequired)
def _guest_login_required(request: Request, exc: GuestLoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(OperatorLoginRequired)
def _operator_login_required(
    request: Request, exc: OperatorLoginRequired
) -> RedirectResponse:
    return RedirectResponse("/operator/login", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
