"""
CMBS loan-tape validator for SEC Form ABS-EE asset data (Exhibit 102, CMBS schema).

Usage:
    python validator.py path/to/exh_102.xml [--out report.html] [--csv flags.csv]

Pipeline:
    1. parse_abs_ee()   XML -> pandas DataFrame (one row per <assets> block)
    2. normalize()      types, units, annualized financials, LTV / debt yield
    3. run_checks()     data-quality (integrity) checks + credit (risk) checks
    4. run_stress()     NOI -10% / -20%, +200bp on floating loans, refi test at maturity
    5. build_report()   one-page HTML table of flagged loans

Field names follow the SEC CMBS asset-data schema
(namespace http://www.sec.gov/edgar/document/absee/cmbs/assetdata).
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Thresholds (tweak here, everything downstream reads these)
# ---------------------------------------------------------------------------
DSCR_FLOOR = 1.00          # below this, property cash flow doesn't cover debt service
DSCR_WATCH = 1.25          # common CMBS watchlist trigger
LTV_MAX = 0.75             # leverage ceiling
DEBT_YIELD_MIN = 0.08      # NOI / balance; refi lenders want ~8-10%
MATURITY_WINDOW_MO = 24    # maturity wall
OCC_DROP_PTS = 0.10        # occupancy down 10+ points vs securitization
OCC_FLOOR = 0.80
NOI_DECLINE = 0.20         # annualized NOI down 20%+ vs underwriting
STALE_FIN_MO = 18          # financials older than this vs reporting date
DSCR_RECALC_TOL = 0.15     # reported vs recomputed DSCR tolerance
RATE_SHOCK = 0.02          # +200bp
NOI_SHOCKS = (0.10, 0.20)
REFI_AMORT_YEARS = 30

DELINQUENT_CODES = {"1", "2", "3", "5", "6"}   # 30+ days, non-performing matured balloon, etc.
FLOATING_RATE_CODES = {"2"}                    # originalInterestRateTypeCode: 1 fixed, 2 ARM/floating

CRITICAL_FIELDS = [
    "reportperiodendactualbalanceamount",
    "maturitydate",
    "reportperiodinterestratepercentage",
    "valuationsecuritizationamount",
    "mostrecentnetoperatingincomeamount",
    "mostrecentdebtservicecoveragenetoperatingincomepercentage",
    "mostrecentphysicaloccupancypercentage",
    "propertytypecode",
]

NUMERIC_FIELDS = [
    "originalloanamount", "originaltermloannumber", "originalamortizationtermnumber",
    "originalinterestratepercentage", "interestratesecuritizationpercentage",
    "reportperiodinterestratepercentage", "originalinterestonlytermnumber",
    "periodicprincipalandinterestpaymentsecuritizationamount",
    "scheduledprincipalbalancesecuritizationamount",
    "reportperiodbeginningscheduleloanbalanceamount", "reportperiodendactualbalanceamount",
    "reportperiodendscheduledloanbalanceamount",
    "valuationsecuritizationamount", "mostrecentvaluationamount",
    "physicaloccupancysecuritizationpercentage", "mostrecentphysicaloccupancypercentage",
    "revenuesecuritizationamount", "mostrecentrevenueamount",
    "operatingexpensessecuritizationamount", "operatingexpensesamount",
    "netoperatingincomesecuritizationamount", "mostrecentnetoperatingincomeamount",
    "netcashflowflowsecuritizationamount", "mostrecentnetcashflowamount",
    "mostrecentdebtserviceamount",
    "debtservicecoveragenetoperatingincomesecuritizationpercentage",
    "mostrecentdebtservicecoveragenetoperatingincomepercentage",
    "debtservicecoveragenetcashflowsecuritizationpercentage",
    "mostrecentdebtservicecoveragenetcashflowpercentage",
    "totalprincipalinterestadvancedoutstandingamount",
    "totaltaxesinsuranceadvancesoutstandingamount",
    "otherexpensesadvancedoutstandingamount",
    "numberpropertiessecuritization", "numberproperties",
    "totalscheduledprincipalinterestdueamount",
]

DATE_FIELDS = [
    "reportingperiodbeginningdate", "reportingperiodenddate", "originationdate",
    "maturitydate", "firstloanpaymentduedate", "valuationsecuritizationdate",
    "mostrecentvaluationdate", "financialssecuritizationdate",
    "mostrecentfinancialsstartdate", "mostrecentfinancialsenddate", "paidthroughdate",
]


# ---------------------------------------------------------------------------
# 1. Parse
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    """Strip XML namespace and lowercase, so casing quirks in the SEC schema
    (e.g. ...NetCashFlowpercentage) don't break lookups."""
    return tag.rsplit("}", 1)[-1].lower()


