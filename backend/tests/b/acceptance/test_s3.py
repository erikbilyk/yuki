"""S3 funds: T39–T59 at layers D, H and K (ACCEPTANCE §4.3).

Cases are judged on plans, operations, ledger entries, receipts, positions and checkpoint
results, read through the v1 routes and, where no route shows a fact, through a read-only
view of the SPEC §7 tables. The figures are the golden values of ACCEPTANCE §3.2 in fen.

The customer's whole path is driven over HTTP: candidate search, plan draft, plan card,
codes outside the chat, simulated clock, trusted e-bills. Cases about bills (T46, T47, T49,
T50, T55, T57, T58) start from the cash position of the end-to-end path: reserve raised to
¥5,000 and rent paid, so that free money is ¥1,000 at the plan approval and the plan bounds
of the fixture apply. T39, T42–T45, T52, T53, T54 and T56 start from the base fixture.

Not here by design: T40, T41, T51 and the S parts of T44 and T47 are scripted-model cases.
No route drafts a REDEEM or a PURCHASE on its own, so T45 checks the plan the server builds
and the order in which it lets the nodes run, not a hand-made plan that the validator refuses.
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest

from wg.contracts.api import (
    ROUTES,
    Card,
    CardKind,
    CardStatus,
    ChallengeView,
    PercentQuality,
    PlanNodeView,
    PlanView,
    PositionView,
    RevisionReason,
    Route,
    VerifyResult,
    route_models,
)
from wg.contracts.engines import (
    Candidate,
    CandidateKind,
    CandidateReason,
    CandidateSearchResult,
    FeeStatus,
    TargetStatus,
)
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    CheckpointResult,
    CommitmentStatus,
    Fault,
    OperationStatus,
    PlanStatus,
    ReceiptStatus,
    Role,
    Tier,
)
from wg.contracts.errors import ErrorBody, ErrorCode

from ..conftest import Live
from ..harness.client import MAIN_ACCOUNT, MAIN_CUSTOMER, WgClient, describe, route_for
from ..harness.inject import drop_imported_transaction, rewind_event_cursors
from ..harness.server import BackendServer
from .common import (
    AFTER_COMMIT,
    LOST_RESPONSE_FAULTS,
    assert_audited,
    assert_cash,
    audit_snapshot,
    load_variant,
    lose_response,
    new_audit_lines,
)

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=timezone(timedelta(hours=8)))
OTHER_CUSTOMER = "cus_other"

FUND_C = "990003"
FUND_D = "990004"
MOM = "pay_mom"
WANGFANG = "pay_wangfang"
RENT = "cmt_rent"
BILL_DUE = date(2026, 11, 20)

SEARCH = {"sector": "technology", "target_max_pct": "40.0000", "fee_cap_fen": 2_000}

START_FEN = 1_800_000
GROSS_FEN = 500_000
FEE_FEN = 1_500
NET_FEN = 498_500
PURCHASE_FEN = 498_500
REVISED_FEN = 478_500
SHARES_C = Decimal("5000.0000")
PCT_BASE = Decimal("39.5089")
PCT_REVISED = Decimal("39.6274")

# Cash after the reserve is ¥5,000 and the rent is paid: C ¥14,600, protected ¥13,600.
PREPARED_C = 1_460_000
PREPARED_PROTECTED = 1_360_000
PREPARED_HEADROOM = 100_000
# The redemption has settled: C ¥19,585, protected ¥13,600, free money ¥5,985.
SETTLED_C = 1_958_500
SETTLED_HEADROOM = 598_500
INSURANCE_FEN = 500_000

# Helpers


def at_day(wg: WgClient, day: int) -> None:
    """Move the simulated clock to 09:00 on T+day."""
    wg.advance(to=EPOCH + timedelta(days=day))


def assert_refused_in_state(response: httpx.Response) -> ErrorBody:
    """A refusal for the state of the object (409) or the request (422), with an error body."""
    assert response.status_code in (409, 422), describe(response)
    return ErrorBody.model_validate_json(response.content)


class Plan:
    """A drafted S3 plan and the customer's steps on it."""

    def __init__(self, wg: WgClient, plan_id: str) -> None:
        self.wg = wg
        self.plan_id = plan_id
        self.card_id: str | None = None
        self.redeem_id = self.node(ActionKind.REDEEM).logical_action_id
        self.purchase_id = self.node(ActionKind.PURCHASE).logical_action_id
        self.check_id = self.node(ActionKind.CHECK_OUTCOME).logical_action_id

    def view(self) -> PlanView:
        return self.wg.plan(self.plan_id)

    def node(self, kind: ActionKind) -> PlanNodeView:
        nodes = [n for n in self.view().nodes if n.kind is kind]
        assert len(nodes) == 1, f"{len(nodes)} {kind} nodes"
        return nodes[0]

    def card(self) -> Card:
        view = self.view()
        assert view.card_id is not None, "the drafted plan shows no card"
        self.card_id = view.card_id
        return self.wg.card(view.card_id)

    def approve(self) -> PlanView:
        """Confirm the plan card: a yellow approval that executes nothing."""
        card = self.card()
        assert card.kind is CardKind.PLAN and card.status is CardStatus.OPEN
        assert card.tier is Tier.YELLOW and not card.requires_code
        confirm = self.wg.confirm(card)
        assert confirm.plan_id == self.plan_id and confirm.approval_id
        assert confirm.challenge is None and confirm.operation is None
        approved = self.view()
        assert approved.root_approval_id == confirm.approval_id
        assert self.node(ActionKind.REDEEM).status is ActionStatus.AWAITING_STEPUP
        return approved

    def redeem(self) -> VerifyResult:
        result = submit_with_code(self.wg, self.redeem_id)
        op = result.operation
        assert op is not None and op.kind is ActionKind.REDEEM
        assert op.status is OperationStatus.SUBMITTED and op.product_id == FUND_C
        assert op.shares == SHARES_C
        return result

    def buy(self) -> VerifyResult:
        result = submit_with_code(self.wg, self.purchase_id)
        assert result.operation is not None and result.operation.kind is ActionKind.PURCHASE
        return result


def drafted(wg: WgClient, *, with_bounds: bool = False, **search: Any) -> Plan:
    found = wg.candidates(**{**SEARCH, **search})
    assert found.result.selected is not None
    draft = wg.draft_plan(found.calc_result_id, with_bounds)
    assert draft.validation.ok and draft.plan_id is not None
    return Plan(wg, draft.plan_id)


def approved(wg: WgClient, *, with_bounds: bool = False, **search: Any) -> Plan:
    plan = drafted(wg, with_bounds=with_bounds, **search)
    plan.approve()
    return plan


def submit_with_code(wg: WgClient, logical_action_id: str) -> VerifyResult:
    """Ask for a code on a node waiting for one and enter it, as the customer does."""
    challenge = wg.challenge(logical_action_id)
    return wg.verify(challenge.challenge_id)


def prepare_cash(wg: WgClient) -> None:
    """Reserve ¥5,000 and rent paid: the cash position the plan bounds are written for."""
    reserve = wg.draft_reserve(500_000)
    assert reserve.tier is Tier.YELLOW
    wg.approve(reserve)
    rent = wg.draft_pay_bill(RENT)
    assert rent.tier is Tier.RED
    outcome = wg.approve(rent)
    assert outcome.operation is not None
    assert outcome.operation.status is OperationStatus.SETTLED
    assert_cash(wg, c=PREPARED_C, protected=PREPARED_PROTECTED, headroom=PREPARED_HEADROOM)


def redeemed_plan(wg: WgClient, *, with_bounds: bool) -> Plan:
    """Prepared cash, an approved plan, the redemption submitted and settled at T+3."""
    prepare_cash(wg)
    plan = approved(wg, with_bounds=with_bounds)
    plan.redeem()
    at_day(wg, 3)
    assert plan.node(ActionKind.PURCHASE).status is ActionStatus.AWAITING_STEPUP
    assert_cash(wg, c=SETTLED_C, protected=PREPARED_PROTECTED, headroom=SETTLED_HEADROOM)
    return plan


