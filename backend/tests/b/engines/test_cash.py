"""Cash engine: formulas, hold pairing, bill walk, monthly estimate, quality and hash."""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from wg.contracts.engines import (
    Assumption,
    AssumptionKind,
    CashSnapshotValues,
    ComputeSnapshot,
    UnresolvedInput,
)
from wg.contracts.enums import BillStatus, CommitmentStatus
from wg.numeric import compute_snapshot

from ..builders import EPOCH, account, bill, hold, inputs

TODAY = date(2026, 11, 2)
CONFIRMED = CommitmentStatus.CONFIRMED
PARTIAL = CommitmentStatus.PARTIALLY_PAID


def statuses(snap: CashSnapshotValues) -> dict[str, BillStatus]:
    return {b.commitment_id: b.status for b in snap.bills}


def test_conforms_to_the_engine_protocol() -> None:
    engine: ComputeSnapshot = compute_snapshot
    assert engine(inputs()).c_fen == 1_800_000


def test_formulas_on_plain_numbers() -> None:
    snap = compute_snapshot(
        inputs(
            posted_fen=1_000_000,
            reserve_fen=100_000,
            allowance_fen=200_000,
            margin_fen=50_000,
            bills=(bill("a", 300_000, TODAY + timedelta(days=3)),),
            holds=(hold("x", 70_000),),
        )
    )
    assert (snap.c_fen, snap.e_fen, snap.l_fen, snap.b_fen, snap.m_fen, snap.u_fen) == (
        1_000_000,
        100_000,
        200_000,
        300_000,
        50_000,
        70_000,
    )
    assert snap.protected_fen == 650_000
    assert snap.headroom_fen == 1_000_000 - 650_000 - 70_000
    assert snap.shortfall_fen == 0


def test_shortfall_when_protection_exceeds_money() -> None:
    snap = compute_snapshot(inputs(posted_fen=1_000_000, reserve_fen=900_000))
    assert snap.protected_fen == 1_260_000
    assert (snap.headroom_fen, snap.shortfall_fen) == (0, 260_000)


def test_exact_fit_has_neither_headroom_nor_shortfall() -> None:
    snap = compute_snapshot(inputs(posted_fen=760_000))
    assert snap.protected_fen == 760_000
    assert (snap.headroom_fen, snap.shortfall_fen) == (0, 0)


def test_all_money_fields_are_ints() -> None:
    snap = compute_snapshot(inputs(bills=(bill("a", 1, TODAY),)))
    for value in (snap.c_fen, snap.protected_fen, snap.headroom_fen, snap.monthly_estimate_fen):
        assert type(value) is int


def test_only_covered_accounts_count_in_c() -> None:
    snap = compute_snapshot(
        inputs(
            accounts=(
                account(1_000_000, "acc_main"),
                account(500_000, "acc_savings"),
                account(9_000_000, "acc_other", covered=False),
            )
        )
    )
    assert snap.c_fen == 1_500_000
    assert snap.quality.uncovered_accounts == ("acc_other",)


# Bills and the 30-day horizon


@pytest.mark.parametrize(
    ("status", "in_b"),
    [
        (CONFIRMED, True),
        (PARTIAL, True),
        (CommitmentStatus.CANDIDATE, False),
        (CommitmentStatus.PAID, False),
        (CommitmentStatus.PAID_REPORTED, False),
        (CommitmentStatus.CANCELLED, False),
        (CommitmentStatus.DISMISSED, False),
    ],
)
def test_only_confirmed_and_partially_paid_bills_are_in_b(
    status: CommitmentStatus, in_b: bool
) -> None:
    snap = compute_snapshot(inputs(bills=(bill("a", 100_000, TODAY, status=status),)))
    assert snap.b_fen == (100_000 if in_b else 0)


def test_horizon_includes_day_thirty_and_excludes_day_thirty_one() -> None:
    snap = compute_snapshot(
        inputs(
            bills=(
                bill("in", 100_000, TODAY + timedelta(days=30)),
                bill("out", 200_000, TODAY + timedelta(days=31)),
            )
        )
    )
    assert snap.b_fen == 100_000


