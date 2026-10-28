"""The cash goldens of ACCEPTANCE §3.1 and §3.3, computed from fixture inputs by the engine."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from wg.contracts.engines import CashAccountInput, CashInputs, CommitmentInput
from wg.contracts.enums import BillStatus, CommitmentStatus
from wg.numeric import compute_snapshot

from ..builders import bill, fixture_inputs, hold
from .data import load_expected, load_fixture

EXP = load_expected()
CASH = EXP["cash"]
S6 = EXP["s6"]
PLAN = load_fixture()["plan_constants"]
PURCHASE_FEN = EXP["funds"]["base_plan"]["purchase_fen"]
RENT_ID = "cmt_rent"
INSURANCE_ID = "cmt_insurance"


def move_cash(inp: CashInputs, delta_fen: int) -> CashInputs:
    """Add `delta_fen` to the main account's posted balance."""
    main, *rest = inp.accounts
    moved = main.model_copy(update={"posted_fen": main.posted_fen + delta_fen})
    return inp.model_copy(update={"accounts": (moved, *rest)})


def with_commitment(inp: CashInputs, commitment_id: str, **changes: Any) -> CashInputs:
    updated = tuple(
        c.model_copy(update=changes) if c.commitment_id == commitment_id else c
        for c in inp.commitments
    )
    return inp.model_copy(update={"commitments": updated})


def add_bill(inp: CashInputs, fen: int, due_in_days: int = 9) -> CashInputs:
    new = bill("new", fen, (inp.as_of + timedelta(days=due_in_days)).date())
    return inp.model_copy(update={"commitments": (*inp.commitments, new)})


def pay_in_full(inp: CashInputs, commitment_id: str, fen: int) -> CashInputs:
    paid = with_commitment(inp, commitment_id, status=CommitmentStatus.PAID, unpaid_fen=0)
    return move_cash(paid, -fen)


def amounts(inp: CashInputs) -> dict[str, int]:
    snap = compute_snapshot(inp)
    return {
        "c_fen": snap.c_fen,
        "protected_fen": snap.protected_fen,
        "headroom_fen": snap.headroom_fen,
    }


def expected(entry: dict[str, Any]) -> dict[str, int]:
    return {k: entry[k] for k in ("c_fen", "protected_fen", "headroom_fen")}


# ACCEPTANCE §3.1 (T17, T22, T32)


def test_start_values() -> None:
    snap = compute_snapshot(fixture_inputs())
    start = CASH["start"]
    for name in ("c", "e", "l", "b", "m", "u", "protected", "headroom", "shortfall"):
        assert getattr(snap, f"{name}_fen") == start[f"{name}_fen"], name
    assert snap.quality.incomplete is False
    assert all(snap.quality.reconciled.values())
    assert {b.status for b in snap.bills} == {BillStatus.SECURED}


def test_reserve_raised_to_5000() -> None:
    inp = fixture_inputs().model_copy(update={"reserve_fen": 500_000})
    assert amounts(inp) == expected(CASH["reserve_raised"] | {"c_fen": 1_800_000})
    assert compute_snapshot(inp).e_fen == CASH["reserve_raised"]["e_fen"]


def test_rent_paid_with_the_reserve_at_5000() -> None:
    # The rent-paid row of ACCEPTANCE §3.1 follows the reserve change: E is 5,000 in 13,600.
    inp = pay_in_full(fixture_inputs(), RENT_ID, 340_000).model_copy(
        update={"reserve_fen": 500_000}
    )
    assert amounts(inp) == expected(CASH["rent_paid"])
    statuses = {b.commitment_id: b.status for b in compute_snapshot(inp).bills}
    assert statuses == {RENT_ID: BillStatus.PAID, INSURANCE_ID: BillStatus.SECURED}


def test_rent_paid_with_the_reserve_at_4000_keeps_the_start_headroom() -> None:
    assert amounts(pay_in_full(fixture_inputs(), RENT_ID, 340_000)) == {
        "c_fen": 1_460_000,
        "protected_fen": 1_260_000,
        "headroom_fen": 200_000,
    }


