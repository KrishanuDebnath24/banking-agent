"""Offline end-to-end test of the web API (phase 4, step 1).

Real API + real MCP servers + the fixed eval bank, with a scripted model. No keys or network.

    python tests/test_api_offline.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="bank-api-test-"))
OFFSET = 800
os.environ.update({"BANK_TEST": "1", "LANGSMITH_TRACING": "false", "EVAL_PORT_OFFSET": str(OFFSET),
                   "OTP_OUTBOX": str(TMP / "outbox.jsonl"), "OBP_FAKE": "1", "AUTH_MODE": "dev",
                   "SESSION_DB": str(TMP / "sessions.db"), "VERIFY_URL": f"http://127.0.0.1:{8104 + OFFSET}"})

from evals import harness  # noqa: E402

harness.configure_env()
os.environ["DEMO_ACCOUNTS_FILE"] = os.environ["EVAL_DEMO"]

import httpx  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

import api.app as api_module  # noqa: E402
from evals import fixture  # noqa: E402
from guardrails.middleware import BLOCKED_SECRET_REPLY  # noqa: E402

OK = True


def check(label, cond):
    global OK
    OK &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label)


class Fake(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": f"id-{uuid.uuid4().hex}", "args": args}])


SCRIPT = iter([
    # balances
    call("task", subagent_type="accounts-agent", description="List the customer's balances."),
    call("list_my_accounts"),
    AIMessage(content="current £5,234.77, savings £1,201.12"),
    AIMessage(content="Your **current** account has £5,234.77 and your savings £1,201.12."),
    # £600 transfer, approved with a code
    call("task", subagent_type="accounts-agent", description="Transfer £600.00 from current to savings."),
    call("transfer_between_my_accounts", from_account="current", to_account="savings", amount=600),
    AIMessage(content="Transferred £600.00."),
    AIMessage(content="Done: £600.00 moved from current to savings."),
    # £20 transfer, declined
    call("task", subagent_type="accounts-agent", description="Transfer £20.00 from current to savings."),
    call("transfer_between_my_accounts", from_account="current", to_account="savings", amount=20),
    AIMessage(content="The customer declined."),
    AIMessage(content="You chose not to go ahead, so I haven't made the transfer."),
])


def latest_code() -> str:
    lines = (TMP / "outbox.jsonl").read_text().splitlines()
    return re.search(r"security code is (\d{6})", json.loads(lines[-1])["body"]).group(1)


async def balance(token, account):
    from agents.coordinator import load_mcp_tools
    tools = {t.name: t for t in (await load_mcp_tools(token))["accounts"]}
    out = await tools["get_balance"].ainvoke({"account": account})
    return json.loads(out[0]["text"] if isinstance(out, list) else out)["balance"]


async def main() -> int:
    app = api_module.create_app(model_factory=lambda: Fake(messages=SCRIPT))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bank.test") as a, \
            httpx.AsyncClient(transport=transport, base_url="http://bank.test") as b:
        cfg = (await a.get("/api/config")).json()
        check("sign-in options list the demo customers", [c["name"] for c in cfg["customers"]] == ["Robert", "Anil"])
        check("not signed in: /api/me is 401", (await a.get("/api/me")).status_code == 401)
        check("not signed in: chat is refused", (await a.post("/api/chat", json={"message": "hi"})).status_code == 401)

        r = await a.post("/api/login", json={"customer": "A"})
        cookie = r.headers.get("set-cookie", "")
        check("sign in as Robert; session cookie is HttpOnly and SameSite=Strict",
              r.status_code == 200 and "httponly" in cookie.lower() and "samesite=strict" in cookie.lower())
        check("the browser never gets the bank token", "fake-token" not in r.text and "fake-token" not in cookie)

        r = (await a.post("/api/chat", json={"message": "What are my balances?"})).json()
        thread = r["thread_id"]
        check("chat answers with the balances", r["status"] == "answered" and "5,234.77" in r["answer"])
        check("what happened: specialist and tool listed",
              r["steps"][0]["label"] == "Accounts specialist" and r["steps"][1]["name"] == "list_my_accounts")
        check("cost reported for the turn", r["cost"].startswith("$"))

        before = await balance("fake-token-A", "savings")
        r = (await a.post("/api/chat", json={"message": "Move £600 to savings", "thread_id": thread})).json()
        check("transfer stops at a confirmation slip", r["status"] == "needs_confirmation"
              and r["slip"]["title"] == "Transfer between your accounts" and ["Amount", "£600.00"] in r["slip"]["rows"])
        busy = await a.post("/api/chat", json={"message": "hello?", "thread_id": thread})
        check("can't chat past a waiting slip (409, slip returned)", busy.status_code == 409 and busy.json()["slip"])

        r = (await a.post("/api/confirm", json={"thread_id": thread, "approve": True})).json()
        check("approve asks for the emailed code", r["status"] == "code_required" and "@" in r["sent_to"])
        r = (await a.post("/api/confirm", json={"thread_id": thread, "approve": True, "code": "000000x"})).json()
        check("wrong code: tries left shown", r["status"] == "wrong_code" and r["attempts_left"] == 2)
        code = latest_code()
        r = (await a.post("/api/confirm", json={"thread_id": thread, "approve": True, "code": code})).json()
        after = await balance("fake-token-A", "savings")
        check("right code: transfer done and answered", r["status"] == "answered" and round(after - before, 2) == 600.0)

        r = (await a.post("/api/chat", json={"message": "Move £20 to savings", "thread_id": thread})).json()
        r = (await a.post("/api/confirm", json={"thread_id": thread, "approve": False})).json()
        check("decline: nothing moved, answer says so", r["status"] == "answered" and "chose not to go ahead" in r["answer"]
              and await balance("fake-token-A", "savings") == after)

        r = (await a.post("/api/chat", json={"message": "my card is 4111 1111 1111 1111, why declined?",
                                             "thread_id": thread})).json()
        check("card number blocked by the guardrail", r["answer"] == BLOCKED_SECRET_REPLY
              and any("card_number" in g for g in r["guard"]))

        hist = (await a.get(f"/api/history?thread_id={thread}")).json()
        texts = " ".join(m["text"] for m in hist["messages"])
        check("history has the conversation", "What are my balances?" in texts and "5,234.77" in texts)
        check("history never contains the card number or the code", "4111" not in texts and code not in texts)

        await b.post("/api/login", json={"customer": "B"})
        check("Anil can't read Robert's conversation",
              (await b.get(f"/api/history?thread_id={thread}")).status_code == 404)
        check("Anil can't confirm on Robert's conversation",
              (await b.post("/api/confirm", json={"thread_id": thread, "approve": True})).status_code == 404)
        check("made-up thread ids are refused", (await a.get("/api/history?thread_id=A-../../x")).status_code == 404)

        page = await a.get("/")
        check("chat page served", page.status_code == 200 and "Demo Bank" in page.text)
        check("security headers set", "frame-ancestors 'none'" in page.headers.get("content-security-policy", "")
              and page.headers.get("x-content-type-options") == "nosniff")

        api_module.RATE_LIMIT = 3
        codes = [(await b.get("/api/me")).status_code for _ in range(5)]
        check("rate limit kicks in", 429 in codes)
        api_module.RATE_LIMIT = 30

        await a.post("/api/logout")
        check("signed out: /api/me is 401", (await a.get("/api/me")).status_code == 401)

    print("\nALL API CHECKS PASSED" if OK else "\nSOME CHECKS FAILED")
    return 0 if OK else 1


if __name__ == "__main__":
    servers = harness.start_servers()
    try:
        code = asyncio.run(main())
    finally:
        harness.stop_servers(servers)
    sys.exit(code)
