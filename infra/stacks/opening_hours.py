"""The CloudFront Function in front of the app: opening hours, and the visitor's address.

It runs at the edge on every request (cheap: 2 million a month are free), before anything reaches
API Gateway or the app:
  - it answers /privacy.html itself (web/privacy.html), so the privacy notice Google links to is
    there at any hour;
  - outside opening hours it answers itself with a "closed" page (or, for the page's background
    requests, a short JSON message), so nobody sees an error while the app is switched off;
  - it copies the visitor's address into X-Viewer-Ip (replacing any header a visitor sent), which
    the app uses to count sign-ups per network.

Opening hours are in Irish time. CloudFront Functions have no time-zone database, so the function
works out summer time itself with the EU rule: from 01:00 UTC on the last Sunday of March until
01:00 UTC on the last Sunday of October.
"""

from __future__ import annotations

import json

CLOSED_PAGE = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Demo Bank assistant: closed</title>
<style>body{margin:0;min-height:100vh;background:#F6F8F8;color:#0F2A33;font:400 1.0625rem/1.55 system-ui,sans-serif}
header{padding:.75rem 1.5rem;background:#0F2A33;color:#F6F8F8;font-weight:700}main{max-width:34rem;margin:14vh auto;padding:0 1.25rem}
h1{font-size:2rem;line-height:1.2;margin:0 0 .75rem}p{color:#4A6169;margin:0 0 1rem}strong{color:#0F2A33}</style></head>
<body><header>Demo Bank</header><main><h1>We're closed right now</h1>
<p>The Demo Bank assistant is open every day from <strong>__OPEN__ to __CLOSE__</strong>, Irish time.</p>
<p>Come back then: your demo bank and conversations are kept for 24 hours.</p>
<p><a href="/privacy.html" style="color:#23645C">Privacy notice</a></p></main></body></html>"""

FUNCTION = """// Opening hours and the visitor's address (made by infra/stacks/opening_hours.py; edit OPEN_HOURS in .env)
var OPEN = __OPEN_MIN__, CLOSE = __CLOSE_MIN__;   // minutes after midnight, Irish time; -1 = always open
var PAGE = __PAGE__;
var JSON_BODY = __JSON__;
var PRIVACY = __PRIVACY__;   // the privacy notice, served from here so it's there even when closed

function lastSundayAt1UTC(year, month) {          // month: 0 = January
    var d = new Date(Date.UTC(year, month + 1, 0, 1, 0, 0));   // last day of the month, 01:00 UTC
    d.setUTCDate(d.getUTCDate() - d.getUTCDay());
    return d.getTime();
}

function irishMinutes(now) {
    var t = now.getTime(), y = now.getUTCFullYear();
    var summer = t >= lastSundayAt1UTC(y, 2) && t < lastSundayAt1UTC(y, 9);
    return (now.getUTCHours() * 60 + now.getUTCMinutes() + (summer ? 60 : 0)) % 1440;
}

function isOpen(now) {
    if (OPEN < 0) return true;
    var m = irishMinutes(now);
    return m >= OPEN && m < CLOSE;
}

function handler(event) {
    var request = event.request;
    request.headers['x-viewer-ip'] = { value: event.viewer.ip };
    if (request.uri === '/privacy' || request.uri === '/privacy.html') {
        return { statusCode: 200, statusDescription: 'OK',
                 headers: { 'content-type': { value: 'text/html; charset=utf-8' },
                            'cache-control': { value: 'max-age=3600' } },
                 body: PRIVACY };
    }
    if (isOpen(new Date())) return request;
    var api = request.uri.indexOf('/api/') === 0;
    return {
        statusCode: 503,
        statusDescription: 'Closed',
        headers: {
            'content-type': { value: api ? 'application/json' : 'text/html; charset=utf-8' },
            'cache-control': { value: 'no-store' },
            'retry-after': { value: '3600' }
        },
        body: api ? JSON_BODY : PAGE
    };
}
"""


def clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


MAX_BYTES = 10240   # CloudFront's limit for a function's code


def compact(html: str) -> str:
    """The page with the line breaks and indentation taken out, to fit in the function."""
    return " ".join(line.strip() for line in html.splitlines() if line.strip())


def function_code(hours: tuple[int, int] | None, privacy: str = "<p>Privacy notice</p>") -> str:
    opens, closes = hours if hours else (-1, -1)
    page = CLOSED_PAGE.replace("__OPEN__", clock(max(opens, 0))).replace("__CLOSE__", clock(max(closes, 0)))
    message = {"detail": f"The demo has closed for today. It's open every day from {clock(max(opens, 0))} "
                         f"to {clock(max(closes, 0))}, Irish time.", "closed": True}
    code = (FUNCTION.replace("__OPEN_MIN__", str(opens)).replace("__CLOSE_MIN__", str(closes))
            .replace("__PAGE__", json.dumps(page)).replace("__JSON__", json.dumps(json.dumps(message)))
            .replace("__PRIVACY__", json.dumps(compact(privacy))))
    if len(code.encode()) > MAX_BYTES:
        raise ValueError(f"The CloudFront Function is {len(code.encode())} bytes; CloudFront allows {MAX_BYTES}. "
                         "Shorten web/privacy.html.")
    return code
