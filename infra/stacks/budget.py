"""BankBudget: an email when AWS spending heads past the monthly limit. Free; stays when the app is deleted.

It counts spending before credits, so it also warns while your credits are paying: otherwise
the bill after credits stays at $0 and the alarm would only fire once the credits are gone.
AWS updates budget figures a few times a day, so an alert can come hours after the spending.
"""

from __future__ import annotations

from aws_cdk import Stack, aws_budgets as budgets
from constructs import Construct


class BudgetStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, limit_usd: float, email: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        if not email:
            raise ValueError("Set BUDGET_EMAIL in .env: the address the budget alerts go to.")

        def alert(kind: str, percent: float):
            return budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type=kind, comparison_operator="GREATER_THAN",
                    threshold=percent, threshold_type="PERCENTAGE"),
                subscribers=[budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=email)])

        budgets.CfnBudget(self, "Monthly",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="banking-agent-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=limit_usd, unit="USD"),
                cost_types=budgets.CfnBudget.CostTypesProperty(include_credit=False, include_refund=False)),
            notifications_with_subscribers=[
                alert("ACTUAL", 50), alert("ACTUAL", 80), alert("ACTUAL", 100),
                alert("FORECASTED", 100),   # "at this rate you'll pass the limit this month"
            ])