def parse_abs_ee(path: str) -> pd.DataFrame:
    root = ET.parse(path).getroot()
    rows = []
    for asset in root.iter():
        if _local(asset.tag) != "assets":
            continue
        row: dict = {}
        n_props = 0
        for child in asset:
            name = _local(child.tag)
            if len(child):  # nested block like <property>
                if name == "property":
                    n_props += 1
                    if n_props > 1:      # keep first property's fields, count the rest
                        continue
                for g in child:
                    if len(g) == 0:
                        row.setdefault(_local(g.tag), (g.text or "").strip())
            else:
                row[name] = (child.text or "").strip()
        row["_property_blocks"] = n_props
        rows.append(row)
    if not rows:
        raise ValueError("No <assets> blocks found. Is this an EX-102 CMBS asset data file?")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. Normalize
# ---------------------------------------------------------------------------
def _to_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%m-%d-%Y", errors="coerce")


def _months_between(a: pd.Series, b: pd.Series) -> pd.Series:
    return (b.dt.year - a.dt.year) * 12 + (b.dt.month - a.dt.month)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in CRITICAL_FIELDS + NUMERIC_FIELDS + DATE_FIELDS:
        if c not in df.columns:
            df[c] = np.nan
    df = df.replace({"": np.nan})

    for c in NUMERIC_FIELDS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in DATE_FIELDS:
        df[c] = _to_date(df[c].astype("string"))

    # Units: percentages in ABS-EE are decimals (0.92, 0.03885). Guard against a tape
    # that reports them as whole numbers (92, 3.885).
    for c in ["physicaloccupancysecuritizationpercentage", "mostrecentphysicaloccupancypercentage"]:
        if df[c].median(skipna=True) > 1.5:
            df[c] = df[c] / 100
    for c in ["reportperiodinterestratepercentage", "interestratesecuritizationpercentage",
              "originalinterestratepercentage"]:
        if df[c].median(skipna=True) > 0.5:
            df[c] = df[c] / 100

    # Row role: "1-001" style ids are per-property detail rows of a portfolio loan.
    df["assetnumber"] = df["assetnumber"].astype("string")
    df["is_property_row"] = df["assetnumber"].str.contains(r"-\d+$", regex=True, na=False)
    # "1A", "1B" style ids are additional notes of loan 1 held in this trust (no property data of their own).
    df["is_note_row"] = df["assetnumber"].str.fullmatch(r"\d+[A-Za-z]+", na=False)
    df["parent_loan"] = df["assetnumber"].str.replace(r"(-\d+|[A-Za-z]+)$", "", regex=True)

    # Annualize most-recent financials: servicers report YTD numbers (e.g. Jan-Jun).
    start = df["mostrecentfinancialsstartdate"]
    end = df["mostrecentfinancialsenddate"]
    days = (end - start).dt.days + 1
    factor = (365.0 / days).where(days.between(60, 400), 1.0)   # unknown/odd periods: leave as is
    df["fin_period_months"] = (days / 30.42).round(1)
    df["annualize_factor"] = factor
    df["noi_ann"] = df["mostrecentnetoperatingincomeamount"] * factor
    df["ncf_ann"] = df["mostrecentnetcashflowamount"] * factor
    df["ds_ann"] = df["mostrecentdebtserviceamount"] * factor

    # Fallback debt service if not reported: balance * rate (IO approximation)
    bal = df["reportperiodendactualbalanceamount"]
    rate = df["reportperiodinterestratepercentage"].fillna(df["interestratesecuritizationpercentage"])
    df["rate"] = rate
    df["ds_ann"] = df["ds_ann"].fillna(bal * rate)

    # Pari passu loans: one whole loan split across several CMBS trusts. Property NOI, value and
    # reported debt service cover the WHOLE loan, but the balance is only this trust's piece.
    # Whole-loan balance = trust balance x (whole-loan debt service / this trust's scheduled debt service).
    trust_ds = df["totalscheduledprincipalinterestdueamount"] * 12
    ratio = (df["ds_ann"] / trust_ds).where(df.get("loanstructurecode", pd.Series(dtype="string")).astype("string") == "PP")
    df["whole_loan_factor"] = ratio.where(ratio > 1.05, 1.0).fillna(1.0)
    df["whole_loan_balance"] = bal * df["whole_loan_factor"]
    # Trust exposure: parent balance plus any extra notes (1A, 1B) the trust holds
    note_bal = df[df["is_note_row"]].groupby("parent_loan")["reportperiodendactualbalanceamount"].sum()
    df["trust_exposure"] = bal + df["assetnumber"].map(note_bal).fillna(0)
    df.loc[df["is_note_row"] | df["is_property_row"], "trust_exposure"] = np.nan

    # Derived metrics
    df["dscr_reported"] = df["mostrecentdebtservicecoveragenetoperatingincomepercentage"]
    df["dscr_ncf_reported"] = df["mostrecentdebtservicecoveragenetcashflowpercentage"]
    df["dscr_recalc"] = df["noi_ann"] / df["ds_ann"]
    value = df["mostrecentvaluationamount"].fillna(df["valuationsecuritizationamount"])
    df["value_used"] = value
    df["ltv"] = df["whole_loan_balance"] / value
    df["debt_yield"] = df["noi_ann"] / df["whole_loan_balance"]
    df["occ_drop"] = (df["physicaloccupancysecuritizationpercentage"]
                      - df["mostrecentphysicaloccupancypercentage"])
    df["noi_change"] = df["noi_ann"] / df["netoperatingincomesecuritizationamount"] - 1
    df["months_to_maturity"] = _months_between(df["reportingperiodenddate"], df["maturitydate"])
    df["is_floating"] = df.get("originalinterestratetypecode", pd.Series(dtype="string")) \
        .astype("string").isin(FLOATING_RATE_CODES)
    return df


