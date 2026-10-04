"""Fit scoring v2 — how well does a posting match Bill's profile?

Replaces the old score ("count priority keywords found in the TITLE"). The old score is still
used as a fallback when conditions.yaml has no `fit:` block. All weights live in conditions.yaml;
this module holds only the mechanics, so tuning never needs a code change.

    points = role_family + seniority + themes(title + job description) + penalty
    stars  = how many `star_thresholds` the points reach            (0..5)

Why the old score was replaced:
  - it only read the title, never the job description;
  - it matched raw substrings ("ai" fired on "ret-ai-l", "data" on "database");
  - "enterprise" counted the same as "pricing", although pricing/monetization is the strongest
    signal in the résumé;
  - it had no idea whether a job was a PM job at all ("Data Scientist, GTM" earned 3 stars).

Assumption to revisit: the themes/weights are derived from the résumé dated 2026-10-04
(usage-based pricing, AI credits, metering, CDP/segmentation, migrations at scale). When the
résumé changes, edit the `fit:` block — not this file.
"""
import html
import re

# Bounds memory and regex time. The signal in a JD is front-loaded (summary, responsibilities),
# so truncating costs little. ~2x the longest JD seen across the watched boards.
MAX_DESC_CHARS = 20000


def html_to_text(s):
    """Greenhouse ships the JD as HTML-escaped HTML inside JSON; Lever lists are plain HTML."""
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)  # second pass: entities that were double-encoded (&amp;nbsp; etc.)
    return re.sub(r"\s+", " ", s).strip()[:MAX_DESC_CHARS]


def _term_regex(term):
    """'monetiz*' -> monetiz\\w*   |   'usage-based' / 'usage based' -> usage[\\s-]+based."""
    t = term.strip().lower()
    wild = t.endswith("*")
    parts = [p for p in re.split(r"[\s\-]+", t.rstrip("*")) if p]
    return r"[\s\-]+".join(re.escape(p) for p in parts) + (r"\w*" if wild else "")


def compile_terms(terms):
    """Word-boundary matcher for a list of terms (None if the list is empty)."""
    if not terms:
        return None
    return re.compile(r"\b(?:%s)\b" % "|".join(_term_regex(t) for t in terms), re.I)


class _Model:
    def __init__(self, cfg):
        def entries(key):
            return cfg.get(key) or []
        self.family = [(e["label"], float(e["points"]), compile_terms(e["titles"]))
                       for e in entries("role_family")]
        self.penalty = [(e["label"], float(e["points"]), compile_terms(e["titles"]),
                         compile_terms(e.get("unless"))) for e in entries("role_penalty")]
        self.seniority = [(e["label"], float(e["points"]), compile_terms(e["titles"]))
                          for e in entries("seniority")]
        self.themes = [(e["label"], float(e.get("title", 0)), float(e.get("jd", 0)),
                        float(e.get("depth", 0)), compile_terms(e["terms"]))
                       for e in entries("themes")]
        # Two caps, deliberately: generic words ("platform", "enterprise", "AI") appear in nearly
        # every JD, so JD evidence may add at most `jd_cap` — it must never outweigh the title.
        self.theme_cap = float(cfg.get("theme_cap", 8))
        self.jd_cap = float(cfg.get("jd_cap", 5))
        self.depth_min = int(cfg.get("depth_min_hits", 3))
        self.thresholds = sorted(float(t) for t in cfg.get("star_thresholds", [5, 8, 11, 14, 17]))


_MODELS = {}  # id(cfg) -> (cfg, model); cfg is held so the id can't be recycled. One cfg per process.


def _model(cfg):
    hit = _MODELS.get(id(cfg))
    if hit is None:
        hit = _MODELS[id(cfg)] = (cfg, _Model(cfg))
    return hit[1]


def score_job(job, cfg):
    """-> (stars, why, points). `why` is a short human-readable reason shown in the digest."""
    m = _model(cfg)
    title = job.get("title", "") or ""
    desc = job.get("desc", "") or ""
    points, why = 0.0, []

    # 1. role family — best match wins (a PM title shouldn't be summed with its own sub-words)
    fam = max(((p, l) for l, p, rx in m.family if rx and rx.search(title)), default=None)
    if fam:
        points += fam[0]
        why.append(fam[1])

    # 2. not-my-job-family penalty — worst match wins, unless an exemption phrase is present
    pens = [(p, l) for l, p, rx, unless in m.penalty
            if rx and rx.search(title) and not (unless and unless.search(title))]
    if pens:
        worst = min(pens)
        points += worst[0]
        why.append("⚠ " + worst[1])

    # 3. seniority — best bonus + worst malus (so "Senior ... Director" nets out, not stacks)
    sen = [(p, l) for l, p, rx in m.seniority if rx and rx.search(title)]
    if sen:
        best, worst = max(sen), min(sen)
        for p, l in {best, worst}:
            points += p
            if p != 0:
                why.append(l)

    # 4. themes — title hits and JD hits are capped separately. A JD hit counts once, plus the
    #    theme's `depth` bonus if it is mentioned >= depth_min_hits times (separates "this role IS
    #    pricing" from "works with the pricing team"). A theme already hit in the title is not
    #    counted again from the JD.
    title_pts = jd_pts = 0.0
    hit_labels = []
    for label, tp, jp, depth, rx in m.themes:
        if rx is None:
            continue
        if tp and rx.search(title):
            title_pts += tp
            hit_labels.append((tp, label))
        elif jp and desc:
            n = len(rx.findall(desc))
            if n:
                p = jp + (depth if n >= m.depth_min else 0.0)
                jd_pts += p
                hit_labels.append((p, label))
    points += min(title_pts, m.theme_cap) + min(jd_pts, m.jd_cap)

    top = [l for _, l in sorted(hit_labels, reverse=True)[:3]]
    reason = " · ".join(why)
    if top:
        reason += (" | " if reason else "") + ", ".join(top)

    stars = sum(1 for t in m.thresholds if points >= t)
    return stars, reason, round(points, 1)
