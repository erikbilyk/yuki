"""Ids in request bodies and queries: a foreign and a missing object give the same 404 (SPEC §6).

T54 covers ids in the path; these are the ids a draft, a query or a plan request names in its
body. Each request is sent once with an object of `cus_other` and once with an id that does
not exist, and both answers must be byte-identical 404 bodies with nothing created.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from wg.contracts.enums import Role

from ..harness.client import WgClient, describe

OTHER_CUSTOMER = "cus_other"


def test_foreign_and_missing_body_ids_answer_alike(app_client: WgClient) -> None:
    wg = app_client
    bill = wg.ebill("pay_brother", "rent", 100_000, date(2026, 11, 20), customer_id=OTHER_CUSTOMER)
    other = wg.fork(with_sessions=False)
    other.login(Role.CUSTOMER, OTHER_CUSTOMER)
    try:
        foreign_draft = other.draft_transfer("pay_brother", 10_000).logical_action_id
    finally:
        other.close()
    mine = wg.draft_transfer("pay_mom", 10_000).logical_action_id
    foreign = {
        "account": "acc_other",
        "payee": "pay_brother",
        "commitment": bill.commitment_id,
        "draft": foreign_draft,
    }
    ghost = {
        "account": "acc_nothingthere0001",
        "payee": "pay_nothingthere0001",
        "commitment": "cmt_nothingthere0001",
        "draft": "act_nothingthere0001",
    }
    due = (date(2026, 11, 2) + timedelta(days=10)).isoformat()

    def calls(ids: dict[str, str]) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [
            ("GET", f"/v1/accounts/{ids['account']}/balance", None),
            ("GET", f"/v1/transfers/history?payee_id={ids['payee']}", None),
            ("POST", "/v1/drafts/transfer", {"payee_id": ids["payee"], "amount_fen": 10_000}),
            (
                "POST",
                "/v1/drafts/transfer",
                {"payee_id": "pay_mom", "amount_fen": 10_000, "account_id": ids["account"]},
            ),
            ("POST", "/v1/drafts/bill", {"commitment_id": ids["commitment"]}),
            (
                "POST",
                "/v1/drafts/bill",
                {"purpose": "房租", "payee_id": ids["payee"], "amount_fen": 1000, "due_date": due},
            ),
            ("POST", "/v1/drafts/pay-bill", {"commitment_id": ids["commitment"]}),
            ("POST", "/v1/drafts/mark-paid-external", {"commitment_id": ids["commitment"]}),
            ("POST", "/v1/drafts/cancel-bill", {"commitment_id": ids["commitment"]}),
            (
                "POST",
                "/v1/plans/compose",
                {"draft_ids": [mine, ids["draft"]], "depends_on": {}},
            ),
        ]

    actions_before = wg.db.count("actions")
    plans_before = wg.db.count("plans")
    for (method, path, body), (_, ghost_path, ghost_body) in zip(
        calls(foreign), calls(ghost), strict=True
    ):
        real = wg.request(method, path, json=body)
        missing = wg.request(method, ghost_path, json=ghost_body)
        assert real.status_code == 404, describe(real)
        assert missing.status_code == 404, describe(missing)
        assert real.content == missing.content, f"{method} {path} tells a foreign object apart"
    assert wg.db.count("actions") == actions_before
    assert wg.db.count("plans") == plans_before
