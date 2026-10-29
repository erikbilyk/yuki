"""The S6 cross-scenario plan driven over HTTP, and the money table of ACCEPTANCE §3.3.

The plan joins six actions: the reserve increase, the redemption of 5,000 shares of C, the
purchase of D, the T+5 check, and the rent and insurance payments. The customer drafts each
part through the guided routes and joins them with `POST /v1/plans/compose`. The fund
orders come from a drafted rebalance plan (`POST /v1/funds/plans`), the only route that
drafts a REDEEM and a PURCHASE; its bounds are expected on the composed plan.

Every helper that moves the plan forward checks the money row it reaches and the node
statuses, so a case reads as the list of steps the customer takes.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from wg.api.app import create_app
from wg.contracts.api import (
    Card,
    CardKind,
    CardStatus,
    ConfirmResult,
    NodePhrase,
    PlanNodeView,
    PlanView,
    VerifyResult,
)
from wg.contracts.engines import PlanBounds
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    CheckpointResult,
    CommitmentStatus,
    OperationStatus,
    PlanKind,
    PlanStatus,
    ReceiptStatus,
    Role,
    Tier,
)
from wg.contracts.errors import ErrorCode

from ..harness.client import MAIN_ACCOUNT, MAIN_CUSTOMER, WgClient
from ..harness.env import harness_settings
from .common import assert_cash

CST = timezone(timedelta(hours=8))
EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=CST)

FUND_C = "990003"
FUND_D = "990004"
MOM = "pay_mom"
DAD = "pay_dad"
LANDLORD = "pay_wanglei"
INSURER = "pay_insurer"
WANGFANG = "pay_wangfang"
RENT = "cmt_rent"
INSURANCE = "cmt_insurance"

SEARCH = {"sector": "technology", "target_max_pct": "40.0000", "fee_cap_fen": 2_000}
BOUNDS = PlanBounds(
    p0_fen=498_500,
    spare0_fen=100_000,
    max_added_fen=120_000,
    max_retained_fen=20_000,
    min_purchase_fen=478_500,
)

RESERVE_START_FEN = 400_000
RESERVE_FEN = 500_000
RENT_FEN = 340_000
INSURANCE_FEN = 500_000
NET_FEN = 498_500
FEE_FEN = 1_500
PURCHASE_FEN = 498_500
REVISED_FEN = 478_500
SHARES_C = Decimal("5000.0000")
PCT_START = Decimal("45.5000")
PCT_BASE = Decimal("39.5089")
PCT_REVISED = Decimal("39.6274")
PCT_NO_PURCHASE = Decimal("42.7778")
TARGET_PCT = Decimal("40.0000")

# The new bill that arrives at T+3 is due at T+9, after the plan approval has expired.
NEW_BILL_DUE = date(2026, 11, 11)


def instant(day: int, hour: int = 9, minute: int = 0, second: int = 0) -> datetime:
    """T+day at the given CST time."""
    return datetime.combine(EPOCH.date() + timedelta(days=day), time(hour, minute, second), CST)


# The plan approval holds through the whole simulated day T+8 (SPEC §15.3).
PLAN_EXPIRY = instant(9, 0)


def at_day(wg: WgClient, day: int) -> None:
    """Move the simulated clock to 09:00 on T+day."""
    wg.advance(to=instant(day))


# Money table (ACCEPTANCE §3.3)


@dataclass(frozen=True)
class Row:
    """C, protected and headroom in fen, with the unpaid bills the snapshot lists."""

    c: int
    protected: int
    headroom: int
    unpaid_fen: int
    unpaid_count: int
    shortfall: int = 0


START = Row(1_800_000, 1_600_000, 200_000, 840_000, 2)
CONFIRMED = Row(1_800_000, 1_700_000, 100_000, 840_000, 2)
REDEMPTION_SUBMITTED = CONFIRMED
RENT_PAID = Row(1_460_000, 1_360_000, 100_000, 500_000, 1)
REDEMPTION_SETTLED = Row(1_958_500, 1_360_000, 598_500, 500_000, 1)
BOUGHT = Row(1_460_000, 1_360_000, 100_000, 500_000, 1)
INSURED = Row(960_000, 860_000, 100_000, 0, 0)

BILL_1200 = Row(1_958_500, 1_480_000, 478_500, 620_000, 2)
BOUGHT_1200 = Row(1_480_000, 1_480_000, 0, 620_000, 2)
INSURED_1200 = Row(980_000, 980_000, 0, 120_000, 1)

BILL_1600 = Row(1_958_500, 1_520_000, 438_500, 660_000, 2)
INSURED_1600 = Row(1_458_500, 1_020_000, 438_500, 160_000, 1)


def assert_row(wg: WgClient, row: Row) -> None:
    view = assert_cash(
        wg, c=row.c, protected=row.protected, headroom=row.headroom, shortfall=row.shortfall
    )
    assert (view.unpaid_bills_fen, view.unpaid_bills_count) == (row.unpaid_fen, row.unpaid_count)


# The plan


NODE_NAMES = ("reserve", "redeem", "purchase", "check", "rent", "insurance")


class S6Plan:
    """A composed S6 plan, its six logical actions and the customer's steps on them."""

    def __init__(self, wg: WgClient, plan_id: str) -> None:
        self.wg = wg
        self.plan_id = plan_id
        nodes = self.view().nodes
        assert len(nodes) == 6, f"{len(nodes)} nodes in the composed plan"
        self.ids: dict[str, str] = {
            "reserve": _one(nodes, ActionKind.DESIGNATE_RESERVE).logical_action_id,
            "redeem": _one(nodes, ActionKind.REDEEM).logical_action_id,
            "purchase": _one(nodes, ActionKind.PURCHASE).logical_action_id,
            "check": _one(nodes, ActionKind.CHECK_OUTCOME).logical_action_id,
            "rent": _bill_node(nodes, RENT).logical_action_id,
            "insurance": _bill_node(nodes, INSURANCE).logical_action_id,
        }

    @property
    def redeem_id(self) -> str:
        return self.ids["redeem"]

    @property
    def purchase_id(self) -> str:
        return self.ids["purchase"]

    @property
    def rent_id(self) -> str:
        return self.ids["rent"]

    @property
    def insurance_id(self) -> str:
        return self.ids["insurance"]

    def view(self) -> PlanView:
        return self.wg.plan(self.plan_id)

    def node(self, name: str) -> PlanNodeView:
        logical_action_id = self.ids[name]
        nodes = [n for n in self.view().nodes if n.logical_action_id == logical_action_id]
        assert len(nodes) == 1, f"{len(nodes)} nodes for {name}"
        return nodes[0]

    def statuses(self) -> dict[str, ActionStatus]:
        by_id = {n.logical_action_id: n.status for n in self.view().nodes}
        return {name: by_id[self.ids[name]] for name in NODE_NAMES}

    def assert_statuses(self, **expected: ActionStatus) -> None:
        actual = self.statuses()
        assert {name: actual[name] for name in expected} == expected, actual

    def card(self) -> Card:
        view = self.view()
        assert view.card_id is not None, "the composed plan shows no card"
        return self.wg.card(view.card_id)

    def approve(self) -> ConfirmResult:
        """Confirm the plan card: yellow, no code; the reserve rises inside the confirmation."""
        card = self.card()
        assert card.kind is CardKind.PLAN and card.status is CardStatus.OPEN
        assert card.plan_id == self.plan_id
        assert card.tier is Tier.YELLOW and not card.requires_code
        assert card.cash_snapshot_id is not None and card.calc_result_id is not None
        confirm = self.wg.confirm(card)
        assert confirm.plan_id == self.plan_id and confirm.approval_id
        assert confirm.challenge is None
        view = self.view()
        assert view.root_approval_id == confirm.approval_id
        assert view.status is PlanStatus.ACTIVE
        assert view.expires_at == PLAN_EXPIRY
        return confirm

    def code_and_verify(self, name: str) -> VerifyResult:
        """Press 获取验证码 on a node waiting for a code and enter the SMS code."""
        challenge = self.wg.challenge(self.ids[name])
        return self.wg.verify(challenge.challenge_id)

    def redeem(self) -> VerifyResult:
        result = self.code_and_verify("redeem")
        op = result.operation
        assert op is not None and op.kind is ActionKind.REDEEM
        assert op.status is OperationStatus.SUBMITTED
        assert op.product_id == FUND_C and op.shares == SHARES_C
        return result

    def pay_rent(self) -> VerifyResult:
        result = self.code_and_verify("rent")
        assert result.operation is not None
        assert result.operation.status is OperationStatus.SETTLED
        return result

    def buy(self) -> VerifyResult:
        result = self.code_and_verify("purchase")
        op = result.operation
        assert op is not None and op.kind is ActionKind.PURCHASE
        return result

    def pay_insurance(self) -> VerifyResult:
        result = self.code_and_verify("insurance")
        assert result.operation is not None
        assert result.operation.status is OperationStatus.SETTLED
        return result