def test_horizon_days_is_an_input() -> None:
    bills = (bill("a", 100_000, TODAY + timedelta(days=10)),)
    assert compute_snapshot(inputs(bills=bills, horizon_days=9)).b_fen == 0
    assert compute_snapshot(inputs(bills=bills, horizon_days=10)).b_fen == 100_000


def test_overdue_bill_stays_in_b() -> None:
    snap = compute_snapshot(inputs(bills=(bill("late", 100_000, TODAY - timedelta(days=5)),)))
    assert snap.b_fen == 100_000
    assert statuses(snap) == {"cmt_late": BillStatus.OVERDUE}


def test_horizon_counts_in_shanghai_days() -> None:
    # 2026-11-01 17:00 UTC is 2026-11-02 01:00 in Shanghai.
    as_of = datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    bills = (bill("edge", 100_000, date(2026, 12, 2)), bill("past", 50_000, date(2026, 12, 3)))
    assert compute_snapshot(inputs(bills=bills, as_of=as_of)).b_fen == 100_000


def test_partially_paid_bill_counts_its_unpaid_part() -> None:
    snap = compute_snapshot(
        inputs(bills=(bill("rent", 240_000, TODAY, status=PARTIAL, amount_fen=340_000),))
    )
    assert snap.b_fen == 240_000
    assert snap.bills[0].unpaid_fen == 240_000


# Holds and purpose pairing


def test_hold_paired_with_a_bill_is_not_in_u() -> None:
    bills = (bill("rent", 340_000, TODAY + timedelta(days=2)),)
    with_hold = compute_snapshot(inputs(bills=bills, holds=(hold("pay", 340_000, "rent"),)))
    without = compute_snapshot(inputs(bills=bills))
    assert with_hold.u_fen == 0
    assert with_hold.b_fen == 340_000
    assert (with_hold.protected_fen, with_hold.headroom_fen) == (
        without.protected_fen,
        without.headroom_fen,
    )


def test_unpaired_and_unrelated_holds_are_in_u() -> None:
    snap = compute_snapshot(
        inputs(
            bills=(bill("rent", 340_000, TODAY),),
            holds=(hold("a", 10_000), hold("b", 20_000, "other"), hold("c", 340_000, "rent")),
        )
    )
    assert snap.u_fen == 30_000


@pytest.mark.parametrize(
    "paired_bill",
    [
        bill("rent", 340_000, TODAY + timedelta(days=45)),
        bill("rent", 0, TODAY, status=CommitmentStatus.PAID),
        bill("rent", 340_000, TODAY, status=CommitmentStatus.CANCELLED),
    ],
    ids=["outside-horizon", "paid", "cancelled"],
)
def test_hold_counts_in_u_when_its_bill_is_not_in_b(paired_bill) -> None:
    snap = compute_snapshot(inputs(bills=(paired_bill,), holds=(hold("pay", 340_000, "rent"),)))
    assert snap.b_fen == 0
    assert snap.u_fen == 340_000


def test_two_holds_on_one_bill_are_both_paired() -> None:
    snap = compute_snapshot(
        inputs(
            bills=(bill("rent", 340_000, TODAY),),
            holds=(hold("p1", 100_000, "rent"), hold("p2", 240_000, "rent")),
        )
    )
    assert snap.u_fen == 0


# Secured, unsecured, overdue


def test_two_bills_money_for_one_only_the_later_is_unsecured() -> None:
    # E + L + M = 760,000 fen; 440,000 fen are free, enough for the rent but not the insurance.
    snap = compute_snapshot(
        inputs(
            posted_fen=760_000 + 440_000,
            bills=(
                bill("insurance", 500_000, TODAY + timedelta(days=7)),
                bill("rent", 340_000, TODAY + timedelta(days=2)),
            ),
        )
    )
    assert statuses(snap) == {
        "cmt_rent": BillStatus.SECURED,
        "cmt_insurance": BillStatus.UNSECURED,
    }
    assert snap.shortfall_fen == 400_000


def test_walk_follows_due_date_not_input_order() -> None:
    bills = (
        bill("c", 100_000, TODAY + timedelta(days=9)),
        bill("a", 100_000, TODAY + timedelta(days=1)),
        bill("b", 100_000, TODAY + timedelta(days=5)),
    )
    snap = compute_snapshot(inputs(posted_fen=760_000 + 200_000, bills=bills))
    assert [b.commitment_id for b in snap.bills] == ["cmt_a", "cmt_b", "cmt_c"]
    assert [b.status for b in snap.bills] == [
        BillStatus.SECURED,
        BillStatus.SECURED,
        BillStatus.UNSECURED,
    ]