# ---------------------------------------------------------------------------
# 3. Checks
# ---------------------------------------------------------------------------
@dataclass
class Check:
    id: str
    kind: str        # "DQ" = data quality / integrity, "CR" = credit risk
    name: str
    severity: str    # High / Medium / Low
    loan_level: bool = True   # False = also applies to property detail rows


CHECKS = [
    Check("DQ01", "DQ", "Missing critical field(s)", "Medium"),
    Check("DQ02", "DQ", "Duplicate asset number", "High", loan_level=False),
    Check("DQ03", "DQ", "Balance exceeds original loan amount", "High"),
    Check("DQ04", "DQ", "Interest rate outside 1%-15%", "High"),
    Check("DQ05", "DQ", "Maturity inconsistent with origination + term", "Medium"),
    Check("DQ06", "DQ", "Reported DSCR doesn't tie to NOI / debt service", "Medium"),
    Check("DQ07", "DQ", f"Financials stale (>{STALE_FIN_MO} mo old)", "Low"),
    Check("DQ08", "DQ", "Occupancy outside 0-100%", "High", loan_level=False),
    Check("CR01", "CR", f"DSCR below {DSCR_FLOOR:.2f}x", "High"),
    Check("CR02", "CR", f"DSCR below {DSCR_WATCH:.2f}x watch level", "Medium"),
    Check("CR03", "CR", f"LTV above {LTV_MAX:.0%}", "Medium"),
    Check("CR04", "CR", f"Debt yield below {DEBT_YIELD_MIN:.0%}", "Medium"),
    Check("CR05", "CR", f"Matures within {MATURITY_WINDOW_MO} months", "Medium"),
    Check("CR06", "CR", f"Occupancy down {OCC_DROP_PTS:.0%}+ pts or below {OCC_FLOOR:.0%} (non-hotel)", "Medium",
          loan_level=False),
    Check("CR07", "CR", f"Annualized NOI down {NOI_DECLINE:.0%}+ vs underwriting", "Medium"),
    Check("CR08", "CR", "Delinquent, advancing, or non-recoverable", "High"),
]


