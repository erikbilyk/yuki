"""Checks the money, percent, count and status claims of an explanation against typed facts.

The check is bounded: it finds amounts, percentages, transaction counts and a fixed list of
status words, and requires each to be supported by a fact of the right meaning (SPEC §17).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from wg.contracts.facts import (
    ClaimCheckResult,
    ClaimViolation,
    ClaimViolationReason,
    CountFact,
    Fact,
    FactBlock,
    FactConcept,
    FactKind,
    MoneyFact,
    PercentFact,
    StatusFact,
)
from wg.textcheck.amounts import find_amounts, parse_chinese_numeral

Reason = ClaimViolationReason

FEASIBLE_WITH_APPROVAL = "feasible_with_new_approval"

_CLAUSE_END_RE = re.compile(r"[。！？!?；;\n，、]|(?<!\d),|,(?!\d)")
_DENIAL_RE = re.compile(r"不可能|无法|不能|做不到|办不到|不可行")

_MONEY_KEYWORDS: tuple[tuple[re.Pattern[str], FactConcept], ...] = (
    (re.compile(r"可自由支配|自由支配|可支配"), FactConcept.HEADROOM),
    (re.compile(r"本月结余|月度结余|每月结余|月结余"), FactConcept.MONTHLY_ESTIMATE),
    (re.compile(r"缺口"), FactConcept.SHORTFALL),
    (re.compile(r"手续费|赎回费"), FactConcept.FEE),
)

_PERCENT_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*[%％]|百分之([零〇一二两三四五六七八九十百]+|\d+(?:\.\d+)?)"
)
_NEW_BILL_RE = re.compile(r"新账单|新增账单|新的账单")
_COUNT_RE = re.compile(r"(?<![\d.])(\d+|[零〇一二两三四五六七八九十]{1,3})\s*(?:笔|单)")


@dataclass(frozen=True)
class StatusRule:
    """A status word and the fact that must exist before the text may use it."""

    pattern: re.Pattern[str]
    concept: FactConcept
    value: str


_STATUS_RULES: tuple[StatusRule, ...] = (
    StatusRule(re.compile(r"已支付|已付款|已转账|已到账"), FactConcept.RECEIPT_STATUS, "SETTLED"),
    StatusRule(re.compile(r"已保障|已预留"), FactConcept.BILL_STATUS, "secured"),
    StatusRule(
        re.compile(r"目标已达成|已达成目标|已达标"), FactConcept.CHECKPOINT_RESULT, "passed"
    ),
    StatusRule(re.compile(r"计划已(?:经)?完成|已完成计划"), FactConcept.PLAN_STATUS, "completed"),
)


def check_claims(explanation: str, block: FactBlock) -> ClaimCheckResult:
    """Return the claims in the explanation that no fact in the block supports."""
    clauses = _clauses(explanation)
    checker = _Checker(explanation, block, clauses)
    checker.check_money()
    checker.check_percents()
    checker.check_counts()
    checker.check_statuses()
    return ClaimCheckResult(
        claims_checked=checker.checked,
        violations=tuple(sorted(checker.violations, key=lambda v: (v.start, v.end))),
    )


def _clauses(text: str) -> list[tuple[int, int]]:
    """Spans of the clauses of the text; a comma inside a number does not end a clause."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _CLAUSE_END_RE.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return spans


def _round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _money_equals(claim_fen: int, fact_fen: int, *, with_fen: bool) -> bool:
    """A claim written with fen equals the fact to the fen; otherwise to the yuan, half up."""
    if claim_fen == fact_fen:
        return True
    return not with_fen and claim_fen == (fact_fen + 50) // 100 * 100


def _written_with_fen(text: str, fen: int) -> bool:
    has_unit = re.search(r"[万千kKwW]|тыс", text) is not None
    return fen % 100 != 0 or (re.search(r"\.\d{2}(?!\d)", text) is not None and not has_unit)


