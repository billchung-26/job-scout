"""Fetcher for Uber's career site (jobs.uber.com). Uber is not on Greenhouse/Lever/Ashby.

UNDOCUMENTED API, found 2026-11-07 by watching the browser's network calls on jobs.uber.com. The same JSON the
site's own pagination uses, no login. Uber can change or close it without notice (it already replaced the
older www.uber.com/api/loadSearchJobsResults, which now answers 404 "Missing RPC handler"); if that happens this
raises, and the company shows up as FETCH ERROR in the run log while the other companies still run.

    GET https://jobs.uber.com/api/jobs/search/?page=N&pagesize=100
      -> {"jobs":[{"Id","Title","Description"(HTML),"Locations":[{City,Region,Country}],"Urls":[{Url,IsDefault}],
                   "Teams","AdditionalText",...}], "totalJobs":828, "totalPages":9, "page":N, "pageSize":100}

pagesize=100 works (10 is the default; ~83 pages). Descriptions are inline, so no second call per job.
Ceiling: MAX_PAGES (50 = 5,000 jobs; Uber has ~830) raises instead of silently truncating.
The `slug` in companies.yaml is unused (kept for the schema).
"""
import json
import ssl
import time
import urllib.error
import urllib.request

import fit

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _CTX = ssl.create_default_context()

BASE = "https://jobs.uber.com"
PAGE_SIZE = 100
MAX_PAGES = 50
PAGE_DELAY = 0.3
RETRIES = 3
TIMEOUT = 60
_HEADERS = {
    # Uber's WAF answers 406 to non-browser requests (seen on www.uber.com and on Accept: */*-only calls).
    # Remove the browser-like UA/Accept if the plain client ever works.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/en/jobs/",
}


def _get_json(url):
    for attempt in range(1, RETRIES + 1):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=TIMEOUT, context=_CTX))
        except urllib.error.HTTPError as e:
            if (e.code < 500 and e.code != 429) or attempt == RETRIES:
                raise
        except (urllib.error.URLError, OSError):   # timeouts / resets: retry, then raise
            if attempt == RETRIES:
                raise
        time.sleep(3 * attempt)


def normalize(j):
    parts = []
    for loc in j.get("Locations") or []:
        p = ", ".join(x for x in (loc.get("City"), loc.get("Region"), loc.get("Country")) if x)
        if p and p not in parts:
            parts.append(p)
    urls = j.get("Urls") or []
    path = next((u["Url"] for u in urls if u.get("IsDefault")), urls[0]["Url"] if urls else "/en/jobs/%s/" % j["Id"])
    body = " ".join(fit.html_to_text(j.get(k) or "") for k in ("Description", "AdditionalText"))
    return {"id": str(j["Id"]), "title": (j.get("Title") or "").strip(), "location": "; ".join(parts),
            "url": BASE + path, "desc": body[:fit.MAX_DESC_CHARS]}


def fetch(slug=None):
    jobs, seen, page, total_pages = [], set(), 1, None
    while True:
        data = _get_json("%s/api/jobs/search/?page=%d&pagesize=%d" % (BASE, page, PAGE_SIZE))
        total_pages = data.get("totalPages", total_pages)
        for j in data.get("jobs") or []:
            if j.get("Id") and j["Id"] not in seen:
                seen.add(j["Id"])
                jobs.append(normalize(j))
        if not data.get("jobs") or total_pages is None or page >= total_pages:
            break
        if page >= MAX_PAGES:
            raise RuntimeError("uber: %s pages reported, refusing to return a truncated list after %d" % (total_pages, MAX_PAGES))
        page += 1
        time.sleep(PAGE_DELAY)
    if not jobs:
        raise RuntimeError("uber: the search API returned no jobs; Uber probably changed its site")
    total = data.get("totalJobs")
    if total and len(jobs) < 0.95 * total:   # guards a silent partial result (e.g. a page that came back empty)
        raise RuntimeError("uber: got %d of %d reported jobs" % (len(jobs), total))
    return jobs
