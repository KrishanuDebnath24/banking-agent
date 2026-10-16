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

## 7. Phase 3: guardrails, costs and evals

### Guardrails: three layers, cheapest first

| Layer | What it does | Cost |
|---|---|---|
| 1. Rules (`guardrails/`, LangChain middleware) | Blocks card numbers, CVVs, PINs, passwords and security codes typed into the chat and removes them from the conversation before any model, store or trace sees them. Masks emails and phone numbers. Masks card/IBAN/account numbers and internal ids in replies. Caps model and tool calls per turn. | free, instant, always on |
| 2. Amazon Bedrock Guardrails | Prompt attacks (jailbreaks, injection), harmful content, denied topics (investment, tax and legal advice), ML-detected card data, and whether the answer is grounded in what the tools returned. Works with Claude on the Anthropic API too. | about $0.10-0.15 per 1,000 short checks |
| 3. Hard controls (phase 2) | Server-side account checks, y/N confirmations, emailed security codes, the £850 limit. The real security boundary: even if 1 and 2 miss something, the agent can't move money or see another customer's data. | |

Bedrock starts in **shadow mode**: it logs what it would block. Run the evals, check it doesn't block
normal questions, then set `GUARDRAIL_MODE=enforce`. To set it up (needs `aws configure`):

```bash
python -m scripts.setup_guardrail --test     # creates it from guardrails/bedrock_policy.py
```

LangSmith traces are redacted before they leave your PC (codes, card data, emails, phones, IBANs,
postcodes, address fields). `LANGSMITH_HIDE_CONTENT=true` sends no message text at all.

### Cost tracker

Every model call (coordinator and subagents) and every Bedrock check is priced and shown after
each turn (`[cost] $0.00093 · 4 model calls · 9.2k tokens · 3.1s`), and saved to `telemetry.db`.

```bash
python -m scripts.cost_report --days 30      # per day, customer, agent, model, and guardrails
```

### Eval suite

39 cases on a fixed fake bank (`evals/fixture.py`), so answers are known and transfers are safe:
balances, spending, statements, follow-ups, write actions, security, prompt injection (including
instructions hidden in a transaction description), PII and scope. Code checks the figures, the
subagents and tools used, proposed actions, leaks, guardrails and efficiency; Claude Sonnet 5.5
judges refusals and grounding with `--judge`.

```bash
python -m evals.run                          # all cases (+ a LangSmith experiment if a key is set)
python -m evals.run --judge                  # add the judge
python -m evals.run --category security --repeat 3
python -m evals.run --list
```

Gates (exit code 1 if any fails): security, injection and PII cases 100%; overall pass rate 90%;
average cost per case $0.01; p95 latency 45 s; no harness errors. `.github/workflows/ci.yml`
runs the offline tests on every push and the eval suite on pull requests, main and nightly.

Offline tests: `python tests/test_guardrails.py` and `python tests/test_evals_offline.py`

## 8. Phase 4, step 1: web chat

The same agents and safety rules, behind a web API and a chat page.

Terminal 1: `python -m mcp_servers.run`   Terminal 2: `python -m api`   Then open http://127.0.0.1:8000

- Sign in as a demo customer (Robert or Anil). On AWS this becomes the bank's sign-in (Cognito).
- Transfers and service requests appear as a payment slip with Approve and Decline. Address changes and
  transfers from £500 ask for the emailed code on the slip itself.
- "What happened" shows, for each answer, the specialist, the bank tools and arguments, any guardrail
  that stepped in, and the cost.
- The bank token stays on the server: the browser only holds a random session id (HttpOnly,
  SameSite=Strict cookie). Customers can only open their own conversations. Security headers and a
  per-session rate limit are on.

| Endpoint | What it does |
|---|---|
| `POST /api/login` | demo sign-in `{customer: "A"}` (AUTH_MODE=dev only) |
| `POST /api/chat` | `{message, thread_id?}` -> an answer, or a confirmation slip |
| `POST /api/confirm` | `{thread_id, approve, code?}` -> code needed, wrong code, or the answer |
| `GET /api/history` | a conversation's messages, and any slip still waiting |
| `GET /healthz` | for the load balancer |

Offline test (no keys): `python tests/test_api_offline.py`

## 9. Phase 4, step 2: Postgres and Docker

On AWS the app runs in containers, and files inside a container vanish when it restarts. So
everything the app saves now goes into one Postgres database when `DATABASE_URL` is set:
conversations (with any slip waiting for approval), security codes, service requests and the
cost ledger. Without `DATABASE_URL` nothing changes: SQLite files, as before. Requests from the old
`service_requests.jsonl` are copied into the database once, on first use.

**Install Docker Desktop** (free for personal use): https://docs.docker.com/desktop/setup/install/windows-install/
Use the WSL 2 option it suggests, restart, and open Docker Desktop once so it's running.

**Run the whole app in Docker**, the way AWS will run it (Postgres + bank servers + web chat):

```powershell
docker compose up --build          # first build takes a few minutes
# open http://127.0.0.1:8000
docker compose logs -f mcp         # in another terminal: security codes appear here (EMAIL_BACKEND=console)
docker compose down                # stop; conversations stay in the database (down -v deletes it)
```

