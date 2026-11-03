"""Fetch ONE job posting (with its structure intact) from the ATS a company is watched through.

The daily scan's fetchers flatten descriptions to one line for scoring; this module goes back to
the source for the selected jobs only, and returns HTML with real headings/bullets so jd_parse can
split requirements from responsibilities. Every fetcher returns the same shape:

    {"title", "location", "url", "html", "comp": [str], "posted": str|None}

and raises LookupError when the posting is gone (closed since it was recommended). Anything else
(network/API change) propagates, so a broken fetcher is loud instead of producing an empty résumé brief.

Per-ATS lookup is by the id inside the recommended URL, so it only works for URLs the scan produced.
Ceilings: Ashby and Jibe have no single-job endpoint, so their whole board is pulled once per run
and cached (Ashby ~1-3 MB, AMD/Jibe up to 7 pages); a batch over many companies is as slow as the
sum of those pulls.
"""
import datetime
import html as htmllib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import apple
import jibe
import scout

_cache = {}


def _money(cents, cur="USD"):
    return "{:,.0f} {}".format(cents / 100, cur)


# ---------------------------------------------------------------- Greenhouse

def _greenhouse(slug, url):
    m = re.search(r"gh_jid=(\d+)", url) or re.search(r"/jobs?/(\d+)", url) or re.search(r"[?&](\d{6,})", url)
    if not m:
        raise LookupError("no Greenhouse job id in %s" % url)
    try:
        j = scout._get("https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s?pay_transparency=true"
                       % (urllib.parse.quote(slug), m.group(1)))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError("Greenhouse no longer has job %s (probably closed)" % m.group(1))
        raise
    comp = []
    for r in j.get("pay_input_ranges") or []:
        comp.append("%s – %s%s" % (_money(r.get("min_cents", 0), r.get("currency_type", "USD")),
                                   _money(r.get("max_cents", 0), r.get("currency_type", "USD")),
                                   (" (%s)" % r["title"]) if r.get("title") else ""))
    return {"title": j.get("title", ""), "location": scout._greenhouse_location(j), "url": url,
            "html": j.get("content", ""), "comp": comp, "posted": (j.get("first_published") or j.get("updated_at") or "")[:10] or None}


# ---------------------------------------------------------------- Ashby

def _ashby(slug, url):
    key = ("ashby", slug)
    if key not in _cache:   # no single-job endpoint: one board pull per company per run
        _cache[key] = scout._get("https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true" % urllib.parse.quote(slug))
    ident = url.rstrip("/").split("/")[-1].split("?")[0]
    for j in _cache[key].get("jobs", []):
        if ident in (j.get("id"), (j.get("jobUrl") or "").rstrip("/").split("/")[-1]):
            comp = []
            c = j.get("compensation") or {}
            if c.get("compensationTierSummary"):
                comp.append(c["compensationTierSummary"])
            elif c.get("scrapeableCompensationSalarySummary"):
                comp.append(c["scrapeableCompensationSalarySummary"])
            return {"title": j.get("title", ""), "location": j.get("location") or "", "url": url,
                    "html": j.get("descriptionHtml") or htmllib.escape(j.get("descriptionPlain", "")),
                    "comp": comp, "posted": (j.get("publishedAt") or "")[:10] or None}
    raise LookupError("Ashby board %s no longer lists %s (probably closed)" % (slug, ident))


# ---------------------------------------------------------------- Lever

