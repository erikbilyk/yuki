"""S2 over HTTP in process: bills, reserve, resolutions, snapshot reads and the simulator."""

from datetime import date

import pytest

from wg.contracts.api import (
    ApprovalRequestResult,
    BillsView,
    Card,
    CashSnapshotView,
    ConfirmResult,
    DraftResult,
    ImportResult,
    OperationView,
    ReceiptView,
    SimCommitmentResult,
    SimPostingResult,
    SmsView,
    SpendingView,
    TransactionsView,
    UnresolvedView,
    VerifyResult,
)
from wg.contracts.enums import (
    ActionStatus,
    BillStatus,
    CommitmentStatus,
    OperationStatus,
    Provenance,
    ReceiptStatus,
    Role,
    Tier,
)
from wg.contracts.errors import ErrorBody, ErrorCode

from .conftest import Browser

pytestmark = pytest.mark.layer("H")

RENT, INSURANCE = "cmt_rent", "cmt_insurance"
START = {"c": 1_800_000, "protected": 1_600_000, "headroom": 200_000}


def post_draft(browser: Browser, name: str, body: dict, status: int = 201):
    response = browser.post(f"/v1/drafts/{name}", body)
    assert response.status_code == status, response.text
    return response


def draft(browser: Browser, name: str, body: dict) -> DraftResult:
    return DraftResult.model_validate(post_draft(browser, name, body).json())


def open_card(browser: Browser, draft_: DraftResult) -> Card:
    response = browser.post(f"/v1/actions/{draft_.logical_action_id}/request-approval")
    assert response.status_code == 200, response.text
    card_id = ApprovalRequestResult.model_validate(response.json()).card_id
    return Card.model_validate(browser.get(f"/v1/cards/{card_id}").json())


def confirm(browser: Browser, card: Card):
    return browser.post(f"/v1/cards/{card.card_id}/confirm", {"terms_hash": card.terms_hash})


def approve(browser: Browser, draft_: DraftResult) -> ConfirmResult | VerifyResult:
    """Confirm the card and, when a code is needed, verify it from the simulated phone."""
    card = open_card(browser, draft_)
    response = confirm(browser, card)
    assert response.status_code in (200, 201, 202), response.text
    confirmed = ConfirmResult.model_validate(response.json())
    if confirmed.challenge is None:
        return confirmed
    sms = SmsView.model_validate(browser.get("/v1/sms").json())
    code = next(m.code for m in sms.messages if m.challenge_id == confirmed.challenge.challenge_id)
    verified = browser.post(
        f"/v1/challenges/{confirmed.challenge.challenge_id}/verify", {"code": code}
    )
    assert verified.status_code in (200, 202), verified.text
    return VerifyResult.model_validate(verified.json())


def snapshot(browser: Browser) -> CashSnapshotView:
    response = browser.get("/v1/cash/snapshot")
    assert response.status_code == 200, response.text
    return CashSnapshotView.model_validate(response.json())


def bills(browser: Browser) -> BillsView:
    return BillsView.model_validate(browser.get("/v1/bills").json())


def error(response) -> ErrorBody:
    return ErrorBody.model_validate(response.json())


def test_snapshot_at_the_epoch_matches_the_golden_values(browser: Browser):
    view = snapshot(browser)
    assert (view.details.c_fen, view.protected_fen, view.headroom_fen) == (
        1_800_000,
        1_600_000,
        200_000,
    )
    assert view.unpaid_bills_fen == 840_000 and view.unpaid_bills_count == 2
    assert view.details.monthly_estimate_fen == 0
    assert [a.reconciled for a in view.details.accounts] == [True, True]
    assert {b.commitment_id: b.status for b in view.details.bills} == {
        RENT: BillStatus.SECURED,
        INSURANCE: BillStatus.SECURED,
    }
    assert snapshot(browser).snapshot_id == view.snapshot_id


