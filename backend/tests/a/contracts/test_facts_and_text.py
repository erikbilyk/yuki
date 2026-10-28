from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from wg.contracts.facts import (
    ClaimCheckResult,
    ClaimViolation,
    ClaimViolationReason,
    Fact,
    FactBlock,
    FactKind,
    MoneyFact,
    PercentFact,
)
from wg.contracts.textcheck import AmountMatch, Comparator, MaskKind, MaskResult

AS_OF = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def _money(fact_id: str = "f1", value=100000) -> dict:
    return {
        "fact_id": fact_id,
        "kind": "money",
        "concept": "headroom",
        "value": value,
        "source_ref": "snp_1",
        "as_of": AS_OF.isoformat(),
        "label": "可自由支配",
    }


def test_facts_are_parsed_by_kind():
    adapter = TypeAdapter(Fact)
    assert isinstance(adapter.validate_python(_money()), MoneyFact)
    percent = adapter.validate_python(
        _money() | {"kind": "percent", "concept": "sector_share", "value": "39.5089"}
    )
    assert isinstance(percent, PercentFact)
    assert adapter.dump_python(percent, mode="json")["value"] == "39.5089"
    with pytest.raises(ValidationError):
        adapter.validate_python(_money(value=1000.0))
    with pytest.raises(ValidationError):
        adapter.validate_python(_money() | {"kind": "percent", "value": 39.5})


def test_fact_block_refuses_duplicate_ids():
    FactBlock(facts=(_money("a"), _money("b")))
    with pytest.raises(ValidationError):
        FactBlock(facts=(_money("a"), _money("a")))


def test_claim_check_result_passes_without_violations():
    assert ClaimCheckResult(claims_checked=3, violations=()).passed
    violation = ClaimViolation(
        claim_kind=FactKind.MONEY,
        text="¥2,000",
        start=4,
        end=10,
        reason=ClaimViolationReason.CONCEPT_MISMATCH,
    )
    result = ClaimCheckResult(claims_checked=3, violations=(violation,))
    assert not result.passed
    assert result.model_dump()["passed"] is False


def test_amount_match():
    match = AmountMatch(fen=340000, start=0, end=6, text="¥3,400")
    assert (match.comparator, match.strict) == (None, False)
    limit = AmountMatch(fen=1, start=2, end=4, text="1元", comparator="<", strict=True)
    assert limit.comparator is Comparator.LT
    AmountMatch(fen=1, start=0, end=2, text="1元", comparator=Comparator.LE, strict=False)
    with pytest.raises(ValidationError):
        AmountMatch(fen=1, start=0, end=1, text="1", strict=True)
    with pytest.raises(ValidationError):
        AmountMatch(fen=1, start=0, end=1, text="1", comparator=Comparator.LE, strict=True)
    with pytest.raises(ValidationError):
        AmountMatch(fen=1, start=0, end=1, text="1", comparator=Comparator.GT, strict=False)
    with pytest.raises(ValidationError):
        AmountMatch(fen=1, start=0, end=1, text="1", comparator="≤")
    with pytest.raises(ValidationError):
        AmountMatch(fen=1, start=3, end=3, text="")


def test_mask_result():
    result = MaskResult(text="验证码 [已隐藏]", kinds=("code", MaskKind.CARD))
    assert result.model_dump(mode="json")["kinds"] == ["code", "card"]
    assert MaskResult(text="x").kinds == ()
    with pytest.raises(ValidationError):
        MaskResult(text="x", kinds=("phone",))
