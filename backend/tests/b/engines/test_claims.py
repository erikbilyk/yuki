"""Claim checker: money, percent, count and status claims against typed facts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wg.contracts.facts import (
    ClaimViolationReason as Reason,
)
from wg.contracts.facts import (
    CountFact,
    Fact,
    FactBlock,
    FactConcept,
    MoneyFact,
    NameFact,
    PercentFact,
    StatusFact,
)
from wg.textcheck.claims import check_claims

AS_OF = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def money(
    fact_id: str, concept: FactConcept, fen: int, subject: str | None = None, label: str = "金额"
) -> MoneyFact:
    return MoneyFact(
        fact_id=fact_id,
        concept=concept,
        subject=subject,
        source_ref="snap",
        as_of=AS_OF,
        label=label,
        value=fen,
    )


def status(
    fact_id: str, concept: FactConcept, value: str, subject: str | None = None, label: str = "状态"
) -> StatusFact:
    return StatusFact(
        fact_id=fact_id,
        concept=concept,
        subject=subject,
        source_ref="rcpt",
        as_of=AS_OF,
        label=label,
        value=value,
    )


def percent(
    fact_id: str, value: str, concept: FactConcept = FactConcept.SECTOR_SHARE
) -> PercentFact:
    return PercentFact(
        fact_id=fact_id,
        concept=concept,
        source_ref="calc",
        as_of=AS_OF,
        label="占比",
        value=Decimal(value),
    )


def count(fact_id: str, value: int) -> CountFact:
    return CountFact(
        fact_id=fact_id,
        concept=FactConcept.UNRESOLVED_COUNT,
        source_ref="snap",
        as_of=AS_OF,
        label="笔数",
        value=value,
    )


def block(*facts: Fact) -> FactBlock:
    return FactBlock(facts=facts)


def reasons(text: str, facts: FactBlock) -> list[Reason]:
    return [v.reason for v in check_claims(text, facts).violations]


HEADROOM = money("f_head", FactConcept.HEADROOM, 548500)
ESTIMATE = money("f_est", FactConcept.MONTHLY_ESTIMATE, 300050)


@pytest.mark.parametrize(
    ("text", "facts", "expected"),
    [
        ("您当前可自由支配的金额为¥5,485。", block(HEADROOM), []),
        ("可自由支配 5485 元", block(HEADROOM), []),
        ("本月结余约¥3,000.50", block(ESTIMATE), []),
        ("本月结余约3001元", block(ESTIMATE), []),
        ("本月结余约3000元", block(ESTIMATE), [Reason.PRECISION]),
        ("可自由支配¥5,000", block(HEADROOM), [Reason.VALUE_MISMATCH]),
        ("本月结余约3002元", block(ESTIMATE), [Reason.VALUE_MISMATCH]),
        ("本月结余约¥3,000.51", block(ESTIMATE), [Reason.VALUE_MISMATCH]),
        ("可自由支配¥3,000.50", block(HEADROOM, ESTIMATE), [Reason.CONCEPT_MISMATCH]),
        ("本月结余¥5,485", block(HEADROOM, ESTIMATE), [Reason.CONCEPT_MISMATCH]),
        ("可自由支配¥9,999", block(ESTIMATE), [Reason.UNSUPPORTED]),
        ("共转出¥12,345", block(HEADROOM), [Reason.UNSUPPORTED]),
        ("共转出¥5,485", block(HEADROOM), []),
        ("日期是10月28日，电话13800138000", block(HEADROOM), []),
    ],
)
def test_money_claims(text: str, facts: FactBlock, expected: list[Reason]) -> None:
    assert reasons(text, facts) == expected


def test_whole_yuan_claim_rounds_the_fact_half_up() -> None:
    facts = block(money("f", FactConcept.FEE, 2549), money("g", FactConcept.FEE, 2550))
    assert reasons("手续费约25元", block(facts.facts[0])) == []
    assert reasons("手续费约26元", block(facts.facts[1])) == []
    assert reasons("手续费约25元", block(facts.facts[1])) == [Reason.PRECISION]


def test_money_claim_with_fen_must_match_to_the_fen() -> None:
    facts = block(money("f", FactConcept.FEE, 2549))
    assert reasons("手续费25.49元", facts) == []
    assert reasons("手续费25.00元", facts) == [Reason.VALUE_MISMATCH]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("该基金占比39.5%", []),
        ("该基金占比39.51%", []),
        ("该基金占比39.5089%", []),
        ("该基金占比40%", [Reason.PRECISION]),
        ("该基金占比39.6%", [Reason.VALUE_MISMATCH]),
        ("该基金占比百分之四十", [Reason.PRECISION]),
    ],
)
def test_percent_claims_round_at_the_claimed_precision(text: str, expected: list[Reason]) -> None:
    assert reasons(text, block(percent("f_pct", "39.5089"))) == expected


def test_percent_without_a_fact_is_unsupported() -> None:
    assert reasons("占比39.5%", block(HEADROOM)) == [Reason.UNSUPPORTED]


def test_a_whole_percent_matches_a_whole_fact() -> None:
    assert reasons("目标上限40%", block(percent("f_max", "40", FactConcept.TARGET_MAX))) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("有2笔未确认交易", []),
        ("有两笔未确认交易", []),
        ("有3笔未确认交易", [Reason.VALUE_MISMATCH]),
        ("分3个月完成，每天一次", []),
    ],
)
def test_count_claims(text: str, expected: list[Reason]) -> None:
    assert reasons(text, block(count("f_cnt", 2))) == expected


def test_count_without_a_fact_is_unsupported() -> None:
    assert reasons("有2笔未确认交易", block(HEADROOM)) == [Reason.UNSUPPORTED]


def name(fact_id: str, subject: str, value: str) -> NameFact:
    return NameFact(
        fact_id=fact_id,
        concept=FactConcept.PAYEE,
        subject=subject,
        source_ref="snap",
        as_of=AS_OF,
        label=value,
        value=value,
    )


RENT = [
    name("n_rent", "cmt_rent", "房租"),
    money("f_rent", FactConcept.BILL_AMOUNT, 400000, "cmt_rent", "房租账单"),
    status("f_rent_rcpt", FactConcept.RECEIPT_STATUS, "SETTLED", "cmt_rent", "房租付款"),
]
INSURANCE = [
    name("n_ins", "cmt_ins", "保险"),
    money("f_ins", FactConcept.BILL_AMOUNT, 160000, "cmt_ins", "保险账单"),
    status("f_ins_st", FactConcept.BILL_STATUS, "secured", "cmt_ins", "保险保障"),
]


@pytest.mark.parametrize("word", ["已支付", "已付款", "已转账", "已到账"])
def test_paid_words_need_a_settled_receipt(word: str) -> None:
    assert reasons(f"房租{word}。", block(*RENT)) == []
    submitted = status("f_sub", FactConcept.RECEIPT_STATUS, "SUBMITTED", "cmt_rent", "房租付款")
    assert reasons(f"房租{word}。", block(submitted)) == [Reason.STATUS_UNSUPPORTED]


def test_t15_transfer_is_not_done_while_submitted() -> None:
    facts = block(status("f_op", FactConcept.RECEIPT_STATUS, "SUBMITTED", "opn_1", "转账回执"))
    result = check_claims("已转账，请放心。", facts)
    assert [(v.reason, v.fact_id, v.text) for v in result.violations] == [
        (Reason.STATUS_UNSUPPORTED, "f_op", "已转账")
    ]


def test_paid_word_is_checked_per_bill_when_the_bill_is_named() -> None:
    unpaid = status("f_ins_rcpt", FactConcept.RECEIPT_STATUS, "SUBMITTED", "cmt_ins", "保险付款")
    facts = block(*RENT, *INSURANCE, unpaid)
    assert reasons("房租已支付，保险已支付。", facts) == [Reason.STATUS_UNSUPPORTED]
    assert reasons("房租已支付。", facts) == []


def test_t30_secured_is_not_paid() -> None:
    facts = block(*INSURANCE)
    assert reasons("保险已保障。", facts) == []
    assert reasons("保险已支付。", facts) == [Reason.STATUS_UNSUPPORTED]
    assert reasons("保险尚未支付。", facts) == []


def test_t30_monthly_estimate_is_not_free_money() -> None:
    assert reasons("可自由支配¥3,000.50", block(HEADROOM, ESTIMATE)) == [Reason.CONCEPT_MISMATCH]


def test_secured_words_need_a_secured_bill() -> None:
    pending = status("f_ins_st", FactConcept.BILL_STATUS, "unsecured", "cmt_ins", "保险保障")
    assert reasons("保险已保障。", block(pending)) == [Reason.STATUS_UNSUPPORTED]
    assert reasons("保险已预留。", block(pending)) == [Reason.STATUS_UNSUPPORTED]
    assert reasons("保险已预留。", block(*INSURANCE)) == []


def test_goal_and_plan_words() -> None:
    passed = status("f_cp", FactConcept.CHECKPOINT_RESULT, "passed")
    failed = status("f_cp", FactConcept.CHECKPOINT_RESULT, "failed")
    assert reasons("目标已达成。", block(passed)) == []
    assert reasons("目标已达成。", block(failed)) == [Reason.STATUS_UNSUPPORTED]
    assert reasons("目标已达成。", block(HEADROOM)) == [Reason.STATUS_UNSUPPORTED]
    done = status("f_pl", FactConcept.PLAN_STATUS, "completed", "pln_1")
    active = status("f_pl", FactConcept.PLAN_STATUS, "active", "pln_1")
    assert reasons("计划已完成。", block(done)) == []
    assert reasons("计划已经完成。", block(active)) == [Reason.STATUS_UNSUPPORTED]


def test_free_to_spend_words_need_their_fact() -> None:
    assert reasons("这部分钱可自由支配。", block(HEADROOM)) == []
    assert reasons("这部分钱可自由支配。", block(ESTIMATE)) == [Reason.STATUS_UNSUPPORTED]
    assert reasons("本月结余为正。", block(HEADROOM)) == [Reason.STATUS_UNSUPPORTED]
    assert reasons("本月结余为正。", block(ESTIMATE)) == []


PURCHASE = money("f_buy", FactConcept.PURCHASE_AMOUNT, 438500, "opt_1", "可申购金额")
FEASIBLE = status(
    "f_feas",
    FactConcept.OPTION_FEASIBILITY,
    FEASIBLE_VALUE := "feasible_with_new_approval",
    "opt_1",
)


def test_t47_denying_a_feasible_amount_is_a_violation() -> None:
    facts = block(PURCHASE, FEASIBLE)
    result = check_claims("¥4,385是不可能的。", facts)
    assert [(v.reason, v.fact_id) for v in result.violations] == [
        (Reason.FEASIBILITY_DENIED, "f_buy")
    ]
    assert reasons("无法申购4385元", facts) == [Reason.FEASIBILITY_DENIED]


def test_t47_feasible_amount_may_be_described_with_its_condition() -> None:
    facts = block(PURCHASE, FEASIBLE)
    assert reasons("¥4,385需要新的批准后才可申购。", facts) == []


def test_t47_infeasible_amount_may_be_denied() -> None:
    other = money("f_big", FactConcept.PURCHASE_AMOUNT, 478500, "opt_2", "可申购金额")
    infeasible = status("f_inf", FactConcept.OPTION_FEASIBILITY, "infeasible", "opt_2")
    facts = block(PURCHASE, FEASIBLE, other, infeasible)
    assert reasons("¥4,785无法申购，¥4,385需要新的批准。", facts) == []


def test_t47_denial_in_another_clause_is_not_flagged() -> None:
    facts = block(PURCHASE, FEASIBLE)
    assert reasons("无法直接执行，¥4,385需要新的批准。", facts) == []


def test_t75_summary_claims() -> None:
    facts = block(
        *INSURANCE,
        name("n_new", "cmt_new", "新账单"),
        money("f_new", FactConcept.BILL_AMOUNT, 50000, "cmt_new", "新账单金额"),
    )
    assert reasons("保险已保障，尚未支付；新账单尚未支付。", facts) == []
    assert reasons("新账单已支付。", facts) == [Reason.STATUS_UNSUPPORTED]


def test_claims_checked_and_spans() -> None:
    text = "可自由支配¥5,485，占比39.5%"
    result = check_claims(text, block(HEADROOM, percent("f_pct", "39.5089")))
    assert result.passed
    assert result.claims_checked == 2
    bad = check_claims("共转出¥12,345", block(HEADROOM))
    violation = bad.violations[0]
    assert bad.claims_checked == 1
    assert "共转出¥12,345"[violation.start : violation.end] == violation.text == "¥12,345"


def test_text_without_claims_passes() -> None:
    result = check_claims("好的，我来帮您看看。", block())
    assert result.passed
    assert result.claims_checked == 0
