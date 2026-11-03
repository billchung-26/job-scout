#!/usr/bin/env python3
"""Pull the job description + requirements for roles from the recommendation list, and turn each
into an ATS-oriented brief you (or the future "generate customized resume" step) can work from.

    python3 jd.py list [--min-stars 3] [--company Notion] [--applied]   # numbered picks from latest.csv
    python3 jd.py get 12 40 "notion connections" https://jobs.ashbyhq.com/...   # number | text | URL
    python3 jd.py batch --min-stars 4                                    # every unapplied role at >= 4 stars
    python3 jd.py resume-from "/path/to/Resume.pdf"                      # refresh resume/resume.txt
    python3 jd.py prune                                                  # delete briefs older than 60 days

Output (git-ignored, local only): jobs/<company>__<title>__<id>.md   human brief
                                  jobs/<company>__<title>__<id>.json machine-readable contract for résumé generation
                                  jobs/INDEX.md                      what has been extracted

Row numbers are the line order of recommendations/latest.csv, so run `list` and `get` against the
same file version (the daily run reorders it).
"""
import argparse
import csv
import datetime
import glob
import json
import os
import re
import sys
import time

import jd_fetch
import jd_parse
import scout

HERE = os.path.dirname(os.path.abspath(__file__))
RECS = os.path.join(HERE, "recommendations", "latest.csv")
OUT = os.path.join(HERE, "jobs")
RESUME = os.path.join(HERE, "resume", "resume.txt")
# Retention: briefs exist to feed an application, not to be an archive. Postings close, and a stale
# brief is worse than none, so anything older than this is deleted on every get/batch/prune.
KEEP_DAYS = 60
POLITE_DELAY = 0.3


def load_recs():
    with open(RECS, newline="") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows, 1):
        r["n"] = i
    return rows


def _resume_text():
    if os.path.exists(RESUME):
        return open(RESUME).read()
    print("note: %s not found, so the brief will not include the résumé gap check. "
          "Create it with: python3 jd.py resume-from <your resume pdf>" % os.path.relpath(RESUME, HERE), file=sys.stderr)
    return None


# ---------------------------------------------------------------- selection

def _stars(r):
    try:
        return int(r["Fit"])
    except ValueError:
        return 0


def cmd_list(args):
    for r in load_recs():
        if _stars(r) < args.min_stars or (r["Applied"] == "Yes" and not args.applied):
            continue
        if args.company and args.company.lower() not in r["Company"].lower():
            continue
        print("%4d  %d*  %-16s %s  [%s]" % (r["n"], _stars(r), r["Company"][:16], r["Role"].strip()[:70], r["Location"][:28]))


def _select(selector, rows):
    if selector.isdigit():
        hit = [r for r in rows if r["n"] == int(selector)]
    elif selector.startswith("http"):
        hit = [r for r in rows if r["URL"] == selector]
    else:
        words = selector.lower().split()
        hit = [r for r in rows if all(w in (r["Company"] + " " + r["Role"]).lower() for w in words)]
    if len(hit) != 1:
        print("  '%s': %s" % (selector, "no match in latest.csv" if not hit else
                              "%d matches, be more specific:\n      " % len(hit) + "\n      ".join(
                                  "%d  %s: %s" % (r["n"], r["Company"], r["Role"].strip()[:60]) for r in hit[:8])))
        return None
    return hit[0]


# ---------------------------------------------------------------- extraction

def _company_cfg(name, companies):
    for c in companies:
        if c["name"].lower() == name.lower():
            return c
    raise LookupError("'%s' is not in companies.yaml / companies_auto.yaml any more" % name)


def _slug(row):
    m = re.search(r"gh_jid=(\d+)", row["URL"])   # Stripe-style URLs end in /search; the id is in the query
    ident = m.group(1) if m else re.sub(r"[^A-Za-z0-9]", "", row["URL"].split("?")[0].rstrip("/").split("/")[-1])[:10]
    base = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (row["Company"], row["Role"])).lower()).strip("-")[:70]
    return "%s__%s" % (base, ident)


