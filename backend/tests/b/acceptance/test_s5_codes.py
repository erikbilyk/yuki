"""S5 direct debits: codes on the RED changes (raising a cap, resuming); T106 at layers D and H.

Both changes show a RED card and run only after the SMS code of their own challenge. An expired
code, the code of another challenge and an extra `user_confirmed` field are refused, audited and
change nothing. A cap raise re-checks the requests waiting above the old cap (R-S5-1) only when
it executes. The customer session cannot call the merchant simulator. A case passes on stored
agreements, requests, operations and audit rows. Every S5 contract name is a string here, so the
module imports on a build without S5.

The `video_suspended` variant gives both changes at once: resume mdt_video and raise the cap of
mdt_cloud (expected and cap 2,100) to 2,500 with a 2,500 request waiting above the old cap.
"""

from __future__ import annotations

from typing import Any

import pytest

from wg.contracts.enums import Role
from wg.contracts.errors import ErrorCode

from ..harness.client import WgClient, describe
from .common import assert_audited, audit_snapshot
from .direct_debit import (
    AWAITING,
    CLOUD,
    OUT_OF_MANDATE,
    VIDEO,
    action_count,
    audit_events,
    body,
    confirm_card_response,
    debit_request,
    draft_change,
    has_direct_debits,
    mandate,
    merchant_request,
    money_state,
    open_card,
    operation_count,
    operations_of,
    outcome,
    payment_operations,
    receipt,
    refused,
    reset_s5,
    sms_code,
    stored_requests,
    verify_response,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

KINDS = {"raise": "MANDATE_CAP_RAISE", "resume": "MANDATE_RESUME"}
BEFORE = {"raise": (2_100, OUT_OF_MANDATE), "resume": ("SUSPENDED",)}
AFTER = {"raise": (2_500, AWAITING), "resume": ("ACTIVE",)}
WHICH = pytest.mark.parametrize("which", ["raise", "resume"])
CODE_REFUSALS = (ErrorCode.CODE_INVALID.value, ErrorCode.CODE_SCOPE_MISMATCH.value)


class Change:
    """One RED change: its draft, its card and, once confirmed, its challenge."""

    def __init__(self, wg: WgClient, which: str) -> None:
        self.wg, self.which = wg, which
        self.waiting_id: str | None = None
        if which == "raise":
            waiting = merchant_request(wg, CLOUD, 2_500)
            assert outcome(waiting) == (OUT_OF_MANDATE, "CAP"), waiting
            self.waiting_id = waiting["request_id"]
            self.draft = draft_change(wg, CLOUD, "set_cap", 2_500)
        else:
            self.draft = draft_change(wg, VIDEO, "resume")
        assert self.draft["kind"] == KINDS[which] and self.draft["tier"] == "RED", self.draft
        self.card = open_card(wg, self.draft)
        assert self.card["tier"] == "RED" and self.card["requires_code"] is True, self.card
        self.challenge_id: str | None = None

    def observe(self) -> tuple[Any, ...]:
        if self.which == "raise":
            assert self.waiting_id is not None
            request = debit_request(self.wg, self.waiting_id)
            return (mandate(self.wg, CLOUD)["cap_fen"], request["status"])
        return (mandate(self.wg, VIDEO)["status"],)

    def confirm(self) -> str:
        """Confirm the card: a RED change gets a challenge and no operation."""
        result = body(confirm_card_response(self.wg, self.card), 200, 201, 202)
        assert result.get("operation") is None and result["challenge"] is not None, result
        challenge_id: str = result["challenge"]["challenge_id"]
        self.challenge_id = challenge_id
        return challenge_id

    def code(self) -> str:
        assert self.challenge_id is not None
        return sms_code(self.wg, self.challenge_id)

    def verify(self, code: str | None = None) -> dict[str, Any]:
        assert self.challenge_id is not None
        return body(verify_response(self.wg, self.challenge_id, code), 200, 201, 202)

    def assert_executed(self, result: dict[str, Any]) -> None:
        operation = result["operation"]
        assert operation is not None and operation["status"] == "SETTLED", result
        shown = receipt(self.wg, operation["operation_id"])
        assert (shown["kind"], shown["status"]) == (KINDS[self.which], "SETTLED"), shown
        assert [o["operation_id"] for o in operations_of(self.wg, KINDS[self.which])] == [
            operation["operation_id"]
        ]
        assert self.observe() == AFTER[self.which]


def fresh(wg: WgClient) -> None:
    reset_s5(wg, "video_suspended")


class Untouched:
    """The state a refused step must leave as it was."""

    def __init__(self, wg: WgClient, *changes: Change) -> None:
        self.wg, self.changes = wg, changes
        self.money = money_state(wg)
        self.counts = (action_count(wg), operation_count(wg))
        self.requests = stored_requests(wg)

    def assert_held(self) -> None:
        wg = self.wg
        assert money_state(wg) == self.money
        assert (action_count(wg), operation_count(wg)) == self.counts
        assert stored_requests(wg) == self.requests
        for change in self.changes:
            assert change.observe() == BEFORE[change.which]
        assert payment_operations(wg) == []


# T106


@pytest.mark.case("T106")
@WHICH
def test_t106_the_change_runs_only_after_the_correct_code(wg: WgClient, which: str) -> None:
    fresh(wg)
    change = Change(wg, which)
    untouched = Untouched(wg, change)

    change.confirm()
    untouched.assert_held()
    assert change.observe() == BEFORE[which]

    result = change.verify()
    change.assert_executed(result)
    assert payment_operations(wg) == []
    if which == "raise":
        # Nothing is paid: the waiting request is only answerable now (R-S5-1).
        assert debit_request(wg, change.waiting_id or "")["payable"] is True
        assert len(audit_events(wg, "debit_request_updated")) == 1


@pytest.mark.case("T106")
@WHICH
def test_t106_an_expired_code_is_refused_and_a_new_code_works(wg: WgClient, which: str) -> None:
    fresh(wg)
    change = Change(wg, which)
    challenge_id = change.confirm()
    code = change.code()
    untouched = Untouched(wg, change)

    wg.advance(minutes=5)
    before = audit_snapshot(wg)
    refused(verify_response(wg, challenge_id, code), 409, ErrorCode.CODE_EXPIRED.value)

    untouched.assert_held()
    assert_audited(wg, before, ErrorCode.CODE_EXPIRED)

    fresh_challenge = wg.reissue(challenge_id)
    assert fresh_challenge.challenge_id != challenge_id
    change.challenge_id = fresh_challenge.challenge_id
    change.assert_executed(change.verify())


@pytest.mark.case("T106")
@pytest.mark.parametrize("attacked", ["raise", "resume"])
def test_t106_the_code_of_another_challenge_is_refused(wg: WgClient, attacked: str) -> None:
    fresh(wg)
    changes = {which: Change(wg, which) for which in ("raise", "resume")}
    for change in changes.values():
        change.confirm()
    target, other = changes[attacked], changes["resume" if attacked == "raise" else "raise"]
    for _ in range(5):
        if other.code() != target.code():
            break
        assert target.challenge_id is not None
        target.challenge_id = wg.reissue(target.challenge_id).challenge_id
    assert other.code() != target.code()
    untouched = Untouched(wg, *changes.values())
    before = audit_snapshot(wg)

    response = verify_response(wg, target.challenge_id or "", other.code())

    assert response.status_code in (409, 422), response.text
    assert response.json()["code"] in CODE_REFUSALS, response.text
    untouched.assert_held()
    assert_audited(wg, before)

    # Each change still runs with its own code, and only that one.
    target.assert_executed(target.verify())
    assert other.observe() == BEFORE[other.which]
    other.assert_executed(other.verify())


@pytest.mark.case("T106")
@WHICH
def test_t106_a_user_confirmed_field_gives_no_authority(wg: WgClient, which: str) -> None:
    fresh(wg)
    change = Change(wg, which)
    untouched = Untouched(wg, change)
    before = audit_snapshot(wg)

    on_confirm = confirm_card_response(wg, change.card, user_confirmed=True)

    assert on_confirm.status_code == 422, on_confirm.text
    assert body(wg.get(f"/v1/cards/{change.card['card_id']}"))["status"] == "open"
    untouched.assert_held()
    assert_audited(wg, before)

    challenge_id = change.confirm()
    code = change.code()
    before = audit_snapshot(wg)
    on_verify = wg.post(
        f"/v1/challenges/{challenge_id}/verify", {"code": code, "user_confirmed": True}
    )

    assert on_verify.status_code == 422, on_verify.text
    untouched.assert_held()
    assert_audited(wg, before)

    change.assert_executed(change.verify(code))


@pytest.mark.case("T106")
def test_t106_a_customer_session_cannot_call_the_merchant_simulator(wg: WgClient) -> None:
    reset_s5(wg)
    state, actions = money_state(wg), action_count(wg)
    customer = wg.fork(with_sessions=False)
    customer.login(Role.CUSTOMER)
    request = {
        "merchant_id": "mer_video",
        "merchant_request_id": "mreq-t106-customer",
        "mandate_ref": VIDEO,
        "amount_fen": 2_500,
        "currency": "CNY",
        "description_raw": "",
    }
    try:
        # SPEC 6 answers 401 SESSION_EXPIRED for a route of another role, S5-ACCEPTANCE T106
        # says 403 FORBIDDEN_ROUTE; both refuse the call, and the open question is not the
        # subject of this case.
        for role in (Role.CUSTOMER, None):
            response = (
                customer.post("/sim/debit-requests", request, role=role)
                if role
                else customer.post("/sim/debit-requests", request)
            )
            assert response.status_code in (401, 403), describe(response)
            assert response.json().get("code") in ("SESSION_EXPIRED", "FORBIDDEN_ROUTE")
    finally:
        customer.close()

    assert stored_requests(wg) == []
    assert audit_events(wg, "debit_request_received") == []
    assert money_state(wg) == state and action_count(wg) == actions
