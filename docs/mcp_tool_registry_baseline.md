# MCP ToolRegistry 基线快照

生成时间：`2026-08-10T02:38:26.735948+00:00`

数据源：每个已配置 MCP Server 的真实 `tools/list`，随后由 ToolLoader 注册到独立 ToolRegistry。

| Server | tools/list |
| --- | ---: |
| excel-mcp-remote | 18 |
| office-mcp | 30 |
| **合计** | **48** |

## 集合差异

- `server_minus_registry`：[]
- `registry_minus_server`：[]
- `server_minus_semantics`：[]
- `semantics_minus_server`：[]
- `agent_selected_minus_registry_or_legacy`：['query_leave_record', 'remote_credit_risk_db_tool', 'remote_docx_generator_tool', 'remote_report_builder_tool', 'remote_salary_info_tool', 'remote_todo_query_tool', 'remote_unicorn_db_tool', 'remote_weather_tool', 'save_leave_record']

## 工具明细

| Server | 工具 | 场景 | 操作 | Agent引用 |
| --- | --- | --- | --- | --- |
| excel-mcp-remote | apply_formula | spreadsheet | apply | - |
| excel-mcp-remote | copy_range | spreadsheet | copy | - |
| excel-mcp-remote | copy_worksheet | spreadsheet | copy | - |
| excel-mcp-remote | create_chart | spreadsheet | create | - |
| excel-mcp-remote | create_pivot_table | spreadsheet | create | - |
| excel-mcp-remote | create_workbook | spreadsheet | create | - |
| excel-mcp-remote | create_worksheet | spreadsheet | create | - |
| excel-mcp-remote | delete_range | spreadsheet | delete | - |
| excel-mcp-remote | delete_worksheet | spreadsheet | delete | - |
| excel-mcp-remote | format_range | spreadsheet | format | - |
| excel-mcp-remote | get_workbook_metadata | spreadsheet | query | - |
| excel-mcp-remote | merge_cells | spreadsheet | merge | - |
| excel-mcp-remote | read_data_from_excel | spreadsheet | query | - |
| excel-mcp-remote | rename_worksheet | spreadsheet | update | - |
| excel-mcp-remote | unmerge_cells | spreadsheet | unmerge | - |
| excel-mcp-remote | validate_excel_range | spreadsheet | validate | - |
| excel-mcp-remote | validate_formula_syntax | spreadsheet | validate | - |
| excel-mcp-remote | write_data_to_excel | spreadsheet | write | - |
| office-mcp | cancel_calendar_event | calendar | cancel | RemoteScheduleAgent |
| office-mcp | cancel_meeting | meeting | cancel | RemoteMeetingManagerAgent |
| office-mcp | cancel_travel_request | travel | cancel | RemoteOfficeAssistantAgent |
| office-mcp | create_calendar_event | calendar | create | RemoteHRCalendarAgent |
| office-mcp | create_meeting | meeting | create | RemoteMeetingManagerAgent |
| office-mcp | create_travel_request | travel | create | RemoteOfficeAssistantAgent |
| office-mcp | enroll_course | course | enroll | RemoteKnowledgeAgent |
| office-mcp | estimate_trip_cost | travel | query | RemoteOfficeAssistantAgent |
| office-mcp | find_free_slots | calendar | query | RemoteHRCalendarAgent |
| office-mcp | find_meeting_slots | meeting | query | RemoteMeetingManagerAgent |
| office-mcp | get_course_detail | course | query | RemoteKnowledgeAgent |
| office-mcp | get_delivery_status | messaging | query | RemoteCommunicationAgent, RemoteEmailDispatchAgent |
| office-mcp | get_employee_contact | personnel | query | RemoteCommunicationAgent |
| office-mcp | get_employee_profile | personnel | query | RemoteHRAssistantAgent |
| office-mcp | get_learning_record | course | query | RemoteKnowledgeAgent |
| office-mcp | get_manager_chain | personnel | query | RemoteHRAssistantAgent |
| office-mcp | get_meeting_minutes | meeting | query | RemoteMeetingManagerAgent |
| office-mcp | get_org_unit | personnel | query | RemoteHRAssistantAgent |
| office-mcp | get_travel_request | travel | query | RemoteOfficeAssistantAgent |
| office-mcp | list_calendar_events | calendar | query | RemoteHRCalendarAgent |
| office-mcp | list_course_sessions | course | query | RemoteKnowledgeAgent |
| office-mcp | list_message_templates | messaging | query | RemoteCommunicationAgent, RemoteEmailDispatchAgent |
| office-mcp | resolve_contacts | messaging | query | RemoteCommunicationAgent |
| office-mcp | search_courses | course | query | RemoteKnowledgeAgent |
| office-mcp | search_employees | personnel | query | RemoteHRAssistantAgent |
| office-mcp | search_travel_policy | travel | query | RemoteOfficeAssistantAgent |
| office-mcp | send_email | messaging | send | RemoteCommunicationAgent, RemoteEmailDispatchAgent |
| office-mcp | send_message | messaging | send | RemoteCommunicationAgent, RemoteEmailDispatchAgent |
| office-mcp | update_calendar_event | calendar | update | RemoteScheduleAgent |
| office-mcp | update_meeting | meeting | update | RemoteMeetingManagerAgent |
