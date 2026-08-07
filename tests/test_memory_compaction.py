import asyncio

import pytest

from src.memory.compaction import (
    CompactionEngine,
    CompactionToolCallError,
    CompactionValidationError,
    NO_TOOL_WARNING,
    SUMMARY_SECTIONS,
    build_compaction_prompt,
    completed_turns,
    deterministic_summary,
    parse_compaction_response,
    render_compaction_segments,
    select_recent_turns,
    summary_user_message_ids,
)
from src.memory.models import MemoryMessage, RecoveryAttachments
from src.memory.store import MemoryStore, MemoryStoreError


def _message(message_id, sequence, role, content):
    return MemoryMessage(
        message_id=message_id,
        user_id="alice",
        session_id="thread",
        sequence=sequence,
        role=role,
        content=content,
    )


def _valid_summary():
    return "\n\n".join(
        f"## {index}. {section}\ncontent {index}"
        for index, section in enumerate(SUMMARY_SECTIONS, 1)
    )


def test_prompt_has_double_no_tool_guard_and_redacts_secrets():
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz"
    prompt = build_compaction_prompt([_message("m1", 1, "user", secret)])

    assert prompt.startswith(NO_TOOL_WARNING)
    assert prompt.endswith(NO_TOOL_WARNING)
    assert secret not in prompt
    assert "[REDACTED]" in prompt


def test_prompt_excludes_system_tool_and_runtime_plan_payloads():
    prompt = build_compaction_prompt(
        [
            {
                "message_id": "system",
                "sequence": 1,
                "role": "system",
                "content": "SYSTEM SECRET CONFIGURATION",
            },
            {
                "message_id": "tool",
                "sequence": 2,
                "role": "tool",
                "content": "RAW TOOL RESULT",
            },
            _message("user", 3, "user", "ordinary conversation"),
        ]
    )

    assert "ordinary conversation" in prompt
    assert "SYSTEM SECRET CONFIGURATION" not in prompt
    assert "RAW TOOL RESULT" not in prompt


def test_xml_parser_strips_analysis_and_requires_nine_sections():
    candidate = (
        '<memory_compaction version="1"><analysis>discard me</analysis><summary>'
        + _valid_summary()
        + "</summary></memory_compaction>"
    )
    summary = parse_compaction_response(candidate)

    assert "discard me" not in summary
    for section in SUMMARY_SECTIONS:
        assert section in summary


def test_user_message_ids_are_read_only_from_the_required_summary_section():
    summary = _valid_summary().replace(
        "## 6. All User Messages\ncontent 6",
        "## 6. All User Messages\n- [u1] first\n- [u2] second",
    )

    assert summary_user_message_ids(summary) == ("u1", "u2")


def test_tool_call_invalidates_compaction():
    candidate = {
        "content": "",
        "tool_calls": [{"name": "bad", "args": {}, "id": "1"}],
    }
    with pytest.raises(CompactionToolCallError):
        parse_compaction_response(candidate)


def test_engine_creates_four_part_envelope_and_store_keeps_tail(tmp_path):
    messages = [
        _message("m1", 1, "user", "first request " * 200),
        _message("m2", 2, "assistant", "first result " * 200),
    ]
    engine = CompactionEngine(summarizer=None, trigger_tokens=1, target_tokens=1000)
    record = asyncio.run(
        engine.compact(
            messages,
            attachments=RecoveryAttachments(
                current_plan={"steps": ["one"]},
                active_skills=("planner",),
                async_tasks=({"task_id": "bg-1", "status": "running"},),
            ),
            hook_results=[{"hook": "pre-compact", "status": "ok"}],
        )
    )
    segments = render_compaction_segments(record)

    assert len(segments) == 4
    assert [segment["metadata"]["memory_type"] for segment in segments] == [
        "boundary",
        "summary",
        "attachments",
        "hook_results",
    ]
    assert "bg-1" in segments[2]["content"]

    store = MemoryStore(tmp_path / "memory.sqlite3")
    for message in messages:
        store.append_message(message)
    store.save_compaction(record)
    store.append_message(
        user_id="alice",
        session_id="thread",
        role="user",
        content="new tail",
        message_id="m3",
    )
    latest, tail = store.messages_after_compaction("alice", "thread")
    assert latest.compaction_id == record.compaction_id
    assert [message.message_id for message in tail] == ["m3"]


