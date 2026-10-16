"""Transactions MCP server: history, spending summaries and statements.

All totals are computed here in code, so answers never depend on the model's arithmetic.
"""

from __future__ import annotations

import json
from collections import defaultdict

from mcp.server.fastmcp import Context, FastMCP

from mcp_servers.common import (PORTS, OBPError, bank, customer, error, parse_date, rows_for,
                                 transport_security)

mcp = FastMCP("transactions", port=PORTS["transactions"], stateless_http=True, json_response=True,
              transport_security=transport_security(),
              instructions="Transaction history, spending summaries and statements for the logged-in customer.")


async def _rows(ctx, account, search, from_date, to_date):
    """Rows per account. account=None means all of the customer's accounts."""
    cust = await customer(ctx)
    if account is None:
        ids = list(cust.accounts.values())
    else:
        acc_id = cust.resolve(account)
        if acc_id is None:
            return cust, None
        ids = [acc_id]
    f, t = parse_date(from_date, "from_date"), parse_date(to_date, "to_date")
    out = {}
    for acc_id in ids:
        raw = await bank(cust.obp.transactions, cust.token, cust.bank_id, acc_id)
        out[cust.name_of(acc_id)] = rows_for(cust, raw, f, t, search)
    return cust, out


def _requested(from_date, to_date) -> dict:
    """The period the caller asked for, so the answer can state it (not guess it from the data)."""
    return {"from": parse_date(from_date, "from_date") or "earliest available",
            "to": parse_date(to_date, "to_date") or "latest available"}


def _period(rows_by_acc: dict) -> dict:
    days = [r["date"] for rows in rows_by_acc.values() for r in rows if r["date"]]
    return {"first_transaction": min(days) if days else None, "last_transaction": max(days) if days else None}


@mcp.tool()
async def get_transactions(ctx: Context, account: str | None = None, search: str | None = None,
                           from_date: str | None = None, to_date: str | None = None, limit: int = 30) -> str:
    """List transactions, newest first. Negative amounts are money out, positive are money in.

    Args:
        account: "current" or "savings"; leave empty for all accounts.
        search: optional comma-separated words to match in the description, e.g. "tesco,lidl".
        from_date: optional first day, YYYY-MM-DD.
        to_date: optional last day, YYYY-MM-DD.
        limit: maximum transactions per account (default 30, max 100).
    """
    try:
        cust, by_acc = await _rows(ctx, account, search, from_date, to_date)
        if by_acc is None:
            return cust.denied()
        limit = max(1, min(int(limit), 100))
        return json.dumps({"currency": cust.currency, "requested_period": _requested(from_date, to_date),
                           "transactions_found_between": _period(by_acc),
                           "accounts": {a: {"count": len(r), "transactions": r[:limit]} for a, r in by_acc.items()}})
    except ValueError as e:
        return error("invalid_input", message=str(e))
    except PermissionError as e:
        return error("not_authorised", detail=str(e))
    except OBPError as e:
        return error("bank_error", status=e.status)


@mcp.tool()
async def summarize_spending(ctx: Context, account: str | None = None, search: str | None = None,
                             from_date: str | None = None, to_date: str | None = None) -> str:
    """Totals for one or all accounts: money in, money out, spending per payee and income per payer,
    plus the date range covered. Use for any "how much did I spend / earn" question.

    Args:
        account: "current" or "savings"; leave empty for all accounts in one call.
        search: optional comma-separated payee words, e.g. "tesco,sainsbury,lidl" for groceries.
        from_date: optional first day, YYYY-MM-DD.
        to_date: optional last day, YYYY-MM-DD.
    """
    try:
        cust, by_acc = await _rows(ctx, account, search, from_date, to_date)
        if by_acc is None:
            return cust.denied()
        accounts, total_in, total_out = {}, 0.0, 0.0
        for name, rows in by_acc.items():
            spend, income = defaultdict(float), defaultdict(float)
            counts: dict[str, int] = defaultdict(int)
            for r in rows:
                (spend if r["amount"] < 0 else income)[r["description"]] += abs(r["amount"])
                counts[r["description"]] += 1
            m_in = round(sum(income.values()), 2)
            m_out = round(sum(spend.values()), 2)
            total_in += m_in
            total_out += m_out
            accounts[name] = {
                "transactions": len(rows), "money_in": m_in, "money_out": m_out,
                "spending_by_payee": [{"payee": p, "total": round(v, 2), "count": counts[p]}
                                      for p, v in sorted(spend.items(), key=lambda kv: -kv[1])],
                "income_by_payer": [{"payer": p, "total": round(v, 2), "count": counts[p]}
                                    for p, v in sorted(income.items(), key=lambda kv: -kv[1])],
            }
        return json.dumps({"currency": cust.currency, "filter": search or "none",
                           "requested_period": _requested(from_date, to_date),
                           "transactions_found_between": _period(by_acc),
                           "accounts": accounts,
                           "totals": {"money_in": round(total_in, 2), "money_out": round(total_out, 2)},
                           "note": "Transfers between the customer's own accounts appear as money out of one "
                                   "account and money in to the other."})
    except ValueError as e:
        return error("invalid_input", message=str(e))
    except PermissionError as e:
        return error("not_authorised", detail=str(e))
    except OBPError as e:
        return error("bank_error", status=e.status)


@mcp.tool()
async def get_statement(account: str, ctx: Context, from_date: str | None = None, to_date: str | None = None) -> str:
    """A statement for one account: opening balance, each transaction in date order with the
    running balance, and closing balance.

    Args:
        account: "current" or "savings".
        from_date: optional first day, YYYY-MM-DD.
        to_date: optional last day, YYYY-MM-DD.
    """
    try:
        cust, by_acc = await _rows(ctx, account, None, from_date, to_date)
        if by_acc is None:
            return cust.denied()
        name, rows = next(iter(by_acc.items()))
        lines = list(reversed(rows))  # oldest first
        closing = float(lines[-1]["balance_after"]) if lines and lines[-1]["balance_after"] is not None else None
        opening = round(closing - sum(r["amount"] for r in lines), 2) if closing is not None else None
        found = _period(by_acc)
        period = {"from": parse_date(from_date, "from_date") or found["first_transaction"],
                  "to": parse_date(to_date, "to_date") or found["last_transaction"]}
        return json.dumps({"account": name, "currency": cust.currency, "statement_period": period,
                           "transactions_found_between": found,
                           "opening_balance": opening, "opening_balance_date": period["from"],
                           "closing_balance": closing, "closing_balance_date": period["to"],
                           "money_in": round(sum(r["amount"] for r in lines if r["amount"] > 0), 2),
                           "money_out": round(-sum(r["amount"] for r in lines if r["amount"] < 0), 2),
                           "lines": lines})
    except ValueError as e:
        return error("invalid_input", message=str(e))
    except PermissionError as e:
        return error("not_authorised", detail=str(e))
    except OBPError as e:
        return error("bank_error", status=e.status)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