def _one(nodes: tuple[PlanNodeView, ...], kind: ActionKind) -> PlanNodeView:
    found = [n for n in nodes if n.kind is kind]
    assert len(found) == 1, f"{len(found)} {kind} nodes"
    return found[0]


def _bill_node(nodes: tuple[PlanNodeView, ...], commitment_id: str) -> PlanNodeView:
    found = [n for n in nodes if n.kind is ActionKind.PAY_BILL and n.commitment_id == commitment_id]
    assert len(found) == 1, f"{len(found)} payments of {commitment_id}"
    return found[0]


def compose(wg: WgClient, *, with_bounds: bool = True) -> S6Plan:
    """Draft the six actions and join them; the plan exists but nothing is approved."""
    found = wg.candidates(**SEARCH)
    assert found.result.selected is not None, "the search found no passing swap"
    funds = wg.draft_plan(found.calc_result_id, with_bounds)
    assert funds.validation.ok and funds.plan_id is not None
    fund_nodes = {n.kind: n.logical_action_id for n in wg.plan(funds.plan_id).nodes}

    reserve = wg.draft_reserve(RESERVE_FEN)
    assert reserve.tier is Tier.YELLOW
    rent = wg.draft_pay_bill(RENT)
    insurance = wg.draft_pay_bill(INSURANCE)
    redeem = fund_nodes[ActionKind.REDEEM]
    purchase = fund_nodes[ActionKind.PURCHASE]
    draft_ids = [
        reserve.logical_action_id,
        redeem,
        purchase,
        fund_nodes[ActionKind.CHECK_OUTCOME],
        rent.logical_action_id,
        insurance.logical_action_id,
    ]
    composed = wg.compose(draft_ids, {purchase: [redeem]})
    assert composed.kind is PlanKind.MIXED and composed.validation.ok
    assert composed.plan_id is not None

    plan = S6Plan(wg, composed.plan_id)
    view = plan.view()
    assert view.status is PlanStatus.DRAFT and view.root_approval_id is None
    assert view.bounds == (BOUNDS if with_bounds else None)
    assert plan.node("purchase").depends_on == (plan.redeem_id,)
    assert plan.node("check").depends_on == ()
    assert plan.node("rent").payee_id == LANDLORD and plan.node("rent").amount_fen == RENT_FEN
    assert plan.node("insurance").payee_id == INSURER
    assert plan.node("insurance").amount_fen == INSURANCE_FEN
    return plan


