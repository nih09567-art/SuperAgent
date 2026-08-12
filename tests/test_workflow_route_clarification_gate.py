from src.workflow.process import _enforce_profile_clarification_gate


def test_missing_business_key_cannot_be_overridden_by_contract_closure():
    route = {
        "decision": "DISPATCH",
        "selected_agent": "RemoteHRAssistantAgent",
        "reason_codes": ["CONTRACT_OUTPUT_CLOSURE"],
    }

    blocked = _enforce_profile_clarification_gate(
        {
            "missing_fields": ["employee_or_criteria"],
            "needs_clarification": True,
        },
        route,
    )

    assert blocked is True
    assert route["decision"] == "CLARIFY"
    assert route["selected_agent"] is None
    assert route["reason_codes"] == ["MISSING_REQUIRED_FIELDS"]


def test_complete_profile_keeps_contract_closure_dispatchable():
    route = {
        "decision": "DISPATCH",
        "selected_agent": "RemoteHRAssistantAgent",
        "reason_codes": ["CONTRACT_OUTPUT_CLOSURE"],
    }

    blocked = _enforce_profile_clarification_gate(
        {"missing_fields": [], "needs_clarification": False},
        route,
    )

    assert blocked is False
    assert route["decision"] == "DISPATCH"
    assert route["selected_agent"] == "RemoteHRAssistantAgent"
