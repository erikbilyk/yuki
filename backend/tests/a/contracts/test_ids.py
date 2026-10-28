import pytest
from pydantic import TypeAdapter, ValidationError

from wg.contracts import ids
from wg.contracts.ids import ApprovalId, FundCode, IdPrefix, MandateRef, new_id

SPEC_PREFIXES = set(
    "cus_ ses_ opr_ acc_ pay_ pln_ act_ apr_ chl_ opn_ rsv_ rcp_ evt_ snp_ clc_ cmt_ lck_ "
    "mdt_ req_ mer_".split()
)
FIXTURE_PREFIXES = set("pur_ stm_ lot_ fee_".split())

TYPE_BY_PREFIX = {
    IdPrefix.CUSTOMER: ids.CustomerId,
    IdPrefix.SESSION: ids.SessionId,
    IdPrefix.OPERATOR: ids.OperatorId,
    IdPrefix.ACCOUNT: ids.AccountId,
    IdPrefix.PAYEE: ids.PayeeId,
    IdPrefix.PLAN: ids.PlanId,
    IdPrefix.ACTION: ids.LogicalActionId,
    IdPrefix.APPROVAL: ids.ApprovalId,
    IdPrefix.CHALLENGE: ids.ChallengeId,
    IdPrefix.OPERATION: ids.OperationId,
    IdPrefix.RESERVATION: ids.ReservationId,
    IdPrefix.RECEIPT: ids.ReceiptId,
    IdPrefix.EVENT: ids.EventId,
    IdPrefix.CASH_SNAPSHOT: ids.CashSnapshotId,
    IdPrefix.CALC_RESULT: ids.CalcResultId,
    IdPrefix.COMMITMENT: ids.CommitmentId,
    IdPrefix.LOCK: ids.LockId,
    IdPrefix.PURPOSE: ids.PurposeId,
    IdPrefix.STATEMENT: ids.StatementId,
    IdPrefix.LOT: ids.LotId,
    IdPrefix.FEE_SCHEDULE: ids.FeeScheduleId,
    IdPrefix.MANDATE: ids.MandateRef,
    IdPrefix.DEBIT_REQUEST: ids.DebitRequestId,
    IdPrefix.MERCHANT: ids.MerchantId,
}


def test_prefixes_cover_the_spec():
    assert {prefix.value for prefix in IdPrefix} == SPEC_PREFIXES | FIXTURE_PREFIXES
    assert set(TYPE_BY_PREFIX) == set(IdPrefix)


@pytest.mark.parametrize("prefix", list(IdPrefix))
def test_new_id_validates_only_as_its_own_type(prefix):
    value = new_id(prefix)
    assert value.startswith(prefix.value)
    assert TypeAdapter(TYPE_BY_PREFIX[prefix]).validate_python(value) == value
    for other, id_type in TYPE_BY_PREFIX.items():
        if other is not prefix:
            with pytest.raises(ValidationError):
                TypeAdapter(id_type).validate_python(value)


def test_new_id_is_unique():
    assert len({new_id(IdPrefix.OPERATION) for _ in range(1000)}) == 1000


def test_mandate_ref_and_approval_id_never_cross_validate():
    mandate = new_id(IdPrefix.MANDATE)
    approval = new_id(IdPrefix.APPROVAL)
    assert TypeAdapter(MandateRef).validate_python(mandate) == mandate
    assert TypeAdapter(ApprovalId).validate_python(approval) == approval
    with pytest.raises(ValidationError):
        TypeAdapter(MandateRef).validate_python(approval)
    with pytest.raises(ValidationError):
        TypeAdapter(ApprovalId).validate_python(mandate)


@pytest.mark.parametrize("value", ["apr_", "APR_x", "apr_X1", "apr-1", "xapr_1", " apr_1", 1])
def test_malformed_ids_are_rejected(value):
    with pytest.raises(ValidationError):
        TypeAdapter(ApprovalId).validate_python(value)


def test_fund_codes_keep_leading_zeros():
    assert TypeAdapter(FundCode).validate_python("010737") == "010737"
    for value in (10737, "10737", "0107370", "01073A"):
        with pytest.raises(ValidationError):
            TypeAdapter(FundCode).validate_python(value)
