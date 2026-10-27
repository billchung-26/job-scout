"""Fetcher for Apple's career site (jobs.apple.com). Apple has no Greenhouse/Lever/Ashby board.

UNDOCUMENTED API, reverse-engineered from the public site on 2026-10-27. It is the same JSON the
search page itself calls, no login involved, but Apple can change or close it without notice.
If it breaks, this fetcher raises, the company shows up under "errors" in the run log and in the
Telegram digest, and the other companies still run.

How it works:
  1. GET /api/v1/CSRFToken -> token in the X-Apple-CSRF-Token header (+ a session cookie).
  2. POST /api/v1/search, 20 jobs per page (US: ~4,450 jobs = ~223 pages, ~2 min polite pace).
     The list carries only a short `jobSummary`, and the search is a poor title filter (full-text
     over the whole posting: "product manager" matched 6 title hits in the first 200 results),
     so we pull the whole listing and filter by title ourselves.
  3. For jobs whose title passes conditions.yaml's include/exclude keywords only, GET
     /api/v1/jobDetails/PIPE-<positionId> for the full description (fit.py scores on it).
     Roughly 100-200 calls instead of 4,450.

companies.yaml:   - name: Apple / ats: apple / slug: "postLocation-USA"   (slug = Apple's location filter)

Ceilings: MAX_PAGES guards a runaway listing (raises rather than truncating). MAX_DETAILS caps the
description calls; past it, remaining jobs keep the short summary and score on title only.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
import http.cookiejar

import yaml

import fit

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _CTX = ssl.create_default_context()

BASE = "https://jobs.apple.com"
PAGE_DELAY = 0.25      # seconds between calls: ~2 min for a full US pull; be a polite client
MAX_PAGES = 600        # 12,000 jobs; Apple US listing is ~4,450 today
MAX_DETAILS = 400
TIMEOUT = 40
_HEADERS = {
    # Apple's WAF answers 406/403 to non-browser User-Agents on some paths (seen for jobs.apple.com
    # HTML + the Workday sites); a browser-like UA is needed. Remove if the plain UA ever works.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "Origin": BASE,
    "Referer": BASE + "/en-us/search",
}


def _opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                       urllib.request.HTTPSHandler(context=_CTX))


def _token(op):
    resp = op.open(urllib.request.Request(BASE + "/api/v1/CSRFToken", headers=_HEADERS), timeout=TIMEOUT)
    tok = resp.headers.get("X-Apple-CSRF-Token")
    if not tok:
        raise RuntimeError("apple: no CSRF token in response; Apple probably changed its job-search API")
    return tok


def _title_filter():
    """include/exclude title keywords from conditions.yaml, only to decide which jobs get a detail call.
    A superset is fine (extra calls), so this is deliberately looser than scout.matches()."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "conditions.yaml")) as f:
            cond = yaml.safe_load(f) or {}
    except OSError:
        return lambda title: True
    inc = [k.lower() for k in cond.get("include_title_keywords") or []]
    exc = [k.lower() for k in cond.get("exclude_title_keywords") or []]
    return lambda title: (not inc or any(k in title.lower() for k in inc)) and not any(k in title.lower() for k in exc)


def fetch(slug):
    op = _opener()
    tok = _token(op)

    def call(url, body=None):
        headers = dict(_HEADERS, **{"X-Apple-CSRF-Token": tok})
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
        return json.load(op.open(req, timeout=TIMEOUT))["res"]

    # --- 1. whole listing; the same position appears once per location, merge them ---
    by_pos, page, total = {}, 1, None
    while True:
        res = call(BASE + "/api/v1/search", {
            "query": "", "filters": {"locations": [slug]}, "page": page, "locale": "en-us", "sort": "",
            "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}})
        total = res.get("totalRecords", total)
        batch = res.get("searchResults") or []
        for j in batch:
            pos = str(j.get("positionId") or j.get("id"))
            cities = [l.get("name") or l.get("city") or "" for l in j.get("locations") or []]
            rec = by_pos.setdefault(pos, {
                "id": pos, "title": j.get("postingTitle", ""), "locs": [],
                "url": "%s/en-us/details/%s/%s" % (BASE, pos, j.get("transformedPostingTitle", "")),
                "desc": j.get("jobSummary", "")})
            for c in cities:
                if c and c not in rec["locs"]:
                    rec["locs"].append(c)
        if not batch or (total is not None and page * 20 >= total):
            break
        if page >= MAX_PAGES:
            raise RuntimeError("apple: still more jobs after %d pages; refusing to return a truncated list" % MAX_PAGES)
        page += 1
        time.sleep(PAGE_DELAY)

    # --- 2. full descriptions only where the title could pass the filter ---
    wanted, detailed = _title_filter(), 0
    for rec in by_pos.values():
        if detailed >= MAX_DETAILS or not wanted(rec["title"]):
            continue
        detailed += 1
        time.sleep(PAGE_DELAY)
        try:
            d = call("%s/api/v1/jobDetails/PIPE-%s?locale=en-us" % (BASE, rec["id"]))
        except (urllib.error.URLError, ValueError, KeyError):
            continue  # keep the short summary; one bad detail must not sink the whole company
        body = " ".join(fit.html_to_text(d.get(k) or "") for k in
                        ("jobSummary", "description", "minimumQualifications", "preferredQualifications"))
        rec["desc"] = body[:fit.MAX_DESC_CHARS]

    # "United States" is appended because the list gives bare city names ("Cupertino") and the
    # location filter in conditions.yaml keys on it; the slug restricts the search to the US already.
    return [{"id": r["id"], "title": r["title"],
             "location": "%s, United States" % "; ".join(r["locs"]) if r["locs"] else "United States",
             "url": r["url"], "desc": r["desc"]} for r in by_pos.values()]
