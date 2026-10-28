"""Fault points of the gateway fire only when armed, each at its own point (SPEC §12.7)."""

import pytest

from wg.contracts.enums import Fault, OperationStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.k6.gateway import CRASH_EXIT_STATUS, ResponseDropped

from .conftest import START_FEN, Bench, Exited, Paused

pytestmark = pytest.mark.layer("D")


def transfers(bench: Bench) -> list[dict]:
    return bench.rows("SELECT * FROM operations WHERE kind = 'TRANSFER'")


def test_no_fault_fires_when_none_is_armed(bench: Bench):
    for _ in range(3):
        bench.submit(bench.ready(10_000))
    assert bench.hooks == []
    assert bench.rows("SELECT * FROM failure_records") == []
    assert bench.rows("SELECT * FROM domain_events WHERE type = 'fault_fired'") == []


@pytest.mark.case("T11")
def test_crash_before_commit_leaves_nothing_and_does_not_rearm(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.CRASH_BEFORE_COMMIT)

    with pytest.raises(Exited) as exited:
        bench.submit(action)

    assert exited.value.status == CRASH_EXIT_STATUS
    assert transfers(bench) == []
    assert bench.balance() == START_FEN
    assert Fault.CRASH_BEFORE_COMMIT not in bench.armed()
    # Repeating the request after the restart executes once.
    assert bench.submit(action).operation.status is OperationStatus.SETTLED
    assert bench.hooks == [f"exit:{CRASH_EXIT_STATUS}"]


@pytest.mark.case("T11")
def test_crash_after_commit_keeps_the_committed_operation(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.CRASH_AFTER_COMMIT)

    with pytest.raises(Exited):
        bench.submit(action)

    [stored] = transfers(bench)
    assert stored["status"] == "SETTLED"
    again = bench.submit(action)
    assert not again.created and again.operation.operation_id == stored["operation_id"]
    assert bench.balance() == START_FEN - 50_000


@pytest.mark.case("T11")
def test_pause_before_commit_holds_the_unit_open(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.PAUSE_BEFORE_COMMIT)

    with pytest.raises(Paused) as paused:
        bench.submit(action)

    assert paused.value.marker == bench.gateway.fault_wait_file
    assert transfers(bench) == []
    assert bench.armed() == set()


@pytest.mark.case("T11")
def test_pause_after_commit_follows_the_commit(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.PAUSE_AFTER_COMMIT)

    with pytest.raises(Paused):
        bench.submit(action)

    assert [row["status"] for row in transfers(bench)] == ["SETTLED"]
    assert bench.armed() == set()


def test_drop_response_commits_and_withholds_the_result(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.DROP_RESPONSE)

    with pytest.raises(ResponseDropped) as dropped:
        bench.submit(action)

    [stored] = transfers(bench)
    assert dropped.value.operation.operation_id == stored["operation_id"]
    assert bench.submit(action).operation.operation_id == stored["operation_id"]
    assert len(bench.rows("SELECT * FROM receipts")) == 1


@pytest.mark.case("T13")
def test_unexpected_rolls_back_records_the_failure_and_fires_once_per_count(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.UNEXPECTED, count=2)

    for _ in range(2):
        with pytest.raises(WgError) as failed:
            bench.submit(action)
        assert failed.value.code is ErrorCode.UNEXPECTED_FAILURE

    assert transfers(bench) == []
    assert bench.rows("SELECT * FROM reservations") == []
    failures = bench.rows("SELECT error_class, logical_action_id FROM failure_records")
    assert (
        failures
        == [{"error_class": "unexpected", "logical_action_id": action.logical_action_id}] * 2
    )
    assert bench.armed() == set()
    assert bench.submit(action).operation.status is OperationStatus.SETTLED


def test_busy_retries_the_whole_unit(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.BUSY)

    assert bench.submit(action).operation.status is OperationStatus.SETTLED
    assert len(transfers(bench)) == 1
    assert bench.rows("SELECT * FROM failure_records") == []


def test_busy_on_every_attempt_ends_in_busy_retry(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.BUSY, count=4)

    with pytest.raises(WgError) as failed:
        bench.submit(action)

    assert failed.value.code is ErrorCode.BUSY_RETRY
    assert transfers(bench) == []
    assert bench.rows("SELECT * FROM failure_records") == []
    assert bench.submit(action).operation.status is OperationStatus.SETTLED


def test_faults_are_not_taken_by_a_repeat(bench: Bench):
    action = bench.ready(50_000)
    bench.submit(action)
    bench.arm(Fault.CRASH_BEFORE_COMMIT)
    bench.arm(Fault.DROP_RESPONSE)

    assert not bench.submit(action).created
    assert bench.armed() == {Fault.CRASH_BEFORE_COMMIT, Fault.DROP_RESPONSE}
    assert bench.hooks == []


def test_a_refused_submission_consumes_no_after_commit_fault(bench: Bench):
    draft = bench.draft(50_000)
    bench.arm(Fault.DROP_RESPONSE)
    with pytest.raises(WgError):
        bench.gateway.submit("cus_main", draft.logical_action_id)
    assert bench.armed() == {Fault.DROP_RESPONSE}
