"""Params and request hash of MANDATE_CREATE (S5-SPEC §6.1)."""

import pytest

from wg.contracts.enums import ActionKind
from wg.packs.mandates import CREATE_PARAMS, MandateCreatePack

KIND = ActionKind.MANDATE_CREATE
PARAMS = {
    "account_id": "acc_main",
    "merchant_id": "mer_music",
    "expected_fen": 1_500,
    "cap_fen": 2_000,
    "debit_day": 15,
    "months": 12,
}
pack = MandateCreatePack()


def test_the_canonical_params_are_exactly_the_six_of_the_spec():
    assert tuple(pack.canonical(KIND, PARAMS)) == CREATE_PARAMS
    assert CREATE_PARAMS == (
        "account_id",
        "merchant_id",
        "expected_fen",
        "cap_fen",
        "debit_day",
        "months",
    )


def test_a_cap_equal_to_the_expected_amount_is_allowed():
    assert pack.canonical(KIND, PARAMS | {"cap_fen": 1_500})["cap_fen"] == 1_500


@pytest.mark.parametrize(
    "change",
    [
        {"cap_fen": 1_499},
        {"expected_fen": 0},
        {"months": 0},
        {"debit_day": -1},
        {"expected_fen": 15.0},
        {"months": True},
        {"months": None},
        {"surplus": 1},
    ],
)
def test_malformed_params_are_refused(change):
    with pytest.raises(ValueError):
        pack.canonical(KIND, PARAMS | change)


def test_a_term_over_twelve_months_survives_canonical_to_get_its_own_refusal():
    assert pack.canonical(KIND, PARAMS | {"months": 24})["months"] == 24


@pytest.mark.parametrize("name", CREATE_PARAMS)
def test_the_hash_covers_every_param(name):
    changed = PARAMS | {name: "x" if isinstance(PARAMS[name], str) else PARAMS[name] + 1}
    assert pack.request_hash(KIND, "cus_main", changed) != pack.request_hash(
        KIND, "cus_main", PARAMS
    )


def test_the_hash_covers_the_customer_and_nothing_else():
    first = pack.request_hash(KIND, "cus_main", PARAMS)
    assert first == pack.request_hash(KIND, "cus_main", dict(PARAMS))
    assert first != pack.request_hash(KIND, "cus_other", PARAMS)
