# Job Scout

A personal, self-hosted job watcher for AI companies. It checks companies' career
boards every morning, filters roles to your profile, ranks them by fit, and pushes
new matches to Telegram — then gradually grows its own watchlist with related startups.

No LinkedIn or Indeed dependency. It reads the **public job-board APIs** that most AI
companies use (Greenhouse, Lever, Ashby) and iCIMS Jibe career sites (AMD), so there's nothing to scrape and nothing to
authorize.

## Features

- **Multi-company watcher** — pulls live openings from Greenhouse / Lever / Ashby boards.
- **Fit filtering + ranking** — keep/drop by title & location keywords; rank by priority keywords. All editable in `conditions.yaml`, no code changes.
- **"What's new" diffing** — after the first run, only newly posted roles are surfaced.
- **Telegram push** — new matches (with clickable links) sent straight to your phone.
- **Self-growing watchlist** — a vetted backlog (`discovery_queue.yaml`) of related startups; the tool promotes a couple per day into `companies_auto.yaml` and tells you what it added.
- **On-demand recommender** — `recommend.py` produces a full ranked list of every current matching role across all watched companies.
- **Auto watchlist report** — `watchlist.md` (generated each run) shows what's watched, what's queued, and what can't be watched.

## Layout

| File | Purpose |
|---|---|
| `scout.py` | Main run: fetch → filter → rank → diff → notify → grow the list |
| `recommend.py` | On-demand full ranked recommendation across all companies |
| `get_chat_id.py` | Helper to find your Telegram chat id |
| `companies.yaml` | Your curated watchlist (companies + ATS + slug) |
| `discovery_queue.yaml` | Vetted backlog of related startups to auto-add |
| `conditions.yaml` | Editable filters + fit-scoring weights (`fit:` block) |
| `jibe.py` | Fetcher for iCIMS Jibe career sites (`ats: jibe`, slug = hostname + optional API params) |
| `fit.py` | Fit scoring v2: role family + seniority + résumé themes in title *and* job description. Weights live in `conditions.yaml` |
| `secrets.example.yaml` | Template — copy to `secrets.yaml` and fill in (git-ignored) |

## Setup

```bash
# 1. Create a Telegram bot via @BotFather, then:
cp secrets.example.yaml secrets.yaml       # paste your bot token
# 2. Message your bot once, then get your chat id:
python3 get_chat_id.py                       # paste the number into secrets.yaml
# 3. Test the pipe:
python3 scout.py --test
# 4. Run it:
python3 scout.py            # daily watcher
python3 recommend.py        # on-demand full recommendations
```

Requires Python 3 with `pyyaml` and `certifi`.

### Automating (macOS)

Schedule `scout.py` with a `launchd` agent (e.g. daily at 8:00 AM) so new-role alerts
arrive without you doing anything.

## Notes

- Some companies (Google, Waymo, and others on Workday/custom systems) don't
  expose a public board and can't be watched this way — `watchlist.md` lists them.
- `secrets.yaml`, runtime state, and generated reports are git-ignored.
