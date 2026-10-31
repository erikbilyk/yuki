"""Direct-debit agreements: reads (S5-SPEC §3.5) and the customer's decision drafts (§6.3).

Lists show only the customer's own requests and never probes; a foreign, probe or unknown
request is the same NOT_FOUND. Merchant names come from the registry, amounts from rows.

A decision draft takes the amount and the account from the request and the agreement only.
A payment draft runs the whole payment gate: a request that cannot be paid, a changed
agreement or an amount above the single limit refuses the draft, and the money reasons are
listed on it like a transfer's.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from wg.contracts.api import (
    DebitDecisionRequest,
    DebitRequestsQuery,
    DebitRequestsView,
    DebitRequestView,
    DraftResult,
    MandatesView,
    MandateView,
    MerchantsView,
    MerchantView,
    ProtectionPeriodView,
    SubscriptionSpendQuery,
    SubscriptionSpendRow,
    SubscriptionSpendView,
)
from wg.contracts.enums import (
    ActionKind,
    AuditActor,
    DebitDecision,
    DebitRequestStatus,
    MandateStatus,
    Provenance,
)
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.core import mandate_rules
from wg.core.k1_session import not_found
from wg.core.k3_policy import check_not_locked, is_risk_reducing
from wg.k6 import actions, debit_requests, mandates
from wg.k6.actions import ActionRow
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.debit_requests import DebitRequestRow, PaidRequest
from wg.k6.mandates import MandateRow
from wg.k6.simtime import SHANGHAI, now
from wg.packs.base import Infeasible
from wg.packs.mandates import OPEN_DECISION_STATUSES, decision_target
from wg.packs.registry import pack_for
from wg.services.actions import ActionService
from wg.services.refusals import record_refusal

# Refusals that end a decision draft at once; the money reasons are listed on the draft.
_REFUSED_AT_DRAFT = frozenset(
    {
        ErrorCode.REQUEST_NOT_PAYABLE,
        ErrorCode.MANDATE_CHANGED,
        ErrorCode.SINGLE_LIMIT_EXCEEDED,
    }
)
_DECISION_KINDS = {
    DebitDecision.PAY: ActionKind.DEBIT_PAY,
    DebitDecision.DECLINE: ActionKind.DEBIT_DECLINE,
}
_SERVER_RECORD = {"provenance": Provenance.SERVER_RECORD.value}


class MandateService:
    def __init__(self, db: Database, action_service: ActionService) -> None:
        self.db = db
        self.action_service = action_service

    def mandates(self, customer_id: str) -> MandatesView:
        """Every agreement of the customer; the monthly total counts the ACTIVE ones."""
        with self.db.read() as conn:
            at = now(conn)
            views = [
                _mandate_view(conn, mandate, at)
                for mandate in mandates.customer_mandates(conn, customer_id)
            ]
        return MandatesView(
            as_of=at,
            monthly_total_fen=sum(
                view.expected_fen for view in views if view.status is MandateStatus.ACTIVE
            ),
            mandates=tuple(views),
        )

    def merchants(self) -> MerchantsView:
        with self.db.read() as conn:
            rows = mandates.verified_merchants(conn)
        return MerchantsView(
            merchants=tuple(
                MerchantView(merchant_id=row.merchant_id, display_name=row.display_name)
                for row in rows
            )
        )

    def debit_requests(self, customer_id: str, query: DebitRequestsQuery) -> DebitRequestsView:
        with self.db.read() as conn:
            at = now(conn)
            rows = debit_requests.customer_requests(
                conn, customer_id, status=query.status, mandate_ref=query.mandate_ref
            )
            views = tuple(_request_view(conn, row, at) for row in rows)
        return DebitRequestsView(requests=views)

    def debit_request(self, customer_id: str, request_id: str) -> DebitRequestView:
        with self.db.read() as conn:
            row = debit_requests.find_request(conn, request_id)
            if row is None or row.probe or row.customer_id != customer_id:
                raise not_found()
            return _request_view(conn, row, now(conn))

    def subscription_spend(
        self, customer_id: str, query: SubscriptionSpendQuery
    ) -> SubscriptionSpendView:
        """Payments of requests in the last `days`, one row per agreement of the customer."""
        with self.db.read() as conn:
            since = now(conn) - timedelta(days=query.days)
            paid: dict[str, list[PaidRequest]] = defaultdict(list)
            for request in debit_requests.paid_requests(conn, customer_id, since):
                paid[request.mandate_ref].append(request)
            rows = tuple(
                _spend_row(conn, mandate.mandate_ref, paid[mandate.mandate_ref])
                for mandate in mandates.customer_mandates(conn, customer_id)
            )
        return SubscriptionSpendView(
            days=query.days, rows=rows, total_fen=sum(row.paid_fen for row in rows)
        )

    # Drafts (S5-SPEC §6.3)

    def draft_debit_decision(
        self,
        customer_id: str,
        body: DebitDecisionRequest,
        actor: AuditActor = AuditActor.CUSTOMER,
    ) -> DraftResult:
        """A DEBIT_PAY or DEBIT_DECLINE in DRAFT for a request the customer can still answer."""
        with self._audited(customer_id, "draft_refused", body.request_id, actor):
            action, reasons = self.db.run_in_uow(
                lambda uow: self._insert_decision(uow, customer_id, body, actor),
                "draft debit decision",
            )
        return DraftResult(
            logical_action_id=action.logical_action_id,
            revision=action.revision,
            kind=action.kind,
            status=action.status,
            tier=action.tier,
            feasible=not reasons,
            reasons=tuple(r.code for r in reasons),
        )

    def _insert_decision(
        self, uow: UnitOfWork, customer_id: str, body: DebitDecisionRequest, actor: AuditActor
    ) -> tuple[ActionRow, tuple[Infeasible, ...]]:
        request = debit_requests.find_request(uow, body.request_id)
        if request is None or request.probe or request.customer_id != customer_id:
            raise not_found()
        assert request.mandate_ref is not None
        mandate, request = decision_target(uow, customer_id, request.mandate_ref, body.request_id)
        kind = _DECISION_KINDS[body.decision]
        pack = pack_for(kind)
        params = pack.canonical(kind, _decision_params(kind, mandate, request))
        check_not_locked(
            uow,
            customer_id,
            None,
            risk_reducing=is_risk_reducing(uow, customer_id, kind, params),
        )
        reasons = pack.precheck(uow, customer_id, kind, params, now(uow))
        for reason in reasons:
            if reason.code in _REFUSED_AT_DRAFT:
                raise reason.error()
        for open_action in actions.actions_of_kinds(
            uow, {kind}, OPEN_DECISION_STATUSES, customer_id
        ):
            if open_action.params.get("request_id") == request.request_id:
                raise WgError(
                    ErrorCode.DUPLICATE_INTENT,
                    {"logical_action_id": open_action.logical_action_id},
                )
        provenance: dict[str, Any] = {
            "request_id": {"provenance": Provenance.USER_UTTERANCE.value},
            "mandate_ref": _SERVER_RECORD,
        }
        if kind is ActionKind.DEBIT_PAY:
            provenance.update(
                account_id=_SERVER_RECORD, mandate_version=_SERVER_RECORD, amount_fen=_SERVER_RECORD
            )
        action = self.action_service.insert_draft(
            uow, customer_id, kind, params, provenance, reasons, actor
        )
        return action, reasons

    @contextmanager
    def _audited(self, customer_id: str, event: str, ref: str, actor: AuditActor) -> Iterator[None]:
        try:
            yield
        except WgError as error:
            if error.error_class is ErrorClass.EXPECTED:
                record_refusal(self.db, customer_id, event, ref, error, actor=actor)
            raise


def _decision_params(
    kind: ActionKind, mandate: MandateRow, request: DebitRequestRow
) -> dict[str, Any]:
    """Every value from the stored request and agreement; nothing from the merchant's text."""
    params: dict[str, Any] = {"mandate_ref": mandate.mandate_ref, "request_id": request.request_id}
    if kind is ActionKind.DEBIT_PAY:
        params.update(
            account_id=mandate.account_id,
            mandate_version=mandate.version,
            amount_fen=request.amount_fen,
        )
    return params


