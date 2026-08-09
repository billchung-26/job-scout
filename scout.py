#!/usr/bin/env python3
"""
job_scout - watch AI companies' career boards, push matching roles to Telegram,
and gradually grow the watchlist with related startups.

Data source: the PUBLIC job-board APIs of Greenhouse / Lever / Ashby.
No auth, no scraping, no LinkedIn/Indeed dependency.

Run:
    python3 scout.py          # normal daily run
    python3 scout.py --test   # send a Telegram test message

Files:
    companies.yaml        - your curated watchlist (hand-edited; never touched by the script)
    companies_auto.yaml   - startups the script auto-added from the backlog
    discovery_queue.yaml  - vetted backlog; top 2 promoted each run
    conditions.yaml       - your editable filters + ranking keywords
    secrets.yaml          - Telegram bot_token + chat_id (git-ignored)
    state.json            - last-seen job ids (for "what's new" diffing)
    digests/YYYY-MM-DD.md - full ranked digest each run
    watchlist.md          - auto-generated in/out list
"""

import csv
import html
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

import yaml

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
DIGEST_DIR = os.path.join(HERE, "digests")
QUEUE_FILE = os.path.join(HERE, "discovery_queue.yaml")
AUTO_FILE = os.path.join(HERE, "companies_auto.yaml")
PROMOTE_STAMP = os.path.join(HERE, ".last_promote")
RECS_DIR = os.path.join(HERE, "recommendations")
REC_STATE_FILE = os.path.join(HERE, "rec_state.json")
APPLIED_FILE = os.path.join(HERE, "applied.yaml")
SUNSET_DAYS = 30
TIMEOUT = 20
PROMOTE_PER_RUN = 2

# Companies known to be unwatchable via these APIs (for the watchlist report).
NOT_WATCHABLE = [
    ("Own career site (not on these APIs)",
     ["Google", "Waymo (Alphabet)", "Nvidia (Workday)", "AMD (Workday)", "Uber (custom)"]),
    ("Migrated off / no longer served by the public API",
     ["RudderStack", "Snowplow", "Forethought", "dbt Labs", "Fireworks AI", "Aurora Innovation"]),
    ("No public Greenhouse/Lever/Ashby board found",
     ["AI21", "Adept", "Census", "Chef Robotics", "Cognigy", "Contextual AI",
      "Crescendo", "EvenUp", "Hippocratic AI", "Luma AI", "Magic", "Rippling",
      "Skild AI", "Windsurf/Codeium"]),
]


# ---------------------------------------------------------------- fetching

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-scout/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_greenhouse(slug):
    data = _get("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug)
    return [{
        "id": str(j.get("id")),
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
    } for j in data.get("jobs", [])]


def fetch_lever(slug):
    data = _get("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "id": str(j.get("id")),
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
        })
    return out


def fetch_ashby(slug):
    data = _get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    return [{
        "id": str(j.get("id")),
        "title": j.get("title", ""),
        "location": j.get("locationName") or j.get("location", "") or "",
        "url": j.get("jobUrl") or j.get("applyUrl", "") or "",
    } for j in data.get("jobs", [])]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def careers_url(ats, slug):
    return {
        "greenhouse": "https://job-boards.greenhouse.io/%s" % slug,
        "ashby": "https://jobs.ashbyhq.com/%s" % slug,
        "lever": "https://jobs.lever.co/%s" % slug,
    }.get(ats, "")


# ---------------------------------------------------------------- matching

def matches(job, cond):
    title = job["title"].lower()
    loc = (job.get("location") or "").lower()

    inc = [k.lower() for k in cond.get("include_title_keywords") or []]
    if inc and not any(k in title for k in inc):
        return False
    exc = [k.lower() for k in cond.get("exclude_title_keywords") or []]
    if exc and any(k in title for k in exc):
        return False
    locs = [k.lower() for k in cond.get("locations") or []]
    if locs and not any(k in loc for k in locs):
        return False
    return True