def run_checks(df: pd.DataFrame) -> pd.DataFrame:
    """Return long table: one row per (asset, failed check) with a detail string."""
    hits = []

    def add(mask, cid, detail_fn):
        for idx in df.index[mask.fillna(False)]:
            hits.append({"idx": idx, "check": cid, "detail": detail_fn(df.loc[idx])})

    loan = ~df["is_property_row"] & ~df["is_note_row"]
    any_row = ~df["is_note_row"]

    # DQ01 missing critical fields (loan rows only; detail rows legitimately omit loan fields)
    missing = df[CRITICAL_FIELDS].isna()
    add(loan & missing.any(axis=1), "DQ01",
        lambda r: "missing: " + ", ".join(c for c in CRITICAL_FIELDS if pd.isna(r[c])))

    # DQ02 duplicate ids
    add(df["assetnumber"].duplicated(keep=False), "DQ02", lambda r: f"asset {r['assetnumber']} appears more than once")

    # DQ03 balance > original (no neg-am in conduit)
    add(loan & (df["reportperiodendactualbalanceamount"] > df["originalloanamount"] * 1.0001), "DQ03",
        lambda r: f"bal ${r['reportperiodendactualbalanceamount']:,.0f} > orig ${r['originalloanamount']:,.0f}")

    # DQ04 rate sanity
    add(loan & ~df["rate"].between(0.01, 0.15) & df["rate"].notna(), "DQ04",
        lambda r: f"rate {r['rate']:.4%}")

    # DQ05 maturity vs origination + term (allow +/- 2 months for first-payment conventions)
    implied = _months_between(df["originationdate"], df["maturitydate"])
    add(loan & ((implied - df["originaltermloannumber"]).abs() > 2), "DQ05",
        lambda r: f"orig->maturity {int(_months_between(pd.Series([r['originationdate']]), pd.Series([r['maturitydate']]))[0])} mo vs term {int(r['originaltermloannumber'])} mo")

    # DQ06 DSCR tie-out
    add(loan & ((df["dscr_reported"] - df["dscr_recalc"]).abs() > DSCR_RECALC_TOL), "DQ06",
        lambda r: f"reported {r['dscr_reported']:.2f}x vs recomputed {r['dscr_recalc']:.2f}x")

    # DQ07 stale financials
    age = _months_between(df["mostrecentfinancialsenddate"], df["reportingperiodenddate"])
    add(loan & (age > STALE_FIN_MO), "DQ07", lambda r: f"financials end {r['mostrecentfinancialsenddate']:%m/%Y}")

    # DQ08 occupancy range
    occ = df["mostrecentphysicaloccupancypercentage"]
    add(any_row & ((occ < 0) | (occ > 1.0001)), "DQ08", lambda r: f"occupancy {r['mostrecentphysicaloccupancypercentage']}")

    # Credit checks. Prefer recomputed DSCR (annualized NOI / annualized DS); fall back to reported.
    dscr = df["dscr_recalc"].fillna(df["dscr_reported"])
    df["dscr_used"] = dscr
    add(loan & (dscr < DSCR_FLOOR), "CR01", lambda r: f"DSCR {r['dscr_used']:.2f}x")
    add(loan & dscr.between(DSCR_FLOOR, DSCR_WATCH, inclusive="left"), "CR02", lambda r: f"DSCR {r['dscr_used']:.2f}x")
    add(loan & (df["ltv"] > LTV_MAX), "CR03", lambda r: f"LTV {r['ltv']:.0%}")
    add(loan & (df["debt_yield"] < DEBT_YIELD_MIN), "CR04", lambda r: f"DY {r['debt_yield']:.1%}")
    add(loan & df["months_to_maturity"].between(-999, MATURITY_WINDOW_MO), "CR05",
        lambda r: f"{int(r['months_to_maturity'])} mo to maturity ({r['maturitydate']:%m/%Y})")
    # Hotels normally run 65-80% occupancy, so the absolute floor only applies to non-lodging.
    not_hotel = df["propertytypecode"].astype("string") != "LO"
    add(any_row & ((df["occ_drop"] >= OCC_DROP_PTS) | ((occ < OCC_FLOOR) & not_hotel)), "CR06",
        lambda r: f"occ {r['physicaloccupancysecuritizationpercentage']:.0%} -> {r['mostrecentphysicaloccupancypercentage']:.0%}")
    add(loan & (df["noi_change"] <= -NOI_DECLINE), "CR07", lambda r: f"NOI {r['noi_change']:+.0%} (annualized)")
    adv = df[["totalprincipalinterestadvancedoutstandingamount", "totaltaxesinsuranceadvancesoutstandingamount",
              "otherexpensesadvancedoutstandingamount"]].fillna(0).sum(axis=1)
    status = df.get("paymentstatusloancode", pd.Series(dtype="string")).astype("string")
    nonrec = df.get("nonrecoverabilityindicator", pd.Series(dtype="string")).astype("string").str.lower() == "true"
    add(loan & (status.isin(DELINQUENT_CODES) | (adv > 0) | nonrec), "CR08",
        lambda r: f"status {r.get('paymentstatusloancode', '?')}, advances ${(adv[r.name]):,.0f}")

    out = pd.DataFrame(hits, columns=["idx", "check", "detail"])
    meta = {c.id: c for c in CHECKS}
    out["kind"] = out["check"].map(lambda c: meta[c].kind)
    out["severity"] = out["check"].map(lambda c: meta[c].severity)
    out["name"] = out["check"].map(lambda c: meta[c].name)
    return out


