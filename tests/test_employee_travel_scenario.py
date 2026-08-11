from pathlib import Path

from src.service.employee_travel import EmployeeTravelService, handle_travel_turn


def _complete_draft() -> dict[str, str]:
    return {
        "employee_name": "李娜",
        "origin": "上海",
        "destination": "北京",
        "start_date": "2026-08-12",
        "end_date": "2026-08-14",
        "purpose": "客户拜访",
        "project_or_cost_center": "CC102",
        "transport_preference": "高铁",
        "accommodation_standard": "500元/晚",
    }


def test_travel_dialogue_collects_missing_fields_one_at_a_time(tmp_path: Path):
    service = EmployeeTravelService(tmp_path / "travel.json")
    response = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message="帮李娜办差旅",
    )

    assert response["kind"] == "clarification"
    assert response["missing_field"] == "origin"
    assert response["state"]["draft"]["employee_name"] == "李娜"

    answers = (
        ("上海", "destination"),
        ("北京", "start_date"),
        ("2026-08-12", "end_date"),
        ("2026-08-14", "purpose"),
        ("客户拜访", "project_or_cost_center"),
        ("CC102", "transport_preference"),
        ("高铁", "accommodation_standard"),
    )
    for answer, next_field in answers:
        response = handle_travel_turn(
            service,
            user_id="user-a",
            conversation_id="conversation-a",
            message=answer,
            state=response["state"],
        )
        assert response["missing_field"] == next_field

    response = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message="500元/晚",
        state=response["state"],
    )
    assert response["kind"] == "plan"
    assert response["budget"]["transport"] == 800.0
    assert response["budget"]["accommodation"] == 1000.0
    assert response["budget"]["allowance"] == 300.0
    assert response["budget"]["total"] == 2100.0
    assert response["budget"]["trip_days"] == 3
    assert response["budget"]["nights"] == 2
    assert "路线约" in response["budget"]["note"]
    assert not (tmp_path / "travel.json").exists()


def test_confirmation_is_required_and_idempotent(tmp_path: Path):
    storage = tmp_path / "travel.json"
    service = EmployeeTravelService(storage)
    draft = _complete_draft()

    assert service.query(user_id="user-a") == []
    record, created = service.create(
        user_id="user-a",
        draft=draft,
        plan_id="plan-one",
    )
    duplicate, duplicate_created = service.create(
        user_id="user-a",
        draft=draft,
        plan_id="plan-one",
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate["request_id"] == record["request_id"]
    assert len(service.query(user_id="user-a")) == 1


def test_query_update_and_cancel_are_scoped_to_the_user(tmp_path: Path):
    service = EmployeeTravelService(tmp_path / "travel.json")
    record, _ = service.create(
        user_id="user-a",
        draft=_complete_draft(),
        plan_id="plan-one",
    )
    service.create(
        user_id="user-b",
        draft={**_complete_draft(), "employee_name": "王强"},
        plan_id="plan-two",
    )

    assert [item["request_id"] for item in service.query(
        user_id="user-a",
        employee_name="李娜",
        destination="北京",
        travel_date="2026-08-13",
        status="已创建",
    )] == [record["request_id"]]

    updated = service.update(
        user_id="user-a",
        request_id=record["request_id"],
        changes={"destination": "深圳", "accommodation_standard": "600元/晚"},
    )
    assert updated["destination"] == "深圳"
    assert updated["budget"]["accommodation"] == 1200.0
    assert updated["status"] == "已修改"

    cancelled = service.cancel(user_id="user-a", request_id=record["request_id"])
    assert cancelled["status"] == "已撤销"
    try:
        service.update(
            user_id="user-a",
            request_id=record["request_id"],
            changes={"destination": "广州"},
        )
    except ValueError as exc:
        assert "不能修改" in str(exc)
    else:
        raise AssertionError("cancelled travel request was unexpectedly modified")


def test_natural_language_query_modify_and_cancel(tmp_path: Path):
    service = EmployeeTravelService(tmp_path / "travel.json")
    record, _ = service.create(
        user_id="user-a",
        draft=_complete_draft(),
        plan_id="plan-one",
    )

    queried = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message="查询李娜的差旅",
    )
    assert queried["records"][0]["request_id"] == record["request_id"]

    destination_query = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message="查询去北京的差旅",
    )
    assert destination_query["records"][0]["request_id"] == record["request_id"]

    date_query = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message="查询2026-08-13的差旅记录",
    )
    assert date_query["records"][0]["request_id"] == record["request_id"]

    modified = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message=f"修改{record['request_id']}目的地为深圳",
        state={
            "mode": "collecting",
            "draft": {"employee_name": "错误草稿"},
            "awaiting_field": "origin",
        },
    )
    assert modified["records"][0]["destination"] == "深圳"
    assert modified["records"][0]["budget"]["transport"] > record["budget"]["transport"]
    assert modified["records"][0]["budget"]["total"] > record["budget"]["total"]

    lhasa = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message=f"修改{record['request_id']}目的地为拉萨",
    )
    assert lhasa["records"][0]["destination"] == "拉萨"
    assert lhasa["records"][0]["budget"]["transport"] > modified["records"][0]["budget"]["transport"]
    assert "路线约" in lhasa["records"][0]["budget"]["note"]

    status_query = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message="查询已修改的差旅",
    )
    assert status_query["records"][0]["request_id"] == record["request_id"]

    cancelled = handle_travel_turn(
        service,
        user_id="user-a",
        conversation_id="conversation-a",
        message=f"撤销 {record['request_id']}",
    )
    assert cancelled["records"][0]["status"] == "已撤销"


def test_frontend_persists_and_renders_travel_scenario():
    root = Path(__file__).parents[1]
    source = (root / "web" / "app.js").read_text(encoding="utf-8")
    styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

    assert "travelScenario: normalizeTravelScenario(activeTravelScenario)" in source
    assert "activeTravelScenario = normalizeTravelScenario(normalized.travelScenario)" in source
    assert 'fetch("/api/travel/turn"' in source
    assert 'fetch("/api/travel/confirm"' in source
    assert 'confirmButton.textContent = "确认执行"' in source
    assert 'table.className = "travel-records-table"' in source
    assert ".travel-records-table" in styles
