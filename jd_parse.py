"""Turn a job posting (HTML) into: labelled sections, hard facts, and an ATS keyword report.

Pure functions, no network. The keyword report is the part meant for résumé tailoring:
for every concept in jd_keywords.yaml it says how hard the posting leans on it (counts in the
requirements vs. anywhere), WHICH spelling the posting uses, and whether the résumé already
uses that spelling.

Known limits (be honest about these when relying on the output):
  * Section splitting is heuristic (headings vary wildly). When no known heading is found,
    `sections_found` is False and everything lands in "overview": still searchable, not split.
  * Keyword matching is literal/word-boundary. It cannot tell "we use SQL" from "SQL is a plus",
    except through the requirements/preferred split.
  * "have" only means the word appears in the résumé, not that it is said convincingly.
"""
import html
import os
import re
from collections import Counter

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
HEAD = "\x01"           # marks a heading line while converting HTML
BOLD_OPEN, BOLD_CLOSE = "\x02", "\x03"

# Order matters: first match wins ("preferred qualifications" must not fall into requirements).
SECTION_RULES = [
    ("preferred",        r"preferred|nice[- ]to[- ]have|bonus|a plus|great to have|ideal(ly)?|extra credit|even better|stand out"),
    ("compensation",     r"compensation|salary|pay range|base pay|pay transparency|total rewards|benefits|perks|what we offer|equity"),
    ("legal",            r"equal (employment )?opportunity|eeo|accommodation|privacy|e-?verify|background check|fair chance|applicant notice"),
    ("requirements",     r"requirement|qualification|what we.re looking for|who you are|about you|you have|you bring|you.ll bring|must[- ]have|minimum|basic|what you.ll need|what you need|skills|experience|you are|ideal candidate"),
    ("responsibilities", r"responsibilit|what you.ll do|what you will do|you will|you.ll|day[- ]to[- ]day|in this role|the role|your impact|what you.ll own|key duties|the job|about the (role|job|position|team)|the opportunity|your mission|what you.ll be doing"),
    ("about",            r"about (us|the company)|who we are|our (mission|story|team|company)|company overview|overview|why [a-z0-9. ]+\??$"),
]