def approved(wg: WgClient, *, with_bounds: bool = True) -> S6Plan:
    """The start row, the composed plan and its confirmed card (ACCEPTANCE §3.3, T+0)."""
    assert_row(wg, START)
    plan = compose(wg, with_bounds=with_bounds)
    assert_row(wg, START)
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_START_FEN

    plan.approve()
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN
    plan.assert_statuses(
        reserve=ActionStatus.SETTLED,
        redeem=ActionStatus.AWAITING_STEPUP,
        purchase=ActionStatus.DRAFT,
        check=ActionStatus.DRAFT,
        rent=ActionStatus.DRAFT,
        insurance=ActionStatus.DRAFT,
    )
    assert_row(wg, CONFIRMED)
    return plan


def redeemed(wg: WgClient, *, with_bounds: bool = True) -> S6Plan:
    plan = approved(wg, with_bounds=with_bounds)
    plan.redeem()
    assert_row(wg, REDEMPTION_SUBMITTED)
    plan.assert_statuses(redeem=ActionStatus.SUBMITTED, purchase=ActionStatus.DRAFT)
    return plan


def rent_paid(wg: WgClient, plan: S6Plan) -> None:
    """T+2: the rent node waits for its code; the payment settles at once (本行)."""
    at_day(wg, 2)
    plan.assert_statuses(
        rent=ActionStatus.AWAITING_STEPUP,
        insurance=ActionStatus.DRAFT,
        purchase=ActionStatus.DRAFT,
    )
    assert plan.node("rent").phrase is NodePhrase.AWAITING_CODE
    plan.pay_rent()
    assert_row(wg, RENT_PAID)
    assert_bill_paid_once(wg, plan.rent_id, RENT, RENT_FEN, LANDLORD)
    plan.assert_statuses(rent=ActionStatus.SETTLED, redeem=ActionStatus.SUBMITTED)


def redemption_settled(wg: WgClient, plan: S6Plan) -> None:
    """T+3 09:00: the redemption settles and the purchase waits for its code."""
    at_day(wg, 3)
    assert_redemption_settled(wg, plan)
    assert_row(wg, REDEMPTION_SETTLED)
    plan.assert_statuses(redeem=ActionStatus.SETTLED, purchase=ActionStatus.AWAITING_STEPUP)
    assert wg.db.fund_operations("PURCHASE") == []