def test_rent_payment_lowers_c_protected_and_unpaid_together(browser: Browser):
    created = draft(browser, "pay-bill", {"commitment_id": RENT, "amount_fen": 100_000})
    assert created.tier is Tier.YELLOW and created.commitment_id == RENT
    result = approve(browser, created)
    assert result.operation is not None and result.operation.status is OperationStatus.SETTLED

    receipt = ReceiptView.model_validate(
        browser.get(f"/v1/receipts/{result.operation.operation_id}").json()
    )
    assert receipt.status is ReceiptStatus.SETTLED and receipt.unpaid_after_fen == 240_000
    view = snapshot(browser)
    assert (view.details.c_fen, view.protected_fen, view.headroom_fen) == (
        1_700_000,
        1_500_000,
        200_000,
    )
    rent = next(b for b in bills(browser).bills if b.commitment_id == RENT)
    assert rent.status is CommitmentStatus.PARTIALLY_PAID and rent.unpaid_fen == 240_000

    rest = draft(browser, "pay-bill", {"commitment_id": RENT})
    assert rest.tier is Tier.RED
    approve(browser, rest)
    view = snapshot(browser)
    assert (view.details.c_fen, view.protected_fen, view.headroom_fen) == (
        1_460_000,
        1_260_000,
        200_000,
    )
    assert next(b for b in bills(browser).bills if b.commitment_id == RENT).status is (
        CommitmentStatus.PAID
    )


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"commitment_id": RENT, "amount_fen": 340_001}, ErrorCode.AMOUNT_OVER_UNPAID),
        ({"commitment_id": "cmt_nobody"}, ErrorCode.NOT_FOUND),
    ],
)
def test_pay_bill_drafts_that_cannot_exist_are_refused(browser: Browser, body, code):
    response = browser.post("/v1/drafts/pay-bill", body)
    assert response.status_code in (404, 409), response.text
    assert error(response).code is code


def test_a_second_payment_cannot_exceed_what_is_unpaid_with_one_in_flight(browser: Browser):
    other = draft(
        browser,
        "bill",
        {
            "purpose": "家政服务",
            "payee_id": "pay_dad",
            "amount_fen": 30_000,
            "due_date": "2026-11-10",
        },
    )
    approve(browser, other)
    commitment_id = other.commitment_id
    assert commitment_id is not None

    first = approve(
        browser, draft(browser, "pay-bill", {"commitment_id": commitment_id, "amount_fen": 20_000})
    )
    assert first.operation is not None and first.operation.status is OperationStatus.SUBMITTED
    response = browser.post(
        "/v1/drafts/pay-bill", {"commitment_id": commitment_id, "amount_fen": 20_000}
    )
    assert response.status_code == 409 and error(response).code is ErrorCode.AMOUNT_OVER_UNPAID
    # The hold of the first payment is the bill's own: it is not counted twice.
    view = snapshot(browser)
    assert (view.details.b_fen, view.details.u_fen) == (870_000, 0)


def test_the_bill_check_of_the_gateway_rejects_a_payee_that_is_not_the_bills(browser: Browser):
    # A TRANSFER to the landlord is a discretionary debit, not a payment of the bill.
    created = browser.post(
        "/v1/drafts/transfer", {"payee_id": "pay_wanglei", "amount_fen": 340_000}
    )
    assert created.status_code == 201
    result = DraftResult.model_validate(created.json())
    assert not result.feasible and ErrorCode.INSUFFICIENT_HEADROOM in result.reasons


def test_reserve_up_is_yellow_down_is_red_and_moves_no_money(browser: Browser):
    up = draft(browser, "reserve", {"target_fen": 500_000})
    assert up.tier is Tier.YELLOW
    card = open_card(browser, up)
    assert [(c.before, c.after) for c in card.changes] == [
        (1_600_000, 1_700_000),
        (200_000, 100_000),
    ]
    assert any("不会产生新的资金" in w for w in card.warnings)
    result = approve(browser, up)
    assert result.operation is None
    view = snapshot(browser)
    assert (view.details.e_fen, view.protected_fen, view.headroom_fen) == (
        500_000,
        1_700_000,
        100_000,
    )

    down = draft(browser, "reserve", {"target_fen": 400_000})
    assert down.tier is Tier.RED
    assert approve(browser, down).action_status is ActionStatus.SETTLED
    assert snapshot(browser).details.e_fen == 400_000


def test_a_yellow_reserve_draft_becomes_red_when_the_reserve_moved_first(browser: Browser):
    first = draft(browser, "reserve", {"target_fen": 500_000})
    second = draft(browser, "reserve", {"target_fen": 450_000})
    assert first.tier is second.tier is Tier.YELLOW
    card = open_card(browser, second)
    approve(browser, first)

    response = confirm(browser, card)

    # 450,000 is now below the reserve: the recomputed tier is RED and the gateway wants a code.
    assert response.status_code == 409 and error(response).code is ErrorCode.TIER_CHANGED
    assert snapshot(browser).details.e_fen == 500_000
    with browser.db.read() as conn:
        status = conn.execute(
            "SELECT status FROM actions WHERE logical_action_id = ?", (second.logical_action_id,)
        ).fetchone()[0]
    assert status == ActionStatus.AWAITING_STEPUP.value


