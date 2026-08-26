# NC Election Dashboard — 2026 General Election

A public, auto-updating dashboard of actionable NC absentee/provisional data — built to answer "what's still fixable?" rather than just showing final counts. Pulls directly from the [NC State Board of Elections public data files](https://www.ncsbe.gov/results-data/absentee-and-provisional-data) every day via GitHub Actions, and is hosted for free on GitHub Pages.

**Public, no login required.**

---

## What it shows

- % curable mail ballots
- SDR (same-day registration) failed verification
- SDR cured
- Absentee cured
- Provisional ballot outcomes (approved / not counted / partial) and top rejection reasons
- Demographics (race, gender, party) of returned absentee ballots
- County-level breakdown
- Daily trend lines once a few days of history accumulate

Everything is an aggregate count. The pipeline drops voter name/address/phone columns immediately after downloading the raw NCSBE file — no individual voter record is ever written to this repo or displayed on the page.

---

## How it works

scripts/build_data.py -> downloads + aggregates NCSBE files, writes docs/data/*.json
.github/workflows/... -> runs the script daily, commits the data, deploys docs/ to Pages
docs/index.html -> the dashboard itself, reads docs/data/latest.json + trend.json


---

## One-time setup

1. Create a new GitHub repo and push this folder's contents to it.
2. In the repo settings → **Pages**, set the source to **GitHub Actions**.
3. In repo settings → **Actions → General → Workflow permissions**, choose **"Read and write permissions"** (the workflow needs to commit the daily data update).
4. Push to `main` — the first workflow run will publish the site. After that it runs automatically every day (see the cron schedule in `.github/workflows/update-data.yml`); you can also trigger it manually from the Actions tab any time (e.g. the morning after Election Day).

---

## ⚠️ Before it goes live: calibrate the status categories

NCSBE doesn't publish a fixed list of status codes — the exact strings can shift slightly between election cycles. `config.json` ships with values confirmed against real 2024 general election data. Once NCSBE publishes the first absentee file for the 2026 general (typically ~September, when ballots start going out), run:

```bash
pip install -r scripts/requirements.txt
python scripts/build_data.py --inspect
```

This writes `docs/data/status_breakdown_debug.json` with every raw status value actually in use and its count. Compare that against the lists in `config.json` and adjust to match exactly.

**Important notes from the 2024 calibration:**

- **SDR failed verification** lives in `ballot_rtn_status` (as `"SDR-FAILED VERIFICATION"`), not in `mail_veri_status`. Those are two different fields tracking different things — `mail_veri_status` tracks voter record verification stages and is not used for the SDR KPI.
- **`mail_veri_status`** values (NEW VOTER, 1ST VFY, 2ND VFY, DENIED, etc.) are logged separately in the debug file for reference but are not currently used in any dashboard calculation.
- **The provisional file** is encoded in UTF-16, not UTF-8. The pipeline auto-detects this, so it works without any manual intervention — but if you try to open the raw file in a text editor and it looks like gibberish, that's why.
- The single most important calibration check is `curable_statuses` — do this with input from whoever on the team is closest to the cure process, since % curable is the number partners will care about most.

---

## Adjusting for a different election

Change `election_date` in `config.json` (format `YYYY-MM-DD`) — everything else derives from that. When switching elections, also:

- Clear out `docs/data/history/` (delete all dated `.json` files, keep `.gitkeep`) so the trend chart starts fresh
- Re-enable the cron schedule in `.github/workflows/update-data.yml` if it was paused
- Run the `--inspect` flag once to recalibrate status codes for the new cycle

See `OPERATIONS.md` for the full go-live checklist.

---

## Branding

Styled to SCSJ's 2026 brand guidelines. Colors are pulled exactly from the palette page. Typography uses Anton (stand-in for VTC Martin) and Source Serif 4 (stand-in for ABC Marist) — both are free web fonts substituting for SCSJ's licensed typefaces. If licensed `.woff2` files for VTC Martin and ABC Marist become available for web use, swap them in via the `@font-face` declaration and `font-family` references in `docs/index.html`. Logo lives at `docs/assets/scsj-wordmark-black.png`.

---

## How this was built

This dashboard was built with the assistance of Claude (Anthropic's AI assistant). Claude was used as a technical collaborator throughout the build — not as a push-button solution, but as a tool that required direction, review, and iteration.

Claude wrote the initial pipeline, dashboard, and automation workflow from a plain-language description of what the tool needed to do. Real bugs were then found by testing against actual NCSBE data — including a UTF-16 encoding issue in the provisional file, a status-code mismatch that was silently undercounting 70,907 spoiled early-voting ballots, and a memory problem that crashed the pipeline on large general-election files. Each was diagnosed by sending Claude the actual error log or raw data output, getting a fix, and verifying the result against official NCSBE figures.

Claude can also help diagnose future bugs. If something breaks, paste the error text from the "Run data pipeline" step in the Actions tab into a new Claude session along with a brief description of what you were trying to do. See `OPERATIONS.md` for more detail on how to do this effectively.

---

## Local preview

```bash
cd docs
python -m http.server 8000
# open http://localhost:8000
```
