#!/usr/bin/env python3
"""Find new companies to watch from VC / accelerator lists, and stage them into discovery_queue.yaml.

    python3 sourcing.py --source yc   --add 20            # YC companies that are hiring
    python3 sourcing.py --source a16z --add 20            # a16z portfolio (Venture/Growth stage)
    python3 sourcing.py --source yc --dry-run             # show what would be added, write nothing

Pipeline per candidate: list -> drop what we already track -> find its public job board (Ashby, Greenhouse,
Lever; slug guessed from name + website domain) -> verify the board really is that company -> count roles
that pass your title filter -> rank -> append the top N to the queue (scout.py promotes 2/day from there).

SOURCES (both undocumented/third-party, either can change; a failure raises with a plain message):
  yc    https://yc-oss.github.io/api/companies/hiring.json   open mirror of YC's directory (community project,
        not YC itself). Fields used: name, website, team_size, batch, stage, top_company, status, isHiring.
  a16z  https://a16z.com/portfolio/   the page embeds the whole portfolio as JSON (title, web, stages,
        year_founded); we keep stage Venture/Growth, drop exits (M&A/IPO/SPAC/DPO) and seed.
Not built yet, same method would work: Sequoia and LSVP (jobs boards run on "Consider"), General Catalyst and
Accel (on "Getro").

Honest limits: (1) most startups have NO public ATS board we can read (custom careers page, Workable,
Rippling...), so expect roughly 15-30% of candidates to resolve; (2) the identity check is heuristic (Greenhouse
publishes the board's company name; for Ashby/Lever we require the company name in the job text); (3) slug
guessing misses unusual slugs; (4) YC's list is of companies, not of how good their PM roles are.
"""
import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import certifi
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "sourcing_cache.json")
QUEUE = os.path.join(HERE, "discovery_queue.yaml")
# Retention: a negative result ("no board found") is cached so repeat runs don't re-probe thousands of URLs,
# but boards appear later, so every entry expires. Hard cap keeps the file small (git-ignored, local only).
CACHE_TTL_DAYS = 45
CACHE_MAX = 5000
_CTX = ssl.create_default_context(cafile=certifi.where())
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
       "Accept": "application/json,text/html,*/*;q=0.8", "Accept-Language": "en-US"}


def _get(url, timeout=25, limit=6_000_000):
    r = urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout, context=_CTX)
    return r.read(limit)


def _json(url):
    try:
        return json.loads(_get(url))
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ---------------------------------------------------------------- sources

def source_yc(min_team, max_team):
    data = json.loads(_get("https://yc-oss.github.io/api/companies/hiring.json", 60))
    out = []
    for c in data:
        if c.get("status") != "Active" or not c.get("isHiring") or c.get("nonprofit"):
            continue
        n = c.get("team_size") or 0
        if not (min_team <= n <= max_team):
            continue
        out.append({"name": c["name"], "website": c.get("website") or "", "source": "YC %s" % (c.get("batch") or "?"),
                    "size": n, "note": (c.get("one_liner") or "")[:90], "rank": (1 if c.get("top_company") else 0, n)})
    return out


def source_a16z():
    text = _get("https://a16z.com/portfolio/", 60).decode("utf-8", "replace")
    dec, best = json.JSONDecoder(), []
    for src in (text, html.unescape(text)):
        for m in re.finditer(r'\[\{"id":"', src):
            try:
                arr, _ = dec.raw_decode(src, m.start())
            except ValueError:
                continue
            if isinstance(arr, list) and len(arr) > len(best):
                best = arr
    if len(best) < 300:
        raise RuntimeError("a16z: portfolio JSON not found in the page (got %d entries); the page layout probably changed" % len(best))
    out = []
    for c in best:
        stages = set(c.get("stages") or [])
        if not stages & {"Venture", "Growth"} or stages & {"M&A", "IPO", "SPAC", "DPO", "EXIT"}:
            continue
        if not c.get("title") or c["title"] == "[untitled]":
            continue
        out.append({"name": c["title"], "website": c.get("web") or "", "source": "a16z %s" % "/".join(sorted(stages)),
                    "size": 0, "note": "founded %s" % (c.get("year_founded") or "?"), "rank": (0, int(c.get("year_founded") or 0))})
    return out


