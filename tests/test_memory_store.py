from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from src.memory.models import LongTermMemoryStatus
from src.memory.retrieval import (
    LexicalMemoryRetriever,
    TaggedMemoryRetriever,
    format_untrusted_memories,
)
from src.memory.store import MemoryStore, SecretDetectedError


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
    assert "preference.language" in rebuilt
    assert "preference.report_style" in rebuilt
