"""Fetcher for iCIMS "Jibe" career sites — careers.amd.com, and any other company on the platform.

Jibe sites expose a public JSON API at https://<host>/api/jobs (no auth, and the full job
description is included, which fit.py scores on). In companies.yaml the `slug` is the hostname,
optionally followed by "?" and extra API query params:

    - name: AMD
      ats: jibe
      slug: "careers.amd.com?country=United States"

Extra params go straight to the API. They are config, not code, on purpose: the API honours
`country` server-side (AMD: 663 of 1,304 jobs; a full pull is ~42 MB / 30 s, US-only is about half),
and that coupling to your location filter should be visible in companies.yaml rather than buried here.
Drop the "?country=..." part if you ever want non-US roles.

Verified against careers.amd.com on 2026-10-08. Other Jibe tenants are assumed to behave the same
(same platform, same API) but only AMD has actually been tested.
"""
import json
import ssl
import urllib.parse
import urllib.request

import fit

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _CTX = ssl.create_default_context()

PAGE_SIZE = 100   # the API answers HTTP 422 for limit > 100 (checked: 200 -> 422)
MAX_PAGES = 100   # runaway guard (10,000 jobs). Exceeding it RAISES instead of silently truncating
TIMEOUT = 60


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-scout/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize(host, d):
    # "City, State, Country" with consecutive repeats dropped ("Singapore, Singapore, Singapore").
    # Including the country matters: the location filter keys on "united states".
    parts = []
    for p in (d.get("city"), d.get("state"), d.get("country")):
        p = (p or "").strip()
        if p and (not parts or p.lower() != parts[-1].lower()):
            parts.append(p)
    body = " ".join(fit.html_to_text(d.get(k) or "") for k in ("description", "responsibilities", "qualifications"))
    meta = d.get("meta_data") or {}
    ident = str(d.get("req_id") or d.get("slug") or "")
    return {
        "id": ident,
        "title": d.get("title", ""),
        "location": ", ".join(parts) or d.get("full_location") or "",
        "url": meta.get("canonical_url") or "https://%s/careers-home/jobs/%s" % (host, ident),
        "desc": body[:fit.MAX_DESC_CHARS],
    }


def fetch(slug):
    host, _, extra = slug.partition("?")
    extra_params = urllib.parse.parse_qsl(extra)
    jobs, page = [], 1
    while True:
        qs = urllib.parse.urlencode([("page", page), ("limit", PAGE_SIZE)] + extra_params)
        data = _get_json("https://%s/api/jobs?%s" % (host, qs))
        batch = data.get("jobs") or []
        jobs.extend(_normalize(host, b.get("data") or {}) for b in batch)
        total = data.get("totalCount")
        if not batch or (total is not None and len(jobs) >= total):
            return jobs
        if page >= MAX_PAGES:
            raise RuntimeError("jibe %s: more jobs after %d pages; refusing to return a truncated list" % (host, MAX_PAGES))
        page += 1