# ---------------------------------------------------------------- board resolution

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def slug_guesses(name, website):
    base = re.sub(r"\b(inc|labs?|technologies|technology|hq|co|corp)\b\.?$", "", name.lower().strip()).strip()
    host = urllib.parse.urlparse(website if "//" in website else "//" + website).netloc.lower()
    label = re.sub(r"^(www\.)", "", host).split(".")[0] if host else ""
    guesses = [_norm(name), _norm(base), re.sub(r"[^a-z0-9]+", "-", base).strip("-"), label, _norm(base) + "hq", _norm(base) + "ai"]
    return [g for g in dict.fromkeys(guesses) if g and len(g) >= 2]


def _titles_pass(titles, include, exclude):
    n = 0
    for t in titles:
        tl = t.lower()
        if any(k in tl for k in include) and not any(k in tl for k in exclude):
            n += 1
    return n


_GH_SUFFIX = {"", "inc", "labs", "lab", "ai", "hq", "technologies", "technology", "co", "corp", "io", "app"}


def _mentions(raw_text, phrase):
    """Whole-word occurrences ("eve" must not match inside "everyone")."""
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", raw_text.lower()))


def _same_company(name, website, raw_text):
    """Ashby/Lever boards carry no company name, so the name has to show up in the job text itself.
    Short or generic names (Eve, Cape, Bounce, Forward...) also need the company's domain in the text."""
    full = name.strip()
    first = full.split()[0]
    ok = _mentions(raw_text, full) >= 2 or (len(first) >= 5 and _mentions(raw_text, first) >= 3)
    if ok and len(_norm(full)) < 6:
        host = urllib.parse.urlparse(website if "//" in website else "//" + website).netloc.lower().removeprefix("www.")
        ok = bool(host) and host in raw_text.lower()
    return ok


