#!/usr/bin/env python3
"""Google Careers source for job-scout, fed by the daily "New job(s) match your search on Google
Careers" alert e-mail (careers-noreply@google.com).

WHY AN E-MAIL: google.com/about/careers has no public job-board API like Greenhouse/Lever/Ashby, and
its search page is rendered client-side. The alert e-mail is the one stable, sanctioned feed Bill
already receives, so we read that instead of scraping.

TWO HALVES (they run in different places on purpose):
  1. INGEST  (local, needs Gmail)  - `python3 google_email.py alert.html [--date YYYY-MM-DD]`
     parses one alert e-mail's HTML and merges its jobs into google_jobs.yaml (committed to git).
     GitHub Actions cannot read Gmail, so a person/agent with Gmail access runs this part and pushes.
  2. FETCH   (daily run, no Gmail) - fetch(slug) is registered in scout.FETCHERS as ats "google";
     it only reads google_jobs.yaml, so the daily GitHub Action needs no new secret.

WHAT THE E-MAIL DOES NOT GIVE US (known gaps, not bugs):
  - Only the ~20 newest jobs of the saved search ("Product Manager", US, early+mid level at the time
    of writing); a role that is older than the e-mail's window never shows up unless it was ingested
    earlier. Seeding therefore starts from the first e-mail ingested, not from all of Google.
  - No full job description: only the 2-5 "minimum qualification" bullets. fit.py scores mostly on the
    title for these roles, and the Keywords column is thinner than for other companies.
  - Nothing says when a job closes. Entries age out of google_jobs.yaml after RETAIN_DAYS (below);
    a role closed earlier than that stays listed until then.
  - Location is "Multiple Sites" for many jobs. The saved search is restricted to location=United
    States, so we write "Multiple Sites, United States" (derivable from the alert's own link).
  - The posted age is relative ("4 days ago"), so first_seen is "e-mail date minus age": +/- 1 day.

If Google changes the e-mail layout the parser finds 0 jobs from a non-empty e-mail and raises, so
the failure is loud instead of silently emptying the list.
"""
import html
import os
import re
import sys
from datetime import date, datetime, timedelta

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "google_jobs.yaml")

# Delete path for the store (every create needs a delete): entries whose first_seen is older than
# this are dropped on each ingest. 45 > scout.SUNSET_DAYS (30) so the recommender's own sunset always
# fires first and the two never fight; the store never grows past ~45 days of alerts.
RETAIN_DAYS = 45

COMPANY = "Google"
LOCATION_SUFFIX = "United States"   # the alert's search is location=United States (see module doc)

# One job block in the e-mail:  <h5><a href=URL><u>TITLE</u></a></h5>
#   <span>ORG &ndash; LOCATION<br>AGE</span><span><ul><li>..</li></ul></span>
_JOB = re.compile(
    r'<h5[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>\s*<u>(.*?)</u>\s*</a>\s*</h5>\s*'
    r'<span[^>]*>(.*?)<br>\s*(.*?)</span>\s*'
    r'<span[^>]*>\s*<ul>(.*?)</ul>', re.S)
_ID = re.compile(r'/jobs/results/(\d+)-([^?]*)')


def _text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _age_days(s):
    """'Yesterday' / 'Today' / '4 days ago' / '1 week ago' / '3 hours ago' -> int days, or None."""
    s = s.strip().lower()
    if s in ("today", "just now"):
        return 0
    if s == "yesterday":
        return 1
    m = re.match(r"(\d+)\s*(hour|day|week|month)s?\s+ago", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return {"hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]


def parse(email_html, email_date):
    """-> list of job dicts (see fetch()). Raises if a non-empty e-mail yields nothing."""
    jobs = []
    for href, title, orgloc, age, ul in _JOB.findall(email_html):
        m = _ID.search(href.replace("&amp;", "&"))
        if not m:
            continue
        jid, slug = m.group(1), m.group(2)
        org, _, loc = _text(orgloc).partition("–")
        org, loc = org.strip(), loc.strip()
        days = _age_days(_text(age))
        if days is None:
            print("google_email: unreadable age %r for %s - assuming it is new today" % (_text(age), jid))
            days = 0
        bullets = [_text(li) for li in re.findall(r"<li>(.*?)</li>", ul, re.S)]
        jobs.append({
            "id": jid,
            # Query string dropped: it carries utm tracking + the saved-search terms, not the job.
            "url": "https://www.google.com/about/careers/applications/jobs/results/%s-%s" % (jid, slug),
            "title": _text(title),
            "org": org or COMPANY,
            "location": ("%s, %s" % (loc, LOCATION_SUFFIX)) if loc else LOCATION_SUFFIX,
            "first_seen": (email_date - timedelta(days=days)).isoformat(),
            "last_seen": email_date.isoformat(),
            "desc": " ".join(bullets),
        })
    if not jobs and "careers" in email_html.lower() and "<h5" in email_html:
        raise ValueError("Google alert e-mail has job-like markup but 0 jobs parsed: the layout changed. "
                         "Update _JOB in google_email.py; google_jobs.yaml was NOT modified.")
    return jobs


def load_store():
    if not os.path.exists(STORE):
        return {}
    with open(STORE) as f:
        d = yaml.safe_load(f) or {}
    return {str(j["id"]): j for j in (d.get("jobs") or [])}


def merge(new_jobs, today):
    """Merge into the store: keep the EARLIEST first_seen, newest last_seen; prune stale entries.
    Returns (store, n_added)."""
    store = load_store()
    added = 0
    for j in new_jobs:
        old = store.get(j["id"])
        if old:
            j["first_seen"] = min(old["first_seen"], j["first_seen"])
            j["last_seen"] = max(old["last_seen"], j["last_seen"])
        else:
            added += 1
        store[j["id"]] = j
    cutoff = (today - timedelta(days=RETAIN_DAYS)).isoformat()
    store = {k: v for k, v in store.items() if v["first_seen"] >= cutoff}
    return store, added


def save_store(store):
    jobs = sorted(store.values(), key=lambda j: (j["first_seen"], j["id"]), reverse=True)
    with open(STORE, "w") as f:
        f.write("# Generated by google_email.py from the Google Careers alert e-mail. Do not hand-edit:\n"
                "# the next ingest rewrites it. Entries are pruned %d days after first_seen.\n" % RETAIN_DAYS)
        yaml.safe_dump({"jobs": jobs}, f, sort_keys=False, allow_unicode=True, width=1000)


def fetch(slug):
    """scout.FETCHERS entry (ats: google). slug is ignored (kept so companies.yaml has the usual shape).
    Returns the shape the other fetchers return, plus first_seen, which update_recommendations uses for first-time roles."""
    import fit  # local import: fit imports nothing from here, but keep the module light for the CLI
    out = []
    for j in load_store().values():
        out.append({
            "id": j["id"], "title": j["title"], "location": j["location"], "url": j["url"],
            "desc": j.get("desc", "")[:fit.MAX_DESC_CHARS],
            "first_seen": j["first_seen"],
        })
    return out


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    path = argv[0]
    day = date.today()
    if "--date" in argv:
        day = datetime.strptime(argv[argv.index("--date") + 1], "%Y-%m-%d").date()
    with open(path, encoding="utf-8") as f:
        jobs = parse(f.read(), day)
    store, added = merge(jobs, day)
    save_store(store)
    print("google_email: %d jobs in e-mail, %d new, %d in store" % (len(jobs), added, len(store)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