def settled_at_t3(wg: WgClient, *, with_bounds: bool = True) -> S6Plan:
    """Every base step up to the settled redemption at T+3."""
    plan = redeemed(wg, with_bounds=with_bounds)
    rent_paid(wg, plan)
    redemption_settled(wg, plan)
    return plan


def bought(wg: WgClient, plan: S6Plan, amount_fen: int = PURCHASE_FEN, revision: int = 1) -> str:
    plan.buy()
    return assert_purchase_settled(wg, plan, amount_fen, revision=revision)


def assert_checkpoint(
    plan: S6Plan, result: CheckpointResult, pct: Decimal, *, at: datetime | None = None
) -> None:
    checkpoint = plan.view().checkpoint
    assert checkpoint is not None, "no checkpoint result"
    assert checkpoint.result is result and checkpoint.measured_pct == pct
    assert checkpoint.target_max_pct == TARGET_PCT
    assert checkpoint.evaluated_at == (at or instant(5))
    rows = plan.wg.db.checkpoints(plan.plan_id)
    assert [row["result"] for row in rows] == [result.value]


# Records


def single_operation(wg: WgClient, logical_action_id: str) -> dict[str, Any]:
    rows = wg.db.operations(logical_action_id)
    assert len(rows) == 1, f"{len(rows)} operations for {logical_action_id}"
    return rows[0]


def customer_effect(wg: WgClient, operation_id: str) -> int:
    """Balanced ledger entries; returns the net effect on the customer's account."""
    entries = wg.db.ledger_entries(operation_id)
    assert entries and sum(e["amount_fen"] for e in entries) == 0
    return sum(e["amount_fen"] for e in entries if e["account_id"] == MAIN_ACCOUNT)


def one_receipt(wg: WgClient, operation_id: str, status: ReceiptStatus) -> str:
    stored = wg.db.receipts(operation_id)
    assert len(stored) == 1, f"{len(stored)} receipts for {operation_id}"
    assert stored[0]["status"] == status
    return stored[0]["receipt_id"]


def assert_bill_paid_once(
    wg: WgClient, logical_action_id: str, commitment_id: str, amount_fen: int, payee_id: str
) -> str:
    """One SETTLED payment of the bill to its own payee; the bill is paid; one receipt."""
    row = single_operation(wg, logical_action_id)
    view = wg.operation(row["operation_id"])
    assert view.status is OperationStatus.SETTLED and view.kind is ActionKind.PAY_BILL
    assert (view.payee_id, view.commitment_id, view.amount_fen) == (
        payee_id,
        commitment_id,
        amount_fen,
    )
    assert customer_effect(wg, view.operation_id) == -amount_fen
    one_receipt(wg, view.operation_id, ReceiptStatus.SETTLED)
    bill = wg.db.commitment(commitment_id)
    assert bill["status"] == CommitmentStatus.PAID and bill["unpaid_fen"] == 0
    return view.operation_id


def assert_redemption_settled(wg: WgClient, plan: S6Plan) -> str:
    """One SETTLED redemption of 5,000 shares of C: ¥4,985 credited once, ¥15 fee."""
    row = single_operation(wg, plan.redeem_id)
    view = wg.operation(row["operation_id"])
    assert view.status is OperationStatus.SETTLED and view.kind is ActionKind.REDEEM
    assert view.product_id == FUND_C and view.shares == SHARES_C
    assert customer_effect(wg, view.operation_id) == NET_FEN
    one_receipt(wg, view.operation_id, ReceiptStatus.SETTLED)
    receipt = wg.receipt(view.operation_id)
    assert (receipt.fee_fen, receipt.net_fen) == (FEE_FEN, NET_FEN)
    return view.operation_id


def assert_purchase_settled(
    wg: WgClient, plan: S6Plan, amount_fen: int, *, revision: int = 1
) -> str:
    """One SETTLED purchase of D for `amount_fen` at the given revision; one receipt."""
    row = single_operation(wg, plan.purchase_id)
    view = wg.operation(row["operation_id"])
    assert view.status is OperationStatus.SETTLED and view.kind is ActionKind.PURCHASE
    assert view.product_id == FUND_D and view.revision == revision
    assert view.amount_fen == amount_fen
    assert customer_effect(wg, view.operation_id) == -amount_fen
    one_receipt(wg, view.operation_id, ReceiptStatus.SETTLED)
    return view.operation_id


