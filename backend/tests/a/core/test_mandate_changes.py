"""What a change of an agreement does to it and to its requests (S5-SPEC §5.3, §6.1, §6.2).

Pure rules and the change pack's re-validation. The cap-raise numbers are those of
R-S5-1 (S5-ACCEPTANCE §3.3): cloud, cap 2,100, requests 2,500, 2,400 and 3,000 one minute
apart, cap raised to 2,500.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

import pytest

from wg.contracts.enums import ActionKind, DebitReason, DebitRequestStatus, MandateStatus
from wg.contracts.errors import ErrorCode
from wg.core.mandate_rules import (
    CHANGE_FROM,
    CHANGE_TO,
    RequestChange,
    cap_change_kind,
    cap_raise_changes,
    expiry_changes,
    probe_amount,
    request_changes,
)
from wg.k6.mandates import MandateRow
from wg.k6.simtime import SHANGHAI
from wg.packs.mandates import change_refusal

K = ActionKind
M = MandateStatus
S = DebitRequestStatus
R = DebitReason
EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)

CLOUD = MandateRow(
    mandate_ref="mdt_cloud",
    customer_id="cus_main",
    account_id="acc_main",
    merchant_id="mer_cloud",
    purpose_id="pur_sub_cloud",
    expected_fen=2_100,
    cap_fen=2_100,
    debit_day=12,
    starts_on=date(2026, 10, 2),
    expires_on=date(2027, 10, 1),
    status=M.ACTIVE,
    cancelled_at=None,
    version=1,
)


@dataclass(frozen=True)
class Request:
    request_id: str
    period_key: str
    status: DebitRequestStatus
    reason_code: DebitReason | None
    amount_fen: int
    received_at: datetime
    respond_by: datetime


def request(
    request_id: str,
    amount: int,
    status: DebitRequestStatus = S.OUT_OF_MANDATE,
    reason: DebitReason | None = R.CAP,
    received: datetime = EPOCH,
) -> Request:
    key = f"{received.year:04d}-{received.month:02d}"
    return Request(
        request_id, key, status, reason, amount, received, received + timedelta(hours=72)
    )


def minutes(n: int) -> datetime:
    return EPOCH + timedelta(minutes=n)


R_S5_1 = (
    request("req_a", 2_500, received=minutes(0)),
    request("req_b", 2_400, received=minutes(1)),
    request("req_c", 3_000, received=minutes(2)),
)


# Transitions (S5-SPEC §5.3)

ALL_KINDS = sorted(CHANGE_FROM, key=lambda kind: kind.value)


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("status", list(M))
def test_each_change_starts_only_from_its_statuses(kind: ActionKind, status: MandateStatus):
    allowed = {
        K.MANDATE_CANCEL: {M.ACTIVE, M.SUSPENDED},
        K.MANDATE_SUSPEND: {M.ACTIVE},
        K.MANDATE_RESUME: {M.SUSPENDED},
        K.MANDATE_CAP_LOWER: {M.ACTIVE, M.SUSPENDED},
        K.MANDATE_CAP_RAISE: {M.ACTIVE, M.SUSPENDED},
    }[kind]
    params = {"mandate_ref": "mdt_cloud", "mandate_version": 1}
    if kind is K.MANDATE_CAP_LOWER:
        params["cap_fen"] = 2_000
    if kind is K.MANDATE_CAP_RAISE:
        params["cap_fen"] = 2_500
    refusal = change_refusal(replace(CLOUD, status=status), kind, params, EPOCH)
    if status in allowed:
        assert refusal is None
    else:
        assert refusal is not None and refusal.code is ErrorCode.MANDATE_CHANGED
        assert refusal.detail == {"reason": "status", "mandate_ref": "mdt_cloud", "status": status}


def test_status_changes_lead_to_their_status():
    assert CHANGE_TO == {
        K.MANDATE_CANCEL: M.CANCELLED,
        K.MANDATE_SUSPEND: M.SUSPENDED,
        K.MANDATE_RESUME: M.ACTIVE,
    }


def test_resubmission_checks_version_then_status_then_expiry_then_direction():
    params = {"mandate_ref": "mdt_cloud", "mandate_version": 1, "cap_fen": 2_000}
    lower = K.MANDATE_CAP_LOWER

    changed = change_refusal(replace(CLOUD, version=2, status=M.CANCELLED), lower, params, EPOCH)
    assert changed is not None and changed.detail["reason"] == "version"
    expired_at = datetime(2027, 10, 2, 0, 0, tzinfo=SHANGHAI)
    assert change_refusal(CLOUD, lower, params, expired_at - timedelta(seconds=1)) is None
    expired = change_refusal(CLOUD, lower, params, expired_at)
    assert expired is not None and expired.detail == {
        "reason": "expired",
        "mandate_ref": "mdt_cloud",
    }
    # A cap now below the requested one would make this a raise: refused, never applied.
    wrong_way = change_refusal(replace(CLOUD, cap_fen=1_900), lower, params, EPOCH)
    assert wrong_way is not None and wrong_way.code is ErrorCode.MANDATE_CHANGED
    over_limit = change_refusal(CLOUD, K.MANDATE_CAP_RAISE, params | {"cap_fen": 5_000_001}, EPOCH)
    assert over_limit is not None and over_limit.code is ErrorCode.SINGLE_LIMIT_EXCEEDED


def test_the_direction_of_a_new_cap_decides_the_kind():
    assert cap_change_kind(2_100, 2_000) is K.MANDATE_CAP_LOWER
    assert cap_change_kind(2_100, 2_500) is K.MANDATE_CAP_RAISE
    assert cap_change_kind(2_100, 2_100) is None


# Pending requests (S5-SPEC §5.3)

MIXED = (
    request("req_wait", 2_100, S.AWAITING_CLIENT, None),
    request("req_cap", 2_500),
    request("req_used", 2_000, S.OUT_OF_MANDATE, R.PERIOD_USED),
    request("req_paid", 2_100, S.PAID, None, received=datetime(2026, 10, 12, 9, tzinfo=SHANGHAI)),
)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [(K.MANDATE_CANCEL, R.MANDATE_CANCELLED), (K.MANDATE_SUSPEND, R.MANDATE_SUSPENDED)],
)
def test_cancel_and_suspend_decline_only_the_requests_awaiting_the_customer(kind, reason):
    assert request_changes(kind, CLOUD, MIXED, EPOCH) == (
        RequestChange("req_wait", S.AUTO_DECLINED, reason),
    )


def test_resume_changes_no_request():
    assert request_changes(K.MANDATE_RESUME, replace(CLOUD, status=M.SUSPENDED), MIXED, EPOCH) == ()


def test_a_lower_cap_moves_waiting_requests_above_it_out_of_the_agreement():
    assert request_changes(K.MANDATE_CAP_LOWER, CLOUD, MIXED, EPOCH, cap_fen=2_100) == ()
    assert request_changes(K.MANDATE_CAP_LOWER, CLOUD, MIXED, EPOCH, cap_fen=2_099) == (
        RequestChange("req_wait", S.OUT_OF_MANDATE, R.CAP),
    )


def test_expiry_declines_the_requests_awaiting_the_customer():
    assert expiry_changes(MIXED) == (RequestChange("req_wait", S.AUTO_DECLINED, R.MANDATE_EXPIRED),)


def test_r_s5_1_the_earliest_eligible_request_awaits_the_customer_again():
    changes = request_changes(K.MANDATE_CAP_RAISE, CLOUD, R_S5_1, minutes(3), cap_fen=2_500)
    # 2,500 waits again, 2,400 can no longer take the period, 3,000 stays above the cap.
    assert changes == (
        RequestChange("req_a", S.AWAITING_CLIENT, None),
        RequestChange("req_b", S.OUT_OF_MANDATE, R.PERIOD_USED),
    )


def test_r_s5_1_a_period_with_a_paid_request_promotes_nothing():
    paid = request("req_paid", 2_100, S.PAID, None, received=minutes(0))
    changes = cap_raise_changes(CLOUD, (*R_S5_1, paid), 2_500, minutes(3))
    assert changes == (
        RequestChange("req_a", S.OUT_OF_MANDATE, R.PERIOD_USED),
        RequestChange("req_b", S.OUT_OF_MANDATE, R.PERIOD_USED),
    )


def test_r_s5_1_a_request_past_its_reply_time_is_untouched():
    late = request("req_late", 2_500, received=EPOCH - timedelta(hours=72))
    fresh = request("req_fresh", 2_400, received=minutes(1))
    changes = cap_raise_changes(CLOUD, (late, fresh), 2_500, minutes(2))
    assert changes == (RequestChange("req_fresh", S.AWAITING_CLIENT, None),)


def test_a_cap_raise_promotes_one_request_per_period():
    december = request("req_dec", 2_500, received=datetime(2026, 12, 1, 0, 1, tzinfo=SHANGHAI))
    november = request("req_nov", 2_500, received=datetime(2026, 11, 30, 23, 59, tzinfo=SHANGHAI))
    now = datetime(2026, 12, 1, 0, 2, tzinfo=SHANGHAI)
    assert cap_raise_changes(CLOUD, (november, december), 2_500, now) == (
        RequestChange("req_nov", S.AWAITING_CLIENT, None),
        RequestChange("req_dec", S.AWAITING_CLIENT, None),
    )


def test_a_cap_raise_promotes_nothing_on_a_suspended_or_expired_agreement():
    assert cap_raise_changes(replace(CLOUD, status=M.SUSPENDED), R_S5_1, 2_500, minutes(3)) == ()
    ended = replace(CLOUD, expires_on=date(2026, 11, 1))
    assert cap_raise_changes(ended, R_S5_1, 2_500, minutes(3)) == ()


def test_probe_amounts():
    video = replace(CLOUD, expected_fen=2_500, cap_fen=2_000)
    assert probe_amount(K.MANDATE_CANCEL, video, ()) == 2_500
    assert probe_amount(K.MANDATE_SUSPEND, video, ()) == 2_500
    assert probe_amount(K.MANDATE_CAP_LOWER, video, ()) == 2_001
