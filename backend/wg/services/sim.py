"""Simulator controls for demos and tests: reset, clock, faults, payments in (SPEC §12.7, §15.3)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import timedelta

from wg.contracts.api import (
    AgentModeRequest,
    ClockAdvanceRequest,
    ClockView,
    EbillRequest,
    ExternalDebitRequest,
    FaultRequest,
    FaultsView,
    FaultView,
    ImportRequest,
    ImportResult,
    IncomingPaymentRequest,
    SimCommitmentResult,
    SimDebitRequest,
    SimDebitResult,
    SimPostingResult,
    SimResetRequest,
    SimResetResult,
    SimSmsRequest,
    SimSmsResult,
)
from wg.contracts.enums import AuditActor
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.fixture import FixtureError
from wg.core.hashes import sha256_hex
from wg.core.k1_session import not_found, wall_clock
from wg.core.k5_coordinator import Coordinator
from wg.k6 import (
    accounts,
    audit,
    bank,
    commitments,
    debit_requests,
    faults,
    fixture,
    imports,
    sessions,
    snapshots,
)
from wg.k6.db import Database, UnitOfWork
from wg.k6.gateway import CRASH_EXIT_STATUS
from wg.k6.simtime import ClockState, now, read_clock

log = logging.getLogger("wg.sim")

EBILL_SOURCE = "trusted_ebill"


def clock_view(state: ClockState) -> ClockView:
    return ClockView(now=state.now, epoch=state.epoch, day=state.day)


class SimService:
    def __init__(
        self,
        db: Database,
        coordinator: Coordinator,
        *,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        self.db = db
        self.coordinator = coordinator
        self._exit = exit_process

    def reset(self, request: SimResetRequest) -> SimResetResult:
        """Load the profile and variant in place of the current content; the clock returns to T0."""
        try:
            profile = fixture.read_profile(request.profile, request.variant)
        except FixtureError:
            raise WgError(
                ErrorCode.UNSUPPORTED_REQUEST,
                {"profile": request.profile, "variant": request.variant},
            ) from None
        state = fixture.reset(self.db, profile, request.variant)
        return SimResetResult(
            profile=request.profile, variant=request.variant, clock=clock_view(state)
        )

    def advance_clock(self, request: ClockAdvanceRequest) -> ClockView:
        """Walk the clock to `to`, or forward by days and minutes, through every due instant."""
        with self.db.read() as conn:
            current = read_clock(conn).now
        if request.to is not None:
            target = request.to
        else:
            target = current + timedelta(days=request.days or 0, minutes=request.minutes or 0)
        return clock_view(self.coordinator.advance_clock(target))

    def crash(self) -> None:
        """End the process at once, as a power cut would; the next start recovers (SPEC §12.8)."""
        self.db.run_in_uow(
            lambda uow: audit.record(uow, AuditActor.SYSTEM, "sim_crash"), "sim crash"
        )
        log.warning("simulated crash")
        self._exit(CRASH_EXIT_STATUS)

    def set_agent_mode(self, request: AgentModeRequest) -> int:
        """Set the agent mode of every current customer session (SPEC §26); returns how many.

        A session logged in afterwards starts coordinated, so the mode is set after login.
        """

        def run(uow: UnitOfWork) -> int:
            count = sessions.set_agent_mode(uow, request.mode, wall_clock())
            audit.record(
                uow,
                AuditActor.SYSTEM,
                "agent_mode_set",
                decision=request.mode.value,
                reason=str(count),
            )
            return count

        return self.db.run_in_uow(run, "sim agent mode")

    def arm_fault(self, request: FaultRequest) -> FaultsView:
        def arm(uow: UnitOfWork) -> FaultsView:
            faults.arm(uow, request.fault, request.count, request.persistent)
            return FaultsView(
                faults=tuple(
                    FaultView(
                        fault=f.fault,
                        remaining_count=f.remaining_count,
                        persistent=f.persistent,
                        armed_at=f.armed_at,
                    )
                    for f in faults.armed_faults(uow)
                )
            )

        return self.db.run_in_uow(arm, "sim arm fault")

    def incoming_payment(self, request: IncomingPaymentRequest) -> SimPostingResult:
        """Money from outside arrives; payer name and remark are untrusted text (T10)."""

        def post(uow: UnitOfWork) -> SimPostingResult:
            account = accounts.find_account(uow, request.account_id)
            if account is None or account.customer_id is None:
                raise not_found()
            operation = bank.post_incoming_payment(
                uow,
                customer_id=account.customer_id,
                account_id=account.account_id,
                amount_fen=request.amount_fen,
                payer_name=request.payer_name,
                remark=request.remark,
                at=now(uow),
            )
            after = accounts.find_account(uow, account.account_id)
            assert after is not None
            return SimPostingResult(
                operation_id=operation.operation_id,
                account_id=account.account_id,
                amount_fen=request.amount_fen,
                posted_fen_after=after.balance_fen,
            )

        return self.db.run_in_uow(post, "sim incoming payment")

    def external_debit(self, request: ExternalDebitRequest) -> SimPostingResult:
        """A debit the bank did not take through YUKI; the merchant text is untrusted."""

        def post(uow: UnitOfWork) -> SimPostingResult:
            account = accounts.find_account(uow, request.account_id)
            if account is None or account.customer_id is None:
                raise not_found()
            operation = bank.post_external_debit(
                uow,
                customer_id=account.customer_id,
                account_id=account.account_id,
                amount_fen=request.amount_fen,
                merchant=request.merchant_raw,
                at=now(uow),
            )
            after = accounts.find_account(uow, account.account_id)
            assert after is not None
            return SimPostingResult(
                operation_id=operation.operation_id,
                account_id=account.account_id,
                amount_fen=-request.amount_fen,
                posted_fen_after=after.balance_fen,
            )

        return self.db.run_in_uow(post, "sim external debit")

    def import_statement(self, request: ImportRequest) -> ImportResult:
        """Import a statement of the loaded profile again; lines already stored change nothing."""
        with self.db.read() as conn:
            row = conn.execute("SELECT profile, variant FROM sim_profile").fetchone()
        if row is None:
            raise not_found()
        profile = fixture.read_profile(row["profile"], row["variant"])
        source = next((s for s in profile.statements if s.id == request.statement_id), None)
        if source is None:
            raise not_found()

        def run(uow: UnitOfWork) -> ImportResult:
            account = accounts.find_account(uow, source.account)
            assert account is not None and account.customer_id is not None
            statement = fixture.statement_of(source, read_clock(uow).epoch_date)
            outcome = imports.import_statement(uow, statement)
            snapshot = snapshots.ensure_snapshot(uow, account.customer_id)
            audit.record(
                uow,
                AuditActor.SYSTEM,
                "statement_imported",
                customer_id=account.customer_id,
                ref=statement.statement_id,
                reason=f"new={outcome.new_count} duplicate={outcome.duplicate_count}",
            )
            return ImportResult(
                statement_id=statement.statement_id,
                new_transactions=outcome.new_count,
                duplicate_transactions=outcome.duplicate_count,
                new_commitments=0,
                snapshot_id=snapshot.snapshot_id,
                inputs_hash=snapshot.inputs_hash,
            )

        return self.db.run_in_uow(run, "sim import")

    def ebill(self, request: EbillRequest) -> SimCommitmentResult:
        """A bill from the bank's own e-bill service: a candidate the customer confirms."""

        def post(uow: UnitOfWork) -> SimCommitmentResult:
            payee = accounts.find_payee(uow, request.payee_id)
            if payee is None or payee.customer_id != request.customer_id or not payee.active:
                raise not_found()
            bill = commitments.find_duplicate_candidate(
                uow,
                request.customer_id,
                payee.payee_id,
                request.kind,
                request.amount_fen,
                request.due_date,
            )
            if bill is None:
                bill = commitments.insert_candidate(
                    uow,
                    customer_id=request.customer_id,
                    payee_id=payee.payee_id,
                    kind=request.kind,
                    amount_fen=request.amount_fen,
                    due_date=request.due_date,
                    source=EBILL_SOURCE,
                )
                audit.record(
                    uow,
                    AuditActor.SYSTEM,
                    "ebill_received",
                    customer_id=request.customer_id,
                    ref=bill.commitment_id,
                )
            return SimCommitmentResult(commitment_id=bill.commitment_id, status=bill.status)

        return self.db.run_in_uow(post, "sim ebill")

    def debit_request(self, request: SimDebitRequest) -> SimDebitResult:
        """A merchant asks to debit a customer under an agreement (S5-SPEC §5.2).

        The request is only checked and stored; nothing is paid until the customer decides.
        """
        received = debit_requests.receive_debit_request(
            self.db,
            debit_requests.IncomingDebit(
                merchant_id=request.merchant_id,
                merchant_request_id=request.merchant_request_id,
                mandate_ref=request.mandate_ref,
                amount_fen=request.amount_fen,
                currency=request.currency,
                description_raw=request.description_raw,
            ),
        )
        row = received.request
        return SimDebitResult(
            request_id=row.request_id,
            status=row.status,
            reason_code=row.reason_code,
            respond_by=row.respond_by,
            duplicate=received.duplicate,
        )

    def sms(self, request: SimSmsRequest) -> SimSmsResult:
        """Untrusted SMS text. It creates no bill and sets no payee, amount or status.

        The text is not stored; the audit row keeps a short digest so the event can be matched
        with the message later.
        """

        def record(uow: UnitOfWork) -> SimSmsResult:
            audit.record(
                uow,
                AuditActor.SYSTEM,
                "untrusted_sms",
                customer_id=request.customer_id,
                ref=sha256_hex({"text": request.text})[:12],
                decision="no_action",
            )
            return SimSmsResult(commitment_id=None)

        with self.db.read() as conn:
            known = conn.execute(
                "SELECT 1 FROM customers WHERE customer_id = ?", (request.customer_id,)
            ).fetchone()
        if known is None:
            raise not_found()
        return self.db.run_in_uow(record, "sim sms")
