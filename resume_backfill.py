#!/usr/bin/env python3
"""One-off/idempotent: link resumes that /job-resume already made (recorded in ~/Desktop/job-answers/companies/*.md)
to the roles in latest.csv, by job id, and write them to resumes.yaml.

    python3 resume_backfill.py [--dry-run]

Matching is by posting id (uuid or 6+ digit number in the job URL), never by title, so a different role at the
same company is not linked (e.g. Alpaca 'Trading API' vs 'Infrastructure and Platforms'). A section counts only
if it has a PDF link; sections without one (e.g. 'asked again') are ignored.
"""
import csv, glob, os, re, sys, yaml
HERE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.expanduser("~/Desktop/job-answers/companies")
ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?<!\d)\d{6,}(?!\d)")
PDF = re.compile(r"\[(?:pdf|PDF)\]\((https://drive\.google\.com/file/d/[^)\s]+)\)")
rows = list(csv.DictReader(open(os.path.join(HERE, "recommendations", "latest.csv"))))
p = os.path.join(HERE, "resumes.yaml")
have = {(i.get("url") or "").lower() for i in ((yaml.safe_load(open(p)) if os.path.exists(p) else None) or {}).get("resumes") or []}
found = []
for md in sorted(glob.glob(COMP + "/*.md")):
    for sec in re.split(r"(?m)^## ", open(md).read())[1:]:
        pdf = PDF.search(sec)
        if not pdf:
            continue
        job = re.search(r"(?i)\bjob:?\*{0,2}:?\s*(https?://\S+)", sec)
        ids = ID.findall(job.group(1).lower()) if job else []
        for r in rows:
            if r["URL"].lower() not in have and ids and any(i in r["URL"].lower() for i in ids):
                found.append((r, pdf.group(1)))
                have.add(r["URL"].lower())
for r, link in found:
    print("%-12s %s -> %s" % (r["Company"], r["Role"].strip(), link[-20:]))
print(len(found), "linked")
if "--dry-run" not in sys.argv:
    import subprocess
    for r, link in found:
        subprocess.run([sys.executable, os.path.join(HERE, "resume_record.py"), "--url", r["URL"], "--pdf", link,
                        "--company", r["Company"], "--role", r["Role"].strip()], check=True, stdout=subprocess.DEVNULL)
