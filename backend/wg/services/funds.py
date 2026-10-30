"""S3 reads and the rebalance plan draft: positions, exposure, fees, candidates, stress, plans.

Every number comes from B's funds engine (`wg.numeric`) on stored rows, and every
calculation is stored as an immutable calc result with its basis (SPEC §14.7). Drafting a
plan creates its three nodes and its YELLOW plan card; it grants nothing. The customer's
confirmation of that card is the plan approval (SPEC §9.2), and each REDEEM and PURCHASE
still needs its own code when it becomes due.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from wg.contracts.api import (
    CandidatesRequest,
    CandidatesView,
    CardLine,
    DraftResult,
    ExposureBasis,
    ExposureQuery,
    ExposureView,
    FeeEstimateView,
    PercentChange,
    PercentQuality,
    PlanDraftResult,
    PositionsView,
    PositionView,
    StressView,
)
from wg.contracts.engines import (
    Candidate,
    CandidateKind,
    CandidateReason,
    CandidateSearchInput,
    CandidateSearchResult,
    HoldingsSnapshot,
    PlanBounds,
    PositionValue,
    StressResult,
    StressShock,
)
from wg.contracts.enums import ActionKind, AuditActor, PlanKind, PlanMode, Provenance, Tier
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.contracts.money import quantize_fen
from wg.contracts.plan import PlanValidationResult
from wg.core import k4_approval as k4
from wg.core.k1_session import not_found, owned
from wg.core.k3_policy import check_not_locked
from wg.core.k4_approval import CardContent
from wg.core.k5_coordinator import plan_node_terms
from wg.k6 import accounts, actions, audit, calc_results, funds, plans, snapshots
from wg.k6.actions import ActionRow
from wg.k6.calc_results import CalcKind, CalcResultRow
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.funds import FOUR_PLACES, ProductRow
from wg.k6.simtime import SHANGHAI, now, to_db
from wg.numeric import exposure, redemption_fee, search_candidates, stress
from wg.numeric.candidates import inclusive_cap_fen
from wg.numeric.fees import purchase_fee_fen
from wg.packs.base import fen_text
from wg.packs.funds import settlement_due, shares_bought
from wg.packs.registry import pack_for
from wg.services.refusals import record_refusal

CHECKPOINT_TIME = time(9, 0)

# The four fixed shocks of S3 §6, percent loss per fixture fund (A, B, C, D). They illustrate
# the trade-off of a swap; they are not a forecast.
STRESS_SHOCKS: tuple[StressShock, ...] = tuple(
    StressShock(
        name=name,
        loss_pct={
            code: Decimal(pct)
            for code, pct in zip(("990001", "990002", "990003", "990004"), pcts, strict=True)
        },
    )
    for name, pcts in (
        ("market", ("20", "22", "25", "18")),
        ("technology", ("12", "20", "35", "4")),
        ("target_fund", ("10", "15", "5", "40")),
        ("macro", ("15", "18", "20", "16")),
    )
)
SHOCK_LABELS = {
    "market": "广泛市场",
    "technology": "科技板块",
    "target_fund": "目标基金",
    "macro": "宏观",
}
SECTOR_LABELS = {"technology": "科技"}

# The candidate reason a refused plan draft reports, as the error code that names it.
_REASON_CODES: Mapping[CandidateReason, ErrorCode] = {
    CandidateReason.TARGET_NOT_MET: ErrorCode.UNSUPPORTED_REQUEST,
    CandidateReason.TARGET_UNVERIFIED: ErrorCode.TARGET_UNVERIFIED,
    CandidateReason.FEE_CAP: ErrorCode.FEE_CAP,
    CandidateReason.FEE_UNVERIFIED: ErrorCode.DATA_INCOMPLETE,
    CandidateReason.TURNOVER_CAP: ErrorCode.TURNOVER_CAP,
    CandidateReason.PURCHASE_BELOW_MIN: ErrorCode.PURCHASE_BELOW_MIN,
    CandidateReason.SHARE_ROUNDING: ErrorCode.INVALID_DECIMAL,
    CandidateReason.INSUFFICIENT_HEADROOM: ErrorCode.INSUFFICIENT_HEADROOM,
    CandidateReason.SETTLEMENT_CALENDAR: ErrorCode.UNSUPPORTED_REQUEST,
}
_TARGET_REASONS = frozenset({CandidateReason.TARGET_NOT_MET, CandidateReason.TARGET_UNVERIFIED})


def _owner(record: Any) -> str | None:
    return record.customer_id


class FundsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # Reads

    def positions(self, customer_id: str) -> PositionsView:
        """Held funds at their NAV; free shares exclude those in a pending redemption."""
        with self.db.read() as conn:
            views = []
            for position in funds.customer_positions(conn, customer_id):
                product = funds.get_product(conn, position.product_id)
                pending = funds.pending_redemption_shares(
                    conn, position.account_id, position.product_id
                )
                views.append(
                    PositionView(
                        product_id=product.product_id,
                        name=product.name,
                        share_class=product.share_class,
                        shares=position.shares,
                        free_shares=max(position.shares - pending, Decimal(0)),
                        nav=product.nav,
                        nav_date=product.nav_date,
                        value_fen=quantize_fen(position.shares * product.nav),
                    )
                )
            return PositionsView(
                as_of=now(conn),
                positions=tuple(views),
                total_fen=sum(view.value_fen for view in views),
            )

    def exposure(self, customer_id: str, query: ExposureQuery) -> ExposureView:
        """Sector or issuer share of the settled positions, stored as a calc result.

        Public snapshots are dated diagnostics (SPEC §22); none is loaded in this build.
        """
        executable = query.basis is ExposureBasis.EXECUTABLE
        with self._audited(customer_id, "exposure_refused", query.key):

            def run(uow: UnitOfWork) -> tuple[str, Any]:
                positions = funds.position_values(uow, customer_id)
                holdings = funds.holdings_snapshots(uow, executable=executable)
                if not holdings or not positions:
                    raise WgError(ErrorCode.DATA_INCOMPLETE, {"reason": "no_holdings"})
                result = exposure(positions, holdings, query.group, query.key, query.target_max_pct)
                stored = calc_results.store(
                    uow,
                    customer_id=customer_id,
                    kind=CalcKind.EXPOSURE,
                    inputs={
                        "positions": _dump(positions),
                        "holdings": _holdings_basis(holdings),
                        "query": query.model_dump(mode="json"),
                    },
                    basis=_basis(uow, holdings, None),
                    output={"exposure": result.model_dump(mode="json")},
                )
                return stored.calc_result_id, result

            calc_result_id, result = self.db.run_in_uow(run, "exposure")
        return ExposureView(
            calc_result_id=calc_result_id,
            basis=query.basis,
            quality=PercentQuality.CONFIRMED,
            exposure=result,
        )

    def fee_estimate(self, customer_id: str, product_id: str, shares: Decimal) -> FeeEstimateView:
        """The redemption fee over the customer's lots, FIFO; unknown lots give `unverified`."""
        with self.db.read() as conn:
            product = funds.find_product(conn, product_id)
            if product is None:
                raise not_found()
            lots = [
                lot
                for lot in funds.customer_lots(conn, customer_id)
                if lot.product_id == product_id
            ]
            quote = redemption_fee(
                lots,
                shares,
                product.nav,
                product.fee_schedule,
                now(conn).astimezone(SHANGHAI).date(),
            )
        return FeeEstimateView(product_id=product_id, shares=shares, quote=quote)

    # Candidates and stress

    def candidates(self, customer_id: str, request: CandidatesRequest) -> CandidatesView:
        """The grid search on settled positions and the current headroom, stored with its basis."""
        with self._audited(customer_id, "candidates_refused", request.sector):
            calc = self.db.run_in_uow(
                lambda uow: self._search(uow, customer_id, request), "candidate search"
            )
        return CandidatesView(
            calc_result_id=calc.calc_result_id,
            result=CandidateSearchResult.model_validate(calc.output["result"]),
        )

    def _search(
        self, uow: UnitOfWork, customer_id: str, request: CandidatesRequest
    ) -> CalcResultRow:
        at = now(uow)
        constants = funds.plan_constants(uow, customer_id)
        snapshot = snapshots.ensure_snapshot(uow, customer_id)
        holdings = funds.holdings_snapshots(uow, executable=True)
        positions = funds.position_values(uow, customer_id)
        if not holdings or not positions:
            raise WgError(ErrorCode.DATA_INCOMPLETE, {"reason": "no_holdings"})
        checkpoint_day = request.checkpoint_day or constants.checkpoint_day
        trade_date = at.astimezone(SHANGHAI).date()
        fee_cap = inclusive_cap_fen(request.fee_cap_fen, strict=not request.fee_cap_inclusive)
        turnover_cap = (
            request.turnover_cap_fen
            if request.turnover_cap_fen is not None
            else constants.turnover_cap_fen
        )
        inputs = CandidateSearchInput(
            as_of=trade_date,
            checkpoint_on=trade_date + timedelta(days=checkpoint_day),
            positions=tuple(positions),
            snapshots=tuple(holdings),
            products=tuple(p.engine_product() for p in funds.all_products(uow)),
            lots=tuple(funds.customer_lots(uow, customer_id)),
            sector=request.sector,
            target_max_pct=_engine_target(request.target_max_pct, request.target_inclusive),
            fee_cap_fen=fee_cap,
            turnover_cap_fen=turnover_cap,
            headroom_fen=snapshot.values.headroom_fen,
            cash_snapshot_id=snapshot.snapshot_id,
        )
        result = search_candidates(inputs)
        terms = {
            "sector": request.sector,
            "target_max_pct": str(request.target_max_pct),
            "target_inclusive": request.target_inclusive,
            "fee_cap_fen": fee_cap,
            "turnover_cap_fen": turnover_cap,
            "checkpoint_day": checkpoint_day,
            "trade_date": trade_date.isoformat(),
            "checkpoint_on": inputs.checkpoint_on.isoformat(),
        }
        return calc_results.store(
            uow,
            customer_id=customer_id,
            kind=CalcKind.CANDIDATES,
            inputs=inputs.model_dump(mode="json"),
            basis=_basis(uow, holdings, snapshot.snapshot_id),
            output={
                "terms": terms,
                "positions": _dump(positions),
                "result": result.model_dump(mode="json"),
            },
        )

    def stress(self, customer_id: str, calc_result_id: str) -> StressView:
        """The four fixed shocks before and after the selected swap of a candidate search."""
        with self._audited(customer_id, "stress_refused", calc_result_id):

            def run(uow: UnitOfWork) -> tuple[str, StressResult]:
                search = _owned_search(uow, customer_id, calc_result_id)
                stored = _stress_result(uow, customer_id, search)
                return stored.calc_result_id, StressResult.model_validate(stored.output["result"])

            stress_id, result = self.db.run_in_uow(run, "stress")
        return StressView(
            calc_result_id=stress_id, candidates_result_id=calc_result_id, result=result
        )

    # Plan draft (SPEC §9.2, S3 §7)

    def draft_rebalance_plan(
        self,
        customer_id: str,
        calc_result_id: str,
        with_bounds: bool,
        actor: AuditActor = AuditActor.CUSTOMER,
        mode: PlanMode = PlanMode.COORDINATED,
    ) -> PlanDraftResult:
        """The selected swap as a plan: REDEEM, PURCHASE after it, CHECK_OUTCOME; and its card.

        A search without a passing swap is refused with the reason of the swap that came
        closest, so a fee over the cap is FEE_CAP before any card or approval (T42). In flat
        mode (SPEC §26) the plan holds the REDEEM and the PURCHASE only, without dependency,
        checkpoint or plan card; each order is approved on its own card.
        """
        with self._audited(customer_id, "plan_draft_refused", calc_result_id, actor):
            plan_id, nodes = self.db.run_in_uow(
                lambda uow: self._draft_plan(
                    uow, customer_id, calc_result_id, with_bounds, actor, mode
                ),
                "draft rebalance plan",
            )
        return PlanDraftResult(
            plan_id=plan_id,
            kind=PlanKind.REBALANCE,
            validation=PlanValidationResult(ok=True),
            actions=tuple(node_result(node) for node in nodes),
        )

    def _draft_plan(
        self,
        uow: UnitOfWork,
        customer_id: str,
        calc_result_id: str,
        with_bounds: bool,
        actor: AuditActor,
        mode: PlanMode,
    ) -> tuple[str, list[ActionRow]]:
        at = now(uow)
        check_not_locked(uow, customer_id, None)
        search = _owned_search(uow, customer_id, calc_result_id)
        # A plan is drafted on complete cash data only (SPEC §13.3, T35).
        snapshots.require_complete(snapshots.ensure_snapshot(uow, customer_id))
        result = CandidateSearchResult.model_validate(search.output["result"])
        selected = result.selected
        if selected is None:
            raise _no_passing_swap(result)
        assert selected.sell_product is not None and selected.buy_product is not None
        terms = search.output["terms"]
        constants = funds.plan_constants(uow, customer_id)
        sell = funds.get_product(uow, selected.sell_product)
        buy = funds.get_product(uow, selected.buy_product)
        holding = _holding_account(uow, customer_id, sell.product_id)
        debit = accounts.main_account(uow, customer_id)
        if debit is None:
            raise not_found()

        plan = plans.insert_plan(uow, customer_id, PlanKind.REBALANCE, mode)
        provenance = {"calc_result_id": {"provenance": Provenance.SERVER_RECORD.value}}
        redeem = _insert_node(
            uow,
            plan.plan_id,
            customer_id,
            ActionKind.REDEEM,
            {
                "account_id": holding,
                "product_id": sell.product_id,
                "shares": str(selected.shares_redeemed),
            },
            provenance,
        )
        purchase = _insert_node(
            uow,
            plan.plan_id,
            customer_id,
            ActionKind.PURCHASE,
            {
                "account_id": debit.account_id,
                "product_id": buy.product_id,
                "amount_fen": selected.purchase_fen,
            },
            provenance,
            depends_on=() if mode is PlanMode.FLAT else (redeem.logical_action_id,),
        )
        if mode is PlanMode.FLAT:
            audit.record(
                uow,
                actor,
                "plan_drafted",
                customer_id=customer_id,
                ref=plan.plan_id,
                decision=Tier.YELLOW.value,
            )
            return plan.plan_id, [redeem, purchase]
        checkpoint_at = datetime.combine(
            date.fromisoformat(terms["checkpoint_on"]), CHECKPOINT_TIME, tzinfo=SHANGHAI
        )
        check = _insert_node(
            uow,
            plan.plan_id,
            customer_id,
            ActionKind.CHECK_OUTCOME,
            {
                "sector": terms["sector"],
                "target_max_pct": terms["target_max_pct"],
                "target_inclusive": terms["target_inclusive"],
            },
            provenance,
            not_before=checkpoint_at,
        )
        stressed = _stress_result(uow, customer_id, search)
        bounds = constants.bounds if with_bounds else None
        today = at.astimezone(SHANGHAI).date()
        expires_at = datetime.combine(
            today + timedelta(days=constants.expiry_day + 1), time(0, 0), tzinfo=SHANGHAI
        )
        limits = {
            "fee_cap_fen": terms["fee_cap_fen"],
            "turnover_cap_fen": terms["turnover_cap_fen"],
        }
        plan_terms = {
            "scope_kind": "plan",
            "customer_id": customer_id,
            "plan_id": plan.plan_id,
            "kind": PlanKind.REBALANCE.value,
            "nodes": plan_node_terms(uow, plan.plan_id),
            "target": {
                "sector": terms["sector"],
                "target_max_pct": terms["target_max_pct"],
                "target_inclusive": terms["target_inclusive"],
                "checkpoint_at": to_db(checkpoint_at),
            },
            "limits": limits,
            "bounds": bounds.model_dump(mode="json") if bounds else None,
            "calc_result_id": search.calc_result_id,
            "stress_result_id": stressed.calc_result_id,
            "cash_snapshot_id": result.cash_snapshot_id,
            "expires_at": to_db(expires_at),
        }
        content = _plan_card(
            selected=selected,
            start_pct=result.start.upper_pct,
            sell=sell,
            buy=buy,
            redeem_due=settlement_due(at, sell.redeem_settle_days),
            checkpoint_at=checkpoint_at,
            expires_at=expires_at,
            terms=terms,
            stress_result=StressResult.model_validate(stressed.output["result"]),
            no_trade_loss=stressed.output["no_trade_loss"],
            bounds=bounds,
        )
        card = k4.open_plan_card(
            uow,
            customer_id=customer_id,
            plan_id=plan.plan_id,
            terms=plan_terms,
            content=content,
            expires_at=expires_at,
            cash_snapshot_id=result.cash_snapshot_id,
            calc_result_id=search.calc_result_id,
        )
        audit.record(
            uow,
            actor,
            "plan_drafted",
            customer_id=customer_id,
            ref=plan.plan_id,
            decision=Tier.YELLOW.value,
            reason=card.card_id,
        )
        return plan.plan_id, [redeem, purchase, check]

    @contextmanager
    def _audited(
        self, customer_id: str, event: str, ref: str, actor: AuditActor = AuditActor.CUSTOMER
    ) -> Iterator[None]:
        try:
            yield
        except WgError as error:
            if error.error_class is ErrorClass.EXPECTED:
                record_refusal(self.db, customer_id, event, ref, error, actor=actor)
            raise