def confirm_bill(wg: WgClient, amount_fen: int) -> str:
    """A trusted e-bill confirmed by the customer: a commitment change. Returns its ID."""
    candidate = wg.ebill(WANGFANG, "培训费", amount_fen, BILL_DUE)
    draft = wg.draft_confirm_bill(candidate.commitment_id)
    assert draft.kind is ActionKind.CONFIRM_BILL and draft.tier is Tier.YELLOW
    outcome = wg.approve(draft)
    assert outcome.confirm.challenge is None
    row = wg.db.commitment(candidate.commitment_id)
    assert row["status"] == CommitmentStatus.CONFIRMED and row["unpaid_fen"] == amount_fen
    return candidate.commitment_id


def position(wg: WgClient, product_id: str) -> PositionView:
    return next(p for p in wg.positions().positions if p.product_id == product_id)


def single_operation(wg: WgClient, logical_action_id: str) -> dict[str, Any]:
    rows = wg.db.operations(logical_action_id)
    assert len(rows) == 1, f"{len(rows)} operations for {logical_action_id}"
    return rows[0]


def customer_effect(wg: WgClient, operation_id: str) -> int:
    """Balanced ledger entries; returns the net effect on the customer's account."""
    entries = wg.db.ledger_entries(operation_id)
    assert entries and sum(e["amount_fen"] for e in entries) == 0
    return sum(e["amount_fen"] for e in entries if e["account_id"] == MAIN_ACCOUNT)


def assert_redemption_settled(wg: WgClient, plan: Plan) -> str:
    """One SETTLED redemption of 5,000 shares of C: ¥5,000 gross, ¥15 fee, ¥4,985 credited."""
    row = single_operation(wg, plan.redeem_id)
    view = wg.operation(row["operation_id"])
    assert view.status is OperationStatus.SETTLED and view.kind is ActionKind.REDEEM
    assert view.product_id == FUND_C and view.shares == SHARES_C
    assert customer_effect(wg, view.operation_id) == NET_FEN
    stored = wg.db.receipts(view.operation_id)
    assert len(stored) == 1
    receipt = wg.receipt(view.operation_id)
    assert receipt.status is ReceiptStatus.SETTLED and receipt.receipt_id == stored[0]["receipt_id"]
    assert (receipt.fee_fen, receipt.net_fen, receipt.shares) == (FEE_FEN, NET_FEN, SHARES_C)
    return view.operation_id


def assert_purchase_settled(wg: WgClient, plan: Plan, amount_fen: int, *, revision: int = 1) -> str:
    """One SETTLED purchase of D for `amount_fen`, one debit, one receipt."""
    row = single_operation(wg, plan.purchase_id)
    view = wg.operation(row["operation_id"])
    assert view.status is OperationStatus.SETTLED and view.kind is ActionKind.PURCHASE
    assert view.product_id == FUND_D and view.revision == revision
    assert view.amount_fen == amount_fen and view.shares == Decimal(amount_fen) / 100
    assert customer_effect(wg, view.operation_id) == -amount_fen
    stored = wg.db.receipts(view.operation_id)
    assert len(stored) == 1 and stored[0]["status"] == ReceiptStatus.SETTLED
    return view.operation_id


def assert_no_fund_orders(wg: WgClient) -> None:
    assert wg.db.fund_operations("REDEEM") == [] and wg.db.fund_operations("PURCHASE") == []


def swap(result: CandidateSearchResult, sell: str, buy: str, gross_fen: int) -> Candidate:
    matches = [
        c
        for c in result.candidates
        if c.kind is CandidateKind.SWAP
        and (c.sell_product, c.buy_product, c.gross_fen) == (sell, buy, gross_fen)
    ]
    assert len(matches) == 1
    return matches[0]


def race(
    wg: WgClient, sends: Sequence[Callable[[WgClient], httpx.Response]]
) -> list[httpx.Response]:
    """Run the requests at once, each from its own tab of the same browser."""
    tabs = [wg.fork() for _ in sends]
    barrier = threading.Barrier(len(tabs))

    def run(tab: WgClient, send: Callable[[WgClient], httpx.Response]) -> httpx.Response:
        barrier.wait()
        return send(tab)

    try:
        with ThreadPoolExecutor(len(tabs)) as pool:
            return list(pool.map(run, tabs, sends))
    finally:
        for tab in tabs:
            tab.close()


# T39


@pytest.mark.case("T39")
def test_t39_the_search_finds_one_passing_swap(wg: WgClient) -> None:
    found = wg.candidates(**SEARCH)
    result = found.result
    assert result.start.lower_pct == result.start.upper_pct == Decimal("45.5000")
    assert result.start.total_fen == 5_000_000 and result.start.executable

    assert Counter(c.kind for c in result.candidates)[CandidateKind.SWAP] == 60
    assert [c for c in result.candidates if c.passes] == [result.selected]
    chosen = result.selected
    assert chosen is not None and chosen.kind is CandidateKind.SWAP
    assert (chosen.sell_product, chosen.buy_product) == (FUND_C, FUND_D)
    assert (chosen.gross_fen, chosen.fee_fen, chosen.purchase_fen) == (
        GROSS_FEN,
        FEE_FEN,
        PURCHASE_FEN,
    )
    assert chosen.shares_redeemed == SHARES_C and chosen.turnover_fen == 998_500
    assert chosen.checkpoint.lower_pct == chosen.checkpoint.upper_pct == PCT_BASE
    assert chosen.checkpoint.status is TargetStatus.MET

    no_trade = next(c for c in result.candidates if c.kind is CandidateKind.NO_TRADE)
    assert not no_trade.passes and no_trade.checkpoint.upper_pct == Decimal("45.5000")
    assert CandidateReason.TARGET_NOT_MET in no_trade.reasons
    assert result.cash_snapshot_id == wg.snapshot().snapshot_id


@pytest.mark.case("T39")
def test_t39_plan_runs_to_a_passed_checkpoint(wg: WgClient) -> None:
    plan = approved(wg)
    assert plan.view().status is PlanStatus.ACTIVE
    assert plan.node(ActionKind.PURCHASE).depends_on == (plan.redeem_id,)

    plan.redeem()
    assert wg.account().posted_fen == START_FEN
    held = position(wg, FUND_C)
    assert (held.shares, held.free_shares) == (Decimal("10000.0000"), SHARES_C)

    # T+3: the redemption settles; the purchase is the next node waiting for a code.
    at_day(wg, 3)
    assert_redemption_settled(wg, plan)
    assert wg.account().posted_fen == START_FEN + NET_FEN
    assert position(wg, FUND_C).shares == SHARES_C
    assert plan.node(ActionKind.PURCHASE).status is ActionStatus.AWAITING_STEPUP

    plan.buy()
    assert_purchase_settled(wg, plan, PURCHASE_FEN)
    assert wg.account().posted_fen == START_FEN
    assert position(wg, FUND_D).shares == Decimal("9985.0000")
    assert wg.positions().total_fen == 4_998_500
    now = wg.exposure().exposure
    assert now.lower_pct == now.upper_pct == PCT_BASE and now.status is TargetStatus.MET

    # T+5: the check reads settled positions and writes its one result.
    at_day(wg, 5)
    view = plan.view()
    checkpoint = view.checkpoint
    assert checkpoint is not None and checkpoint.result is CheckpointResult.PASSED
    assert checkpoint.measured_pct == PCT_BASE
    assert checkpoint.lower_pct == checkpoint.upper_pct == PCT_BASE
    assert checkpoint.target_max_pct == Decimal("40.0000")
    assert checkpoint.quality is PercentQuality.CONFIRMED
    assert view.status is PlanStatus.COMPLETED
    rows = wg.db.checkpoints(plan.plan_id)
    assert [row["result"] for row in rows] == ["passed"]

    funds = wg.db.fund_operations("REDEEM") + wg.db.fund_operations("PURCHASE")
    assert [row["status"] for row in funds] == [OperationStatus.SETTLED] * 2