# ---------------------------------------------------------------------------
# 4. Stress
# ---------------------------------------------------------------------------
def _mortgage_constant(rate: pd.Series, years: int = REFI_AMORT_YEARS) -> pd.Series:
    r = rate / 12
    n = years * 12
    return 12 * r / (1 - (1 + r) ** -n)


def run_stress(df: pd.DataFrame) -> pd.DataFrame:
    s = pd.DataFrame(index=df.index)
    bal = df["whole_loan_balance"]
    noi = df["noi_ann"].fillna(df["dscr_reported"] * df["ds_ann"])
    ds = df["ds_ann"]
    s["dscr_base"] = noi / ds
    for shock in NOI_SHOCKS:
        s[f"dscr_noi_-{int(shock*100)}"] = noi * (1 - shock) / ds
    # Floating: +200bp on balance (IO approximation; floaters in CMBS are mostly IO)
    s["dscr_rate_+200"] = np.where(df["is_floating"], noi / (ds + bal * RATE_SHOCK), s["dscr_base"])
    # Refi test: can the loan refinance at maturity with NOI -10% at coupon +200bp on 30yr amort?
    refi_ds = bal * _mortgage_constant(df["rate"] + RATE_SHOCK)
    s["refi_dscr"] = noi * (1 - NOI_SHOCKS[0]) / refi_ds
    s["stress_fail"] = (s[[c for c in s.columns if c.startswith("dscr_")]] < DSCR_FLOOR).any(axis=1)
    s["refi_fail"] = s["refi_dscr"] < DSCR_FLOOR
    num = [c for c in s.columns if c not in ("stress_fail", "refi_fail")]
    s.loc[df["is_note_row"], num] = np.nan
    s.loc[df["is_note_row"], ["stress_fail", "refi_fail"]] = False
    return s


