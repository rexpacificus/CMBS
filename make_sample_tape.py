"""Generate a SYNTHETIC EX-102 CMBS tape in the SEC schema for testing validator.py.
Loan 1 uses the real values from a 2022-vintage conduit filing; everything else is made up,
with known errors planted so every check can be verified. Not real deal data."""
import random
from datetime import date

random.seed(7)
NS = "http://www.sec.gov/edgar/document/absee/cmbs/assetdata"
TYPES = ["RT", "OF", "MF", "LO", "IN", "MU", "SS"]
RP_END = date(2025, 10, 14)


def d(x):
    return x.strftime("%m-%d-%Y")


def add_months(x, m):
    y, mo = divmod(x.month - 1 + m, 12)
    return date(x.year + y, mo + 1, min(x.day, 28))


def asset_xml(a):
    prop = "".join(f"<{k}>{v}</{k}>" for k, v in a["property"].items() if v is not None)
    loan = "".join(f"<{k}>{v}</{k}>" for k, v in a.items() if k not in ("property", "_after") and v is not None)
    after = "".join(f"<{k}>{v}</{k}>" for k, v in a["_after"].items() if v is not None)
    return f"<assets>{loan}<property>{prop}</property>{after}</assets>"


def make_loan(num, bal, rate, orig, term, ptype, occ_uw, occ_now, noi_uw, noi_now_ann, value,
              ratecode=1, status="0", adv=0, partial=True, name=None):
    mat = add_months(orig, term)
    ds_ann = bal * rate  # IO
    fin_start, fin_end = (date(2025, 1, 1), date(2025, 6, 30)) if partial else (date(2024, 1, 1), date(2024, 12, 31))
    f = 181 / 365 if partial else 1
    return {
        "assetTypeNumber": "Prospectus Loan ID", "assetNumber": num,
        "reportingPeriodBeginningDate": "09-12-2025", "reportingPeriodEndDate": d(RP_END),
        "originatorName": "Sample Originator", "originationDate": d(orig),
        "originalLoanAmount": f"{bal:.2f}", "originalTermLoanNumber": term, "maturityDate": d(mat),
        "originalAmortizationTermNumber": 0, "originalInterestRatePercentage": rate,
        "interestRateSecuritizationPercentage": rate, "interestAccrualMethodCode": 3,
        "originalInterestRateTypeCode": ratecode, "originalInterestOnlyTermNumber": term,
        "paymentTypeCode": 3, "NumberPropertiesSecuritization": 1, "NumberProperties": 1,
        "interestOnlyIndicator": "true", "balloonIndicator": "true", "modifiedIndicator": "false",
        "property": {
            "propertyName": name or f"Sample Property {num}", "propertyTypeCode": ptype,
            "valuationSecuritizationAmount": f"{value:.2f}", "valuationSecuritizationDate": d(add_months(orig, -1)),
            "physicalOccupancySecuritizationPercentage": occ_uw, "mostRecentPhysicalOccupancyPercentage": occ_now,
            "financialsSecuritizationDate": d(add_months(orig, -3)),
            "mostRecentFinancialsStartDate": d(fin_start), "mostRecentFinancialsEndDate": d(fin_end),
            "netOperatingIncomeSecuritizationAmount": f"{noi_uw:.2f}",
            "mostRecentNetOperatingIncomeAmount": f"{noi_now_ann * f:.2f}",
            "mostRecentNetCashFlowAmount": f"{noi_now_ann * f * 0.95:.2f}",
            "mostRecentDebtServiceAmount": f"{ds_ann * f:.2f}",
            "debtServiceCoverageNetOperatingIncomeSecuritizationPercentage": round(noi_uw / ds_ann, 4),
            "mostRecentDebtServiceCoverageNetOperatingIncomePercentage": round(noi_now_ann / ds_ann, 4),
            "mostRecentDebtServiceCoverageNetCashFlowpercentage": round(noi_now_ann * 0.95 / ds_ann, 4),
        },
        "_after": {
            "reportPeriodEndActualBalanceAmount": f"{bal:.2f}",
            "reportPeriodInterestRatePercentage": rate,
            "paidThroughDate": "10-06-2025", "nonRecoverabilityIndicator": "false",
            "totalPrincipalInterestAdvancedOutstandingAmount": adv,
            "totalTaxesInsuranceAdvancesOutstandingAmount": 0,
            "otherExpensesAdvancedOutstandingAmount": 0, "paymentStatusLoanCode": status,
        },
    }


assets = []
# Loan 1: real values from the filing (Pacific Castle Portfolio, 4 properties, Jan-Jun 2025 financials)
l1 = make_loan("1", 30_000_000, 0.03885, date(2022, 2, 2), 120, "RT", 0.92, 0.90, 6_581_595.61,
               3_485_257.06 * 365 / 181, 107_800_000, name="Pacific Castle Portfolio")
