"""Charts MCP server: draws figures the transactions server already worked out and saved.

It has no bank access at all. Each tool takes a dataset id (ds-...), opens that dataset if it
belongs to the signed-in customer (mcp_servers/datasets.py), and returns a chart for the page to
draw, plus a one-line summary for the chart specialist to report. The figures in a chart therefore
come only from the bank server's code, never from the AI, and the same dataset can be drawn again
in another form without asking the bank.

Which form fits which figures:
    month-by-month totals (1-4 sets of figures)    column_chart, line_chart (up to 3), stacked_column_chart (2-4)
    a breakdown by category or payee                bar_chart (ranked), donut_chart (shares, up to 6)
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import Context, FastMCP

from mcp_servers import datasets
from mcp_servers.common import PORTS, customer, error, transport_security

mcp = FastMCP("charts", port=PORTS["charts"], stateless_http=True, json_response=True,
              transport_security=transport_security(),
              instructions="Draws charts of figures the bank has saved (dataset ids like ds-4f2a9c1e).")

MONTH_FORMS = ("column_chart", "line_chart", "stacked_column_chart")
BREAKDOWN_FORMS = ("bar_chart", "donut_chart")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _money(value: float, currency: str) -> str:
    return f"£{value:,.2f}" if currency == "GBP" else f"{value:,.2f} {currency}"


def _month(key: str) -> str:
    year, month = key.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


async def _open(ctx, dataset_id: str) -> tuple[dict | None, str | None]:
    """(dataset, None) or (None, an error to return)."""
    try:
        cust = await customer(ctx)
    except PermissionError as e:
        return None, error("not_authorised", detail=str(e))
    found = datasets.load(cust.records_key, dataset_id)
    if found is None:
        return None, error("not_found", message="No saved figures with that id for this customer (they may "
                                                 "have expired). Ask for the figures again.")
    return found, None


def _wrong(form: str, data: dict) -> str:
    fits = MONTH_FORMS if data["kind"] == "monthly" else BREAKDOWN_FORMS
    what = "month-by-month totals" if data["kind"] == "monthly" else "a breakdown by " + data.get("by", "category")
    return error("wrong_form", message=f"{form} doesn't fit {what}.", forms_that_fit=list(fits))


def _title(title: str | None, fallback: str) -> str:
    return " ".join((title or "").split())[:60] or fallback


def _monthly(form: str, data: dict, title: str | None) -> str:
    series = data["series"]
    if form == "line" and len(series) > 3:
        return error("wrong_form", message="More than 3 lines can't be told apart.",
                     forms_that_fit=["column_chart", "stacked_column_chart"])
    if form == "stacked" and len(series) < 2:
        return error("wrong_form", message="A stacked chart needs at least two categories; this has one.",
                     forms_that_fit=["column_chart", "line_chart"])
    if len(data["months"]) < 2:
        return error("too_little", message="Only one month of figures: there's nothing to compare. "
                                           "Give the figure in words instead.")
    chart = {"form": form, "title": _title(title, f"{data['label']} by month"), "money": data["money"],
             "currency": data["currency"], "axis": "month", "by": "month", "labels": data["months"],
             "series": [{"name": s["name"], "values": s["by_month"], "counts": s["payments"]} for s in series],
             "notes": data.get("partial") or {}, "note": data.get("note"),
             "total": round(sum(s["total"] for s in series), 2), "period": data["period"],
             "dataset_id": data["dataset_id"]}
    cur = data["currency"]
    names = {"column": "column chart", "line": "line chart", "stacked": "stacked column chart"}
    parts = [f"Drew a {names[form]}: {chart['title']}, {_month(data['months'][0])} to {_month(data['months'][-1])}."]
    for s in series:
        top = max(range(len(s["by_month"])), key=lambda i: s["by_month"][i])
        parts.append(f"{s['name']}: total {_money(s['total'], cur)}, highest {_month(data['months'][top])} "
                     f"({_money(s['by_month'][top], cur)}).")
    if len(series) > 1:
        parts.append(f"All together: {_money(chart['total'], cur)}.")
    if chart["notes"]:
        parts.append("Part months: " + "; ".join(f"{_month(k)} {v}" for k, v in chart["notes"].items()) + ".")
    if chart["note"]:
        parts.append(chart["note"])
    return json.dumps({"chart": chart, "summary": " ".join(parts)})


def _fold(items: list[dict], keep: int) -> list[dict]:
    """The biggest `keep` items, and everything else together as "Other"."""
    other = [i for i in items if i["name"].lower() == "other"]
    rest = [i for i in items if i["name"].lower() != "other"]
    if len(rest) + len(other) <= keep + 1:
        return rest + other
    shown, folded = rest[:keep], rest[keep:] + other
    return shown + [{"name": "Other", "total": round(sum(i["total"] for i in folded), 2),
                     "payments": sum(i["payments"] for i in folded)}]


def _breakdown(form: str, data: dict, title: str | None, keep: int) -> str:
    items = [i for i in data["items"] if i["total"] > 0]
    if len(items) < 2:
        return error("too_little", message="Only one item: there's nothing to compare. Give the figure in words.")
    items = _fold(items, keep)
    chart = {"form": form, "title": _title(title, data["label"]), "money": data["money"],
             "currency": data["currency"], "axis": "name", "by": data.get("by", "category"),
             "labels": [i["name"] for i in items],
             "series": [{"name": data["label"], "values": [i["total"] for i in items],
                         "counts": [i["payments"] for i in items]}],
             "notes": {}, "note": None, "total": data["total"], "period": data["period"],
             "dataset_id": data["dataset_id"]}
    cur = data["currency"]
    shares = ", ".join(f"{i['name']} {_money(i['total'], cur)} ({i['total'] / data['total'] * 100:.0f}%)"
                       for i in items[:4])
    kind = "ranked bar chart" if form == "bar" else "donut chart"
    summary = (f"Drew a {kind}: {chart['title']}, {data['period']['from']} to {data['period']['to']}, total "
               f"{_money(data['total'], cur)}. Biggest: {shares}.")
    return json.dumps({"chart": chart, "summary": summary})


@mcp.tool()
async def column_chart(ctx: Context, dataset_id: str, title: str | None = None) -> str:
    """Columns, one per month: the default for month-by-month totals. With several categories,
    their columns stand side by side in each month.

    Args:
        dataset_id: the id the transactions specialist reported, e.g. "ds-4f2a9c1e".
        title: optional chart title; leave empty to use the figures' own.
    """
    data, problem = await _open(ctx, dataset_id)
    if problem:
        return problem
    return _monthly("column", data, title) if data["kind"] == "monthly" else _wrong("column_chart", data)


@mcp.tool()
async def line_chart(ctx: Context, dataset_id: str, title: str | None = None) -> str:
    """A line through the months: for trends over time; up to 3 categories as separate lines.

    Args:
        dataset_id: the id the transactions specialist reported, e.g. "ds-4f2a9c1e".
        title: optional chart title.
    """
    data, problem = await _open(ctx, dataset_id)
    if problem:
        return problem
    return _monthly("line", data, title) if data["kind"] == "monthly" else _wrong("line_chart", data)


@mcp.tool()
async def stacked_column_chart(ctx: Context, dataset_id: str, title: str | None = None) -> str:
    """Columns per month with 2-4 categories stacked on top of each other: each category's share
    and the month's combined total at once.

    Args:
        dataset_id: the id of month-by-month figures for 2-4 categories.
        title: optional chart title.
    """
    data, problem = await _open(ctx, dataset_id)
    if problem:
        return problem
    return _monthly("stacked", data, title) if data["kind"] == "monthly" else _wrong("stacked_column_chart", data)


@mcp.tool()
async def bar_chart(ctx: Context, dataset_id: str, title: str | None = None, top: int = 8) -> str:
    """Horizontal bars, biggest first: for a breakdown by category or payee. The rest are
    combined as "Other".

    Args:
        dataset_id: the id of a breakdown, e.g. "ds-4f2a9c1e".
        title: optional chart title.
        top: how many to show before "Other" (default 8, at most 12).
    """
    data, problem = await _open(ctx, dataset_id)
    if problem:
        return problem
    if data["kind"] != "breakdown":
        return _wrong("bar_chart", data)
    return _breakdown("bar", data, title, max(2, min(int(top), 12)))


@mcp.tool()
async def donut_chart(ctx: Context, dataset_id: str, title: str | None = None) -> str:
    """A ring split into shares of the whole: for a breakdown, when the customer asks for a pie or
    donut. Shows the 5 biggest and "Other" (more slices can't be told apart).

    Args:
        dataset_id: the id of a breakdown, e.g. "ds-4f2a9c1e".
        title: optional chart title.
    """
    data, problem = await _open(ctx, dataset_id)
    if problem:
        return problem
    if data["kind"] != "breakdown":
        return _wrong("donut_chart", data)
    return _breakdown("donut", data, title, 5)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
