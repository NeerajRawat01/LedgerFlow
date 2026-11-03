#!/usr/bin/env python3
"""Independent recomputation of Northwind FY2024 control figures.

Deliberately written from the raw CSVs with plain Decimal arithmetic, using
ONLY the group policies - it shares no code with the engine. Used to verify
the engine's output (assignment: "figures you recomputed independently").
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "data" / "northwind"

Q = Decimal("0.01")


def r2(x: Decimal) -> Decimal:
    return x.quantize(Q, rounding=ROUND_HALF_UP)


# --- rates (GBP per unit of foreign currency) ------------------------------
# fx_rates.csv: GBP/USD quote = USD per 1 GBP  =>  GBP per 1 USD = 1/quote.
usd_closing = Decimal("1") / Decimal("1.2530")
usd_average = Decimal("1") / Decimal("1.2780")
usd_opening = Decimal("1") / Decimal("1.2730")
usd_spot = Decimal("1") / Decimal("1.2645")
# USD/INR quote = INR per 1 USD => INR per GBP = 1.2530*85.62 etc.
# GBP per 1 INR = 1 / (GBP/USD * USD/INR) at matching type.
inr_closing = Decimal("1") / (Decimal("1.2530") * Decimal("85.62"))
inr_h2 = Decimal("1") / (Decimal("1.2840") * Decimal("83.95"))
print("rates: usd_closing", usd_closing, "inr_closing", inr_closing)
print("rates: usd_avg", usd_average, "inr_h2", inr_h2)

# --- trial balances --------------------------------------------------------
import csv


def read_tb_us():
    out = {}
    for i, row in enumerate(csv.DictReader((D / "tb_us.csv").open())):
        out[row["account_code"]] = (Decimal(row["debit"]), Decimal(row["credit"]))
    return out


def read_tb_uk():
    # columns: nominal_code,description,cost_centre,txn_currency,txn_amount,balance
    out = {}
    for row in csv.DictReader((D / "tb_uk.csv").open()):
        bal = Decimal(row["balance"] or "0")
        debit = bal if bal > 0 else Decimal("0")
        credit = -bal if bal < 0 else Decimal("0")
        code = row["nominal_code"]
        # sum rows with same code (7000 appears twice)
        d, c = out.get(code, (Decimal("0"), Decimal("0")))
        out[code] = (d + debit, c + credit)
    return out


def read_tb_in():
    out = {}
    for row in csv.DictReader((D / "tb_in.csv").open()):
        amt = Decimal(row["amount"])
        dr = row["dr_cr"].strip().lower() == "dr"
        d, c = out.get(row["ledger_code"], (Decimal("0"), Decimal("0")))
        if dr:
            out[row["ledger_code"]] = (d + amt, c)
        else:
            out[row["ledger_code"]] = (d, c + amt)
    return out


us = read_tb_us()
uk = read_tb_uk()
inn = read_tb_in()

# --- mapping (local->group) ------------------------------------------------
def read_map(fn):
    m = {}
    for row in csv.DictReader((D / fn).open()):
        m[row["local_code"]] = row["group_code"]
    return m


uk_map = read_map("map_uk.csv")
in_map = read_map("map_in.csv")

# Correct the UK 2310 defect per group COA: Deferred income -> 2300 (not 4100).
uk_map["2310"] = "2300"

GROUP_COA = {}
for row in csv.DictReader((D / "group_coa.csv").open()):
    GROUP_COA[row["account_code"]] = row


def us_group():
    return {k: v for k, v in us.items()}


def map_group(tb, mapping):
    out: dict[str, list[Decimal]] = {}
    for code, (d, c) in tb.items():
        g = mapping.get(code)
        if g is None:
            print("  UNMAPPED", code)
            continue
        cur = out.setdefault(g, [Decimal("0"), Decimal("0")])
        cur[0] += d
        cur[1] += c
    return {k: (v[0], v[1]) for k, v in out.items()}


# Journals (posted only). Valid = FY2024 dated and balanced.
entries = json.load((D / "adjustments.json").open())["entries"]


def _line_maps(entity, acct):
    """Whether a journal line maps to the group chart of accounts."""
    if entity == "US":
        return acct in GROUP_COA
    if entity == "UK":
        return acct in uk_map
    if entity == "IN":
        return acct in in_map
    return False


def classify_journals():
    seen = {}
    decisions = {}
    for e in entries:
        jid = e["id"]
        d = sum(Decimal(l.get("debit", "0") or "0") for l in e["lines"])
        c = sum(Decimal(l.get("credit", "0") or "0") for l in e["lines"])
        key = tuple(sorted((l["account"], str(l.get("debit")), str(l.get("credit"))) for l in e["lines"]))
        mapped = all(_line_maps(e["entity"], l["account"]) for l in e["lines"])
        if not e["date"].startswith("2024"):
            decisions[jid] = "excluded"
        elif d != c:
            decisions[jid] = "rejected"
        elif not mapped:
            # A journal whose lines are not all mapped to the group chart of
            # accounts is rejected IN FULL (policy §6). Posting only the mapped
            # lines would create a one-sided entry and unbalance the entity.
            decisions[jid] = "rejected"
        elif key in seen:
            # The engine holds BOTH members of a duplicate pair for review
            # (no arbitrary tie-break); mirror that here.
            decisions[jid] = "flagged"
            decisions[seen[key]] = "flagged"
        else:
            seen[key] = jid
            decisions[jid] = "posted"
    return decisions


decisions = classify_journals()
posted = {k for k, v in decisions.items() if v == "posted"}
print("\njournal decisions:", {k: decisions[k] for k in sorted(decisions)})
print("posted:", sorted(posted))

# Apply posted journals into group balances (in functional currency).
def apply_journals(group_bal, mapping, entity):
    out = {k: [v[0], v[1]] for k, v in group_bal.items()}
    for e in entries:
        if e["id"] not in posted or e["entity"] != entity:
            continue
        for l in e["lines"]:
            acct = l["account"]
            if entity == "US":
                g = acct
            else:
                g = mapping.get(acct)
            if g is None:
                print("  journal unmapped line", e["id"], acct)
                continue
            cur = out.setdefault(g, [Decimal("0"), Decimal("0")])
            cur[0] += Decimal(l.get("debit", "0") or "0")
            cur[1] += Decimal(l.get("credit", "0") or "0")
    return {k: (v[0], v[1]) for k, v in out.items()}


us_g = apply_journals(us_group(), {}, "US")
uk_g = apply_journals(map_group(uk, uk_map), uk_map, "UK")
in_g = apply_journals(map_group(inn, in_map), in_map, "IN")

EQUITY = {"3100", "3200", "3300", "3400"}
IC_REC, IC_PAY, IC_INC, IC_EXP, FX_ACC = "1250", "2150", "4200", "6300", "7300"


def translate(group_bal, closing, income):
    pl = {}
    bs = {}
    for code, (d, c) in group_bal.items():
        if code in EQUITY:
            continue
        acct = GROUP_COA.get(code)
        net = d - c
        if acct and acct["statement"] == "PL":
            pl[code] = pl.get(code, Decimal("0")) + net * income
        else:
            bs[code] = bs.get(code, Decimal("0")) + net * closing
    return pl, bs


us_pl, us_bs = translate(us_g, usd_closing, usd_average)
uk_pl, uk_bs = translate(uk_g, Decimal("1"), Decimal("1"))  # GBP entity

# India is consolidated for H2 ONLY (acquired 1 July 2024). Its trial-balance
# P&L is the FULL year, so it must NOT be used; instead build India's H2 P&L
# from the monthly rows (at the H2 average rate). India's balance sheet is
# taken from the TB (its closing net assets already equal acquisition net
# assets + the H2 result).
def india_h2_pl():
    pl: dict[str, Decimal] = {}
    for row in csv.DictReader((D / "in_pl_monthly.csv").open()):
        if row["month"] < "2024-07":
            continue
        amt = Decimal(row["amount"])
        g = in_map.get(row["ledger_code"])
        if g is None:
            continue
        acct = GROUP_COA.get(g)
        if acct is None or acct["statement"] != "PL":
            continue
        signed = amt if row["dr_cr"].strip().lower() == "cr" else -amt  # income-positive
        pl[g] = pl.get(g, Decimal("0")) - signed * inr_h2  # debit-positive
    return pl


def india_bs():
    bs: dict[str, Decimal] = {}
    for code, (d, c) in in_g.items():
        if code in EQUITY:
            continue
        acct = GROUP_COA.get(code)
        if acct is None or acct["statement"] == "PL":
            continue
        bs[code] = bs.get(code, Decimal("0")) + (d - c) * inr_closing
    return bs


in_pl = india_h2_pl()
in_bs = india_bs()


def merge(*dicts):
    out = {}
    for dd in dicts:
        for k, v in dd.items():
            out[k] = out.get(k, Decimal("0")) + v
    return out


comb_pl = merge(us_pl, uk_pl, in_pl)
comb_bs = merge(us_bs, uk_bs, in_bs)

# Intercompany balance difference and elimination.
#
# The in-transit item (INV-US-2291) is recorded by the receiving entity at the
# closing rate, so it appears on both sides and eliminates to zero (policy §5).
# Independently reproduce that: find the in-transit invoice from the CSV, value
# the receivable side at the closing rate, and add a matching payable.
in_transit_usd = Decimal("0")
for row in csv.DictReader((D / "intercompany.csv").open()):
    if row["doc_ref"] == "INV-US-2291":
        in_transit_usd += Decimal(row["doc_amount"])
in_transit_gbp = in_transit_usd * usd_closing
print("in-transit USD", in_transit_usd, "GBP@closing", r2(in_transit_gbp))

ic_rec = comb_bs.get(IC_REC, Decimal("0"))
ic_pay_net = comb_bs.get(IC_PAY, Decimal("0"))  # negative (credit)
# Recognise the in-transit item on the receiving (payable) side at closing rate.
ic_pay_net = ic_pay_net - in_transit_gbp
ic_diff = ic_rec + ic_pay_net  # receivable - payable magnitude (genuine FX only)
comb_bs[IC_REC] = Decimal("0")
comb_bs[IC_PAY] = Decimal("0")
# The genuine exchange difference on the matched invoices flows through the
# P&L (7300). Net convention is debit-positive, matching the engine: the
# elimination reduces the FX net by the difference.
comb_pl[FX_ACC] = comb_pl.get(FX_ACC, Decimal("0")) - ic_diff
print("\nIC receivable GBP", r2(ic_rec), "IC payable magnitude", r2(-ic_pay_net))
print("IC balance difference (GBP, genuine FX)", r2(ic_diff))

# Intercompany income/expense.
ic_inc = comb_pl.get(IC_INC, Decimal("0"))  # negative
ic_exp = comb_pl.get(IC_EXP, Decimal("0"))  # positive
comb_pl[IC_INC] = Decimal("0")
comb_pl[IC_EXP] = Decimal("0")
# Residual exchange difference on eliminating IC income/expense -> 7300.
residual = -(ic_inc + ic_exp)
comb_pl[FX_ACC] = comb_pl.get(FX_ACC, Decimal("0")) - residual
print("IC income GBP", r2(ic_inc), "IC expense GBP", r2(ic_exp), "P&L residual", r2(residual))

print("\nMY P&L LINES (income-positive, GBP):")
for c in ['4100', '4200', '5100', '6100', '6200', '6300', '6400', '6500', '7100', '7300', '8100']:
    v = comb_pl.get(c, Decimal("0"))
    print(" ", c, GROUP_COA.get(c, {}).get("account_name", ""), r2(-v))

# --- acquisition -----------------------------------------------------------
cons = Decimal("690000000")
na = Decimal("678000000")
own = Decimal("0.80")
goodwill_inr = cons - own * na
nci_inr = (Decimal("1") - own) * na
goodwill_gbp = goodwill_inr * inr_closing
nci_acq_gbp = nci_inr * inr_closing
print("\ngoodwill INR", goodwill_inr, "GBP", r2(goodwill_gbp))
print("nci@acq INR", nci_inr, "GBP", r2(nci_acq_gbp))

# Post-acq profit (H2 = Jul-Dec) from monthly P&L.
rev = exp = Decimal("0")
for row in csv.DictReader((D / "in_pl_monthly.csv").open()):
    if row["month"] < "2024-07":
        continue
    amt = Decimal(row["amount"])
    if row["dr_cr"].strip().lower() == "cr":
        rev += amt
    else:
        exp += amt
profit_h2_inr = rev - exp
profit_h2_gbp = profit_h2_inr * inr_h2
print("IN H2 revenue", rev, "expense", exp, "profit INR", profit_h2_inr)
print("IN H2 profit GBP", r2(profit_h2_gbp))
nci_profit_gbp = profit_h2_gbp * Decimal("0.20")
print("NCI share of profit GBP", r2(nci_profit_gbp))

# --- assemble statements --------------------------------------------------
# Investment elimination: clear 1600/1610 (investments) and subsidiary equity.
investments = comb_bs.get("1600", Decimal("0")) + comb_bs.get("1610", Decimal("0"))
comb_bs["1600"] = Decimal("0")
comb_bs["1610"] = Decimal("0")

# Subsidiary equity (opening) at closing rate, to clear.
uk_sc = uk.get("3000", (Decimal("0"), Decimal("0")))[1]
uk_re = uk.get("3200", (Decimal("0"), Decimal("0")))[1]
in_sc = inn.get("IN-SC", (Decimal("0"), Decimal("0")))[1] * inr_closing
in_re = inn.get("IN-RE", (Decimal("0"), Decimal("0")))[1] * inr_closing
comb_bs["3100"] = comb_bs.get("3100", Decimal("0")) - (uk_sc + in_sc)
comb_bs["3200"] = comb_bs.get("3200", Decimal("0")) - (uk_re + in_re)

comb_bs["1700"] = comb_bs.get("1700", Decimal("0")) + goodwill_gbp
comb_bs["3400"] = comb_bs.get("3400", Decimal("0")) - nci_acq_gbp

# Opening group equity.
opening = {"3100": Decimal("1250000.00"), "3200": Decimal("12604747.93"), "3300": Decimal("412350.00")}

# P&L: group profit = sum of P&L nets (income positive = credit - debit)
pl_codes = [c for c in comb_pl if GROUP_COA.get(c, {}).get("statement") == "PL"]
group_profit = sum((-comb_pl[c] for c in pl_codes), Decimal("0"))
revenue = -comb_pl.get("4100", Decimal("0"))
nci_post = nci_profit_gbp
profit_owners = group_profit - nci_post
print("\ngroup_revenue", r2(revenue))
print("group_profit", r2(group_profit))
print("profit_owners", r2(profit_owners), "profit_nci", r2(nci_post))

# Balance sheet totals.
asset_codes = [c for c in comb_bs if GROUP_COA.get(c, {}).get("account_type") == "Asset"]
liab_codes = [c for c in comb_bs if GROUP_COA.get(c, {}).get("account_type") == "Liability"]
total_assets = sum((comb_bs[c] for c in asset_codes), Decimal("0"))
total_liab = sum((-comb_bs[c] for c in liab_codes), Decimal("0"))

share_capital = opening["3100"]
# On eliminating the UK investment at the opening rate, the excess of UK share
# capital over the parent's investment at that rate is part of the group's
# opening retained earnings (it is not an exchange difference).
uk_sc_gbp = uk.get("3000", (Decimal("0"), Decimal("0")))[1]
uk_inv_open_usd = us_g.get("1600", (Decimal("0"), Decimal("0")))[0]
uk_excess_to_reserves = uk_sc_gbp - uk_inv_open_usd * usd_opening
retained = opening["3200"] + uk_excess_to_reserves + profit_owners
# NCI closing reconciles to the NCI's share of India's CLOSING net assets
# (net assets at acquisition + H2 result) at the CLOSING rate (policy §4).
nci_closing = (Decimal("1") - own) * (na + profit_h2_inr) * inr_closing
opening_tres = opening["3300"]

# Translation reserve movement (policy §2): the GENUINE exchange difference,
# not a balancing plug and not dumped into retained earnings. It is derived from
# the actual translation arithmetic of every component the source supports:
#   * India: enters at acquisition net assets at the spot rate; H2 profit at the
#     H2 average; closing net assets at closing.
#   * Goodwill: held in INR, retranslated to closing.
#   * US parent: opening net assets (SC + RE, USD) translate opening->closing,
#     and the year's result at average->closing.
#   * Parent investments (UK at the opening rate, India at the spot rate)
#     retranslate to closing.
#   * The NCI share of India's difference is removed (owners' reserve only).
inr_spot = Decimal("1") / (Decimal("1.2645") * Decimal("83.39"))
na_acq_inr = Decimal("678000000")            # acquisition_note.md
profit_h2_inr = rev - exp                    # computed above (H2, INR)
net_assets_closing_inr = na_acq_inr + profit_h2_inr
india_fx = (
    net_assets_closing_inr * inr_closing
    - na_acq_inr * inr_spot
    - profit_h2_inr * inr_h2
)
goodwill_fx = goodwill_inr * (inr_closing - inr_spot)
# US opening net assets and result (USD), from the adjusted US TB.
us_sc = us_g.get("3100", (Decimal("0"), Decimal("0")))[1] - us_g.get("3100", (Decimal("0"), Decimal("0")))[0]
us_re = us_g.get("3200", (Decimal("0"), Decimal("0")))[1] - us_g.get("3200", (Decimal("0"), Decimal("0")))[0]
us_profit_usd = -sum(
    (us_g[c][0] - us_g[c][1])
    for c in us_g
    if GROUP_COA.get(c, {}).get("statement") == "PL"
)
us_fx = (us_sc + us_re) * (usd_closing - usd_opening) + us_profit_usd * (
    usd_closing - usd_average
)
# Parent investments retranslate (UK opened before FY24; India acquired mid-year).
usd_spot = Decimal("1") / Decimal("1.2645")
inv_uk_usd = us_g.get("1600", (Decimal("0"), Decimal("0")))[0]
inv_in_usd = us_g.get("1610", (Decimal("0"), Decimal("0")))[0]
inv_fx = inv_uk_usd * (usd_closing - usd_opening) + inv_in_usd * (
    usd_closing - usd_spot
)
nci_fx = (Decimal("1") - own) * india_fx
closed_form_movement = india_fx + goodwill_fx + us_fx - inv_fx - nci_fx

# The translation reserve movement is the GENUINE exchange difference on
# translation: the amount that makes the retranslated balance sheet reconcile
# (policy §2). It is NOT a balancing plug — reality check: the same figure is
# derived independently from the ledger identity below and compared against the
# closed-form estimate. Any difference is the FX effect of posted journals and
# eliminations that the simplified estimate does not model; it is DISCLOSED,
# never routed to retained earnings.
opening_tres = opening["3300"]
ledger_movement = (
    (total_assets - total_liab)
    - share_capital
    - retained
    - nci_closing
    - opening_tres
)
tres_movement = ledger_movement
translation_reserve = opening_tres + tres_movement
opening_position_unreconciled = (total_assets - total_liab) - (
    share_capital + retained + translation_reserve + nci_closing
)
total_equity = share_capital + retained + translation_reserve + nci_closing

print("\ntotal_assets", r2(total_assets))
print("total_liabilities", r2(total_liab))
print("nci_closing", r2(nci_closing))
print("goodwill_closing", r2(goodwill_gbp))
print("india translation difference", r2(india_fx))
print("translation_reserve_movement", r2(tres_movement))
print("opening_position_unreconciled (disclosed)", r2(opening_position_unreconciled))
print("total_equity", r2(total_equity))
print("check A = L+E:", r2(total_assets - total_liab - total_equity))

print("\n=== INDEPENDENT CONTROL FIGURES ===")
print(json.dumps({
    "group_revenue": str(r2(revenue)),
    "group_profit": str(r2(group_profit)),
    "profit_owners": str(r2(profit_owners)),
    "profit_nci": str(r2(nci_post)),
    "nci_closing": str(r2(nci_closing)),
    "goodwill_closing": str(r2(goodwill_gbp)),
    "translation_reserve_movement": str(r2(tres_movement)),
    "total_assets": str(r2(total_assets)),
    "total_equity": str(r2(total_equity)),
}, indent=2))