# T42


@pytest.mark.case("T42")
def test_t42_fee_above_the_cap_is_refused_before_approval(wg: WgClient) -> None:
    load_variant(wg, "t42")
    quote = wg.fee_estimate(FUND_C, "5000.0000").quote
    assert quote.status is FeeStatus.VERIFIED
    assert (quote.gross_fen, quote.fee_fen, quote.net_fen) == (GROSS_FEN, 2_500, 497_500)
    assert quote.draws and all(
        d.held_days == 20 and d.rate == Decimal("0.0050") for d in quote.draws
    )

    found = wg.candidates(**SEARCH)
    assert found.result.selected is None
    over = swap(found.result, FUND_C, FUND_D, GROSS_FEN)
    assert over.fee_fen == 2_500 and not over.passes
    assert CandidateReason.FEE_CAP in over.reasons

    approvals, cards = wg.db.count("approvals"), wg.db.count("cards")
    refusal = wg.refused(wg.draft_plan_response(found.calc_result_id), ErrorCode.FEE_CAP)
    assert refusal.code is ErrorCode.FEE_CAP
    assert wg.plans().plans == ()
    assert_no_fund_orders(wg)
    assert wg.db.rows("SELECT COUNT(*) AS n FROM actions WHERE kind IN ('REDEEM', 'PURCHASE')") == [
        {"n": 0}
    ]
    assert (wg.db.count("approvals"), wg.db.count("cards")) == (approvals, cards)


@pytest.mark.case("T42")
def test_t42_cap_of_thirty_yuan_accepts_the_same_swap(wg: WgClient) -> None:
    load_variant(wg, "t42")
    found = wg.candidates(**{**SEARCH, "fee_cap_fen": 3_000})
    chosen = found.result.selected
    assert chosen is not None and (chosen.sell_product, chosen.buy_product) == (FUND_C, FUND_D)
    assert chosen.fee_fen == 2_500
    assert wg.draft_plan(found.calc_result_id).validation.ok


# T43


@pytest.mark.case("T43")
@pytest.mark.layer("D")
def test_t43_unknown_lots_leave_the_fee_unverified(app_client: WgClient) -> None:
    wg = app_client
    load_variant(wg, "t43")
    quote = wg.fee_estimate(FUND_C, "5000.0000").quote
    assert quote.status is FeeStatus.UNVERIFIED
    assert quote.fee_fen is None and quote.net_fen is None, "an unknown fee is not zero"
    assert wg.fee_estimate(FUND_D, "1000.0000").quote.status is FeeStatus.VERIFIED

    found = wg.candidates(**SEARCH)
    assert found.result.selected is None
    unverified = swap(found.result, FUND_C, FUND_D, GROSS_FEN)
    assert unverified.fee_fen is None and not unverified.passes
    assert CandidateReason.FEE_UNVERIFIED in unverified.reasons

    approvals, cards = wg.db.count("approvals"), wg.db.count("cards")
    response = wg.draft_plan_response(found.calc_result_id)
    assert_refused_in_state(response)
    assert wg.plans().plans == ()
    assert_no_fund_orders(wg)
    assert (wg.db.count("approvals"), wg.db.count("cards")) == (approvals, cards)


# T44


@pytest.mark.case("T44")
@pytest.mark.layer("D")
def test_t44_fee_equal_to_a_strict_limit_fails(app_client: WgClient) -> None:
    wg = app_client
    strict = wg.candidates(**{**SEARCH, "fee_cap_fen": FEE_FEN, "fee_cap_inclusive": False})
    assert strict.result.selected is None
    boundary = swap(strict.result, FUND_C, FUND_D, GROSS_FEN)
    assert boundary.fee_fen == FEE_FEN and CandidateReason.FEE_CAP in boundary.reasons
    wg.refused(wg.draft_plan_response(strict.calc_result_id), ErrorCode.FEE_CAP)
    assert wg.plans().plans == ()


@pytest.mark.case("T44")
@pytest.mark.layer("D")
def test_t44_fee_equal_to_an_inclusive_limit_passes(app_client: WgClient) -> None:
    wg = app_client
    inclusive = wg.candidates(**{**SEARCH, "fee_cap_fen": FEE_FEN, "fee_cap_inclusive": True})
    chosen = inclusive.result.selected
    assert chosen is not None and chosen.fee_fen == FEE_FEN
    assert (chosen.sell_product, chosen.buy_product) == (FUND_C, FUND_D)
    assert wg.draft_plan(inclusive.calc_result_id).validation.ok

    one_fen_less = wg.candidates(**{**SEARCH, "fee_cap_fen": FEE_FEN - 1})
    assert one_fen_less.result.selected is None


# T45


@pytest.mark.case("T45")
def test_t45_purchase_runs_only_after_the_redemption_settled(wg: WgClient) -> None:
    plan = approved(wg)
    assert plan.node(ActionKind.REDEEM).depends_on == ()
    assert plan.node(ActionKind.PURCHASE).depends_on == (plan.redeem_id,)

    def purchase_blocked() -> None:
        node = plan.node(ActionKind.PURCHASE)
        assert node.status is ActionStatus.DRAFT, f"purchase is {node.status}"
        assert_refused_in_state(wg.challenge_response(plan.purchase_id))
        assert all(m.logical_action_id != plan.purchase_id for m in wg.sms().messages)
        assert wg.db.fund_operations("PURCHASE") == []

    purchase_blocked()
    plan.redeem()
    purchase_blocked()

    # Submitted at T+0 and not settled before T+3: its proceeds fund nothing yet.
    at_day(wg, 2)
    purchase_blocked()
    assert plan.node(ActionKind.REDEEM).status is ActionStatus.SUBMITTED
    assert wg.account().posted_fen == START_FEN
    assert position(wg, FUND_C).free_shares == SHARES_C

    at_day(wg, 3)
    redeem_id = assert_redemption_settled(wg, plan)
    plan.buy()
    purchase_id = assert_purchase_settled(wg, plan, PURCHASE_FEN)
    settled_at = wg.operation(redeem_id).settled_at
    assert settled_at is not None and wg.operation(purchase_id).submitted_at >= settled_at


# T46


@pytest.mark.case("T46")
def test_t46_bill_within_bounds_revises_the_purchase(wg: WgClient) -> None:
    plan = redeemed_plan(wg, with_bounds=True)
    first = plan.node(ActionKind.PURCHASE)
    assert (first.revision, first.amount_fen) == (1, PURCHASE_FEN)

    commitment_id = confirm_bill(wg, 120_000)

    revised = plan.node(ActionKind.PURCHASE)
    assert revised.logical_action_id == plan.purchase_id and revised.revision == 2
    assert revised.amount_fen == REVISED_FEN
    assert revised.status is ActionStatus.AWAITING_STEPUP, "the new amount needs a new code"
    rows = wg.db.actions(plan.purchase_id)
    assert [row["status"] for row in rows] == [
        ActionStatus.SUPERSEDED,
        ActionStatus.AWAITING_STEPUP,
    ]
    assert rows[0]["superseded_by"] is not None
    view = plan.view()
    change = next(r for r in view.revisions if r.revision == 2)
    assert change.reason is RevisionReason.BILL_CHANGE and change.within_bounds
    assert (change.purchase_before_fen, change.purchase_after_fen) == (PURCHASE_FEN, REVISED_FEN)
    assert wg.db.fund_operations("PURCHASE") == []

    plan.buy()
    assert_purchase_settled(wg, plan, REVISED_FEN, revision=2)
    assert position(wg, FUND_D).shares == Decimal("9785.0000")
    # Cash equals the protected money exactly, and the new bill is still unpaid.
    final = assert_cash(wg, c=1_480_000, protected=1_480_000, headroom=0)
    assert final.unpaid_bills_fen == INSURANCE_FEN + 120_000 and final.unpaid_bills_count == 2
    row = wg.db.commitment(commitment_id)
    assert row["status"] == CommitmentStatus.CONFIRMED and row["unpaid_fen"] == 120_000
    assert wg.db.fund_operations("PURCHASE")[0]["revision"] == 2

    at_day(wg, 5)
    checkpoint = plan.view().checkpoint
    assert checkpoint is not None and checkpoint.result is CheckpointResult.PASSED
    assert checkpoint.measured_pct == PCT_REVISED
    assert checkpoint.quality is PercentQuality.CONFIRMED
    assert wg.exposure().exposure.upper_pct == PCT_REVISED


