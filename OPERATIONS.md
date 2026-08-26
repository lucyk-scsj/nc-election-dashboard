# NC Election Dashboard — Summary & Operations Cheat Sheet

**Live site:** https://lucyk-scsj.github.io/nc-election-dashboard/  
**Repo:** https://github.com/lucyk-scsj/nc-election-dashboard  

---

## What this is

A public, no-login dashboard tracking actionable NC election data — curable mail ballots, same-day-registration status, provisional ballot outcomes, and demographics — for the 2026 NC General Election. It updates itself automatically once a day by pulling directly from NC State Board of Elections' public data files. No one needs to manually download or upload anything once it's running.

It was built to answer "what's still fixable?" rather than just show final counts — differentiating it from the state's own turnout dashboard, per the team's original goal of focusing on actionable data.

---

## How it works (high level)

- `scripts/build_data.py` — downloads and aggregates NCSBE files, writes to `docs/data/*.json`
- `.github/workflows/...` — runs the script daily, commits the data, deploys `docs/` to Pages
- `docs/index.html` — the dashboard itself, reads `docs/data/latest.json` and `trend.json`

1. A scheduled job (GitHub Actions) runs once a day automatically.
2. It downloads two files directly from NCSBE's public server (dl.ncsbe.gov): the statewide absentee/early-voting file, and the statewide provisional ballot file.
3. It aggregates them into summary statistics (curable %, SDR status, provisional outcomes, demographics, county breakdowns) — only aggregate counts are kept; no individual voter names/addresses are ever stored.
4. It writes the results as JSON files into `docs/data/`.
5. The dashboard (`docs/index.html`) reads those JSON files and renders the charts/tables. It's redeployed to the live site automatically after every data update.

Nothing about this requires anyone to log in, run a script by hand, or manually update anything — as long as `config.json` is pointed at the correct, real election date (see cheat sheet below).

---

## Cheat sheet: things you'll actually need to do

### Change which election it's tracking

Edit `config.json` in the repo (pencil icon on GitHub, or via GitHub Desktop):

```json
"election_date": "2026-11-03",
"election_label": "2026 General Election"
```

Change the date (format `YYYY-MM-DD`) and label, commit, then manually trigger a run (next section) so it takes effect immediately instead of waiting for the next scheduled run.

> ⚠️ **Important:** as of this writing, `config.json` may still be set to a past election used for testing (e.g. `2024-11-05`). Double-check this is set to `2026-11-03` before/during the real election so the live public page shows real data, not an old test election.

### Manually trigger a data refresh

Don't wait for the daily schedule — do this any time you want fresh data immediately (e.g., the morning after Election Day):

1. Go to the **Actions** tab on GitHub
2. Click **"Update election data"** in the left sidebar
3. Click **"Run workflow"** → confirm
4. Wait ~30-90 seconds (longer for big files, like a full general election)
5. Refresh the live site once it shows a green checkmark

### Check if something's wrong

1. **Actions** tab → click the most recent run
2. If it shows a red ❌, click into the **"Run data pipeline"** step
3. Read the error message at the bottom — it'll either be a clear Python error (something broke, needs a code fix) or a network/infra hiccup (usually fixable by just re-running)

### Getting help from Claude when something breaks

If the error message isn't clear or the fix isn't obvious, Claude (at claude.ai) can help diagnose most issues with this dashboard. The key is giving it enough context:

1. Screenshot or paste the full error text from the **"Run data pipeline"** step — not just the summary, the actual red error lines
2. Briefly describe what you were trying to do when it broke
3. Paste in the relevant section of this cheat sheet so Claude knows what it's working with

A good starting message looks like:

> "I'm working on a GitHub Pages election dashboard. The GitHub Action that updates data is failing with this error: [paste error]. What's wrong and how do I fix it?"

Claude can read Python error messages and GitHub Actions logs and tell you what went wrong and how to fix it. You'll still need to apply the fix yourself — Claude can't access the repo directly — but it can walk you through each step.

### Edit the dashboard's look, text, or KPIs

Everything visual lives in `docs/index.html` — a single self-contained file (HTML/CSS/JS, no build step). Edit directly on GitHub or via GitHub Desktop, commit, then trigger a run (or just wait for the next daily one) to redeploy.

---

## How to make any code/file change (the reliable way)

GitHub Desktop is the most reliable method — more so than editing files directly in the GitHub website, which repeatedly caused stray-character/corruption issues (especially on `config.json`).

