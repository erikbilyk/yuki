from datetime import UTC, timedelta

import pytest

from wg.contracts.enums import Fault, LockScope
from wg.contracts.events import ClockAdvanced, DomainEventType, LockSet
from wg.k6 import audit, events, faults, locks
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.simtime import SHANGHAI, ClockError, read_clock

from .helpers import EPOCH, add_customer, add_plan, count

pytestmark = pytest.mark.layer("D")


def clock(db: Database):
    with db.read() as conn:
        return read_clock(conn)


# Simulated clock


def test_clock_starts_at_the_epoch(db: Database):
    state = clock(db)
    assert state.now == state.epoch == EPOCH
    assert state.day == 0


def test_clock_advances_with_audit_and_event(db: Database):
    target = EPOCH + timedelta(days=2, minutes=5)
    with db.unit_of_work("advance") as uow:
        state = advance_clock(uow, target)
    assert state.now == target and state.day == 2
    with db.read() as conn:
        rows = audit.list_audit(conn)
        pending = events.unprocessed_events(conn, "test")
    assert [r.event for r in rows] == ["clock_advanced"]
    assert rows[0].ref == "T+2"
    assert pending[0].type is DomainEventType.CLOCK_ADVANCED
    assert pending[0].payload == ClockAdvanced(from_at=EPOCH, to_at=target)


@pytest.mark.case("R8")
def test_clock_cannot_move_backwards(db: Database):
    with db.unit_of_work("advance") as uow:
        advance_clock(uow, EPOCH + timedelta(hours=1))
    with pytest.raises(ClockError, match="backwards"):
        with db.unit_of_work("back") as uow:
            advance_clock(uow, EPOCH)
    assert clock(db).now == EPOCH + timedelta(hours=1)
    with db.unit_of_work("same") as uow:
        advance_clock(uow, EPOCH + timedelta(hours=1))
    assert count(db, "audit_log") == 1


def test_clock_day_follows_shanghai_midnight(db: Database):
    with db.unit_of_work("advance") as uow:
        before = advance_clock(uow, EPOCH.replace(hour=23, minute=59))
        after = advance_clock(uow, EPOCH + timedelta(hours=15))
    assert before.day == 0
    assert after.day == 1
    assert after.now.tzinfo == SHANGHAI


def test_clock_accepts_other_offsets(db: Database):
    utc_target = (EPOCH + timedelta(days=3)).astimezone(UTC)
    with db.unit_of_work("advance") as uow:
        state = advance_clock(uow, utc_target)
    assert state.now == utc_target and state.day == 3


# Domain events and cursors


def lock_payload(n: int) -> LockSet:
    return LockSet(lock_id=f"lck_{n}", scope=LockScope.GLOBAL, scope_ref="global", reason="r")


def test_events_keep_insertion_order_and_cursors_resume(db: Database):
    with db.unit_of_work("events") as uow:
        ids = [
            events.append_event(uow, DomainEventType.LOCK_SET, lock_payload(n)) for n in range(12)
        ]
    assert ids == sorted(ids) and len(set(ids)) == 12
    with db.read() as conn:
        first = events.unprocessed_events(conn, "coordinator", limit=5)
    assert [e.event_id for e in first] == ids[:5]
    with db.unit_of_work("consume") as uow:
        events.mark_processed(uow, "coordinator", first[-1].event_id)
        events.mark_processed(uow, "coordinator", ids[1])

    restarted = Database(db.path)
    with restarted.read() as conn:
        rest = events.unprocessed_events(conn, "coordinator")
        other = events.unprocessed_events(conn, "snapshots")
    restarted.close()
    assert [e.event_id for e in rest] == ids[5:]
    assert len(other) == 12
    assert rest[0].payload == lock_payload(5)


def test_event_cursor_rolls_back_with_its_unit(db: Database):
    with db.unit_of_work("events") as uow:
        event_id = events.append_event(uow, DomainEventType.LOCK_SET, lock_payload(1))
    with pytest.raises(RuntimeError), db.unit_of_work("consume") as uow:
        events.mark_processed(uow, "coordinator", event_id)
        raise RuntimeError("handler failed")
    with db.read() as conn:
        assert len(events.unprocessed_events(conn, "coordinator")) == 1


def test_event_payload_must_match_its_type(db: Database):
    with pytest.raises(TypeError), db.unit_of_work("events") as uow:
        events.append_event(uow, DomainEventType.CLOCK_ADVANCED, lock_payload(1))
    with pytest.raises(ValueError, match="unknown event"), db.unit_of_work("x") as uow:
        events.mark_processed(uow, "coordinator", "evt_000000000099")


# Audit