def new_bill(wg: WgClient, amount_fen: int, due: date = NEW_BILL_DUE) -> tuple[str, Card]:
    """A trusted e-bill confirmed by the customer (CONFIRM_BILL, yellow), and its card."""
    candidate = wg.ebill(WANGFANG, "培训费", amount_fen, due)
    draft = wg.draft_confirm_bill(candidate.commitment_id)
    assert draft.kind is ActionKind.CONFIRM_BILL and draft.tier is Tier.YELLOW
    outcome = wg.approve(draft)
    assert outcome.confirm.challenge is None
    row = wg.db.commitment(candidate.commitment_id)
    assert row["status"] == CommitmentStatus.CONFIRMED and row["unpaid_fen"] == amount_fen
    return candidate.commitment_id, outcome.card


def confirm_new_bill(wg: WgClient, amount_fen: int, due: date = NEW_BILL_DUE) -> str:
    return new_bill(wg, amount_fen, due)[0]


def purchase_rows(wg: WgClient, plan: S6Plan) -> list[dict[str, Any]]:
    return wg.db.actions(plan.purchase_id)


def action_params(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["params_json"])


def bills_in_order(wg: WgClient, plan: S6Plan) -> list[tuple[str, Card]]:
    """¥800, ¥400, ¥400: unchanged ¥4,985, revised ¥4,785, paused with three reasons.

    Returns each bill with the CONFIRM_BILL card the customer confirmed.
    """
    bills = [new_bill(wg, 80_000)]
    node = plan.node("purchase")
    assert (node.revision, node.amount_fen, node.status) == (
        1,
        PURCHASE_FEN,
        ActionStatus.AWAITING_STEPUP,
    )
    assert_row(wg, Row(1_958_500, 1_440_000, 518_500, 580_000, 2))

    bills.append(new_bill(wg, 40_000))
    node = plan.node("purchase")
    assert (node.revision, node.amount_fen, node.status) == (
        2,
        REVISED_FEN,
        ActionStatus.AWAITING_STEPUP,
    )
    assert_row(wg, Row(1_958_500, 1_480_000, 478_500, 620_000, 3))

    bills.append(new_bill(wg, 40_000))
    node = plan.node("purchase")
    assert node.status is ActionStatus.PAUSED and node.revision == 2
    assert set(node.pause_reasons) == {
        ErrorCode.BILL_INCREASE_OVER_LIMIT,
        ErrorCode.RETENTION_OVER_LIMIT,
        ErrorCode.PURCHASE_BELOW_MIN,
    }
    assert_row(wg, Row(1_958_500, 1_520_000, 438_500, 660_000, 4))
    revisions = plan.view().revisions
    assert [r.revision for r in revisions] == [1, 2]
    assert [r.purchase_after_fen for r in revisions if r.revision == 2] == [REVISED_FEN]
    return bills


# Comparing two runs


def run_signature(wg: WgClient) -> dict[str, Counter[Any]]:
    """What one tick or many must leave identical: coordinator work, events and records.

    Clock moves are audited per request, so audit rows and events about the clock itself
    are left out; everything the coordinator, the gateway and the bank wrote is counted.
    """
    coordinator = Counter(
        (row["event"], row["decision"], row["reason"]) for row in wg.db.audit_rows("coordinator")
    )
    events = Counter(
        row["type"]
        for row in wg.db.rows("SELECT type FROM domain_events")
        if "clock" not in row["type"]
    )
    operations = Counter(
        (row["kind"], row["status"], row["amount_fen"], row["shares"]) for row in wg.db.operations()
    )
    actions = Counter(
        (row["kind"], row["revision"], row["status"])
        for row in wg.db.rows("SELECT kind, revision, status FROM actions")
    )
    checkpoints = Counter(
        (row["result"], row["measured_pct"], row["evaluated_at"])
        for row in wg.db.rows("SELECT * FROM checkpoint_results")
    )
    records = Counter(
        {table: wg.db.count(table) for table in ("receipts", "ledger_entries", "reservations")}
    )
    # A restart may record that it reconciled an order; that observation is not an effect.
    records.update(
        f"operation_event:{row['event']}"
        for row in wg.db.rows("SELECT event FROM operation_events")
        if row["event"] != "RECONCILED"
    )
    authority = Counter(wg.db.authority_counts())
    return {
        "coordinator": coordinator,
        "events": events,
        "operations": operations,
        "actions": actions,
        "checkpoints": checkpoints,
        "records": records,
        "authority": authority,
    }


@contextmanager
def reference_app(workdir: Path) -> Iterator[WgClient]:
    """A second in-process backend on its own database, for a run without races or faults."""
    settings = harness_settings(workdir)
    app = create_app(settings)
    with TestClient(app) as http:
        client = WgClient(http, new_http=lambda: TestClient(app), db_path=settings.db_path)
        client.login(Role.DEMO)
        client.reset()
        client.login(Role.CUSTOMER)
        yield client
