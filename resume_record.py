#!/usr/bin/env python3
"""Record the Drive links of a tailored resume, so the next daily run shows them in the list.

    python3 resume_record.py --url <job url> --pdf <drive link> [--docx <drive link>] [--company X --role Y] [--push]

Appends to resumes.yaml (URL is the key, like applied.yaml). Idempotent: a URL already present is updated
in place. Permanent by design: this file is the only index from a job to its resume, and it is small
(one line per job). Roles that vanish from the list simply stay here unused.
"""
import argparse, datetime, os, subprocess, yaml

HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "resumes.yaml")

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True); ap.add_argument("--pdf", required=True)
ap.add_argument("--docx", default=""); ap.add_argument("--company", default=""); ap.add_argument("--role", default="")
ap.add_argument("--push", action="store_true")
ap.add_argument("--refresh", action="store_true", help="after --push, start the daily run so the list/sheet show the link now")
a = ap.parse_args()

d = (yaml.safe_load(open(P)) if os.path.exists(P) else None) or {}
items = d.get("resumes") or []
new = {"company": a.company, "role": a.role, "url": a.url, "pdf": a.pdf, "docx": a.docx,
       "date": datetime.date.today().isoformat()}
for i, it in enumerate(items):
    if (it.get("url") or "").lower() == a.url.lower():
        items[i] = new; break
else:
    items.append(new)
tmp = P + ".tmp"
with open(tmp, "w") as f:
    f.write("# Tailored resume per job (written by resume_record.py; read by scout.py for the Resume column)\n")
    yaml.safe_dump({"resumes": items}, f, sort_keys=False, allow_unicode=True, width=200)
os.replace(tmp, P)
print("recorded: %s %s" % (a.company, a.role))
if a.push:
    g = lambda *x: subprocess.run(["git", "-C", HERE] + list(x), check=True)
    g("add", "resumes.yaml")
    if subprocess.run(["git", "-C", HERE, "diff", "--cached", "--quiet"]).returncode:   # nothing staged = already recorded
        g("commit", "-q", "-m", "Record resume: %s %s" % (a.company, a.role))
    g("pull", "--rebase", "--autostash", "-q"); g("push", "-q")
    print("pushed")
if a.refresh:
    import mark_applied
    mark_applied._dispatch_daily()
