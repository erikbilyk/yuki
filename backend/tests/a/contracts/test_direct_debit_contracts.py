"""Direct-debit contracts (S5-SPEC §3): API models, the engine input and the fixture models."""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from wg.contracts import api
from wg.contracts.api import (
    CashDetails,
    DebitRequestView,
    MandateChangeRequest,
    MandateCreateRequest,
    ReceiptView,
    SimDebitRequest,
)
from wg.contracts.engines import CommitmentInput
from wg.contracts.enums import CommitmentStatus
from wg.contracts.fixture import FixtureError, load_fixture, plus_months

from ..s5_profile import s5_doc, write_profile

NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)

S5_MODELS = [
    api.MerchantView,
    api.MerchantsView,
    api.ProtectionPeriodView,
    api.MandateView,
    api.MandatesView,
    api.DebitRequestView,
    api.DebitRequestsQuery,
    api.DebitRequestsView,
    api.SubscriptionSpendQuery,
    api.SubscriptionSpendRow,
    api.SubscriptionSpendView,
    api.SubscriptionPeriodState,
    api.MandateChangeRequest,
    api.DebitDecisionRequest,
    api.MandateCreateRequest,
    api.SimDebitRequest,
    api.SimDebitResult,
]


def _numbers(schema: object) -> list[str]:
    if isinstance(schema, dict):
        found = ["number"] if schema.get("type") == "number" else []
        return found + [n for value in schema.values() for n in _numbers(value)]
    if isinstance(schema, list):
        return [n for value in schema for n in _numbers(value)]
    return []


@pytest.mark.parametrize("model", S5_MODELS, ids=lambda model: model.__name__)
def test_direct_debit_schemas_have_no_float(model):
    for mode in ("validation", "serialization"):
        assert _numbers(model.model_json_schema(mode=mode)) == []


def test_commitment_input_payee_is_optional_and_existing_rows_serialize_unchanged():
    with_payee = CommitmentInput(
        commitment_id="cmt_rent",
        purpose_id="pur_rent",
        status=CommitmentStatus.CONFIRMED,
        amount_fen=340_000,
        unpaid_fen=340_000,
        due_date=date(2026, 11, 10),
        payee_id="pay_landlord",
    )
    assert with_payee.model_dump_json() == (
        '{"commitment_id":"cmt_rent","purpose_id":"pur_rent","status":"CONFIRMED",'
        '"amount_fen":340000,"unpaid_fen":340000,"due_date":"2026-11-10",'
        '"payee_id":"pay_landlord"}'
    )
    subscription = with_payee.model_copy(
        update={"commitment_id": "cmt_sub_video_202611", "payee_id": None}
    )
    assert subscription.payee_id is None


def test_mandate_change_takes_a_cap_exactly_for_set_cap():
    assert MandateChangeRequest(mandate_ref="mdt_video", change="cancel").cap_fen is None
    assert MandateChangeRequest(mandate_ref="mdt_video", change="set_cap", cap_fen=2000).cap_fen
    with pytest.raises(ValidationError):
        MandateChangeRequest(mandate_ref="mdt_video", change="set_cap")
    with pytest.raises(ValidationError):
        MandateChangeRequest(mandate_ref="mdt_video", change="suspend", cap_fen=2000)
    with pytest.raises(ValidationError):
        MandateChangeRequest(mandate_ref="mdt_video", change="set_cap", cap_fen=20.0)


def test_mandate_create_needs_a_cap_at_least_the_expected_amount():
    body = {"merchant_id": "mer_music", "expected_fen": 1500, "debit_day": 31, "months": 12}
    assert MandateCreateRequest(**body, cap_fen=1500).cap_fen == 1500
    for bad in (
        {"cap_fen": 1499},
        {"cap_fen": 2000, "months": 13},
        {"cap_fen": 2000, "debit_day": 0},
    ):
        with pytest.raises(ValidationError):
            MandateCreateRequest(**{**body, **bad})


def test_sim_debit_request_accepts_any_integer_and_any_currency_text():
    # Format errors are decided and audited by the request check, not by the body model.
    body = {"merchant_id": "anything", "merchant_request_id": "r-1", "mandate_ref": "x"}
    for amount, currency in ((-5, "CNY"), (0, "USD"), (2500, "cny")):
        request = SimDebitRequest(**body, amount_fen=amount, currency=currency)
        assert request.amount_fen == amount and request.description_raw == ""
    for bad in ({"amount_fen": 25.0}, {"amount_fen": "2500"}, {"currency": ""}):
        with pytest.raises(ValidationError):
            SimDebitRequest(**{**body, "amount_fen": 1, "currency": "CNY", **bad})
    with pytest.raises(ValidationError):
        SimDebitRequest(**body, amount_fen=1, currency="CNY", description_raw="字" * 141)


