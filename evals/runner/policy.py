"""The customer policy: approve exactly what the scenario allows, never anything else."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from evals.runner.schema import Allowed, Withhold

PLAN = "PLAN"


def _same_shares(left: Any, right: str) -> bool:
    try:
        return Decimal(str(left)) == Decimal(right)
    except InvalidOperation:
        return False


def action_allowed(kind: str, params: dict[str, Any], entry: Allowed) -> bool:
    """Whether an action of this kind and these params is what `entry` allows."""
    if entry.kind != kind:
        return False
    amount = params.get("amount_fen")
    if amount is None:
        if entry.amount_fen is not None or entry.max_amount_fen is not None:
            return False
    elif entry.amount_fen is not None:
        if amount != entry.amount_fen:
            return False
    elif entry.max_amount_fen is None or amount > entry.max_amount_fen:
        return False
    if entry.shares is not None and not _same_shares(params.get("shares"), entry.shares):
        return False
    if entry.payee_id is not None and params.get("payee_id") != entry.payee_id:
        return False
    return entry.product_id is None or params.get("product_id") == entry.product_id


def _nodes_allowed(nodes: list[dict[str, Any]], entries: tuple[Allowed, ...]) -> bool:
    return bool(nodes) and all(
        any(action_allowed(node["kind"], node.get("params", {}), entry) for entry in entries)
        for node in nodes
    )


def card_allowed(terms: dict[str, Any], allow: list[Allowed]) -> bool:
    """A plan card needs a PLAN entry covering every node; an action card needs a kind entry."""
    if terms.get("scope_kind") == "plan":
        return any(
            entry.kind == PLAN and _nodes_allowed(terms.get("nodes", []), entry.nodes or ())
            for entry in allow
        )
    return any(
        entry.kind != PLAN and action_allowed(terms["kind"], terms.get("params", {}), entry)
        for entry in allow
    )


def code_allowed(kind: str, params: dict[str, Any], allow: list[Allowed]) -> bool:
    """A code is entered for an allowed action, alone or as a node of an allowed plan."""
    for entry in allow:
        if entry.kind == PLAN:
            if any(action_allowed(kind, params, node) for node in entry.nodes or ()):
                return True
        elif action_allowed(kind, params, entry):
            return True
    return False


def code_withheld(kind: str, params: dict[str, Any], withhold: tuple[Withhold, ...]) -> bool:
    """Whether a `withhold` entry names this action; a field the entry omits matches anything."""
    return any(
        entry.kind == kind
        and (entry.amount_fen is None or params.get("amount_fen") == entry.amount_fen)
        and (entry.shares is None or _same_shares(params.get("shares"), entry.shares))
        and (entry.payee_id is None or params.get("payee_id") == entry.payee_id)
        and (entry.product_id is None or params.get("product_id") == entry.product_id)
        for entry in withhold
    )


def describe_terms(terms: dict[str, Any]) -> dict[str, Any]:
    """The part of a card's terms worth keeping in the record."""
    if terms.get("scope_kind") == "plan":
        return {
            "scope": "plan",
            "plan_kind": terms.get("kind"),
            "nodes": [
                {"kind": node["kind"], "params": node.get("params", {})}
                for node in terms.get("nodes", [])
            ],
        }
    return {"scope": "action", "kind": terms.get("kind"), "params": terms.get("params", {})}