def suppress_bad_data(stress: pd.DataFrame, hits: pd.DataFrame) -> pd.DataFrame:
    """Don't report stress results computed from inputs we already know are broken."""
    bad = set(hits.loc[hits["check"].isin(["DQ03", "DQ04", "DQ06"]), "idx"])
    stress = stress.copy()
    for c in stress.columns:
        stress.loc[stress.index.isin(bad), c] = np.nan if stress[c].dtype.kind == "f" else False
    return stress


# ---------------------------------------------------------------------------
# 5. Report
# ---------------------------------------------------------------------------
SEV_RANK = {"High": 3, "Medium": 2, "Low": 1}


def summarize(df: pd.DataFrame, hits: pd.DataFrame, stress: pd.DataFrame) -> pd.DataFrame:
    """One row per flagged asset."""
    loans = df.join(stress)
    flagged_idx = set(hits["idx"]) | set(loans.index[(loans["stress_fail"] | loans["refi_fail"]) & ~loans["is_property_row"] & ~loans["is_note_row"]])
    rows = []
    for i in sorted(flagged_idx, key=lambda x: str(df.loc[x, "assetnumber"])):
        r = loans.loc[i]
        h = hits[hits["idx"] == i]
        sev = max([SEV_RANK[x] for x in h["severity"]] + [2 if r["stress_fail"] else 0, 1 if r["refi_fail"] else 0])
        rows.append({
            "Asset": r["assetnumber"],
            "Property": r.get("propertyname", ""),
            "Type": r.get("propertytypecode", ""),
            "Balance": r["trust_exposure"] if not r["is_property_row"] else np.nan,
            "Whole loan": r["whole_loan_balance"] if r["whole_loan_factor"] > 1 else np.nan,
            "DSCR": np.nan if r["is_property_row"] else r.get("dscr_used", np.nan),
            "LTV": r["ltv"],
            "DY": r["debt_yield"],
            "Occ": r["mostrecentphysicaloccupancypercentage"],
            "Mat": r["maturitydate"],
            "DSCR NOI-20%": np.nan if r["is_property_row"] else r["dscr_noi_-20"],
            "Refi DSCR": r["refi_dscr"],
            "DQ flags": "; ".join(f"{c} {d}" for c, d in zip(h[h.kind == "DQ"]["check"], h[h.kind == "DQ"]["detail"])),
            "Credit flags": "; ".join(f"{c} {d}" for c, d in zip(h[h.kind == "CR"]["check"], h[h.kind == "CR"]["detail"])),
            "Stress": ", ".join(x for x, f in [("fails NOI/rate shock", r["stress_fail"]), ("refi gap", r["refi_fail"])] if f and not r["is_property_row"]),
            "Severity": {3: "High", 2: "Medium", 1: "Low", 0: "Low"}[sev],
            "_sev": sev,
            "_detail_row": r["is_property_row"],
        })
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values(["_sev", "Balance"], ascending=[False, False])
    return out


def _fmt(v, kind):
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NaT:
        return "&ndash;"
    try:
        if kind == "money":
            return f"${v/1e6:,.1f}M"
        if kind == "x":
            return f"{v:.2f}x"
        if kind == "pct":
            return f"{v:.0%}"
        if kind == "pct1":
            return f"{v:.1%}"
        if kind == "date":
            return f"{v:%m/%y}"
    except (TypeError, ValueError):
        return "&ndash;"
    return html.escape(str(v))


