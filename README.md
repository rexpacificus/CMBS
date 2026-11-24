# CMBS Loan-Tape Validator

Validates loan-level CMBS data from SEC Form ABS-EE (Exhibit 102, the asset data file), runs credit stress tests, and produces a one-page table of flagged loans.

## Results: WFCM 2022-C62 (Oct 2025 remittance)

Ran on the SEC ABS-EE asset data for Wells Fargo Commercial Mortgage Trust 2022-C62: 46 loans, $522M current balance.

- **6 high-severity loans, $93.7M (17.9% of pool)**
- 530 Bush Street (SF office): occupancy 84% → 37%, NOI negative
- Three NYC mixed-use loans from the same originator, all 90+ days delinquent
- Reappraisals cut values by up to 63% (Oakridge Apartments $23.7M → $8.7M)
- Recomputed DSCRs tied out to servicer-reported DSCRs on every loan

Full one-page report: [`WFCM_2022-C62_validation.pdf`](WFCM_2022-C62_validation.pdf) · flagged loans: [`flagged_loans.csv`](flagged_loans.csv)

## Run it

```bash
pip install -r requirements.txt
python validator.py exh_102.xml --out report.html --csv flagged_loans.csv --title "WFCM 2022-C62, Oct 2025"
```

## Getting a real tape from EDGAR

1. Go to EDGAR full-text search, filter form type `ABS-EE`, search a conduit shelf (Benchmark, BBCMS, BANK, Wells Fargo Commercial Mortgage Trust, Morgan Stanley Capital I, Citigroup Commercial Mortgage Trust).
2. Pick a **2021 to 2022 vintage** deal and take its **latest** monthly ABS-EE. Seasoned deals are where most-recent NOI, occupancy and DSCR have drifted from underwriting, and 2022 deals are hitting the refi wall.
3. Open the filing index and download `exh_102.xml`.

## Checks

| ID | Type | Check |
|----|------|-------|
| DQ01 | Data quality | Missing critical fields |
| DQ02 | Data quality | Duplicate asset number |
| DQ03 | Data quality | Current balance above original amount |
| DQ04 | Data quality | Interest rate outside 1% to 15% (unit errors) |
| DQ05 | Data quality | Maturity inconsistent with origination date + term |
| DQ06 | Data quality | Reported DSCR doesn't tie to NOI / debt service |
| DQ07 | Data quality | Financials more than 18 months stale |
| DQ08 | Data quality | Occupancy outside 0 to 100% |
| CR01 | Credit | DSCR below 1.00x |
| CR02 | Credit | DSCR 1.00x to 1.25x (watch) |
| CR03 | Credit | LTV above 75% |
| CR04 | Credit | Debt yield below 8% |
| CR05 | Credit | Matures within 24 months |
| CR06 | Credit | Occupancy down 10+ pts, or below 80% (non-hotel; hotels normally run 65-80%) |
| CR07 | Credit | Annualized NOI down 20%+ vs underwriting |
| CR08 | Credit | Delinquent, P&I advances outstanding, or non-recoverable |

**Stress:** NOI down 10% and 20%; +200bp on floating-rate loans; refi test at maturity (NOI down 10%, new 30-year amortizing loan at coupon +200bp, fails below 1.00x). Stress is skipped for loans failing DQ03, DQ04 or DQ06, since the inputs are unreliable.

## Things a naive parser gets wrong (handled here)

- **Portfolio loans** appear as a parent row (`1`) plus per-property detail rows (`1-001`, `1-002`, ...). Summing every row double counts the pool. Detail rows are excluded from balance totals but still get property-level checks (occupancy).
- **Most-recent financials are often year-to-date.** A loan reporting Jan to Jun NOI looks like a 50% NOI collapse vs. full-year underwriting. NOI, NCF and debt service are annualized using the financials start and end dates.
- **Split notes and pari passu loans.** Rows like `1A`, `1B` are extra pieces of loan 1 held in the same trust (counted in pool balance, not as separate loans). Pari passu loans (`loanStructureCode` = PP) are split across several trusts, but NOI and appraisal cover the whole loan, so LTV, debt yield and stress use the whole-loan balance, inferred from whole-loan vs. trust debt service.
- **LTV isn't a field.** It's computed as current balance / most recent valuation (falling back to the securitization appraisal, which in conduit deals is usually the only one, so LTV here is current balance against an appraisal that is years old).
- **Conduit deals are almost all fixed rate**, so the +200bp floating shock usually hits zero loans. The refi test is the rate stress that actually matters for fixed-rate CMBS.

## Files

- `validator.py`: parser, checks, stress, report
- `make_sample_tape.py`: builds a **synthetic** test tape in the SEC schema with planted errors (one per check), used to verify every check fires
- `exh_102.xml`: the real ABS-EE asset data file for WFCM 2022-C62 from SEC EDGAR (public)
- `WFCM_2022-C62_validation.pdf`, `flagged_loans.csv`: output on the real deal
- `requirements.txt`: Python dependencies