@pytest.mark.case("T46")
def test_t46_small_bill_without_bounds_changes_nothing(wg: WgClient) -> None:
    plan = redeemed_plan(wg, with_bounds=False)

    confirm_bill(wg, 50_000)

    after = plan.node(ActionKind.PURCHASE)
    assert (after.revision, after.amount_fen) == (1, PURCHASE_FEN)
    assert after.status is ActionStatus.AWAITING_STEPUP and not after.pause_reasons
    assert [r.revision for r in plan.view().revisions] == [1]
    assert len(wg.db.actions(plan.purchase_id)) == 1
    # ¥500 leaves ¥5,485 free, so the original order still fits.
    assert wg.snapshot().headroom_fen == 548_500

    plan.buy()
    assert_purchase_settled(wg, plan, PURCHASE_FEN)
    at_day(wg, 5)
    checkpoint = plan.view().checkpoint
    assert checkpoint is not None and checkpoint.result is CheckpointResult.PASSED
    assert checkpoint.measured_pct == PCT_BASE


@pytest.mark.case("T46")
def test_t46_large_bill_without_bounds_pauses_on_exact_orders(wg: WgClient) -> None:
    plan = redeemed_plan(wg, with_bounds=False)

    confirm_bill(wg, 120_000)

    node = plan.node(ActionKind.PURCHASE)
    assert node.status is ActionStatus.PAUSED
    assert node.pause_reasons == (ErrorCode.EXACT_ORDERS_ONLY,)
    assert [r.revision for r in plan.view().revisions] == [1]
    assert_refused_in_state(wg.challenge_response(plan.purchase_id))
    assert wg.db.fund_operations("PURCHASE") == []
    assert wg.snapshot().headroom_fen == 478_500


# T47


@pytest.mark.case("T47")
def test_t47_bill_beyond_the_bounds_pauses_the_purchase(wg: WgClient) -> None:
    plan = redeemed_plan(wg, with_bounds=True)
    approvals = wg.db.rows("SELECT COUNT(*) AS n FROM approvals WHERE scope_kind = 'plan'")

    commitment_id = confirm_bill(wg, 160_000)

    node = plan.node(ActionKind.PURCHASE)
    assert node.status is ActionStatus.PAUSED
    assert set(node.pause_reasons) == {
        ErrorCode.BILL_INCREASE_OVER_LIMIT,
        ErrorCode.RETENTION_OVER_LIMIT,
        ErrorCode.PURCHASE_BELOW_MIN,
    }
    assert len(node.pause_reasons) == 3
    assert plan.node(ActionKind.REDEEM).status is ActionStatus.SETTLED
    assert plan.node(ActionKind.CHECK_OUTCOME).status is not ActionStatus.PAUSED

    # No order is possible: nothing was bought, no code can be asked for, no new approval exists.
    assert wg.db.fund_operations("PURCHASE") == []
    assert_refused_in_state(wg.challenge_response(plan.purchase_id))
    assert wg.db.rows("SELECT COUNT(*) AS n FROM approvals WHERE scope_kind = 'plan'") == approvals
    assert wg.db.rows(
        "SELECT COUNT(*) AS n FROM approvals WHERE parent_approval_id IS NOT NULL"
    ) == [{"n": 0}]
    assert position(wg, FUND_D).shares == Decimal("5000.0000")

    # ¥4,785 would use ¥400 of protected money; ¥4,385 is what the free money allows.
    view = assert_cash(wg, c=SETTLED_C, protected=1_520_000, headroom=438_500)
    assert view.headroom_fen < REVISED_FEN
    assert view.unpaid_bills_fen == INSURANCE_FEN + 160_000
    shown = next(b for b in wg.bills().bills if b.commitment_id == commitment_id)
    assert shown.status is CommitmentStatus.CONFIRMED and shown.unpaid_fen == 160_000

    texts = [line.text for line in wg.audit().lines] + [plan.view().model_dump_json()]
    assert not any("不可能" in text or "impossible" in text.lower() for text in texts)

    # The paused purchase is not settled at T+5, so the check says so.
    at_day(wg, 5)
    after = plan.view()
    assert after.checkpoint is not None
    assert after.checkpoint.result is CheckpointResult.CHECKPOINT_MISSED
    assert after.status is not PlanStatus.COMPLETED
    assert wg.db.fund_operations("PURCHASE") == []


# T48


@pytest.mark.case("T48")
@pytest.mark.parametrize("fault", LOST_RESPONSE_FAULTS, ids=[f.value for f in LOST_RESPONSE_FAULTS])
def test_t48_lost_response_on_the_redemption(live: Live, fault: Fault) -> None:
    client = live.client
    plan = approved(client)
    challenge = client.challenge(plan.redeem_id)
    code = client.sms_code(challenge.challenge_id)
    client.arm(fault)

    lose_response(live, fault, lambda: client.verify_response(challenge.challenge_id, code))
    live.restart()

    committed = client.db.operations(plan.redeem_id)
    if fault in AFTER_COMMIT:
        assert len(committed) == 1, "the committed redemption is missing after restart"
    else:
        # Restart recovery may already have resubmitted the READY action.
        assert len(committed) <= 1

    # The client repeats the request whose response it never received.
    retry = client.verify_response(challenge.challenge_id, code)
    if retry.status_code != 200:
        client.refused(retry, ErrorCode.GRANT_CONSUMED)
    operation = single_operation(client, plan.redeem_id)
    if committed:
        assert operation["operation_id"] == committed[0]["operation_id"]
    assert client.operation(operation["operation_id"]).status is OperationStatus.SUBMITTED
    held = position(client, FUND_C)
    assert (held.shares, held.free_shares) == (Decimal("10000.0000"), SHARES_C)

    at_day(client, 3)
    assert_redemption_settled(client, plan)
    assert position(client, FUND_C).shares == SHARES_C
    assert client.db.position_shares(FUND_C) == "5000.0000"
    assert client.account().posted_fen == START_FEN + NET_FEN
    assert len(client.db.fund_operations("REDEEM")) == 1


# T49


@pytest.mark.case("T49")
def test_t49_old_node_cannot_execute_after_the_revision(wg: WgClient) -> None:
    plan = redeemed_plan(wg, with_bounds=True)
    old = wg.challenge(plan.purchase_id)
    old_code = wg.sms_code(old.challenge_id)

    confirm_bill(wg, 120_000)
    assert plan.node(ActionKind.PURCHASE).revision == 2

    late = wg.verify_response(old.challenge_id, old_code)
    wg.refused(late, ErrorCode.SUPERSEDED)
    assert wg.db.fund_operations("PURCHASE") == []
    rows = wg.db.actions(plan.purchase_id)
    assert rows[0]["status"] == ActionStatus.SUPERSEDED
    first = wg.db.challenges(plan.purchase_id)[0]
    assert first["challenge_id"] == old.challenge_id and first["invalidated_at"] is not None
    assert wg.db.sms_codes(old.challenge_id) in ([], [None], [""])
    wg.refused(wg.verify_response(old.challenge_id, old_code), ErrorCode.SUPERSEDED)

    # The paired legitimate path: a new code for the new amount.
    plan.buy()
    assert_purchase_settled(wg, plan, REVISED_FEN, revision=2)
    purchases = wg.db.fund_operations("PURCHASE")
    assert len(purchases) == 1 and purchases[0]["revision"] == 2