def to_structured_text(raw):
    """HTML (or already-plain text) -> text with one block per line, '- ' bullets and heading marks."""
    s = raw or ""
    if "&lt;" in s:               # Greenhouse ships the HTML escaped inside its JSON
        s = html.unescape(s)
    s = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", lambda m: "\n" + HEAD + m.group(1) + "\n", s, flags=re.S | re.I)
    s = re.sub(r"<(strong|b)\b[^>]*>", BOLD_OPEN, s, flags=re.I)
    s = re.sub(r"</(strong|b)>", BOLD_CLOSE, s, flags=re.I)
    s = re.sub(r"<li\b[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"</li>|<br\s*/?>|</p>|</div>|</ul>|</ol>|<p\b[^>]*>|<div\b[^>]*>|<ul\b[^>]*>|<ol\b[^>]*>|</tr>|</h[1-6]>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = []
    for ln in s.split("\n"):
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        ln = re.sub(r"^[•·▪◦‣•●]\s*", "- ", ln)       # plain-text bullets (Apple)
        if not ln or ln in ("-",):
            continue
        # a line that is entirely one bold run is a heading
        m = re.fullmatch(BOLD_OPEN + r"(.+?)" + BOLD_CLOSE + r"\s*:?", ln)
        if m and len(m.group(1)) <= 90 and not ln.startswith("- "):
            ln = HEAD + m.group(1)
        lines.append(ln.replace(BOLD_OPEN, "").replace(BOLD_CLOSE, ""))
    return "\n".join(lines)


def classify(heading):
    h = re.sub(r"\s+", " ", heading.lower().strip(" :*-–—"))
    for name, rx in SECTION_RULES:
        if re.search(rx, h):
            return name
    return None


def _looks_like_heading(line):
    if line.startswith(HEAD):
        return True
    if line.startswith("- ") or len(line) > 70 or line.endswith("."):
        return False
    return line.endswith(":") or (len(line.split()) <= 6 and classify(line) is not None)


def split_sections(structured):
    """-> ({section: [lines]}, sections_found). Unknown headings keep their own name under 'other: <heading>'."""
    secs, cur, found = {"overview": []}, "overview", False
    for line in structured.split("\n"):
        if _looks_like_heading(line):
            head = line.lstrip(HEAD).strip(" :")
            kind = classify(head)
            if kind:
                found = True
            cur = kind or "other: " + head
            secs.setdefault(cur, [])
            continue
        secs[cur].append(line[2:].strip() if line.startswith("- ") else line)
    return {k: v for k, v in secs.items() if v}, found


# ---------------------------------------------------------------- facts

def extract_facts(text, requirement_lines):
    req = " ".join(requirement_lines) or text
    years = sorted({int(m.group(1)) for m in re.finditer(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", req, re.I)
                    if int(m.group(1)) <= 20})
    edu = sorted({m.group(0).lower() for m in re.finditer(r"bachelor'?s?|master'?s?|\bmba\b|ph\.?d|\bdegree\b", text, re.I)})
    cons = []
    for label, rx in [("work authorization/sponsorship", r"work authorization|authori[sz]ed to work|sponsorship|visa\b"),
                      ("security clearance / citizenship", r"security clearance|u\.?s\.? citizen|citizenship|itar|export control"),
                      ("on-site/hybrid expectation", r"\bon-?site\b|in[- ]office|hybrid|\d\s*days? (?:a|per) week"),
                      ("relocation", r"relocat"),
                      ("travel", r"travel (?:up to |required|\d)|\d+%\s*travel")]:
        m = re.search(r".{0,70}(?:%s).{0,70}" % rx, text, re.I | re.S)
        if m:
            cons.append({"type": label, "context": re.sub(r"\s+", " ", m.group(0)).strip()})
    pay = [re.sub(r"\s+", " ", m.group(0)) for m in re.finditer(
        r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d+)?\s?[kK]?\s*(?:-|–|—|to)\s*\$?\s?\d{2,3}(?:,\d{3})?(?:\.\d+)?\s?[kK]?", text)]
    return {"years_experience_mentioned": years, "education_mentioned": edu, "constraints": cons, "pay_ranges_in_text": pay[:4]}


# ---------------------------------------------------------------- keywords

def _load_groups(path=None):
    with open(path or os.path.join(HERE, "jd_keywords.yaml")) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("groups") or [], {t.lower() for t in cfg.get("ignore_terms") or []}


def _form_rx(form):
    f = form.strip().lower()
    wild = f.endswith("*")
    core = re.escape(f.rstrip("*")).replace(r"\ ", r"[\s\-]+").replace(r"\-", r"[\s\-]")
    return re.compile(r"(?<![\w])" + core + (r"\w*" if wild else "") + r"(?![\w])", re.I)


def _surface_forms(text, forms):
    """Counter of the literal strings (lower-cased) that matched, across all forms of one group."""
    c = Counter()
    for form in forms:
        for m in _form_rx(form).finditer(text):
            c[m.group(0).lower()] += 1
    return c


def keyword_report(sections, full_text, resume_text=None, groups_path=None):
    groups, ignore = _load_groups(groups_path)
    req_text = "\n".join(sections.get("requirements", []))
    pref_text = "\n".join(sections.get("preferred", []))
    rows = []
    for g in groups:
        total = _surface_forms(full_text, g["forms"])
        if not total:
            continue
        n_total = sum(total.values())
        n_req = sum(_surface_forms(req_text, g["forms"]).values())
        n_pref = sum(_surface_forms(pref_text, g["forms"]).values())
        row = {"concept": g["label"], "count": n_total, "in_requirements": n_req, "in_preferred": n_pref,
               "posting_wording": [f for f, _ in total.most_common(3)]}
        if resume_text is not None:
            have = _surface_forms(resume_text, g["forms"])
            same = [f for f in row["posting_wording"] if f in have]
            row["resume_status"] = "have" if same else ("have, different wording" if have else "missing")
            row["resume_wording"] = [f for f, _ in have.most_common(3)]
        rows.append(row)
    # requirements weigh double, preferred count once, mere mention is the tiebreak
    rows.sort(key=lambda r: (-(2 * r["in_requirements"] + r["in_preferred"]), -r["count"]))
    covered = {w for r in rows for w in r["posting_wording"]}
    other = Counter(t for t in re.findall(r"\b[A-Z][A-Za-z0-9]*[A-Z0-9][A-Za-z0-9+#.]*\b", full_text)
                    if t.lower() not in ignore and t.lower() not in covered and len(t) > 1)
    return rows, [t for t, n in other.most_common(15) if n >= 2]
