from datetime import datetime, timedelta

import pytest

from wg.contracts.enums import ActionKind, LockScope, Tier
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.contracts.tools import READ_SCOPES, DataSource, Intent
from wg.core import k3_policy
from wg.k6 import locks
from wg.k6.db import Database
from wg.k6.simtime import SHANGHAI, to_db

pytestmark = pytest.mark.layer("D")

MAIN = "cus_main"
OTHER = "cus_other"
DAY = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)


def add_operation(
    db: Database,
    amount_fen: int,
    *,
    customer_id: str = MAIN,
    kind: str = "TRANSFER",
    status: str = "SETTLED",
    at: datetime = DAY,
) -> None:
    account = "acc_main" if customer_id == MAIN else "acc_other"
    with db.unit_of_work("test operation") as uow:
        key = new_id(IdPrefix.OPERATION)
        uow.execute(
            "INSERT INTO operations (operation_id, idempotency_key, request_hash, kind,"
            " customer_id, account_id, amount_fen, status, submitted_at, created_at)"
            " VALUES (?, ?, 'h', ?, ?, ?, ?, ?, ?, ?)",
            (key, key, kind, customer_id, account, amount_fen, status, to_db(at), to_db(at)),
        )


# Tiers (SPEC §10.1, T5)


@pytest.mark.case("T5")
@pytest.mark.parametrize(
    ("day_total_fen", "tier"),
    [(99_999, Tier.YELLOW), (100_000, Tier.YELLOW), (100_001, Tier.RED)],
)
def test_day_total_boundary_is_inclusive(day_total_fen: int, tier: Tier):
    assert k3_policy.transfer_tier_for(day_total_fen, 0) is tier
    assert k3_policy.transfer_tier_for(1, day_total_fen - 1) is tier


@pytest.mark.case("T5")
@pytest.mark.parametrize(
    ("earlier_fen", "amount_fen", "tier"),
    [(50_000, 49_999, Tier.YELLOW), (50_000, 50_000, Tier.YELLOW), (50_000, 50_001, Tier.RED)],
)
def test_tier_uses_the_day_total_from_operations(
    db: Database, earlier_fen: int, amount_fen: int, tier: Tier
):
    add_operation(db, earlier_fen)
    with db.read() as conn:
        assert k3_policy.transfer_tier(conn, MAIN, amount_fen, DAY) is tier


def test_forbidden_limits():
    assert k3_policy.limit_refusal(5_000_000, 0) is None
    assert k3_policy.limit_refusal(5_000_001, 0) is ErrorCode.SINGLE_LIMIT_EXCEEDED
    assert k3_policy.limit_refusal(5_000_000, 5_000_000) is None
    assert k3_policy.limit_refusal(1, 10_000_000) is ErrorCode.DAILY_LIMIT_EXCEEDED
    assert k3_policy.transfer_tier_for(5_000_001, 0) is Tier.FORBIDDEN
    assert k3_policy.transfer_tier_for(1, 10_000_000) is Tier.FORBIDDEN


def test_check_limits_refuses_with_the_limit_code(db: Database):
    add_operation(db, 5_000_000)
    add_operation(db, 4_000_000)
    with db.read() as conn:
        k3_policy.check_limits(conn, MAIN, 1_000_000, DAY)
        with pytest.raises(WgError) as daily:
            k3_policy.check_limits(conn, MAIN, 1_000_001, DAY)
        with pytest.raises(WgError) as single:
            k3_policy.check_limits(conn, OTHER, 5_000_001, DAY)
    assert daily.value.code is ErrorCode.DAILY_LIMIT_EXCEEDED
    assert single.value.code is ErrorCode.SINGLE_LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("kind", "tier"),
    [
        (ActionKind.RESOLVE_ITEM, Tier.GREEN),
        (ActionKind.CHECK_OUTCOME, Tier.GREEN),
        (ActionKind.CONFIRM_BILL, Tier.YELLOW),
        (ActionKind.ADD_PAYEE, Tier.RED),
        (ActionKind.CANCEL_BILL, Tier.RED),
        (ActionKind.MARK_PAID_EXTERNAL, Tier.RED),
        (ActionKind.REDEEM, Tier.RED),
        (ActionKind.PURCHASE, Tier.RED),
    ],
)
def test_fixed_tiers(kind: ActionKind, tier: Tier):
    assert k3_policy.tier_for(kind) is tier


def test_reserve_and_transfer_kinds_need_their_facts():
    reserve = ActionKind.DESIGNATE_RESERVE
    assert k3_policy.tier_for(reserve, reserve_delta_fen=100_000) is Tier.YELLOW
    assert k3_policy.tier_for(reserve, reserve_delta_fen=-1) is Tier.RED
    assert k3_policy.tier_for(ActionKind.PAY_BILL, amount_fen=100_001) is Tier.RED
    with pytest.raises(ValueError):
        k3_policy.tier_for(ActionKind.TRANSFER)
    with pytest.raises(ValueError):
        k3_policy.tier_for(ActionKind.DESIGNATE_RESERVE)
    assert k3_policy.PLAN_APPROVAL_TIER is Tier.YELLOW


