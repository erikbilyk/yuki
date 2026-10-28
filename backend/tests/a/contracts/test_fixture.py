from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from wg.contracts.fixture import Fixture, FixtureError, load_fixture

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "fixture_v1.yaml"
SPEC_VARIANTS = {"t25", "t35", "t42", "t43", "t52", "t64", "t65"}


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return path


def _raw() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _by_id(items):
    return {item.id: item for item in items}


def test_base_profile_validates():
    fixture = load_fixture(FIXTURE)
    assert isinstance(fixture, Fixture)
    assert fixture.profile == "fixture_v1"
    assert fixture.epoch.isoformat() == "2026-11-02T09:00:00+08:00"
    accounts = _by_id(fixture.accounts)
    assert accounts["acc_main"].opening_fen == 1800000
    assert fixture.reserve.target_fen == 400000
    assert fixture.plan_constants.bounds.p0_fen == 498500
    assert _by_id(fixture.products)["990003"].nav == Decimal("1.0000")


def test_fixture_lists_every_spec_variant():
    assert SPEC_VARIANTS <= set(load_fixture(FIXTURE).variants)


@pytest.mark.parametrize("variant", sorted(load_fixture(FIXTURE).variants))
def test_every_variant_validates(variant):
    assert isinstance(load_fixture(FIXTURE, variant), Fixture)


def test_variant_effects():
    t25 = _by_id(load_fixture(FIXTURE, "t25").accounts)
    assert (t25["acc_main"].opening_fen, t25["acc_savings"].opening_fen) == (200000, 1600000)

    statements = _by_id(load_fixture(FIXTURE, "t35").statements)
    ids = [txn.source_txn_id for txn in statements["stm_main_2610"].transactions]
    assert "MAIN-202610-009" not in ids and len(ids) == 9

    t42 = load_fixture(FIXTURE, "t42")
    assert _by_id(t42.lots)["lot_c1"].acquired_offset_days == -20
    assert _by_id(t42.products)["990003"].fee_schedule == "fee_public_c_v1"

    assert "lot_c1" not in _by_id(load_fixture(FIXTURE, "t43").lots)
    for name, field, days in (
        ("t52", "purchase_settle_days", 3),
        ("t64", "redeem_settle_days", 4),
        ("t65", "redeem_settle_days", 6),
    ):
        products = load_fixture(FIXTURE, name).products
        assert {getattr(product, field) for product in products} == {days}


def test_variant_does_not_leak_into_the_base_profile():
    load_fixture(FIXTURE, "t43")
    assert "lot_c1" in _by_id(load_fixture(FIXTURE).lots)


def test_unknown_variant_is_refused():
    with pytest.raises(FixtureError):
        load_fixture(FIXTURE, "t99")


def test_variant_with_a_missing_path_is_refused(tmp_path):
    doc = _raw()
    doc["variants"]["bad"] = {"set": {"products.990009.nav": "2.0000"}}
    with pytest.raises(FixtureError):
        load_fixture(_write(tmp_path, doc), "bad")
    doc["variants"]["bad"] = {"set": {"products.990001.navv": "2.0000"}}
    with pytest.raises(FixtureError):
        load_fixture(_write(tmp_path, doc), "bad")
    doc["variants"]["bad"] = {"remove": {"lots": ["lot_z9"]}}
    with pytest.raises(FixtureError):
        load_fixture(_write(tmp_path, doc), "bad")


def test_variant_values_are_validated(tmp_path):
    doc = _raw()
    doc["variants"]["bad"] = {
        "set": {"positions": [{"account": "acc_main", "product": "990001", "shares": 20000.0}]}
    }
    with pytest.raises(FixtureError):
        load_fixture(_write(tmp_path, doc), "bad")


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda doc: doc["accounts"][0].update(opening_fen=18000.0), "float money"),
        (lambda doc: doc["accounts"][0].update(opening_fen="1800000"), "string money"),
        (lambda doc: doc["positions"][0].update(shares=20000.0), "float shares"),
        (lambda doc: doc["products"][0].update(nav="1e0"), "exponent NAV"),
        (lambda doc: doc["products"][0].update(id=990001), "numeric fund code"),
        (lambda doc: doc["accounts"][0].update(extra="x"), "unknown field"),
        (lambda doc: doc["accounts"].append(dict(doc["accounts"][0])), "duplicate id"),
        (lambda doc: doc["payees"][0].update(bank="icbc"), "bank outside same/other"),
        (lambda doc: doc["commitments"][0].update(status="OPEN"), "unknown status"),
    ],
)
def test_malformed_profiles_are_refused(tmp_path, mutate, reason):
    doc = _raw()
    mutate(doc)
    with pytest.raises(FixtureError):
        load_fixture(_write(tmp_path, doc))


def test_json_dump_keeps_decimals_as_strings():
    dumped = load_fixture(FIXTURE).model_dump(mode="json")
    assert dumped["positions"][0]["shares"] == "20000.0000"
    assert dumped["plan_constants"]["target_max_pct"] == "40.0000"
    assert dumped["fee_schedules"][0]["tiers"][0]["rate"] == "0.0030"


def test_rewritten_profile_loads_unchanged(tmp_path):
    assert load_fixture(_write(tmp_path, _raw())) == load_fixture(FIXTURE)
