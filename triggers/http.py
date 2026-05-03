"""FastAPI HTTP trigger.

Bound to 127.0.0.1; Tailscale serve exposes it to the tailnet.

  uv run uvicorn triggers.http:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from orchestrator import auth, loop
from orchestrator.db import connect, ensure_schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    ensure_schema()
    yield


app = FastAPI(title="home-agent", lifespan=lifespan)


class RunRequest(BaseModel):
    task: str = Field(..., min_length=1)
    profile_hint: str | None = None
    model: str | None = None


class RunResponse(BaseModel):
    response: str
    principal: str


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest, x_agent_token: str = Header(..., alias="X-Agent-Token")):
    with connect() as conn:
        principal = auth.verify(x_agent_token, conn)
    if not principal:
        raise HTTPException(status_code=401, detail="invalid token")

    profile = _resolve_profile(principal, req.profile_hint)
    text = await loop.run(
        task=req.task,
        principal=principal,
        profile=profile,
        model=req.model or "claude-opus-4-7",
    )
    return RunResponse(response=text, principal=principal.user_id)


# Per design §10: profile_hint is a hint; role-allowed profiles win.
ROLE_DEFAULTS = {"admin": "interactive", "adult": "mobile", "child": "mobile"}
ROLE_ALLOWED = {
    "admin": {"interactive", "home", "mobile", "unattended"},
    "adult": {"interactive", "home", "mobile"},
    "child": {"mobile"},
}


def _resolve_profile(principal: auth.Principal, hint: str | None) -> str:
    allowed = ROLE_ALLOWED[principal.role]
    if hint and hint in allowed:
        return hint
    return ROLE_DEFAULTS[principal.role]