# Helpers


def _engine_target(target_max_pct: Decimal, inclusive: bool) -> Decimal:
    """The inclusive target the engine compares with; "低于 40%" allows at most 39.9999 %."""
    return target_max_pct if inclusive else target_max_pct - FOUR_PLACES


def _dump(models: Sequence[Any]) -> list[Any]:
    return [m.model_dump(mode="json") for m in models]


def _holdings_basis(holdings: Sequence[HoldingsSnapshot]) -> list[dict[str, str]]:
    return [
        {"product_id": h.product_id, "source_id": h.source_id, "as_of": h.as_of.isoformat()}
        for h in holdings
    ]


def _basis(
    reader: Reader, holdings: Sequence[HoldingsSnapshot], cash_snapshot_id: str | None
) -> dict[str, Any]:
    """What a calc result was computed from (SPEC §14.7)."""
    products = funds.all_products(reader)
    return {
        "holdings": _holdings_basis(holdings),
        "taxonomy_version": sorted({h.taxonomy_version for h in holdings}),
        "nav_date": max(p.nav_date for p in products).isoformat() if products else None,
        "fee_terms": {p.product_id: p.fee_schedule.terms_version for p in products},
        "cash_snapshot_id": cash_snapshot_id,
    }


def _owned_search(reader: Reader, customer_id: str, calc_result_id: str) -> CalcResultRow:
    calc = owned(calc_results.find(reader, calc_result_id), customer_id, _owner)
    if calc.kind is not CalcKind.CANDIDATES:
        raise not_found()
    return calc