def probe(name, website, include, exclude):
    """-> {"ats","slug","jobs","fit"} or None. Tries each ATS with each slug guess; first verified board wins."""
    full = _norm(name)
    for slug in slug_guesses(name, website):
        q = urllib.parse.quote(slug)
        d = _json("https://api.ashbyhq.com/posting-api/job-board/%s" % q)
        jobs = (d or {}).get("jobs") or []
        if len(jobs) >= 3 and _same_company(name, website, " ".join((j.get("descriptionPlain") or "")[:2500] for j in jobs[:6])):
            return {"ats": "ashby", "slug": slug, "jobs": len(jobs), "fit": _titles_pass([j.get("title", "") for j in jobs], include, exclude)}
        meta = _json("https://boards-api.greenhouse.io/v1/boards/%s" % q)
        bn = _norm((meta or {}).get("name", ""))
        # Greenhouse publishes the board's company name: demand the same name, allowing only a harmless suffix
        # ("Forward" must NOT match "Forward Networks": that was a real false positive in the first dry run)
        if bn and bn.startswith(full) and bn[len(full):] in _GH_SUFFIX:
            d = _json("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % q)
            jobs = (d or {}).get("jobs") or []
            if len(jobs) >= 3:
                return {"ats": "greenhouse", "slug": slug, "jobs": len(jobs), "fit": _titles_pass([j.get("title", "") for j in jobs], include, exclude)}
        d = _json("https://api.lever.co/v0/postings/%s?mode=json" % q)
        if isinstance(d, list) and len(d) >= 3 and _same_company(name, website, " ".join((j.get("descriptionPlain") or "")[:2500] for j in d[:6])):
            return {"ats": "lever", "slug": slug, "jobs": len(d), "fit": _titles_pass([j.get("text", "") for j in d], include, exclude)}
    return None


# ---------------------------------------------------------------- bookkeeping

def _tracked_boards():
    """(ats, slug) of everything already watched or queued: catches the same company under a different name
    (Ambience vs Ambience Healthcare)."""
    out = set()
    for f, key in (("companies.yaml", "companies"), ("companies_auto.yaml", None), ("discovery_queue.yaml", "queue")):
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            d = yaml.safe_load(open(p)) or {}
            items = d.get(key) if key and isinstance(d, dict) else (d if isinstance(d, list) else d.get("companies", []))
            out |= {(c.get("ats"), str(c.get("slug", "")).lower().split("?")[0]) for c in (items or []) if isinstance(c, dict)}
    return out


def _tracked_names():
    names = set()
    for f, key in (("companies.yaml", "companies"), ("companies_auto.yaml", None), ("discovery_queue.yaml", "queue")):
        p = os.path.join(HERE, f)
        if not os.path.exists(p):
            continue
        d = yaml.safe_load(open(p)) or {}
        items = d.get(key) if key and isinstance(d, dict) else (d if isinstance(d, list) else d.get("companies", []) if isinstance(d, dict) else [])
        names |= {_norm(c.get("name", "")) for c in (items or []) if isinstance(c, dict)}
    sys.path.insert(0, HERE)
    import scout   # NOT_WATCHABLE groups: names we already know cannot be watched
    for _grp, lst in scout.NOT_WATCHABLE:
        names |= {_norm(re.sub(r"\(.*", "", n)) for n in lst}
    return names


def _load_cache():
    try:
        c = json.load(open(CACHE))
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - CACHE_TTL_DAYS * 86400
    return {k: v for k, v in c.items() if v.get("t", 0) >= cutoff and not v.get("ok")}


def _save_cache(c):
    items = sorted(c.items(), key=lambda kv: -kv[1].get("t", 0))[:CACHE_MAX]
    json.dump(dict(items), open(CACHE, "w"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["yc", "a16z"], required=True)
    ap.add_argument("--add", type=int, default=20, help="how many verified companies to append to the queue")
    ap.add_argument("--scan", type=int, default=250, help="max candidates to probe (each costs up to ~9 requests)")
    ap.add_argument("--min-team", type=int, default=40, help="YC only: minimum team size")
    ap.add_argument("--max-team", type=int, default=3000, help="YC only: maximum team size")
    ap.add_argument("--min-fit", type=int, default=1, help="require at least this many roles passing your title filter")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cond = yaml.safe_load(open(os.path.join(HERE, "conditions.yaml")))
    include = [k.lower() for k in cond.get("include_title_keywords") or []]
    exclude = [k.lower() for k in cond.get("exclude_title_keywords") or []]

    cands = source_yc(a.min_team, a.max_team) if a.source == "yc" else source_a16z()
    tracked, cache = _tracked_names(), _load_cache()
    tracked.add(_norm("Y Combinator"))   # YC lists itself
    fresh = [c for c in cands if _norm(c["name"]) not in tracked]
    cands_sorted = sorted(fresh, key=lambda c: c["rank"], reverse=True)
    todo = [c for c in cands_sorted if _norm(c["name"]) not in cache][:a.scan]
    print("%s: %d candidates, %d not tracked yet, %d probed now (%d cached as 'no board' within %d days)" % (
        a.source, len(cands), len(fresh), len(todo), len(fresh) - len([c for c in cands_sorted if _norm(c['name']) not in cache]), CACHE_TTL_DAYS))

    found, boards = [], _tracked_boards()
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(probe, c["name"], c["website"], include, exclude): c for c in todo}
        for f in cf.as_completed(futs):
            c = futs[f]
            try:
                r = f.result()
            except Exception as e:   # noqa: one bad candidate must not sink the run
                print("  skip %s: %s" % (c["name"], e))
                continue
            if not r:   # only misses are remembered; a hit must be re-ranked against everything else next time
                cache[_norm(c["name"])] = {"t": time.time(), "ok": False}
            if r and (r["ats"], r["slug"].lower()) not in boards:
                found.append((c, r))
    _save_cache(cache)

    good = sorted([(c, r) for c, r in found if r["fit"] >= a.min_fit], key=lambda cr: -cr[1]["fit"])
    print("%d resolved to a public board, %d have >= %d role(s) passing your filter\n" % (len(found), len(good), a.min_fit))
    for c, r in good[:a.add + 10]:
        print("  %-26s %-10s %-22s %3d jobs, %2d fit | %s | %s" % (c["name"][:26], r["ats"], r["slug"][:22], r["jobs"], r["fit"], c["source"], c["note"]))
    chosen = good[:a.add]
    if a.dry_run or not chosen:
        print("\n(dry run: nothing written)" if a.dry_run else "\nnothing to add")
        return
    q = yaml.safe_load(open(QUEUE)) or {}
    queue = q.get("queue") or []
    have = {(c.get("ats"), str(c.get("slug")).lower()) for c in queue}
    # Load + re-dump (same as scout.promote_from_queue does) instead of appending text: scout rewrites this file
    # with its own indentation, so a text append breaks the YAML. Only name/ats/slug are stored: extra keys
    # would travel into companies_auto.yaml on promotion. Where each came from stays in this run's printout.
    for c, r in chosen:
        if (r["ats"], r["slug"].lower()) not in have:
            queue.append({"name": c["name"], "ats": r["ats"], "slug": r["slug"]})
    with open(QUEUE, "w") as f:
        f.write("# Discovery backlog. scout.py promotes the top 2 each run.\n")
        yaml.safe_dump({"queue": queue}, f, sort_keys=False, allow_unicode=True)
    print("\nqueue now holds %d companies (scout promotes 2 per day)" % len(queue))


if __name__ == "__main__":
    main()
