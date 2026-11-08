#!/usr/bin/env python3
"""Mark roles from the recommendation list as applied, from any checkout of this repo.

    python3 mark_applied.py "Cloudflare: Signals & Data Products" "Hebbia: Product Manager" [--push]
    python3 mark_applied.py --env ROLES          # same, reading ";"-separated specs from $ROLES (used by the GitHub workflow)

A spec is  "<company>: <words of the title>"  (a "|" instead of ":" also works). It must match exactly ONE
row of recommendations/latest.csv, or nothing is written and the candidates are listed. That makes a typo
or an ambiguous title a loud, harmless failure instead of marking the wrong role.

What it does: appends one line per role to applied.yaml (the URL is the key; the daily run and
`recommend.py` read it). It does NOT regenerate the list: the next daily run does that, or
`python3 recommend.py` locally (~10 min). --push commits applied.yaml and pushes it.

Row identity: latest.csv shows ONE url per collapsed multi-location role; marking that url marks the whole row
(see scout._collapse_multilocation).
"""
import csv
import datetime
import os
import re
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
RECS = os.path.join(HERE, "recommendations", "latest.csv")
APPLIED = os.path.join(HERE, "applied.yaml")


def _split(spec):
    for sep in (":", "|"):
        if sep in spec:
            c, t = spec.split(sep, 1)
            return c.strip(), t.strip()
    sys.exit("'%s': write it as  Company: words of the title  (e.g. 'Cloudflare: Signals & Data Products')." % spec)


def _resolve_url(url, rows):
    """A pasted link -> the list row with the same job id (uuid / gh_jid / numeric id), or a stand-in row.
    Matching by id, not by string, because the list may hold a different spelling of the same posting
    (brex.com/careers/ID?gh_jid=ID vs job-boards.greenhouse.io/brex/jobs/ID; Ashby slug case)."""
    ids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{6,}", url.lower())
    hit = [r for r in rows if any(i in r["URL"].lower() for i in ids)] if ids else []
    if len(hit) == 1:
        return hit[0]
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    if hit:
        sys.exit("'%s' matches %d rows in latest.csv; mark them by 'Company: title' instead." % (url, len(hit)))
    return {"Company": host, "Role": "(not in the current list)", "URL": url}


def _resolve(spec, rows):
    if spec.lower().startswith("http"):
        return _resolve_url(spec, rows)
    company, words = _split(spec)
    cl, ws = company.lower(), words.lower().split()
    exact = [r for r in rows if r["Company"].lower() == cl]
    pool = exact or [r for r in rows if cl in r["Company"].lower()]
    norm = lambda t: " ".join(t.lower().split())
    # 1) a row whose whole title equals what was typed wins ("Product Manager" must not also match "Research Product Manager")
    exact = [r for r in pool if norm(r["Role"]) == norm(words)]
    if len(exact) == 1:
        return exact[0]
    # 2) otherwise every typed word must appear as a WHOLE word ("ai" must not match inside "Staircase")
    def has(title, w):
        return re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", title) is not None
    hit = [r for r in pool if all(has(norm(r["Role"]), w) for w in ws)]
    if len(hit) == 1:
        return hit[0]
    if not pool:
        sys.exit("'%s': no company called '%s' in latest.csv." % (spec, company))
    if not hit:
        sys.exit("'%s': no role at %s contains all of: %s.\n  %s's roles:\n    %s" % (
            spec, company, " ".join(ws), company, "\n    ".join(r["Role"].strip() for r in pool[:12])))
    sys.exit("'%s' matches %d roles at %s, add more words of the title:\n    %s" % (
        spec, len(hit), company, "\n    ".join(r["Role"].strip() for r in hit[:8])))


def main():
    args = sys.argv[1:]
    push = "--push" in args
    args = [a for a in args if a != "--push"]
    if args[:1] == ["--env"]:
        specs = [s.strip() for s in os.environ.get(args[1], "").split(";") if s.strip()]
    else:
        specs = args
    if not specs:
        sys.exit(__doc__)

    with open(RECS, newline="") as f:
        rows = list(csv.DictReader(f))
    with open(APPLIED) as f:
        text = f.read()
    # resolve everything first: if any spec fails, nothing is written
    picked = [(_resolve(s, rows), s) for s in specs]
    lines, done = [], []
    for r, spec in picked:
        label = "%s: %s" % (r["Company"], r["Role"].strip())
        if r["URL"].lower() in text.lower():        # applied.yaml is URL-keyed; a substring test is exact enough for full URLs
            print("already applied: %s" % label)
            continue
        role = r["Role"].strip().replace('"', "'")
        lines.append('  - {company: %s, role: "%s", url: "%s"}' % (r["Company"], role, r["URL"]))
        done.append(label)
    if lines:
        header = "  # --- applied %s (mark_applied.py) ---\n" % datetime.date.today().isoformat()
        new = text.rstrip("\n") + "\n" + header + "\n".join(lines) + "\n"
        yaml.safe_load(new)                      # never leave an unparsable applied.yaml behind
        tmp = APPLIED + ".tmp"
        with open(tmp, "w") as f:
            f.write(new)
        os.replace(tmp, APPLIED)
    for label in done:
        print("marked applied: %s" % label)
    if push and lines:
        def git(*a):
            subprocess.run(["git", "-C", HERE] + list(a), check=True)
        git("add", "applied.yaml")
        git("commit", "-q", "-m", "Mark %d role(s) as applied: %s" % (len(done), "; ".join(done))[:200])
        git("pull", "--rebase", "--autostash", "-q")
        git("push", "-q")
        print("pushed. The list and the Drive sheet update on the next daily run.")


if __name__ == "__main__":
    main()
