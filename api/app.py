"""Web API for the banking assistant (phase 4).

    python -m api              # http://127.0.0.1:8000   (start  python -m mcp_servers.run  first)

JSON endpoints, with a cookie session:
    GET  /healthz                      for load balancers
    GET  /api/config                   sign-in options for the page
    POST /api/login    {customer}      demo sign-in (AUTH_MODE=dev); Cognito replaces it on AWS
    POST /api/logout
    GET  /api/me
    POST /api/chat     {message, thread_id?}
    POST /api/confirm  {thread_id, approve, code?}
    GET  /api/history?thread_id=...

Security
- The customer's OBP token stays in the server-side session; the browser only holds a random
  session id in an HttpOnly cookie.
- Conversations belong to one customer: thread ids carry the customer's label and are checked.
- Security codes go from the page to the verify service and are never put in the conversation.
- Per-session rate limit, security headers, and the same guardrails as the terminal chat.

State
- Conversations (and any slip waiting for approval) are in the conversation store: Postgres when
  DATABASE_URL is set, else SESSION_DB. A restart signs people out, but after signing in again
  their conversation, and a waiting slip, come back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if os.environ.get("BANK_TEST") != "1":
    load_dotenv(ROOT / ".env", override=True)

import storage  # noqa: E402
from api.actions import SPECIALISTS, slip  # noqa: E402
from observability import costs  # noqa: E402

COOKIE = "bank_session"
SESSION_IDLE_SECONDS = 2 * 3600
RATE_LIMIT = int(os.environ.get("API_RATE_LIMIT_PER_MIN", "30"))
DECLINED = "The customer declined this action."
CODE_NOT_CONFIRMED = "The security code was not confirmed, so nothing was done."
THREAD_RE = re.compile(r"^[AB]-[0-9a-f]{8}$")
log = logging.getLogger("bank.api")


# ---------- request bodies ----------

class LoginBody(BaseModel):
    customer: str = Field(pattern="^[AB]$")


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = None


class ConfirmBody(BaseModel):
    thread_id: str
    approve: bool
    code: str | None = Field(default=None, max_length=12)


# ---------- server-side state ----------

@dataclass
class Pending:
    """Write actions waiting for the customer, across one or more interrupts."""
    items: list[tuple[str, dict]]                 # (interrupt id, action)
    decisions: list[dict] = field(default_factory=list)
    challenge_id: str | None = None               # security code sent for the current action

    @property
    def current(self) -> dict | None:
        return self.items[len(self.decisions)][1] if len(self.decisions) < len(self.items) else None

    def decide(self, decision: dict) -> None:
        self.decisions.append(decision)
        self.challenge_id = None

    def resume_value(self):
        by_interrupt: dict[str, list] = {}
        for (iid, _), decision in zip(self.items, self.decisions):
            by_interrupt.setdefault(iid, []).append(decision)
        answers = {iid: {"decisions": d} for iid, d in by_interrupt.items()}
        return next(iter(answers.values())) if len(answers) == 1 else answers


@dataclass
class Session:
    id: str
    customer: str
    username: str
    name: str
    token: str = field(repr=False)
    last_seen: float = field(default_factory=time.time)
    agent: Any = None
    pending: dict[str, Pending] = field(default_factory=dict)
    hits: deque = field(default_factory=deque)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)


def _display_name(username: str) -> str:
    return username.split("@")[0].split(".")[0].capitalize()


def _demo_customers() -> dict[str, str]:
    """label -> username, from demo_accounts.json (or the .env logins)."""
    demo_file = Path(os.environ.get("DEMO_ACCOUNTS_FILE", ROOT / "demo_accounts.json"))
    if demo_file.exists():
        return {label: c["username"] for label, c in json.loads(demo_file.read_text())["customers"].items()}
    return {label: os.environ.get(f"CUSTOMER_{label}_USERNAME", f"customer-{label}") for label in ("A", "B")}


def _obp_token(label: str) -> str:
    """Sign in to the bank on the customer's behalf. On AWS the credentials come from Secrets Manager
    and the customer is identified by Cognito; the browser never sees either."""
    if os.environ.get("OBP_FAKE") == "1":
        return f"fake-token-{label}"
    from obp.client import OBPClient
    client = OBPClient.from_env()
    return client.login(os.environ[f"CUSTOMER_{label}_USERNAME"], os.environ[f"CUSTOMER_{label}_PASSWORD"])


def _text(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])


# ---------- the app ----------

def create_app(model_factory=None) -> FastAPI:
    """model_factory: returns the chat model for a new session (tests pass a scripted one)."""
    sessions: dict[str, Session] = {}
    stack = AsyncExitStack()
    saver_lock = asyncio.Lock()
    state: dict[str, Any] = {"saver": None}

    @asynccontextmanager
    async def lifespan(app):
        if os.environ.get("LANGSMITH_TRACING", "").lower() == "true" and os.environ.get("LANGSMITH_API_KEY"):
            from observability.tracing import install_trace_redaction
            install_trace_redaction()
        yield
        await stack.aclose()

    app = FastAPI(title="Banking assistant API", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
        return response

    async def saver():
        async with saver_lock:
            if state["saver"] is None:
                state["saver"] = await stack.enter_async_context(storage.checkpointer())
            return state["saver"]

    def session_of(request: Request) -> Session:
        sid = request.cookies.get(COOKIE)
        sess = sessions.get(sid or "")
        if sess is None or time.time() - sess.last_seen > SESSION_IDLE_SECONDS:
            sessions.pop(sid or "", None)
            raise HTTPException(401, "Please sign in.")
        now = time.time()
        while sess.hits and now - sess.hits[0] > 60:
            sess.hits.popleft()
        if len(sess.hits) >= RATE_LIMIT:
            raise HTTPException(429, "Too many requests. Please wait a moment.")
        sess.hits.append(now)
        sess.last_seen = now
        return sess

    def own_thread(sess: Session, thread_id: str | None, create: bool = False) -> str:
        if not thread_id:
            if not create:
                raise HTTPException(400, "thread_id is required.")
            return f"{sess.customer}-{uuid.uuid4().hex[:8]}"
        if not THREAD_RE.match(thread_id) or not thread_id.startswith(f"{sess.customer}-"):
            raise HTTPException(404, "Conversation not found.")
        return thread_id

    async def agent_for(sess: Session):
        if sess.agent is None:
            from agents.coordinator import build_coordinator
            try:
                sess.agent = await build_coordinator(sess.token, await saver(),
                                                     model=model_factory() if model_factory else None)
            except Exception as e:  # noqa: BLE001
                # The reason goes to the server log (docker compose logs api / CloudWatch).
                log.exception("can't reach the bank servers (%s): %s", type(e).__name__,
                              ", ".join(os.environ.get(k, "default") for k in
                                        ("MCP_ACCOUNTS_URL", "MCP_TRANSACTIONS_URL", "MCP_SERVICE_URL")))
                raise HTTPException(503, "The bank service isn't reachable right now. If you run the app "
                                         "with Python, start  python -m mcp_servers.run  first; in Docker, "
                                         "see  docker compose logs mcp") from e
        return sess.agent

    async def run(sess: Session, thread_id: str, payload, meter, steps: list) -> dict:
        """Stream one step of the agent, recording which specialist and tools were used."""
        agent = await agent_for(sess)
        config = {"configurable": {"thread_id": thread_id}, "callbacks": [costs.CostCallback(meter)],
                  "run_name": "banking_web_turn", "tags": ["web", f"customer-{sess.customer}"],
                  "metadata": {"customer": sess.customer, "thread_id": thread_id}}
        seen: set = set()
        specialist = None
        try:
            await _stream(agent, payload, config, steps, seen, specialist)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 - model or bank failure mid-turn
            # The customer gets a short apology; the reason goes to the server log (CloudWatch on AWS).
            log.exception("turn failed (thread %s): %s", thread_id, type(e).__name__)
            await end_turn(meter)
            raise HTTPException(502, "I couldn't finish that just now. Please try again.") from e
        return config

    async def _stream(agent, payload, config, steps, seen, specialist):
        async for _ns, chunk in agent.astream(payload, config, stream_mode="updates", subgraphs=True):
            for node, data in chunk.items():
                if node == "__interrupt__" or not isinstance(data, dict):
                    continue
                msgs = data.get("messages", [])
                for m in msgs if isinstance(msgs, list) else []:
                    for call in getattr(m, "tool_calls", None) or []:
                        key = (call.get("id"), call.get("name"), repr(call.get("args")))
                        if key in seen:
                            continue
                        seen.add(key)
                        if call["name"] == "task":
                            specialist = call["args"].get("subagent_type")
                            steps.append({"kind": "specialist", "name": specialist,
                                          "label": SPECIALISTS.get(specialist, specialist)})
                        else:
                            steps.append({"kind": "tool", "name": call["name"], "args": call.get("args", {}),
                                          "specialist": specialist})

    async def end_turn(meter) -> None:
        await asyncio.to_thread(costs.end_turn, meter)  # the ledger write mustn't hold up other customers

    def waiting(st) -> list[tuple[str, dict]]:
        """(interrupt id, action) for every write action this conversation is waiting on."""
        return [(i.id, a) for i in st.interrupts for a in (i.value or {}).get("action_requests", [])]

    async def pending_of(sess: Session, thread_id: str) -> Pending | None:
        """The slip this conversation is waiting on. After a restart it is rebuilt from the
        conversation store, which keeps the interrupt."""
        if thread_id not in sess.pending:
            agent = await agent_for(sess)
            items = waiting(await agent.aget_state({"configurable": {"thread_id": thread_id}}))
            if not items:
                return None
            sess.pending[thread_id] = Pending(items=items)
        return sess.pending[thread_id]

    async def finish(sess: Session, thread_id: str, meter, steps: list) -> dict:
        """After a step: either a new confirmation slip, or the answer."""
        agent = await agent_for(sess)
        st = await agent.aget_state({"configurable": {"thread_id": thread_id}})
        common = {"thread_id": thread_id, "steps": steps}
        items = waiting(st)
        if items:
            sess.pending[thread_id] = Pending(items=items)
            await end_turn(meter)
            return {**common, "status": "needs_confirmation", "slip": slip(items[0][1]),
                    **_meter(meter)}
        sess.pending.pop(thread_id, None)
        await end_turn(meter)
        return {**common, "status": "answered", "answer": _text(st.values["messages"][-1]), **_meter(meter)}

    def _meter(meter) -> dict:
        return {"cost": meter.summary_line(), "cost_usd": meter.total_cost, "guard": meter.guard_notes(),
                "latency_ms": meter.latency_ms}

    async def verify(sess: Session, path: str, body: dict) -> dict:
        url = os.environ.get("VERIFY_URL", "http://127.0.0.1:8104")
        try:
            async with httpx.AsyncClient(base_url=url, timeout=30,
                                         headers={"Authorization": f'DirectLogin token="{sess.token}"'}) as http:
                r = await http.post(path, json=body)
                data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(503, "The security code service isn't reachable.") from e
        if r.status_code != 200:
            raise HTTPException(r.status_code if r.status_code in (400, 401, 429) else 502,
                                data.get("message", "The security code couldn't be sent."))
        return data

    # ---------- routes ----------

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/config")
    async def config():
        mode = os.environ.get("AUTH_MODE", "dev")
        customers = [{"customer": label, "name": _display_name(u)} for label, u in sorted(_demo_customers().items())]
        return {"auth_mode": mode, "customers": customers if mode == "dev" else []}

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response):
        if os.environ.get("AUTH_MODE", "dev") != "dev":
            raise HTTPException(404, "Demo sign-in is off.")
        username = _demo_customers().get(body.customer)
        if not username:
            raise HTTPException(404, "Unknown demo customer.")
        try:
            token = await asyncio.to_thread(_obp_token, body.customer)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, "The bank didn't accept the demo sign-in.") from e
        sess = Session(id=secrets.token_urlsafe(32), customer=body.customer, username=username,
                       name=_display_name(username), token=token)
        sessions[sess.id] = sess
        secure = os.environ.get("COOKIE_SECURE", "0") == "1" or request.url.scheme == "https"
        response.set_cookie(COOKIE, sess.id, httponly=True, samesite="strict", secure=secure,
                            max_age=SESSION_IDLE_SECONDS, path="/")
        return {"customer": sess.customer, "name": sess.name}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request):
        sess = session_of(request)
        return {"customer": sess.customer, "name": sess.name}

    @app.post("/api/chat")
    async def chat(body: ChatBody, request: Request):
        sess = session_of(request)
        thread_id = own_thread(sess, body.thread_id, create=True)
        lock = sess.locks.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "Still working on your last message.")
        async with lock:
            waiting_slip = await pending_of(sess, thread_id)
            if waiting_slip is not None:
                return JSONResponse({"thread_id": thread_id, "status": "needs_confirmation",
                                     "slip": slip(waiting_slip.current or {})}, status_code=409)
            meter = costs.start_turn(thread_id, sess.customer)
            steps: list = []
            await run(sess, thread_id, {"messages": [{"role": "user", "content": body.message}]}, meter, steps)
            return await finish(sess, thread_id, meter, steps)

    @app.post("/api/confirm")
    async def confirm(body: ConfirmBody, request: Request):
        sess = session_of(request)
        thread_id = own_thread(sess, body.thread_id)
        lock = sess.locks.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "Still working on your last request.")
        async with lock:
            pending = await pending_of(sess, thread_id)
            if pending is None or pending.current is None:
                raise HTTPException(409, "There's nothing waiting for confirmation.")
            action = pending.current
            if not body.approve:
                pending.decide({"type": "reject", "message": DECLINED})
            elif pending.challenge_id is None:
                started = await verify(sess, "/verify/start", {"tool": action.get("name"), "args": action.get("args", {})})
                if started.get("required"):
                    pending.challenge_id = started["challenge_id"]
                    return {"thread_id": thread_id, "status": "code_required", "slip": slip(action),
                            "sent_to": started["sent_to"], "expires_in": started["expires_in"]}
                pending.decide({"type": "approve"})
            else:
                if not (body.code or "").strip():
                    raise HTTPException(400, "Enter the 6-digit code.")
                checked = await verify(sess, "/verify/check", {"challenge_id": pending.challenge_id, "code": body.code})
                if checked.get("verified"):
                    pending.decide({"type": "approve"})
                elif checked.get("attempts_left", 0) > 0:
                    return {"thread_id": thread_id, "status": "wrong_code", "slip": slip(action),
                            "attempts_left": checked["attempts_left"]}
                else:
                    pending.decide({"type": "reject", "message": CODE_NOT_CONFIRMED})
            if pending.current is not None:  # more actions in the same step
                return {"thread_id": thread_id, "status": "needs_confirmation", "slip": slip(pending.current)}
            from langgraph.types import Command
            meter = costs.start_turn(thread_id, sess.customer)
            steps: list = []
            sess.pending.pop(thread_id, None)
            await run(sess, thread_id, Command(resume=pending.resume_value()), meter, steps)
            return await finish(sess, thread_id, meter, steps)

    @app.get("/api/history")
    async def history(thread_id: str, request: Request):
        sess = session_of(request)
        thread_id = own_thread(sess, thread_id)
        agent = await agent_for(sess)
        st = await agent.aget_state({"configurable": {"thread_id": thread_id}})
        messages = []
        for m in (st.values or {}).get("messages", []):
            kind = getattr(m, "type", "")
            if kind == "human" or (kind == "ai" and not getattr(m, "tool_calls", None)):
                text = _text(m)
                if text.strip():
                    messages.append({"role": "you" if kind == "human" else "bank", "text": text})
        pending = await pending_of(sess, thread_id)
        return {"thread_id": thread_id, "messages": messages,
                "slip": slip(pending.current) if pending and pending.current else None}

    web = ROOT / "web"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


app = create_app()
