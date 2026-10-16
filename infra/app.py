"""The AWS setup as code (AWS CDK). Run the cdk commands from this folder:

    cdk deploy BankBudget      the budget alarm (free; keep it)
    cdk diff BankApp           what the app would create, without creating anything
    cdk deploy BankApp         create the app (phase 4, step 4)
    cdk destroy BankApp        delete it after the demo

Settings come from ../.env (see settings.py). Passwords and keys never do.
"""

from __future__ import annotations

import aws_cdk as cdk

from settings import Settings
from stacks.bank import BankStack
from stacks.budget import BudgetStack

settings = Settings.load()
env = cdk.Environment(account=settings.account, region=settings.region)
app = cdk.App()

if settings.budget_email:
    BudgetStack(app, "BankBudget", env=env, limit_usd=settings.budget_usd, email=settings.budget_email,
                description="banking-agent: monthly budget alarm (free)")
else:
    print("Note: BUDGET_EMAIL isn't set in .env, so the BankBudget stack is skipped.")
BankStack(app, "BankApp", env=env, settings=settings,
          description="banking-agent: web chat, bank servers and database for a demo")

app.synth()
