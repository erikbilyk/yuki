"""Reads of direct-debit agreements, merchants' requests and subscription spending (S5-SPEC §3.5).

Lists show only the customer's own requests and never probes; a foreign, probe or unknown
request is the same NOT_FOUND. Merchant names come from the registry, amounts from rows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from wg.contracts.api import (
    DebitRequestsQuery,
    DebitRequestsView,
    DebitRequestView,
    MandatesView,
    MandateView,
    MerchantsView,
    MerchantView,
    ProtectionPeriodView,
    SubscriptionSpendQuery,
    SubscriptionSpendRow,
    SubscriptionSpendView,
)
from wg.contracts.enums import DebitRequestStatus, MandateStatus
from wg.core import mandate_rules
from wg.core.k1_session import not_found
from wg.k6 import debit_requests, mandates
from wg.k6.db import Database, Reader
from wg.k6.debit_requests import DebitRequestRow, PaidRequest
from wg.k6.mandates import MandateRow
from wg.k6.simtime import SHANGHAI, now


class MandateService:
    def __init__(self, db: Database) -> None:
        self.db = db

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
