#!/usr/bin/env python3
"""
NC Election Dashboard — data pipeline v2
Downloads and aggregates three NCSBE files:
  1. absentee_{date}.zip          — ballot-level file, split into MAIL and EARLY VOTING
  2. absentee_demo_stats_{date}.csv — requested ballot demographics by week/county
  3. provisional_{date}.txt        — provisional ballot outcomes

Only aggregate counts are written to docs/data/.
No individual voter names, addresses, or registration numbers
are ever stored in this repo or written to any output file.
"""
import argparse
import io
import json
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import pandas as pd

ROOT       = Path(__file__).resolve().parent.parent
CONFIG     = ROOT / "config.json"
DATA_DIR   = ROOT / "docs" / "data"
HIST_DIR   = DATA_DIR / "history"
USER_AGENT = "Mozilla/5.0 (compatible; nc-election-dashboard/2.0)"

# ── status-code sets (confirmed against real 2024 data) ─────────────────────
ACCEPTED   = ["ACCEPTED", "ACCEPTED - CURED", "ACCEPTED - EXCEPTION"]
CURABLE    = ["PENDING", "PENDING CURE", "WITNESS INFO INCOMPLETE",
              "SIGNATURE MISSING", "AFFIDAVIT INCOMPLETE",
              "ASSISTANT INFO INCOMPLETE", "PHOTO ID CURABLE"]
CURED      = ["ACCEPTED - CURED", "CURED"]
REJECTED   = ["SPOILED", "SPOILED-EV", "RETURNED UNDELIVERABLE", "REJECTED"]
SDR_FAILED = ["SDR-FAILED VERIFICATION"]

# columns we actually use — everything else (PII) is dropped at read time
ABSENTEE_COLS = [
    "county_desc", "race", "ethnicity", "gender", "age",
    "voter_party_code", "ballot_req_type", "ballot_req_dt",
    "ballot_send_dt", "ballot_rtn_dt", "ballot_rtn_status",
    "sdr", "mail_veri_status",
]
DEMO_STATS_COLS = [
    "county_name", "party_desc", "race_desc", "ethncity_desc",
    "gender_desc", "age_range", "request_week_num", "group_count",
]
PROVISIONAL_COLS = [
    "county_name", "pv_status", "pv_party", "pv_gender",
    "pv_ethnicity", "pv_race", "not_counted_reason",
]
CHUNK = 250_000


def load_config():
    with open(CONFIG) as f:
        return json.load(f)


def election_date_parts(cfg):
    d = datetime.strptime(cfg["election_date"], "%Y-%m-%d")
    return d.strftime("%Y_%m_%d"), d.strftime("%Y%m%d")


def download_to_file(url, dest, timeout=180):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 1024)


def pct(n, d):
    return round(100 * n / d, 2) if d else None


