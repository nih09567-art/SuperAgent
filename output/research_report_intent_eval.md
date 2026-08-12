# Intent Profile Evaluation Report

- Suite version: 2.0
- Selected cases: 33
- Evaluation: rule

## rule 模式

- Cases: 33
- Degraded cases: 0
- overall_case_pass_rate: 0.6061
- primary_intent_accuracy: 0.9394
- primary_goal_accuracy: 1.0
- sub_intent_precision: 1.0
- sub_intent_recall: 0.9848
- explicit_intent_accuracy: 1.0
- inferred_intent_accuracy: 1.0
- provenance_accuracy: 1.0
- negated_recognition_accuracy: 1.0
- negated_action_misexecution_rate: 0.0
- unknown_rejection_accuracy: 1.0
- clarification_accuracy: 0.75
- clarification_question_accuracy: 1.0
- conditional_dependency_accuracy: 1.0
- entity_field_accuracy: 0.9646
- dependency_exact_accuracy: 0.9231

### 来源统计

| Source | Count |
|---|---:|
| rule | 70 |

### 分层结果

| Tier | Pass rate |
|---|---:|
| challenge | 0.5714 |
| regression | 0.5556 |
| semantic | 0.7500 |

### 分类结果

| Category | Pass rate |
|---|---:|
| ambiguity | 0.5000 |
| conditional | 1.0000 |
| conditional_action | 1.0000 |
| conditional_dependency | 1.0000 |
| cross_domain_composite | 0.0000 |
| deduplication | 1.0000 |
| dependency_order | 0.0000 |
| entity_and_send | 0.0000 |
| entity_boundary | 1.0000 |
| implicit_dependency | 0.0000 |
| keyword_boundary | 0.0000 |
| missing_field_and_dependency | 1.0000 |
| negation | 0.0000 |
| parallel_boundary | 1.0000 |
| paraphrase | 0.0000 |
| regression | 1.0000 |
| risk_action | 1.0000 |
| semantic_synonym | 1.0000 |
| single_intent | 0.8333 |
| synonym_and_action | 1.0000 |
| unknown_intent | 0.0000 |
| unknown_rejection | 1.0000 |

### 用例明细

| Case | Tier | Category | Status | Primary | Goal | Explicit | Inferred | Negation | Clarify | Source mode |
|---|---|---|---|---|---|---|---|---|---|---|
| hr_basic_info | regression | single_intent | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| hr_salary_month | regression | single_intent | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| knowledge_leave_policy | regression | single_intent | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| weather_tomorrow | regression | single_intent | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| programming_java_learning | regression | single_intent | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| travel_schedule_query | regression | single_intent | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| meeting_cancel | regression | risk_action | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| research_risk_report_send | regression | cross_domain_composite | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| leave_application_requires_employee_context | regression | implicit_dependency | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| leave_letter_synonym_requires_employee_context | regression | implicit_dependency | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| employment_certificate_requires_employee_context | regression | implicit_dependency | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| certificate_send_email_recipient | regression | entity_and_send | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| duplicate_employee_keywords | regression | deduplication | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| duplicate_document_keywords | regression | deduplication | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| ambiguous_employee_request | regression | ambiguity | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| unknown_general_request | regression | unknown_intent | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| conditional_risk_report | regression | conditional_dependency | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| hr_income_proof_send_order | challenge | dependency_order | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| missing_recipient_income_proof | challenge | missing_field_and_dependency | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| office_meeting_notify_actions | challenge | conditional_action | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| knowledge_report_boundary | challenge | keyword_boundary | FAIL | FAIL | PASS | PASS | PASS | PASS | PASS | rule |
| meeting_synonym_open_meeting | challenge | synonym_and_action | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| leave_material_paraphrase | challenge | paraphrase | FAIL | FAIL | PASS | PASS | PASS | PASS | PASS | rule |
| recipient_synonym_handover | challenge | entity_boundary | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| hr_profile_leave_records_summary | regression | parallel_boundary | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| semantic_synonym_income_proof | semantic | semantic_synonym | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| semantic_weak_schedule | semantic | semantic_synonym | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| semantic_negated_send | semantic | negation | FAIL | PASS | PASS | PASS | PASS | PASS | FAIL | rule |
| semantic_consultation_permissions | semantic | unknown_rejection | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| semantic_conditional_meeting | semantic | conditional | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| semantic_missing_recipient | semantic | ambiguity | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |
| semantic_keyword_false_positive | semantic | negation | FAIL | PASS | PASS | PASS | PASS | PASS | FAIL | rule |
| semantic_original_rule_regression | semantic | regression | PASS | PASS | PASS | PASS | PASS | PASS | PASS | rule |

### 失败摘要

- `weather_tomorrow`：missing_fields, confidence；实际 intent=weather_query，goal=weather_query，sub_intents=['weather_query']
- `research_risk_report_send`：confidence；实际 intent=information_research，goal=information_research，sub_intents=['information_research', 'risk_analysis', 'report_generation', 'message_or_email_send']
- `leave_application_requires_employee_context`：entities；实际 intent=employee_information_query，goal=document_generation，sub_intents=['employee_information_query', 'document_generation']
- `leave_letter_synonym_requires_employee_context`：confidence；实际 intent=employee_information_query，goal=document_generation，sub_intents=['employee_information_query', 'document_generation']
- `employment_certificate_requires_employee_context`：entities, confidence；实际 intent=employee_information_query，goal=document_generation，sub_intents=['employee_information_query', 'document_generation']
- `certificate_send_email_recipient`：confidence；实际 intent=employee_information_query，goal=document_generation，sub_intents=['employee_information_query', 'document_generation', 'message_or_email_send']
- `ambiguous_employee_request`：missing_fields, confidence；实际 intent=employee_information_query，goal=employee_information_query，sub_intents=['employee_information_query']
- `unknown_general_request`：missing_fields；实际 intent=general_assistance，goal=general_assistance，sub_intents=[]
- `hr_income_proof_send_order`：confidence；实际 intent=employee_information_query，goal=employee_information_query，sub_intents=['employee_information_query', 'salary_query', 'document_generation', 'message_or_email_send']
- `knowledge_report_boundary`：primary_intent, subtask_actions, dependencies；实际 intent=report_generation，goal=report_generation，sub_intents=['report_generation', 'knowledge_lookup']
- `leave_material_paraphrase`：sub_intents, primary_intent, entities, subtask_count, subtask_actions, dependencies, task_type, composite；实际 intent=document_generation，goal=document_generation，sub_intents=['document_generation']
- `semantic_negated_send`：missing_fields, clarification；实际 intent=report_generation，goal=report_generation，sub_intents=['report_generation']
- `semantic_keyword_false_positive`：missing_fields, clarification；实际 intent=general_assistance，goal=general_assistance，sub_intents=[]