def fit_score(job, cond):
    title = job["title"].lower()
    pri = [k.lower() for k in cond.get("priority_keywords") or []]
    return sum(1 for k in pri if k in title)


# ---------------------------------------------------------------- config / state

def load_companies():
    """Merge curated + auto files, tag source, dedupe by (ats, slug)."""
    companies = []
    for fn, source in (("companies.yaml", "curated"), ("companies_auto.yaml", "auto")):
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            continue
        with open(p) as f:
            d = yaml.safe_load(f) or {}
        for c in (d.get("companies") or []):
            c = dict(c)
            c["_source"] = source
            companies.append(c)
    seen, uniq = set(), []
    for c in companies:
        k = (c.get("ats"), c.get("slug"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def load_conditions():
    with open(os.path.join(HERE, "conditions.yaml")) as f:
        return yaml.safe_load(f) or {}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------- notify (Telegram)

def load_secrets():
    p = os.path.join(HERE, "secrets.yaml")
    if os.path.exists(p):
        with open(p) as f:
            return yaml.safe_load(f) or {}
    return {}


def _tg_send(token, chat_id, text):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "job-scout/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def notify_telegram(report, secrets):
    """Push only truly-new roles (skip first-run baselines, which go to the file only)."""
    tg = (secrets or {}).get("telegram") or {}
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if not token or not chat_id:
        return

    blocks, total = [], 0
    for name, fresh, first_run in report:
        if first_run or not fresh:
            continue
        lines = ["<b>%s</b>" % html.escape(name)]
        for j in fresh:
            total += 1
            star = (" " + "⭐" * j["score"]) if j["score"] else ""
            lines.append('• <a href="%s">%s</a> — %s%s' % (
                html.escape(j["url"]), html.escape(j["title"]),
                html.escape(j["location"] or "n/a"), star))
        blocks.append("\n".join(lines))

    if total == 0:
        return

    header = "🔔 <b>Job Scout</b> — %s\n%d new matching role(s)" % (
        datetime.now().strftime("%Y-%m-%d"), total)
    messages, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 4000:
            messages.append(cur)
            cur = ""
        cur = (cur + "\n\n" + b) if cur else b
    if cur:
        messages.append(cur)

    for m in messages:
        try:
            _tg_send(token, chat_id, m)
        except Exception as e:  # noqa
            print("Telegram send failed: %s" % e)
    print("Sent %d Telegram message(s), %d new role(s)." % (len(messages), total))


def notify_promotions(promoted, secrets):
    tg = (secrets or {}).get("telegram") or {}
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    plural = "" if len(promoted) == 1 else "s"
    lines = ["🆕 <b>Added to your watchlist</b> (%d related startup%s):" % (len(promoted), plural)]
    for c in promoted:
        url = careers_url(c["ats"], c["slug"])
        lines.append('• <a href="%s">%s</a> — %s\n   %d open · %d match your filters' % (
            html.escape(url), html.escape(c["name"]), html.escape(c.get("related", "")),
            c.get("_total", 0), c.get("_matches", 0)))
    text = "\n".join(lines)
    if token and chat_id:
        try:
            _tg_send(token, chat_id, text)
        except Exception as e:  # noqa
            print("Telegram promo send failed: %s" % e)


# ---------------------------------------------------------------- discovery growth

def _promoted_today():
    if os.path.exists(PROMOTE_STAMP):
        with open(PROMOTE_STAMP) as f:
            return f.read().strip() == datetime.now().strftime("%Y-%m-%d")
    return False


def _mark_promoted_today():
    with open(PROMOTE_STAMP, "w") as f:
        f.write(datetime.now().strftime("%Y-%m-%d"))


def promote_from_queue(n, existing, cond):
    """Promote up to n backlog companies into companies_auto.yaml. Verify each still
    returns jobs; skip (retry later) if it doesn't; drop ones we already watch.
    Runs at most once per calendar day, so extra manual runs don't drain the backlog."""
    if _promoted_today():
        return []
    if not os.path.exists(QUEUE_FILE):
        return []
    with open(QUEUE_FILE) as f:
        queue = (yaml.safe_load(f) or {}).get("queue") or []

    existing_keys = set((c.get("ats"), c.get("slug")) for c in existing)
    promoted, rest = [], []
    for cand in queue:
        key = (cand.get("ats"), cand.get("slug"))
        if len(promoted) >= n:
            rest.append(cand)
            continue
        if key in existing_keys:
            continue  # already watching -> drop from queue
        fetch = FETCHERS.get(cand.get("ats"))
        jobs = None
        if fetch:
            try:
                jobs = fetch(cand["slug"])
            except Exception:  # noqa
                jobs = None
        if jobs:
            wanted = [j for j in jobs if matches(j, cond)]
            for j in wanted:
                j["score"] = fit_score(j, cond)
            wanted.sort(key=lambda j: (-j["score"], j["title"].lower()))
            c = dict(cand)
            c["_total"] = len(jobs)
            c["_matches"] = len(wanted)
            c["_wanted"] = wanted
            c["_all_ids"] = [j["id"] for j in jobs]
            promoted.append(c)
        else:
            rest.append(cand)  # keep for a later retry

    with open(QUEUE_FILE, "w") as f:
        f.write("# Discovery backlog. scout.py promotes the top %d each run.\n" % PROMOTE_PER_RUN)
        yaml.safe_dump({"queue": rest}, f, sort_keys=False, allow_unicode=True)

    if promoted:
        auto = []
        if os.path.exists(AUTO_FILE):
            with open(AUTO_FILE) as f:
                auto = (yaml.safe_load(f) or {}).get("companies") or []
        for c in promoted:
            auto.append({"name": c["name"], "ats": c["ats"], "slug": c["slug"]})
        with open(AUTO_FILE, "w") as f:
            f.write("# Auto-added daily by scout.py from discovery_queue.yaml.\n")
            f.write("# Safe to edit/remove; move favorites into companies.yaml if you like.\n")
            yaml.safe_dump({"companies": auto}, f, sort_keys=False, allow_unicode=True)
        print("Promoted: " + ", ".join(c["name"] for c in promoted))
    _mark_promoted_today()
    return promoted


# ---------------------------------------------------------------- outputs

def write_digest(report, errors):
    os.makedirs(DIGEST_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DIGEST_DIR, "%s.md" % today)

    total_new = sum(len(f) for _, f, _ in report)
    lines = ["# Job Scout digest — %s" % today, "",
             "**%d matching new role(s)** across %d companies." % (total_new, len(report)), ""]
    for name, fresh, first_run in report:
        tag = " _(baseline — first run, showing all current matches)_" if first_run else ""
        lines.append("## %s%s" % (name, tag))
        if not fresh:
            lines.append("_No new matching roles._\n")
            continue
        for j in fresh:
            star = " ⭐" * j["score"]
            lines.append("- **[%s](%s)** — %s%s" % (j["title"], j["url"], j["location"] or "n/a", star))
        lines.append("")
    if errors:
        lines.append("---\n### ⚠️ Issues")
        for e in errors:
            lines.append("- %s" % e)
        lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))
    print("Wrote %s (%d new matches)" % (path, total_new))


