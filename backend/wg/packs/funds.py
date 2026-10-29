"""REDEEM and PURCHASE of funds, and the CHECK_OUTCOME node of a rebalance plan (SPEC §14, §15).

Prices are the simulator's: the NAV is known before the order, so the redemption fee and the
shares bought are fixed when the order is accepted. A redemption settles on its fund's
calendar (`redeem_settle_days`, T+3 09:00 in the fixture); a purchase with no settle days
settles in the unit of work that submits it, otherwise at 09:00 of its due day. Positions
and lots change only at settlement.

A purchase is a discretionary debit, so it must fit in the headroom recomputed from current
rows; it is paid from settled cash only, so it is refused while a redemption of its plan has
not settled (UNSETTLED_FUNDS). The plan's fee cap and turnover cap count every fund order
of the plan, cumulatively.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from wg.contracts.api import CardLine
from wg.contracts.enums import ActionKind, ActionStatus, Tier
from wg.contracts.errors import ErrorCode
from wg.contracts.money import quantize_fen
from wg.core import k3_policy
from wg.core.hashes import sha256_hex
from wg.core.k4_approval import CardContent
from wg.k6 import accounts, approvals, bank, funds, operations, plans
from wg.k6.actions import ActionRow
from wg.k6.bank import BankRequest, FundOrder, FundSide
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Reader, UnitOfWork
from wg.k6.funds import FOUR_PLACES, ProductRow, shares_text
from wg.k6.operations import OperationRow, ReservationRow
from wg.k6.simtime import SHANGHAI, to_db
from wg.numeric import compute_snapshot, redemption_fee
from wg.numeric.fees import purchase_fee_fen
from wg.packs.base import Infeasible, OperationTerms, fen_text

SETTLEMENT_TIME = time(9, 0)

_PARAMS: Mapping[ActionKind, tuple[str, ...]] = {
    ActionKind.REDEEM: ("account_id", "product_id", "shares"),
    ActionKind.PURCHASE: ("account_id", "product_id", "amount_fen"),
}


def settlement_due(submitted_at: datetime, settle_days: int) -> datetime:
    """`max(submitted_at, date_cst(submitted_at) + settle_days at 09:00 CST)` (SPEC §15.3)."""
    local = submitted_at.astimezone(SHANGHAI)
    due = datetime.combine(
        local.date() + timedelta(days=settle_days), SETTLEMENT_TIME, tzinfo=SHANGHAI
    )
    return max(local, due)


def shares_bought(invested_fen: int, nav: Decimal) -> Decimal:
    """Shares for an invested amount: amount / NAV, half-up to four places."""
    return (Decimal(invested_fen) / 100 / nav).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def parse_shares(value: Any) -> Decimal:
    """A positive share count with at most four decimals, from its stored text."""
    try:
        shares = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"shares {value!r} is not a decimal") from exc
    if not shares.is_finite() or shares <= 0 or shares != shares.quantize(FOUR_PLACES):
        raise ValueError(f"shares {value!r} is not a positive 4-place decimal")
    return shares


class FundsPack:
    kinds = frozenset(_PARAMS)

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        required = _PARAMS[ActionKind(kind)]
        unknown = set(params) - set(required)
        missing = [name for name in required if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        result: dict[str, Any] = {
            "account_id": str(params["account_id"]),
            "product_id": str(params["product_id"]),
        }
        if ActionKind(kind) is ActionKind.REDEEM:
            result["shares"] = shares_text(parse_shares(params["shares"]))
        else:
            amount = params["amount_fen"]
            if type(amount) is not int or amount <= 0:
                raise ValueError("amount_fen is a positive integer")
            result["amount_fen"] = amount
        return result

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        business: dict[str, Any] = {
            "kind": ActionKind(kind).value,
            "customer_id": customer_id,
            "account_id": params["account_id"],
            "counterparty": params["product_id"],
        }
        if "shares" in params:
            business["shares"] = params["shares"]
        else:
            business["amount_fen"] = params["amount_fen"]
        return sha256_hex(business)

    def tier(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> Tier:
        return k3_policy.tier_for(kind)

    def precheck(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
        *,
        plan_id: str | None = None,
    ) -> tuple[Infeasible, ...]:
        """Free shares or settled cash and headroom, minimum, then the plan's caps."""
        product = funds.get_product(reader, str(params["product_id"]))
        account = accounts.find_account(reader, str(params["account_id"]))
        if account is None or account.customer_id != customer_id:
            raise ValueError(f"account {params['account_id']} is not the customer's")
        if ActionKind(kind) is ActionKind.REDEEM:
            reasons, gross, fee = _redemption_checks(
                reader, product, account.account_id, params, at
            )
        else:
            reasons, gross, fee = _purchase_checks(
                reader, customer_id, product, account, params, at
            )
            if plan_id is not None:
                reasons.extend(_unsettled_redemptions(reader, plan_id))
        if plan_id is not None and fee is not None:
            reasons.extend(_plan_limits(reader, plan_id, gross, fee))
        return tuple(reasons)

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        product = funds.get_product(reader, str(action.params["product_id"]))
        if action.kind is ActionKind.REDEEM:
            shares = Decimal(str(action.params["shares"]))
            due = settlement_due(at, product.redeem_settle_days)
            lines = (
                CardLine(key="order", label="赎回", text=f"{product.name} {shares} 份"),
                CardLine(key="gross", label="按净值估算", text=fen_text(_gross(shares, product))),
                CardLine(key="settles", label="到账时间", text=_when(due)),
            )
            title = f"赎回{product.name}"
        else:
            amount = int(action.params["amount_fen"])
            due = settlement_due(at, product.purchase_settle_days)
            lines = (
                CardLine(key="order", label="买入", text=f"{product.name} {fen_text(amount)}"),
                CardLine(key="settles", label="确认时间", text=_when(due)),
            )
            title = f"买入{product.name}"
        return CardContent(title=title, lines=lines, warnings=("确认后需要输入短信验证码。",))

    def operation_terms(self, reader: Reader, action: ActionRow, at: datetime) -> OperationTerms:
        product = funds.get_product(reader, str(action.params["product_id"]))
        if action.kind is ActionKind.REDEEM:
            shares = Decimal(str(action.params["shares"]))
            due = settlement_due(at, product.redeem_settle_days)
            return OperationTerms(
                account_id=str(action.params["account_id"]),
                amount_fen=_gross(shares, product),
                shares=shares,
                product_id=product.product_id,
                settle_due_at=due,
                settles_at_once=due <= at,
            )
        amount = int(action.params["amount_fen"])
        invested = amount - purchase_fee_fen(amount, product.fee_schedule)
        due = settlement_due(at, product.purchase_settle_days)
        return OperationTerms(
            account_id=str(action.params["account_id"]),
            amount_fen=amount,
            shares=shares_bought(invested, product.nav),
            product_id=product.product_id,
            settle_due_at=due,
            settles_at_once=due <= at,
        )

    def reserve(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms
    ) -> ReservationRow | None:
        """A purchase holds its amount; a redemption debits no money and holds none."""
        if operation.action_kind is ActionKind.REDEEM:
            return None
        assert terms.amount_fen is not None
        return operations.insert_reservation(
            uow,
            operation_id=operation.operation_id,
            account_id=terms.account_id,
            amount_fen=terms.amount_fen,
        )

    def apply(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms, at: datetime
    ) -> BankRequest:
        assert terms.product_id is not None and terms.shares is not None
        assert terms.amount_fen is not None
        product = funds.get_product(uow, terms.product_id)
        if operation.action_kind is ActionKind.REDEEM:
            quote = redemption_fee(
                funds.lots_of(uow, terms.account_id, product.product_id),
                terms.shares,
                product.nav,
                product.fee_schedule,
                at.astimezone(SHANGHAI).date(),
            )
            assert quote.fee_fen is not None and quote.net_fen is not None
            side, gross, fee, cash = FundSide.REDEEM, quote.gross_fen, quote.fee_fen, quote.net_fen
        else:
            fee = purchase_fee_fen(terms.amount_fen, product.fee_schedule)
            side, gross, cash = FundSide.PURCHASE, terms.amount_fen, -terms.amount_fen
        order = FundOrder(
            key=operation.idempotency_key,
            operation_id=operation.operation_id,
            account_id=terms.account_id,
            product_id=product.product_id,
            side=side,
            shares=terms.shares,
            nav=product.nav,
            gross_fen=gross,
            fee_fen=fee,
            cash_fen=cash,
            settle_due_at=terms.settle_due_at,
        )
        return bank.submit_fund_order(uow, order, at, settle_now=terms.settles_at_once)

    def settle(self, uow: UnitOfWork, operation: OperationRow, at: datetime) -> BankRequest:
        return bank.settle_fund_order(uow, operation.idempotency_key, at)

    def on_settled(self, uow: UnitOfWork, operation: OperationRow) -> dict[str, Any]:
        """Shares leave or join the position, with its lots (FIFO on the way out)."""
        assert operation.account_id is not None and operation.product_id is not None
        assert operation.shares is not None and operation.settled_at is not None
        account_id, product_id, shares = (
            operation.account_id,
            operation.product_id,
            operation.shares,
        )
        if operation.action_kind is ActionKind.REDEEM:
            funds.check_lots_match(uow, account_id, product_id)
            position = funds.add_shares(uow, account_id, product_id, -shares)
            funds.draw_lots(uow, account_id, product_id, shares)
        else:
            before = funds.find_position(uow, account_id, product_id)
            lots_known = (
                before is None
                or before.shares == 0
                or bool(funds.lots_of(uow, account_id, product_id))
            )
            position = funds.add_shares(uow, account_id, product_id, shares)
            if lots_known:
                request = bank.find_request(uow, operation.idempotency_key)
                assert request is not None
                funds.insert_lot(
                    uow,
                    account_id=account_id,
                    product_id=product_id,
                    shares=shares,
                    acquired_on=operation.settled_at.astimezone(SHANGHAI).date(),
                    cost_fen=int(request.effect["gross_fen"]) - int(request.effect["fee_fen"]),
                )
        return {"position_shares_after": shares_text(position.shares)}

    def receipt_effect(self, operation: OperationRow, bank_request: BankRequest) -> dict[str, Any]:
        effect = bank_request.effect
        gross, fee = int(effect["gross_fen"]), int(effect["fee_fen"])
        settled_at = bank_request.settled_at
        return {
            "account_id": operation.account_id,
            "product_id": operation.product_id,
            "shares": effect["shares"],
            "nav": effect["nav"],
            "gross_fen": gross,
            "fee_fen": fee,
            "net_fen": gross - fee,
            "settled_at": to_db(settled_at) if settled_at else None,
            "reason": effect.get("reason"),
        }


