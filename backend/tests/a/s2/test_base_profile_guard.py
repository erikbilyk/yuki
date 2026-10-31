"""The base profile's cash snapshot at the epoch is pinned (S5-SPEC §11.4).

The values and inputs hashes were computed on the release build before direct-debit
agreements existed; adding them must not change a single input of fixture_v1.
"""

import pytest

from wg.k6.cash_inputs import cash_inputs_from_db
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