# T50


@pytest.mark.case("T50")
def test_t50_expired_code_is_refused_and_a_new_step_up_works(wg: WgClient) -> None:
    plan = approved(wg)
    challenge = wg.challenge(plan.redeem_id)
    code = wg.sms_code(challenge.challenge_id)
    wg.advance(minutes=5)
    before = audit_snapshot(wg)

    wg.refused(wg.verify_response(challenge.challenge_id, code), ErrorCode.CODE_EXPIRED)
    assert wg.db.fund_operations("REDEEM") == []
    assert plan.node(ActionKind.REDEEM).status is ActionStatus.AWAITING_STEPUP
    assert wg.sms().messages == ()
    assert_audited(wg, before, ErrorCode.CODE_EXPIRED)

    fresh = wg.reissue(challenge.challenge_id)
    assert fresh.challenge_id != challenge.challenge_id
    result = wg.verify(fresh.challenge_id)
    assert result.operation is not None and result.operation.status is OperationStatus.SUBMITTED
    assert len(wg.db.fund_operations("REDEEM")) == 1


@pytest.mark.case("T50")
def test_t50_code_for_another_amount_is_refused(wg: WgClient) -> None:
    plan = redeemed_plan(wg, with_bounds=True)
    old = wg.challenge(plan.purchase_id)
    old_code = wg.sms_code(old.challenge_id)
    confirm_bill(wg, 120_000)

    new = wg.challenge(plan.purchase_id)
    for _ in range(5):
        if wg.sms_code(new.challenge_id) != old_code:
            break
        new = wg.reissue(new.challenge_id)
    assert wg.sms_code(new.challenge_id) != old_code
    before = audit_snapshot(wg)

    wg.refused(wg.verify_response(new.challenge_id, old_code), ErrorCode.CODE_INVALID)
    assert wg.db.fund_operations("PURCHASE") == []
    assert_audited(wg, before)

    wg.verify(new.challenge_id)
    assert_purchase_settled(wg, plan, REVISED_FEN, revision=2)


@pytest.mark.case("T50")
def test_t50_user_confirmed_gives_no_authority(wg: WgClient) -> None:
    plan = drafted(wg)
    card = plan.card()
    approvals = wg.db.count("approvals")
    before = audit_snapshot(wg)

    wg.refused(wg.confirm_response(card, user_confirmed=True), ErrorCode.UNSUPPORTED_REQUEST)
    assert wg.db.count("approvals") == approvals and plan.view().root_approval_id is None
    assert wg.card(card.card_id).status is CardStatus.OPEN
    assert plan.node(ActionKind.REDEEM).status is ActionStatus.DRAFT
    assert_audited(wg, before)

    plan.approve()
    challenge = wg.challenge(plan.redeem_id)
    code = wg.sms_code(challenge.challenge_id)
    before = audit_snapshot(wg)
    forged = wg.post(
        f"/v1/challenges/{challenge.challenge_id}/verify", {"code": code, "user_confirmed": True}
    )
    wg.refused(forged, ErrorCode.UNSUPPORTED_REQUEST)
    assert wg.db.fund_operations("REDEEM") == []
    assert plan.node(ActionKind.REDEEM).status is ActionStatus.AWAITING_STEPUP
    assert_audited(wg, before)

    assert wg.verify(challenge.challenge_id, code).operation is not None
    assert len(wg.db.fund_operations("REDEEM")) == 1


# T52


@pytest.mark.case("T52")
def test_t52_purchase_settling_after_the_checkpoint_is_missed(wg: WgClient) -> None:
    load_variant(wg, "t52")
    plan = approved(wg)
    plan.redeem()
    at_day(wg, 3)
    assert_redemption_settled(wg, plan)

    submitted = plan.buy().operation
    assert submitted is not None and submitted.status is OperationStatus.SUBMITTED
    assert submitted.settle_due_at == EPOCH + timedelta(days=6)
    assert position(wg, FUND_D).shares == Decimal("5000.0000")

    before = audit_snapshot(wg)
    at_day(wg, 5)
    view = plan.view()
    assert view.checkpoint is not None
    assert view.checkpoint.result is CheckpointResult.CHECKPOINT_MISSED
    assert view.status is not PlanStatus.COMPLETED
    assert plan.node(ActionKind.PURCHASE).status is ActionStatus.SUBMITTED
    assert not any("已完成" in line for line in new_audit_lines(wg, before))
    assert [row["result"] for row in wg.db.checkpoints(plan.plan_id)] == ["checkpoint_missed"]

    # The late settlement never rewrites the result.
    at_day(wg, 6)
    assert_purchase_settled(wg, plan, PURCHASE_FEN)
    after = plan.view()
    assert after.checkpoint is not None
    assert after.checkpoint.result is CheckpointResult.CHECKPOINT_MISSED
    assert after.status is not PlanStatus.COMPLETED
    assert [row["result"] for row in wg.db.checkpoints(plan.plan_id)] == ["checkpoint_missed"]


# T53


@pytest.mark.case("T53")
@pytest.mark.parametrize(
    "kind", [ActionKind.REDEEM, ActionKind.PURCHASE], ids=["redeem", "purchase"]
)
def test_t53_two_concurrent_submits_make_one_effect(wg: WgClient, kind: ActionKind) -> None:
    plan = approved(wg)
    if kind is ActionKind.PURCHASE:
        plan.redeem()
        at_day(wg, 3)
    action_id = plan.redeem_id if kind is ActionKind.REDEEM else plan.purchase_id
    challenge = wg.challenge(action_id)
    code = wg.sms_code(challenge.challenge_id)

    first, second = race(wg, [lambda tab: tab.verify_response(challenge.challenge_id, code)] * 2)

    # A redemption is still SUBMITTED when verify returns, so it answers 202 instead of 200.
    accepted = (200, 202)
    results = [r for r in (first, second) if r.status_code in accepted]
    assert results, f"neither submit succeeded: {describe(first)} / {describe(second)}"
    for response in (first, second):
        if response.status_code not in accepted:
            wg.refused(response, ErrorCode.GRANT_CONSUMED)
    returned = [VerifyResult.model_validate_json(r.content).operation for r in results]
    assert all(op is not None for op in returned)
    assert len({op.operation_id for op in returned if op is not None}) == 1

    row = single_operation(wg, action_id)
    reservations = wg.db.rows(
        "SELECT * FROM reservations WHERE operation_id = ?", (row["operation_id"],)
    )
    assert len(reservations) <= 1
    if kind is ActionKind.PURCHASE:
        assert len(reservations) == 1
        assert customer_effect(wg, row["operation_id"]) == -PURCHASE_FEN
        assert wg.account().posted_fen == START_FEN
    assert len(wg.db.fund_operations(kind.value)) == 1


# T54