def test_engine_discards_model_tool_call_instead_of_fallback():
    class BadModel:
        async def ainvoke(self, _prompt):
            return {
                "content": "",
                "tool_calls": [{"name": "bad", "args": {}, "id": "1"}],
            }

    engine = CompactionEngine(summarizer=BadModel(), trigger_tokens=1)
    with pytest.raises(CompactionToolCallError):
        asyncio.run(engine.compact([_message("m1", 1, "user", "hello")]))


def test_engine_rejects_deterministic_compaction_that_does_not_reduce_tokens():
    engine = CompactionEngine(summarizer=None, trigger_tokens=1, target_tokens=1000)

    with pytest.raises(CompactionValidationError, match="does not reduce"):
        asyncio.run(engine.compact([_message("m1", 1, "user", "hello")]))


def test_engine_rejects_model_compaction_that_does_not_reduce_tokens():
    class Model:
        async def ainvoke(self, _prompt):
            return (
                '<memory_compaction version="1"><analysis>discard</analysis><summary>'
                + _valid_summary()
                + "</summary></memory_compaction>"
            )

    engine = CompactionEngine(
        summarizer=Model(),
        trigger_tokens=1,
        target_tokens=1000,
        fallback_on_error=False,
    )

    with pytest.raises(CompactionValidationError, match="does not reduce"):
        asyncio.run(engine.compact([_message("m1", 1, "user", "hello")]))


def test_engine_records_when_llm_summary_is_actually_used():
    class Model:
        calls = 0

        async def ainvoke(self, _prompt):
            self.calls += 1
            return (
                '<memory_compaction version="1"><analysis>discard</analysis><summary>'
                + _valid_summary()
                + "</summary></memory_compaction>"
            )

    model = Model()
    record = asyncio.run(
        CompactionEngine(
            summarizer=model,
            trigger_tokens=1,
            target_tokens=120,
            fallback_on_error=False,
        ).compact(
            [
                _message("u1", 1, "user", "large request " * 250),
                _message("a1", 2, "assistant", "large answer " * 250),
            ]
        )
    )

    assert model.calls == 1
    assert record.metadata["summary_mode"] == "llm"
    assert record.metadata["summarizer_used"] is True
    assert record.metadata["fallback"] is False


def test_deterministic_fallback_inherits_prior_semantics_and_filters_stale_work():
    prior = _valid_summary()
    prior = prior.replace(
        "## 1. Primary Request and Intent\ncontent 1",
        "## 1. Primary Request and Intent\nBuild the governed memory pipeline.",
    ).replace(
        "## 6. All User Messages\ncontent 6",
        "## 6. All User Messages\n- [u0] Keep the original recovery semantics.",
    ).replace(
        "## 8. Current Work\ncontent 8",
        "## 8. Current Work\nImplement stable message identity and semantic recovery.",
    )
    messages = [
        MemoryMessage(
            message_id="summary:old",
            user_id="alice",
            session_id="thread",
            sequence=1,
            role="assistant",
            content=prior,
            metadata={"memory_type": "prior_summary"},
        ),
        _message("a-question", 2, "assistant", "请补充报告主题？"),
        _message("u1", 3, "user", "主题是季度销售。"),
        _message("a-error", 4, "assistant", "工作流执行失败。"),
    ]

    summary = deterministic_summary(messages)

    assert "Keep the original recovery semantics." in summary
    assert "Implement stable message identity and semantic recovery." in summary
    current_work = summary.split("## 8. Current Work\n", 1)[1].split(
        "## 9. Optional Next Step", 1
    )[0]
    assert "请补充报告主题" not in current_work
    assert "工作流执行失败" not in current_work