def test_partial_payment_of_rent() -> None:
    part = CASH["rent_partial_payment"]
    start = compute_snapshot(fixture_inputs())
    inp = move_cash(
        with_commitment(
            fixture_inputs(), RENT_ID, status=CommitmentStatus.PARTIALLY_PAID, unpaid_fen=240_000
        ),
        -part["paid_fen"],
    )
    snap = compute_snapshot(inp)
    assert snap.c_fen == part["c_fen"]
    assert snap.protected_fen == part["protected_fen"]
    assert snap.headroom_fen == part["headroom_fen"]
    assert snap.headroom_fen - start.headroom_fen == part["headroom_change_fen"]
    rent = next(b for b in snap.bills if b.commitment_id == RENT_ID)
    assert rent.unpaid_fen == part["unpaid_fen"]


@pytest.mark.parametrize("raised", [False, True])
def test_monthly_estimate(raised: bool) -> None:
    month = CASH["monthly_estimate"]
    inp = fixture_inputs()
    if raised:
        inp = inp.model_copy(update={"reserve_fen": 500_000})
    snap = compute_snapshot(inp)
    assert snap.monthly_estimate_fen == month["value_fen"]
    assert (
        month["income_fen"]
        - month["spent_fen"]
        - month["allowance_left_fen"]
        - month["bills_due_this_month_fen"]
        == month["value_fen"]
    )


# ACCEPTANCE §3.3 money table, from the cash flows of each step


def fixture_flows() -> dict[str, int]:
    return {
        "rent": CASH["rent_paid"]["c_fen"] - CASH["start"]["c_fen"],
        "settlement": EXP["funds"]["redemption"]["net_fen"],
        "insurance": -500_000,
    }


def step_inputs(branch: str, step: str) -> CashInputs:
    """Inputs of one step of the S6 path, built from flows and bill events only."""
    purchase = {
        "base": PURCHASE_FEN,
        "branch_1200": S6["branch_1200"]["t3_purchase"]["purchase_fen"],
        "branch_1600": 0,
    }[branch]
    new_bill = {"branch_1200": 120_000, "branch_1600": 160_000}.get(branch)
    order = [
        "t0_start",
        "t0_plan_confirmed",
        "t0_redemption_submitted",
        "t2_rent_paid",
        "t3_redemption_settled",
        "t3_bill_confirmed",
        "t3_purchase",
        "t7_insurance_paid",
    ]
    flows = fixture_flows()
    inp = fixture_inputs()
    for name in order[: order.index(step) + 1]:
        if name == "t0_plan_confirmed":
            inp = inp.model_copy(update={"reserve_fen": PLAN["reserve_after_plan_fen"]})
        elif name == "t2_rent_paid":
            inp = pay_in_full(inp, RENT_ID, -flows["rent"])
        elif name == "t3_redemption_settled":
            inp = move_cash(inp, flows["settlement"])
        elif name == "t3_bill_confirmed" and new_bill:
            inp = add_bill(inp, new_bill)
        elif name == "t3_purchase":
            inp = move_cash(inp, -purchase)
        elif name == "t7_insurance_paid":
            inp = pay_in_full(inp, INSURANCE_ID, -flows["insurance"])
    return inp


S6_CASES = [
    pytest.param(branch, step, entry, id=f"{branch}-{step}")
    for branch, steps in ((b, S6[b]) for b in ("base", "branch_1200", "branch_1600"))
    for step, entry in steps.items()
    if isinstance(entry, dict) and "c_fen" in entry
]


@pytest.mark.parametrize(("branch", "step", "entry"), S6_CASES)
def test_s6_money_table(branch: str, step: str, entry: dict[str, Any]) -> None:
    assert amounts(step_inputs(branch, step)) == expected(entry)


def test_s6_table_has_every_step_of_every_branch() -> None:
    # Base has no bill step; the 1,600 branch has no purchase step.
    assert len(S6_CASES) == 7 + 8 + 7


def test_1600_branch_has_no_purchase_but_pays_insurance_from_the_unspent_money() -> None:
    inp = step_inputs("branch_1600", "t7_insurance_paid")
    assert amounts(inp) == {"c_fen": 1_458_500, "protected_fen": 1_020_000, "headroom_fen": 438_500}


def test_new_bill_stays_unpaid_when_the_insurance_is_paid_in_the_1200_branch() -> None:
    snap = compute_snapshot(step_inputs("branch_1200", "t7_insurance_paid"))
    unpaid = {b.commitment_id: (b.status, b.unpaid_fen) for b in snap.bills}
    assert unpaid["cmt_new"] == (BillStatus.SECURED, 120_000)
    assert unpaid[INSURANCE_ID][0] is BillStatus.PAID


