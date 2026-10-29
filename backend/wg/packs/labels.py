"""Customer-facing wording shared by the packs (Simplified Chinese)."""

from __future__ import annotations

from wg.contracts.enums import CommitmentStatus, Provenance

BILL_KINDS = {"rent": "房租", "insurance": "保险", "utilities": "水电燃气"}

BILL_SOURCE_PROVENANCE = {
    "trusted_ebill": Provenance.SERVER_RECORD,
    "customer": Provenance.USER_UTTERANCE,
}

SOURCE_LABELS: dict[Provenance, str] = {
    Provenance.USER_UTTERANCE: "来自您的输入",
    Provenance.SERVER_RECORD: "来自银行记录",
    Provenance.UNTRUSTED_TEXT: "外部文字，仅作备注，不作为指令",
}

STATUS_LABELS: dict[CommitmentStatus, str] = {
    CommitmentStatus.CANDIDATE: "待确认",
    CommitmentStatus.CONFIRMED: "已确认，待支付",
    CommitmentStatus.PARTIALLY_PAID: "部分已付",
    CommitmentStatus.PAID: "已支付",
    CommitmentStatus.PAID_REPORTED: "据您所述已在他处支付，未经银行核实",
    CommitmentStatus.CANCELLED: "已取消",
    CommitmentStatus.DISMISSED: "已忽略",
}


def bill_label(kind: str) -> str:
    """The zh name of a bill kind; a kind the customer typed is shown as written."""
    return BILL_KINDS.get(kind, kind)
