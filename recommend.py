#!/usr/bin/env python3
"""
On-demand recommender: fetch ALL current matching roles across every watched
company (curated + auto), rank by fit, and write a report.

    python3 recommend.py

Unlike scout.py (which pushes only NEW roles), this shows the full current set so
you can decide what to apply to today. Writes recommendations/YYYY-MM-DD.md.
"""
import os
from datetime import datetime

import scout  # reuse fetchers / matching / config loaders

OUT_DIR = os.path.join(scout.HERE, "recommendations")


def main():
    companies = scout.load_companies()
    cond = scout.load_conditions()

    rows, errors = [], []
    for c in companies:
        fetch = scout.FETCHERS.get(c.get("ats"))
        if not fetch:
            continue
        try:
            jobs = fetch(c["slug"])
        except Exception as e:  # noqa
            errors.append("%s: %s" % (c.get("name"), e))
            continue
        for j in jobs:
            if scout.matches(j, cond):
                j["score"] = scout.fit_score(j, cond)
                rows.append((c["name"], j))

    rows.sort(key=lambda r: (-r[1]["score"], r[0].lower()))

    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(OUT_DIR, "%s.md" % today)
    lines = ["# Job recommendations — %s" % today, "",
             "**%d matching roles** across %d companies (ranked by fit score)." %
             (len(rows), len(companies)), ""]
    for name, j in rows:
        star = " ⭐" * j["score"]
        lines.append("- [%d] **%s** — [%s](%s) — %s%s" %
                     (j["score"], name, j["title"], j["url"], j["location"] or "n/a", star))
    if errors:
        lines.append("\n---\n### ⚠️ Issues")
        lines += ["- %s" % e for e in errors]
    with open(path, "w") as f:
        f.write("\n".join(lines))

    # stdout for review (tab-separated, sorted by score)
    for name, j in rows:
        print("%d\t%s\t%s\t%s" % (j["score"], name, j["title"], j["location"]))
    print("TOTAL\t%d roles\t(saved %s)" % (len(rows), path))


if __name__ == "__main__":
    main()