def _mandate_view(reader: Reader, mandate: MandateRow, at: datetime) -> MandateView:
    requests = debit_requests.mandate_requests(reader, mandate.mandate_ref)
    return MandateView(
        mandate_ref=mandate.mandate_ref,
        merchant_id=mandate.merchant_id,
        merchant_display=mandates.merchant_display(reader, mandate.mandate_ref),
        account_id=mandate.account_id,
        status=mandate.status,
        expected_fen=mandate_rules.expected_amount(mandate, requests),
        cap_fen=mandate.cap_fen,
        period="monthly",
        debit_day=mandate.debit_day,
        next_debit_date=mandate_rules.next_debit_date(mandate, at.astimezone(SHANGHAI).date()),
        starts_on=mandate.starts_on,
        expires_on=mandate.expires_on,
        version=mandate.version,
        protection=tuple(
            ProtectionPeriodView(
                period_key=period.period_key,
                due_date=period.due_date,
                expected_fen=period.expected_fen,
                until=period.until,
            )
            for period in mandate_rules.protected_periods(mandate, requests, at)
        ),
        pending_requests=sum(1 for r in requests if r.status is DebitRequestStatus.AWAITING_CLIENT),
    )


def _request_view(reader: Reader, row: DebitRequestRow, at: datetime) -> DebitRequestView:
    assert row.mandate_ref is not None, "requests without an agreement are never shown"
    mandate = mandates.find_mandate(reader, row.mandate_ref)
    assert mandate is not None
    return DebitRequestView(
        request_id=row.request_id,
        mandate_ref=row.mandate_ref,
        merchant_id=row.merchant_id,
        merchant_display=mandates.merchant_display(reader, row.mandate_ref),
        amount_fen=row.amount_fen,
        currency="CNY",
        period_key=row.period_key,
        status=row.status,
        reason_code=row.reason_code,
        received_at=row.received_at,
        respond_by=row.respond_by,
        description_raw=row.description_raw,
        payable=_payable(row, mandate, at),
        operation_id=row.operation_id,
    )


def _payable(row: DebitRequestRow, mandate: MandateRow, at: datetime) -> bool:
    """Awaiting the customer, before its deadline, under an ACTIVE, unexpired agreement and cap."""
    return (
        row.status is DebitRequestStatus.AWAITING_CLIENT
        and at < row.respond_by
        and mandate.status is MandateStatus.ACTIVE
        and mandate_rules.authorizes(mandate.expires_on, at)
        and row.amount_fen <= mandate.cap_fen
    )


def _spend_row(reader: Reader, mandate_ref: str, paid: list[PaidRequest]) -> SubscriptionSpendRow:
    last = paid[-1] if paid else None
    return SubscriptionSpendRow(
        mandate_ref=mandate_ref,
        merchant_display=mandates.merchant_display(reader, mandate_ref),
        paid_count=len(paid),
        paid_fen=sum(p.amount_fen for p in paid),
        last_paid_on=last.paid_at.astimezone(SHANGHAI).date() if last else None,
        last_amount_fen=last.amount_fen if last else None,
    )