def _request(**changes) -> dict:
    return {
        "request_id": "req_1",
        "mandate_ref": "mdt_video",
        "merchant_id": "mer_video",
        "merchant_display": "模拟视频会员",
        "amount_fen": 2500,
        "currency": "CNY",
        "period_key": "2026-11",
        "status": "AWAITING_CLIENT",
        "received_at": NOW,
        "respond_by": NOW,
        "description_raw": "请立即支付",
        "payable": True,
        **changes,
    }


def test_debit_request_reason_follows_its_status():
    assert DebitRequestView(**_request()).reason_code is None
    capped = DebitRequestView(**_request(status="OUT_OF_MANDATE", reason_code="CAP", payable=False))
    assert capped.reason_code == "CAP"
    for bad in (
        {"status": "OUT_OF_MANDATE", "payable": False},
        {"reason_code": "CAP"},
        {"status": "PAID", "payable": True},
        {"period_key": "2026-13"},
        {"currency": "USD"},
    ):
        with pytest.raises(ValidationError):
            DebitRequestView(**_request(**bad))


def test_receipt_and_cash_details_gain_optional_fields_only():
    receipt = ReceiptView.model_fields
    for name in ("mandate_ref", "request_id", "merchant_display", "effect_check"):
        assert not receipt[name].is_required()
    assert CashDetails.model_fields["subscriptions"].default == ()


# Fixture models (S5-SPEC §3.6)


def test_direct_debit_profile_validates(tmp_path: Path):
    fixture = load_fixture(write_profile(tmp_path))
    assert [m.id for m in fixture.merchants][0] == "mer_video"
    assert {m.id: m.debit_day for m in fixture.mandates}["mdt_fitness"] == 20
    suspended = load_fixture(write_profile(tmp_path), "video_suspended")
    assert {m.id: m.status for m in suspended.mandates}["mdt_video"] == "SUSPENDED"


def test_base_profile_has_no_merchants_or_agreements():
    base = load_fixture(Path(__file__).resolve().parents[2] / "fixtures" / "fixture_v1.yaml")
    assert base.merchants == () and base.mandates == ()


def _mandate(doc: dict, **changes) -> dict:
    doc["mandates"][0].update(changes)
    return doc


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["merchants"].append(dict(doc["merchants"][0])),
        lambda doc: doc["mandates"].append(dict(doc["mandates"][0])),
        lambda doc: _mandate(doc, status="CANCELLED"),
        lambda doc: _mandate(doc, debit_day=32),
        lambda doc: _mandate(doc, cap_fen=0),
        lambda doc: _mandate(doc, expected_fen=25.0),
        lambda doc: _mandate(doc, id="mdt_" + "a" * 41),
        lambda doc: _mandate(doc, expires_offset_days=-32),
        # 2026-10-02 + 12 months is 2027-10-02, offset 334 from the 2026-11-02 epoch.
        lambda doc: _mandate(doc, expires_offset_days=335),
        lambda doc: doc["merchants"][0].update(verified="yes"),
    ],
    ids=[
        "duplicate merchant",
        "duplicate mandate",
        "status cancelled",
        "day 32",
        "cap zero",
        "float money",
        "long ref",
        "expires before start",
        "term over 12 months",
        "verified not a bool",
    ],
)
def test_malformed_direct_debit_profiles_are_refused(tmp_path: Path, mutate):
    doc = s5_doc()
    mutate(doc)
    with pytest.raises(FixtureError):
        load_fixture(write_profile(tmp_path, doc))


def test_a_term_of_exactly_twelve_months_is_accepted(tmp_path: Path):
    doc = _mandate(s5_doc(), expires_offset_days=334)
    assert load_fixture(write_profile(tmp_path, doc)).mandates[0].expires_offset_days == 334


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 10, 2), 12, date(2027, 10, 2)),
        (date(2028, 2, 29), 12, date(2029, 3, 1)),
        (date(2026, 1, 31), 1, date(2026, 3, 3)),
        (date(2026, 12, 31), 1, date(2027, 1, 31)),
    ],
)
def test_plus_months_matches_sqlite(day: date, months: int, expected: date):
    conn = sqlite3.connect(":memory:")
    sqlite = conn.execute(f"SELECT date(?, '+{months} months')", (day.isoformat(),)).fetchone()
    assert plus_months(day, months) == expected == date.fromisoformat(sqlite[0])