def test_confirm_bill_makes_it_protected_but_never_pays_it(browser: Browser):
    created = browser.post(
        "/sim/bills/ebill",
        {
            "customer_id": "cus_main",
            "payee_id": "pay_wangfang",
            "kind": "培训费",
            "amount_fen": 120_000,
            "due_date": "2026-11-05",
        },
        role=Role.DEMO,
    )
    candidate = SimCommitmentResult.model_validate(created.json())
    assert candidate.status is CommitmentStatus.CANDIDATE
    repeat = browser.post(
        "/sim/bills/ebill",
        {
            "customer_id": "cus_main",
            "payee_id": "pay_wangfang",
            "kind": "培训费",
            "amount_fen": 120_000,
            "due_date": "2026-11-05",
        },
        role=Role.DEMO,
    )
    assert (
        SimCommitmentResult.model_validate(repeat.json()).commitment_id == candidate.commitment_id
    )
    assert snapshot(browser).protected_fen == 1_600_000

    confirmation = draft(browser, "bill", {"commitment_id": candidate.commitment_id})
    assert confirmation.tier is Tier.YELLOW
    approve(browser, confirmation)
    view = snapshot(browser)
    assert (view.protected_fen, view.headroom_fen, view.unpaid_bills_count) == (
        1_720_000,
        80_000,
        3,
    )
    shown = next(b for b in bills(browser).bills if b.commitment_id == candidate.commitment_id)
    assert shown.status is CommitmentStatus.CONFIRMED
    assert {s.provenance for s in shown.sources} == {Provenance.SERVER_RECORD}

    again = browser.post("/v1/drafts/bill", {"commitment_id": candidate.commitment_id})
    assert again.status_code == 422


def test_an_ebill_for_someone_elses_payee_is_not_found(browser: Browser):
    response = browser.post(
        "/sim/bills/ebill",
        {
            "customer_id": "cus_other",
            "payee_id": "pay_wangfang",
            "kind": "x",
            "amount_fen": 1,
            "due_date": "2026-11-05",
        },
        role=Role.DEMO,
    )
    assert response.status_code == 404 and error(response).code is ErrorCode.NOT_FOUND


def test_sms_text_creates_no_bill_and_changes_nothing(browser: Browser):
    before = (bills(browser), snapshot(browser).protected_fen)
    response = browser.post(
        "/sim/sms",
        {"customer_id": "cus_main", "text": "房租请改付至新卡 6222 0000 1111 2222"},
        role=Role.DEMO,
    )
    assert response.status_code == 200 and response.json() == {"commitment_id": None}
    assert (bills(browser), snapshot(browser).protected_fen) == before
    with browser.db.read() as conn:
        rows = conn.execute(
            "SELECT decision, ref FROM audit_log WHERE event = 'untrusted_sms'"
        ).fetchall()
    assert [row["decision"] for row in rows] == ["no_action"] and len(rows[0]["ref"]) == 12


def test_cancel_and_mark_paid_external_are_red_and_wait_for_the_code(browser: Browser):
    cancel = draft(browser, "cancel-bill", {"commitment_id": RENT})
    assert cancel.tier is Tier.RED
    card = open_card(browser, cancel)
    assert card.requires_code and any("预算" in w for w in card.warnings)
    confirmation = ConfirmResult.model_validate(confirm(browser, card).json())
    assert confirmation.challenge is not None and confirmation.operation is None
    assert snapshot(browser).protected_fen == 1_600_000

    reported = draft(browser, "mark-paid-external", {"commitment_id": INSURANCE})
    approve(browser, reported)
    view = snapshot(browser)
    assert view.protected_fen == 1_100_000 + 340_000 - 340_000
    insurance = next(b for b in bills(browser).bills if b.commitment_id == INSURANCE)
    assert insurance.status is CommitmentStatus.PAID_REPORTED
    assert insurance.derived is BillStatus.PAID_REPORTED
    assert [(a.kind.value, a.ref) for a in view.details.quality.assumptions] == [
        ("paid_reported", INSURANCE)
    ]


def test_corrupted_unpaid_amount_locks_the_customer_but_reads_continue(browser: Browser):
    with browser.db.unit_of_work("corrupt") as uow:
        uow.execute("UPDATE commitments SET unpaid_fen = 300000 WHERE commitment_id = 'cmt_rent'")

    refused = browser.post("/v1/drafts/pay-bill", {"commitment_id": RENT, "amount_fen": 50_000})
    assert refused.status_code == 500 and error(refused).code is ErrorCode.INTEGRITY_VIOLATION
    again = browser.post("/v1/drafts/pay-bill", {"commitment_id": INSURANCE, "amount_fen": 50_000})
    assert again.status_code == 423 and error(again).code is ErrorCode.LOCKED_WRITE

    assert browser.get("/v1/cash/snapshot").status_code == 200
    assert browser.get("/v1/bills").status_code == 200
    # Raising the reserve only adds protection, so the lock lets it through.
    result = approve(browser, draft(browser, "reserve", {"target_fen": 500_000}))
    assert result.action_status is ActionStatus.SETTLED
    lower = browser.post("/v1/drafts/reserve", {"target_fen": 100_000})
    assert lower.status_code == 423