l1["property"]["mostRecentNetOperatingIncomeAmount"] = "3485257.06"
l1["property"]["mostRecentDebtServiceAmount"] = "1270762.28"
l1["property"]["mostRecentDebtServiceCoverageNetOperatingIncomePercentage"] = 2.7426
l1["NumberPropertiesSecuritization"] = l1["NumberProperties"] = 4
assets.append(l1)
for k in range(1, 5):  # property detail rows
    p = make_loan(f"1-00{k}", 7_500_000, 0.03885, date(2022, 2, 2), 120, "RT", 0.92,
                  [0.95, 0.91, 0.88, 0.71][k - 1], 1_645_000, 1_700_000, 26_950_000, name=f"Pacific Castle {k}")
    assets.append(p)

# Generic healthy-ish pool
for n in range(2, 41):
    bal = random.choice([5, 8, 10, 12.5, 15, 20, 25, 35, 50, 65]) * 1e6
    rate = round(random.uniform(0.032, 0.052), 5)
    orig = date(2022, random.randint(1, 4), random.randint(1, 28))
    term = random.choice([60, 120, 120, 120])
    occ_uw = round(random.uniform(0.88, 0.99), 2)
    noi_uw = bal * random.uniform(0.085, 0.13)
    noi_now = noi_uw * random.uniform(0.85, 1.12)
    value = bal / random.uniform(0.45, 0.68)
    assets.append(make_loan(str(n), bal, rate, orig, term, random.choice(TYPES), occ_uw,
                            round(min(0.99, occ_uw + random.uniform(-0.05, 0.03)), 2), noi_uw, noi_now, value,
                            partial=random.random() < 0.7))

A = {a["assetNumber"]: a for a in assets}
P = lambda n: A[n]["property"]
# --- Planted problems -------------------------------------------------------
# 7: office loan in distress: DSCR 0.82x, NOI down ~35%, occupancy collapse, delinquent, advancing
A["7"].update(make_loan("7", 50e6, 0.0455, date(2022, 2, 10), 120, "OF", 0.94, 0.61, 50e6*0.0455*1.27,
                        50e6*0.0455*0.82, 50e6/0.62, status="3", adv=1_250_000, name="Midtown Office Tower"))
# 9: balance > original (data error)
A["9"]["_after"]["reportPeriodEndActualBalanceAmount"] = f"{float(A['9']['originalLoanAmount'])*1.15:.2f}"
# 11: rate keyed as whole number (data error)
A["11"]["_after"]["reportPeriodInterestRatePercentage"] = 4.25
# 13: maturity doesn't match term (data error)
A["13"]["maturityDate"] = d(add_months(date(2022, 3, 1), 84)); A["13"]["originalTermLoanNumber"] = 120
# 15: reported DSCR doesn't tie (data error)
P("15")["mostRecentDebtServiceCoverageNetOperatingIncomePercentage"] = 2.95
# 17: missing fields
P("17")["mostRecentPhysicalOccupancyPercentage"] = None; P("17")["valuationSecuritizationAmount"] = None
# 19: high leverage
P("19")["valuationSecuritizationAmount"] = f"{float(A['19']['originalLoanAmount'])/0.82:.2f}"
# 21, 22: floating rate, thin coverage -> fail +200bp
for n in ("21", "22"):
    A[n]["originalInterestRateTypeCode"] = 2
    ds = float(P(n)["mostRecentDebtServiceAmount"])
    P(n)["mostRecentNetOperatingIncomeAmount"] = f"{ds*1.30:.2f}"
    P(n)["mostRecentDebtServiceCoverageNetOperatingIncomePercentage"] = 1.30
# 23: stale financials
P("23")["mostRecentFinancialsStartDate"] = "01-01-2023"; P("23")["mostRecentFinancialsEndDate"] = "12-31-2023"
# 25: occupancy > 100% (data error)
P("25")["mostRecentPhysicalOccupancyPercentage"] = 1.12
# 27: DSCR 1.15 watch + matures inside 24 mo (5-yr loan)
A["27"]["originalTermLoanNumber"] = 60; A["27"]["maturityDate"] = d(add_months(date(2022, 3, 15), 60)); A["27"]["originationDate"] = "03-15-2022"
ds = float(P("27")["mostRecentDebtServiceAmount"])
P("27")["mostRecentNetOperatingIncomeAmount"] = f"{ds*1.15:.2f}"
P("27")["mostRecentDebtServiceCoverageNetOperatingIncomePercentage"] = 1.15
# duplicate asset row
assets.append(dict(A["30"]))

xml = (f'<?xml version="1.0" encoding="us-ascii"?>\n<assetData xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
       f'xmlns="{NS}">' + "".join(asset_xml(a) for a in assets) + "</assetData>")
open("sample_exh_102_SYNTHETIC.xml", "w").write(xml)
print(f"wrote {len(assets)} asset rows")
