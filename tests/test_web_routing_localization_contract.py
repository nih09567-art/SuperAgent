from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "web" / "app.js"


def test_routing_decision_reasons_are_localized_without_renaming_agents():
    source = APP_JS.read_text(encoding="utf-8")

    for code, label in {
        "AUTHORIZED": "当前用户已授权",
        "AGENT_ONLINE": "Agent 在线",
        "COMPOSITE_SUBTASK_ACTION": "可承担复合任务中的子步骤",
        "INTENT_MATCH": "意图匹配",
        "CAPABILITY_MATCH": "能力匹配",
        "SCENARIO_MATCH": "场景匹配",
    }.items():
        assert f'{code}: "{label}"' in source

    assert ".map(localizeRoutingReasonCode).join" in source
    assert "escapeHtml(item.agent_id || \"-\")" in source


def test_routing_decision_and_exclusion_codes_use_the_same_localizer():
    source = APP_JS.read_text(encoding="utf-8")

    assert 'DISPATCH: "已分派"' in source
    assert "localizeRoutingDecision(decision)" in source
    assert "localizeRoutingReasonCode(item.reason_code)" in source
    assert "localizeRoutingReasonCode(item)" in source