class CheckpointPack:
    """CHECK_OUTCOME: the plan's target measured on settled positions at its instant.

    It moves no money and needs no approval of its own; the coordinator evaluates it.
    """

    kinds = frozenset({ActionKind.CHECK_OUTCOME})

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        required = ("sector", "target_max_pct", "target_inclusive")
        unknown = set(params) - set(required)
        missing = [name for name in required if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        target = Decimal(str(params["target_max_pct"]))
        if not target.is_finite() or target < 0 or target > 100:
            raise ValueError("target_max_pct is a percent")
        if type(params["target_inclusive"]) is not bool:
            raise ValueError("target_inclusive is a boolean")
        return {
            "sector": str(params["sector"]),
            "target_max_pct": str(target.quantize(FOUR_PLACES)),
            "target_inclusive": params["target_inclusive"],
        }

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        return sha256_hex({"kind": ActionKind(kind).value, "customer_id": customer_id, **params})

    def tier(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> Tier:
        return k3_policy.tier_for(kind)

    def precheck(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
        *,
        plan_id: str | None = None,
    ) -> tuple[Infeasible, ...]:
        return ()

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        comparator = "≤" if action.params["target_inclusive"] else "<"
        target = f"{action.params['sector']} {comparator} {action.params['target_max_pct']}%"
        return CardContent(
            title="检查目标",
            lines=(CardLine(key="target", label="目标", text=target),),
        )


def _gross(shares: Decimal, product: ProductRow) -> int:
    return quantize_fen(shares * product.nav)


def _when(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _redemption_checks(
    reader: Reader,
    product: ProductRow,
    account_id: str,
    params: Mapping[str, Any],
    at: datetime,
) -> tuple[list[Infeasible], int, int | None]:
    """Free shares and a verified fee; returns the reasons, the gross and the fee."""
    funds.check_lots_match(reader, account_id, product.product_id)
    shares = Decimal(str(params["shares"]))
    position = funds.find_position(reader, account_id, product.product_id)
    held = position.shares if position is not None else Decimal(0)
    free = held - funds.pending_redemption_shares(reader, account_id, product.product_id)
    reasons: list[Infeasible] = []
    if shares > free:
        reasons.append(
            Infeasible(
                ErrorCode.INSUFFICIENT_FUNDS, {"free_shares": shares_text(max(free, Decimal(0)))}
            )
        )
        return reasons, _gross(shares, product), None
    quote = redemption_fee(
        funds.lots_of(reader, account_id, product.product_id),
        shares,
        product.nav,
        product.fee_schedule,
        at.astimezone(SHANGHAI).date(),
    )
    if quote.fee_fen is None:
        # An unknown fee blocks an executable order; it is never taken as zero (SPEC §14.4).
        reasons.append(Infeasible(ErrorCode.DATA_INCOMPLETE, {"reason": "fee_unverified"}))
    return reasons, quote.gross_fen, quote.fee_fen


def _purchase_checks(
    reader: Reader,
    customer_id: str,
    product: ProductRow,
    account: accounts.AccountRow,
    params: Mapping[str, Any],
    at: datetime,
) -> tuple[list[Infeasible], int, int]:
    """Minimum, posted money on the account, headroom; returns the reasons, gross and fee."""
    amount = int(params["amount_fen"])
    reasons: list[Infeasible] = []
    if amount < product.min_purchase_fen:
        reasons.append(
            Infeasible(ErrorCode.PURCHASE_BELOW_MIN, {"min_purchase_fen": product.min_purchase_fen})
        )
    available = account.balance_fen - operations.reserved_fen(reader, account.account_id)
    if amount > available:
        reasons.append(
            Infeasible(
                ErrorCode.INSUFFICIENT_FUNDS,
                {"available_fen": available, "shortfall_fen": amount - available},
            )
        )
    if account.covered:
        snapshot = compute_snapshot(cash_inputs_from_db(reader, customer_id, at))
        if amount > snapshot.headroom_fen:
            reasons.append(
                Infeasible(
                    ErrorCode.INSUFFICIENT_HEADROOM,
                    {
                        "headroom_fen": snapshot.headroom_fen,
                        "over_headroom_fen": amount - snapshot.headroom_fen,
                    },
                )
            )
    return reasons, amount, purchase_fee_fen(amount, product.fee_schedule)


def _unsettled_redemptions(reader: Reader, plan_id: str) -> list[Infeasible]:
    """Expected proceeds never fund a purchase: every redemption of the plan must be SETTLED."""
    pending = [
        node.logical_action_id
        for node in plans.nodes(reader, plan_id)
        if node.kind is ActionKind.REDEEM and node.status is not ActionStatus.SETTLED
    ]
    if not pending:
        return []
    return [Infeasible(ErrorCode.UNSETTLED_FUNDS, {"pending": list(pending)})]


def plan_limits(reader: Reader, plan_id: str) -> Mapping[str, Any] | None:
    """The fee and turnover caps the customer approved for the plan, if it is approved."""
    plan = plans.find_plan(reader, plan_id)
    if plan is None or plan.root_approval_id is None:
        return None
    approval = approvals.find_approval(reader, plan.root_approval_id)
    if approval is None:
        return None
    limits = approval.terms.get("limits")
    return limits if isinstance(limits, Mapping) else None


def _plan_limits(reader: Reader, plan_id: str, gross_fen: int, fee_fen: int) -> list[Infeasible]:
    limits = plan_limits(reader, plan_id)
    if limits is None:
        return []
    trades = funds.plan_trades(reader, plan_id)
    fees = sum(fee for _, fee in trades) + fee_fen
    turnover = sum(gross for gross, _ in trades) + gross_fen
    reasons = []
    if fees > int(limits["fee_cap_fen"]):
        reasons.append(
            Infeasible(ErrorCode.FEE_CAP, {"fee_fen": fees, "fee_cap_fen": limits["fee_cap_fen"]})
        )
    if turnover > int(limits["turnover_cap_fen"]):
        reasons.append(
            Infeasible(
                ErrorCode.TURNOVER_CAP,
                {"turnover_fen": turnover, "turnover_cap_fen": limits["turnover_cap_fen"]},
            )
        )
    return reasons