def build_record(row, job, resume_text):
    structured = jd_parse.to_structured_text(job["html"])
    sections, found = jd_parse.split_sections(structured)
    full = "\n".join(l.lstrip(jd_parse.HEAD) for l in structured.split("\n"))
    req_lines = sections.get("requirements", []) + sections.get("preferred", [])
    kws, other = jd_parse.keyword_report(sections, full, resume_text)
    return {
        "company": row["Company"], "title": job["title"] or row["Role"].strip(), "location": job["location"] or row["Location"],
        "url": row["URL"], "fit_stars": _stars(row), "fit_why": row["Why"], "posted": job["posted"], "comp": job["comp"],
        "extracted_at": datetime.date.today().isoformat(), "sections_found": found,
        "sections": sections, "facts": jd_parse.extract_facts(full, req_lines),
        "keywords": kws, "other_terms": other, "resume_checked": resume_text is not None, "full_text": full}


def render_md(rec):
    L = ["# %s: %s" % (rec["company"], rec["title"]), "",
         "- **Apply:** %s" % rec["url"], "- **Location:** %s" % rec["location"],
         "- **Fit:** %d★ (%s)" % (rec["fit_stars"], rec["fit_why"]),
         "- **Posted:** %s    **Brief extracted:** %s" % (rec["posted"] or "n/a", rec["extracted_at"])]
    pay = rec["comp"] or rec["facts"]["pay_ranges_in_text"]
    if pay:
        L.append("- **Pay:** " + " | ".join(pay))
    L += ["", "## ATS cheat-sheet", "",
          "> A keyword hit is not evidence. Add a term to your résumé only where it is true and you can back it up.", "",
          "**Mirror this title exactly in your headline/summary:** `%s`" % rec["title"], ""]
    f = rec["facts"]
    L.append("**Hard facts:** years asked: %s · education: %s" % (
        ", ".join(map(str, f["years_experience_mentioned"])) or "none stated", ", ".join(f["education_mentioned"]) or "none stated"))
    for c in f["constraints"]:
        L.append("- ⚠ %s: …%s…" % (c["type"], c["context"]))
    L.append("")
    kws = rec["keywords"]
    if rec["resume_checked"]:
        miss = [k for k in kws if k["resume_status"] == "missing" and (k["in_requirements"] or k["count"] >= 3)][:10]
        reword = [k for k in kws if k["resume_status"] == "have, different wording"][:8]
        have = [k for k in kws if k["resume_status"] == "have"][:10]
        L += ["### Not in your résumé yet (add only if true)", ""] + (
            ["- **%s**: posting says %s (req %d / pref %d / total %d)" % (k["concept"], " / ".join("`%s`" % w for w in k["posting_wording"]),
                                                                       k["in_requirements"], k["in_preferred"], k["count"]) for k in miss] or ["- nothing important missing"]) + [""]
        L += ["### Your résumé has the idea but uses different wording (ATS matches literally)", ""] + (
            ["- **%s**: posting `%s`, you wrote `%s`" % (k["concept"], "` / `".join(k["posting_wording"]), "` / `".join(k["resume_wording"])) for k in reword] or ["- none"]) + [""]
        L += ["### Already matching (keep, and lead with the strongest)", ""] + (
            ["- **%s** (req %d, total %d)" % (k["concept"], k["in_requirements"], k["count"]) for k in have] or ["- none"]) + [""]
    else:
        L += ["### Keywords the posting stresses", ""] + ["- **%s**: %s (req %d / pref %d / total %d)" % (
            k["concept"], " / ".join(k["posting_wording"]), k["in_requirements"], k["in_preferred"], k["count"]) for k in kws[:15]] + [""]
    if rec["other_terms"]:
        L += ["**Other terms in the posting (tools/acronyms/names, check relevance):** " + ", ".join(rec["other_terms"]), ""]
    if not rec["sections_found"]:
        L += ["> ⚠ Could not recognise the section headings in this posting, so requirements/preferred are NOT separated "
              "(everything is under Overview). Read the full text below.", ""]
    order = ["requirements", "preferred", "responsibilities", "overview", "about", "compensation"]
    names = {"requirements": "Requirements (as written)", "preferred": "Preferred / nice to have", "responsibilities": "Responsibilities",
             "overview": "Overview", "about": "About the company", "compensation": "Compensation & benefits"}
    for key in order + [k for k in rec["sections"] if k.startswith("other:")]:
        lines = rec["sections"].get(key)
        if lines:
            L += ["## " + names.get(key, key.replace("other: ", "")), ""] + ["- " + l for l in lines] + [""]
    L += ["## Full text", "", rec["full_text"], ""]
    return "\n".join(L)


