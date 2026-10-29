"""Fund order prechecks inside a plan: settled funding, cumulative caps; immutable calc results."""

import sqlite3

import pytest

from wg.contracts.enums import ActionKind
from wg.contracts.errors import ErrorCode
from wg.k6 import calc_results
from wg.k6.calc_results import CalcKind
from wg.k6.simtime import now
from wg.packs.registry import pack_for

from .conftest import Browser
from .test_s3_routes import advance_to_day, approved, code_for, node, rows

pytestmark = pytest.mark.layer("D")

MAIN = "cus_main"


def precheck(browser: Browser, kind: ActionKind, params: dict, plan_id: str) -> list[ErrorCode]:
    pack = pack_for(kind)
    with browser.db.read() as conn:
        reasons = pack.precheck(
            conn, MAIN, kind, pack.canonical(kind, params), now(conn), plan_id=plan_id
        )
    return [reason.code for reason in reasons]


def purchase(amount_fen: int) -> dict:
    return {"account_id": "acc_main", "product_id": "990004", "amount_fen": amount_fen}


def test_purchase_is_refused_until_the_redemption_settles(browser: Browser):
    view = approved(browser)
    assert ErrorCode.UNSETTLED_FUNDS in precheck(
        browser, ActionKind.PURCHASE, purchase(498_500), view.plan_id
    )
    code_for(browser, node(view, ActionKind.REDEEM).logical_action_id)
    advance_to_day(browser, 2)
    # Submitted and two days old: the proceeds are still expected money, not posted money.
    assert ErrorCode.UNSETTLED_FUNDS in precheck(
        browser, ActionKind.PURCHASE, purchase(498_500), view.plan_id
    )
    advance_to_day(browser, 3)
    assert precheck(browser, ActionKind.PURCHASE, purchase(498_500), view.plan_id) == []


def test_turnover_and_fee_caps_count_the_whole_plan(browser: Browser):
    view = approved(browser)
    code_for(browser, node(view, ActionKind.REDEEM).logical_action_id)
    advance_to_day(browser, 3)
    # ¥5,000 redeemed: a purchase above ¥5,000 takes the plan over the ¥10,000 turnover cap.
    assert precheck(browser, ActionKind.PURCHASE, purchase(500_000), view.plan_id) == []
    reasons = precheck(browser, ActionKind.PURCHASE, purchase(500_001), view.plan_id)
    assert reasons == [ErrorCode.TURNOVER_CAP]

    # ¥15 already paid in fees; another ¥6 redemption fee is over the ¥20 cap.
    more = {"account_id": "acc_main", "product_id": "990003", "shares": "1000.0000"}
    assert ErrorCode.FEE_CAP not in precheck(browser, ActionKind.REDEEM, more, view.plan_id)
    more["shares"] = "2000.0000"
    assert ErrorCode.FEE_CAP in precheck(browser, ActionKind.REDEEM, more, view.plan_id)


def test_redemption_cannot_take_shares_already_in_a_pending_redemption(browser: Browser):
    view = approved(browser)
    code_for(browser, node(view, ActionKind.REDEEM).logical_action_id)
    more = {"account_id": "acc_main", "product_id": "990003", "shares": "5000.0001"}
    assert ErrorCode.INSUFFICIENT_FUNDS in precheck(browser, ActionKind.REDEEM, more, view.plan_id)


def test_calc_results_are_immutable_and_stored_once_per_input(browser: Browser):
    with browser.db.unit_of_work("test calc") as uow:
        first = calc_results.store(
            uow,
            customer_id=MAIN,
            kind=CalcKind.EXPOSURE,
            inputs={"a": 1},
            basis={"cash_snapshot_id": None},
            output={"x": "1.0000"},
        )
        again = calc_results.store(
            uow,
            customer_id=MAIN,
            kind=CalcKind.EXPOSURE,
            inputs={"a": 1},
            basis={},
            output={"x": "2.0000"},
        )
    assert again == first
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with browser.db.unit_of_work("test calc update") as uow:
            uow.execute(
                "UPDATE calc_results SET output_json = '{}' WHERE calc_result_id = ?",
                (first.calc_result_id,),
            )
    stored = rows(browser, "SELECT output_json FROM calc_results")
    assert stored == [{"output_json": '{"x":"1.0000"}'}]
