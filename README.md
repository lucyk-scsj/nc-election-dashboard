# NC Election Dashboard — 2026 Election Data Dashboard

A public, auto-updating dashboard of actionable NC absentee/provisional data —
built to answer "what's still fixable?" rather than just showing final counts.
Pulls directly from the [NC State Board of Elections public data files](https://www.ncsbe.gov/results-data/absentee-and-provisional-data)
every day via GitHub Actions, and is hosted for free on GitHub Pages.

**Public, no login required.** 

## What it shows

- % curable mail ballots
- SDR (same-day registration) failed verification
- SDR cured
- Absentee cured
- Provisional ballot outcomes (approved / not counted / partial) and top
  rejection reasons
- Demographics (race, gender, party) of returned absentee ballots
- County-level breakdown
- Daily trend lines once a few days of history accumulate

Everything is an **aggregate count**. The pipeline drops voter name/address/phone
columns immediately after downloading the raw NCSBE file — no individual voter
record is ever written to this repo or displayed on the page.

## How it works

```
scripts/build_data.py   -> downloads + aggregates NCSBE files, writes docs/data/*.json
.github/workflows/...   -> runs the script daily, commits the data, deploys docs/ to Pages
docs/index.html          -> the dashboard itself, reads docs/data/latest.json + trend.json
```

## One-time setup

1. Create a new GitHub repo and push this folder's contents to it.
2. In the repo settings → **Pages**, set the source to **GitHub Actions**.
3. In repo settings → **Actions → General → Workflow permissions**, choose
   **"Read and write permissions"** (the workflow needs to commit the daily
   data update).
4. Push to `main` — the first workflow run will publish the site. After that
   it runs automatically every day (see the cron schedule in
   `.github/workflows/update-data.yml`); you can also trigger it manually
   from the **Actions** tab any time (e.g. the morning after Election Day).

## ⚠️ Before it goes live: calibrate the status categories

NCSBE doesn't publish a fixed enum for `ballot_rtn_status` / `mail_veri_status`
— the exact strings can shift slightly between election cycles. `config.json`
ships with best-guess defaults based on recent (2024–2025) files. **Once
NCSBE publishes the first absentee file for the 2026 general** (they said
data doesn't appear until ballots start going out — typically ~September),
run:

```bash
pip install -r scripts/requirements.txt
python scripts/build_data.py --inspect
```

This writes `docs/data/status_breakdown_debug.json` with every raw status
value actually in use and its count. Compare that against the lists in
`config.json` (`curable_statuses`, `cured_statuses`, `sdr_failed_verification_statuses`,
etc.) and adjust them to match exactly. This is the single most important
step for making sure "% curable" means what you think it means — do this
with input from whoever on the team is closest to the cure process, since
that's the number partners will care about most.

## Decisions baked in from the team discussion

- **Public, not password-protected.** Per the team's lean, and to avoid the
  complexity (and confidentiality issues) of merging in hotline data.
- **Kept separate from the daily partner email digest.** This dashboard is a
  *complement* to that email, not a replacement — if/when you want it to
  replace or supplement the digest, tell partners explicitly so they don't
  lose the "shows up in my inbox automatically" convenience they value. This
  repo doesn't send anything; that's a separate step for the team.
- **Focus on actionable data** (curable %, SDR status) rather than just
  horse-race turnout numbers, to differentiate from sites like the state's
  own dashboard or other trackers.
- Not yet wired to solicit feedback from the CBOE Working Group on which
  figures matter most — the KPI set above is a starting point, easy to
  reorder or trim in `docs/index.html` once you hear back from them.

## Adjusting for a different election

Change `election_date` in `config.json` (format `YYYY-MM-DD`) — everything
else derives from that.

## Local preview

```bash
cd docs
python -m http.server 8000
# open http://localhost:8000
```
