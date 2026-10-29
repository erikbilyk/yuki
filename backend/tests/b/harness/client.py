"""HTTP client for acceptance cases: role logins, CSRF, and helpers over the v1 routes.

The same client drives the in-process app (a TestClient) and a subprocess backend (an
httpx.Client with trust_env=False), so a case reads identically at layers D, H and K.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from wg.contracts.api import (
    ROUTES,
    AccountsView,
    AccountView,
    ApprovalRequestResult,
    AuditView,
    BalanceView,
    BillsView,
    CancelResult,
    CandidatesView,
    Card,
    CashSnapshotView,
    ChallengeView,
    ClockView,
    ConfirmResult,
    DraftResult,
    ExposureView,
    FaultsView,
    FeeEstimateView,
    ImportResult,
    LocksView,
    LockView,
    OperationView,
    PayeesView,
    PlanDraftResult,
    PlansView,
    PlanView,
    PositionsView,
    ReceiptView,
    ReconcileResult,
    Route,
    RouteRole,
    SessionEntry,
    SimCommitmentResult,
    SimPostingResult,
    SimResetResult,
    SimSmsResult,
    SmsView,
    SpendingView,
    StressView,
    TransactionsView,
    TransferHistoryView,
    UnresolvedView,
    VerifyResult,
)
from wg.contracts.enums import CommitmentStatus, Fault, Role
from wg.contracts.errors import HTTP_STATUS, ErrorBody, ErrorCode
from wg.contracts.fixture import Fixture, load_fixture

from .db import DbView
from .env import FIXTURE_PATH

CSRF_HEADER = "X-CSRF-Token"
MAIN_CUSTOMER = "cus_main"
MAIN_ACCOUNT = "acc_main"
REQUEST_TIMEOUT_S = 30.0
# Confirm and verify answer 202 while the operation they submitted is SUBMITTED or UNKNOWN.
EXECUTION_STATUS = (200, 201, 202)

M = TypeVar("M", bound=BaseModel)

_ROLE_OF_ROUTE = {
    RouteRole.CUSTOMER: Role.CUSTOMER,
    RouteRole.OPERATOR: Role.OPERATOR,
    RouteRole.DEMO: Role.DEMO,
}


def route_for(method: str, path: str) -> Route | None:
    """The route whose template matches a concrete path, such as /v1/cards/crd_x/confirm."""
    parts = path.split("?", 1)[0].strip("/").split("/")
    for route in ROUTES:
        template = route.path.strip("/").split("/")
        if route.method != method or len(template) != len(parts):
            continue
        pairs = zip(template, parts, strict=True)
        if all(t == p or (t.startswith("{") and t.endswith("}")) for t, p in pairs):
            return route
    return None


def describe(response: httpx.Response) -> str:
    request = response.request
    return f"{request.method} {request.url.path} -> {response.status_code}: {response.text[:500]}"


def http_client(base_url: str) -> httpx.Client:
    """A client for a local backend that ignores proxy variables in the environment."""
    return httpx.Client(base_url=base_url, trust_env=False, timeout=REQUEST_TIMEOUT_S)


@dataclass(frozen=True)
class TransferOutcome:
    """Everything one guided transfer returned, from draft to execution."""

    draft: DraftResult
    card: Card
    confirm: ConfirmResult
    verify: VerifyResult | None

    @property
    def logical_action_id(self) -> str:
        return self.draft.logical_action_id

    @property
    def operation(self) -> OperationView | None:
        if self.verify is not None:
            return self.verify.operation
        return self.confirm.operation


class WgClient:
    """One browser: a cookie jar holding up to one session per role, and their CSRF tokens."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        new_http: Callable[[], httpx.Client],
        db_path: Path,
        fixture: Fixture | None = None,
    ) -> None:
        self.http = http
        self._new_http = new_http
        self.db = DbView(db_path)
        self.fixture = fixture or load_fixture(FIXTURE_PATH)
        self.csrf: dict[Role, str] = {}
        self._subjects: dict[Role, str] = {}

    # Transport

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        csrf: bool | None = None,
        role: Role | None = None,
    ) -> httpx.Response:
        """Send a request; POSTs to CSRF routes carry the token of the route's role."""
        route = route_for(method, path)
        if csrf is None:
            csrf = route.csrf if route else method == "POST"
        headers = {}
        if csrf:
            token_role = role or (_ROLE_OF_ROUTE.get(route.role) if route else Role.CUSTOMER)
            token = self.csrf.get(token_role) if token_role else None
            if token:
                headers[CSRF_HEADER] = token
        return self.http.request(method, path, json=json, params=params, headers=headers)

    def get(self, path: str, **params: Any) -> httpx.Response:
        query = {key: value for key, value in params.items() if value is not None}
        return self.request("GET", path, params=query or None)

    def post(self, path: str, json: Any = None, **options: Any) -> httpx.Response:
        return self.request("POST", path, json=json, **options)

    @staticmethod
    def ok(response: httpx.Response, model: type[M], status: tuple[int, ...] = (200, 201)) -> M:
        """Parse a success body into its contract model; fail with the response otherwise."""
        assert response.status_code in status, describe(response)
        return model.model_validate_json(response.content)

    @staticmethod
    def refused(response: httpx.Response, *codes: ErrorCode) -> ErrorBody:
        """Assert an error body with one of `codes` and the HTTP status the contract gives it."""
        assert response.status_code >= 400, describe(response)
        body = ErrorBody.model_validate_json(response.content)
        assert body.code in codes, describe(response)
        assert response.status_code == HTTP_STATUS[body.code], describe(response)
        return body

    def fork(self, *, with_sessions: bool = True) -> WgClient:
        """A second connection; with sessions it is another tab of the same browser."""
        clone = WgClient(
            self._new_http(), new_http=self._new_http, db_path=self.db.path, fixture=self.fixture
        )
        if with_sessions:
            for cookie in self.http.cookies.jar:
                clone.http.cookies.jar.set_cookie(cookie)
            clone.csrf = dict(self.csrf)
            clone._subjects = dict(self._subjects)
        return clone

    def close(self) -> None:
        self.http.close()

    # Sessions (SPEC §6)

    def login(self, role: Role = Role.CUSTOMER, customer_id: str = MAIN_CUSTOMER) -> SessionEntry:
        if role is Role.CUSTOMER:
            person = next(c for c in self.fixture.customers if c.id == customer_id)
            handle, password, subject = person.handle, person.password, person.id
        else:
            staff = next(o for o in self.fixture.operators if o.role is role)
            handle, password, subject = staff.handle, staff.password, staff.id
        response = self.http.post("/v1/auth/login", json={"handle": handle, "password": password})
        entry = self.ok(response, SessionEntry)
        assert entry.role is role and entry.subject_id == subject, describe(response)
        self.csrf[role] = entry.csrf_token
        self._subjects[role] = customer_id
        return entry

    def relogin(self) -> None:
        """Log in again every role this client used, for example after a restart."""
        for role, customer_id in list(self._subjects.items()):
            self.login(role, customer_id)

    # Simulator (demo role)

    def reset(self, variant: str | None = None, profile: str = "fixture_v1") -> SimResetResult:
        body = {"profile": profile, "variant": variant}
        return self.ok(self.post("/sim/reset", body), SimResetResult)

    def advance(
        self, *, days: int | None = None, minutes: int | None = None, to: datetime | None = None
    ) -> ClockView:
        if to is not None:
            body: dict[str, Any] = {"to": to.isoformat()}
        else:
            body = {"days": days, "minutes": minutes}
        return self.ok(self.post("/sim/clock/advance", body), ClockView)

    def arm(self, fault: Fault, count: int = 1, persistent: bool = False) -> FaultsView:
        body = {"fault": fault.value, "count": count, "persistent": persistent}
        return self.ok(self.post("/sim/faults", body), FaultsView)

    def incoming_payment(
        self, amount_fen: int, payer_name: str, remark: str, account_id: str = MAIN_ACCOUNT
    ) -> SimPostingResult:
        body = {
            "account_id": account_id,
            "amount_fen": amount_fen,
            "payer_name": payer_name,
            "remark": remark,
        }
        return self.ok(self.post("/sim/incoming-payment", body), SimPostingResult)

    def external_debit(
        self, amount_fen: int, merchant_raw: str, account_id: str = MAIN_ACCOUNT
    ) -> SimPostingResult:
        body = {"account_id": account_id, "amount_fen": amount_fen, "merchant_raw": merchant_raw}
        return self.ok(self.post("/sim/external-debit", body), SimPostingResult)

    def import_response(self, statement_id: str) -> httpx.Response:
        return self.post("/sim/import", {"statement_id": statement_id})

    def import_statement(self, statement_id: str) -> ImportResult:
        return self.ok(self.import_response(statement_id), ImportResult)

    def ebill(
        self,
        payee_id: str,
        kind: str,
        amount_fen: int,
        due_date: date,
        customer_id: str = MAIN_CUSTOMER,
    ) -> SimCommitmentResult:
        """A trusted e-bill from the bank: it arrives as a CANDIDATE commitment."""
        body = {
            "customer_id": customer_id,
            "payee_id": payee_id,
            "kind": kind,
            "amount_fen": amount_fen,
            "due_date": due_date.isoformat(),
        }
        return self.ok(self.post("/sim/bills/ebill", body), SimCommitmentResult)

    def sms_text(self, text: str, customer_id: str = MAIN_CUSTOMER) -> SimSmsResult:
        """Untrusted SMS text delivered to the customer's phone."""
        body = {"customer_id": customer_id, "text": text}
        return self.ok(self.post("/sim/sms", body), SimSmsResult)

    # Drafts and approval (SPEC §9)

    def draft_transfer_response(
        self,
        payee_id: str,
        amount_fen: int,
        *,
        remark: str | None = None,
        account_id: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"payee_id": payee_id, "amount_fen": amount_fen}
        if remark is not None:
            body["remark"] = remark
        if account_id is not None:
            body["account_id"] = account_id
        return self.post("/v1/drafts/transfer", body)

    def draft_transfer(self, payee_id: str, amount_fen: int, **options: Any) -> DraftResult:
        response = self.draft_transfer_response(payee_id, amount_fen, **options)
        return self.ok(response, DraftResult, (201,))

    def request_approval(self, logical_action_id: str) -> ApprovalRequestResult:
        response = self.post(f"/v1/actions/{logical_action_id}/request-approval")
        return self.ok(response, ApprovalRequestResult)

    def card(self, card_id: str) -> Card:
        return self.ok(self.get(f"/v1/cards/{card_id}"), Card, (200,))

    def open_card(self, logical_action_id: str) -> Card:
        """Ask for approval and read the card the trusted UI would show."""
        return self.card(self.request_approval(logical_action_id).card_id)

    def confirm_response(
        self, card: Card, terms_hash: str | None = None, **extra: Any
    ) -> httpx.Response:
        body = {"terms_hash": terms_hash or card.terms_hash, **extra}
        return self.post(f"/v1/cards/{card.card_id}/confirm", body)

    def confirm(self, card: Card) -> ConfirmResult:
        return self.ok(self.confirm_response(card), ConfirmResult, EXECUTION_STATUS)

    def sms(self) -> SmsView:
        return self.ok(self.get("/v1/sms"), SmsView, (200,))

    def sms_code(self, challenge_id: str) -> str:
        """The code the simulated phone shows for an open challenge."""
        codes = [m.code for m in self.sms().messages if m.challenge_id == challenge_id]
        assert len(codes) == 1, f"expected one SMS for {challenge_id}, found {len(codes)}"
        return codes[0]

    def verify_response(self, challenge_id: str, code: str) -> httpx.Response:
        return self.post(f"/v1/challenges/{challenge_id}/verify", {"code": code})

    def verify(self, challenge_id: str, code: str | None = None) -> VerifyResult:
        code = code or self.sms_code(challenge_id)
        return self.ok(self.verify_response(challenge_id, code), VerifyResult, EXECUTION_STATUS)

    def challenge_response(self, logical_action_id: str) -> httpx.Response:
        return self.post(f"/v1/actions/{logical_action_id}/challenge")

    def challenge(self, logical_action_id: str) -> ChallengeView:
        return self.ok(self.challenge_response(logical_action_id), ChallengeView)

    def reissue(self, challenge_id: str) -> ChallengeView:
        response = self.post(f"/v1/challenges/{challenge_id}/reissue")
        return self.ok(response, ChallengeView)

    def cancel_response(self, logical_action_id: str) -> httpx.Response:
        return self.post(f"/v1/actions/{logical_action_id}/cancel")

    def cancel(self, logical_action_id: str) -> CancelResult:
        return self.ok(self.cancel_response(logical_action_id), CancelResult)

    # S2 drafts (SPEC §19.2)

    def draft_response(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.post(f"/v1/drafts/{path}", body)

    def draft(self, path: str, body: dict[str, Any]) -> DraftResult:
        return self.ok(self.draft_response(path, body), DraftResult, (201,))

    def draft_reserve(self, target_fen: int) -> DraftResult:
        return self.draft("reserve", {"target_fen": target_fen})

    def pay_bill_response(
        self, commitment_id: str, amount_fen: int | None = None, account_id: str | None = None
    ) -> httpx.Response:
        body: dict[str, Any] = {"commitment_id": commitment_id}
        if amount_fen is not None:
            body["amount_fen"] = amount_fen
        if account_id is not None:
            body["account_id"] = account_id
        return self.draft_response("pay-bill", body)

    def draft_pay_bill(
        self, commitment_id: str, amount_fen: int | None = None, account_id: str | None = None
    ) -> DraftResult:
        response = self.pay_bill_response(commitment_id, amount_fen, account_id)
        return self.ok(response, DraftResult, (201,))

    def draft_confirm_bill(self, commitment_id: str) -> DraftResult:
        return self.draft("bill", {"commitment_id": commitment_id})

    def draft_new_bill(
        self, purpose: str, payee_id: str, amount_fen: int, due_date: date
    ) -> DraftResult:
        body = {
            "purpose": purpose,
            "payee_id": payee_id,
            "amount_fen": amount_fen,
            "due_date": due_date.isoformat(),
        }
        return self.draft("bill", body)

    def draft_mark_paid_external(self, commitment_id: str) -> DraftResult:
        return self.draft("mark-paid-external", {"commitment_id": commitment_id})

    def draft_cancel_bill(self, commitment_id: str) -> DraftResult:
        return self.draft("cancel-bill", {"commitment_id": commitment_id})

    def draft_resolution(self, txn_id: str, category: str) -> DraftResult:
        return self.draft("resolution", {"txn_id": txn_id, "category": category})

    def approve(self, draft: DraftResult) -> TransferOutcome:
        """Ask for approval, confirm the card and, for a RED card, verify the SMS code."""
        card = self.open_card(draft.logical_action_id)
        confirm = self.confirm(card)
        verify = None
        if confirm.challenge is not None:
            verify = self.verify(confirm.challenge.challenge_id)
        return TransferOutcome(draft=draft, card=card, confirm=confirm, verify=verify)

    def transfer(self, payee_id: str, amount_fen: int, **options: Any) -> TransferOutcome:
        """Draft, approve and, for a RED card, verify the SMS code: the customer's whole path."""
        draft = self.draft_transfer(payee_id, amount_fen, **options)
        card = self.open_card(draft.logical_action_id)
        confirm = self.confirm(card)
        verify = None
        if confirm.challenge is not None:
            verify = self.verify(confirm.challenge.challenge_id)
        return TransferOutcome(draft=draft, card=card, confirm=confirm, verify=verify)

    # Reads

    def accounts(self) -> AccountsView:
        return self.ok(self.get("/v1/accounts"), AccountsView, (200,))

    def account(self, account_id: str = MAIN_ACCOUNT) -> AccountView:
        return next(a for a in self.accounts().accounts if a.account_id == account_id)

    def balance(self, account_id: str = MAIN_ACCOUNT) -> BalanceView:
        return self.ok(self.get(f"/v1/accounts/{account_id}/balance"), BalanceView, (200,))

    def history(self, days: int = 90) -> TransferHistoryView:
        return self.ok(self.get("/v1/transfers/history", days=days), TransferHistoryView, (200,))

    def operation(self, operation_id: str) -> OperationView:
        return self.ok(self.get(f"/v1/operations/{operation_id}"), OperationView, (200,))

    def receipt(self, operation_id: str) -> ReceiptView:
        return self.ok(self.get(f"/v1/receipts/{operation_id}"), ReceiptView, (200,))

    def payees(self) -> PayeesView:
        return self.ok(self.get("/v1/payees"), PayeesView, (200,))

    def snapshot(self) -> CashSnapshotView:
        return self.ok(self.get("/v1/cash/snapshot"), CashSnapshotView, (200,))

    def bills(self, status: CommitmentStatus | None = None) -> BillsView:
        query = {"status": status.value} if status is not None else {}
        return self.ok(self.get("/v1/bills", **query), BillsView, (200,))

    def unresolved(self) -> UnresolvedView:
        return self.ok(self.get("/v1/unresolved"), UnresolvedView, (200,))

    def spending(self, days: int = 30) -> SpendingView:
        return self.ok(self.get("/v1/spending", days=days), SpendingView, (200,))

    def transactions(self, days: int = 90, limit: int = 100) -> TransactionsView:
        response = self.get("/v1/transactions", days=days, limit=limit)
        return self.ok(response, TransactionsView, (200,))

    def plans(self) -> PlansView:
        return self.ok(self.get("/v1/plans"), PlansView, (200,))

    def plan_response(self, plan_id: str) -> httpx.Response:
        return self.get(f"/v1/plans/{plan_id}")

    def plan(self, plan_id: str) -> PlanView:
        return self.ok(self.plan_response(plan_id), PlanView, (200,))

    def positions(self) -> PositionsView:
        return self.ok(self.get("/v1/funds/positions"), PositionsView, (200,))

    def exposure(
        self, key: str = "technology", target_max_pct: str | None = "40.0000"
    ) -> ExposureView:
        response = self.get("/v1/funds/exposure", key=key, target_max_pct=target_max_pct)
        return self.ok(response, ExposureView, (200,))

    def fee_estimate_response(self, product_id: str, shares: str) -> httpx.Response:
        return self.get("/v1/funds/fees/estimate", product_id=product_id, shares=shares)

    def fee_estimate(self, product_id: str, shares: str) -> FeeEstimateView:
        response = self.fee_estimate_response(product_id, shares)
        return self.ok(response, FeeEstimateView, (200,))

    # Funds plans (SPEC §14, §19.2)

    def candidates_response(self, **body: Any) -> httpx.Response:
        return self.post("/v1/funds/candidates", body)

    def candidates(self, **body: Any) -> CandidatesView:
        return self.ok(self.candidates_response(**body), CandidatesView)

    def stress(self, calc_result_id: str) -> StressView:
        response = self.post("/v1/funds/stress", {"calc_result_id": calc_result_id})
        return self.ok(response, StressView)

    def draft_plan_response(self, calc_result_id: str, with_bounds: bool = False) -> httpx.Response:
        body = {"calc_result_id": calc_result_id, "with_bounds": with_bounds}
        return self.post("/v1/funds/plans", body)

    def draft_plan(self, calc_result_id: str, with_bounds: bool = False) -> PlanDraftResult:
        response = self.draft_plan_response(calc_result_id, with_bounds)
        return self.ok(response, PlanDraftResult, (201,))

    def compose_response(
        self, draft_ids: list[str], depends_on: dict[str, list[str]] | None = None
    ) -> httpx.Response:
        body = {"draft_ids": draft_ids, "depends_on": depends_on or {}}
        return self.post("/v1/plans/compose", body)

    def compose(
        self, draft_ids: list[str], depends_on: dict[str, list[str]] | None = None
    ) -> PlanDraftResult:
        return self.ok(self.compose_response(draft_ids, depends_on), PlanDraftResult, (201,))

    def stop_plan_response(self, plan_id: str) -> httpx.Response:
        return self.post(f"/v1/plans/{plan_id}/stop")

    def stop_plan(self, plan_id: str) -> PlanView:
        return self.ok(self.stop_plan_response(plan_id), PlanView)

    def audit(self) -> AuditView:
        return self.ok(self.get("/v1/audit"), AuditView, (200,))

    # Operator (SPEC §16)

    def operator_audit(self) -> AuditView:
        return self.ok(self.get("/v1/operator/audit"), AuditView, (200,))

    def locks(self, active_only: bool = True) -> LocksView:
        response = self.get("/v1/operator/locks", active_only=str(active_only).lower())
        return self.ok(response, LocksView, (200,))

    def unlock_response(self, lock_id: str, reason: str) -> httpx.Response:
        return self.post(f"/v1/operator/locks/{lock_id}/unlock", {"reason": reason})

    def unlock(self, lock_id: str, reason: str) -> LockView:
        return self.ok(self.unlock_response(lock_id, reason), LockView, (200,))

    def reconcile(self, customer_id: str | None = None) -> ReconcileResult:
        body = {} if customer_id is None else {"customer_id": customer_id}
        return self.ok(self.post("/v1/operator/reconcile", body), ReconcileResult, (200,))