def _lever(slug, url):
    ident = url.rstrip("/").split("/")[-1].split("?")[0]
    try:
        j = scout._get("https://api.lever.co/v0/postings/%s/%s" % (urllib.parse.quote(slug), ident))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError("Lever no longer has posting %s (probably closed)" % ident)
        raise
    parts = [j.get("description") or ""]
    for lst in j.get("lists") or []:   # requirements/responsibilities live in `lists`, not in description
        parts.append("<h3>%s</h3><ul>%s</ul>" % (lst.get("text", ""), lst.get("content", "")))
    parts.append(j.get("additional") or "")
    sr = j.get("salaryRange") or {}
    comp = ["%s – %s %s" % (sr.get("min"), sr.get("max"), sr.get("currency", ""))] if sr.get("min") else []
    created = j.get("createdAt")
    return {"title": j.get("text", ""), "location": (j.get("categories") or {}).get("location", ""), "url": url,
            "html": "\n".join(parts), "comp": comp,
            "posted": datetime.datetime.fromtimestamp(created / 1000, datetime.timezone.utc).strftime("%Y-%m-%d") if created else None}


# ---------------------------------------------------------------- Jibe (AMD ...)

def _jibe(slug, url):
    host, _, extra = slug.partition("?")
    ident = url.rstrip("/").split("/")[-1].split("?")[0]
    key = ("jibe", slug)
    if key not in _cache:
        raw, page, params = [], 1, urllib.parse.parse_qsl(extra)
        while True:
            data = jibe._get_json("https://%s/api/jobs?%s" % (host, urllib.parse.urlencode([("page", page), ("limit", jibe.PAGE_SIZE)] + params)))
            batch = data.get("jobs") or []
            raw.extend(b.get("data") or {} for b in batch)
            if not batch or len(raw) >= (data.get("totalCount") or 0) or page >= jibe.MAX_PAGES:
                break
            page += 1
        _cache[key] = raw
    for d in _cache[key]:
        if ident in (str(d.get("req_id")), str(d.get("slug"))):
            body = "".join("<h3>%s</h3>%s" % (h, d.get(k) or "") for h, k in
                           (("Description", "description"), ("Responsibilities", "responsibilities"), ("Qualifications", "qualifications")))
            return {"title": d.get("title", ""), "location": ", ".join(p for p in (d.get("city"), d.get("state"), d.get("country")) if p),
                    "url": url, "html": body, "comp": [], "posted": (d.get("posted_date") or "")[:10] or None}
    raise LookupError("%s no longer lists %s (probably closed)" % (host, ident))


# ---------------------------------------------------------------- Apple

def _apple(slug, url):
    m = re.search(r"/details/(\d+)", url)
    if not m:
        raise LookupError("no Apple position id in %s" % url)
    op = apple._opener()
    tok = apple._token(op)
    req = urllib.request.Request("%s/api/v1/jobDetails/PIPE-%s?locale=en-us" % (apple.BASE, m.group(1)),
                                 headers=dict(apple._HEADERS, **{"X-Apple-CSRF-Token": tok}))
    try:
        d = json.load(op.open(req, timeout=apple.TIMEOUT))["res"]
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise LookupError("Apple no longer lists position %s (probably closed)" % m.group(1))
        raise
    def block(h, k):
        t = d.get(k) or ""
        return "<h3>%s</h3>%s" % (h, htmllib.escape(t).replace("\n", "<br>")) if t.strip() else ""
    body = (block("Summary", "jobSummary") + block("Description", "description")
            + block("Minimum Qualifications", "minimumQualifications") + block("Preferred Qualifications", "preferredQualifications"))
    locs = [l.get("name", "") for l in (d.get("locations") or []) if isinstance(l, dict)]
    return {"title": d.get("postingTitle", ""), "location": "; ".join(locs), "url": url, "html": body,
            "comp": [], "posted": d.get("postingDateMeta")}


_FETCHERS = {"greenhouse": _greenhouse, "ashby": _ashby, "lever": _lever, "jibe": _jibe, "apple": _apple}


def fetch_job(company_cfg, url):
    fn = _FETCHERS.get(company_cfg.get("ats"))
    if not fn:
        raise LookupError("no single-job fetcher for ats '%s' (%s)" % (company_cfg.get("ats"), company_cfg.get("name")))
    return fn(company_cfg["slug"], url)