# ── absentee (chunked to handle general-election file sizes) ─────────────────
def fetch_absentee_df(cfg):
    us, compact = election_date_parts(cfg)
    url = (f"https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{us}"
           f"/absentee_{compact}.zip")
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "absentee.zip"
        download_to_file(url, zp)
        with zipfile.ZipFile(zp) as zf:
            member = next(n for n in zf.namelist()
                          if n.lower().endswith(".csv")
                          and "__macosx" not in n.lower())
            cp = Path(tmp) / "absentee.csv"
            with zf.open(member) as src, open(cp, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

        hdr = pd.read_csv(cp, nrows=0, encoding="utf-8",
                          encoding_errors="replace")
        hdr.columns = [c.strip().lower() for c in hdr.columns]
        cols = [c for c in ABSENTEE_COLS if c in hdr.columns]
        print(f"[build] absentee columns matched: {cols}", file=sys.stderr)

        # aggregate in chunks to keep memory low
        total = 0
        status_c    = Counter()
        # mail accumulators
        m_accepted = m_curable = m_cured = m_rejected = 0
        m_sdr_total = m_sdr_failed = m_sdr_cured = 0
        m_county    = {}   # county -> {returned, accepted, curable}
        m_race_acc  = Counter(); m_race_rej = Counter()
        m_race_cur  = Counter(); m_gender_c = Counter()
        m_party_c   = Counter()
        # early-voting accumulators
        ev_total = ev_accepted = ev_curable = ev_cured = ev_rejected = 0
        ev_sdr_total = ev_sdr_failed = ev_sdr_cured = 0
        ev_county = {}
        ev_race   = Counter(); ev_gender = Counter(); ev_party = Counter()

        reader = pd.read_csv(cp, dtype=str, low_memory=False,
                             usecols=cols, encoding="utf-8",
                             encoding_errors="replace",
                             chunksize=CHUNK)
        for chunk in reader:
            chunk.columns = [c.strip().lower() for c in chunk.columns]
            for c in chunk.columns:
                chunk[c] = chunk[c].astype(str).str.strip().str.upper()

            total += len(chunk)
            status_c.update(chunk["ballot_rtn_status"].value_counts().to_dict())

            mail = chunk[chunk["ballot_req_type"] == "MAIL"]
            ev   = chunk[chunk["ballot_req_type"] == "EARLY VOTING"]

            def _agg(df, acc_a, acc_cu, acc_cu2, acc_r,
                     sdr_t, sdr_f, sdr_cu, county_d,
                     race_acc, race_rej, race_cur, gender_c, party_c):
                n = len(df)
                a  = int(df["ballot_rtn_status"].isin(ACCEPTED).sum())
                cu = int(df["ballot_rtn_status"].isin(CURABLE).sum())
                cu2= int(df["ballot_rtn_status"].isin(CURED).sum())
                r  = int(df["ballot_rtn_status"].isin(REJECTED).sum())
                sm = df["sdr"].eq("Y") if "sdr" in df else pd.Series([False]*n)
                sf = int((sm & df["ballot_rtn_status"].isin(SDR_FAILED)).sum())
                sc = int((sm & df["ballot_rtn_status"].isin(CURED)).sum())

                if "county_desc" in df:
                    g = df.groupby("county_desc").agg(
                        returned=("ballot_rtn_status","count"),
                        accepted=("ballot_rtn_status",
                                  lambda s: s.isin(ACCEPTED).sum()),
                        curable =("ballot_rtn_status",
                                  lambda s: s.isin(CURABLE).sum()),
                    )
                    for county, row in g.iterrows():
                        acc = county_d.setdefault(county,
                              {"returned":0,"accepted":0,"curable":0})
                        acc["returned"] += int(row["returned"])
                        acc["accepted"] += int(row["accepted"])
                        acc["curable"]  += int(row["curable"])

                if "race" in df:
                    accepted_mask = df["ballot_rtn_status"].isin(ACCEPTED)
                    cured_mask    = df["ballot_rtn_status"].isin(CURED)
                    race_acc.update(df.loc[accepted_mask, "race"]
                                    .value_counts().to_dict())
                    race_rej.update(df.loc[~accepted_mask & ~cured_mask,
                                           "race"].value_counts().to_dict())
                    race_cur.update(df.loc[cured_mask, "race"]
                                    .value_counts().to_dict())
                if "gender" in df:
                    gender_c.update(df["gender"].value_counts().to_dict())
                if "voter_party_code" in df:
                    party_c.update(df["voter_party_code"]
                                   .value_counts().to_dict())

                return n, a, cu, cu2, r, int(sm.sum()), sf, sc

            # mail
            mn, ma, mcu, mcu2, mr, mst, msf, msc = _agg(
                mail, m_accepted, m_curable, m_cured, m_rejected,
                m_sdr_total, m_sdr_failed, m_sdr_cured, m_county,
                m_race_acc, m_race_rej, m_race_cur, m_gender_c, m_party_c)
            m_accepted  += ma;  m_curable  += mcu; m_cured    += mcu2
            m_rejected  += mr;  m_sdr_total+= mst; m_sdr_failed+=msf
            m_sdr_cured += msc

            # early voting
            en, ea, ecu, ecu2, er, est, esf, esc = _agg(
                ev, ev_accepted, ev_curable, ev_cured, ev_rejected,
                ev_sdr_total, ev_sdr_failed, ev_sdr_cured, ev_county,
                ev_race, Counter(), Counter(), ev_gender, ev_party)
            ev_total    += en;  ev_accepted += ea;  ev_curable += ecu
            ev_cured    += ecu2;ev_rejected += er;  ev_sdr_total+=est
            ev_sdr_failed+=esf; ev_sdr_cured+=esc

    mail_returned = sum(v["returned"] for k, v in m_county.items() if k and k.upper() != "NAN")
    ev_returned   = sum(v["returned"] for k, v in ev_county.items() if k and k.upper() != "NAN")

    return {
        "mail": {
            "total_returned": mail_returned,
            "accepted": m_accepted, "curable": m_curable,
            "cured": m_cured, "rejected_or_spoiled": m_rejected,
            "pct_curable_of_returned": pct(m_curable, mail_returned),
            "sdr": {"total_sdr_ballots": m_sdr_total,
                    "failed_verification": m_sdr_failed,
                    "cured": m_sdr_cured,
                    "pct_failed_of_sdr": pct(m_sdr_failed, m_sdr_total)},
            "by_county": [{"county_desc": k, **v}
                          for k, v in m_county.items()
                          if k and k.upper() != "NAN"],
            "demographics": {
                "race_accepted": dict(m_race_acc),
                "race_not_accepted": dict(m_race_rej),
                "race_cured": dict(m_race_cur),
                "gender": dict(m_gender_c),
                "party": dict(m_party_c),
            },
            "raw_status_counts": dict(status_c),
        },
        "early_voting": {
            "total_returned": ev_returned,
            "accepted": ev_accepted, "curable": ev_curable,
            "cured": ev_cured, "rejected_or_spoiled": ev_rejected,
            "pct_curable_of_returned": pct(ev_curable, ev_returned),
            "sdr": {"total_sdr_ballots": ev_sdr_total,
                    "failed_verification": ev_sdr_failed,
                    "cured": ev_sdr_cured,
                    "pct_failed_of_sdr": pct(ev_sdr_failed, ev_sdr_total)},
            "by_county": [{"county_desc": k, **v}
                          for k, v in ev_county.items()
                          if k and k.upper() != "NAN"],
            "demographics": {
                "race": dict(ev_race),
                "gender": dict(ev_gender),
                "party": dict(ev_party),
            },
        },
    }


# ── demo stats (requested ballots) ───────────────────────────────────────────
def fetch_demo_stats(cfg):
    us, compact = election_date_parts(cfg)
    url = (f"https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{us}"
           f"/absentee_demo_stats_{compact}.csv")
    with tempfile.TemporaryDirectory() as tmp:
        fp = Path(tmp) / "demo_stats.csv"
        download_to_file(url, fp)
        df = pd.read_csv(fp, dtype=str, low_memory=False,
                         encoding="utf-8", encoding_errors="replace")
    df.columns = [c.strip().lower() for c in df.columns]
    # strip null bytes that appear in primary_ballot_party
    for c in df.columns:
        df[c] = df[c].astype(str).str.replace("\x00", "", regex=False).str.strip()
    df["group_count"] = pd.to_numeric(df["group_count"], errors="coerce").fillna(0)

    total_requested = int(df["group_count"].sum())

    # statewide by race
    by_race = (df.groupby("race_desc")["group_count"].sum()
               .sort_values(ascending=False).to_dict())
    by_race = {k: int(v) for k, v in by_race.items()
               if k and k.upper() != "NAN"}

    # statewide by gender
    by_gender = (df.groupby("gender_desc")["group_count"].sum()
                 .sort_values(ascending=False).to_dict())
    by_gender = {k: int(v) for k, v in by_gender.items()
                 if k and k.upper() != "NAN"}

    # statewide by party
    by_party = (df.groupby("party_desc")["group_count"].sum()
                .sort_values(ascending=False).to_dict())
    by_party = {k: int(v) for k, v in by_party.items()
                if k and k.upper() != "NAN"}

    # statewide by age range
    by_age = (df.groupby("age_range")["group_count"].sum()
              .to_dict())
    by_age = {k: int(v) for k, v in by_age.items()
              if k and k.upper() != "NAN"}

    # weekly trend (statewide)
    by_week = (df.groupby("request_week_num")["group_count"].sum()
               .sort_index().to_dict())
    by_week = {str(k): int(v) for k, v in by_week.items()}

    # county totals (for CSV download)
    by_county = (df.groupby("county_name")["group_count"].sum()
                 .sort_values(ascending=False).to_dict())
    by_county = [{"county_name": k, "requested": int(v)}
                 for k, v in by_county.items()
                 if k and k.upper() != "NAN"]

    return {
        "total_requested": total_requested,
        "by_race": by_race,
        "by_gender": by_gender,
        "by_party": by_party,
        "by_age": by_age,
        "by_week": by_week,
        "by_county": by_county,
    }


# ── provisional ──────────────────────────────────────────────────────────────
def detect_encoding(path, keywords, sep="\t"):
    for enc in ["utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252"]:
        try:
            hdr = pd.read_csv(path, sep=sep, nrows=0, encoding=enc)
            cols = [c.strip().lower() for c in hdr.columns]
            if any(any(kw in c for kw in keywords) for c in cols):
                return enc, cols
        except (UnicodeError, pd.errors.ParserError):
            continue
    hdr = pd.read_csv(path, sep=sep, nrows=0, encoding="utf-8-sig",
                      encoding_errors="replace")
    return "utf-8-sig", [c.strip().lower() for c in hdr.columns]


def fetch_provisional_df(cfg):
    us, compact = election_date_parts(cfg)
    url = (f"https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{us}"
           f"/provisional_{compact}.txt")
    with tempfile.TemporaryDirectory() as tmp:
        fp = Path(tmp) / "provisional.txt"
        download_to_file(url, fp)
        enc, cols = detect_encoding(
            fp, ["county", "pv_status", "pv_party", "status"])
        print(f"[build] provisional encoding={enc} cols={cols}",
              file=sys.stderr)
        cols_present = [c for c in PROVISIONAL_COLS if c in cols]
        if not cols_present:
            print("[build] WARNING: no provisional columns matched — "
                  "reading all columns", file=sys.stderr)
            df = pd.read_csv(fp, sep="\t", dtype=str, low_memory=False,
                             encoding=enc, encoding_errors="replace")
        else:
            df = pd.read_csv(fp, sep="\t", dtype=str, low_memory=False,
                             usecols=cols_present, encoding=enc,
                             encoding_errors="replace")
    df.columns = [c.strip().lower() for c in df.columns]
    pii = ["full_name", "res_addr_street", "res_addr_csz", "phone_num",
           "voter_reg_num"]
    df = df.drop(columns=[c for c in pii if c in df.columns], errors="ignore")
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip().str.upper()
    return df


def summarize_provisional(df):
    total = len(df)
    sc    = df["pv_status"].value_counts().to_dict() if "pv_status" in df else {}
    approved   = int(sc.get("APPROVED", 0))
    not_counted= int(sc.get("NOT COUNTED", 0))
    partial    = int(sc.get("PARTIAL", 0))
    by_county  = (df.groupby("county_name").size()
                  .reset_index(name="count").to_dict(orient="records")
                  if "county_name" in df else [])
    reasons    = (df["not_counted_reason"].value_counts().to_dict()
                  if "not_counted_reason" in df else {})
    demo = {
        "race":   df["pv_race"].value_counts().to_dict()   if "pv_race"   in df else {},
        "gender": df["pv_gender"].value_counts().to_dict() if "pv_gender" in df else {},
        "party":  df["pv_party"].value_counts().to_dict()  if "pv_party"  in df else {},
    }
    return {
        "total": int(total), "approved": approved,
        "not_counted": not_counted, "partial": partial,
        "pct_approved": pct(approved, total),
        "by_county": by_county,
        "not_counted_reasons": reasons,
        "demographics": demo,
        "raw_status_counts": sc,
    }


# ── trend rebuild ─────────────────────────────────────────────────────────────
def rebuild_trend():
    trend = []
    for p in sorted(HIST_DIR.glob("*.json")):
        try:
            snap = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if snap.get("status") == "no_data_yet":
            continue
        mail = (snap.get("absentee_mail") or
                snap.get("absentee") or {})    # back-compat with v1 snapshots
        ev   = snap.get("absentee_early_voting") or {}
        prov = snap.get("provisional") or {}
        trend.append({
            "date": p.stem,
            # mail
            "mail_curable":      mail.get("curable"),
            "mail_cured":        mail.get("cured"),
            "mail_pct_curable":  mail.get("pct_curable_of_returned"),
            "mail_sdr_failed":   (mail.get("sdr") or {}).get("failed_verification"),
            # early voting
            "ev_curable":        ev.get("curable"),
            "ev_cured":          ev.get("cured"),
            "ev_sdr_failed":     (ev.get("sdr") or {}).get("failed_verification"),
            # provisional
            "prov_total":        prov.get("total"),
            "prov_approved":     prov.get("approved"),
        })
    (DATA_DIR / "trend.json").write_text(json.dumps(trend, indent=2))
    return trend


# ── placeholder ───────────────────────────────────────────────────────────────
def placeholder(cfg, note):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "election_date": cfg["election_date"],
        "election_label": cfg["election_label"],
        "status": "no_data_yet", "note": note,
    }


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HIST_DIR.mkdir(parents=True, exist_ok=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "election_date": cfg["election_date"],
        "election_label": cfg["election_label"],
        "status": "ok",
    }

    # absentee (mail + early voting split)
    try:
        ab = fetch_absentee_df(cfg)
        result["absentee_mail"]          = ab["mail"]
        result["absentee_early_voting"]  = ab["early_voting"]
    except (HTTPError, URLError, StopIteration) as e:
        print(f"[build] absentee not available: {e}", file=sys.stderr)
        result["absentee_mail"] = result["absentee_early_voting"] = None
        result["status"] = "partial"

    # demo stats (requested ballots)
    try:
        result["requested_ballots"] = fetch_demo_stats(cfg)
    except (HTTPError, URLError) as e:
        print(f"[build] demo_stats not available: {e}", file=sys.stderr)
        result["requested_ballots"] = None
        result["status"] = "partial"

    # provisional
    try:
        prov_df = fetch_provisional_df(cfg)
        result["provisional"] = summarize_provisional(prov_df)
    except (HTTPError, URLError, StopIteration) as e:
        print(f"[build] provisional not available: {e}", file=sys.stderr)
        result["provisional"] = None
        if result["status"] == "ok":
            result["status"] = "partial"

    if all(result.get(k) is None
           for k in ["absentee_mail","absentee_early_voting","provisional"]):
        result = placeholder(cfg,
            "NCSBE has not yet published files for this election date.")

    (DATA_DIR / "latest.json").write_text(json.dumps(result, indent=2))

    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (HIST_DIR / f"{cfg['election_date']}.json").write_text(
        json.dumps(result, indent=2))

    rebuild_trend()

    if args.inspect:
        debug = {
            "absentee_mail_status_values":
                (result.get("absentee_mail") or {}).get("raw_status_counts"),
            "provisional_status_values":
                (result.get("provisional") or {}).get("raw_status_counts"),
        }
        (DATA_DIR / "status_breakdown_debug.json").write_text(
            json.dumps(debug, indent=2))
        print("[build] wrote status_breakdown_debug.json")

    print(f"[build] done — status={result['status']}")


if __name__ == "__main__":
    main()
