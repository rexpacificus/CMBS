**CMBS loan-tape validator**

I built this to check the loan-level data that CMBS deals file with the SEC every month (Form ABS-EE, Exhibit 102). It reads the XML, runs data-quality and credit checks on every loan, stress tests DSCR, and writes a one-page report of the loans that need a closer look.

**What it found on WFCM 2022-C62**

I ran it on the October 2025 filing for Wells Fargo Commercial Mortgage Trust 2022-C62, a conduit deal with 46 loans and $522M outstanding. Six loans came out high severity, $93.7M or about 18% of the pool. The worst is 530 Bush Street, a San Francisco office building where occupancy fell from 84% to 37% and NOI is now negative. Three NYC mixed-use loans from the same lender are all 90+ days delinquent, and new appraisals have cut some property values by more than half (Oakridge Apartments went from $23.7M to $8.7M). As a sanity check, the DSCRs I recomputed from raw NOI and debt service matched the servicer's reported numbers on every loan.

The full report is in [WFCM_2022-C62_validation.pdf](WFCM_2022-C62_validation.pdf) and the flagged loans are in [flagged_loans.csv](flagged_loans.csv).

**Running it**

```bash
pip install -r requirements.txt
python validator.py exh_102.xml --out report.html --csv flagged_loans.csv --title "WFCM 2022-C62, Oct 2025"
```

To try another deal, search EDGAR for ABS-EE filings from a conduit shelf (Benchmark, BBCMS, BANK, WFCM and so on), open a recent filing and download its exh_102.xml. Deals from 2021 and 2022 are the most interesting since their numbers have had time to move away from underwriting.

**The checks**

| ID | Check |
|----|-------|
| DQ01 | Missing critical fields |
| DQ02 | Duplicate asset number |
| DQ03 | Current balance above original amount |
| DQ04 | Interest rate outside 1% to 15% |
| DQ05 | Maturity doesn't match origination date plus term |
| DQ06 | Reported DSCR doesn't tie to NOI / debt service |
| DQ07 | Financials more than 18 months old |
| DQ08 | Occupancy outside 0 to 100% |
| CR01 | DSCR below 1.00x |
| CR02 | DSCR between 1.00x and 1.25x |
| CR03 | LTV above 75% |
| CR04 | Debt yield below 8% |
| CR05 | Matures within 24 months |
| CR06 | Occupancy down 10+ points, or below 80% (not applied to hotels, which normally run 65 to 80%) |
| CR07 | Annualized NOI down 20%+ from underwriting |
| CR08 | Delinquent, has advances outstanding, or marked non-recoverable |

DQ checks catch problems with the data itself and CR checks catch credit risk. For the stress tests I cut NOI by 10% and 20%, add 200bp to any floating-rate loans, and run a refinance test where each loan has to refinance at maturity at its rate plus 200bp on a 30-year amortization with NOI down 10%. Loans that fail DQ03, DQ04 or DQ06 are left out of the stress tests since their inputs can't be trusted.

**Things that tripped me up in the real data**

Portfolio loans show up as one main row plus a row for each property (1, 1-001, 1-002...), so adding up every row double counts the balance. Some loans also have extra notes in the same trust (1A, 1B), which belong to the pool balance but aren't separate loans.

The "most recent" financials are often year to date. A loan reporting January to June NOI looks like it lost half its income compared to full-year underwriting, so I annualize everything using the reporting dates.

Pari passu loans are split across several CMBS deals, but the NOI and appraisal on the tape are for the whole property. Comparing those to just this trust's share made one loan look like it had a 1% LTV. I back out the whole-loan balance from the debt service instead.

LTV isn't actually a field. I calculate it from the current balance and the most recent appraisal, which for most loans is still the one from origination.

Almost every conduit loan is fixed rate, so the floating-rate shock usually doesn't hit anything. The refinance test is the rate stress that matters for these deals.

**Files**

validator.py does the parsing, checks, stress tests and report. make_sample_tape.py builds a fake tape in the SEC format with one error planted for each check, which I used to make sure every check actually fires. exh_102.xml is the real WFCM 2022-C62 filing from EDGAR.
