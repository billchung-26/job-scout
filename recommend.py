#!/usr/bin/env python3
"""
On-demand recommender. Fetches all current matching roles across every watched
company and refreshes recommendations/latest.md (same engine scout.py runs daily):
hides roles you've applied to (applied.yaml) and sunsets any role recommended for
more than 30 days.

    python3 recommend.py
"""
import scout  # reuse fetchers / matching / config / recommendation engine


def main():
    companies = scout.load_companies()
    cond = scout.load_conditions()

    all_matching, errors = [], []
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
                all_matching.append((c["name"], j))

    records = scout.update_recommendations(all_matching)
    for r in records[:15]:
        print("%d\t%s\t%s\t%s\t%s" %
              (r["score"], "APPLIED" if r["applied"] else "-", r["company"], r["role"], r["location"]))
    print("TOTAL: %d roles  ->  recommendations/latest.xlsx / .csv / .md" % len(records))
    for e in errors:
        print("skip: %s" % e)


if __name__ == "__main__":
    main()