1. Open **GitHub Desktop** (already set up, pointed at this repo locally on your machine)
2. Find the local repo folder: **Repository → Show in Finder/Explorer**
3. Edit the file(s) there directly (with a plain text editor — avoid TextEdit's "rich text" mode on Mac, which can introduce hidden corruption)
4. Back in GitHub Desktop: it auto-detects changes → write a commit summary → **Commit to main** → **Push origin**
5. If push fails with "stale info" / rejected: click **Fetch origin** then **Pull origin** first, then push again
6. Go to **Actions** tab → **Run workflow** to see the change live immediately (for data-affecting changes) or just wait for the next scheduled run (for dashboard/visual changes)

---

## Example: fixing a bug using Claude

This is a real example of how a bug was found and fixed during the build of this dashboard.

**What happened:** The GitHub Action ran successfully but the Provisional Ballot Outcomes chart showed "0 of 130,462 cast" — clearly wrong, since NCSBE's official count for 2024 was ~65,000. The number 130,462 was also suspicious — almost exactly double the real figure.

**Step 1 — Find the actual error**

Actions tab → most recent run → "Run data pipeline" → found two log lines:
```
[build_data] provisional file header columns: ['ЁЁc', 'unnamed: 1', 'unnamed: 2'...]
[build_data] provisional columns matched: []
```


The column names were garbled — clearly something was wrong with how the file was being read.

**Step 2 — Ask Claude**

Screenshot of those log lines, plus this message:

> "The provisional file is showing garbled column names and matching zero columns. Here are the log lines: [pasted the two lines above]. What's wrong?"

Claude's diagnosis: NCSBE's provisional file is encoded in UTF-16, not UTF-8. The code only knew how to handle UTF-8, so every character came out as garbage. The double row count (130,462 vs. ~65,000) was a side effect of the garbled text being parsed as twice as many rows.

**Step 3 — Claude wrote the fix**

Claude updated `scripts/build_data.py` to automatically try multiple encodings (UTF-8, UTF-16, UTF-16-LE, etc.) and pick whichever one produces recognizable column names. It packaged the updated file as a zip download.

**Step 4 — Apply the fix via GitHub Desktop**

1. Downloaded and unzipped the file Claude provided
2. Found the local repo folder: GitHub Desktop → Repository → Show in Finder
3. Copied the new `build_data.py` into the `scripts/` folder, replacing the old one
4. Switched to GitHub Desktop — it showed `build_data.py` as modified
5. Typed a commit summary: "Fix UTF-16 encoding for provisional file"
6. Clicked **Commit to main**
7. Clicked **Push origin**
8. Went to GitHub → Actions tab → clicked **Run workflow**

**Step 5 — Verify the fix worked**

Waited for the green checkmark → refreshed the live site → Provisional Ballot Outcomes now showed real numbers: 65,230 total, 20,532 approved (31.5%). Cross-checked against NCSBE's official published total of 65,013 — within 0.3%, confirmed correct.

---

## What's been validated

We tested the pipeline against two real past NC elections to confirm accuracy before relying on it for 2026:

| Check | Dashboard shows | Official NCSBE figure | Match |
|---|---|---|---|
| Provisional ballots cast (2024 general) | 65,230 | 65,013 | within 0.3% |
| SDR failed verification (2024 general) | 1,055 | 1,055 | exact |
| Curable / cured absentee ballots (2024 general) | matched real breakdown | — | exact |
| Total absentee/early-voting returned (2024 general) | 4,700,602 | 4,521,953 accepted | expected gap* |

*\* Our total includes ballots later rejected/spoiled, not just accepted ones — so a gap vs. an "accepted only" figure is expected, not an error.*

Along the way we found and fixed several real bugs, all now resolved in the code:

- **A file-encoding issue** — NCSBE's provisional file turned out to be UTF-16, not UTF-8 (now auto-detected)
- **A status-code mismatch** — NCSBE's exact wording for "spoiled early voting" and "SDR failed verification" didn't match our first guess (now corrected using real confirmed values)
- **A memory issue with large files** — the 2024 general-election file is much bigger than a small municipal test file (the pipeline now streams downloads instead of loading everything into memory at once)

---

## Where things live (file map)

| File | Purpose |
|---|---|
| `config.json` | Election date + status-code calibration |
| `scripts/build_data.py` | The data pipeline (fetches/processes NCSBE files) |
| `.github/workflows/update-data.yml` | The daily automation (schedule + steps) |
| `docs/index.html` | The dashboard itself (all HTML/CSS/JS) |
| `docs/assets/` | Logo files |
| `docs/data/latest.json` | Most recent data snapshot (what the dashboard reads) |
| `docs/data/history/` | One snapshot per day, for the trend chart |
| `README.md` | Setup instructions for a new team member |