def test_same_due_date_is_ordered_by_commitment_id() -> None:
    bills = (bill("b", 100_000, TODAY), bill("a", 100_000, TODAY))
    snap = compute_snapshot(inputs(posted_fen=760_000 + 100_000 + 100_000 - 1, bills=bills))
    assert statuses(snap) == {"cmt_a": BillStatus.SECURED, "cmt_b": BillStatus.UNSECURED}


def test_bill_that_exactly_fits_is_secured() -> None:
    snap = compute_snapshot(
        inputs(posted_fen=760_000 + 340_000, bills=(bill("a", 340_000, TODAY),))
    )
    assert snap.headroom_fen == 0
    assert statuses(snap) == {"cmt_a": BillStatus.SECURED}


def test_unpaired_hold_reduces_the_money_free_for_bills() -> None:
    bills = (bill("a", 340_000, TODAY),)
    base = inputs(posted_fen=760_000 + 340_000, bills=bills)
    assert statuses(compute_snapshot(base)) == {"cmt_a": BillStatus.SECURED}
    held = compute_snapshot(base.model_copy(update={"holds": (hold("x", 1),)}))
    assert statuses(held) == {"cmt_a": BillStatus.UNSECURED}


def test_overdue_bill_is_overdue_and_still_uses_money_in_the_walk() -> None:
    snap = compute_snapshot(
        inputs(
            posted_fen=760_000 + 340_000,
            bills=(
                bill("late", 340_000, TODAY - timedelta(days=1)),
                bill("next", 1, TODAY + timedelta(days=1)),
            ),
        )
    )
    assert statuses(snap) == {"cmt_late": BillStatus.OVERDUE, "cmt_next": BillStatus.UNSECURED}


def test_bill_due_today_is_not_overdue() -> None:
    snap = compute_snapshot(inputs(bills=(bill("a", 100_000, TODAY),)))
    assert statuses(snap) == {"cmt_a": BillStatus.SECURED}


def test_paid_and_paid_reported_bills_are_listed_with_nothing_unpaid() -> None:
    snap = compute_snapshot(
        inputs(
            bills=(
                bill("done", 0, TODAY, status=CommitmentStatus.PAID, amount_fen=100_000),
                bill("said", 340_000, TODAY, status=CommitmentStatus.PAID_REPORTED),
                bill("open", 100_000, TODAY),
                bill("gone", 100_000, TODAY, status=CommitmentStatus.CANCELLED),
                bill("idea", 100_000, TODAY, status=CommitmentStatus.CANDIDATE),
            )
        )
    )
    assert statuses(snap) == {
        "cmt_done": BillStatus.PAID,
        "cmt_open": BillStatus.SECURED,
        "cmt_said": BillStatus.PAID_REPORTED,
    }
    assert {b.commitment_id: b.unpaid_fen for b in snap.bills}["cmt_said"] == 0
    assert snap.b_fen == 100_000


def test_paid_reported_is_an_assumption() -> None:
    snap = compute_snapshot(
        inputs(bills=(bill("ins", 500_000, TODAY, status=CommitmentStatus.PAID_REPORTED),))
    )
    assert snap.quality.assumptions == (
        Assumption(kind=AssumptionKind.PAID_REPORTED, ref="cmt_ins"),
    )
    assert snap.b_fen == 0


def test_supplied_assumption_is_not_repeated() -> None:
    given = Assumption(kind=AssumptionKind.PAID_REPORTED, ref="cmt_ins")
    snap = compute_snapshot(
        inputs(
            bills=(bill("ins", 500_000, TODAY, status=CommitmentStatus.PAID_REPORTED),),
            assumptions=(given,),
        )
    )
    assert snap.quality.assumptions == (given,)


def test_no_assumption_without_paid_reported() -> None:
    assert compute_snapshot(inputs(bills=(bill("a", 1, TODAY),))).quality.assumptions == ()


# Monthly estimate


