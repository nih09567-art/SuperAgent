import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from src.memory.compaction import CompactionEngine
from src.memory.models import LongTermMemoryStatus, RecoveryAttachments
from src.memory.retrieval import (
    LexicalMemoryRetriever,
    TaggedMemoryRetriever,
    format_untrusted_memories,
)
from src.memory.store import MemoryStore, MemoryStoreError, SecretDetectedError


def test_transcript_persists_and_isolates_users(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    stored = store.append_message(
        user_id="alice",
        session_id="thread",
        role="user",
        content="hello",
        message_id="m1",
    )

    reloaded = MemoryStore(path)
    assert reloaded.list_messages("alice", "thread") == [stored]
    assert reloaded.list_messages("bob", "thread") == []


def test_concurrent_appends_keep_all_messages(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    def append(index: int):
        return store.append_message(
            user_id="alice",
            session_id="thread",
            role="user",
            content=f"message {index}",
            message_id=f"m{index}",
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(append, range(20)))

    messages = store.list_messages("alice", "thread")
    assert len(messages) == 20
    assert [message.sequence for message in messages] == list(range(1, 21))
    assert {message.message_id for message in messages} == {
        f"m{index}" for index in range(20)
    }


def test_turn_consolidation_claim_is_atomic_across_store_instances(tmp_path):
    path = tmp_path / "memory.sqlite3"
    first = MemoryStore(path)
    second = MemoryStore(path)

    assert first.claim_turn_consolidation("alice", "thread", "u-1", 2) is True
    assert second.claim_turn_consolidation("alice", "thread", "u-1", 2) is False

    first.release_turn_consolidation_claim("alice", "thread", "u-1")
    assert second.claim_turn_consolidation("alice", "thread", "u-1", 2) is True

    second.mark_turn_consolidated("alice", "thread", "u-1", 2)
    assert first.claim_turn_consolidation("alice", "thread", "u-1", 2) is False
    assert first.list_consolidated_turn_ids("alice", "thread") == {"u-1"}


def test_short_term_secrets_are_redacted_before_disk_write(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz"
    message = store.append_message(
        user_id="alice",
        session_id="thread",
        role="user",
        content=f"key={secret}",
        message_id="secret-message",
    )

    assert secret not in message.content
    assert "[REDACTED]" in message.content
    assert secret.encode() not in path.read_bytes()


def test_long_term_lifecycle_and_secret_rejection(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = store.remember(
        user_id="alice",
        content="I prefer concise reports",
        kind="preference",
        memory_key="report-style",
        provenance={"source": "user"},
    )
    second = store.remember(
        user_id="alice",
        content="I prefer detailed reports",
        kind="preference",
        memory_key="report-style",
        provenance={"source": "user"},
    )

    active = store.list_long_term("alice")
    assert [record.memory_id for record in active] == [second.memory_id]
    superseded = store.get_long_term("alice", first.memory_id)
    assert superseded.status == LongTermMemoryStatus.SUPERSEDED.value
    assert superseded.superseded_by == second.memory_id

    assert store.delete_long_term("alice", second.memory_id) is True
    assert store.list_long_term("alice") == []

    with pytest.raises(SecretDetectedError):
        store.remember(
            user_id="alice",
            content="remember sk-test-abcdefghijklmnopqrstuvwxyz",
            provenance={"source": "user"},
        )
    with pytest.raises(ValueError, match="unsupported memory kind"):
        store.remember(
            user_id="alice",
            content="invalid kind",
            kind="unknown",
            provenance={"source": "user"},
        )


def test_expired_memory_is_not_active(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    record = store.remember(
        user_id="alice",
        content="temporary constraint",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        provenance={"source": "test"},
    )

    assert store.list_long_term("alice") == []
    assert store.get_long_term("alice", record.memory_id).status == "expired"


def test_lexical_retrieval_is_relevant_and_user_scoped(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    weather = store.remember(
        user_id="alice",
        content="北京天气报告使用摄氏温度",
        kind="preference",
        confidence=0.9,
        provenance={"source": "user"},
    )
    store.remember(
        user_id="alice",
        content="代码示例使用 Python",
        kind="preference",
        provenance={"source": "user"},
    )
    store.remember(
        user_id="bob",
        content="北京天气报告使用华氏温度",
        kind="preference",
        provenance={"source": "user"},
    )

    results = LexicalMemoryRetriever().retrieve(
        "北京天气温度",
        [*store.list_long_term("alice"), *store.list_long_term("bob")],
        user_id="alice",
        top_k=5,
    )
    assert results[0].memory.memory_id == weather.memory_id
    assert all(result.memory.user_id == "alice" for result in results)


def test_tagged_retrieval_uses_label_and_read_time_decay(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    active = store.remember(
        user_id="alice",
        content="raw source should not be injected",
        kind="preference",
        memory_key="preference.language",
        value="zh",
        label="Default response language: Chinese.",
        importance=1.0,
        confidence=1.0,
        decay_class="pinned",
        tags=("preference.language",),
        provenance={"source": "test"},
    )
    store.remember(
        user_id="alice",
        content="old task hint",
        kind="episodic",
        memory_key="task.document.report",
        label="Use an obsolete report hint.",
        importance=1.0,
        confidence=1.0,
        decay_class="fast",
        last_reinforced_at=datetime.now(UTC) - timedelta(days=180),
        scope="task",
        tags=("task.document.report",),
        provenance={"source": "test"},
    )

    results = TaggedMemoryRetriever().retrieve(
        "write report",
        store.list_long_term("alice"),
        user_id="alice",
        intent_tags=("task.document.report",),
    )
    rendered = format_untrusted_memories(results)

    assert [item.memory.memory_id for item in results] == [active.memory_id]
    assert "Default response language: Chinese." in rendered
    assert "raw source should not be injected" not in rendered
    assert format_untrusted_memories(results, token_budget=1) == ""


def test_project_retrieval_requires_matching_project_scope(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    current = store.remember(
        user_id="alice",
        content="Project report uses Chinese",
        kind="preference",
        scope="project",
        memory_key="project.report.language.current",
        label="Project reports use Chinese.",
        tags=("task.document.report",),
        metadata={"project_id": "project-a"},
        provenance={"source": "test"},
    )
    other = store.remember(
        user_id="alice",
        content="Other project report uses English",
        kind="preference",
        scope="project",
        memory_key="project.report.language.other",
        label="Other project reports use English.",
        tags=("task.document.report",),
        metadata={"project_id": "project-b"},
        provenance={"source": "test"},
    )

    records = [*store.list_long_term("alice", statuses=("active",))]
    retriever = TaggedMemoryRetriever()
    result = retriever.retrieve(
        "write report",
        records,
        user_id="alice",
        scopes=("project",),
        intent_tags=("task.document.report",),
        project_id="project-a",
    )

    assert [item.memory.memory_id for item in result] == [current.memory_id]
    assert other.memory_id not in {item.memory.memory_id for item in result}
    assert retriever.retrieve(
        "write report",
        records,
        user_id="alice",
        scopes=("project",),
        intent_tags=("task.document.report",),
    ) == []


def test_duplicate_tag_reinforces_one_logical_record(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = store.remember(
        user_id="alice",
        content="Default language is Chinese",
        kind="preference",
        memory_key="preference.language",
        label="Default language is Chinese",
        confidence=0.8,
        provenance={"source": "turn-1"},
    )
    reinforced = store.remember(
        user_id="alice",
        content="Default language is Chinese",
        kind="preference",
        memory_key="preference.language",
        label="Default language is Chinese",
        confidence=0.9,
        provenance={"source": "turn-2"},
    )

    assert reinforced.memory_id == first.memory_id
    assert reinforced.reinforcement_count == 1
    assert reinforced.confidence == 0.9
    assert len(store.list_long_term("alice")) == 1


def test_schema_migration_keeps_legacy_long_term_rows_readable(tmp_path):
    path = tmp_path / "memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE memory_long_term (
            memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            content TEXT NOT NULL, normalized_content TEXT NOT NULL,
            kind TEXT NOT NULL, scope TEXT NOT NULL, confidence REAL NOT NULL,
            provenance_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
            memory_key TEXT, workflow_id TEXT, session_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT,
            superseded_at TEXT, superseded_by TEXT, deleted_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    now = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO memory_long_term (
            memory_id, user_id, content, normalized_content, kind, scope,
            confidence, provenance_json, status, created_at, updated_at
        ) VALUES ('legacy', 'alice', 'legacy preference', 'legacy preference',
                  'preference', 'user', 1.0, '{}', 'active', ?, ?)
        """,
        (now, now),
    )
    connection.commit()
    connection.close()

    record = MemoryStore(path).get_long_term("alice", "legacy")

    assert record is not None
    assert record.label is None
    assert record.decay_class == "medium"
    assert record.reinforcement_count == 0


def test_extractor_version_migration_does_not_replay_completed_turns(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    store.advance_consolidation_watermark(
        "alice", "thread", 4, extractor_version="llm-taxonomy-v2"
    )
    store.mark_turn_consolidated(
        "alice",
        "thread",
        "u1",
        4,
        extractor_version="llm-taxonomy-v2",
    )

    migrated = MemoryStore(path)

    assert migrated.get_consolidation_watermark("alice", "thread") == 4
    assert migrated.list_consolidated_turn_ids("alice", "thread") == {"u1"}


def test_markdown_projection_failure_preserves_previous_view_and_regenerates(
    tmp_path, monkeypatch
):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = store.remember(
        user_id="alice",
        content="Use Chinese reports",
        kind="preference",
        memory_key="preference.language",
        label="Reports use Chinese.",
        provenance={"source": "test"},
    )
    target = store.project_markdown("alice")
    previous = target.read_text(encoding="utf-8")
    second = store.remember(
        user_id="alice",
        content="Use a concise report style",
        kind="preference",
        memory_key="preference.report_style",
        label="Reports are concise.",
        provenance={"source": "test"},
    )

    monkeypatch.setattr("src.memory.store.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store.project_markdown("alice")

    assert target.read_text(encoding="utf-8") == previous
    assert store.get_long_term("alice", second.memory_id).status == "active"

    monkeypatch.undo()
    target.unlink()
    regenerated = store.project_markdown("alice")
    assert regenerated == target
    rebuilt = target.read_text(encoding="utf-8")
    assert "- preference.language: Reports use Chinese." in rebuilt
    assert "- preference.report_style: Reports are concise." in rebuilt
    assert "## preference" not in rebuilt


def test_markdown_projection_renders_structured_tag_and_value(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.remember(
        user_id="alice",
        content="raw source should remain in SQLite",
        kind="preference",
        memory_key="preference.language",
        value="Chinese",
        label="Default response language: Chinese",
        provenance={"source": "test"},
    )

    rendered = store.project_markdown("alice").read_text(encoding="utf-8")

    assert "- preference.language: Chinese" in rendered
    assert "Default response language" not in rendered
    assert "raw source should remain in SQLite" not in rendered
    assert "## preference" not in rendered


def test_successful_compaction_projects_immutable_and_latest_markdown(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.remember(
        user_id="alice",
        content="Chinese",
        kind="preference",
        memory_key="preference.language",
        label="Default response language: Chinese",
        provenance={"source": "test"},
    )
    memory_path = store.project_markdown("alice")
    memory_before_compaction = memory_path.read_text(encoding="utf-8")
    messages = [
        store.append_message(
            user_id="alice",
            session_id="thread",
            role="user",
            content="prepare a detailed quarterly report " * 200,
            message_id="u1",
        ),
        store.append_message(
            user_id="alice",
            session_id="thread",
            role="assistant",
            content="completed report content " * 200,
            message_id="a1",
        ),
    ]
    record = asyncio.run(
        CompactionEngine(summarizer=None, trigger_tokens=1, target_tokens=120).compact(
            messages,
            attachments=RecoveryAttachments(
                current_plan={
                    "steps": ["write report"],
                    "credential": "Bearer abcdefghijklmnop",
                },
                active_skills=("reporter",),
            ),
            hook_results=({"token": "sk-abcdefghijklmnop"},),
            retained_message_ids=("a1",),
            retained_messages=(messages[1],),
            retained_turn_count=1,
            covered_user_message_ids=("u1",),
        )
    )

    saved = store.save_compaction(record)
    immutable = store.compaction_markdown_path(
        "alice", "thread", compaction_id=saved.compaction_id
    )
    latest = store.compaction_markdown_path("alice", "thread")
    rendered = latest.read_text(encoding="utf-8")

    assert immutable.exists()
    assert latest.exists()
    assert immutable.read_text(encoding="utf-8") == rendered
    assert "## Boundary" in rendered
    assert "## Structured Summary" in rendered
    assert "## Retained Turns" in rendered
    assert "## Attachments" in rendered
    assert "## Covered Messages" in rendered
    assert f"- Before: {saved.boundary.token_count_before}" in rendered
    assert f"- After: {saved.boundary.token_count_after}" in rendered
    assert "- Summary mode: deterministic" in rendered
    assert "u1" in rendered
    assert "completed report content" in rendered
    assert "Bearer abcdefghijklmnop" not in rendered
    assert "sk-abcdefghijklmnop" not in rendered
    assert "[REDACTED]" in rendered
    assert saved.metadata["compaction_generation"] == 1
    assert saved.metadata["markdown_projection_path"] == str(latest)
    memory_view = memory_path.read_text(encoding="utf-8")
    assert memory_view == memory_before_compaction
    assert "Current Context Compaction" not in memory_view
    assert saved.summary not in memory_view


def test_compaction_projection_failure_rolls_back_new_database_record(
    tmp_path, monkeypatch
):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    messages = [
        store.append_message(
            user_id="alice",
            session_id="thread",
            role="user",
            content="large source request " * 200,
            message_id="u1",
        ),
        store.append_message(
            user_id="alice",
            session_id="thread",
            role="assistant",
            content="large source result " * 200,
            message_id="a1",
        ),
    ]
    record = asyncio.run(
        CompactionEngine(summarizer=None, trigger_tokens=1, target_tokens=120).compact(
            messages,
            covered_user_message_ids=("u1",),
        )
    )
    monkeypatch.setattr(
        "src.memory.store.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(MemoryStoreError, match="Markdown projection failed"):
        store.save_compaction(record)

    assert store.latest_compaction("alice", "thread") is None