@pytest.mark.case("T54")
@pytest.mark.layer("H")
def test_t54_another_customers_plan_is_not_found(server_client: WgClient) -> None:
    wg = server_client
    plan = approved(wg)
    plan.redeem()
    at_day(wg, 3)
    pending = wg.challenge(plan.purchase_id)
    operation_id = single_operation(wg, plan.redeem_id)["operation_id"]
    mine = {
        "plan": plan.plan_id,
        "action": plan.purchase_id,
        "card": plan.card_id,
        "challenge": pending.challenge_id,
        "operation": operation_id,
    }
    ghost = {
        "plan": "pln_nothingthere0001",
        "action": "act_nothingthere0001",
        "card": "crd_nothingthere0001",
        "challenge": "chl_nothingthere0001",
        "operation": "opn_nothingthere0001",
    }
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/v1/plans/{plan}", None),
        ("POST", "/v1/plans/{plan}/stop", None),
        ("POST", "/v1/actions/{action}/request-approval", None),
        ("POST", "/v1/actions/{action}/cancel", None),
        ("POST", "/v1/actions/{action}/challenge", None),
        ("GET", "/v1/cards/{card}", None),
        ("POST", "/v1/cards/{card}/confirm", {"terms_hash": "0" * 64}),
        ("POST", "/v1/challenges/{challenge}/verify", {"code": "000000"}),
        ("POST", "/v1/challenges/{challenge}/reissue", None),
        ("GET", "/v1/operations/{operation}", None),
        ("GET", "/v1/receipts/{operation}", None),
    ]
    before_plan = plan.view()
    actions_before = wg.db.rows("SELECT logical_action_id, revision, status FROM actions")
    operations_before = wg.db.count("operations")
    open_codes = wg.sms()

    other = wg.fork(with_sessions=False)
    other.login(Role.CUSTOMER, OTHER_CUSTOMER)
    try:
        audit_before = audit_snapshot(other)
        for method, template, body in calls:
            real = other.request(method, template.format(**mine), json=body)
            missing = other.request(method, template.format(**ghost), json=body)
            assert real.status_code == 404, describe(real)
            assert missing.status_code == 404, describe(missing)
            assert real.json() == missing.json(), f"{template} tells a foreign object apart"
        assert other.plans().plans == ()
        assert_audited(other, audit_before)
    finally:
        other.close()

    assert plan.view() == before_plan
    assert wg.db.rows("SELECT logical_action_id, revision, status FROM actions") == actions_before
    assert wg.db.count("operations") == operations_before
    assert wg.sms() == open_codes
    assert wg.verify(pending.challenge_id).operation is not None


# T55


@pytest.mark.case("T55")
@pytest.mark.layer("D")
def test_t55_bill_confirmed_before_the_purchase_revises_it(app_client: WgClient) -> None:
    wg = app_client
    plan = redeemed_plan(wg, with_bounds=True)
    old = wg.challenge(plan.purchase_id)
    old_code = wg.sms_code(old.challenge_id)

    confirm_bill(wg, 120_000)

    stale = wg.verify_response(old.challenge_id, old_code)
    wg.refused(stale, ErrorCode.STALE_SNAPSHOT, ErrorCode.SUPERSEDED)
    assert wg.db.fund_operations("PURCHASE") == []
    revised = plan.node(ActionKind.PURCHASE)
    assert revised.revision == 2 and revised.amount_fen == REVISED_FEN

    plan.buy()
    assert_purchase_settled(wg, plan, REVISED_FEN, revision=2)
    assert len(wg.db.fund_operations("PURCHASE")) == 1
    view = wg.snapshot()
    assert view.details.b_fen == INSURANCE_FEN + 120_000 and view.unpaid_bills_count == 2


@pytest.mark.case("T55")
@pytest.mark.layer("D")
def test_t55_bill_confirmed_after_the_purchase_shows_a_shortfall(app_client: WgClient) -> None:
    wg = app_client
    plan = redeemed_plan(wg, with_bounds=True)
    plan.buy()
    assert_purchase_settled(wg, plan, PURCHASE_FEN)
    assert_cash(wg, c=PREPARED_C, protected=PREPARED_PROTECTED, headroom=PREPARED_HEADROOM)

    confirm_bill(wg, 120_000)

    # The purchase is final: no revision, a ¥200 shortfall, the bill counted once.
    view = assert_cash(wg, c=PREPARED_C, protected=1_480_000, headroom=0, shortfall=20_000)
    assert view.details.b_fen == INSURANCE_FEN + 120_000 and view.unpaid_bills_count == 2
    assert plan.node(ActionKind.PURCHASE).revision == 1
    assert [r.revision for r in plan.view().revisions] == [1]
    assert_purchase_settled(wg, plan, PURCHASE_FEN)
    assert len(wg.db.fund_operations("PURCHASE")) == 1


@pytest.mark.case("T55")
@pytest.mark.layer("D")
def test_t55_bill_and_purchase_submitted_together(app_client: WgClient) -> None:
    wg = app_client
    plan = redeemed_plan(wg, with_bounds=True)
    pending = wg.challenge(plan.purchase_id)
    code = wg.sms_code(pending.challenge_id)
    candidate = wg.ebill(WANGFANG, "培训费", 120_000, BILL_DUE)
    card = wg.open_card(wg.draft_confirm_bill(candidate.commitment_id).logical_action_id)

    verify, bill = race(
        wg,
        [
            lambda tab: tab.verify_response(pending.challenge_id, code),
            lambda tab: tab.confirm_response(card),
        ],
    )

    assert bill.status_code in (200, 201), describe(bill)
    if wg.db.fund_operations("PURCHASE"):
        assert verify.status_code == 200, describe(verify)
        assert_purchase_settled(wg, plan, PURCHASE_FEN)
        assert_cash(wg, c=PREPARED_C, protected=1_480_000, headroom=0, shortfall=20_000)
    else:
        wg.refused(verify, ErrorCode.STALE_SNAPSHOT, ErrorCode.SUPERSEDED)
        assert plan.node(ActionKind.PURCHASE).revision == 2
        plan.buy()
        assert_purchase_settled(wg, plan, REVISED_FEN, revision=2)
    assert len(wg.db.fund_operations("PURCHASE")) == 1
    assert wg.snapshot().details.b_fen == INSURANCE_FEN + 120_000


# T56


def settlement_state(wg: WgClient, plan: Plan) -> dict[str, Any]:
    operation_id = single_operation(wg, plan.redeem_id)["operation_id"]
    events = wg.db.rows(
        "SELECT event FROM operation_events WHERE operation_id = ? ORDER BY operation_event_id",
        (operation_id,),
    )
    return {
        "posted_fen": wg.account().posted_fen,
        "shares": wg.db.position_shares(FUND_C),
        "entries": sorted(
            (e["account_id"], e["amount_fen"]) for e in wg.db.ledger_entries(operation_id)
        ),
        "receipts": [r["receipt_id"] for r in wg.db.receipts(operation_id)],
        "events": [e["event"] for e in events],
        "operation": wg.operation(operation_id),
    }


@pytest.mark.case("T56")
@pytest.mark.layer("D")
def test_t56_repeated_settlement_changes_nothing(app_client: WgClient) -> None:
    wg = app_client
    plan = approved(wg)
    plan.redeem()
    at_day(wg, 3)
    settled = settlement_state(wg, plan)
    assert settled["posted_fen"] == START_FEN + NET_FEN and settled["shares"] == "5000.0000"
    assert len(settled["receipts"]) == 1 and settled["events"].count("SETTLED") == 1
    assert sum(amount for _, amount in settled["entries"]) == 0

    # Every later trigger finds the settlement due and must leave it alone.
    at_day(wg, 3)
    wg.advance(minutes=1)
    wg.advance(days=1)
    wg.login(Role.OPERATOR)
    wg.reconcile(MAIN_CUSTOMER)
    wg.reconcile(MAIN_CUSTOMER)
    wg.login(Role.CUSTOMER)

    assert settlement_state(wg, plan) == settled
    assert_redemption_settled(wg, plan)


@pytest.mark.case("T56")
def test_t56_replayed_events_after_restart_change_nothing(live: Live) -> None:
    client = live.client
    plan = approved(client)
    plan.redeem()
    at_day(client, 3)
    settled = settlement_state(client, plan)

    # The event consumers forget how far they got and read every event again.
    live.server.stop()
    rewind_event_cursors(live.server.db_path)
    live.server.start()
    live.client.relogin()

    assert settlement_state(client, plan) == settled
    assert_redemption_settled(client, plan)