def test_monthly_estimate_formula() -> None:
    snap = compute_snapshot(
        inputs(
            income_fen=1_200_000,
            spent_fen=60_000,
            allowance_fen=300_000,
            bills=(bill("a", 340_000, date(2026, 11, 4)), bill("b", 500_000, date(2026, 11, 30))),
        )
    )
    assert snap.monthly_estimate_fen == 0


def test_monthly_estimate_ignores_bills_due_in_other_months() -> None:
    snap = compute_snapshot(
        inputs(
            bills=(
                bill("next", 500_000, date(2026, 12, 1)),
                bill("this", 100_000, date(2026, 11, 30)),
            )
        )
    )
    assert snap.monthly_estimate_fen == 1_200_000 - 60_000 - 300_000 - 100_000
    assert snap.b_fen == 600_000


def test_monthly_estimate_uses_the_shanghai_calendar_month() -> None:
    # 2026-10-31 17:00 UTC is already 1 November in Shanghai.
    as_of = datetime(2026, 10, 31, 17, 0, tzinfo=UTC)
    bills = (bill("oct", 100_000, date(2026, 10, 31)), bill("nov", 200_000, date(2026, 11, 1)))
    snap = compute_snapshot(
        inputs(bills=bills, as_of=as_of, income_fen=0, spent_fen=0, allowance_fen=0)
    )
    assert snap.monthly_estimate_fen == -200_000


def test_monthly_estimate_can_be_negative_and_uses_unpaid_part() -> None:
    snap = compute_snapshot(
        inputs(
            income_fen=0,
            spent_fen=0,
            allowance_fen=0,
            bills=(bill("rent", 240_000, date(2026, 11, 4), status=PARTIAL, amount_fen=340_000),),
        )
    )
    assert snap.monthly_estimate_fen == -240_000


def test_monthly_estimate_is_independent_of_headroom() -> None:
    snap = compute_snapshot(inputs(posted_fen=5_000_000))
    assert snap.headroom_fen == 5_000_000 - 760_000
    assert snap.monthly_estimate_fen == 1_200_000 - 60_000 - 300_000


# Quality


def test_reconciliation_flags_per_account_and_incomplete() -> None:
    snap = compute_snapshot(
        inputs(accounts=(account(1_000, "acc_main"), account(2_000, "acc_savings", balanced=False)))
    )
    assert snap.quality.reconciled == {"acc_main": True, "acc_savings": False}
    assert snap.quality.incomplete is True


def test_complete_when_every_covered_account_reconciles() -> None:
    snap = compute_snapshot(inputs(accounts=(account(1_000, "acc_main"), account(2_000, "acc_x"))))
    assert snap.quality.incomplete is False


def test_unreconciled_uncovered_account_does_not_make_the_snapshot_incomplete() -> None:
    snap = compute_snapshot(
        inputs(
            accounts=(
                account(1_000, "acc_main"),
                account(2_000, "acc_x", covered=False, balanced=False),
            )
        )
    )
    assert snap.quality.reconciled["acc_x"] is False
    assert snap.quality.incomplete is False


def test_unresolved_items_are_counted_with_their_absolute_impact() -> None:
    snap = compute_snapshot(
        inputs(
            unresolved=(
                UnresolvedInput(txn_id="t1", amount_fen=-30_000),
                UnresolvedInput(txn_id="t2", amount_fen=10_000),
            )
        )
    )
    assert (snap.quality.unresolved_count, snap.quality.unresolved_impact_fen) == (2, 40_000)
    assert snap.c_fen == 1_800_000


def test_quality_carries_the_snapshot_time() -> None:
    assert compute_snapshot(inputs()).quality.as_of == EPOCH


# Hash


def test_same_inputs_give_the_same_hash() -> None:
    assert compute_snapshot(inputs()).inputs_hash == compute_snapshot(inputs()).inputs_hash