def test_audit_rows_filter_by_customer(db: Database):
    with db.unit_of_work("audit") as uow:
        audit.record(uow, "customer", "card_confirmed", customer_id="cus_main", ref="act_1")
        audit.record(uow, "system", "clock_advanced")
        audit.record(uow, "operator", "lock_cleared", customer_id="cus_other")
    with db.read() as conn:
        mine = audit.list_audit(conn, customer_id="cus_main")
        everything = audit.list_audit(conn)
    assert [(r.event, r.ref, r.at) for r in mine] == [("card_confirmed", "act_1", EPOCH)]
    assert [r.event for r in everything] == ["lock_cleared", "clock_advanced", "card_confirmed"]


# Locks


def test_lock_set_and_clear_with_events_and_audit(db: Database):
    add_plan(db, "pln_one")
    with db.unit_of_work("operator") as uow:
        uow.execute(
            "INSERT INTO operators (operator_id, handle, password_hash, role, created_at)"
            " VALUES ('opr_ops', 'ops', 'x', 'operator', 't')"
        )
    with db.unit_of_work("lock") as uow:
        lock = locks.set_lock(uow, LockScope.PLAN, "pln_one", "breaker")
        again = locks.set_lock(uow, LockScope.PLAN, "pln_one", "breaker")
    assert again.lock_id == lock.lock_id and lock.active
    with db.read() as conn:
        assert [x.lock_id for x in locks.locks_in_scope(conn, "cus_main", "pln_one")] == [
            lock.lock_id
        ]
        assert locks.locks_in_scope(conn, "cus_main", "pln_other") == []
    with db.unit_of_work("unlock") as uow:
        cleared = locks.clear_lock(uow, lock.lock_id, "opr_ops", "reconciled")
    assert not cleared.active and cleared.cleared_by == "opr_ops"
    with pytest.raises(ValueError, match="already cleared"), db.unit_of_work("again") as uow:
        locks.clear_lock(uow, lock.lock_id, "opr_ops", "twice")
    with db.read() as conn:
        assert locks.list_locks(conn) == []
        assert len(locks.list_locks(conn, active_only=False)) == 1
        kinds = [e.type for e in events.unprocessed_events(conn, "t")]
        trail = [(r.event, r.customer_id) for r in audit.list_audit(conn)]
    assert kinds == [DomainEventType.LOCK_SET, DomainEventType.LOCK_CLEARED]
    assert trail == [("lock_cleared", "cus_main"), ("lock_set", "cus_main")]


def test_customer_and_global_locks_cover_every_plan(db: Database):
    add_customer(db, "cus_main")
    with db.unit_of_work("lock") as uow:
        locks.set_lock(uow, LockScope.CUSTOMER, "cus_main", "ledger")
    with db.read() as conn:
        assert len(locks.locks_in_scope(conn, "cus_main", "pln_any")) == 1
        assert locks.locks_in_scope(conn, "cus_other") == []
    with db.unit_of_work("lock") as uow:
        glob = locks.set_lock(uow, LockScope.GLOBAL, "ignored", "incident")
    assert glob.scope_ref == "global"
    with db.read() as conn:
        assert len(locks.locks_in_scope(conn, "cus_other")) == 1


# Faults


def test_fault_fires_count_times_then_disarms(db: Database):
    with db.unit_of_work("arm") as uow:
        faults.arm(uow, Fault.BANK_ACK_LOST, count=2)
    fired = [faults.take(db, Fault.BANK_ACK_LOST) for _ in range(3)]
    assert fired == [True, True, False]
    with db.read() as conn:
        assert faults.armed_faults(conn) == []
        payloads = [e.payload for e in events.unprocessed_events(conn, "t")]
    assert [p.remaining_count for p in payloads] == [1, 0]


def test_fault_consumption_rolls_back_with_its_unit(db: Database):
    with db.unit_of_work("arm") as uow:
        faults.arm(uow, Fault.UNEXPECTED)
    with pytest.raises(RuntimeError), db.unit_of_work("execute") as uow:
        assert faults.consume(uow, Fault.UNEXPECTED)
        raise RuntimeError("fault fired")
    with db.read() as conn:
        assert [f.remaining_count for f in faults.armed_faults(conn)] == [1]


def test_only_persistent_faults_survive_a_restart(db: Database):
    with db.unit_of_work("arm") as uow:
        faults.arm(uow, Fault.CRASH_AFTER_COMMIT, count=3, persistent=True)
        faults.arm(uow, Fault.DROP_RESPONSE)
        faults.arm(uow, Fault.DROP_RESPONSE, count=4)
    with db.read() as conn:
        assert {(f.fault, f.remaining_count) for f in faults.armed_faults(conn)} == {
            (Fault.CRASH_AFTER_COMMIT, 3),
            (Fault.DROP_RESPONSE, 4),
        }
    with db.unit_of_work("start") as uow:
        assert faults.clear_transient(uow) == 1
    with db.read() as conn:
        remaining = faults.armed_faults(conn)
    assert [(f.fault, f.persistent) for f in remaining] == [(Fault.CRASH_AFTER_COMMIT, True)]


def test_fault_count_must_be_positive(db: Database):
    with pytest.raises(ValueError), db.unit_of_work("arm") as uow:
        faults.arm(uow, Fault.BUSY, count=0)
