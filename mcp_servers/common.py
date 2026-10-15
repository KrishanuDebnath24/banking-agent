"""Shared code for the three MCP servers.

Identity comes only from the customer's OBP token, sent by the agent as an
HTTP header on every MCP call. The server asks OBP who the token belongs to,
then looks up that customer's allowed (visible) accounts in demo_accounts.json.
Nothing the model writes can change who the customer is or which accounts
they may touch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Tests pass their own settings (fake bank, printed codes) and must not pick up a real .env.
if os.environ.get("BANK_TEST") != "1":
    load_dotenv(ROOT / ".env", override=True)

from agents.context import VISIBLE_ROLES, _first_seed_time, account_key  # noqa: E402
from obp.client import OBPClient, OBPError  # noqa: E402

DEMO_FILE = Path(os.environ.get("DEMO_ACCOUNTS_FILE", ROOT / "demo_accounts.json"))


def _make_client():
    if os.environ.get("OBP_FAKE") == "1":
        from obp.fake import FakeOBP
        fixture = os.environ.get("OBP_FAKE_FIXTURE")  # the fixed eval bank (evals/fixture.py)
        return FakeOBP.from_fixture(fixture) if fixture else FakeOBP.from_demo(DEMO_FILE)
    return OBPClient.from_env()


CLIENT = _make_client()
_OFFSET = int(os.environ.get("MCP_PORT_OFFSET", "0"))  # evals and tests run a second set of servers
PORTS = {"accounts": 8101 + _OFFSET, "transactions": 8102 + _OFFSET, "service": 8103 + _OFFSET,
         "verify": 8104 + _OFFSET}  # verify: security codes (plain HTTP, not MCP)


@dataclass
class Customer:
    label: str
    username: str
    token: str = field(repr=False)
    bank_id: str
    currency: str
    accounts: dict[str, str]          # visible name -> account id
    show_since: str | None = None

    def resolve(self, account: str | None) -> str | None:
        """ "current", "my current account", or this customer's own account id -> the id; else None."""
        if account is None:
            return None
        key = account_key(account)
        if key in self.accounts:
            return self.accounts[key]
        return account.strip() if account.strip() in self.accounts.values() else None

    def name_of(self, account_id: str) -> str:
        return next((n for n, i in self.accounts.items() if i == account_id), account_id)

    def denied(self) -> str:
        return json.dumps({"error": "access_denied",
                           "message": "That account does not belong to this customer.",
                           "allowed_accounts": list(self.accounts)})


_CACHE: dict[str, tuple[Customer, float]] = {}
_TOKEN_RE = re.compile(r'token="?([^",\s]+)"?', re.I)


def _token_from_ctx(ctx) -> str:
    try:
        headers = ctx.request_context.request.headers
    except Exception as e:  # noqa: BLE001
        raise PermissionError("no HTTP request context") from e
    return token_from_header(headers.get("authorization", ""))


def token_from_header(auth: str) -> str:
    """The OBP token from an Authorization header: DirectLogin token="..." (or Bearer ...)."""
    m = _TOKEN_RE.search(auth)
    if m:
        return m.group(1)
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    raise PermissionError("missing customer token")


def _lookup(token: str) -> Customer:
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _CACHE.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        username = CLIENT.current_user(token).get("username")
    except OBPError as e:
        raise PermissionError("the bank did not accept this login; please log in again") from e
    demo = json.loads(DEMO_FILE.read_text())
    for label, c in demo["customers"].items():
        if c["username"] == username:
            cust = Customer(label=label, username=username, token=token, bank_id=demo["bank_id"],
                            currency=c["currency"], accounts={r: c[r] for r in VISIBLE_ROLES},
                            # The shared sandbox has years of other people's test data; the fake bank doesn't.
                            show_since=None if os.environ.get("OBP_FAKE") == "1" else _first_seed_time(label))
            _CACHE[key] = (cust, time.time() + 300)
            return cust
    raise PermissionError("this user is not a demo customer")


async def customer(ctx) -> Customer:
    """The customer behind this MCP call (raises PermissionError if unknown)."""
    token = _token_from_ctx(ctx)
    return await asyncio.to_thread(_lookup, token)


async def customer_for_token(token: str) -> Customer:
    """The customer behind a raw OBP token (raises PermissionError if unknown)."""
    return await asyncio.to_thread(_lookup, token)


async def bank(fn, *args, **kwargs):
    """Run a blocking OBP call without blocking the other servers."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def error(code: str, **extra) -> str:
    return json.dumps({"error": code, **extra})


def parse_date(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as e:
        raise ValueError(f"{name} must be a date like 2026-10-01") from e


def rows_for(cust: Customer, raw: list[dict], from_date: str | None, to_date: str | None,
             search: str | None) -> list[dict]:
    """OBP transactions -> simple rows, newest first, filtered by date and payee words."""
    terms = [t.strip().lower() for t in (search or "").split(",") if t.strip()]
    rows = []
    for t in raw:
        d = t.get("details", {})
        day = (d.get("completed") or d.get("posted") or "")[:10]
        if cust.show_since and day and day < cust.show_since:
            continue  # pre-demo noise in the shared sandbox
        if from_date and day < from_date:
            continue
        if to_date and day > to_date:
            continue
        desc = d.get("description") or ""
        if terms and not any(term in desc.lower() for term in terms):
            continue
        rows.append({"date": day, "amount": round(float(d.get("value", {}).get("amount", 0) or 0), 2),
                     "description": desc, "balance_after": d.get("new_balance", {}).get("amount")})
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


__all__ = ["CLIENT", "PORTS", "Customer", "OBPError", "bank", "customer", "customer_for_token", "error",
           "parse_date", "rows_for", "token_from_header"]