def build_report(df, hits, stress, summary, title, source) -> str:
    loans = df[~df["is_property_row"] & ~df["is_note_row"]]
    total_bal = df.loc[~df["is_property_row"], "reportperiodendactualbalanceamount"].sum()
    flagged_loans = summary[~summary["_detail_row"]].drop_duplicates("Asset") if len(summary) else summary
    n_high = int((flagged_loans["_sev"] == 3).sum()) if len(flagged_loans) else 0
    flagged_bal = flagged_loans["Balance"].sum() if len(flagged_loans) else 0
    period = df["reportingperiodenddate"].max()
    n_float = int(loans["is_floating"].sum())
    st = stress.loc[loans.index]

    check_counts = hits.groupby("check").size()
    check_rows = "".join(
        f"<tr><td>{c.id}</td><td>{html.escape(c.name)}</td><td>{c.severity}</td><td class=n>{int(check_counts.get(c.id, 0))}</td></tr>"
        for c in CHECKS)

    cols = [("Asset", "s"), ("Property", "s"), ("Type", "s"), ("Balance", "money"), ("Whole loan", "money"), ("DSCR", "x"),
            ("LTV", "pct"), ("DY", "pct1"), ("Occ", "pct"), ("Mat", "date"), ("DSCR NOI-20%", "x"),
            ("Refi DSCR", "x"), ("DQ flags", "s"), ("Credit flags", "s"), ("Stress", "s"), ("Severity", "s")]
    head = "".join(f"<th>{c}</th>" for c, _ in cols)
    body = ""
    for _, r in summary.iterrows():
        cls = f"sev{r['_sev']}" + (" detail" if r["_detail_row"] else "")
        body += f"<tr class='{cls}'>" + "".join(
            f"<td class={'n' if k not in ('s',) else 't'}>{_fmt(r[c], k)}</td>" for c, k in cols) + "</tr>"

    stress_line = (
        f"Base WA DSCR {np.average(st['dscr_base'].fillna(0), weights=loans['reportperiodendactualbalanceamount'].fillna(0)):.2f}x &middot; "
        f"loans &lt;1.00x: base {int((st['dscr_base'] < 1).sum())}, NOI&minus;10% {int((st['dscr_noi_-10'] < 1).sum())}, "
        f"NOI&minus;20% {int((st['dscr_noi_-20'] < 1).sum())}, +200bp floaters {int(((st['dscr_rate_+200'] < 1) & loans['is_floating']).sum())} "
        f"({n_float} floating loans) &middot; refi gap (NOI&minus;10%, coupon+200bp, 30yr amort, &lt;1.00x): {int(st['refi_fail'].sum())}"
    )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
