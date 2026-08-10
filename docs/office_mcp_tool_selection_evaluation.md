# 统一 MCP 工具选择评估

生成时间：`2026-08-10T02:38:45.820544+00:00`

候选来自真实 tools/list 生成的 ToolRegistry 基线快照；本评估不执行工具。

| 指标 | 实际结果 |
| --- | ---: |
| 总工具覆盖率 | 100.000000% |
| Server 识别准确率 | 100.000000% |
| Tool Top-1 | 100.000000% |
| Tool Top-3 | 100.000000% |
| 参数/Schema 有效率 | 100.000000% |
| 不存在工具幻觉率 | 0.000000% |
| 平均过滤前候选数 | 48.000000 |
| 平均过滤后候选数 | 2.170000 |
| 平均选择耗时 | 1.513715 ms |
| audit 建议与实际契约一致率 | 100.000000% |

## 用例结果

| ID | 期望 | Top-1 | Top-3 | 过滤前/后 | 耗时(ms) |
| --- | --- | --- | --- | ---: | ---: |
| normal-01 | office-mcp:search_employees | office-mcp:search_employees | office-mcp:search_employees / office-mcp:get_employee_contact / office-mcp:get_employee_profile | 48/4 | 3.734400 |
| normal-02 | office-mcp:get_employee_profile | office-mcp:get_employee_profile | office-mcp:get_employee_profile / office-mcp:get_employee_contact / office-mcp:search_employees | 48/4 | 1.904200 |
| normal-03 | office-mcp:get_org_unit | office-mcp:get_org_unit | office-mcp:get_org_unit / office-mcp:search_employees / office-mcp:get_employee_contact | 48/5 | 1.433200 |
| normal-04 | office-mcp:get_manager_chain | office-mcp:get_manager_chain | office-mcp:get_manager_chain / office-mcp:get_employee_profile / office-mcp:get_employee_contact | 48/4 | 1.622900 |
| normal-05 | office-mcp:get_employee_contact | office-mcp:get_employee_contact | office-mcp:get_employee_contact / office-mcp:search_employees / office-mcp:get_employee_profile | 48/4 | 1.655500 |
| normal-06 | office-mcp:list_calendar_events | office-mcp:list_calendar_events | office-mcp:list_calendar_events | 48/1 | 1.330000 |
| normal-07 | office-mcp:find_free_slots | office-mcp:find_free_slots | office-mcp:find_free_slots | 48/1 | 1.350000 |
| normal-08 | office-mcp:create_calendar_event | office-mcp:create_calendar_event | office-mcp:create_calendar_event | 48/1 | 1.510100 |
| normal-09 | office-mcp:update_calendar_event | office-mcp:update_calendar_event | office-mcp:update_calendar_event | 48/1 | 1.438400 |
| normal-10 | office-mcp:cancel_calendar_event | office-mcp:cancel_calendar_event | office-mcp:cancel_calendar_event | 48/1 | 1.298800 |
| normal-11 | office-mcp:search_courses | office-mcp:search_courses | office-mcp:search_courses / office-mcp:get_learning_record | 48/2 | 1.282900 |
| normal-12 | office-mcp:get_course_detail | office-mcp:get_course_detail | office-mcp:get_course_detail / office-mcp:list_course_sessions / office-mcp:get_learning_record | 48/4 | 1.318800 |
| normal-13 | office-mcp:list_course_sessions | office-mcp:list_course_sessions | office-mcp:list_course_sessions / office-mcp:get_course_detail / office-mcp:get_learning_record | 48/4 | 1.706700 |
| normal-14 | office-mcp:enroll_course | office-mcp:enroll_course | office-mcp:enroll_course | 48/1 | 1.376100 |
| normal-15 | office-mcp:get_learning_record | office-mcp:get_learning_record | office-mcp:get_learning_record / office-mcp:search_courses | 48/2 | 1.330300 |
| normal-16 | office-mcp:search_travel_policy | office-mcp:search_travel_policy | office-mcp:search_travel_policy / office-mcp:get_travel_request | 48/2 | 1.310500 |
| normal-17 | office-mcp:estimate_trip_cost | office-mcp:estimate_trip_cost | office-mcp:estimate_trip_cost / office-mcp:search_travel_policy / office-mcp:get_travel_request | 48/3 | 1.921600 |
| normal-18 | office-mcp:create_travel_request | office-mcp:create_travel_request | office-mcp:create_travel_request | 48/1 | 1.377800 |
| normal-19 | office-mcp:get_travel_request | office-mcp:get_travel_request | office-mcp:get_travel_request / office-mcp:search_travel_policy | 48/2 | 1.310300 |
| normal-20 | office-mcp:cancel_travel_request | office-mcp:cancel_travel_request | office-mcp:cancel_travel_request | 48/1 | 1.312400 |
| normal-21 | office-mcp:find_meeting_slots | office-mcp:find_meeting_slots | office-mcp:find_meeting_slots | 48/1 | 1.720900 |
| normal-22 | office-mcp:create_meeting | office-mcp:create_meeting | office-mcp:create_meeting | 48/1 | 1.315000 |
| normal-23 | office-mcp:update_meeting | office-mcp:update_meeting | office-mcp:update_meeting | 48/1 | 1.312400 |
| normal-24 | office-mcp:cancel_meeting | office-mcp:cancel_meeting | office-mcp:cancel_meeting | 48/1 | 1.278500 |
| normal-25 | office-mcp:get_meeting_minutes | office-mcp:get_meeting_minutes | office-mcp:get_meeting_minutes | 48/1 | 1.742500 |
| normal-26 | office-mcp:resolve_contacts | office-mcp:resolve_contacts | office-mcp:resolve_contacts / office-mcp:list_message_templates | 48/2 | 1.527400 |
| normal-27 | office-mcp:list_message_templates | office-mcp:list_message_templates | office-mcp:list_message_templates / office-mcp:resolve_contacts | 48/2 | 1.321100 |
| normal-28 | office-mcp:send_email | office-mcp:send_email | office-mcp:send_email | 48/1 | 1.297800 |
| normal-29 | office-mcp:send_message | office-mcp:send_message | office-mcp:send_message | 48/1 | 1.515500 |
| normal-30 | office-mcp:get_delivery_status | office-mcp:get_delivery_status | office-mcp:get_delivery_status / office-mcp:resolve_contacts / office-mcp:list_message_templates | 48/3 | 1.411600 |
| excel-01 | excel-mcp-remote:apply_formula | excel-mcp-remote:apply_formula | excel-mcp-remote:apply_formula | 48/1 | 1.397000 |
| excel-02 | excel-mcp-remote:validate_formula_syntax | excel-mcp-remote:validate_formula_syntax | excel-mcp-remote:validate_formula_syntax | 48/1 | 1.421800 |
| excel-03 | excel-mcp-remote:format_range | excel-mcp-remote:format_range | excel-mcp-remote:format_range | 48/1 | 1.645700 |
| excel-04 | excel-mcp-remote:read_data_from_excel | excel-mcp-remote:read_data_from_excel | excel-mcp-remote:read_data_from_excel / excel-mcp-remote:get_workbook_metadata | 48/2 | 1.640000 |
| excel-05 | excel-mcp-remote:write_data_to_excel | excel-mcp-remote:write_data_to_excel | excel-mcp-remote:write_data_to_excel / excel-mcp-remote:read_data_from_excel / excel-mcp-remote:create_workbook | 48/6 | 1.356800 |
| excel-06 | excel-mcp-remote:create_workbook | excel-mcp-remote:create_workbook | excel-mcp-remote:create_workbook | 48/1 | 1.424700 |
| excel-07 | excel-mcp-remote:create_worksheet | excel-mcp-remote:create_worksheet | excel-mcp-remote:create_worksheet / excel-mcp-remote:create_workbook | 48/2 | 1.694100 |
| excel-08 | excel-mcp-remote:create_chart | excel-mcp-remote:create_chart | excel-mcp-remote:create_chart / excel-mcp-remote:create_workbook / excel-mcp-remote:create_worksheet | 48/3 | 1.707900 |
| excel-09 | excel-mcp-remote:create_pivot_table | excel-mcp-remote:create_pivot_table | excel-mcp-remote:create_pivot_table / excel-mcp-remote:create_workbook / excel-mcp-remote:create_worksheet | 48/3 | 1.423400 |
| excel-10 | excel-mcp-remote:copy_worksheet | excel-mcp-remote:copy_worksheet | excel-mcp-remote:copy_worksheet | 48/1 | 1.371400 |
| excel-11 | excel-mcp-remote:delete_worksheet | excel-mcp-remote:delete_worksheet | excel-mcp-remote:delete_worksheet | 48/1 | 1.596500 |
| excel-12 | excel-mcp-remote:rename_worksheet | excel-mcp-remote:rename_worksheet | excel-mcp-remote:rename_worksheet | 48/1 | 1.689500 |
| excel-13 | excel-mcp-remote:get_workbook_metadata | excel-mcp-remote:get_workbook_metadata | excel-mcp-remote:get_workbook_metadata | 48/1 | 1.439900 |
| excel-14 | excel-mcp-remote:merge_cells | excel-mcp-remote:merge_cells | excel-mcp-remote:merge_cells / excel-mcp-remote:unmerge_cells / excel-mcp-remote:delete_range | 48/10 | 1.407000 |
| excel-15 | excel-mcp-remote:unmerge_cells | excel-mcp-remote:unmerge_cells | excel-mcp-remote:unmerge_cells | 48/1 | 1.658700 |
| excel-16 | excel-mcp-remote:copy_range | excel-mcp-remote:copy_range | excel-mcp-remote:copy_range | 48/1 | 1.615400 |
| excel-17 | excel-mcp-remote:delete_range | excel-mcp-remote:delete_range | excel-mcp-remote:delete_range / excel-mcp-remote:delete_worksheet | 48/2 | 1.498400 |
| excel-18 | excel-mcp-remote:validate_excel_range | excel-mcp-remote:validate_excel_range | excel-mcp-remote:validate_excel_range | 48/1 | 1.393600 |
| ambiguous-01 | office-mcp:search_employees | office-mcp:search_employees | office-mcp:search_employees / office-mcp:get_employee_contact / office-mcp:get_employee_profile | 48/4 | 1.552400 |
| missing-01 | - | ABSTAIN | - | 48/0 | 1.524000 |
| missing-02 | - | ABSTAIN | - | 48/0 | 1.300500 |
| unknown-01 | - | ABSTAIN | - | 48/0 | 1.259000 |
| unknown-02 | - | ABSTAIN | office-mcp:get_employee_contact / office-mcp:get_employee_profile / office-mcp:get_learning_record | 48/10 | 1.241600 |

## 口径

- `total_tool_coverage_rate_pct`：unique expected (server, tool) pairs in normal cases / discovered registry tools
- `server_recognition_accuracy_pct`：labeled cases selecting the expected MCP Server / labeled cases
- `tool_top_1_pct`：labeled cases selecting the expected (server, tool) at rank 1 / labeled cases
- `tool_top_3_pct`：labeled cases containing the expected (server, tool) in Top-3 / labeled cases
- `parameter_schema_valid_rate_pct`：selected outcomes satisfying live tools/list Schema / selected outcomes
- `nonexistent_tool_hallucination_rate_pct`：unknown cases returning a selected tool / unknown cases
- `average_candidate_count_before_filter`：mean ToolRegistry candidates before layered filters
- `average_candidate_count_after_filter`：mean Schema-valid candidates after layered filters
- `average_selection_time_ms`：mean in-process wall-clock selection latency per case
- `audit_recommendation_actual_match_pct`：labeled cases whose recommendation maps to the expected current concrete/legacy contract / labeled cases