def _after_trade(
    before: Sequence[PositionValue], candidate: Candidate, products: Mapping[str, ProductRow]
) -> list[PositionValue]:
    """Fund positions once the swap has settled: shares sold at NAV, net proceeds bought."""
    after = {p.product_id: p for p in before}
    if candidate.sell_product is not None and candidate.gross_fen > 0:
        held = after[candidate.sell_product]
        after[candidate.sell_product] = held.model_copy(
            update={
                "shares": held.shares - candidate.shares_redeemed,
                "value_fen": held.value_fen - candidate.gross_fen,
            }
        )
    if candidate.buy_product is not None and candidate.purchase_fen > 0:
        product = products[candidate.buy_product]
        invested = candidate.purchase_fen - purchase_fee_fen(
            candidate.purchase_fen, product.fee_schedule
        )
        bought = shares_bought(invested, product.nav)
        held_buy = after.get(candidate.buy_product)
        shares = bought + (held_buy.shares if held_buy else Decimal(0))
        after[candidate.buy_product] = PositionValue(
            product_id=product.product_id,
            shares=shares,
            nav=product.nav,
            value_fen=quantize_fen(shares * product.nav),
        )
    return [p for p in after.values() if p.shares > 0]


def _stress_result(uow: UnitOfWork, customer_id: str, search: CalcResultRow) -> CalcResultRow:
    """Stress the selected swap of a search; stored as a calc result of its own."""
    result = CandidateSearchResult.model_validate(search.output["result"])
    selected = result.selected
    if selected is None or selected.fee_fen is None:
        raise WgError(ErrorCode.UNSUPPORTED_REQUEST, {"reason": "no_selected_candidate"})
    before = list(_search_positions(search))
    products = {p.product_id: p for p in funds.all_products(uow)}
    after = _after_trade(before, selected, products)
    stressed = stress(STRESS_SHOCKS, before, after, selected.fee_fen)
    no_trade = {row.shock: row.before_loss_fen for row in stressed.rows}
    return calc_results.store(
        uow,
        customer_id=customer_id,
        kind=CalcKind.STRESS,
        inputs={
            "candidates_result_id": search.calc_result_id,
            "shocks": _dump(STRESS_SHOCKS),
        },
        basis={**search.basis, "candidates_result_id": search.calc_result_id},
        output={"result": stressed.model_dump(mode="json"), "no_trade_loss": no_trade},
    )


