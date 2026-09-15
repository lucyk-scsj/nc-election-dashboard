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
import json
import shutil
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
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

ACCEPTED   = ["ACCEPTED", "ACCEPTED - CURED", "ACCEPTED - EXCEPTION"]
CURABLE    = ["PENDING", "PENDING CURE", "WITNESS INFO INCOMPLETE",
              "SIGNATURE MISSING", "AFFIDAVIT INCOMPLETE",
              "ASSISTANT INFO INCOMPLETE", "PHOTO ID CURABLE"]
CURED      = ["ACCEPTED - CURED", "CURED"]
REJECTED   = ["SPOILED", "SPOILED-EV", "RETURNED UNDELIVERABLE", "REJECTED"]
SDR_FAILED = ["SDR-FAILED VERIFICATION"]

NC_COUNTIES = [
    "ALAMANCE","ALEXANDER","ALLEGHANY","ANSON","ASHE","AVERY","BEAUFORT","BERTIE",
    "BLADEN","BRUNSWICK","BUNCOMBE","BURKE","CABARRUS","CALDWELL","CAMDEN","CARTERET",
    "CASWELL","CATAWBA","CHATHAM","CHEROKEE","CHOWAN","CLAY","CLEVELAND","COLUMBUS",
    "CRAVEN","CUMBERLAND","CURRITUCK","DARE","DAVIDSON","DAVIE","DUPLIN","DURHAM",
    "EDGECOMBE","FORSYTH","FRANKLIN","GASTON","GATES","GRAHAM","GRANVILLE","GREENE",
    "GUILFORD","HALIFAX","HARNETT","HAYWOOD","HENDERSON","HERTFORD","HOKE","HYDE",
    "IREDELL","JACKSON","JOHNSTON","JONES","LEE","LENOIR","LINCOLN","MACON",
    "MADISON","MARTIN","MCDOWELL","MECKLENBURG","MITCHELL","MONTGOMERY","MOORE",
    "NASH","NEW HANOVER","NORTHAMPTON","ONSLOW","ORANGE","PAMLICO","PASQUOTANK",
    "PENDER","PERQUIMANS","PERSON","PITT","POLK","RANDOLPH","RICHMOND","ROBESON",
    "ROCKINGHAM","ROWAN","RUTHERFORD","SAMPSON","SCOTLAND","STANLY","STOKES","SURRY",
    "SWAIN","TRANSYLVANIA","TYRRELL","UNION","VANCE","WAKE","WARREN","WASHINGTON",
    "WATAUGA","WAYNE","WILKES","WILSON","YADKIN","YANCEY",
]

AGE_BUCKETS = [
    ("18-25",  18, 25),
    ("26-40",  26, 40),
    ("41-65",  41, 65),
    ("66+",    66, 999),
]

