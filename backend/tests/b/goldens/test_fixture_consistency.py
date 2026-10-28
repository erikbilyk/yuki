"""The base fixture reproduces the golden inputs, and each variant changes only what it should.

Everything here is plain arithmetic on the YAML files; no application code is imported.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Any

import pytest

from .data import by_id, date_at, epoch_date, load_expected, load_fixture, load_raw_fixture

VARIANTS = ["t25", "t35", "t42", "t43", "t52", "t64", "t65"]
HUNDRED = Decimal(100)
FEN_PLACES = Decimal(1)
PCT_PLACES = Decimal("0.0001")


@pytest.fixture(scope="module")
def fx() -> dict[str, Any]:
    return load_fixture()


@pytest.fixture(scope="module")
def exp() -> dict[str, Any]:
    return load_expected()


def money(amount: Decimal) -> int:
    """Round a fen-denominated Decimal half-up to whole fen."""
    return int(amount.quantize(FEN_PLACES, rounding=ROUND_HALF_UP))


def percent(part: Decimal | int, whole: Decimal | int) -> str:
    return str(
        (Decimal(part) / Decimal(whole) * HUNDRED).quantize(PCT_PLACES, rounding=ROUND_HALF_UP)
    )


def walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every leaf of a document with a dotted path (list items by id, else by index)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            label = index
            if isinstance(value, dict):
                label = value.get("id", value.get("source_txn_id", index))
            yield from walk(value, f"{path}.{label}")
    else:
        yield path, node


def changed_paths(base: dict[str, Any], other: dict[str, Any]) -> set[str]:
    left, right = dict(walk(base)), dict(walk(other))
    return {
        p for p in left.keys() | right.keys() if left.get(p, KeyError) != right.get(p, KeyError)
    }


def statement_sum(statement: dict[str, Any]) -> int:
    return sum(t["amount_fen"] for t in statement["transactions"])


def unreconciled(doc: dict[str, Any]) -> dict[str, int]:
    """Statement id -> opening + transactions - closing, for statements that do not reconcile."""
    gaps = {
        s["id"]: s["opening_fen"] + statement_sum(s) - s["closing_fen"] for s in doc["statements"]
    }
    return {sid: gap for sid, gap in gaps.items() if gap}


def fund_values(doc: dict[str, Any]) -> dict[str, Decimal]:
    """Value of each fund in fen (shares x NAV)."""
    nav = {p["id"]: Decimal(p["nav"]) for p in doc["products"]}
    return {
        pos["product"]: Decimal(pos["shares"]) * nav[pos["product"]] * HUNDRED
        for pos in doc["positions"]
    }


def sector_weight(doc: dict[str, Any], sector: str) -> dict[str, Decimal]:
    return {
        s["product"]: sum(
            (Decimal(r["weight"]) for r in s["rows"] if r["sector"] == sector), Decimal(0)
        )
        for s in doc["holdings_snapshots"]
    }


def issuer_weight(doc: dict[str, Any], issuer: str) -> dict[str, Decimal]:
    return {
        s["product"]: sum(
            (Decimal(r["weight"]) for r in s["rows"] if r["issuer"] == issuer), Decimal(0)
        )
        for s in doc["holdings_snapshots"]
    }


def redemption_fee(gross_fen: int, rate: str) -> int:
    return money(Decimal(gross_fen) * Decimal(rate))


def tier_rate(schedule: dict[str, Any], days_held: int) -> str:
    for tier in schedule["tiers"]:
        if tier["min_days"] <= days_held and (
            tier["max_days"] is None or days_held <= tier["max_days"]
        ):
            return tier["rate"]
    raise LookupError(days_held)


def swap_result(fx: dict[str, Any], sell: str, buy: str, gross_fen: int) -> dict[str, Any]:
    """Holdings after selling `gross_fen` of one fund (flat fee) and buying another with the net."""
    rate = by_id(fx["fee_schedules"])["fee_flat_v1"]["tiers"][0]["rate"]
    fee = redemption_fee(gross_fen, rate)
    values = fund_values(fx)
    values[sell] -= gross_fen
    values[buy] += gross_fen - fee
    return {"values": values, "fee_fen": fee, "net_fen": gross_fen - fee}


def technology_after(fx: dict[str, Any], values: dict[str, Decimal]) -> Decimal:
    weights = sector_weight(fx, "technology")
    return sum((values[p] * weights[p] for p in values), Decimal(0))


# --- structure -------------------------------------------------------------


def test_top_level_keys(fx: dict[str, Any]) -> None:
    assert set(fx) == {
        "profile",
        "epoch",
        "customers",
        "operators",
        "accounts",
        "payees",
        "statements",
        "commitments",
        "reserve",
        "allowance",
        "margin_fen",
        "income_this_month_fen",
        "spent_this_month_fen",
        "products",
        "fee_schedules",
        "positions",
        "lots",
        "holdings_snapshots",
        "plan_constants",
    }
    assert fx["profile"] == "fixture_v1"
    assert epoch_date(fx) == date(2026, 11, 2)
    assert set(load_raw_fixture()["variants"]) == set(VARIANTS)


@pytest.mark.parametrize("variant", [None, *VARIANTS])
def test_no_floats_and_integer_fen(variant: str | None) -> None:
    doc = load_fixture(variant)
    for path, value in walk(doc):
        assert not isinstance(value, float), path
        if path.rsplit(".", 1)[-1].endswith("_fen"):
            assert type(value) is int, path


def test_decimal_fields_are_strings_with_four_places(fx: dict[str, Any]) -> None:
    decimals = [p["nav"] for p in fx["products"]]
    decimals += [p["shares"] for p in fx["positions"]] + [lot["shares"] for lot in fx["lots"]]
    decimals += [s["unresolved_weight"] for s in fx["holdings_snapshots"]]
    decimals += [r["weight"] for s in fx["holdings_snapshots"] for r in s["rows"]]
    decimals += [sched["purchase_rate"] for sched in fx["fee_schedules"]]
    decimals += [t["rate"] for sched in fx["fee_schedules"] for t in sched["tiers"]]
    decimals += [fx["plan_constants"]["target_max_pct"]]
    for text in decimals:
        assert isinstance(text, str), text
        assert Decimal(text).as_tuple().exponent == -4, text


def test_ids_are_unique_and_prefixed(fx: dict[str, Any]) -> None:
    prefixes = {
        "customers": "cus_",
        "operators": "opr_",
        "accounts": "acc_",
        "payees": "pay_",
        "statements": "stm_",
        "commitments": "cmt_",
    }
    for section, prefix in prefixes.items():
        ids = [item["id"] for item in fx[section]]
        assert len(ids) == len(set(ids)), section
        assert all(i.startswith(prefix) for i in ids), section
    codes = [p["id"] for p in fx["products"]]
    assert all(isinstance(c, str) and len(c) == 6 and c.isdigit() for c in codes)


def test_references_resolve(fx: dict[str, Any]) -> None:
    customers = by_id(fx["customers"])
    accounts = by_id(fx["accounts"])
    payees = by_id(fx["payees"])
    products = by_id(fx["products"])
    schedules = by_id(fx["fee_schedules"])
    for item in fx["accounts"] + fx["payees"] + fx["commitments"]:
        assert item["customer"] in customers
    for statement in fx["statements"]:
        assert statement["account"] in accounts
    for commitment in fx["commitments"]:
        assert payees[commitment["payee"]]["customer"] == commitment["customer"]
    for position in fx["positions"]:
        assert position["account"] in accounts and position["product"] in products
    for lot in fx["lots"]:
        assert lot["product"] in products
    for snapshot in fx["holdings_snapshots"]:
        assert snapshot["product"] in products
    for product in fx["products"]:
        assert product["fee_schedule"] in schedules
    purposes = {c["purpose_id"] for c in fx["commitments"] if c["status"] == "CONFIRMED"}
    assert set(fx["allowance"]["excluded_purpose_ids"]) <= purposes


def test_logins(fx: dict[str, Any]) -> None:
    logins = fx["customers"] + fx["operators"]
    handles = [login["handle"] for login in logins]
    assert len(handles) == len(set(handles))
    assert {c["id"] for c in fx["customers"]} == {"cus_main", "cus_other"}
    assert sorted(o["role"] for o in fx["operators"]) == ["demo", "operator"]
    assert all(login["password"] for login in logins)


def test_payee_labels(fx: dict[str, Any]) -> None:
    payees = by_id(fx["payees"])
    assert payees["pay_mom"]["bank"] == "same" and payees["pay_mom"]["account_masked"] == "··1234"
    assert payees["pay_dad"]["bank"] == "other" and payees["pay_dad"]["account_masked"] == "··5678"
    assert payees["pay_wanglei"]["bank"] == "same"
    assert payees["pay_liming"]["bank"] == "other"
    assert {p["bank"] for p in fx["payees"]} == {"same", "other"}
    wang = [p for p in fx["payees"] if p["customer"] == "cus_main" and p["alias"].startswith("王")]
    assert len(wang) == 2


# --- statements ------------------------------------------------------------


def test_statements_reconcile_and_chain(fx: dict[str, Any]) -> None:
    assert unreconciled(fx) == {}
    accounts = by_id(fx["accounts"])
    for account_id, account in accounts.items():
        chain = [s for s in fx["statements"] if s["account"] == account_id]
        assert chain, account_id
        for previous, current in pairwise(chain):
            assert current["opening_fen"] == previous["closing_fen"], current["id"]
        assert chain[-1]["closing_fen"] == account["opening_fen"], account_id


def test_statement_periods_and_dates(fx: dict[str, Any]) -> None:
    main = [s for s in fx["statements"] if s["account"] == "acc_main"]
    assert [s["period"] for s in main] == [
        "2026-08-01/2026-08-31",
        "2026-09-01/2026-09-30",
        "2026-10-01/2026-10-31",
        "2026-11-01/2026-11-02",
    ]
    for statement in fx["statements"]:
        start, end = (date.fromisoformat(part) for part in statement["period"].split("/"))
        for txn in statement["transactions"]:
            assert start <= date_at(fx, txn["posted_offset_days"]) <= end, txn["source_txn_id"]


def test_source_txn_ids_are_unique_per_account(fx: dict[str, Any]) -> None:
    seen: set[tuple[str, str]] = set()
    for statement in fx["statements"]:
        for txn in statement["transactions"]:
            key = (statement["account"], txn["source_txn_id"])
            assert key not in seen, key
            seen.add(key)
            assert type(txn["amount_fen"]) is int and txn["amount_fen"] != 0


def test_current_month_income_and_spending(fx: dict[str, Any]) -> None:
    november = [s for s in fx["statements"] if s["id"] == "stm_main_2611"][0]
    txns = november["transactions"]
    salary = [t for t in txns if t["category_hint"] == "salary"]
    assert [(t["amount_fen"], t["posted_offset_days"]) for t in salary] == [(1200000, -1)]
    assert sum(t["amount_fen"] for t in salary) == fx["income_this_month_fen"]
    spent = -sum(
        t["amount_fen"]
        for t in txns
        if t["amount_fen"] < 0 and t["category_hint"] != "own_transfer"
    )
    assert spent == fx["spent_this_month_fen"] == 60000


def test_unresolved_transfer_and_own_transfer(fx: dict[str, Any]) -> None:
    payees = by_id(fx["payees"])
    transactions = [(s["account"], t) for s in fx["statements"] for t in s["transactions"]]
    unresolved = [(a, t) for a, t in transactions if t["category_hint"] is None]
    assert len(unresolved) == 1
    account, txn = unresolved[0]
    assert (account, txn["amount_fen"], txn["counterparty"]) == (
        "acc_main",
        -200000,
        payees["pay_liming"]["alias"],
    )
    own = [(a, t) for a, t in transactions if t["category_hint"] == "own_transfer"]
    assert own, "an own-account transfer is part of the history"
    for account, txn in own:
        mirror = [
            t
            for a, t in own
            if a != account
            and t["posted_offset_days"] == txn["posted_offset_days"]
            and t["amount_fen"] == -txn["amount_fen"]
        ]
        assert len(mirror) == 1, txn["source_txn_id"]


def test_recurring_rent_history(fx: dict[str, Any]) -> None:
    rent = [t for s in fx["statements"] for t in s["transactions"] if t["category_hint"] == "rent"]
    assert [t["amount_fen"] for t in rent] == [-340000] * 3
    assert {t["counterparty"] for t in rent} == {"王磊"}


# --- cash inputs -----------------------------------------------------------


def test_cash_start_values(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    start = exp["cash"]["start"]
    covered = [a for a in fx["accounts"] if a["customer"] == "cus_main" and a["covered"]]
    posted = sum(a["opening_fen"] for a in covered)
    horizon_end = 30
    unpaid = sum(
        c["unpaid_fen"]
        for c in fx["commitments"]
        if c["customer"] == "cus_main"
        and c["status"] in {"CONFIRMED", "PARTIALLY_PAID"}
        and c["due_offset_days"] <= horizon_end
    )
    protected = (
        fx["reserve"]["target_fen"] + fx["allowance"]["remaining_fen"] + unpaid + fx["margin_fen"]
    )
    assert posted == start["c_fen"] == 1800000
    assert unpaid == start["b_fen"] == 840000
    assert protected == start["protected_fen"] == 1600000
    assert max(0, posted - protected) == start["headroom_fen"] == 200000
    assert max(0, protected - posted) == start["shortfall_fen"] == 0
    assert fx["reserve"]["target_fen"] == start["e_fen"]
    assert fx["allowance"]["remaining_fen"] == start["l_fen"]
    assert fx["margin_fen"] == start["m_fen"]


def test_commitments(fx: dict[str, Any]) -> None:
    bills = by_id(fx["commitments"])
    rent, insurance = bills["cmt_rent"], bills["cmt_insurance"]
    assert (rent["payee"], rent["amount_fen"], rent["due_offset_days"]) == (
        "pay_wanglei",
        340000,
        2,
    )
    assert (insurance["payee"], insurance["amount_fen"], insurance["due_offset_days"]) == (
        "pay_insurer",
        500000,
        7,
    )
    for bill in bills.values():
        assert bill["status"] == "CONFIRMED" and bill["unpaid_fen"] == bill["amount_fen"]
        assert date_at(fx, bill["due_offset_days"]).month == 11


def test_monthly_estimate_and_allowance(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    month = exp["cash"]["monthly_estimate"]
    due_this_month = sum(
        c["unpaid_fen"] for c in fx["commitments"] if date_at(fx, c["due_offset_days"]).month == 11
    )
    assert due_this_month == month["bills_due_this_month_fen"]
    estimate = fx["income_this_month_fen"] - (
        fx["spent_this_month_fen"] + fx["allowance"]["remaining_fen"] + due_this_month
    )
    assert estimate == month["value_fen"] == 0
    assert (
        fx["allowance"]["budget_fen"]
        == fx["allowance"]["remaining_fen"] + fx["spent_this_month_fen"]
    )
    assert fx["allowance"]["period"] == "2026-11"


def test_cash_goldens_are_arithmetically_consistent(exp: dict[str, Any]) -> None:
    cash = exp["cash"]
    start = cash["start"]
    assert (
        start["protected_fen"] == start["e_fen"] + start["l_fen"] + start["b_fen"] + start["m_fen"]
    )
    assert start["headroom_fen"] == start["c_fen"] - start["protected_fen"] - start["u_fen"]
    raised = cash["reserve_raised"]
    assert raised["e_fen"] - start["e_fen"] == 100000
    assert raised["protected_fen"] == start["protected_fen"] + 100000
    assert raised["headroom_fen"] == raised["c_fen"] - raised["protected_fen"]
    rent = 340000
    paid = cash["rent_paid"]
    assert (paid["c_fen"], paid["protected_fen"]) == (
        raised["c_fen"] - rent,
        raised["protected_fen"] - rent,
    )
    assert paid["headroom_fen"] == raised["headroom_fen"]
    part = cash["rent_partial_payment"]
    assert part["unpaid_fen"] == rent - part["paid_fen"]
    assert part["c_fen"] == start["c_fen"] - part["paid_fen"]
    assert part["protected_fen"] == start["protected_fen"] - part["paid_fen"]
    assert part["headroom_fen"] - start["headroom_fen"] == part["headroom_change_fen"] == 0


def test_s6_table_is_arithmetically_consistent(exp: dict[str, Any]) -> None:
    for branch in ("base", "branch_1200", "branch_1600"):
        for step, row in exp["s6"][branch].items():
            if isinstance(row, dict) and "c_fen" in row:
                assert row["headroom_fen"] == max(0, row["c_fen"] - row["protected_fen"]), (
                    branch,
                    step,
                )
    s6 = exp["s6"]
    assert s6["base"]["t0_start"] == {
        "c_fen": 1800000,
        "protected_fen": 1600000,
        "headroom_fen": 200000,
    }
    for column in ("branch_1200", "branch_1600"):
        for step in (
            "t0_start",
            "t0_plan_confirmed",
            "t0_redemption_submitted",
            "t2_rent_paid",
            "t3_redemption_settled",
        ):
            assert s6[column][step] == s6["base"][step], (column, step)
    settled = s6["base"]["t3_redemption_settled"]
    foreign = s6["foreign_debit_6000"]
    assert (
        max(0, settled["protected_fen"] - (settled["c_fen"] - foreign["debit_fen"]))
        == foreign["shortfall_fen"]
    )
    late = s6["bill_1200_after_purchase"]
    purchased = s6["base"]["t3_purchase"]
    assert (
        max(0, purchased["protected_fen"] + late["bill_fen"] - purchased["c_fen"])
        == late["shortfall_fen"]
    )
    small = s6["bill_500_without_bounds"]
    assert (
        settled["c_fen"] - (settled["protected_fen"] + small["bill_fen"]) == small["headroom_fen"]
    )
    for column, bill in (("branch_1200", 120000), ("branch_1600", 160000)):
        confirmed = s6[column]["t3_bill_confirmed"]
        assert confirmed["protected_fen"] == settled["protected_fen"] + bill
    revised = s6["branch_1200"]["t3_purchase"]
    assert revised["c_fen"] == settled["c_fen"] - revised["purchase_fen"]
    assert (
        s6["base"]["t3_purchase"]["c_fen"]
        == settled["c_fen"] - exp["funds"]["base_plan"]["purchase_fen"]
    )
    for column in ("base", "branch_1200", "branch_1600"):
        before = s6[column].get("t3_purchase") or s6[column]["t3_bill_confirmed"]
        after = s6[column]["t7_insurance_paid"]
        assert before["c_fen"] - after["c_fen"] == 500000
        assert before["protected_fen"] - after["protected_fen"] == 500000


# --- funds -----------------------------------------------------------------


def test_fund_values_and_exposure(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    codes = exp["products"]
    values = fund_values(fx)
    assert {letter: int(values[code]) for letter, code in codes.items()} == exp["funds"][
        "holdings_value_fen"
    ]
    total = sum(values.values())
    assert total == exp["funds"]["total_value_fen"] == 5000000
    technology = technology_after(fx, values)
    assert technology == exp["funds"]["technology"]["value_fen"] == 2275000
    assert percent(technology, total) == exp["funds"]["technology"]["pct"] == "45.5000"
    issuer = sum((values[p] * w for p, w in issuer_weight(fx, "发行人X").items()), Decimal(0))
    assert issuer == exp["funds"]["issuer_x"]["value_fen"] == 355000
    assert percent(issuer, total) == exp["funds"]["issuer_x"]["pct"]


def test_documented_weights(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    codes = exp["products"]
    technology = sector_weight(fx, "technology")
    assert technology[codes["C"]] == Decimal("0.7000") and technology[codes["D"]] == Decimal(
        "0.1000"
    )
    # The split between A and B is a reconstruction: only this sum is documented.
    a, b = Decimal("20000") * technology[codes["A"]], Decimal("15000") * technology[codes["B"]]
    assert a + b == Decimal("15250")
    assert all(Decimal("0.16") <= technology[codes[k]] <= Decimal("0.65") for k in "AB")
    issuer = issuer_weight(fx, "发行人X")
    assert [str(issuer[codes[k]]) for k in "ABCD"] == ["0.0800", "0.0700", "0.0600", "0.0600"]


def test_holdings_rows(fx: dict[str, Any]) -> None:
    identity: dict[str, tuple[str, str]] = {}
    assert {s["product"] for s in fx["holdings_snapshots"]} == {p["id"] for p in fx["products"]}
    for snapshot in fx["holdings_snapshots"]:
        assert snapshot["executable"] is True
        assert Decimal(snapshot["unresolved_weight"]) == 0
        assert sum(Decimal(r["weight"]) for r in snapshot["rows"]) == 1
        x_rows = [r for r in snapshot["rows"] if r["issuer"] == "发行人X"]
        assert len(x_rows) == 1 and x_rows[0]["sector"] == "technology"
        for row in snapshot["rows"]:
            assert identity.setdefault(row["security_code"], (row["issuer"], row["sector"])) == (
                row["issuer"],
                row["sector"],
            )
            assert Decimal(row["weight"]) > 0


def test_positions_and_lots(fx: dict[str, Any]) -> None:
    for product in fx["products"]:
        shares = sum(Decimal(p["shares"]) for p in fx["positions"] if p["product"] == product["id"])
        lots = [lot for lot in fx["lots"] if lot["product"] == product["id"]]
        assert len(lots) == 1
        assert sum(Decimal(lot["shares"]) for lot in lots) == shares
        assert lots[0]["acquired_offset_days"] == -60
        assert lots[0]["cost_fen"] == int(shares * Decimal(product["nav"]) * HUNDRED)
        assert (product["share_class"], product["nav"], product["nav_date_offset_days"]) == (
            "C",
            "1.0000",
            0,
        )
        assert (
            product["redeem_settle_days"],
            product["purchase_settle_days"],
            product["min_purchase_fen"],
        ) == (3, 0, 10000)


def test_fee_schedules(fx: dict[str, Any]) -> None:
    schedules = by_id(fx["fee_schedules"])
    flat, public = schedules["fee_flat_v1"], schedules["fee_public_c_v1"]
    assert flat["terms_version"] == "synthetic_0.3pct_v1"
    assert {tier_rate(flat, days) for days in (1, 20, 60)} == {"0.0030"}
    expected = {1: "0.0150", 6: "0.0150", 7: "0.0050", 29: "0.0050", 30: "0.0000", 60: "0.0000"}
    assert {days: tier_rate(public, days) for days in expected} == expected
    assert {p["fee_schedule"] for p in fx["products"]} == {"fee_flat_v1"}
    assert all(Decimal(s["purchase_rate"]) == 0 for s in fx["fee_schedules"])


def test_redemption_golden(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    redemption = exp["funds"]["redemption"]
    product = by_id(fx["products"])[exp["products"][redemption["product"]]]
    gross = money(Decimal(redemption["shares"]) * Decimal(product["nav"]) * HUNDRED)
    rate = by_id(fx["fee_schedules"])[product["fee_schedule"]]["tiers"][0]["rate"]
    fee = redemption_fee(gross, rate)
    assert (gross, fee, gross - fee) == (
        redemption["gross_fen"],
        redemption["fee_fen"],
        redemption["net_fen"],
    )
    assert redemption_fee(1, "0.0030") == 0 and redemption_fee(1000000, "0.00305") == 3050


def test_plan_constants_match_goldens(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    plan = fx["plan_constants"]
    bounds = plan["bounds"]
    redemption = exp["funds"]["redemption"]
    s6 = exp["s6"]["base"]
    assert bounds["p0_fen"] == redemption["net_fen"] == exp["funds"]["base_plan"]["purchase_fen"]
    assert bounds["spare0_fen"] == s6["t0_plan_confirmed"]["headroom_fen"]
    assert bounds["min_purchase_fen"] == bounds["p0_fen"] - bounds["max_retained_fen"]
    assert plan["reserve_after_plan_fen"] == exp["cash"]["reserve_raised"]["e_fen"]
    assert (
        plan["target_max_pct"],
        plan["checkpoint_day"],
        plan["fee_cap_fen"],
        plan["turnover_cap_fen"],
        plan["expiry_day"],
    ) == (
        "40.0000",
        5,
        2000,
        1000000,
        8,
    )
    assert exp["revision_sequence"]["bounds"] == bounds
    assert redemption["fee_fen"] <= plan["fee_cap_fen"]


@pytest.mark.parametrize(
    ("golden", "purchase"),
    [("base_plan", 498500), ("revision", 478500), ("hypothetical_purchase", 438500)],
)
def test_plan_outcome_goldens(
    fx: dict[str, Any], exp: dict[str, Any], golden: str, purchase: int
) -> None:
    codes = exp["products"]
    values = fund_values(fx)
    values[codes["C"]] -= 500000
    values[codes["D"]] += purchase
    assets = sum(values.values())
    technology = technology_after(fx, values)
    want = exp["funds"][golden]
    assert want["purchase_fen"] == purchase
    assert percent(technology, assets) == want["technology_pct"]
    if "technology_fen" in want:
        assert technology == want["technology_fen"] and assets == want["assets_fen"]
        assert percent(values[codes["D"]], assets) == want["d_weight_pct"]
        assert 500000 + purchase == want["turnover_fen"]


def test_no_purchase_branch_golden(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    codes = exp["products"]
    values = fund_values(fx)
    values[codes["C"]] -= 500000
    assets = sum(values.values())
    want = exp["funds"]["no_purchase_branch"]
    assert (technology_after(fx, values), assets) == (want["technology_fen"], want["assets_fen"])
    assert percent(technology_after(fx, values), assets) == want["technology_pct"] == "42.7778"


def test_swap_c_to_d_4000_fails_target(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    codes = exp["products"]
    result = swap_result(fx, codes["C"], codes["D"], 400000)
    pct = percent(technology_after(fx, result["values"]), sum(result["values"].values()))
    want = exp["funds"]["swap_c_to_d_4000"]
    assert pct == want["technology_pct"]
    assert (Decimal(pct) <= Decimal(fx["plan_constants"]["target_max_pct"])) is want[
        "passes_target"
    ]


def test_candidate_grid_has_exactly_one_passing_swap(
    fx: dict[str, Any], exp: dict[str, Any]
) -> None:
    plan = fx["plan_constants"]
    grid = exp["funds"]["candidate_grid"]
    codes = [p["id"] for p in fx["products"]]
    headroom = exp["s6"]["base"]["t3_redemption_settled"]["headroom_fen"]
    held = fund_values(fx)
    passing = []
    swaps = 0
    for sell in codes:
        for buy in codes:
            if sell == buy:
                continue
            for gross in grid["amounts_fen"]:
                swaps += 1
                result = swap_result(fx, sell, buy, gross)
                assets = sum(result["values"].values())
                share = technology_after(fx, result["values"]) / assets * HUNDRED
                if (
                    gross <= held[sell]
                    and share <= Decimal(plan["target_max_pct"])
                    and result["fee_fen"] <= plan["fee_cap_fen"]
                    and gross + result["net_fen"] <= plan["turnover_cap_fen"]
                    and result["net_fen"] <= headroom
                ):
                    passing.append((sell, buy, gross))
    assert swaps == grid["swaps"] == grid["ordered_pairs"] * len(grid["amounts_fen"])
    letters = {code: letter for letter, code in exp["products"].items()}
    assert [(letters[s], letters[b], g) for s, b, g in passing] == [
        (item["sell"], item["buy"], item["gross_fen"]) for item in grid["passing"]
    ]


def test_threshold_and_single_leg_goldens(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    codes = exp["products"]
    weights = sector_weight(fx, "technology")
    rate = Decimal(by_id(fx["fee_schedules"])["fee_flat_v1"]["tiers"][0]["rate"])
    target = Decimal(fx["plan_constants"]["target_max_pct"]) / HUNDRED
    values = fund_values(fx)
    total = sum(values.values())
    excess = technology_after(fx, values) - target * total
    delta = weights[codes["C"]] - weights[codes["D"]]
    # Selling x of C and buying D with x(1 - rate): excess <= x * (delta + rate * (w_D - target)).
    slope = delta + rate * (weights[codes["D"]] - target)
    threshold = excess / slope
    golden = exp["funds"]["continuous_threshold"]
    assert abs(threshold - golden["gross_fen"]) < Decimal("0.5")
    assert abs((Decimal(500000) - threshold) - golden["overpay_vs_grid_fen"]) < Decimal("0.5")
    single = exp["funds"]["single_leg"]
    sell_only = excess / (weights[codes["C"]] - target)
    buy_only = excess / (target - weights[codes["D"]])
    for needed in (sell_only, buy_only):
        assert abs(needed - single["needed_gross_fen"]) < Decimal("0.5")
    fee = redemption_fee(single["needed_gross_fen"], str(rate))
    assert fee == single["sell_only_fee_fen"] > fx["plan_constants"]["fee_cap_fen"]
    headroom = exp["s6"]["base"]["t3_redemption_settled"]["headroom_fen"]
    assert single["needed_gross_fen"] > headroom


def test_stress_goldens(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    codes = [p["id"] for p in fx["products"]]
    letters = exp["products"]
    stress = exp["funds"]["stress"]
    fee = exp["funds"]["redemption"]["fee_fen"]
    scenarios = {"no_trade": (fund_values(fx), 0)}
    for name, purchase in (("base", 498500), ("revision", 478500)):
        values = fund_values(fx)
        values[letters["C"]] -= 500000
        values[letters["D"]] += purchase
        scenarios[name] = (values, fee)
    for shock, percents in stress["shocks_pct"].items():
        for name, (values, trade_fee) in scenarios.items():
            loss = (
                sum(
                    (
                        values[code] * Decimal(p) / HUNDRED
                        for code, p in zip(codes, percents, strict=True)
                    ),
                    Decimal(0),
                )
                + trade_fee
            )
            assert loss == stress["loss_fen"][shock][name], (shock, name)
    target = stress["loss_fen"]["target_fund"]
    assert (
        target["base"] - target["no_trade"]
        == stress["adverse_target_fund_vs_no_trade_fen"]
        == 175900
    )


def test_revision_sequence_goldens(exp: dict[str, Any]) -> None:
    bounds = exp["revision_sequence"]["bounds"]
    for step in exp["revision_sequence"]["steps"] + exp["revision_sequence"]["repeats"]:
        x = step["x_fen"]
        candidate = bounds["p0_fen"] - max(0, x - bounds["spare0_fen"])
        reasons = []
        if candidate != bounds["p0_fen"]:
            if x > bounds["max_added_fen"]:
                reasons.append("BILL_INCREASE_OVER_LIMIT")
            if bounds["p0_fen"] - candidate > bounds["max_retained_fen"]:
                reasons.append("RETENTION_OVER_LIMIT")
            if candidate < bounds["min_purchase_fen"]:
                reasons.append("PURCHASE_BELOW_MIN")
            if candidate > step["headroom_fen"]:
                reasons.append("INSUFFICIENT_HEADROOM")
        decision = (
            "pause" if reasons else ("unchanged" if candidate == step["current_fen"] else "revise")
        )
        assert (decision, candidate) == (step["decision"], step["purchase_fen"]), step["event"]
        assert reasons == step.get("reasons", []), step["event"]
    settled = exp["s6"]["base"]["t3_redemption_settled"]["headroom_fen"]
    first, second, third = exp["revision_sequence"]["steps"]
    assert [s["headroom_fen"] for s in (first, second, third)] == [
        settled - 80000,
        settled - 120000,
        settled - 160000,
    ]
    assert exp["revision_sequence"]["rejected_sketch_purchase_fen"] == 458500


# --- variants --------------------------------------------------------------

ALLOWED_CHANGES = {
    "t25": (
        "accounts.acc_main.opening_fen",
        "accounts.acc_savings.opening_fen",
        "statements.stm_main_2611.closing_fen",
        "statements.stm_main_2611.transactions.",
        "statements.stm_sav_2611.closing_fen",
        "statements.stm_sav_2611.transactions.",
    ),
    "t35": ("statements.stm_main_2610.transactions.MAIN-202610-009.",),
    "t42": ("lots.lot_c1.acquired_offset_days", "products.990003.fee_schedule"),
    "t43": ("lots.lot_c1.",),
    "t52": tuple(
        f"products.{code}.purchase_settle_days" for code in ("990001", "990002", "990003", "990004")
    ),
    "t64": tuple(
        f"products.{code}.redeem_settle_days" for code in ("990001", "990002", "990003", "990004")
    ),
    "t65": tuple(
        f"products.{code}.redeem_settle_days" for code in ("990001", "990002", "990003", "990004")
    ),
}


@pytest.mark.parametrize("variant", VARIANTS)
def test_variant_changes_only_its_paths(fx: dict[str, Any], variant: str) -> None:
    changes = changed_paths(fx, load_fixture(variant))
    assert changes, variant
    stray = {p for p in changes if not p.startswith(ALLOWED_CHANGES[variant])}
    assert stray == set(), variant
    for prefix in ALLOWED_CHANGES[variant]:
        assert any(p.startswith(prefix) for p in changes), (variant, prefix)


def test_applying_a_variant_leaves_the_base_untouched(fx: dict[str, Any]) -> None:
    before = load_fixture()
    for variant in VARIANTS:
        load_fixture(variant)
    assert load_fixture() == before == fx


def test_t25_moves_money_between_accounts(exp: dict[str, Any]) -> None:
    doc = load_fixture("t25")
    want = exp["variants"]["t25"]
    accounts = by_id(doc["accounts"])
    assert accounts["acc_main"]["opening_fen"] == want["acc_main_posted_fen"]
    assert accounts["acc_savings"]["opening_fen"] == want["acc_savings_posted_fen"]
    assert (
        accounts["acc_main"]["opening_fen"] + accounts["acc_savings"]["opening_fen"]
        == want["total_posted_fen"]
    )
    rent = by_id(doc["commitments"])["cmt_rent"]["amount_fen"]
    assert accounts["acc_main"]["opening_fen"] < rent <= want["total_posted_fen"]
    assert unreconciled(doc) == {}
    november = [
        t for s in doc["statements"] if s["id"] == "stm_main_2611" for t in s["transactions"]
    ]
    assert (
        -sum(
            t["amount_fen"]
            for t in november
            if t["amount_fen"] < 0 and t["category_hint"] != "own_transfer"
        )
        == doc["spent_this_month_fen"]
    )
    assert (
        sum(t["amount_fen"] for t in november if t["category_hint"] == "salary")
        == doc["income_this_month_fen"]
    )


def test_t35_leaves_one_statement_unreconciled(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    want = exp["variants"]["t35"]
    doc = load_fixture("t35")
    assert unreconciled(doc) == {"stm_main_2610": want["imbalance_fen"]}
    removed = [
        t
        for s in fx["statements"]
        for t in s["transactions"]
        if t["source_txn_id"] == want["missing_source_txn_id"]
    ]
    assert len(removed) == 1 and removed[0]["amount_fen"] == -want["imbalance_fen"]
    assert removed[0]["category_hint"] is not None
    assert [a["opening_fen"] for a in doc["accounts"]] == [a["opening_fen"] for a in fx["accounts"]]


def test_t42_tiered_fee_on_young_lots(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    doc = load_fixture("t42")
    schedules = by_id(doc["fee_schedules"])
    product = by_id(doc["products"])["990003"]
    lot = by_id(doc["lots"])["lot_c1"]
    days_held = -lot["acquired_offset_days"]
    rate = tier_rate(schedules[product["fee_schedule"]], days_held)
    fee = redemption_fee(500000, rate)
    assert (days_held, rate) == (20, "0.0050")
    assert fee == exp["variants"]["t42"]["redemption_fee_fen"] == 2500
    assert fee > doc["plan_constants"]["fee_cap_fen"]
    base_lot = by_id(fx["lots"])["lot_c1"]
    assert tier_rate(schedules["fee_public_c_v1"], -base_lot["acquired_offset_days"]) == "0.0000"


def test_t43_removes_only_the_lots_of_fund_c(fx: dict[str, Any], exp: dict[str, Any]) -> None:
    doc = load_fixture("t43")
    assert [lot["product"] for lot in doc["lots"] if lot["product"] == "990003"] == exp["variants"][
        "t43"
    ]["lots_for_product"]
    assert {lot["id"] for lot in fx["lots"]} - {lot["id"] for lot in doc["lots"]} == {"lot_c1"}
    assert doc["positions"] == fx["positions"]


@pytest.mark.parametrize(
    ("variant", "field"),
    [("t52", "purchase_settle_days"), ("t64", "redeem_settle_days"), ("t65", "redeem_settle_days")],
)
def test_settlement_variants(exp: dict[str, Any], variant: str, field: str) -> None:
    doc = load_fixture(variant)
    assert {p[field] for p in doc["products"]} == {exp["variants"][variant][field]}
    other = "redeem_settle_days" if field == "purchase_settle_days" else "purchase_settle_days"
    base = {p["id"]: p[other] for p in load_fixture()["products"]}
    assert {p["id"]: p[other] for p in doc["products"]} == base