def _search_positions(search: CalcResultRow) -> list[PositionValue]:
    """The positions a stored search was run on."""
    stored = search.output.get("positions")
    if stored is None:
        raise WgError(ErrorCode.DATA_INCOMPLETE, {"reason": "search_without_positions"})
    return [PositionValue.model_validate(p) for p in stored]


def _no_passing_swap(result: CandidateSearchResult) -> WgError:
    """The refusal for a search with no passing swap: the reasons of the closest swap.

    The closest is a swap that meets the target with the fewest other reasons; without one,
    any swap with the fewest reasons. Candidates come ranked, so ties keep the grid's order.
    """
    swaps = [c for c in result.candidates if c.kind is CandidateKind.SWAP]
    if not swaps:
        return WgError(ErrorCode.UNSUPPORTED_REQUEST, {"reason": "no_candidates"})
    on_target = [c for c in swaps if not _TARGET_REASONS & set(c.reasons)]
    closest = min(on_target or swaps, key=lambda c: len(c.reasons))
    codes = list(dict.fromkeys(_REASON_CODES[r] for r in closest.reasons))
    return WgError(
        codes[0],
        {
            "codes": [c.value for c in codes],
            "reasons": [r.value for r in closest.reasons],
            "sell_product": closest.sell_product,
            "buy_product": closest.buy_product,
            "gross_fen": closest.gross_fen,
            "fee_fen": closest.fee_fen,
        },
    )