# Shortfall and headroom cases outside the table


def test_t69_foreign_debit_of_6000_after_settlement_leaves_a_shortfall_of_15() -> None:
    case = S6["foreign_debit_6000"]
    inp = move_cash(step_inputs("base", case["after"]), -case["debit_fen"])
    snap = compute_snapshot(inp)
    assert snap.shortfall_fen == case["shortfall_fen"] == 1_500
    assert snap.headroom_fen == 0


def test_t55_bill_of_1200_after_the_purchase_leaves_a_shortfall_of_200() -> None:
    case = S6["bill_1200_after_purchase"]
    inp = add_bill(step_inputs("base", case["after"]), case["bill_fen"])
    snap = compute_snapshot(inp)
    assert (snap.c_fen, snap.protected_fen) == (1_460_000, 1_480_000)
    assert snap.shortfall_fen == case["shortfall_fen"] == 20_000
    assert snap.headroom_fen == 0


def test_t55_purchase_hold_and_bill_give_the_same_shortfall_before_posting() -> None:
    # The purchase is a hold of 4,985 that has not posted yet; the new bill counts once in B.
    case = S6["bill_1200_after_purchase"]
    inp = add_bill(step_inputs("base", "t3_redemption_settled"), case["bill_fen"])
    held = inp.model_copy(update={"holds": (hold("purchase", PURCHASE_FEN),)})
    snap = compute_snapshot(held)
    assert (snap.b_fen, snap.u_fen) == (500_000 + case["bill_fen"], PURCHASE_FEN)
    assert snap.shortfall_fen == case["shortfall_fen"]
    assert snap.headroom_fen == 0


def test_bill_of_500_without_bounds_leaves_headroom_of_5485() -> None:
    case = S6["bill_500_without_bounds"]
    inp = add_bill(step_inputs("base", case["after"]), case["bill_fen"])
    snap = compute_snapshot(inp)
    assert snap.headroom_fen == case["headroom_fen"] == 548_500
    assert snap.shortfall_fen == 0
    assert snap.headroom_fen >= PURCHASE_FEN


# Variants


def test_t35_removed_transaction_makes_the_snapshot_incomplete() -> None:
    expected_variant = EXP["variants"]["t35"]
    snap = compute_snapshot(fixture_inputs("t35"))
    assert snap.quality.incomplete is True
    assert snap.quality.reconciled[expected_variant["unreconciled_account"]] is False
    assert snap.quality.reconciled["acc_savings"] is True
    # The balances themselves do not change; only the quality flag does.
    assert snap.c_fen == CASH["start"]["c_fen"]
    assert snap.inputs_hash != compute_snapshot(fixture_inputs()).inputs_hash


def test_t35_imbalance_is_the_removed_transaction() -> None:
    main = next(a for a in fixture_inputs("t35").accounts if a.account_id == "acc_main")
    assert isinstance(main, CashAccountInput)
    assert (
        main.opening_fen + main.imported_sum_fen - main.closing_fen
        == EXP["variants"]["t35"]["imbalance_fen"]
    )


def test_t25_money_is_enough_in_total_but_split_across_accounts() -> None:
    variant = EXP["variants"]["t25"]
    snap = compute_snapshot(fixture_inputs("t25"))
    assert snap.c_fen == variant["total_posted_fen"]
    assert snap.headroom_fen == CASH["start"]["headroom_fen"]
    posted = {a.account_id: a.posted_fen for a in fixture_inputs("t25").accounts}
    assert posted == {
        "acc_main": variant["acc_main_posted_fen"],
        "acc_savings": variant["acc_savings_posted_fen"],
    }


def test_repeated_import_does_not_change_the_hash() -> None:
    # T18: the same statement imported twice produces the same inputs, hence the same hash.
    assert (
        compute_snapshot(fixture_inputs()).inputs_hash
        == compute_snapshot(fixture_inputs()).inputs_hash
    )


def test_fixture_commitments_are_typed_inputs() -> None:
    assert all(isinstance(c, CommitmentInput) for c in fixture_inputs().commitments)