@page {{ size: letter landscape; margin: 0.3in; }}
@media print {{ body {{ zoom: 0.68; margin:0; }} }}
body {{ font: 9px/1.3 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color:#1a1a1a; margin:16px; }}
h1 {{ font-size:15px; margin:0 0 2px; }} .sub {{ color:#555; margin-bottom:8px; }}
.kpis {{ display:flex; gap:18px; margin:6px 0 8px; }} .kpi b {{ display:block; font-size:14px; }}
.kpi span {{ color:#666; }}
table {{ border-collapse:collapse; width:100%; }} th,td {{ border-bottom:1px solid #e3e3e3; padding:2px 4px; vertical-align:top; }}
th {{ text-align:left; background:#f4f4f4; font-weight:600; position:sticky; top:0; }}
td.n {{ text-align:right; white-space:nowrap; }} td.t {{ max-width:230px; }}
tr.sev3 td:first-child {{ border-left:3px solid #c0392b; }} tr.sev2 td:first-child {{ border-left:3px solid #e6a23c; }}
tr.sev1 td:first-child {{ border-left:3px solid #9aa5b1; }} tr.detail {{ color:#666; font-style:italic; }}
.grid {{ display:grid; grid-template-columns: 1fr 2.2fr; gap:14px; margin-top:8px; }}
.note {{ color:#666; margin-top:6px; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class=sub>Source: {html.escape(source)} &middot; reporting period ending {period:%b %d, %Y} &middot; generated {datetime.now():%Y-%m-%d}</div>
<div class=kpis>
 <div class=kpi><b>{len(loans)}</b><span>loans ({int(df['is_note_row'].sum())} extra notes)</span></div>
 <div class=kpi><b>{int(df['is_property_row'].sum())}</b><span>property detail rows</span></div>
 <div class=kpi><b>${total_bal/1e6:,.0f}M</b><span>pool balance</span></div>
 <div class=kpi><b>{n_high}</b><span>high-severity loans</span></div>
 <div class=kpi><b>{len(flagged_loans)}</b><span>loans flagged (any)</span></div>
 <div class=kpi><b>{flagged_bal/total_bal:.0%}</b><span>of balance flagged</span></div>
 <div class=kpi><b>{len(hits)}</b><span>check failures</span></div>
</div>
<div class=sub><b>Stress:</b> {stress_line}</div>
<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
<div class=grid><div><table><thead><tr><th>Check</th><th>Description</th><th>Sev</th><th>Hits</th></tr></thead><tbody>{check_rows}</tbody></table></div>
<div class=note>DSCR = annualized most-recent NOI / annualized debt service (servicer YTD figures are annualized by the financials start/end dates; reported DSCR is used only if those are missing).
LTV = current balance / most recent valuation (securitization appraisal if none). DY = annualized NOI / balance.
Refi DSCR assumes NOI &minus;10% and a new 30-year amortizing loan at the current coupon +200bp. Stress is not computed for loans failing DQ03/DQ04/DQ06 (inputs unreliable). Pari passu loans: LTV, debt yield and stress use the whole-loan balance (inferred from whole-loan vs. trust debt service), since NOI and value cover the whole loan; Balance shows this trust's exposure. Italic rows are per-property detail rows of portfolio loans (balance excluded from totals).
DQ = data-quality / integrity check, CR = credit check. Thresholds are set at the top of validator.py.</div></div>
</body></html>"""


# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("xml")
    p.add_argument("--out", default="report.html")
    p.add_argument("--csv", default="flagged_loans.csv")
    p.add_argument("--title", default="CMBS Loan-Tape Validation")
    a = p.parse_args(argv)

    raw = parse_abs_ee(a.xml)
    df = normalize(raw)
    hits = run_checks(df)
    stress = suppress_bad_data(run_stress(df), hits)
    summary = summarize(df, hits, stress)

    with open(a.out, "w") as f:
        f.write(build_report(df, hits, stress, summary, a.title, a.xml.replace("\\", "/").rsplit("/", 1)[-1]))
    summary.drop(columns=["_sev", "_detail_row"]).to_csv(a.csv, index=False)

    loans = df[~df["is_property_row"] & ~df["is_note_row"]]
    fl = summary[~summary["_detail_row"]].drop_duplicates("Asset") if len(summary) else summary
    pool = df.loc[~df["is_property_row"], "reportperiodendactualbalanceamount"].sum()
    print(f"{len(loans)} loans (+{int(df['is_note_row'].sum())} extra notes, {int(df['is_property_row'].sum())} property detail rows), ${pool/1e6:,.1f}M")
    print(f"{len(CHECKS)} checks, {len(hits)} failures, {len(fl)} loans flagged, "
          f"{int((fl['_sev']==3).sum()) if len(fl) else 0} high severity "
          f"(${fl.loc[fl['_sev']==3,'Balance'].sum()/1e6:,.1f}M, {fl.loc[fl['_sev']==3,'Balance'].sum()/pool:.1%} of pool)")
    print(hits.groupby(["check", "name"]).size().to_string())
    print(f"-> {a.out}, {a.csv}")


if __name__ == "__main__":
    sys.exit(main())