def _holding_account(reader: Reader, customer_id: str, product_id: str) -> str:
    for position in funds.customer_positions(reader, customer_id):
        if position.product_id == product_id:
            return position.account_id
    raise not_found()


def _insert_node(
    uow: UnitOfWork,
    plan_id: str,
    customer_id: str,
    kind: ActionKind,
    params: dict[str, Any],
    provenance: dict[str, Any],
    *,
    depends_on: Sequence[str] = (),
    not_before: datetime | None = None,
) -> ActionRow:
    pack = pack_for(kind)
    canonical = pack.canonical(kind, params)
    at = now(uow)
    node = actions.insert_action(
        uow,
        logical_action_id=new_id(IdPrefix.ACTION),
        customer_id=customer_id,
        kind=kind,
        params=canonical,
        request_hash=pack.request_hash(kind, customer_id, canonical),
        tier=pack.tier(uow, customer_id, kind, canonical, at),
        plan_id=plan_id,
        depends_on=depends_on,
        not_before=not_before,
        provenance=provenance,
    )
    audit.record(
        uow,
        AuditActor.CUSTOMER,
        "draft_created",
        customer_id=customer_id,
        ref=node.logical_action_id,
        decision=node.tier.value,
    )
    return node


def node_result(node: ActionRow) -> DraftResult:
    """A plan node as the draft routes report it; a node exists only when it is feasible."""
    return DraftResult(
        logical_action_id=node.logical_action_id,
        revision=node.revision,
        kind=node.kind,
        status=node.status,
        tier=node.tier,
        feasible=True,
    )


