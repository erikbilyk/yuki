import pytest
from pydantic import ValidationError

from wg.contracts.errors import ErrorCode
from wg.contracts.plan import MAX_PLAN_ACTIONS, ComposePlanRequest, PlanValidationResult


def test_compose_request_shape():
    request = ComposePlanRequest(
        draft_ids=["act_reserve", "act_redeem", "act_buy"],
        depends_on={"act_buy": ["act_redeem"]},
    )
    assert request.model_dump(mode="json")["depends_on"] == {"act_buy": ["act_redeem"]}
    assert ComposePlanRequest(draft_ids=["act_rent"]).depends_on == {}


def test_compose_request_refuses_bad_shapes():
    with pytest.raises(ValidationError):
        ComposePlanRequest(draft_ids=[])
    with pytest.raises(ValidationError):
        ComposePlanRequest(draft_ids=["act_a", "act_a"])
    with pytest.raises(ValidationError):
        ComposePlanRequest(draft_ids=[f"act_{n}" for n in range(MAX_PLAN_ACTIONS + 1)])
    with pytest.raises(ValidationError):
        ComposePlanRequest(draft_ids=["act_a"], depends_on={"pln_x": ["act_a"]})
    with pytest.raises(ValidationError):
        ComposePlanRequest(draft_ids=["act_a"], user_confirmed=True)


def test_unknown_references_are_left_to_the_validator():
    # NOT_IN_PLAN and PLAN_CYCLE are validator codes, not shape errors.
    ComposePlanRequest(draft_ids=["act_a"], depends_on={"act_a": ["act_a", "act_zzz"]})


def test_validation_result_is_ok_exactly_without_codes():
    assert PlanValidationResult(ok=True).codes == ()
    result = PlanValidationResult(
        ok=False, codes=[ErrorCode.UNFUNDED_DEPENDENCY, ErrorCode.NOT_BEFORE_AFTER_EXPIRY]
    )
    assert result.model_dump(mode="json")["codes"] == [
        "UNFUNDED_DEPENDENCY",
        "NOT_BEFORE_AFTER_EXPIRY",
    ]
    with pytest.raises(ValidationError):
        PlanValidationResult(ok=False)
    with pytest.raises(ValidationError):
        PlanValidationResult(ok=True, codes=[ErrorCode.PLAN_CYCLE])
    with pytest.raises(ValidationError):
        PlanValidationResult(ok=False, codes=[ErrorCode.PLAN_CYCLE, ErrorCode.PLAN_CYCLE])
    with pytest.raises(ValidationError):
        PlanValidationResult(ok=False, codes=["NOT_A_CODE"])