def test_resolution_is_a_green_customer_action_and_changes_the_unresolved_list(browser: Browser):
    unresolved = UnresolvedView.model_validate(browser.get("/v1/unresolved").json())
    item = unresolved.items[0]
    assert (item.amount_fen, item.impact_fen, unresolved.count) == (-200_000, 200_000, 1)

    created = draft(browser, "resolution", {"txn_id": item.txn_id, "category": "personal_transfer"})
    assert created.tier is Tier.GREEN
    approve(browser, created)
    unresolved = UnresolvedView.model_validate(browser.get("/v1/unresolved").json())
    assert unresolved.count == 0
    # A second class for the same line is refused: a resolution is not silently replaced.
    response = browser.post("/v1/drafts/resolution", {"txn_id": item.txn_id, "category": "refund"})
    again = DraftResult.model_validate(response.json())
    assert not again.feasible and ErrorCode.ALREADY_SETTLED in again.reasons
    unknown = browser.post("/v1/drafts/resolution", {"txn_id": item.txn_id, "category": "nonsense"})
    assert unknown.status_code == 422


def test_spending_groups_outgoing_lines_and_leaves_out_own_transfers(browser: Browser):
    spending = SpendingView.model_validate(browser.get("/v1/spending", days=90).json())
    keys = {row.key for row in spending.rows}
    assert "salary" not in keys and "own_transfer" not in keys and "unresolved" in keys
    assert spending.total_spent_fen == sum(row.spent_fen for row in spending.rows)
    by_month = SpendingView.model_validate(
        browser.get("/v1/spending", days=90, group_by="month").json()
    )
    assert by_month.total_spent_fen == spending.total_spent_fen

    lines = TransactionsView.model_validate(browser.get("/v1/transactions", days=90).json())
    assert lines.transactions and all(t.resolved or t.category is None for t in lines.transactions)
    assert browser.get("/v1/transactions", account_id="acc_other_main").status_code == 404


def test_import_is_a_no_op_for_a_known_statement(browser: Browser):
    before = snapshot(browser)
    result = ImportResult.model_validate(
        browser.sim("/sim/import", {"statement_id": "stm_main_2610"}).json()
    )
    assert result.new_transactions == 0 and result.duplicate_transactions > 0
    assert result.new_commitments == 0
    assert result.snapshot_id == before.snapshot_id and result.inputs_hash
    unknown = browser.sim("/sim/import", {"statement_id": "stm_nothing"})
    assert unknown.status_code == 404


def test_external_debit_creates_a_shortfall_without_touching_bills(browser: Browser):
    result = SimPostingResult.model_validate(
        browser.sim(
            "/sim/external-debit",
            {"account_id": "acc_main", "amount_fen": 400_000, "merchant_raw": "模拟商户"},
        ).json()
    )
    assert (result.amount_fen, result.posted_fen_after) == (-400_000, 1_400_000)
    view = snapshot(browser)
    assert (view.headroom_fen, view.shortfall_fen, view.mode.value) == (0, 200_000, "shortfall")
    assert {b.commitment_id: b.status for b in view.details.bills} == {
        RENT: BillStatus.SECURED,
        INSURANCE: BillStatus.UNSECURED,
    }
    too_much = browser.sim(
        "/sim/external-debit",
        {"account_id": "acc_main", "amount_fen": 9_000_000, "merchant_raw": "x"},
    )
    assert too_much.status_code == 409


@pytest.mark.parametrize("name", ["pay-bill", "cancel-bill", "mark-paid-external"])
def test_a_missing_bill_is_not_found_for_every_draft(browser: Browser, name: str):
    response = browser.post(f"/v1/drafts/{name}", {"commitment_id": "cmt_ghost"})
    assert response.status_code == 404 and error(response).code is ErrorCode.NOT_FOUND