def _when(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _plan_card(
    *,
    selected: Candidate,
    start_pct: Decimal,
    sell: ProductRow,
    buy: ProductRow,
    redeem_due: datetime,
    checkpoint_at: datetime,
    expires_at: datetime,
    terms: Mapping[str, Any],
    stress_result: StressResult,
    no_trade_loss: Mapping[str, int],
    bounds: PlanBounds | None,
) -> CardContent:
    """The plan approval card (S3 §7): orders, limits, target, checkpoint, expiry, stress."""
    assert selected.fee_fen is not None
    sector = SECTOR_LABELS.get(str(terms["sector"]), str(terms["sector"]))
    comparator = "≤" if terms["target_inclusive"] else "<"
    net = selected.gross_fen - (selected.fee_fen - _purchase_fee(selected, buy))
    lines = [
        CardLine(
            key="redeem",
            label="赎回",
            text=(
                f"{sell.name} {selected.shares_redeemed} 份，"
                f"按净值 {fen_text(selected.gross_fen)}，"
                f"到账 {fen_text(net)}（{_when(redeem_due)}）"
            ),
        ),
        CardLine(
            key="purchase",
            label="买入",
            text=f"{buy.name} {fen_text(selected.purchase_fen)}，赎回款到账后执行",
        ),
        CardLine(
            key="fee",
            label="交易费用",
            text=f"{fen_text(selected.fee_fen)}（上限 {fen_text(int(terms['fee_cap_fen']))}）",
        ),
        CardLine(
            key="turnover",
            label="成交额",
            text=(
                f"{fen_text(selected.turnover_fen)}"
                f"（上限 {fen_text(int(terms['turnover_cap_fen']))}）"
            ),
        ),
        CardLine(
            key="target",
            label="目标",
            text=(
                f"{sector}占比 {comparator} {terms['target_max_pct']}%，"
                f"交易后估算 {selected.checkpoint.upper_pct}%"
            ),
        ),
        CardLine(key="checkpoint", label="检查时间", text=_when(checkpoint_at)),
        CardLine(key="expires", label="授权有效至", text=f"{_when(expires_at)} 之前"),
    ]
    if bounds is not None:
        lines.append(
            CardLine(
                key="bounds",
                label="允许调整",
                text=(
                    f"新账单累计不超过 {fen_text(bounds.max_added_fen)}，先用空闲资金 "
                    f"{fen_text(bounds.spare0_fen)}；买入金额最多减少 "
                    f"{fen_text(bounds.max_retained_fen)}，"
                    f"不低于 {fen_text(bounds.min_purchase_fen)}"
                ),
            )
        )
    adverse = 0
    for row in stress_result.rows:
        before = int(no_trade_loss.get(row.shock, row.before_loss_fen))
        lines.append(
            CardLine(
                key=f"stress_{row.shock}",
                label=f"压力情景：{SHOCK_LABELS.get(row.shock, row.shock)}",
                text=f"不交易损失 {fen_text(before)}，交易后损失 {fen_text(row.after_loss_fen)}",
            )
        )
        if row.shock == "target_fund":
            adverse = row.change_fen
    warnings = [
        "这是候选网格中最小的可行调仓，不是最优交易，也不是投资建议。",
        "赎回和买入执行前都需要分别输入短信验证码。",
    ]
    if adverse > 0:
        warnings.insert(0, f"「目标基金」下跌情景中，交易后损失比不交易多 {fen_text(adverse)}。")
    return CardContent(
        title=f"调仓计划：{sector}占比降至 {terms['target_max_pct']}% 以内",
        lines=tuple(lines),
        changes=(
            PercentChange(
                key="sector_share",
                label=f"{sector}占比",
                before=start_pct,
                after=selected.checkpoint.upper_pct,
                quality=PercentQuality.ESTIMATED,
            ),
        ),
        warnings=tuple(warnings),
    )


def _purchase_fee(candidate: Candidate, buy: ProductRow) -> int:
    return purchase_fee_fen(candidate.purchase_fen, buy.fee_schedule)
