"""The AWS setup as code (AWS CDK). Run the cdk commands from this folder:

    cdk deploy BankBudget      the budget alarm (free; keep it)
    cdk diff BankApp           what the app would create or change, without doing it
    cdk deploy BankApp         create or update the app (open OPEN_HOURS, 09:00-17:00 Irish time)
    cdk destroy BankApp        delete it (visitors' banks go with the database; OBP data stays)

Settings come from ../.env (see settings.py). Passwords and keys never do.
"""

from __future__ import annotations

import sys

import aws_cdk as cdk

from settings import Settings
from stacks.bank import BankStack
from stacks.budget import BudgetStack

settings = Settings.load()
if not settings.account:   # the cdk command fills this in from your AWS sign-in
    sys.exit("\nCan't tell which AWS account to use, so nothing was changed: the cdk command couldn't find\n"
             "your AWS sign-in (it has probably run out). Run  aws login  and try again.\n"
             "If it still says this, run this once in the same PowerShell window, then try again:\n"
             "  aws configure export-credentials --format powershell | Invoke-Expression\n")
env = cdk.Environment(account=settings.account, region=settings.region)
app = cdk.App()

if settings.budget_email:
    BudgetStack(app, "BankBudget", env=env, limit_usd=settings.budget_usd, email=settings.budget_email,
                description="banking-agent: monthly budget alarm (free)")
else:
    print("Note: BUDGET_EMAIL isn't set in .env, so the BankBudget stack is skipped.")
BankStack(app, "BankApp", env=env, settings=settings,
          description="banking-agent: the assistant on AWS, open during OPEN_HOURS")

app.synth()
