#!/usr/bin/env python3
"""
Pulls NC State Board of Elections absentee + provisional files for the
configured election, aggregates them into summary statistics, and writes
JSON that the dashboard (docs/index.html) reads.

IMPORTANT: the raw NCSBE files contain PII (voter name, address, phone).
This script drops those columns immediately after loading. Only aggregate
counts are ever written to docs/data/ -- nothing in this repo should ever
contain an individual voter's name or address.

Usage:
    python scripts/build_data.py              # normal daily run
    python scripts/build_data.py --inspect     # also dump raw status-value
                                                # counts for calibration
"""
import argparse
import io
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "docs" / "data"
HISTORY_DIR = DATA_DIR / "history"

USER_AGENT = "Mozilla/5.0 (compatible; nc-election-dashboard/1.0)"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def election_date_parts(cfg):
    d = datetime.strptime(cfg["election_date"], "%Y-%m-%d")
    return d.strftime("%Y_%m_%d"), d.strftime("%Y%m%d")


def download_to_file(url, dest_path, timeout=180):
    """Stream a URL to disk in chunks rather than loading it all into
    memory at once -- needed for the large general-election files."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)


def fetch_bytes(url, timeout=180):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_absentee_df(cfg):
    """Statewide absentee file: one row per absentee/early-voting ballot.
    Streams to disk and only loads the columns we need, since this file can
    be huge in a general election (every early-voting ballot statewide) --
    loading it fully into memory (raw bytes + decoded text + dataframe all
    at once) is what was causing out-of-memory kills on large files."""
    us_date, compact_date = election_date_parts(cfg)
    url = f"https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{us_date}/absentee_{compact_date}.zip"
    keep_cols = [
        "county_desc", "race", "ethnicity", "gender", "age",
        "voter_party_code", "ballot_req_type", "ballot_req_dt",
        "ballot_send_dt", "ballot_rtn_dt", "ballot_rtn_status",
        "sdr", "mail_veri_status",
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "absentee.zip"
        download_to_file(url, zip_path)

        with zipfile.ZipFile(zip_path) as zf:
            member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            csv_path = Path(tmpdir) / "absentee.csv"
            # stream the member out to disk rather than zf.read() (which
            # would load the whole thing into memory at once)
            with zf.open(member) as src, open(csv_path, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

        # peek at the header to know which of our wanted columns actually
        # exist in this file, so usecols doesn't error on a missing one
        header = pd.read_csv(csv_path, nrows=0, encoding="utf-8",
                              encoding_errors="replace")
        header.columns = [c.strip().lower() for c in header.columns]
        cols_present = [c for c in keep_cols if c in header.columns]
        print(f"[build_data] absentee file header columns: {list(header.columns)}")
        print(f"[build_data] absentee columns matched: {cols_present}")

        if not cols_present:
            # none of our expected column names matched -- rather than
            # silently reading zero rows (a real pandas gotcha with an
            # empty usecols list), fall back to reading everything so we
            # don't lose data, and make the mismatch loud in the logs
            print("[build_data] WARNING: no expected absentee columns matched "
                  "the file header -- reading all columns as a fallback. "
                  "Check the header columns printed above against config.json.")
            df = pd.read_csv(csv_path, dtype=str, low_memory=False,
                              encoding="utf-8", encoding_errors="replace")
        else:
            # encoding_errors="replace" handles the stray non-UTF-8 bytes NCSBE
            # files sometimes have (e.g. curly quotes in name fields) without
            # needing a second full-file decode pass
            df = pd.read_csv(
                csv_path, dtype=str, low_memory=False,
                usecols=cols_present, encoding="utf-8", encoding_errors="replace",
            )

    df.columns = [c.strip().lower() for c in df.columns]
    # safety net: never keep known PII-bearing columns, even on the
    # fallback path where we read everything
    pii_cols = ["voter_last_name", "voter_first_name", "voter_full_name",
                "full_name", "res_addr_street", "mail_addr_street",
                "phone_num", "voter_reg_num", "ncid"]
    df = df.drop(columns=[c for c in pii_cols if c in df.columns], errors="ignore")
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip().str.upper()
    return df


def fetch_provisional_df(cfg):
    us_date, compact_date = election_date_parts(cfg)
    url = f"https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{us_date}/provisional_{compact_date}.txt"
    keep_cols = [
        "county_name", "pv_status", "pv_party", "pv_gender",
        "pv_ethnicity", "pv_race", "not_counted_reason",
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        txt_path = Path(tmpdir) / "provisional.txt"
        download_to_file(url, txt_path)

        header = pd.read_csv(txt_path, sep="\t", nrows=0,
                              encoding="utf-8-sig", encoding_errors="replace")
        header.columns = [c.strip().lower() for c in header.columns]
        cols_present = [c for c in keep_cols if c in header.columns]
        print(f"[build_data] provisional file header columns: {list(header.columns)}")
        print(f"[build_data] provisional columns matched: {cols_present}")

        if not cols_present:
            print("[build_data] WARNING: no expected provisional columns matched "
                  "the file header -- reading all columns as a fallback. "
                  "Check the header columns printed above against config.json.")
            df = pd.read_csv(txt_path, sep="\t", dtype=str, low_memory=False,
                              encoding="utf-8-sig", encoding_errors="replace")
        else:
            df = pd.read_csv(
                txt_path, sep="\t", dtype=str, low_memory=False,
                usecols=cols_present, encoding="utf-8-sig", encoding_errors="replace",
            )
    df.columns = [c.strip().lower() for c in df.columns]
    pii_cols = ["full_name", "res_addr_street", "res_addr_csz", "phone_num",
                "voter_reg_num", "reasonable_impediment_reason"]
    df = df.drop(columns=[c for c in pii_cols if c in df.columns], errors="ignore")
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip().str.upper()
    return df


def pct(n, d):
    return round(100 * n / d, 2) if d else None


def summarize_absentee(df, cfg):
    total = len(df)
    status_counts = df["ballot_rtn_status"].value_counts().to_dict() if "ballot_rtn_status" in df else {}

    accepted = df["ballot_rtn_status"].isin([s.upper() for s in cfg["accepted_statuses"]]).sum()
    curable = df["ballot_rtn_status"].isin([s.upper() for s in cfg["curable_statuses"]]).sum()
    cured = df["ballot_rtn_status"].isin([s.upper() for s in cfg["cured_statuses"]]).sum()
    rejected = df["ballot_rtn_status"].isin([s.upper() for s in cfg["spoiled_or_rejected_statuses"]]).sum()

    sdr_mask = df["sdr"].eq("Y") if "sdr" in df else pd.Series([False] * len(df))
    sdr_total = int(sdr_mask.sum())
    sdr_failed = int((sdr_mask & df["ballot_rtn_status"].isin(
        [s.upper() for s in cfg.get("sdr_failed_verification_ballot_statuses", [])])).sum())
    sdr_cured = int((sdr_mask & df["ballot_rtn_status"].isin(
        [s.upper() for s in cfg["cured_statuses"]])).sum())

    by_county = (
        df.groupby("county_desc").agg(
            returned=("ballot_rtn_status", "count"),
            accepted=("ballot_rtn_status", lambda s: s.isin([x.upper() for x in cfg["accepted_statuses"]]).sum()),
            curable=("ballot_rtn_status", lambda s: s.isin([x.upper() for x in cfg["curable_statuses"]]).sum()),
        ).reset_index().to_dict(orient="records")
        if "county_desc" in df else []
    )

    demographics = {
        "race": df["race"].value_counts().to_dict() if "race" in df else {},
        "gender": df["gender"].value_counts().to_dict() if "gender" in df else {},
        "party": df["voter_party_code"].value_counts().to_dict() if "voter_party_code" in df else {},
        "ballot_type": df["ballot_req_type"].value_counts().to_dict() if "ballot_req_type" in df else {},
    }

    return {
        "total_returned": int(total),
        "accepted": int(accepted),
        "curable": int(curable),
        "cured": int(cured),
        "rejected_or_spoiled": int(rejected),
        "pct_curable_of_returned": pct(curable, total),
        "sdr": {
            "total_sdr_ballots": sdr_total,
            "failed_verification": sdr_failed,
            "cured": sdr_cured,
            "pct_failed_of_sdr": pct(sdr_failed, sdr_total),
        },
        "by_county": by_county,
        "demographics": demographics,
        "raw_status_counts": status_counts,
        "raw_mail_veri_status_counts": (
            df["mail_veri_status"].value_counts().to_dict()
            if "mail_veri_status" in df else {}
        ),
    }


def summarize_provisional(df):
    total = len(df)
    status_counts = df["pv_status"].value_counts().to_dict() if "pv_status" in df else {}
    approved = int(status_counts.get("APPROVED", 0))
    not_counted = int(status_counts.get("NOT COUNTED", 0))
    partial = int(status_counts.get("PARTIAL", 0))

    by_county = (
        df.groupby("county_name").size().reset_index(name="count").to_dict(orient="records")
        if "county_name" in df else []
    )
    reasons = df["not_counted_reason"].value_counts().to_dict() if "not_counted_reason" in df else {}
    demographics = {
        "race": df["pv_race"].value_counts().to_dict() if "pv_race" in df else {},
        "gender": df["pv_gender"].value_counts().to_dict() if "pv_gender" in df else {},
        "party": df["pv_party"].value_counts().to_dict() if "pv_party" in df else {},
    }

    return {
        "total": int(total),
        "approved": approved,
        "not_counted": not_counted,
        "partial": partial,
        "pct_approved": pct(approved, total),
        "by_county": by_county,
        "not_counted_reasons": reasons,
        "demographics": demographics,
        "raw_status_counts": status_counts,
    }


def placeholder(cfg, note):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "election_date": cfg["election_date"],
        "election_label": cfg["election_label"],
        "status": "no_data_yet",
        "note": note,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true",
                         help="also write a raw status-value debug dump")
    args = parser.parse_args()

    cfg = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "election_date": cfg["election_date"],
        "election_label": cfg["election_label"],
        "status": "ok",
    }

    try:
        absentee_df = fetch_absentee_df(cfg)
        result["absentee"] = summarize_absentee(absentee_df, cfg)
    except (HTTPError, URLError, StopIteration) as e:
        print(f"[build_data] absentee file not available yet: {e}", file=sys.stderr)
        result["absentee"] = None
        result["status"] = "partial"

    try:
        provisional_df = fetch_provisional_df(cfg)
        result["provisional"] = summarize_provisional(provisional_df)
    except (HTTPError, URLError, StopIteration) as e:
        print(f"[build_data] provisional file not available yet: {e}", file=sys.stderr)
        result["provisional"] = None
        result["status"] = "partial"

    if result.get("absentee") is None and result.get("provisional") is None:
        result = placeholder(
            cfg,
            "NCSBE has not yet published absentee/provisional files for this "
            "election date. Files typically appear once ballots start going "
            "out (Stage 1) and become richer through early voting and "
            "Election Day. This page will populate automatically once data "
            "is available.",
        )

    # write latest snapshot
    (DATA_DIR / "latest.json").write_text(json.dumps(result, indent=2))

    # append to dated history (one file per day this ran, keyed by run date)
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (HISTORY_DIR / f"{run_date}.json").write_text(json.dumps(result, indent=2))

    # rebuild a lightweight trend index for the dashboard's time-series charts
    trend = []
    for p in sorted(HISTORY_DIR.glob("*.json")):
        try:
            snap = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if snap.get("status") == "no_data_yet":
            continue
        trend.append({
            "date": p.stem,
            "pct_curable": (snap.get("absentee") or {}).get("pct_curable_of_returned"),
            "curable": (snap.get("absentee") or {}).get("curable"),
            "cured": (snap.get("absentee") or {}).get("cured"),
            "sdr_failed": ((snap.get("absentee") or {}).get("sdr") or {}).get("failed_verification"),
            "provisional_total": (snap.get("provisional") or {}).get("total"),
            "provisional_approved": (snap.get("provisional") or {}).get("approved"),
        })
    (DATA_DIR / "trend.json").write_text(json.dumps(trend, indent=2))

    if args.inspect:
        debug = {
            "absentee_ballot_rtn_status_values": (result.get("absentee") or {}).get("raw_status_counts"),
            "provisional_pv_status_values": (result.get("provisional") or {}).get("raw_status_counts"),
        }
        (DATA_DIR / "status_breakdown_debug.json").write_text(json.dumps(debug, indent=2))
        print("Wrote docs/data/status_breakdown_debug.json -- compare these "
              "raw values against config.json's status lists.")

    print(f"[build_data] wrote {DATA_DIR / 'latest.json'} (status={result['status']})")


if __name__ == "__main__":
    main()
