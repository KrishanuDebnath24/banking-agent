# Banking Agent Demo

Multi-agent banking assistant (Deep Agents on AWS) using the Open Bank Project sandbox.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # fill in your consumer key and the two customer logins
```

## 1. Check the OBP connection

```bash
python -m scripts.smoke_test
```

Logs in customers A and B, lists A's accounts, balances and transactions,
then confirms B is refused when reading A's account.

## 2. Pick demo accounts

```bash
python -m scripts.list_accounts           # table of accounts + a suggestion
python -m scripts.list_accounts --write   # save the suggestion to demo_accounts.json
```

Each customer gets four accounts:

| Role | Shown to the agent | Plays |
|---|---|---|
| current | yes | everyday account |
| savings | yes | savings / tax pot |
| income | no | employer or clients |
| merchants | no | shops and billers |

### If customer B has no accounts

Shared sandbox logins are often locked or empty. If B shows no accounts:

```bash
python -m scripts.setup_customer_b            # dry run
python -m scripts.setup_customer_b --apply    # opens 4 accounts for B, funds B's income from A
```

## 3. Seed realistic transactions

```bash
python -m scripts.seed_transactions            # dry run: prints the plan
python -m scripts.seed_transactions --apply    # creates the transfers
```

Options: `--customer A`, `--cycles 2` (months of activity). Every transfer is
logged to `seed_log.jsonl`. OBP dates transactions when they are created, so
seeded data carries today's date.

## 4. Chat with the phase 1 agent

```bash
pip install -r requirements.txt     # adds LangChain, LangGraph, LangSmith
python -m scripts.chat              # as customer A
python -m scripts.chat --customer B
```

Set `MODEL` (and `OLLAMA_BASE_URL` for Ollama) plus the LangSmith settings in `.env`,
then check the model: `python -m scripts.check_llm`.
Try: "what are my balances?", "how much did I spend on groceries?",
"what did I pay in rent?", "show me account r9y4bwhc43woma1gxvg8k4r5q" (should be refused).

Offline test (no keys needed): `python tests/test_agent_offline.py`

## 5. Phase 2: coordinator + 3 subagents over MCP

```
coordinator ──task──> accounts-agent      ── Accounts MCP     :8101  balances, own-account transfers
            ──task──> transactions-agent  ── Transactions MCP :8102  history, totals, statements
            ──task──> service-agent       ── Service MCP      :8103  profile, address, service requests
chat app  ──────────────────────────────────── Verify service   :8104  security codes (not MCP, not the AI)
```

Terminal 1 (leave running):

```bash
python -m mcp_servers.run
```

Terminal 2:

```bash
python -m scripts.chat_multi                 # customer A
python -m scripts.chat_multi --customer B
python -m scripts.chat_multi --thread A-1a2b3c4d   # continue a saved conversation
```

- The customer's OBP token travels only in MCP request headers. Each server asks OBP who
  the token belongs to and allows only that customer's current and savings accounts.
- Transfers (up to £850), address changes, cheque books and KYC updates stop and ask you
  to confirm before they run.
- Address changes and transfers from £500 also ask for a security code emailed to you (see 6).
- A new address is saved at the bank, on the customer's own OBP profile, and the agent reads it
  back from there. Transfers are real OBP transactions on both accounts.
- Cheque books and KYC updates get an `SR-` reference, are logged in `service_requests.jsonl`
  (the back office queue) and sent to the bank as a customer message. Address changes are
  logged there too, as completed.
- Conversations and pending confirmations are saved in `sessions.db`.

Try: "move £20 from current to savings", "move £600 from current to savings" (asks for a code),
"what did I spend on groceries this month?", "give me a statement for my savings",
"I've moved to 12 Main Street, Maynooth, W23 X1Y2" (asks for a code), "what address do you have
for me?", "order a cheque book", "what service requests do I have?", and as Robert ask for the
balance of Anil's current account by its ID from `demo_accounts.json` (refused).

Offline test (no keys, fake bank): `python tests/test_multi_agent_offline.py`

## 6. Security codes

Address changes, and transfers from `OTP_TRANSFER_THRESHOLD` (default £500) up to the £850
limit, need a 6-digit code:

```
you>  move £600 from current to savings
  CONFIRM: Transfer £600.00 from current to savings
  Go ahead? [y/N] y
  We've emailed a 6-digit security code to k***@gmail.com. It expires in 5 minutes.
  Security code: 482913
bank> Done. £600.00 moved; current is now ...
```

- The code goes from your keyboard straight to the verify service (port 8104). It never enters
  the conversation, so the AI model, `sessions.db` and LangSmith never see it.
- Each code is tied to one exact action (same accounts and amount, or same address), works once,
  expires after 5 minutes and dies after 3 wrong tries. The email says what it approves.
- The MCP tools themselves refuse to transfer or change the address without a confirmed code,
  so the step can't be skipped by the model or by calling the servers directly.
- Transfers between your own accounts don't need a code under UK/EU rules; the demo asks for
  larger ones anyway to show the step.

Setup: with `EMAIL_BACKEND=console` (the default) codes are printed in the MCP server window.
For real email, use a Gmail account: turn on 2-Step Verification, create an app password at
https://myaccount.google.com/apppasswords, then in `.env` set `EMAIL_BACKEND=smtp`,
`SMTP_USER`, `SMTP_PASSWORD` (the app password) and `OTP_EMAIL_TO` (the inbox for codes). Check:

```bash
python -m scripts.check_codes     # settings, a test email, and saving to the bank profile
```

## Layout

```
obp/client.py                  Direct Login, reads, transfers, customer messages
scripts/smoke_test.py          connection and authorisation check
scripts/list_accounts.py       survey accounts, write demo_accounts.json
scripts/setup_customer_b.py    open and fund accounts for B
scripts/probe_limit.py         find the largest transfer that needs no approval
scripts/seed_transactions.py   create realistic activity
agents/context.py              per-customer token + allowed accounts (never shown to the model)
agents/tools.py                read tools with account checks; totals computed in code
agents/llm.py                  model from .env: Bedrock, Ollama or Anthropic
agents/agent.py                phase 1 single agent
scripts/check_llm.py           check the model answers and can call tools
scripts/chat.py                terminal chat with tracing
tests/test_agent_offline.py    fake model + fake bank end-to-end test
obp/fake.py                    in-memory fake bank (OBP_FAKE=1)
mcp_servers/common.py          identity from the token, allowed accounts, filters
mcp_servers/accounts/server.py      Accounts MCP server
mcp_servers/transactions/server.py  Transactions MCP server
mcp_servers/services/server.py      Service MCP server
mcp_servers/verification.py    security codes: email, checks, the verify service
mcp_servers/run.py             start the servers
scripts/check_codes.py         check code email + bank profile storage
scripts/probe_challenge.py     showed OBP needs a second approver above the limit
agents/coordinator.py          phase 2: Deep Agents coordinator + 3 subagents
scripts/chat_multi.py          phase 2 terminal chat with confirmations
tests/test_multi_agent_offline.py  phase 2 end-to-end test
```