def write_outputs(rec, row):
    os.makedirs(OUT, exist_ok=True)
    base = os.path.join(OUT, _slug(row))
    with open(base + ".json", "w") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
    with open(base + ".md", "w") as f:
        f.write(render_md(rec))
    return base + ".md"


def write_index():
    rows = []
    for p in sorted(glob.glob(os.path.join(OUT, "*.json"))):
        try:
            r = json.load(open(p))
        except ValueError:
            continue
        rows.append("| %d★ | %s | %s | %s | [brief](%s) | %s |" % (r["fit_stars"], r["company"], r["title"].replace("|", "/"), r["extracted_at"],
                                                                    os.path.basename(p)[:-5] + ".md", r["url"]))
    with open(os.path.join(OUT, "INDEX.md"), "w") as f:
        f.write("# Extracted job briefs (auto-generated by jd.py; briefs older than %d days are deleted)\n\n| Fit | Company | Role | Extracted | Brief | Posting |\n|---|---|---|---|---|---|\n%s\n"
                % (KEEP_DAYS, "\n".join(rows)))


def cmd_prune(_args=None, quiet=False):
    cutoff, gone = time.time() - KEEP_DAYS * 86400, 0
    for p in glob.glob(os.path.join(OUT, "*.md")) + glob.glob(os.path.join(OUT, "*.json")):
        if os.path.basename(p) != "INDEX.md" and os.path.getmtime(p) < cutoff:
            os.remove(p)
            gone += 1
    if os.path.isdir(OUT):
        write_index()
    if not quiet or gone:
        print("pruned %d file(s) older than %d days" % (gone, KEEP_DAYS))


def _extract(rows_to_do, companies):
    resume, ok, failed = _resume_text(), [], []
    for row in sorted(rows_to_do, key=lambda r: r["Company"]):   # same company together: Ashby/Jibe boards are cached per run
        label = "%s: %s" % (row["Company"], row["Role"].strip()[:60])
        try:
            job = jd_fetch.fetch_job(_company_cfg(row["Company"], companies), row["URL"])
            path = write_outputs(build_record(row, job, resume), row)
            ok.append(path)
            print("  ok   %s\n       -> %s" % (label, os.path.relpath(path, HERE)))
        except LookupError as e:
            failed.append(label)
            print("  SKIP %s\n       %s" % (label, e))
        time.sleep(POLITE_DELAY)
    write_index()
    print("\n%d brief(s) written, %d skipped. Index: jobs/INDEX.md" % (len(ok), len(failed)))
    if rows_to_do and not ok:
        sys.exit(1)


def cmd_get(args):
    cmd_prune(quiet=True)
    rows = load_recs()
    picked = [r for r in (_select(s, rows) for s in args.what) if r]
    if picked:
        _extract(picked, scout.load_companies())


def cmd_batch(args):
    cmd_prune(quiet=True)
    todo = [r for r in load_recs() if _stars(r) >= args.min_stars and r["Applied"] != "Yes"]
    print("%d unapplied role(s) at >= %d stars" % (len(todo), args.min_stars))
    if len(todo) > args.limit:
        sys.exit("That is more than --limit %d (each is a live API call; Apple/AMD boards are slow). "
                 "Raise --limit or the star threshold." % args.limit)
    _extract(todo, scout.load_companies())


def cmd_resume_from(args):
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("pypdf is not installed: run `pip3 install pypdf`, or paste your résumé text into resume/resume.txt by hand.")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(args.pdf).pages)
    if len(text.strip()) < 500:
        sys.exit("Only %d characters came out of that PDF (scanned image?). Paste the text into resume/resume.txt by hand." % len(text.strip()))
    os.makedirs(os.path.dirname(RESUME), exist_ok=True)
    with open(RESUME, "w") as f:
        f.write(text)
    print("wrote %s (%d chars). It is git-ignored: it contains your phone/email." % (os.path.relpath(RESUME, HERE), len(text)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--min-stars", type=int, default=3); p.add_argument("--company"); p.add_argument("--applied", action="store_true"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("get"); p.add_argument("what", nargs="+"); p.set_defaults(fn=cmd_get)
    p = sub.add_parser("batch"); p.add_argument("--min-stars", type=int, default=4); p.add_argument("--limit", type=int, default=60); p.set_defaults(fn=cmd_batch)
    p = sub.add_parser("resume-from"); p.add_argument("pdf"); p.set_defaults(fn=cmd_resume_from)
    p = sub.add_parser("prune"); p.set_defaults(fn=cmd_prune)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