class _Checker:
    def __init__(self, text: str, block: FactBlock, clauses: Sequence[tuple[int, int]]) -> None:
        self.text = text
        self.clauses = clauses
        self.checked = 0
        self.violations: list[ClaimViolation] = []
        self._facts = block.facts
        self._labels = self._labels_by_subject(block.facts)
        self._bill_subjects = {
            f.subject
            for f in block.facts
            if f.concept == FactConcept.BILL_STATUS and f.subject is not None
        }
        self._keyword_spans_used: set[tuple[int, int]] = set()

    def check_money(self) -> None:
        money = [f for f in self._facts if isinstance(f, MoneyFact)]
        for amount in find_amounts(self.text):
            self.checked += 1
            clause = self._clause_of(amount.start)
            keyword = self._nearest_keyword(clause, amount.start, amount.end)
            with_fen = _written_with_fen(amount.text, amount.fen)
            matching = [f for f in money if _money_equals(amount.fen, f.value, with_fen=with_fen)]
            if keyword is not None:
                self._keyword_spans_used.add(keyword[:2])
                reason, fact_id = self._judge_concept(
                    amount.fen, keyword[2], matching, money, with_fen=with_fen
                )
            else:
                reason, fact_id = (None, None) if matching else (Reason.UNSUPPORTED, None)
            if reason is not None:
                self._report(FactKind.MONEY, amount.start, amount.end, reason, fact_id)
            elif _DENIAL_RE.search(self.text[clause[0] : clause[1]]):
                self._check_denial(amount.start, amount.end, matching)

    def _judge_concept(
        self,
        claim_fen: int,
        concept: FactConcept,
        matching: list[MoneyFact],
        money: list[MoneyFact],
        *,
        with_fen: bool,
    ) -> tuple[Reason | None, str | None]:
        if any(f.concept == concept for f in matching):
            return None, None
        if matching:
            return Reason.CONCEPT_MISMATCH, matching[0].fact_id
        of_concept = [f for f in money if f.concept == concept]
        if not of_concept:
            return Reason.UNSUPPORTED, None
        truncated = not with_fen and any(f.value // 100 * 100 == claim_fen for f in of_concept)
        return (Reason.PRECISION if truncated else Reason.VALUE_MISMATCH), of_concept[0].fact_id

    def _check_denial(self, start: int, end: int, matching: list[MoneyFact]) -> None:
        """Flag a denial word next to an amount a fact marks as feasible with a new approval."""
        feasible = {
            f.subject
            for f in self._facts
            if isinstance(f, StatusFact)
            and f.concept == FactConcept.OPTION_FEASIBILITY
            and f.value == FEASIBLE_WITH_APPROVAL
        }
        for fact in matching:
            if fact.concept == FactConcept.PURCHASE_AMOUNT and fact.subject in feasible:
                self._report(FactKind.MONEY, start, end, Reason.FEASIBILITY_DENIED, fact.fact_id)
                return

    def check_percents(self) -> None:
        percents = [f for f in self._facts if isinstance(f, PercentFact)]
        for match in _PERCENT_RE.finditer(self.text):
            self.checked += 1
            claim = _percent_value(match)
            if claim is None:
                continue
            places = max(1, len(match.group(0).partition(".")[2].rstrip("%％ ")))
            if any(_round_half_up(f.value, places) == claim for f in percents):
                continue
            coarse = [f for f in percents if _round_half_up(f.value, 0) == claim]
            if coarse:
                reason, fact_id = Reason.PRECISION, coarse[0].fact_id
            elif percents:
                reason, fact_id = Reason.VALUE_MISMATCH, None
            else:
                reason, fact_id = Reason.UNSUPPORTED, None
            self._report(FactKind.PERCENT, match.start(), match.end(), reason, fact_id)

    def check_counts(self) -> None:
        counts = [f for f in self._facts if isinstance(f, CountFact)]
        for match in _COUNT_RE.finditer(self.text):
            token = match.group(1)
            claim = int(token) if token.isdigit() else parse_chinese_numeral(token)
            if claim is None:
                continue
            self.checked += 1
            if any(f.value == claim for f in counts):
                continue
            reason = Reason.VALUE_MISMATCH if counts else Reason.UNSUPPORTED
            fact_id = counts[0].fact_id if counts else None
            self._report(FactKind.COUNT, match.start(), match.end(), reason, fact_id)

    def check_statuses(self) -> None:
        for rule in _STATUS_RULES:
            for match in rule.pattern.finditer(self.text):
                self.checked += 1
                clause = self._clause_of(match.start())
                supported, fact_id = self._status_support(rule, clause)
                if not supported:
                    self._report(
                        FactKind.STATUS,
                        match.start(),
                        match.end(),
                        Reason.STATUS_UNSUPPORTED,
                        fact_id,
                    )
        self._check_concept_words()

    def _check_concept_words(self) -> None:
        """The words 可自由支配 and 本月结余 without an amount still need a fact of that concept."""
        for pattern, concept in _MONEY_KEYWORDS[:2]:
            for match in pattern.finditer(self.text):
                if match.span() in self._keyword_spans_used:
                    continue
                self.checked += 1
                if not any(f.concept == concept for f in self._facts):
                    self._report(
                        FactKind.STATUS,
                        match.start(),
                        match.end(),
                        Reason.STATUS_UNSUPPORTED,
                        None,
                    )

    def _status_support(self, rule: StatusRule, clause: tuple[int, int]) -> tuple[bool, str | None]:
        """Whether a fact supports the status word, and the fact it was compared with."""
        candidates = [
            f for f in self._facts if isinstance(f, StatusFact) and f.concept == rule.concept
        ]
        mentioned = self._subjects_named(clause)
        wrong = next(
            (f for f in candidates if f.subject in mentioned and f.value != rule.value), None
        )
        if wrong is not None:
            return False, wrong.fact_id
        backed = {f.subject for f in candidates}
        unbacked = sorted(s for s in mentioned if s not in backed)
        if unbacked:
            fact = next(f for f in self._facts if f.subject == unbacked[0])
            return False, fact.fact_id
        if mentioned:
            return True, None
        if any(f.value == rule.value for f in candidates):
            return True, None
        return False, candidates[0].fact_id if candidates else None

    def _subjects_named(self, clause: tuple[int, int]) -> set[str]:
        """Subjects the clause refers to by a label of one of their facts or as the new bill."""
        clause_text = self.text[clause[0] : clause[1]]
        named = {
            subject
            for subject, labels in self._labels.items()
            if any(label in clause_text for label in labels)
        }
        if _NEW_BILL_RE.search(clause_text):
            named |= self._bill_subjects
        return named

    @staticmethod
    def _labels_by_subject(facts: Sequence[Fact]) -> dict[str, set[str]]:
        """Names a subject may be called by: the labels of its facts and their segments."""
        labels: dict[str, set[str]] = {}
        for fact in facts:
            if fact.subject is None:
                continue
            names = labels.setdefault(fact.subject, set())
            names.update(
                part for part in re.split(r"[\s·:：/（）()、]+", fact.label) if len(part) >= 2
            )
        return labels

    def _clause_of(self, position: int) -> tuple[int, int]:
        return next((c for c in self.clauses if c[0] <= position <= c[1]), (0, len(self.text)))

    def _nearest_keyword(
        self, clause: tuple[int, int], start: int, end: int
    ) -> tuple[int, int, FactConcept] | None:
        best: tuple[int, tuple[int, int, FactConcept]] | None = None
        window = self.text[clause[0] : clause[1]]
        for pattern, concept in _MONEY_KEYWORDS:
            for match in pattern.finditer(window):
                k_start, k_end = clause[0] + match.start(), clause[0] + match.end()
                distance = max(k_start - end, start - k_end, 0)
                if best is None or distance < best[0]:
                    best = (distance, (k_start, k_end, concept))
        return None if best is None else best[1]

    def _report(
        self, kind: FactKind, start: int, end: int, reason: Reason, fact_id: str | None
    ) -> None:
        self.violations.append(
            ClaimViolation(
                claim_kind=kind,
                text=self.text[start:end],
                start=start,
                end=end,
                reason=reason,
                fact_id=fact_id,
            )
        )


def _percent_value(match: re.Match[str]) -> Decimal | None:
    if match.group(1) is not None:
        return Decimal(match.group(1))
    token = match.group(2)
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return Decimal(token)
    value = parse_chinese_numeral(token)
    return None if value is None else Decimal(value)
