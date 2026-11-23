#!/usr/bin/env python3
"""Which recommended roles still need a tailored resume?  (the input of the /job-resume batch)

    python3 resume_queue.py [--min-fit 3] [--limit 10] [--json]

A role is queued when: Fit >= --min-fit (default 3, i.e. "more than 2"), it is NOT applied, and its URL is
not in resumes.yaml yet. Order: highest fit first, then newest first (a fresh posting is the one still
worth applying to). --limit caps one batch because each resume is hand-tailored (see the job-resume skill)
and costs a few minutes of model time; the rest wait for the next batch.
"""
import csv, json, os, sys, yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def queue(min_fit=3, limit=10):
    done = set()
    p = os.path.join(HERE, "resumes.yaml")
    if os.path.exists(p):
        for i in (yaml.safe_load(open(p)) or {}).get("resumes") or []:
            done.add((i.get("url") or "").strip().lower())
    rows = list(csv.DictReader(open(os.path.join(HERE, "recommendations", "latest.csv"))))
    todo = [r for r in rows if float(r["Fit"] or 0) >= min_fit and r["Applied"] != "Yes"
            and r["URL"].strip().lower() not in done]
    todo.sort(key=lambda r: r["First Seen"], reverse=True)       # newest first ...
    todo.sort(key=lambda r: -float(r["Fit"]))                    # ... then stable-sort by fit
    return todo[:limit], len(todo)


if __name__ == "__main__":
    a = sys.argv[1:]
    opt = lambda k, d: int(a[a.index(k) + 1]) if k in a else d
    batch, total = queue(opt("--min-fit", 3), opt("--limit", 10))
    if "--json" in a:
        print(json.dumps(batch, indent=1))
    else:
        print("%d role(s) need a resume; this batch:" % total)
        for r in batch:
            print("  fit %s  %s: %s  (%s)" % (r["Fit"], r["Company"], r["Role"], r["First Seen"]))
