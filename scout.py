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

import html
import json
import os
import ssl
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
TIMEOUT = 20
PROMOTE_PER_RUN = 2

# Companies known to be unwatchable via these APIs (for the watchlist report).
NOT_WATCHABLE = [
    ("Own career site (not on these APIs)",
     ["Google", "Waymo (Alphabet)", "Zoox (Amazon Jobs)"]),
    ("Probed, no public Greenhouse/Lever/Ashby board",
     ["Ada", "AI21", "Adept", "Applied Intuition", "Augment", "Census",
      "Chef Robotics", "Clay", "Cognigy", "Contextual AI", "Crescendo",
      "dbt Labs", "EvenUp", "Forethought", "Hippocratic AI", "Luma AI",
      "Magic", "Rippling", "Rudderstack", "Sana", "Skild AI", "Snowplow",
      "Sourcegraph", "Windsurf/Codeium"]),
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
    loc = job["location"].lower()

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
            c = dict(cand)
            c["_total"] = len(jobs)
            c["_matches"] = sum(1 for j in jobs if matches(j, cond))
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
    print("Wrote watchlist.md (%d watched)." % len(stats))


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
    report, errors, stats = [], [], []

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
                      "ats": ats, "total": len(jobs), "matches": len(wanted)})
        time.sleep(0.3)

    save_state(new_state)
    write_digest(report, errors)

    secrets = load_secrets()
    notify_telegram(report, secrets)

    # grow the watchlist with related startups
    promoted = promote_from_queue(PROMOTE_PER_RUN, companies, cond)
    if promoted:
        notify_promotions(promoted, secrets)
        for c in promoted:
            stats.append({"name": c["name"], "source": "auto (new today)",
                          "ats": c["ats"], "total": c.get("_total", 0),
                          "matches": c.get("_matches", 0)})

    write_watchlist(stats)


if __name__ == "__main__":
    sys.exit(main())
