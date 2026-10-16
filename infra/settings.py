"""What the AWS setup reads from your .env, and what it never reads.

Three kinds of setting:
  - infrastructure: region, budget, who may open the app (BUDGET_*, ALLOWED_CIDR, AWS_REGION)
  - app settings that aren't secret: model name, bank URL, usernames... -> plain container settings
  - secrets (passwords, API keys): NOT read here. They go from .env to AWS Secrets Manager with
    python -m infra.secrets push, and the containers read them from there at start-up. So they
    never appear in the templates CDK writes, in CloudFormation, or in the console.
"""

from __future__ import annotations

import ipaddress
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
)

# Kept in Secrets Manager (python -m infra.secrets push); the containers get them at start-up.
SECRET_KEYS = (
    "OBP_CONSUMER_KEY", "OBP_CONSUMER_SECRET", "CUSTOMER_A_PASSWORD", "CUSTOMER_B_PASSWORD",
    "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "SMTP_PASSWORD",
)


def read_env(path: Path | None = None) -> dict[str, str]:
    """.env values (without touching this process's environment), then real environment variables."""
    path = path or Path(os.environ.get("BANK_ENV_FILE", ROOT / ".env"))
    values = {k: v for k, v in dotenv_values(path).items() if v is not None} if path.exists() else {}
    for key in ("AWS_REGION", "BUDGET_USD", "BUDGET_EMAIL", "ALLOWED_CIDR", *PLAIN_KEYS):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


@dataclass
class Settings:
    region: str = "eu-west-1"
    account: str | None = None
    budget_usd: float = 25.0
    budget_email: str = ""
    allowed_cidr: str = ""                       # who may open the app until sign-in and HTTPS arrive
    app_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> "Settings":
        env = read_env() if env is None else env
        cidr = env.get("ALLOWED_CIDR", "").strip()
        if cidr:
            ipaddress.ip_network(cidr, strict=False)  # fails loudly on a typo
            if cidr.endswith("/0"):
                raise ValueError("ALLOWED_CIDR must be your own address (like 81.2.3.4/32), not the whole internet.")
        return cls(region=env.get("AWS_REGION", "").strip() or "eu-west-1",
                   account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                   budget_usd=float(env.get("BUDGET_USD", "").strip() or 25),
                   budget_email=env.get("BUDGET_EMAIL", "").strip(),
                   allowed_cidr=cidr,
                   app_env={k: env[k] for k in PLAIN_KEYS if env.get(k, "").strip()})