def test_a_paid_bill_cannot_be_confirmed_or_paid_again(browser: Browser):
    approve(browser, draft(browser, "pay-bill", {"commitment_id": RENT}))
    response = browser.post("/v1/drafts/pay-bill", {"commitment_id": RENT})
    assert response.status_code == 409 and error(response).code is ErrorCode.BILL_NOT_PAYABLE
    revive = browser.post("/v1/drafts/bill", {"commitment_id": RENT})
    assert revive.status_code == 422
    cancel = draft(browser, "cancel-bill", {"commitment_id": RENT})
    assert not cancel.feasible and ErrorCode.BILL_NOT_PAYABLE in cancel.reasons


def test_operations_view_of_a_bill_payment_carries_the_commitment(browser: Browser):
    result = approve(browser, draft(browser, "pay-bill", {"commitment_id": RENT}))
    assert result.operation is not None
    view = OperationView.model_validate(
        browser.get(f"/v1/operations/{result.operation.operation_id}").json()
    )
    assert view.commitment_id == RENT and view.kind.value == "PAY_BILL"
    assert date(2026, 11, 2) <= view.submitted_at.date()


def test_a_rejected_bill_payment_leaves_the_bill_unpaid_and_says_why(browser: Browser):
    assert browser.sim("/sim/faults", {"fault": "settlement_reject"}).status_code == 200
    result = approve(
        browser, draft(browser, "pay-bill", {"commitment_id": RENT, "amount_fen": 50_000})
    )
    assert result.operation is not None and result.operation.status is OperationStatus.REJECTED

    receipt = ReceiptView.model_validate(
        browser.get(f"/v1/receipts/{result.operation.operation_id}").json()
    )
    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason_code is ErrorCode.BANK_REJECTED and receipt.unpaid_after_fen is None
    rent = next(b for b in bills(browser).bills if b.commitment_id == RENT)
    assert (rent.status, rent.unpaid_fen) == (CommitmentStatus.CONFIRMED, 340_000)
    view = snapshot(browser)
    assert (view.details.c_fen, view.protected_fen) == (1_800_000, 1_600_000)


def ledger_accounts(browser: Browser, operation_id: str) -> dict[str, int]:
    with browser.db.read() as conn:
        rows = conn.execute(
            "SELECT account_id, amount_fen FROM ledger_entries WHERE operation_id = ?",
            (operation_id,),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def posted(browser: Browser, account_id: str) -> int:
    response = browser.get(f"/v1/accounts/{account_id}/balance")
    assert response.status_code == 200, response.text
    return response.json()["posted_fen"]


@pytest.mark.case("T25")
def test_a_transfer_between_own_covered_accounts_keeps_c_and_needs_no_headroom(
    browser: Browser,
):
    # ¥3,000 is more than the ¥2,000 headroom, but the money stays in covered accounts.
    created = draft(browser, "transfer", {"payee_id": "pay_self_savings", "amount_fen": 300_000})
    assert created.feasible and created.tier is Tier.RED
    card = open_card(browser, created)
    assert [(c.key, c.before, c.after) for c in card.changes] == [
        ("account_posted", 1_800_000, 1_500_000),
        ("to_account_posted", 0, 300_000),
    ]

    result = approve(browser, created)
    assert result.action_status is ActionStatus.SETTLED and result.operation is not None
    assert ledger_accounts(browser, result.operation.operation_id) == {
        "acc_main": -300_000,
        "acc_savings": 300_000,
    }
    assert (posted(browser, "acc_main"), posted(browser, "acc_savings")) == (1_500_000, 300_000)
    view = snapshot(browser)
    assert (view.details.c_fen, view.protected_fen, view.headroom_fen) == tuple(START.values())


def test_a_transfer_to_the_account_it_comes_from_is_refused(browser: Browser):
    response = post_draft(
        browser, "transfer", {"payee_id": "pay_self_main", "amount_fen": 10_000}, 422
    )
    assert error(response).code is ErrorCode.UNSUPPORTED_REQUEST


@pytest.mark.case("T25")
def test_a_plan_pays_rent_from_savings_after_the_own_transfer_settles(browser: Browser):
    rent = draft(browser, "pay-bill", {"commitment_id": RENT, "account_id": "acc_savings"})
    assert not rent.feasible and ErrorCode.INSUFFICIENT_FUNDS in rent.reasons
    alone = browser.post("/v1/plans/compose", {"draft_ids": [rent.logical_action_id]})
    assert error(alone).code is ErrorCode.PLAN_UNFUNDED

    move = draft(browser, "transfer", {"payee_id": "pay_self_savings", "amount_fen": 340_000})
    body = {
        "draft_ids": [move.logical_action_id, rent.logical_action_id],
        "depends_on": {rent.logical_action_id: [move.logical_action_id]},
    }
    response = browser.post("/v1/plans/compose", body)
    assert response.status_code == 201, response.text
