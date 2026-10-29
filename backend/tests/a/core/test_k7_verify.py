"""K7: fact rendering and the replacement templates."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from wg.contracts.facts import (
    CountFact,
    DateFact,
    FactBlock,
    FactConcept,
    MoneyFact,
    PercentFact,
    StatusFact,
)
from wg.core.k7_verify import (
    TEMPLATE_AWAITING,
    TEMPLATE_DRAFT,
    TEMPLATE_EMPTY,
    TEMPLATE_FACTS,
    fallback_explanation,
    format_yuan,
    render_fact,
)
from wg.textcheck.claims import check_claims

pytestmark = pytest.mark.layer("U")

AS_OF = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
COMMON = {"source_ref": "snp_1", "as_of": AS_OF}


def status(value: str, concept: FactConcept = FactConcept.ACTION_STATUS) -> StatusFact:
    return StatusFact(fact_id=value, concept=concept, label="转给妈妈", value=value, **COMMON)


@pytest.mark.parametrize(
    ("fen", "text"),
    [(0, "¥0.00"), (5, "¥0.05"), (340_000, "¥3,400.00"), (123_456_789, "¥1,234,567.89")],
)
def test_money_is_shown_in_yuan_with_fen(fen: int, text: str):
    assert format_yuan(fen) == text


def test_each_fact_kind_renders_from_its_template():
    facts = [
        MoneyFact(
            fact_id="m", concept=FactConcept.HEADROOM, label="可自由支配", value=12_345, **COMMON
        ),
        PercentFact(
            fact_id="p",
            concept=FactConcept.SECTOR_SHARE,
            label="科技占比",
            value=Decimal("39.5089"),
            **COMMON,
        ),
        CountFact(
            fact_id="c", concept=FactConcept.BILL_UNPAID, label="未支付账单笔数", value=2, **COMMON
        ),
        DateFact(
            fact_id="d",
            concept=FactConcept.DUE_DATE,
            label="保险到期日",
            value=date(2026, 11, 9),
            **COMMON,
        ),
        status("AWAITING_APPROVAL"),
        StatusFact(
            fact_id="b",
            concept=FactConcept.BILL_STATUS,
            label="保险",
            value="secured",
            **COMMON,
        ),
    ]
    assert [render_fact(f) for f in facts] == [
        "可自由支配：¥123.45",
        "科技占比：39.51%",
        "未支付账单笔数：2笔",
        "保险到期日：2026年11月9日",
        "转给妈妈：等待您在确认卡片上确认",
        "保险：资金已保障，尚未支付",
    ]


@pytest.mark.parametrize(
    ("facts", "template"),
    [
        ((status("DRAFT"), status("AWAITING_APPROVAL")), TEMPLATE_AWAITING),
        ((status("DRAFT"),), TEMPLATE_DRAFT),
        ((status("SETTLED", FactConcept.RECEIPT_STATUS),), TEMPLATE_FACTS),
        ((), TEMPLATE_EMPTY),
    ],
)
def test_the_replacement_follows_the_facts_and_passes_its_own_check(facts, template):
    block = FactBlock(facts=facts)
    assert fallback_explanation(block) == template
    assert check_claims(template, block).passed
