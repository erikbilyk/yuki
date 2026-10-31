"""The base profile's cash snapshot at the epoch is pinned (S5-SPEC §11.4).

The values and inputs hashes were computed on the release build before direct-debit
agreements existed; adding them must not change a single input of fixture_v1.
"""

from datetime import datetime

import pytest

from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.clock import advance_clock
from wg.k6.simtime import SHANGHAI
from wg.numeric import compute_snapshot

EPOCH_SNAPSHOTS = {
    "cus_main": {
        "inputs_hash": "cdc10fcb29b68fe5cd546de8846e203d847eb8bc95b51d7a409ec7d689b729e6",
        "c_fen": 1_800_000,
        "e_fen": 400_000,
        "l_fen": 300_000,
        "b_fen": 840_000,
        "m_fen": 60_000,
        "u_fen": 0,
        "protected_fen": 1_600_000,
        "headroom_fen": 200_000,
        "shortfall_fen": 0,
        "monthly_estimate_fen": 0,
    },
    "cus_other": {
        "inputs_hash": "ca281393a51996a4584c850e652cf54f6a2c03caa535111db8417a332659e513",
        "c_fen": 500_000,
        "e_fen": 0,
        "l_fen": 0,
        "b_fen": 0,
        "m_fen": 0,
        "u_fen": 0,
        "protected_fen": 0,
        "headroom_fen": 500_000,
        "shortfall_fen": 0,
        "monthly_estimate_fen": 0,
    },
}


@pytest.mark.parametrize("customer_id", sorted(EPOCH_SNAPSHOTS))
def test_base_profile_snapshot_at_the_epoch_is_unchanged(db, customer_id: str):
    with db.read() as conn:
        values = compute_snapshot(cash_inputs_from_db(conn, customer_id))
    expected = EPOCH_SNAPSHOTS[customer_id]
    assert {name: getattr(values, name) for name in expected} == expected


# Inputs hashes at later instants, computed with the release code on the same profile.
LATER_HASHES = {
    (11, 5): {
        "cus_main": "09aefcfae28e603f5ddff03810dee0e272ced0b56efcf500de7f74a4572c05fd",
        "cus_other": "9dae4754a680d93e422efea612df0d7348bc1b557bd7361f14c8d3c4c6baf789",
    },
    (11, 20): {
        "cus_main": "1fd4834bf3aa60b62c0e9db01aebb0191cc6b5bedf2568024bd1d777766359e7",
        "cus_other": "eb48e31b7559ef5fefd1e6573895fafa693fae5d952b69a9d3e18ef786d2c277",
    },
    (12, 1): {
        "cus_main": "eb72b043848986776ada22f4585b775fe65e0a4997decd2e9cce12f8f3cd9936",
        "cus_other": "bd4f879317b79349b5ba41a3baac13aeb60b23b6a09922f299b11ed6521b2a41",
    },
}


@pytest.mark.parametrize("month_day", sorted(LATER_HASHES))
def test_base_profile_inputs_are_unchanged_later_in_the_month(db, month_day: tuple[int, int]):
    with db.unit_of_work("test clock") as uow:
        advance_clock(uow, datetime(2026, *month_day, tzinfo=SHANGHAI))
    for customer_id, expected in LATER_HASHES[month_day].items():
        with db.read() as conn:
            inputs = cash_inputs_from_db(conn, customer_id)
        assert compute_snapshot(inputs).inputs_hash == expected, customer_id
        assert all(c.payee_id is not None for c in inputs.commitments)
