"""The plan validator of SPEC §15.1: one test per code, their order, and the money walk."""

from datetime import datetime, timedelta

import pytest

from wg.contracts.enums import ActionKind, CommitmentStatus
from wg.contracts.errors import ErrorCode
from wg.core.k5_validator import (
    CODE_ORDER,
    Bill,
    MoneyFacts,
    Proceeds,
    ProposedNode,
    topological_order,
    validate_plan,
)
from wg.k6.simtime import SHANGHAI

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
MAIN = "acc_main"


def day(n: int) -> datetime:
    return T0 + timedelta(days=n)


def facts(**changes) -> MoneyFacts:
    """The S6 start: ¥18,000 posted, headroom ¥2,000, reserve ¥4,000, two bills."""
    base = {
        "now": T0,
        "expires_at": day(9) - timedelta(hours=9),
        "available_fen": {MAIN: 1_800_000},
        "covered_accounts": frozenset({MAIN}),
        "headroom_fen": 200_000,
        "reserve_fen": 400_000,
        "bills": {
            "cmt_rent": Bill(CommitmentStatus.CONFIRMED, "pay_wanglei"),
            "cmt_insurance": Bill(CommitmentStatus.CONFIRMED, "pay_insurer"),
        },
        "book": frozenset({"pay_wanglei", "pay_insurer", "pay_mom"}),
        "proceeds": {"redeem": Proceeds(day(3), 498_500)},
    }
    return MoneyFacts(**{**base, **changes})


def reserve(target: int = 500_000) -> ProposedNode:
    return ProposedNode("reserve", ActionKind.DESIGNATE_RESERVE, {"target_fen": target})


def redeem() -> ProposedNode:
    params = {"account_id": MAIN, "product_id": "990003", "shares": "5000.0000"}
    return ProposedNode("redeem", ActionKind.REDEEM, params)


def purchase(amount: int = 498_500, depends_on: tuple[str, ...] = ("redeem",)) -> ProposedNode:
    params = {"account_id": MAIN, "product_id": "990004", "amount_fen": amount}
    return ProposedNode("purchase", ActionKind.PURCHASE, params, depends_on)


def check() -> ProposedNode:
    params = {"sector": "technology", "target_max_pct": "40.0000", "target_inclusive": True}
    return ProposedNode("check", ActionKind.CHECK_OUTCOME, params, not_before=day(5))


def bill(
    lid: str, commitment: str, payee: str, amount: int, due: int, node_id: str | None = None
) -> ProposedNode:
    params = {
        "account_id": MAIN,
        "payee_id": payee,
        "amount_fen": amount,
        "commitment_id": commitment,
    }
    return ProposedNode(node_id or lid, ActionKind.PAY_BILL, params, not_before=day(due))


def s6() -> list[ProposedNode]:
    return [
        reserve(),
        redeem(),
        purchase(),
        check(),
        bill("rent", "cmt_rent", "pay_wanglei", 340_000, 2),
        bill("insurance", "cmt_insurance", "pay_insurer", 500_000, 7),
    ]


def codes(nodes, **changes) -> tuple[ErrorCode, ...]:
    return validate_plan(nodes, facts(**changes)).codes


def test_the_s6_plan_is_valid():
    result = validate_plan(s6(), facts())
    assert result.ok and result.codes == ()


def test_a_dependency_outside_the_plan_is_not_in_plan():
    nodes = [*s6()[:2], purchase(depends_on=("redeem", "act_elsewhere"))]
    assert codes(nodes) == (ErrorCode.NOT_IN_PLAN,)


def test_a_kind_that_cannot_be_planned_is_unknown():
    confirm = ProposedNode("confirm", ActionKind.CONFIRM_BILL, {"commitment_id": "cmt_x"})
    assert codes([reserve(), confirm]) == (ErrorCode.UNKNOWN_ACTION_KIND,)


def test_a_cycle_is_refused():
    looped = ProposedNode("redeem", ActionKind.REDEEM, redeem().params, ("purchase",))
    assert codes([looped, purchase()]) == (ErrorCode.PLAN_CYCLE,)
    assert topological_order([looped, purchase()]) is None


def test_a_purchase_from_expected_proceeds_is_refused():
    nodes = [redeem(), purchase(depends_on=())]
    assert ErrorCode.UNFUNDED_DEPENDENCY in codes(nodes)


def test_a_bill_that_is_not_payable_is_refused():
    paid = {"cmt_rent": Bill(CommitmentStatus.PAID, "pay_wanglei")}
    assert codes([bill("rent", "cmt_rent", "pay_wanglei", 340_000, 2)], bills=paid) == (
        ErrorCode.BILL_NOT_PAYABLE,
    )


@pytest.mark.parametrize("payee", ["pay_mom", "pay_stranger"])
def test_a_payee_other_than_the_bills_or_outside_the_book_is_refused(payee: str):
    assert codes([bill("rent", "cmt_rent", payee, 340_000, 2)]) == (ErrorCode.PAYEE_MISMATCH,)


def test_two_payments_of_one_bill_are_one_intent_twice():
    nodes = [
        bill("rent", "cmt_rent", "pay_wanglei", 100_000, 2),
        bill("rent", "cmt_rent", "pay_wanglei", 100_000, 2, node_id="rent_again"),
    ]
    assert codes(nodes) == (ErrorCode.DUPLICATE_INTENT,)


def test_a_node_due_after_the_plan_expiry_is_refused():
    late = bill("insurance", "cmt_insurance", "pay_insurer", 500_000, 9)
    assert codes([late]) == (ErrorCode.NOT_BEFORE_AFTER_EXPIRY,)


def test_a_purchase_beyond_free_money_and_proceeds_is_unfunded():
    # ¥1,000 left after the reserve plus ¥4,985 proceeds cannot pay ¥6,000.
    assert codes([reserve(), redeem(), purchase(600_000)]) == (ErrorCode.PLAN_UNFUNDED,)
    assert codes([reserve(), redeem(), purchase(598_500)]) == ()


def test_proceeds_count_only_after_settlement():
    transfer = ProposedNode(
        "gift",
        ActionKind.TRANSFER,
        {"account_id": MAIN, "payee_id": "pay_mom", "amount_fen": 300_000},
    )
    # The transfer waits for nothing, so the redemption's money is not there yet.
    assert codes([reserve(), redeem(), transfer]) == (ErrorCode.PLAN_UNFUNDED,)


def test_a_bill_needs_posted_money_but_no_headroom():
    rent = bill("rent", "cmt_rent", "pay_wanglei", 340_000, 2)
    assert codes([rent], headroom_fen=0) == ()
    assert codes([rent], available_fen={MAIN: 300_000}) == (ErrorCode.PLAN_UNFUNDED,)


def test_codes_follow_the_fixed_order():
    nodes = [
        redeem(),
        purchase(depends_on=()),
        bill("rent", "cmt_rent", "pay_mom", 340_000, 2),
        bill("late", "cmt_insurance", "pay_insurer", 500_000, 10),
    ]
    found = codes(nodes)
    assert found == (
        ErrorCode.UNFUNDED_DEPENDENCY,
        ErrorCode.PAYEE_MISMATCH,
        ErrorCode.NOT_BEFORE_AFTER_EXPIRY,
        ErrorCode.PLAN_UNFUNDED,
    )
    assert list(found) == sorted(found, key=CODE_ORDER.index)