# T57


def contested_headroom(wg: WgClient) -> tuple[Plan, ChallengeView, str, ChallengeView, str]:
    """A purchase of ¥4,985 and a transfer of ¥2,000, each waiting with a code, for ¥5,985."""
    plan = redeemed_plan(wg, with_bounds=False)
    buy = wg.challenge(plan.purchase_id)
    transfer = wg.draft_transfer(MOM, 200_000)
    assert transfer.tier is Tier.RED and transfer.feasible
    confirm = wg.confirm(wg.open_card(transfer.logical_action_id))
    assert confirm.challenge is not None
    return (
        plan,
        buy,
        wg.sms_code(buy.challenge_id),
        confirm.challenge,
        wg.sms_code(confirm.challenge.challenge_id),
    )


def assert_one_won(wg: WgClient) -> None:
    purchases = wg.db.fund_operations("PURCHASE")
    transfers = [o for o in wg.db.money_operations(MAIN_CUSTOMER) if o["kind"] == "TRANSFER"]
    assert len(purchases) + len(transfers) == 1, "both claimed the same free money"
    view = wg.snapshot()
    assert view.shortfall_fen == 0 and view.protected_fen == PREPARED_PROTECTED
    assert view.details.c_fen >= view.protected_fen


@pytest.mark.case("T57")
@pytest.mark.parametrize("first", ["purchase", "transfer"])
def test_t57_the_second_claim_on_the_headroom_is_refused(wg: WgClient, first: str) -> None:
    _, buy, buy_code, transfer, transfer_code = contested_headroom(wg)
    sends = {
        "purchase": lambda: wg.verify_response(buy.challenge_id, buy_code),
        "transfer": lambda: wg.verify_response(transfer.challenge_id, transfer_code),
    }
    second = "transfer" if first == "purchase" else "purchase"

    winner = sends[first]()
    assert winner.status_code == 200, describe(winner)
    loser = sends[second]()
    wg.refused(loser, ErrorCode.INSUFFICIENT_HEADROOM)
    assert_one_won(wg)


@pytest.mark.case("T57")
def test_t57_parallel_claims_on_the_headroom(wg: WgClient) -> None:
    _, buy, buy_code, transfer, transfer_code = contested_headroom(wg)

    first, second = race(
        wg,
        [
            lambda tab: tab.verify_response(buy.challenge_id, buy_code),
            lambda tab: tab.verify_response(transfer.challenge_id, transfer_code),
        ],
    )

    assert sorted([first.status_code == 200, second.status_code == 200]) == [False, True]
    loser = second if first.status_code == 200 else first
    wg.refused(loser, ErrorCode.INSUFFICIENT_HEADROOM)
    assert_one_won(wg)


# T58


@pytest.mark.case("T58")
@pytest.mark.layer("D")
@pytest.mark.parametrize("incomplete", [True, False], ids=["incomplete", "complete"])
def test_t58_incomplete_snapshot_pauses_the_purchase(
    app_client: WgClient, incomplete: bool
) -> None:
    wg = app_client
    plan = redeemed_plan(wg, with_bounds=False)
    if incomplete:
        drop_imported_transaction(wg.db.path, "MAIN-202610-009")

    confirm_bill(wg, 50_000)

    quality = wg.snapshot().details.quality
    node = plan.node(ActionKind.PURCHASE)
    if incomplete:
        assert quality.incomplete and quality.reconciled == {
            MAIN_ACCOUNT: False,
            "acc_savings": True,
        }
        assert node.status is ActionStatus.PAUSED
        assert node.pause_reasons == (ErrorCode.DATA_INCOMPLETE,)
        assert_refused_in_state(wg.challenge_response(plan.purchase_id))
        assert wg.db.fund_operations("PURCHASE") == []
        assert position(wg, FUND_D).shares == Decimal("5000.0000")
        assert plan.node(ActionKind.REDEEM).status is ActionStatus.SETTLED
    else:
        # The same ¥500 bill on complete data needs no decision (pair to T46).
        assert not quality.incomplete
        assert node.status is ActionStatus.AWAITING_STEPUP and not node.pause_reasons
        plan.buy()
        assert_purchase_settled(wg, plan, PURCHASE_FEN)


# T59

MONEY_FIELD = re.compile(r".*_fen$")
DECIMAL_FIELD = re.compile(r"^(nav|rate|weight|shares.*|.*_(pct|weight|shares))$")
DECIMAL_TEXT = re.compile(r"^-?[0-9]+\.[0-9]{4}$")
NOT_A_SUCCESS = {("POST", "/v1/chat/messages"), ("POST", "/v1/operator/locks/{id}/unlock")}


def schema_types(schema: dict[str, Any]) -> set[str]:
    found = {schema["type"]} if isinstance(schema.get("type"), str) else set()
    for branch in schema.get("anyOf", []) + schema.get("oneOf", []):
        found |= schema_types(branch)
    return found


def schema_problems(name: str, schema: Any) -> list[str]:
    """Number types anywhere, and money or decimal properties with the wrong JSON type."""
    problems: list[str] = []
    if isinstance(schema, dict):
        if schema.get("type") == "number":
            problems.append(f"{name}: number type")
        for key, sub in (schema.get("properties") or {}).items():
            types = schema_types(sub) - {"null"}
            if MONEY_FIELD.match(key) and types and types != {"integer"}:
                problems.append(f"{name}.{key}: money field is {sorted(types)}")
            if DECIMAL_FIELD.match(key) and types and types not in ({"string"}, {"object"}):
                problems.append(f"{name}.{key}: decimal field is {sorted(types)}")
        for sub in schema.values():
            problems += schema_problems(name, sub)
    elif isinstance(schema, list):
        for sub in schema:
            problems += schema_problems(name, sub)
    return problems


def reject_float(text: str) -> float:
    raise AssertionError(f"float literal {text} in a response")