Stop `python -m api` / `python -m mcp_servers.run` first if they're running (same ports).
Docker reads your `.env`, with three things to know:
- Model: Docker can't see `aws configure` credentials. With `MODEL=bedrock_converse:...`, put a Bedrock
  API key in `AWS_BEARER_TOKEN_BEDROCK`, or use `MODEL=anthropic:...` with `ANTHROPIC_API_KEY`. (On AWS the
  container gets credentials from its role.) Same for the Bedrock guardrail: without credentials it is
  skipped and logged, never blocking.
- A value containing `$` needs single quotes in `.env`: `PASSWORD='ab$cd'`.
- `demo_accounts.json` and `seed_log.jsonl` are copied into the image; `.env` never is.

**Or keep running Python on your PC with Postgres from Docker:**

```powershell
docker compose up -d db
# in .env:  DATABASE_URL=postgresql://bank:bank-local-only@localhost:5432/bank
python -m mcp_servers.run          # both say "Records: Postgres bank@localhost:5432/bank"
python -m api
```

Look inside the database: `docker compose exec db psql -U bank -d bank -c "\dt"`

**Tests on Postgres** (with `docker compose up -d db` running; each run uses its own throwaway schema):

```powershell
python tests/test_storage.py                    # SQLite only
$env:TEST_DATABASE_URL="postgresql://bank:bank-local-only@localhost:5432/bank"
python tests/test_storage.py                    # SQLite, then the same checks on Postgres
python tests/test_api_offline.py                # the web API end to end on Postgres
Remove-Item Env:TEST_DATABASE_URL
```

## 10. Phase 4, step 3: the AWS setup as code

`infra/` describes everything the app needs on AWS, in Python (AWS CDK). One command creates it,
one deletes it, so it only exists (and costs) while you're demoing.

| Stack | What it is | Cost |
|---|---|---|
| `BankBudget` | an email at 50%, 80% and 100% of $25 a month, and when the month is heading past it. Counts spending *before* credits, so it warns while credits are paying | free; keep it |
| `BankApp` | network without a NAT gateway; the smallest Postgres (in subnets with no internet route); one Fargate task running the web chat and the bank servers side by side; a load balancer only your address can reach (`ALLOWED_CIDR`); logs kept a week | about $0.09 an hour while it exists, so a 3-hour demo costs about $0.30, a forgotten day about $2 |

Passwords and API keys never go into the templates: `python -m infra.app_secrets push` copies them
from `.env` to AWS Secrets Manager, and the containers read them from there when they start.

**One-time setup** (from the project folder, `.venv` active):

```powershell
pip install -r infra/requirements.txt
npm install -g aws-cdk
aws sts get-caller-identity                              # which AWS login you're using
aws freetier get-account-plan-state --region us-east-1   # FREE or PAID plan
```

**Step 3: the budget alarm only.** Put `BUDGET_EMAIL=you@example.com` in `.env`, then:

```powershell
cd infra
cdk bootstrap            # once per account and region: CDK's own small bucket and image store
cdk deploy BankBudget    # creates the budget alarm
cdk diff BankApp         # lists what step 4 will create; creates nothing
cd ..
```

**Checks without AWS** (also run on GitHub): `python infra/tests/test_stacks.py`. These build the
templates and check, for example: no NAT gateway, database not public, only your address reaches the
load balancer, no password in any template, and the AWS security checks (cdk-nag), where every
finding is either fixed or accepted with a written reason in `infra/stacks/bank.py`.

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
guardrails/pii.py              deterministic PII detection and masking
guardrails/middleware.py       input/output guardrails, contact masking, call limits
guardrails/bedrock.py          Amazon Bedrock Guardrails client (shadow/enforce)
guardrails/bedrock_policy.py   the Bedrock guardrail, as code
observability/costs.py         per-turn cost meter and the ledger
observability/tracing.py       PII redaction for LangSmith traces
scripts/setup_guardrail.py     create/update the Bedrock guardrail
scripts/cost_report.py         cost report from the ledger
evals/fixture.py               the fixed eval bank and its oracle
evals/cases.py                 the 39 eval cases
evals/harness.py               runs a case against the real agents
evals/evaluators.py            code scorers + the Claude judge
evals/run.py                   the eval CLI, gates, LangSmith experiments
tests/test_guardrails.py       guardrails, costs, trace redaction
tests/test_evals_offline.py    the eval machinery, with a scripted model
.github/workflows/ci.yml       offline tests + eval gates in CI
api/app.py                     web API: sessions, chat, confirmations with codes, history
api/actions.py                 what each action looks like on the confirmation slip
web/                           the chat page (index.html, app.css, app.js)
tests/test_api_offline.py      the web API end to end, with a scripted model
storage/__init__.py            where records go: Postgres (DATABASE_URL) or SQLite files
tests/test_storage.py          every kind of record, on SQLite and Postgres
tests/pg_scratch.py            a throwaway Postgres schema per test run
Dockerfile                     one image: web chat (default) or bank servers
docker-compose.yml             Postgres + bank servers + web chat on your PC
infra/app.py                   the AWS setup (CDK): BankBudget and BankApp
infra/settings.py              which .env settings go to AWS (and that secrets never do)
infra/stacks/budget.py         the budget alarm
infra/stacks/bank.py           network, database, Fargate task, load balancer, logs
infra/app_secrets.py           copy passwords and keys from .env to Secrets Manager
infra/tests/test_stacks.py     checks on the AWS setup, no AWS account needed
```
