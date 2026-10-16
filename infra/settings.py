"""What the AWS setup reads from your .env, and what it never reads.

Three kinds of setting:
  - infrastructure: region, budget, opening hours, who may sign in (AWS_REGION, BUDGET_*, OPEN_HOURS,
    OPEN_TO_PUBLIC, APP_MAX_COPIES)
  - app settings that aren't secret: model name, bank URL, usernames... -> plain container settings
  - secrets (passwords, API keys): NOT read here. They go from .env to AWS Secrets Manager with
    python -m infra.app_secrets push, and the containers read them from there at start-up. So they
    never appear in the templates CDK writes, in CloudFormation, or in the console.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "banking-agent"
SECRET_NAME = f"{PROJECT}/app"

# Copied from .env into the containers as plain settings (nothing secret in this list).
PLAIN_KEYS = (
    "OBP_BASE_URL", "OBP_API_VERSION", "OBP_BANK_ID", "OBP_MAX_TRANSFER", "OBP_FAKE",
    "CUSTOMER_A_USERNAME", "CUSTOMER_B_USERNAME",
    "MODEL", "LANGSMITH_TRACING", "LANGSMITH_PROJECT", "LANGSMITH_HIDE_CONTENT",
    "OTP_TRANSFER_THRESHOLD", "OTP_EMAIL_TO", "OTP_EMAIL_TO_A", "OTP_EMAIL_TO_B",
    "EMAIL_BACKEND", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "EMAIL_FROM",
    "GUARDRAIL_ID", "GUARDRAIL_VERSION", "GUARDRAIL_REGION", "GUARDRAIL_MODE", "GUARDRAIL_ON_ERROR",
    "API_RATE_LIMIT_PER_MIN", "COST_ALERT_DAILY_USD",
    "GUEST_TTL_HOURS", "GUEST_DAILY_MESSAGES", "GUEST_SIGNUPS_PER_IP_HOUR", "DAILY_SPEND_CAP_USD",
    "GOOGLE_CLIENT_ID", "OWNER_EMAILS", "INVITED_EMAILS", "CONTACT_EMAIL",
    "SIGNIN_CODES_PER_EMAIL", "SIGNIN_CODES_PER_IP_HOUR", "SIGNIN_EMAILS_PER_DAY",
)

# Kept in Secrets Manager (python -m infra.secrets push); the containers get them at start-up.
SECRET_KEYS = (
    "OBP_CONSUMER_KEY", "OBP_CONSUMER_SECRET", "CUSTOMER_A_PASSWORD", "CUSTOMER_B_PASSWORD",
    "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "SMTP_PASSWORD",
    "APP_SECRET",   # signs visitors' bank tokens; the web chat and the bank servers must share it
    "GOOGLE_CLIENT_SECRET",   # "Continue with Google"
    "ORIGIN_SECRET",          # CloudFront's secret header; made for you by  app_secrets push
)


def read_env(path: Path | None = None) -> dict[str, str]:
    """.env values (without touching this process's environment), then real environment variables."""
    path = path or Path(os.environ.get("BANK_ENV_FILE", ROOT / ".env"))
    values = {k: v for k, v in dotenv_values(path).items() if v is not None} if path.exists() else {}
    for key in ("AWS_REGION", "BUDGET_USD", "BUDGET_EMAIL", "OPEN_HOURS", "OPEN_TO_PUBLIC", "APP_MAX_COPIES",
                *PLAIN_KEYS):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def parse_hours(text: str) -> tuple[int, int] | None:
    """OPEN_HOURS like "09:00-17:00" -> (540, 1020) minutes after midnight; "always" -> None."""
    text = (text or "09:00-17:00").strip().lower()
    if text in ("always", "24h", "24/7"):
        return None
    try:
        start, end = (part.strip() for part in text.split("-"))
        minutes = [int(t.split(":")[0]) * 60 + int(t.split(":")[1]) for t in (start, end)]
    except (ValueError, IndexError):
        raise ValueError('OPEN_HOURS must look like 09:00-17:00 (or "always").') from None
    opens, closes = minutes
    # The database starts 30 minutes early and stops 10 minutes late, all on the same day.
    if not (30 <= opens < closes <= 23 * 60 + 45):
        raise ValueError("OPEN_HOURS must open after 00:30, close by 23:45, and close after it opens.")
    return opens, closes


@dataclass
class Settings:
    region: str = "eu-west-1"
    account: str | None = None
    budget_usd: float = 25.0
    budget_email: str = ""
    hours: tuple[int, int] | None = (9 * 60, 17 * 60)   # open 09:00-17:00; None = always open
    time_zone: str = "Europe/Dublin"
    open_to_public: bool = False                 # False: invite-only (OWNER_EMAILS + INVITED_EMAILS)
    max_copies: int = 3                          # copies of the app when busy
    app_env: dict[str, str] = field(default_factory=dict)

    @property
    def contact_email(self) -> str:
        """Shown on the privacy notice: CONTACT_EMAIL, else the first OWNER_EMAILS address."""
        owners = [e.strip() for e in self.app_env.get("OWNER_EMAILS", "").split(",") if e.strip()]
        return self.app_env.get("CONTACT_EMAIL", "").strip() or (owners[0] if owners else "the owner of this demo")

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> "Settings":
        env = read_env() if env is None else env
        copies = int(env.get("APP_MAX_COPIES", "").strip() or 3)
        if not 1 <= copies <= 10:
            raise ValueError("APP_MAX_COPIES must be between 1 and 10.")
        return cls(region=env.get("AWS_REGION", "").strip() or "eu-west-1",
                   account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                   budget_usd=float(env.get("BUDGET_USD", "").strip() or 25),
                   budget_email=env.get("BUDGET_EMAIL", "").strip(),
                   hours=parse_hours(env.get("OPEN_HOURS", "")),
                   time_zone=env.get("OPEN_TIME_ZONE", "").strip() or "Europe/Dublin",
                   open_to_public=env.get("OPEN_TO_PUBLIC", "0").strip() == "1",
                   max_copies=copies,
                   app_env={k: env[k] for k in PLAIN_KEYS if env.get(k, "").strip()})