def field_problems(path: str, value: Any) -> list[str]:
    """Money fields must be JSON integers and decimal fields 4-place strings, at any depth."""
    problems: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}.{key}"
            if item is not None and MONEY_FIELD.match(key):
                if isinstance(item, bool) or not isinstance(item, int):
                    problems.append(f"{where} = {item!r} is not an integer")
            elif item is not None and DECIMAL_FIELD.match(key):
                for leaf in item.values() if isinstance(item, dict) else [item]:
                    if not (isinstance(leaf, str) and DECIMAL_TEXT.match(leaf)):
                        problems.append(f"{where} = {leaf!r} is not a 4-place decimal string")
            else:
                problems += field_problems(where, item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems += field_problems(f"{path}[{index}]", item)
    return problems


def sweep(wg: WgClient, secret: str, plan: Plan, ids: dict[str, str]) -> None:
    """Call every route at least once; failures are as welcome as successes."""
    ghost = "nothingthere0001"
    wg.get("/healthz")
    wg.get("/v1/session")
    wg.get("/v1/accounts")
    for account in (MAIN_ACCOUNT, "acc_savings", f"acc_{ghost}"):
        wg.get(f"/v1/accounts/{account}/balance")
    wg.get("/v1/payees")
    wg.get("/v1/transfers/history")
    wg.get(f"/v1/operations/{ids['operation']}")
    wg.get(f"/v1/operations/opn_{ghost}")
    wg.get(f"/v1/receipts/{ids['operation']}")
    wg.get(f"/v1/receipts/opn_{ghost}")
    wg.get("/v1/cash/snapshot")
    wg.get("/v1/bills")
    wg.get("/v1/spending")
    wg.get("/v1/transactions")
    wg.get("/v1/unresolved")
    wg.get("/v1/funds/exposure", key="technology", target_max_pct="40.0000")
    wg.get("/v1/funds/exposure", key="technology", basis="public")
    wg.get("/v1/funds/exposure", key="")
    wg.get("/v1/funds/positions")
    wg.get("/v1/funds/fees/estimate", product_id=FUND_C, shares="5000.0000")
    wg.get("/v1/funds/fees/estimate", product_id=FUND_C, shares="1e3")
    wg.get("/v1/plans")
    wg.get(f"/v1/plans/{plan.plan_id}")
    wg.get(f"/v1/plans/pln_{ghost}")
    wg.get("/v1/audit")
    wg.get(f"/v1/cards/{ids['card']}")
    wg.get(f"/v1/cards/crd_{ghost}")
    wg.get("/v1/sms")
    wg.get("/v1/chat/history")

    found = wg.candidates(**SEARCH)
    wg.candidates_response(**{**SEARCH, "target_max_pct": 40.5})
    wg.post("/v1/funds/stress", {"calc_result_id": found.calc_result_id})
    wg.post("/v1/funds/stress", {"calc_result_id": f"clc_{ghost}"})
    wg.draft_plan_response(found.calc_result_id)
    wg.draft_plan_response(f"clc_{ghost}")

    transfers = [wg.draft_transfer_response(MOM, 10_000) for _ in range(3)]
    wg.post("/v1/drafts/transfer", {"payee_id": MOM, "amount_fen": 10_050.5})
    drafts = [json.loads(r.text)["logical_action_id"] for r in transfers if r.status_code == 201]
    wg.draft_response("bill", {"commitment_id": f"cmt_{ghost}"})
    wg.draft_response("reserve", {"target_fen": 500_000})
    wg.draft_response("resolution", {"txn_id": "MAIN-202611-999", "category": "groceries"})
    wg.pay_bill_response("cmt_insurance")
    wg.draft_response("mark-paid-external", {"commitment_id": "cmt_insurance"})
    wg.draft_response("cancel-bill", {"commitment_id": "cmt_insurance"})
    wg.post("/v1/plans/compose", {"draft_ids": drafts[:2], "depends_on": {drafts[1]: [drafts[0]]}})
    wg.post("/v1/plans/compose", {"draft_ids": [f"act_{ghost}"], "depends_on": {}})
    approval = wg.post(f"/v1/actions/{drafts[2]}/request-approval")
    card_id = json.loads(approval.text).get("card_id")
    if card_id:
        wg.confirm_response(wg.card(card_id))
    wg.post(f"/v1/actions/act_{ghost}/request-approval")
    wg.post(f"/v1/actions/{drafts[0]}/cancel")
    wg.post(f"/v1/actions/{drafts[0]}/cancel")

    wg.challenge_response(plan.purchase_id)
    wg.verify_response(ids["challenge"], "000000")
    wg.post(f"/v1/challenges/{ids['challenge']}/reissue")
    wg.post(f"/v1/challenges/chl_{ghost}/verify", {"code": "000000"})
    wg.post(f"/v1/plans/pln_{ghost}/stop")
    wg.post("/v1/channels/pairing-code")

    wg.post("/v1/chat/messages", {"text": "我的科技基金占比是多少？"})
    wg.post("/v1/chat/messages", {"text": ""})

    wg.post(
        "/v1/channels/feishu/messages",
        {"message_id": "msg_t59", "open_id": "ou_unbound", "text": "余额"},
        csrf=False,
    )
    channel = wg.http.post(
        "/v1/channels/feishu/messages",
        json={"message_id": "msg_t59b", "open_id": "ou_unbound", "text": "余额"},
        headers={"X-Channel-Secret": secret},
    )
    assert channel.status_code == 200, describe(channel)

    wg.login(Role.OPERATOR)
    wg.get("/v1/operator/locks", active_only="false")
    wg.get("/v1/operator/audit")
    wg.post(f"/v1/operator/locks/lck_{ghost}/unlock", {"reason": "check"})
    wg.post("/v1/operator/reconcile", {})

    wg.advance(minutes=1)
    wg.post("/sim/faults", {"fault": Fault.PROBE_TIMEOUT.value, "count": 1})
    wg.import_response("stm_main_2611")
    wg.post("/sim/sms", {"customer_id": MAIN_CUSTOMER, "text": "您的账单已出，请及时还款"})
    wg.post(
        "/sim/incoming-payment",
        {"account_id": MAIN_ACCOUNT, "amount_fen": 10_000, "payer_name": "王芳", "remark": "还款"},
    )
    wg.post(
        "/sim/external-debit",
        {"account_id": MAIN_ACCOUNT, "amount_fen": 1_000, "merchant_raw": "模拟商店"},
    )
    wg.post(
        "/sim/bills/ebill",
        {
            "customer_id": MAIN_CUSTOMER,
            "payee_id": WANGFANG,
            "kind": "水电费",
            "amount_fen": 9_000,
            "due_date": BILL_DUE.isoformat(),
        },
    )
    wg.post("/sim/clock/advance", {"days": 1, "minutes": 1})
    wg.reset()
    wg.login(Role.CUSTOMER)
    wg.post("/v1/auth/login", {"handle": "nobody", "password": "wrong"}, csrf=False)
    wg.post("/v1/auth/logout", {"role": Role.CUSTOMER.value})


@pytest.mark.case("T59")
@pytest.mark.layer("H")
def test_t59_no_float_in_any_schema_or_response(
    server_client: WgClient, server: BackendServer
) -> None:
    wg = server_client
    problems: list[str] = []
    for model in route_models():
        for mode in ("validation", "serialization"):
            schema = model.model_json_schema(mode=mode)
            problems += schema_problems(f"{model.__name__}[{mode}]", schema)
    assert not problems, "\n".join(problems)

    seen: list[httpx.Response] = []

    def record(response: httpx.Response) -> None:
        response.read()
        seen.append(response)

    wg.http.event_hooks = {"request": [], "response": [record]}

    # A plan that is stopped, then one that is revised and executed; then every route once more.
    stopped = approved(wg)
    wg.ok(wg.stop_plan_response(stopped.plan_id), PlanView)
    prepare_cash(wg)
    plan = approved(wg, with_bounds=True)
    plan.redeem()
    at_day(wg, 3)
    pending = wg.reissue(wg.challenge(plan.purchase_id).challenge_id)
    confirm_bill(wg, 120_000)
    plan.buy()
    at_day(wg, 5)
    assert plan.view().status is PlanStatus.COMPLETED
    ids = {
        "operation": single_operation(wg, plan.redeem_id)["operation_id"],
        "card": plan.card_id,
        "challenge": pending.challenge_id,
    }
    sweep(wg, server.env["WG_CHANNEL_SECRET"], plan, ids)

    problems: list[str] = []
    answered: dict[tuple[str, str], list[int]] = defaultdict(list)
    for response in seen:
        request = response.request
        route: Route | None = route_for(request.method, request.url.path)
        label = f"{request.method} {request.url.path} -> {response.status_code}"
        if route is None:
            problems.append(f"{label}: no such route in ROUTES")
            continue
        answered[(route.method, route.path)].append(response.status_code)
        if not response.content:
            continue
        try:
            body = json.loads(response.text, parse_float=reject_float)
        except (AssertionError, ValueError) as exc:
            problems.append(f"{label}: {exc}")
            continue
        problems += [f"{label}: {p}" for p in field_problems("$", body)]
    assert not problems, "\n".join(problems[:40])

    expected = {(r.method, r.path) for r in ROUTES if r.response is not None}
    assert not expected - set(answered), (
        f"routes never answered: {sorted(expected - set(answered))}"
    )
    refused_only = {
        key for key, codes in answered.items() if key in expected and all(c >= 400 for c in codes)
    }
    assert refused_only <= NOT_A_SUCCESS, f"no success body scanned for {sorted(refused_only)}"