def test_max_tier():
    assert k3_policy.max_tier(Tier.YELLOW, Tier.GREEN) is Tier.YELLOW
    assert k3_policy.max_tier(Tier.RED, Tier.FORBIDDEN, Tier.YELLOW) is Tier.FORBIDDEN


# Daily sum (SPEC §10.2)


def test_daily_sum_counts_each_transfer_once_on_its_shanghai_day(db: Database):
    add_operation(db, 10_000, status="SUBMITTED")
    add_operation(db, 20_000, status="SETTLED", kind="PAY_BILL")
    add_operation(db, 40_000, status="UNKNOWN")
    add_operation(db, 80_000, status="REJECTED")
    add_operation(db, 160_000, kind="PURCHASE")
    add_operation(db, 320_000, customer_id=OTHER)
    start = datetime(2026, 11, 2, 0, 0, tzinfo=SHANGHAI)
    add_operation(db, 1, at=start)
    add_operation(db, 2, at=start - timedelta(microseconds=1))
    add_operation(db, 4, at=start + timedelta(days=1))
    with db.read() as conn:
        assert k3_policy.daily_sum(conn, MAIN, DAY.date()) == 70_001
        assert k3_policy.daily_sum(conn, MAIN, DAY.date() - timedelta(days=1)) == 2
        assert k3_policy.daily_sum(conn, OTHER, DAY.date()) == 320_000


def test_daily_sum_reads_shanghai_days_from_utc_instants(db: Database):
    # 2026-11-01 16:30 UTC is 2026-11-02 00:30 in Shanghai.
    add_operation(db, 7, at=datetime.fromisoformat("2026-11-01T16:30:00+00:00"))
    with db.read() as conn:
        assert k3_policy.daily_sum(conn, MAIN, DAY.date()) == 7


# Locks (SPEC §16)


def test_writes_under_a_lock_are_refused_unless_risk_reducing(db: Database):
    with db.unit_of_work("test lock") as uow:
        locks.set_lock(uow, LockScope.CUSTOMER, MAIN, "test")
    with db.read() as conn:
        with pytest.raises(WgError) as refused:
            k3_policy.check_not_locked(conn, MAIN, None)
        assert refused.value.code is ErrorCode.LOCKED_WRITE
        assert refused.value.http_status == 423
        k3_policy.check_not_locked(conn, MAIN, None, risk_reducing=True)
        k3_policy.check_not_locked(conn, OTHER, None)


# Read scopes (SPEC §10.3)


def test_registry_is_read_from_the_seeded_table(db: Database):
    with db.read() as conn:
        for intent, sources in READ_SCOPES.items():
            assert k3_policy.allowed_sources(conn, intent) == sources


def test_read_in_scope_passes_and_counts_nothing(db: Database):
    k3_policy.check_read_scope(
        db,
        customer_id=MAIN,
        tool="get_balance",
        intent=Intent.BALANCE_QUERY,
        sources=frozenset({DataSource.ACCOUNTS}),
    )
    with db.read() as conn:
        assert k3_policy.extra_read_count(conn) == 0


def test_read_outside_scope_is_refused_and_counted_in_the_audit(db: Database):
    for _ in range(2):
        with pytest.raises(WgError) as refused:
            k3_policy.check_read_scope(
                db,
                customer_id=MAIN,
                tool="get_transactions",
                intent=Intent.BALANCE_QUERY,
                sources=frozenset({DataSource.ACCOUNTS, DataSource.TRANSACTIONS}),
            )
        assert refused.value.code is ErrorCode.READ_SCOPE_DENIED
        assert refused.value.detail["sources"] == ["transactions"]
    with db.read() as conn:
        assert k3_policy.extra_read_count(conn) == 2
        assert k3_policy.extra_read_count(conn, MAIN) == 2
        assert k3_policy.extra_read_count(conn, OTHER) == 0
        row = conn.execute(
            "SELECT actor, ref, decision FROM audit_log WHERE event = 'extra_read'"
        ).fetchone()
    assert tuple(row) == ("agent", "get_transactions", "READ_SCOPE_DENIED")


def test_read_scope_check_never_runs_inside_a_unit(db: Database):
    with db.unit_of_work("outer") as _:
        with pytest.raises(RuntimeError):
            k3_policy.check_read_scope(
                db,
                customer_id=MAIN,
                tool="get_accounts",
                intent=Intent.BALANCE_QUERY,
                sources=frozenset({DataSource.ACCOUNTS}),
            )