ABSENTEE_COLS = [
    "county_desc", "race", "ethnicity", "gender", "age",
    "voter_party_code", "ballot_req_type", "ballot_req_dt",
    "ballot_send_dt", "ballot_rtn_dt", "ballot_rtn_status",
    "sdr", "mail_veri_status", "site_name",
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


def age_bucket(age_series):
    """Return a Counter of age bucket labels from a numeric age Series."""
    c = Counter()
    for label, lo, hi in AGE_BUCKETS:
        c[label] = int(((age_series >= lo) & (age_series <= hi)).sum())
    return c


def race_pct_table(race_acc, race_rej, race_cur):
    """
    Build per-race percentage breakdown:
    accepted_pct + not_accepted_pct = 100% of that race's total returned.
    """
    all_races = set(race_acc) | set(race_rej) | set(race_cur)
    rows = []
    for race in sorted(all_races):
        if not race or race.upper() == "NAN":
            continue
        acc  = race_acc.get(race, 0)
        rej  = race_rej.get(race, 0)
        cur  = race_cur.get(race, 0)
        total = acc + rej + cur
        rows.append({
            "race": race,
            "total": total,
            "accepted": acc,
            "not_accepted": rej,
            "cured": cur,
            "accepted_pct": pct(acc, total),
            "not_accepted_pct": pct(rej, total),
            "cured_pct": pct(cur, total),
        })
    rows.sort(key=lambda r: -r["total"])
    return rows


def age_pct_table(age_acc, age_rej, age_cur):
    """Same structure as race_pct_table but for age buckets."""
    all_buckets = [b[0] for b in AGE_BUCKETS]
    rows = []
    for label in all_buckets:
        acc  = age_acc.get(label, 0)
        rej  = age_rej.get(label, 0)
        cur  = age_cur.get(label, 0)
        total = acc + rej + cur
        rows.append({
            "age_group": label,
            "total": total,
            "accepted": acc,
            "not_accepted": rej,
            "cured": cur,
            "accepted_pct": pct(acc, total),
            "not_accepted_pct": pct(rej, total),
            "cured_pct": pct(cur, total),
        })
    return rows


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

        hdr = pd.read_csv(cp, nrows=0, encoding="utf-8", encoding_errors="replace")
        hdr.columns = [c.strip().lower() for c in hdr.columns]
        cols = [c for c in ABSENTEE_COLS if c in hdr.columns]
        print(f"[build] absentee columns matched: {cols}", file=sys.stderr)

        # ── statewide accumulators ────────────────────────────────────────
        status_c = Counter()

        # mail
        m_county  = defaultdict(lambda: {"returned":0,"accepted":0,"curable":0,"cured":0,"rejected":0})
        m_race_acc = Counter(); m_race_rej = Counter(); m_race_cur = Counter()
        m_age_acc  = Counter(); m_age_rej  = Counter(); m_age_cur  = Counter()
        m_gender   = Counter(); m_party    = Counter(); m_ethnicity = Counter()
        m_ethn_acc = Counter(); m_ethn_rej = Counter(); m_ethn_cur = Counter()
        m_curable_cats = Counter()
        m_county_curable_cats = defaultdict(Counter)  # county -> {status: count}

        # per-county mail demographics (for dropdown)
        mc_race_acc = defaultdict(Counter); mc_race_rej = defaultdict(Counter)
        mc_age_acc  = defaultdict(Counter); mc_age_rej  = defaultdict(Counter)
        mc_ethnicity = defaultdict(Counter)

        # early voting
        ev_county = defaultdict(lambda: {"returned":0,"accepted":0,"curable":0,"cured":0,"rejected":0})
        ev_race_acc = Counter(); ev_race_rej = Counter(); ev_race_cur = Counter()
        ev_age_acc  = Counter(); ev_age_rej  = Counter(); ev_age_cur  = Counter()
        ev_gender   = Counter(); ev_party    = Counter(); ev_ethnicity = Counter()
        ev_ethn_acc = Counter(); ev_ethn_rej = Counter(); ev_ethn_cur = Counter()

        # site usage: county -> site -> {returned, race Counter, age Counter}
        ev_sites = defaultdict(lambda: defaultdict(
            lambda: {"returned": 0, "race": Counter(), "age": Counter()}))

        # per-county ev demographics
        ec_race_acc = defaultdict(Counter); ec_race_rej = defaultdict(Counter)
        ec_age_acc  = defaultdict(Counter); ec_age_rej  = defaultdict(Counter)

        reader = pd.read_csv(cp, dtype=str, low_memory=False,
                             usecols=cols, encoding="utf-8",
                             encoding_errors="replace", chunksize=CHUNK)

        for chunk in reader:
            chunk.columns = [c.strip().lower() for c in chunk.columns]
            for c in chunk.columns:
                chunk[c] = chunk[c].astype(str).str.strip().str.upper()

            status_c.update(chunk["ballot_rtn_status"].value_counts().to_dict())

            mail = chunk[chunk["ballot_req_type"] == "MAIL"]
            ev   = chunk[chunk["ballot_req_type"] == "EARLY VOTING"]

            # curable category breakdown — statewide and per county
            if "county_desc" in mail.columns:
                for _, row in mail[mail["ballot_rtn_status"].isin(CURABLE)].iterrows():
                    status = row["ballot_rtn_status"]
                    county = row.get("county_desc", "")
                    m_curable_cats[status] += 1
                    if county and county != "NAN":
                        m_county_curable_cats[county][status] += 1
            else:
                for status, cnt in mail["ballot_rtn_status"].value_counts().items():
                    if status in CURABLE:
                        m_curable_cats[status] += int(cnt)

            for df_chunk, county_d, race_a, race_r, race_c, age_a, age_r, age_c, \
                    gend, prty, ethn, cr_race_a, cr_race_r, cr_age_a, cr_age_r, \
                    site_d in [
                (mail, m_county, m_race_acc, m_race_rej, m_race_cur,
                 m_age_acc, m_age_rej, m_age_cur,
                 m_gender, m_party, m_ethnicity,
                 mc_race_acc, mc_race_rej, mc_age_acc, mc_age_rej, None),
                (ev,   ev_county, ev_race_acc, ev_race_rej, ev_race_cur,
                 ev_age_acc, ev_age_rej, ev_age_cur,
                 ev_gender, ev_party, ev_ethnicity,
                 ec_race_acc, ec_race_rej, ec_age_acc, ec_age_rej, ev_sites),
            ]:
                if df_chunk.empty:
                    continue

                acc_mask  = df_chunk["ballot_rtn_status"].isin(ACCEPTED)
                cur_mask  = df_chunk["ballot_rtn_status"].isin(CURED)
                cbl_mask  = df_chunk["ballot_rtn_status"].isin(CURABLE)
                rej_mask  = df_chunk["ballot_rtn_status"].isin(REJECTED)
                sdr_mask  = df_chunk["sdr"].eq("Y") if "sdr" in df_chunk else \
                            pd.Series([False]*len(df_chunk))

                # county aggregation
                if "county_desc" in df_chunk:
                    for county, grp in df_chunk.groupby("county_desc"):
                        if not county or county == "NAN":
                            continue
                        d = county_d[county]
                        d["returned"]  += len(grp)
                        d["accepted"]  += int(grp["ballot_rtn_status"].isin(ACCEPTED).sum())
                        d["curable"]   += int(grp["ballot_rtn_status"].isin(CURABLE).sum())
                        d["cured"]     += int(grp["ballot_rtn_status"].isin(CURED).sum())
                        d["rejected"]  += int(grp["ballot_rtn_status"].isin(REJECTED).sum())

                # statewide race
                if "race" in df_chunk:
                    race_a.update(df_chunk.loc[acc_mask,  "race"].value_counts().to_dict())
                    race_r.update(df_chunk.loc[~acc_mask & ~cur_mask, "race"].value_counts().to_dict())
                    race_c.update(df_chunk.loc[cur_mask,  "race"].value_counts().to_dict())

                # per-county race (for dropdown)
                if "county_desc" in df_chunk and "race" in df_chunk:
                    for county, grp in df_chunk.groupby("county_desc"):
                        if not county or county == "NAN":
                            continue
                        am = grp["ballot_rtn_status"].isin(ACCEPTED)
                        cm = grp["ballot_rtn_status"].isin(CURED)
                        cr_race_a[county].update(grp.loc[am, "race"].value_counts().to_dict())
                        cr_race_r[county].update(grp.loc[~am & ~cm, "race"].value_counts().to_dict())

                # statewide age
                if "age" in df_chunk:
                    age_num = pd.to_numeric(df_chunk["age"], errors="coerce")
                    age_a.update(age_bucket(age_num[acc_mask.values]))
                    age_r.update(age_bucket(age_num[(~acc_mask & ~cur_mask).values]))
                    age_c.update(age_bucket(age_num[cur_mask.values]))

                # per-county age
                if "county_desc" in df_chunk and "age" in df_chunk:
                    age_num = pd.to_numeric(df_chunk["age"], errors="coerce")
                    for county, grp in df_chunk.groupby("county_desc"):
                        if not county or county == "NAN":
                            continue
                        gam = grp["ballot_rtn_status"].isin(ACCEPTED)
                        gcm = grp["ballot_rtn_status"].isin(CURED)
                        gidx = grp.index
                        cr_age_a[county].update(
                            age_bucket(age_num[gidx][gam.values]))
                        cr_age_r[county].update(
                            age_bucket(age_num[gidx][(~gam & ~gcm).values]))

                if "gender" in df_chunk:
                    gend.update(df_chunk["gender"].value_counts().to_dict())
                if "voter_party_code" in df_chunk:
                    prty.update(df_chunk["voter_party_code"].value_counts().to_dict())
                if "ethnicity" in df_chunk:
                    ethn.update(df_chunk["ethnicity"].value_counts().to_dict())
                    ethn_acc = Counter()
                    ethn_rej = Counter()
                    ethn_cur = Counter()
                    ethn_acc.update(df_chunk.loc[acc_mask, "ethnicity"].value_counts().to_dict())
                    ethn_rej.update(df_chunk.loc[~acc_mask & ~cur_mask, "ethnicity"].value_counts().to_dict())
                    ethn_cur.update(df_chunk.loc[cur_mask, "ethnicity"].value_counts().to_dict())
                    # route to the right accumulator based on which df_chunk we're in
                    if cr_race_a is mc_race_acc:  # mail
                        m_ethn_acc.update(ethn_acc); m_ethn_rej.update(ethn_rej); m_ethn_cur.update(ethn_cur)
                        # per-county ethnicity
                        if "county_desc" in df_chunk:
                            for county, grp in df_chunk.groupby("county_desc"):
                                if not county or county == "NAN":
                                    continue
                                mc_ethnicity[county].update(
                                    grp["ethnicity"].value_counts().to_dict())
                    else:  # early voting
                        ev_ethn_acc.update(ethn_acc); ev_ethn_rej.update(ethn_rej); ev_ethn_cur.update(ethn_cur)

                # site usage (early voting only)
                if site_d is not None and "site_name" in df_chunk and "county_desc" in df_chunk:
                    for (county, site), grp in df_chunk.groupby(["county_desc", "site_name"]):
                        if (not county or county == "NAN" or
                                not site or site.strip() in ("", "NAN", "NONE")):
                            continue
                        s = site_d[county][site]
                        s["returned"] += len(grp)
                        if "race" in grp:
                            s["race"].update(grp["race"].value_counts().to_dict())
                        if "age" in grp:
                            age_num = pd.to_numeric(grp["age"], errors="coerce")
                            s["age"].update(age_bucket(age_num))

    # ── build county rows ─────────────────────────────────────────────────────
    def county_rows(county_d, cr_race_a, cr_race_r, cr_age_a, cr_age_r, county_curable=None, county_ethn=None):
        rows = []
        # ensure all 100 NC counties appear, filling missing ones with zeros
        all_counties = {c: county_d.get(c, {"returned":0,"accepted":0,"curable":0,"cured":0,"rejected":0})
                        for c in NC_COUNTIES}
        for k, v in all_counties.items():
            row = {
                "county_desc": k,
                **v,
                "race_pct": race_pct_table(
                    dict(cr_race_a.get(k, {})),
                    dict(cr_race_r.get(k, {})),
                    {}),
                "age_pct": age_pct_table(
                    dict(cr_age_a.get(k, {})),
                    dict(cr_age_r.get(k, {})),
                    {}),
            }
            if county_curable is not None:
                row["curable_categories"] = dict(county_curable.get(k, {}))
            if county_ethn is not None:
                row["ethnicity"] = dict(county_ethn.get(k, {}))
            rows.append(row)
        return rows

    mail_rows = county_rows(m_county, mc_race_acc, mc_race_rej,
                            mc_age_acc, mc_age_rej, m_county_curable_cats, mc_ethnicity)
    ev_rows   = county_rows(ev_county, ec_race_acc, ec_race_rej,
                            ec_age_acc, ec_age_rej)

    # ── site usage serialization ──────────────────────────────────────────────
    def serialize_sites(site_d):
        out = {}
        for county, sites in site_d.items():
            out[county] = [
                {"site": site,
                 "returned": data["returned"],
                 "race": dict(data["race"]),
                 "age": dict(data["age"])}
                for site, data in sorted(
                    sites.items(), key=lambda x: -x[1]["returned"])
                if data["returned"] > 0
            ]
        return out

    mail_returned = sum(v["returned"] for v in m_county.values())
    ev_returned   = sum(v["returned"] for v in ev_county.values())

    return {
        "mail": {
            "total_returned":        mail_returned,
            "accepted":              sum(v["accepted"]  for v in m_county.values()),
            "curable":               sum(v["curable"]   for v in m_county.values()),
            "cured":                 sum(v["cured"]     for v in m_county.values()),
            "rejected_or_spoiled":   sum(v["rejected"]  for v in m_county.values()),
            "pct_curable_of_returned": pct(
                sum(v["curable"] for v in m_county.values()), mail_returned),
            "sdr": {"total_sdr_ballots": 0, "failed_verification": 0,
                    "cured": 0, "pct_failed_of_sdr": None},
            "curable_categories":    dict(m_curable_cats),
            "by_county":             mail_rows,
            "demographics": {
                "race_pct":   race_pct_table(dict(m_race_acc),
                                             dict(m_race_rej),
                                             dict(m_race_cur)),
                "age_pct":    age_pct_table(dict(m_age_acc),
                                            dict(m_age_rej),
                                            dict(m_age_cur)),
                "gender":     dict(m_gender),
                "party":      dict(m_party),
                "ethnicity_pct": race_pct_table(dict(m_ethn_acc),
                                               dict(m_ethn_rej),
                                               dict(m_ethn_cur)),
                "ethnicity":  dict(m_ethnicity),
            },
            "raw_status_counts": dict(status_c),
        },
        "early_voting": {
            "total_returned":        ev_returned,
            "accepted":              sum(v["accepted"]  for v in ev_county.values()),
            "curable":               sum(v["curable"]   for v in ev_county.values()),
            "cured":                 sum(v["cured"]     for v in ev_county.values()),
            "rejected_or_spoiled":   sum(v["rejected"]  for v in ev_county.values()),
            "pct_curable_of_returned": pct(
                sum(v["curable"] for v in ev_county.values()), ev_returned),
            "sdr": {"total_sdr_ballots": 0, "failed_verification": 0,
                    "cured": 0, "pct_failed_of_sdr": None},
            "by_county":    ev_rows,
            "sites_by_county": serialize_sites(ev_sites),
            "demographics": {
                "race_pct":   race_pct_table(dict(ev_race_acc),
                                             dict(ev_race_rej),
                                             dict(ev_race_cur)),
                "age_pct":    age_pct_table(dict(ev_age_acc),
                                            dict(ev_age_rej),
                                            dict(ev_age_cur)),
                "gender":     dict(ev_gender),
                "party":      dict(ev_party),
                "ethnicity_pct": race_pct_table(dict(ev_ethn_acc),
                                               dict(ev_ethn_rej),
                                               dict(ev_ethn_cur)),
                "ethnicity":  dict(ev_ethnicity),
            },
        },
    }


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
    for c in df.columns:
        df[c] = df[c].astype(str).str.replace("\x00", "", regex=False).str.strip()
    df["group_count"] = pd.to_numeric(df["group_count"], errors="coerce").fillna(0)

    def agg(col):
        d = df.groupby(col)["group_count"].sum().sort_values(ascending=False).to_dict()
        return {k: int(v) for k, v in d.items() if k and k.upper() != "NAN"}

    by_county_raw = df.groupby("county_name")["group_count"].sum().to_dict()
    by_county = [{"county_name": k, "requested": int(v)}
                 for k, v in by_county_raw.items()
                 if k and k.upper() != "NAN"]

    return {
        "total_requested": int(df["group_count"].sum()),
        "by_race":         agg("race_desc"),
        "by_gender":       agg("gender_desc"),
        "by_party":        agg("party_desc"),
        "by_age":          agg("age_range"),
        "by_ethnicity":    agg("ethncity_desc"),
        "by_week":         {str(k): int(v) for k, v in
                            df.groupby("request_week_num")["group_count"]
                            .sum().sort_index().items()},
        "by_county":       by_county,
    }


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
        enc, cols = detect_encoding(fp, ["county", "pv_status", "pv_party", "status"])
        print(f"[build] provisional encoding={enc}", file=sys.stderr)
        cols_present = [c for c in PROVISIONAL_COLS if c in cols]
        if not cols_present:
            df = pd.read_csv(fp, sep="\t", dtype=str, low_memory=False,
                             encoding=enc, encoding_errors="replace")
        else:
            df = pd.read_csv(fp, sep="\t", dtype=str, low_memory=False,
                             usecols=cols_present, encoding=enc,
                             encoding_errors="replace")
    df.columns = [c.strip().lower() for c in df.columns]
    pii = ["full_name", "res_addr_street", "res_addr_csz", "phone_num", "voter_reg_num"]
    df = df.drop(columns=[c for c in pii if c in df.columns], errors="ignore")
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip().str.upper()
    return df


def summarize_provisional(df):
    total = len(df)
    sc    = df["pv_status"].value_counts().to_dict() if "pv_status" in df else {}
    approved    = int(sc.get("APPROVED", 0))
    not_counted = int(sc.get("NOT COUNTED", 0))
    partial     = int(sc.get("PARTIAL", 0))
    by_county   = (df.groupby("county_name").size()
                   .reset_index(name="count").to_dict(orient="records")
                   if "county_name" in df else [])
    reasons = (df["not_counted_reason"].value_counts().to_dict()
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


def rebuild_trend():
    trend = []
    for p in sorted(HIST_DIR.glob("retrieved_*.json")):
        try:
            snap = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if snap.get("status") == "no_data_yet":
            continue
        mail = snap.get("absentee_mail") or snap.get("absentee") or {}
        ev   = snap.get("absentee_early_voting") or {}
        prov = snap.get("provisional") or {}
        trend.append({
            "date":             p.stem.replace("retrieved_", "", 1),
            "mail_curable":     mail.get("curable"),
            "mail_cured":       mail.get("cured"),
            "mail_pct_curable": mail.get("pct_curable_of_returned"),
            "ev_curable":       ev.get("curable"),
            "ev_cured":         ev.get("cured"),
            "ev_sdr_failed":    (ev.get("sdr") or {}).get("failed_verification"),
            "prov_total":       prov.get("total"),
            "prov_approved":    prov.get("approved"),
        })
    (DATA_DIR / "trend.json").write_text(json.dumps(trend, indent=2))
    return trend


def placeholder(cfg, note):
    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "election_date":  cfg["election_date"],
        "election_label": cfg["election_label"],
        "status": "no_data_yet", "note": note,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HIST_DIR.mkdir(parents=True, exist_ok=True)

    result = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "election_date":  cfg["election_date"],
        "election_label": cfg["election_label"],
        "status": "ok",
    }

    try:
        ab = fetch_absentee_df(cfg)
        result["absentee_mail"]         = ab["mail"]
        result["absentee_early_voting"] = ab["early_voting"]
    except (HTTPError, URLError, StopIteration) as e:
        print(f"[build] absentee not available: {e}", file=sys.stderr)
        result["absentee_mail"] = result["absentee_early_voting"] = None
        result["status"] = "partial"

    try:
        result["requested_ballots"] = fetch_demo_stats(cfg)
    except (HTTPError, URLError) as e:
        print(f"[build] demo_stats not available: {e}", file=sys.stderr)
        result["requested_ballots"] = None
        result["status"] = "partial"

    try:
        prov_df = fetch_provisional_df(cfg)
        result["provisional"] = summarize_provisional(prov_df)
    except (HTTPError, URLError, StopIteration) as e:
        print(f"[build] provisional not available: {e}", file=sys.stderr)
        result["provisional"] = None
        if result["status"] == "ok":
            result["status"] = "partial"

    if all(result.get(k) is None
           for k in ["absentee_mail", "absentee_early_voting", "provisional"]):
        result = placeholder(cfg,
            "NCSBE has not yet published files for this election date.")

    (DATA_DIR / "latest.json").write_text(json.dumps(result, indent=2))

    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (HIST_DIR / f"retrieved_{run_date}.json").write_text(json.dumps(result, indent=2))

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