def write_watchlist(stats):
    lines = ["# Job Scout — Watchlist", "",
             "_Auto-generated %s. Watched via the public Greenhouse / Lever / Ashby APIs._"
             % datetime.now().strftime("%Y-%m-%d %H:%M"), "",
             "## ✅ Watching (%d companies)" % len(stats), "",
             "| Company | Source | Platform | Matches / Total |",
             "|---|---|---|---|"]
    for s in sorted(stats, key=lambda x: x["name"].lower()):
        lines.append("| %s | %s | %s | %d / %d |" %
                     (s["name"], s["source"], s["ats"], s["matches"], s["total"]))
    lines.append("")

    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE) as f:
            q = (yaml.safe_load(f) or {}).get("queue") or []
        lines.append("## ⏳ In discovery backlog — %d left (~%d added/day)" % (len(q), PROMOTE_PER_RUN))
        lines.append("")
        for c in q:
            lines.append("- %s — %s" % (c["name"], c.get("related", "")))
        lines.append("")

    lines.append("## ❌ Not watchable by this tool")
    lines.append("")
    for grp, names in NOT_WATCHABLE:
        lines.append("**%s:** %s\n" % (grp, ", ".join(names)))

    with open(os.path.join(HERE, "watchlist.md"), "w") as f:
        f.write("\n".join(lines))
    _write_companies_xlsx(stats)
    print("Wrote watchlist.md + watchlist.xlsx (%d watched)." % len(stats))