def test_bounded_summary_preserves_all_covered_user_message_ids():
    messages = []
    covered = []
    for index in range(12):
        user_id = f"u{index}"
        covered.append(user_id)
        messages.extend(
            [
                _message(user_id, index * 2 + 1, "user", "large request " * 100),
                _message(
                    f"a{index}",
                    index * 2 + 2,
                    "assistant",
                    "large answer " * 100,
                ),
            ]
        )

    record = asyncio.run(
        CompactionEngine(summarizer=None, trigger_tokens=1, target_tokens=100).compact(
            messages,
            covered_user_message_ids=covered,
        )
    )

    assert set(summary_user_message_ids(record.summary)) == set(covered)


def test_recent_turn_preflight_keeps_whole_turns_without_summary_retries():
    messages = [
        _message("u1", 1, "user", "first"),
        _message("a1", 2, "assistant", "first result"),
        _message("u2", 3, "user", "second"),
        _message("a2", 4, "assistant", "second result"),
        _message("u3", 5, "user", "current unanswered request"),
    ]

    turns = completed_turns(messages)
    retained = select_recent_turns(
        turns,
        available_tokens=1000,
        summary_target_tokens=20,
    )

    assert [[item.message_id for item in turn] for turn in retained] == [
        ["u1", "a1"],
        ["u2", "a2"],
    ]
    assert all(item.message_id != "u3" for turn in retained for item in turn)


def test_completed_turns_groups_interleaved_outputs_by_explicit_turn_id():
    messages = [
        MemoryMessage(
            message_id="u-a",
            user_id="alice",
            session_id="thread",
            sequence=1,
            role="user",
            content="request A",
            metadata={"turn_id": "u-a"},
        ),
        MemoryMessage(
            message_id="u-b",
            user_id="alice",
            session_id="thread",
            sequence=2,
            role="user",
            content="request B",
            metadata={"turn_id": "u-b"},
        ),
        MemoryMessage(
            message_id="a-a",
            user_id="alice",
            session_id="thread",
            sequence=3,
            role="assistant",
            content="result A",
            metadata={"turn_id": "u-a"},
        ),
        MemoryMessage(
            message_id="a-b",
            user_id="alice",
            session_id="thread",
            sequence=4,
            role="assistant",
            content="result B",
            metadata={"turn_id": "u-b"},
        ),
    ]

    turns = completed_turns(messages)

    assert [[item.message_id for item in turn] for turn in turns] == [
        ["u-a", "a-a"],
        ["u-b", "a-b"],
    ]


def test_recent_turn_preflight_degrades_locally_to_one_or_zero_turns():
    turns = completed_turns(
        [
            _message("u1", 1, "user", "older request"),
            _message("a1", 2, "assistant", "older answer"),
            _message("u2", 3, "user", "new request " * 20),
            _message("a2", 4, "assistant", "new answer " * 20),
        ]
    )

    one = select_recent_turns(
        turns,
        available_tokens=120,
        summary_target_tokens=20,
    )
    zero = select_recent_turns(
        turns,
        available_tokens=20,
        summary_target_tokens=20,
    )

    assert len(one) <= 1
    assert zero == ()


def test_compaction_commit_rejects_changed_transcript_watermark(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    messages = [
        store.append_message(
            user_id="alice",
            session_id="thread",
            role="user",
            content="first request " * 200,
            message_id="u1",
        ),
        store.append_message(
            user_id="alice",
            session_id="thread",
            role="assistant",
            content="first result " * 200,
            message_id="a1",
        ),
    ]
    record = asyncio.run(
        CompactionEngine(summarizer=None, trigger_tokens=1).compact(messages)
    )
    record.metadata["transcript_watermark_sequence"] = 2
    store.append_message(
        user_id="alice",
        session_id="thread",
        role="user",
        content="arrived during compaction",
        message_id="u2",
    )

    with pytest.raises(MemoryStoreError, match="watermark changed"):
        store.save_compaction(record)

    assert store.latest_compaction("alice", "thread") is None
