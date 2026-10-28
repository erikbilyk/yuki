"""Prefixed opaque identifiers (SPEC §5.2) and one constrained string type per prefix."""

from __future__ import annotations

import secrets
from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints


class IdPrefix(StrEnum):
    CUSTOMER = "cus_"
    SESSION = "ses_"
    OPERATOR = "opr_"
    ACCOUNT = "acc_"
    PAYEE = "pay_"
    PLAN = "pln_"
    ACTION = "act_"
    APPROVAL = "apr_"
    CHALLENGE = "chl_"
    OPERATION = "opn_"
    RESERVATION = "rsv_"
    RECEIPT = "rcp_"
    EVENT = "evt_"
    CASH_SNAPSHOT = "snp_"
    CALC_RESULT = "clc_"
    COMMITMENT = "cmt_"
    LOCK = "lck_"
    PURPOSE = "pur_"
    STATEMENT = "stm_"
    LOT = "lot_"
    FEE_SCHEDULE = "fee_"
    MANDATE = "mdt_"
    DEBIT_REQUEST = "req_"
    MERCHANT = "mer_"
    CARD = "crd_"


def new_id(prefix: IdPrefix) -> str:
    """A fresh identifier: the prefix followed by 16 random hex digits."""
    return f"{IdPrefix(prefix).value}{secrets.token_hex(8)}"


def _prefixed(prefix: IdPrefix) -> StringConstraints:
    return StringConstraints(strict=True, pattern=rf"^{prefix.value}[a-z0-9_]{{1,64}}$")


CustomerId = Annotated[str, _prefixed(IdPrefix.CUSTOMER)]
SessionId = Annotated[str, _prefixed(IdPrefix.SESSION)]
OperatorId = Annotated[str, _prefixed(IdPrefix.OPERATOR)]
AccountId = Annotated[str, _prefixed(IdPrefix.ACCOUNT)]
PayeeId = Annotated[str, _prefixed(IdPrefix.PAYEE)]
PlanId = Annotated[str, _prefixed(IdPrefix.PLAN)]
LogicalActionId = Annotated[str, _prefixed(IdPrefix.ACTION)]
ApprovalId = Annotated[str, _prefixed(IdPrefix.APPROVAL)]
ChallengeId = Annotated[str, _prefixed(IdPrefix.CHALLENGE)]
OperationId = Annotated[str, _prefixed(IdPrefix.OPERATION)]
ReservationId = Annotated[str, _prefixed(IdPrefix.RESERVATION)]
ReceiptId = Annotated[str, _prefixed(IdPrefix.RECEIPT)]
EventId = Annotated[str, _prefixed(IdPrefix.EVENT)]
CashSnapshotId = Annotated[str, _prefixed(IdPrefix.CASH_SNAPSHOT)]
CalcResultId = Annotated[str, _prefixed(IdPrefix.CALC_RESULT)]
CommitmentId = Annotated[str, _prefixed(IdPrefix.COMMITMENT)]
LockId = Annotated[str, _prefixed(IdPrefix.LOCK)]
PurposeId = Annotated[str, _prefixed(IdPrefix.PURPOSE)]
StatementId = Annotated[str, _prefixed(IdPrefix.STATEMENT)]
LotId = Annotated[str, _prefixed(IdPrefix.LOT)]
FeeScheduleId = Annotated[str, _prefixed(IdPrefix.FEE_SCHEDULE)]
MandateRef = Annotated[str, _prefixed(IdPrefix.MANDATE)]
DebitRequestId = Annotated[str, _prefixed(IdPrefix.DEBIT_REQUEST)]
MerchantId = Annotated[str, _prefixed(IdPrefix.MERCHANT)]
CardId = Annotated[str, _prefixed(IdPrefix.CARD)]

# Fund codes are six-digit strings and keep their leading zeros ("010737").
FundCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{6}$")]