def _write_companies_xlsx(stats):
    """Company list as a multi-sheet Excel workbook (Watching / Backlog / Not Watchable)."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return

    def style_sheet(ws, widths):
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9D9D9")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    wb = Workbook()
    wb.properties.created = wb.properties.modified = datetime(2024, 1, 1)  # deterministic file

    # --- Sheet 1: Watching ---
    ws = wb.active
    ws.title = "Watching"
    ws.append(["Company", "Source", "Platform", "Matching", "Total", "Careers URL"])
    for s in sorted(stats, key=lambda x: x["name"].lower()):
        url = careers_url(s.get("ats"), s.get("slug", ""))
        ws.append([s["name"], s["source"], s["ats"], s["matches"], s["total"], url])
        if url:
            c = ws.cell(row=ws.max_row, column=6)
            c.hyperlink = url
            c.font = Font(color="0563C1", underline="single")
    style_sheet(ws, [22, 16, 12, 10, 8, 55])

    # --- Sheet 2: Backlog ---
    ws2 = wb.create_sheet("Backlog")
    ws2.append(["Company", "Platform", "Slug", "Related to"])
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE) as f:
            for c in (yaml.safe_load(f) or {}).get("queue") or []:
                ws2.append([c.get("name"), c.get("ats"), c.get("slug"), c.get("related", "")])
    style_sheet(ws2, [22, 12, 20, 44])

    # --- Sheet 3: Not Watchable ---
    ws3 = wb.create_sheet("Not Watchable")
    ws3.append(["Reason", "Company"])
    for grp, names in NOT_WATCHABLE:
        for n in names:
            ws3.append([grp, n])
    style_sheet(ws3, [44, 24])

    wb.save(os.path.join(HERE, "watchlist.xlsx"))


# ---------------------------------------------------------------- recommendations

def load_applied():
    """Return a set of applied job URLs (hidden from recommendations)."""
    if not os.path.exists(APPLIED_FILE):
        return set()
    with open(APPLIED_FILE) as f:
        d = yaml.safe_load(f) or {}
    keys = set()
    for item in (d.get("applied") or []):
        if isinstance(item, dict) and item.get("url"):
            keys.add(item["url"].strip())
        elif isinstance(item, str):
            keys.add(item.strip())
    return keys


def _job_key(name, j):
    return (j.get("url") or "").strip() or ("%s|%s" % (name, j.get("title", "")))


def update_recommendations(all_matching):
    """Refresh recommendations (latest.md / .csv / .xlsx): track first-seen per role,
    flag roles you've applied to (applied.yaml) in an 'Applied' column, and sunset any
    NON-applied role recommended for more than SUNSET_DAYS."""
    os.makedirs(RECS_DIR, exist_ok=True)
    today = datetime.now().date()
    today_s = today.strftime("%Y-%m-%d")

    prior = {}
    if os.path.exists(REC_STATE_FILE):
        with open(REC_STATE_FILE) as f:
            prior = json.load(f)
    applied = load_applied()

    new_state, records = {}, []
    n_applied = n_sunset = 0
    for name, j in all_matching:
        key = _job_key(name, j)
        first_seen = prior.get(key, today_s)      # carry forward, or first seen today
        new_state[key] = first_seen               # only current-open roles are kept
        try:
            age = (today - datetime.strptime(first_seen, "%Y-%m-%d").date()).days
        except Exception:  # noqa
            age = 0
        is_applied = key in applied or (j.get("url", "").strip() in applied)
        if is_applied:
            n_applied += 1
        elif age > SUNSET_DAYS:                    # sunset only un-applied stale roles
            n_sunset += 1
            continue
        records.append({
            "score": j.get("score", 0), "company": name, "role": j.get("title", ""),
            "location": j.get("location", "") or "n/a", "url": j.get("url", ""),
            "first_seen": first_seen, "age": age, "applied": is_applied,
        })

    with open(REC_STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=2)

    # not-applied first, then by fit desc, then freshest
    records.sort(key=lambda r: (r["applied"], -r["score"], r["age"], r["company"].lower()))

    _write_recs_md(records, n_applied, n_sunset)
    _write_recs_csv(records)
    _write_recs_xlsx(records)
    print("Updated recommendations: latest.md / .csv / .xlsx  (%d roles, %d applied, %d sunset hidden)"
          % (len(records), n_applied, n_sunset))
    return records


def _write_recs_md(records, n_applied, n_sunset):
    lines = ["# Job recommendations (live)", "",
             "_Updated %s. %d roles · %d applied · %d sunset (>%dd) hidden._" %
             (datetime.now().strftime("%Y-%m-%d %H:%M"), len(records), n_applied, n_sunset, SUNSET_DAYS),
             "", "| Fit | Company | Role | Location | Applied | First seen | Age |",
             "|---|---|---|---|---|---|---|"]
    for r in records:
        newtag = " 🆕" if r["age"] == 0 and not r["applied"] else ""
        stars = "⭐" * r["score"]
        lines.append("| %s | %s | [%s](%s)%s | %s | %s | %s | %dd |" %
                     (stars or "—", r["company"], r["role"], r["url"], newtag,
                      r["location"], "✅" if r["applied"] else "", r["first_seen"], r["age"]))
    with open(os.path.join(RECS_DIR, "latest.md"), "w") as f:
        f.write("\n".join(lines))


def _write_recs_csv(records):
    with open(os.path.join(RECS_DIR, "latest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Fit", "Company", "Role", "Location", "Applied", "First Seen", "Age (days)", "URL"])
        for r in records:
            w.writerow([r["score"], r["company"], r["role"], r["location"],
                        "Yes" if r["applied"] else "No", r["first_seen"], r["age"], r["url"]])


def _write_recs_xlsx(records):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return  # openpyxl not installed -> csv/md still written
    wb = Workbook()
    wb.properties.created = wb.properties.modified = datetime(2024, 1, 1)  # deterministic file
    ws = wb.active
    ws.title = "Recommendations"
    headers = ["Fit", "Company", "Role", "Location", "Applied", "First Seen", "Age (days)", "URL"]
    ws.append(headers)
    for r in records:
        ws.append([r["score"], r["company"], r["role"], r["location"],
                   "Yes" if r["applied"] else "No", r["first_seen"], r["age"], r["url"]])
        row = ws.max_row
        if r["url"]:                                   # make Role a clickable link
            c = ws.cell(row=row, column=3)
            c.hyperlink = r["url"]
            c.font = Font(color="0563C1", underline="single")
        if r["applied"]:                               # shade applied rows green
            for col in range(1, len(headers) + 1):
                ws.cell(row=row, column=col).fill = PatternFill("solid", fgColor="E2EFDA")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9D9D9")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, w in enumerate([6, 16, 52, 30, 9, 12, 10, 55], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    wb.save(os.path.join(RECS_DIR, "latest.xlsx"))


# ---------------------------------------------------------------- git auto-push

def git_autopush():
    """Commit and push updated lists to GitHub. Uses the repo's deploy key
    (core.sshCommand), so it works unattended from the launchd job."""
    try:
        subprocess.run(["git", "-C", HERE, "add", "-A"], check=True, capture_output=True)
        if subprocess.run(["git", "-C", HERE, "diff", "--cached", "--quiet"]).returncode == 0:
            print("git: no changes to push.")
            return
        msg = "Auto-update lists — %s" % datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "-C", HERE, "commit", "-q", "-m", msg], check=True, capture_output=True)
        p = subprocess.run(["git", "-C", HERE, "push", "origin", "main"], capture_output=True, text=True)
        print("git: pushed update." if p.returncode == 0
              else "git push failed: %s" % ((p.stderr or p.stdout).strip()[:200]))
    except Exception as e:  # noqa
        print("git autopush error: %s" % e)


# ---------------------------------------------------------------- main

def main():
    if "--test" in sys.argv:
        secrets = load_secrets()
        tg = (secrets or {}).get("telegram") or {}
        if not tg.get("bot_token") or not tg.get("chat_id"):
            print("secrets.yaml is missing bot_token or chat_id — fill it in first.")
            return 1
        _tg_send(tg["bot_token"], tg["chat_id"],
                 "✅ <b>Job Scout</b> is connected. You'll get new-role alerts here.")
        print("Test message sent — check Telegram.")
        return 0

    companies = load_companies()
    cond = load_conditions()
    state = load_state()
    new_state = {}
    report, errors, stats, all_matching = [], [], [], []

    for c in companies:
        name, ats, slug = c.get("name"), c.get("ats"), c.get("slug")
        fetch = FETCHERS.get(ats)
        if not fetch:
            errors.append("%s: unknown ats '%s'" % (name, ats))
            continue
        try:
            jobs = fetch(slug)
        except urllib.error.HTTPError as e:
            errors.append("%s: HTTP %s (check the slug '%s')" % (name, e.code, slug))
            continue
        except Exception as e:  # noqa
            errors.append("%s: %s" % (name, e))
            continue

        seen = set(state.get(name, []))
        new_state[name] = [j["id"] for j in jobs]

        wanted = [j for j in jobs if matches(j, cond)]
        for j in wanted:
            j["score"] = fit_score(j, cond)
        wanted.sort(key=lambda j: (-j["score"], j["title"].lower()))

        first_run = name not in state
        fresh = wanted if first_run else [j for j in wanted if j["id"] not in seen]
        report.append((name, fresh, first_run))
        stats.append({"name": name, "source": c.get("_source", "curated"),
                      "ats": ats, "slug": slug, "total": len(jobs), "matches": len(wanted)})
        all_matching.extend((name, j) for j in wanted)
        time.sleep(0.3)

    secrets = load_secrets()

    # grow the watchlist with related startups — do this BEFORE saving state/digest/
    # notifying, so a newly promoted company's current openings are scanned and
    # surfaced the same day it's added, not just starting the next run.
    promoted = promote_from_queue(PROMOTE_PER_RUN, companies, cond)
    if promoted:
        notify_promotions(promoted, secrets)
        for c in promoted:
            stats.append({"name": c["name"], "source": "auto (new today)",
                          "ats": c["ats"], "slug": c["slug"], "total": c.get("_total", 0),
                          "matches": c.get("_matches", 0)})
            wanted = c.get("_wanted") or []
            all_matching.extend((c["name"], j) for j in wanted)
            report.append((c["name"], wanted, True))
            new_state[c["name"]] = c.get("_all_ids") or []

    save_state(new_state)
    write_digest(report, errors)
    notify_telegram(report, secrets)

    write_watchlist(stats)
    update_recommendations(all_matching)
    git_autopush()


if __name__ == "__main__":
    sys.exit(main())
