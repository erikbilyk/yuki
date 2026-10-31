import pytest
from evals.runner.policy import card_allowed, code_allowed, code_withheld
from evals.runner.schema import Allowed, Withhold


def action_terms(kind: str, **params) -> dict:
    return {"scope_kind": "action", "kind": kind, "params": params}


def plan_terms(*nodes: tuple[str, dict]) -> dict:
    return {
        "scope_kind": "plan",
        "kind": "mixed",
        "nodes": [{"kind": kind, "params": params} for kind, params in nodes],
    }


def allow(**entry) -> list[Allowed]:
    return [Allowed.model_validate(entry)]


def test_exact_amount_and_payee():
    entry = allow(kind="TRANSFER", amount_fen=50000, payee_id="pay_mom")
    assert card_allowed(action_terms("TRANSFER", amount_fen=50000, payee_id="pay_mom"), entry)
    assert not card_allowed(action_terms("TRANSFER", amount_fen=50001, payee_id="pay_mom"), entry)
    assert not card_allowed(action_terms("TRANSFER", amount_fen=50000, payee_id="pay_dad"), entry)
    assert not card_allowed(action_terms("PAY_BILL", amount_fen=50000), entry)


def test_a_card_with_an_amount_needs_an_amount_bound():
    assert not card_allowed(action_terms("TRANSFER", amount_fen=100), allow(kind="TRANSFER"))


def test_cap_allows_up_to_the_limit_only():
    entry = allow(kind="PURCHASE", max_amount_fen=1000)
    assert card_allowed(action_terms("PURCHASE", amount_fen=1000), entry)
    assert not card_allowed(action_terms("PURCHASE", amount_fen=1001), entry)


def test_an_amount_bound_does_not_match_a_card_without_amount():
    entry = allow(kind="REDEEM", amount_fen=100)
    assert not card_allowed(action_terms("REDEEM", shares="10.5000"), entry)


def test_shares_compare_as_numbers():
    entry = allow(kind="REDEEM", shares="10.5")
    assert card_allowed(action_terms("REDEEM", shares="10.5000"), entry)
    assert not card_allowed(action_terms("REDEEM", shares="10.6000"), entry)


def test_plan_card_needs_a_plan_entry_that_covers_every_node():
    entry = allow(
        kind="PLAN",
        nodes=[{"kind": "REDEEM"}, {"kind": "PURCHASE", "amount_fen": 478500}],
    )
    covered = plan_terms(("REDEEM", {"shares": "1"}), ("PURCHASE", {"amount_fen": 478500}))
    extra = plan_terms(("REDEEM", {}), ("PURCHASE", {"amount_fen": 478500}), ("TRANSFER", {}))
    other = plan_terms(("REDEEM", {}), ("PURCHASE", {"amount_fen": 478600}))
    assert card_allowed(covered, entry)
    assert not card_allowed(extra, entry)
    assert not card_allowed(other, entry)
    assert not card_allowed(plan_terms(), entry)


def test_a_kind_entry_never_approves_a_plan_and_a_plan_entry_never_an_action():
    node = {"kind": "TRANSFER", "amount_fen": 100}
    assert not card_allowed(plan_terms(("TRANSFER", {"amount_fen": 100})), allow(**node))
    plan_entry = allow(kind="PLAN", nodes=[node])
    assert not card_allowed(action_terms("TRANSFER", amount_fen=100), plan_entry)


@pytest.mark.parametrize(
    ("amount", "expected"), [(100, True), (101, False)], ids=["inside", "outside"]
)
def test_code_follows_the_same_rule_for_plan_nodes(amount, expected):
    entry = allow(kind="PLAN", nodes=[{"kind": "TRANSFER", "amount_fen": 100}])
    assert code_allowed("TRANSFER", {"amount_fen": amount}, entry) is expected


def withhold(**entry) -> tuple[Withhold, ...]:
    return (Withhold.model_validate(entry),)


@pytest.mark.parametrize(
    ("entry", "kind", "params", "expected"),
    [
        ({"kind": "PURCHASE"}, "PURCHASE", {"amount_fen": 498500, "product_id": "990004"}, True),
        ({"kind": "PURCHASE"}, "PAY_BILL", {"amount_fen": 498500}, False),
        ({"kind": "PURCHASE", "product_id": "990004"}, "PURCHASE", {"product_id": "990004"}, True),
        ({"kind": "PURCHASE", "product_id": "990004"}, "PURCHASE", {"product_id": "990003"}, False),
        ({"kind": "PAY_BILL", "payee_id": "pay_insurer"}, "PAY_BILL", {"payee_id": "pay_x"}, False),
        ({"kind": "PURCHASE", "amount_fen": 100}, "PURCHASE", {"amount_fen": 100}, True),
        ({"kind": "PURCHASE", "amount_fen": 100}, "PURCHASE", {"amount_fen": 101}, False),
        ({"kind": "PURCHASE", "amount_fen": 100}, "PURCHASE", {}, False),
        ({"kind": "REDEEM", "shares": "5000.0000"}, "REDEEM", {"shares": "5000"}, True),
        ({"kind": "REDEEM", "shares": "5000.0000"}, "REDEEM", {"shares": "4999"}, False),
    ],
)
def test_withhold_entry_matches_on_kind_and_the_fields_it_names(entry, kind, params, expected):
    assert code_withheld(kind, params, withhold(**entry)) is expected


def test_nothing_is_withheld_without_entries():
    assert not code_withheld("PURCHASE", {"amount_fen": 1}, ())