def test_hash_is_sha256_of_canonical_json() -> None:
    inp = inputs(bills=(bill("a", 100_000, TODAY),), holds=(hold("h", 5, "a"),))
    text = json.dumps(
        inp.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert compute_snapshot(inp).inputs_hash == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        {"posted_fen": 1_800_001},
        {"reserve_fen": 500_000},
        {"allowance_fen": 1},
        {"margin_fen": 1},
        {"income_fen": 1},
        {"spent_fen": 1},
        {"horizon_days": 29},
        {"holds": (hold("h", 1),)},
        {"bills": (bill("a", 1, TODAY),)},
        {"unresolved": (UnresolvedInput(txn_id="t", amount_fen=1),)},
        {"as_of": EPOCH + timedelta(minutes=1)},
    ],
    ids=lambda c: next(iter(c)),
)
def test_every_input_changes_the_hash(change: dict) -> None:
    assert compute_snapshot(inputs(**change)).inputs_hash != compute_snapshot(inputs()).inputs_hash


def test_hash_does_not_depend_on_derived_values() -> None:
    # Moving a hold's pairing changes U but the hash follows the inputs only.
    a = compute_snapshot(inputs(bills=(bill("x", 1, TODAY),), holds=(hold("h", 5, "x"),)))
    b = compute_snapshot(inputs(bills=(bill("x", 1, TODAY),), holds=(hold("h", 5, "y"),)))
    assert a.u_fen != b.u_fen
    assert a.inputs_hash != b.inputs_hash


# Properties over seeded random inputs


def random_inputs(rng: random.Random):
    count = rng.randint(0, 4)
    bills = tuple(
        bill(
            f"b{i}",
            rng.randint(0, 800_000),
            TODAY + timedelta(days=rng.randint(-10, 40)),
            status=rng.choice([CONFIRMED, PARTIAL, CommitmentStatus.PAID_REPORTED]),
        )
        for i in range(count)
    )
    holds = tuple(
        hold(f"h{i}", rng.randint(0, 300_000), rng.choice([None, "b0", "b1", "zz"]))
        for i in range(rng.randint(0, 3))
    )
    return inputs(
        posted_fen=rng.randint(-100_000, 3_000_000),
        reserve_fen=rng.randint(0, 800_000),
        allowance_fen=rng.randint(0, 500_000),
        margin_fen=rng.randint(0, 100_000),
        bills=bills,
        holds=holds,
    )


def test_headroom_and_shortfall_are_never_both_positive() -> None:
    rng = random.Random(20261028)
    for _ in range(500):
        snap = compute_snapshot(random_inputs(rng))
        assert snap.headroom_fen == 0 or snap.shortfall_fen == 0
        assert (
            snap.headroom_fen - snap.shortfall_fen == snap.c_fen - snap.protected_fen - snap.u_fen
        )
        assert snap.protected_fen == snap.e_fen + snap.l_fen + snap.b_fen + snap.m_fen


def test_paying_part_of_a_bill_leaves_headroom_and_shortfall_unchanged() -> None:
    rng = random.Random(20261029)
    for _ in range(500):
        paid = rng.randint(1, 340_000)
        unpaid = rng.randint(paid, 600_000)
        due = TODAY + timedelta(days=rng.randint(0, 30))
        posted = rng.randint(0, 3_000_000)
        before = compute_snapshot(
            inputs(posted_fen=posted, bills=(bill("rent", unpaid, due, amount_fen=unpaid),))
        )
        after_status = PARTIAL if paid < unpaid else CommitmentStatus.PAID
        after = compute_snapshot(
            inputs(
                posted_fen=posted - paid,
                bills=(bill("rent", unpaid - paid, due, status=after_status, amount_fen=unpaid),),
            )
        )
        assert (after.headroom_fen, after.shortfall_fen) == (
            before.headroom_fen,
            before.shortfall_fen,
        )
        assert after.c_fen == before.c_fen - paid
        assert after.b_fen == before.b_fen - paid


def test_a_paired_hold_never_changes_the_amounts() -> None:
    rng = random.Random(20261030)
    for _ in range(300):
        unpaid = rng.randint(1, 600_000)
        due = TODAY + timedelta(days=rng.randint(0, 30))
        base = inputs(posted_fen=rng.randint(0, 3_000_000), bills=(bill("rent", unpaid, due),))
        held = base.model_copy(update={"holds": (hold("pay", unpaid, "rent"),)})
        a, b = compute_snapshot(base), compute_snapshot(held)
        assert (a.u_fen, a.protected_fen, a.headroom_fen, a.shortfall_fen) == (
            b.u_fen,
            b.protected_fen,
            b.headroom_fen,
            b.shortfall_fen,
        )
