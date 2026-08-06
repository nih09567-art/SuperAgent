"""Transactional SQLite persistence for short- and long-term Agent Memory."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from .models import (
    CompactionRecord,
    LongTermMemory,
    LongTermMemoryStatus,
    MemoryMessage,
    parse_datetime,
    utc_now,
)
from .utils import (
    contains_secret,
    normalize_content,
    redact_secrets,
    safe_identifier,
    to_json_safe,
)


class MemoryStoreError(RuntimeError):
    pass


class MemoryScopeError(MemoryStoreError):
    pass


class MessageIdConflictError(MemoryStoreError):
    pass


class SecretDetectedError(MemoryStoreError, ValueError):
    pass


def _iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(to_json_safe(value), ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class MemoryStore:
    """SQLite repository with WAL and short write transactions.

    A new connection is opened per operation so the same store object is safe to
    use from FastAPI workers and ``asyncio.to_thread`` calls.
    """

    def __init__(self, path: str | Path) -> None:
        requested = Path(path).expanduser()
        if requested.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            requested = requested / "memory.sqlite3"
        self.path = requested.resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._projection_lock_guard = threading.Lock()
        self._projection_locks: dict[str, threading.Lock] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_messages (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    workflow_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (user_id, session_id, message_id),
                    UNIQUE (user_id, session_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_messages_scope_sequence
                ON memory_messages(user_id, session_id, sequence);

                CREATE TABLE IF NOT EXISTS memory_compactions (
                    compaction_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    last_message_id TEXT NOT NULL,
                    boundary_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '{}',
                    hook_results_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_compactions_scope_sequence
                ON memory_compactions(user_id, session_id, last_sequence DESC);

                CREATE TABLE IF NOT EXISTS memory_long_term (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    memory_key TEXT,
                    workflow_id TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    superseded_at TEXT,
                    superseded_by TEXT,
                    deleted_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_memory_long_term_user_status
                ON memory_long_term(user_id, status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_long_term_user_key
                ON memory_long_term(user_id, memory_key, status);

                CREATE TABLE IF NOT EXISTS memory_consolidation_watermarks (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    extractor_version TEXT NOT NULL DEFAULT 'llm-taxonomy-v1',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id, extractor_version)
                );

                CREATE TABLE IF NOT EXISTS memory_consolidated_turns (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    extractor_version TEXT NOT NULL DEFAULT 'llm-taxonomy-v1',
                    last_sequence INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (
                        user_id, session_id, turn_id, extractor_version
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_memory_consolidated_turns_sequence
                ON memory_consolidated_turns(
                    user_id, session_id, extractor_version, last_sequence
                );
                """
            )
            self._ensure_columns(
                connection,
                "memory_compactions",
                {
                    "retained_message_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                    "retained_turn_count": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            self._ensure_columns(
                connection,
                "memory_long_term",
                {
                    "memory_value_json": "TEXT",
                    "label": "TEXT",
                    "importance": "REAL NOT NULL DEFAULT 1.0",
                    "decay_class": "TEXT NOT NULL DEFAULT 'medium'",
                    "last_reinforced_at": "TEXT",
                    "reinforcement_count": "INTEGER NOT NULL DEFAULT 0",
                    "source_message_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                    "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                    "extractor_version": "TEXT",
                    "tags_json": "TEXT NOT NULL DEFAULT '[]'",
                },
            )

    @staticmethod
    def _ensure_columns(
        connection: sqlite3.Connection,
        table: str,
        columns: Mapping[str, str],
    ) -> None:
        existing = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _validate_scope(user_id: str, session_id: str | None = None) -> None:
        if not str(user_id).strip():
            raise MemoryScopeError("user_id is required")
        if session_id is not None and not str(session_id).strip():
            raise MemoryScopeError("session_id is required")

    def append_message(
        self,
        message: MemoryMessage | Mapping[str, Any] | None = None,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        role: str | None = None,
        content: str | None = None,
        message_id: str | None = None,
        workflow_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryMessage:
        if message is None:
            if user_id is None or session_id is None or role is None or content is None:
                raise ValueError("message or user_id/session_id/role/content is required")
            message = MemoryMessage(
                message_id=message_id or uuid4().hex,
                user_id=user_id,
                session_id=session_id,
                sequence=0,
                role=role,
                content=content,
                workflow_id=workflow_id,
                metadata=dict(metadata or {}),
            )
        elif isinstance(message, Mapping):
            message = MemoryMessage.from_dict(message)
        self._validate_scope(message.user_id, message.session_id)

        sanitized = redact_secrets(message.content)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT * FROM memory_messages
                    WHERE user_id=? AND session_id=? AND message_id=?
                    """,
                    (message.user_id, message.session_id, message.message_id),
                ).fetchone()
                if existing is not None:
                    stored = self._row_to_message(existing)
                    if stored.role != message.role or stored.content != sanitized:
                        raise MessageIdConflictError(
                            f"message_id {message.message_id!r} has different content"
                        )
                    connection.execute("COMMIT")
                    return stored

                next_sequence = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM memory_messages WHERE user_id=? AND session_id=?
                    """,
                    (message.user_id, message.session_id),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO memory_messages (
                        user_id, session_id, sequence, message_id, role, content,
                        created_at, workflow_id, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.user_id,
                        message.session_id,
                        next_sequence,
                        message.message_id,
                        message.role,
                        sanitized,
                        _iso(message.created_at),
                        message.workflow_id,
                        _json(message.metadata),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return MemoryMessage(
            message_id=message.message_id,
            user_id=message.user_id,
            session_id=message.session_id,
            sequence=int(next_sequence),
            role=message.role,
            content=sanitized,
            created_at=message.created_at,
            workflow_id=message.workflow_id,
            metadata=dict(message.metadata),
        )

    def append_messages(
        self, messages: Iterable[MemoryMessage | Mapping[str, Any]]
    ) -> list[MemoryMessage]:
        return [self.append_message(message) for message in messages]

    def list_messages(
        self,
        user_id: str,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[MemoryMessage]:
        self._validate_scope(user_id, session_id)
        sql = (
            "SELECT * FROM memory_messages "
            "WHERE user_id=? AND session_id=? AND sequence>? "
            "ORDER BY sequence ASC"
        )
        parameters: list[Any] = [user_id, session_id, after_sequence]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(max(0, int(limit)))
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._row_to_message(row) for row in rows]

    get_messages = list_messages

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MemoryMessage:
        return MemoryMessage(
            message_id=row["message_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            sequence=int(row["sequence"]),
            role=row["role"],
            content=row["content"],
            created_at=parse_datetime(row["created_at"]) or utc_now(),
            workflow_id=row["workflow_id"],
            metadata=dict(_loads(row["metadata_json"], {})),
        )

    def save_compaction(self, record: CompactionRecord) -> CompactionRecord:
        self._validate_scope(record.user_id, record.session_id)
        boundary = record.boundary
        inserted = False
        projection_started = False
        saved = record
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM memory_compactions WHERE compaction_id=?",
                    (record.compaction_id,),
                ).fetchone()
                if existing is not None:
                    saved = self._row_to_compaction(existing)
                else:
                    covered = connection.execute(
                        """
                        SELECT message_id FROM memory_messages
                        WHERE user_id=? AND session_id=? AND sequence=?
                        """,
                        (record.user_id, record.session_id, boundary.last_sequence),
                    ).fetchone()
                    if covered is None or covered["message_id"] != boundary.last_message_id:
                        raise MemoryStoreError("compaction boundary does not match transcript")
                    expected_watermark = record.metadata.get("transcript_watermark_sequence")
                    if expected_watermark is not None:
                        current_watermark = connection.execute(
                            """
                            SELECT COALESCE(MAX(sequence), 0) FROM memory_messages
                            WHERE user_id=? AND session_id=?
                            """,
                            (record.user_id, record.session_id),
                        ).fetchone()[0]
                        if int(current_watermark) != int(expected_watermark):
                            raise MemoryStoreError(
                                "compaction transcript watermark changed before commit"
                            )
                    generation = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM memory_compactions
                            WHERE user_id=? AND session_id=?
                            """,
                            (record.user_id, record.session_id),
                        ).fetchone()[0]
                    ) + 1
                    latest_path = self.compaction_markdown_path(
                        record.user_id, record.session_id
                    )
                    saved = replace(
                        record,
                        metadata={
                            **record.metadata,
                            "compaction_generation": generation,
                            "markdown_projection_path": str(latest_path),
                        },
                    )
                    connection.execute(
                        """
                        INSERT INTO memory_compactions (
                            compaction_id, user_id, session_id, last_sequence,
                            last_message_id, boundary_json, summary, attachments_json,
                            hook_results_json, metadata_json, retained_message_ids_json,
                            retained_turn_count, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            saved.compaction_id,
                            saved.user_id,
                            saved.session_id,
                            boundary.last_sequence,
                            boundary.last_message_id,
                            _json(boundary),
                            saved.summary,
                            _json(saved.attachments),
                            _json(saved.hook_results),
                            _json(saved.metadata),
                            _json(boundary.retained_message_ids),
                            int(boundary.retained_turn_count),
                            _iso(saved.created_at),
                        ),
                    )
                    inserted = True
                self._expire_records(connection, saved.user_id)
                long_term_rows = connection.execute(
                    """
                    SELECT * FROM memory_long_term
                    WHERE user_id=? AND status='active'
                    ORDER BY updated_at DESC, memory_id ASC
                    """,
                    (saved.user_id,),
                ).fetchall()
                compaction_rows = connection.execute(
                    """
                    SELECT * FROM memory_compactions
                    WHERE user_id=?
                    ORDER BY session_id ASC, last_sequence DESC, created_at DESC
                    """,
                    (saved.user_id,),
                ).fetchall()
                projection_compactions: dict[str, CompactionRecord] = {}
                for row in compaction_rows:
                    projection_compactions.setdefault(
                        row["session_id"], self._row_to_compaction(row)
                    )
                projection_record = projection_compactions[saved.session_id]
                projection_started = True
                self.project_compaction_markdown(projection_record)
                self.project_markdown(
                    saved.user_id,
                    records_override=[
                        self._row_to_long_term(row) for row in long_term_rows
                    ],
                    compactions_override=list(projection_compactions.values()),
                )
                connection.execute("COMMIT")
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if projection_started:
                    self._restore_compaction_projections(
                        saved,
                        remove_immutable=inserted,
                    )
                    raise MemoryStoreError(
                        "compaction Markdown projection failed"
                    ) from exc
                raise
        return saved

    commit_compaction = save_compaction

    def latest_compaction(
        self, user_id: str, session_id: str
    ) -> CompactionRecord | None:
        self._validate_scope(user_id, session_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_compactions
                WHERE user_id=? AND session_id=?
                ORDER BY last_sequence DESC, created_at DESC LIMIT 1
                """,
                (user_id, session_id),
            ).fetchone()
        return self._row_to_compaction(row) if row is not None else None

    get_latest_compaction = latest_compaction

    def list_compactions(
        self, user_id: str, session_id: str
    ) -> list[CompactionRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_compactions
                WHERE user_id=? AND session_id=?
                ORDER BY last_sequence ASC, created_at ASC
                """,
                (user_id, session_id),
            ).fetchall()
        return [self._row_to_compaction(row) for row in rows]

    def latest_compactions_for_user(self, user_id: str) -> list[CompactionRecord]:
        self._validate_scope(user_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_compactions
                WHERE user_id=?
                ORDER BY session_id ASC, last_sequence DESC, created_at DESC
                """,
                (user_id,),
            ).fetchall()
        latest = {}
        for row in rows:
            latest.setdefault(row["session_id"], self._row_to_compaction(row))
        return list(latest.values())

    @staticmethod
    def _row_to_compaction(row: sqlite3.Row) -> CompactionRecord:
        return CompactionRecord.from_dict(
            {
                "compaction_id": row["compaction_id"],
                "user_id": row["user_id"],
                "session_id": row["session_id"],
                "boundary": _loads(row["boundary_json"], {}),
                "summary": row["summary"],
                "attachments": _loads(row["attachments_json"], {}),
                "hook_results": _loads(row["hook_results_json"], []),
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
        )

    def messages_after_compaction(
        self, user_id: str, session_id: str
    ) -> tuple[CompactionRecord | None, list[MemoryMessage]]:
        record = self.latest_compaction(user_id, session_id)
        after = record.boundary.last_sequence if record else 0
        return record, self.list_messages(user_id, session_id, after_sequence=after)

    def remember(
        self,
        *,
        user_id: str,
        content: str,
        kind: str = "fact",
        memory_key: str | None = None,
        scope: str = "user",
        confidence: float = 1.0,
        provenance: Mapping[str, Any] | None = None,
        workflow_id: str | None = None,
        session_id: str | None = None,
        expires_at: datetime | str | None = None,
        metadata: Mapping[str, Any] | None = None,
        memory_id: str | None = None,
        value: Any = None,
        label: str | None = None,
        importance: float = 1.0,
        decay_class: str = "medium",
        last_reinforced_at: datetime | str | None = None,
        reinforcement_count: int = 0,
        source_message_ids: Sequence[str] | None = None,
        sensitivity: str = "normal",
        extractor_version: str | None = None,
        tags: Sequence[str] | None = None,
        status: str = "active",
    ) -> LongTermMemory:
        self._validate_scope(user_id)
        clean = str(content).strip()
        if not clean:
            raise ValueError("memory content is required")
        if contains_secret(clean):
            raise SecretDetectedError("secret-looking content cannot be remembered")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not 0.0 <= float(importance) <= 1.0:
            raise ValueError("importance must be between 0 and 1")
        allowed_kinds = {"fact", "preference", "constraint", "decision", "episodic"}
        if kind not in allowed_kinds:
            raise ValueError(f"unsupported memory kind: {kind}")
        if not str(scope).strip():
            raise ValueError("memory scope is required")
        lifecycle_status = str(status or "active").casefold()
        if lifecycle_status not in {"active", "pending"}:
            raise ValueError("new memory status must be active or pending")
        normalized = normalize_content(clean)
        normalized_tags = tuple(
            sorted({str(item).strip().casefold() for item in tags or () if str(item).strip()})
        )
        normalized_label = redact_secrets(str(label or clean).strip())
        if contains_secret(normalized_label):
            raise SecretDetectedError("secret-looking label cannot be remembered")
        now = utc_now()
        identifier = memory_id or uuid4().hex
        expires = parse_datetime(expires_at)
        reinforced = parse_datetime(last_reinforced_at) or now

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if memory_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM memory_long_term
                        WHERE user_id=? AND memory_key=? AND scope=? AND status=?
                        ORDER BY updated_at DESC LIMIT 1
                        """,
                        (user_id, memory_key, scope, lifecycle_status),
                    ).fetchone()
                else:
                    existing = connection.execute(
                        """
                        SELECT * FROM memory_long_term
                        WHERE user_id=? AND normalized_content=? AND status=?
                        ORDER BY updated_at DESC LIMIT 1
                        """,
                        (user_id, normalized, lifecycle_status),
                    ).fetchone()
                if existing is not None and (
                    not memory_key or normalize_content(existing["content"]) == normalized
                ):
                    existing_metadata = dict(_loads(existing["metadata_json"], {}))
                    incoming_metadata = dict(metadata or {})
                    existing_sources = list(
                        existing_metadata.get("source_conversations") or []
                    )
                    known_turns = {
                        str(item.get("turn_id") or "")
                        for item in existing_sources
                        if isinstance(item, Mapping)
                    }
                    for source in incoming_metadata.get("source_conversations") or []:
                        if not isinstance(source, Mapping):
                            continue
                        turn_id = str(source.get("turn_id") or "")
                        if turn_id and turn_id in known_turns:
                            continue
                        existing_sources.append(dict(source))
                        if turn_id:
                            known_turns.add(turn_id)
                    merged_metadata = {**existing_metadata, **incoming_metadata}
                    merged_metadata["source_conversations"] = existing_sources[-10:]
                    merged_provenance = {
                        **dict(_loads(existing["provenance_json"], {})),
                        **dict(provenance or {}),
                    }
                    existing_source_ids = list(
                        _loads(existing["source_message_ids_json"], [])
                    )
                    merged_source_ids = tuple(
                        dict.fromkeys(
                            [str(item) for item in existing_source_ids]
                            + [str(item) for item in source_message_ids or ()]
                        )
                    )
                    connection.execute(
                        """
                        UPDATE memory_long_term
                        SET confidence=?, importance=?, label=?, memory_value_json=?,
                            last_reinforced_at=?, reinforcement_count=?, updated_at=?,
                            source_message_ids_json=?, tags_json=?, extractor_version=?,
                            provenance_json=?, metadata_json=?
                        WHERE memory_id=? AND user_id=?
                        """,
                        (
                            max(float(existing["confidence"]), float(confidence)),
                            max(float(existing["importance"] or 0.0), float(importance)),
                            normalized_label,
                            _json(value) if value is not None else None,
                            _iso(reinforced),
                            int(existing["reinforcement_count"] or 0)
                            + max(1, int(reinforcement_count or 0)),
                            _iso(now),
                            _json(merged_source_ids),
                            _json(normalized_tags),
                            extractor_version,
                            _json(merged_provenance),
                            _json(merged_metadata),
                            existing["memory_id"],
                            user_id,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM memory_long_term WHERE memory_id=?",
                        (existing["memory_id"],),
                    ).fetchone()
                    connection.execute("COMMIT")
                    return self._row_to_long_term(row)
                if existing is not None and lifecycle_status == "active":
                    connection.execute(
                        """
                        UPDATE memory_long_term
                        SET status='superseded', superseded_at=?, superseded_by=?,
                            updated_at=? WHERE memory_id=? AND user_id=?
                        """,
                        (_iso(now), identifier, _iso(now), existing["memory_id"], user_id),
                    )
                connection.execute(
                    """
                    INSERT INTO memory_long_term (
                        memory_id, user_id, content, normalized_content, kind,
                        scope, confidence, provenance_json, status, memory_key,
                        workflow_id, session_id, created_at, updated_at,
                        expires_at, superseded_at, superseded_by, deleted_at,
                        metadata_json, memory_value_json, label, importance, decay_class,
                        last_reinforced_at, reinforcement_count, source_message_ids_json,
                        sensitivity, extractor_version, tags_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        user_id,
                        clean,
                        normalized,
                        kind,
                        scope,
                        float(confidence),
                        _json(dict(provenance or {})),
                        lifecycle_status,
                        memory_key,
                        workflow_id,
                        session_id,
                        _iso(now),
                        _iso(now),
                        _iso(expires) if expires else None,
                        _json(dict(metadata or {})),
                        _json(value) if value is not None else None,
                        normalized_label,
                        float(importance),
                        str(decay_class or "medium"),
                        _iso(reinforced),
                        max(0, int(reinforcement_count or 0)),
                        _json(tuple(source_message_ids or ())),
                        str(sensitivity or "normal"),
                        extractor_version,
                        _json(normalized_tags),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM memory_long_term WHERE memory_id=?", (identifier,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return self._row_to_long_term(row)

    def _expire_records(self, connection: sqlite3.Connection, user_id: str) -> None:
        now = _iso()
        connection.execute(
            """
            UPDATE memory_long_term SET status='expired', updated_at=?
            WHERE user_id=? AND status='active' AND expires_at IS NOT NULL
              AND expires_at<=?
            """,
            (now, user_id, now),
        )

    def list_long_term(
        self,
        user_id: str,
        *,
        statuses: Sequence[str] = ("active",),
    ) -> list[LongTermMemory]:
        self._validate_scope(user_id)
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_records(connection, user_id)
                rows = connection.execute(
                    f"""
                    SELECT * FROM memory_long_term
                    WHERE user_id=? AND status IN ({placeholders})
                    ORDER BY updated_at DESC, memory_id ASC
                    """,
                    [user_id, *statuses],
                ).fetchall()
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return [self._row_to_long_term(row) for row in rows]

    def get_long_term(self, user_id: str, memory_id: str) -> LongTermMemory | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memory_long_term WHERE user_id=? AND memory_id=?",
                (user_id, memory_id),
            ).fetchone()
        return self._row_to_long_term(row) if row is not None else None

    def delete_long_term(self, user_id: str, memory_id: str) -> bool:
        self._validate_scope(user_id)
        now = _iso()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE memory_long_term SET status='deleted', deleted_at=?, updated_at=?
                WHERE user_id=? AND memory_id=? AND status!='deleted'
                """,
                (now, now, user_id, memory_id),
            )
        return cursor.rowcount > 0

    forget = delete_long_term

    def get_consolidation_watermark(
        self, user_id: str, session_id: str, *, extractor_version: str = "llm-taxonomy-v1"
    ) -> int:
        self._validate_scope(user_id, session_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT last_sequence FROM memory_consolidation_watermarks
                WHERE user_id=? AND session_id=? AND extractor_version=?
                """,
                (user_id, session_id, extractor_version),
            ).fetchone()
        return int(row["last_sequence"] if row else 0)

    def advance_consolidation_watermark(
        self,
        user_id: str,
        session_id: str,
        sequence: int,
        *,
        extractor_version: str = "llm-taxonomy-v1",
    ) -> int:
        self._validate_scope(user_id, session_id)
        now = _iso()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO memory_consolidation_watermarks
                    (user_id, session_id, last_sequence, extractor_version, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, session_id, extractor_version) DO UPDATE SET
                    last_sequence=MAX(last_sequence, excluded.last_sequence),
                    updated_at=excluded.updated_at
                """,
                (user_id, session_id, max(0, int(sequence)), extractor_version, now),
            )
            row = connection.execute(
                """
                SELECT last_sequence FROM memory_consolidation_watermarks
                WHERE user_id=? AND session_id=? AND extractor_version=?
                """,
                (user_id, session_id, extractor_version),
            ).fetchone()
        return int(row["last_sequence"] if row else 0)

    def list_consolidated_turn_ids(
        self,
        user_id: str,
        session_id: str,
        *,
        extractor_version: str = "llm-taxonomy-v1",
    ) -> set[str]:
        self._validate_scope(user_id, session_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT turn_id FROM memory_consolidated_turns
                WHERE user_id=? AND session_id=? AND extractor_version=?
                  AND status='completed'
                """,
                (user_id, session_id, extractor_version),
            ).fetchall()
        return {str(row["turn_id"]) for row in rows}

    def claim_turn_consolidation(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        last_sequence: int,
        *,
        extractor_version: str = "llm-taxonomy-v1",
        lease_seconds: int = 300,
    ) -> bool:
        self._validate_scope(user_id, session_id)
        identifier = str(turn_id).strip()
        if not identifier:
            raise ValueError("turn_id is required")
        now = utc_now()
        stale_before = now - timedelta(seconds=max(1, int(lease_seconds)))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_consolidated_turns (
                    user_id, session_id, turn_id, extractor_version,
                    last_sequence, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'processing', ?)
                ON CONFLICT(
                    user_id, session_id, turn_id, extractor_version
                ) DO UPDATE SET
                    last_sequence=MAX(last_sequence, excluded.last_sequence),
                    status='processing',
                    updated_at=excluded.updated_at
                WHERE memory_consolidated_turns.status='processing'
                  AND memory_consolidated_turns.updated_at <= ?
                """,
                (
                    user_id,
                    session_id,
                    identifier,
                    extractor_version,
                    max(0, int(last_sequence)),
                    _iso(now),
                    _iso(stale_before),
                ),
            )
        return cursor.rowcount == 1

    def release_turn_consolidation_claim(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        *,
        extractor_version: str = "llm-taxonomy-v1",
    ) -> None:
        self._validate_scope(user_id, session_id)
        identifier = str(turn_id).strip()
        if not identifier:
            raise ValueError("turn_id is required")
        with closing(self._connect()) as connection:
            connection.execute(
                """
                DELETE FROM memory_consolidated_turns
                WHERE user_id=? AND session_id=? AND turn_id=?
                  AND extractor_version=? AND status='processing'
                """,
                (user_id, session_id, identifier, extractor_version),
            )

    def mark_turn_consolidated(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        last_sequence: int,
        *,
        extractor_version: str = "llm-taxonomy-v1",
    ) -> None:
        self._validate_scope(user_id, session_id)
        identifier = str(turn_id).strip()
        if not identifier:
            raise ValueError("turn_id is required")
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO memory_consolidated_turns (
                    user_id, session_id, turn_id, extractor_version,
                    last_sequence, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'completed', ?)
                ON CONFLICT(
                    user_id, session_id, turn_id, extractor_version
                ) DO UPDATE SET
                    last_sequence=MAX(last_sequence, excluded.last_sequence),
                    status='completed',
                    updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    session_id,
                    identifier,
                    extractor_version,
                    max(0, int(last_sequence)),
                    _iso(),
                ),
            )

    def project_markdown(
        self,
        user_id: str,
        *,
        scope: str | None = None,
        path: str | Path | None = None,
        records_override: Sequence[LongTermMemory] | None = None,
        compactions_override: Sequence[CompactionRecord] | None = None,
    ) -> Path:
        """Atomically materialize active labels as an inspectable MEMORY.md view."""

        self._validate_scope(user_id)
        records = (
            list(records_override)
            if records_override is not None
            else self.list_long_term(user_id)
        )
        compactions = (
            list(compactions_override)
            if compactions_override is not None
            else self.latest_compactions_for_user(user_id)
        )
        if any(record.user_id != user_id for record in records):
            raise MemoryScopeError("long-term projection user mismatch")
        if any(record.user_id != user_id for record in compactions):
            raise MemoryScopeError("compaction projection user mismatch")
        if scope:
            records = [item for item in records if item.scope in {"user", scope}]
        target = Path(path) if path else self.markdown_path(user_id, scope=scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Memory", "", f"<!-- user: {user_id} -->", ""]
        grouped: dict[str, list[LongTermMemory]] = {}
        for record in records:
            grouped.setdefault(record.kind, []).append(record)
        for kind in sorted(grouped):
            lines.extend([f"## {kind}", ""])
            for record in sorted(
                grouped[kind], key=lambda item: (item.memory_key or item.label or item.content)
            ):
                label = redact_secrets(record.label or record.content).strip()
                key = f"`{record.memory_key}` " if record.memory_key else ""
                evidence_at = record.last_reinforced_at or record.updated_at
                lines.append(
                    f"- {key}{label} _(evidence: {evidence_at.isoformat()})_"
                )
            lines.append("")
        if compactions:
            lines.extend(
                [
                    "## Current Context Compaction (Diagnostic Only)",
                    "",
                    "<!-- This section is not a long-term memory record and is not recalled into prompts. -->",
                    "",
                ]
            )
            for record in compactions:
                generation = int(
                    record.metadata.get("compaction_generation") or 0
                )
                lines.extend(
                    [
                        f"### Session `{record.session_id}`",
                        "",
                        f"- Generation: {generation}",
                        f"- Covered through: `{record.boundary.last_message_id}`",
                        f"- Tokens: {record.boundary.token_count_before} -> {record.boundary.token_count_after}",
                        f"- Snapshot: `{record.metadata.get('markdown_projection_path') or ''}`",
                        "",
                        redact_secrets(record.summary),
                        "",
                    ]
                )
        payload = "\n".join(lines).rstrip() + "\n"
        lock_key = str(target.resolve(strict=False)).casefold()
        with self._projection_lock_guard:
            projection_lock = self._projection_locks.setdefault(
                lock_key, threading.Lock()
            )
        with projection_lock:
            fd, temporary = tempfile.mkstemp(
                dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    try:
                        os.remove(temporary)
                    except OSError:
                        pass
        return target

    def project_compaction_markdown(self, record: CompactionRecord) -> Path:
        """Materialize one immutable compaction snapshot and refresh LATEST.md."""

        self._validate_scope(record.user_id, record.session_id)
        messages = self.list_messages(record.user_id, record.session_id)
        by_id = {message.message_id: message for message in messages}
        retained = [
            by_id[message_id]
            for message_id in record.boundary.retained_message_ids
            if message_id in by_id
        ]
        covered_ids = tuple(
            str(item)
            for item in (
                record.metadata.get("covered_message_ids")
                or record.metadata.get("covered_user_message_ids")
                or ()
            )
        )
        generation = int(record.metadata.get("compaction_generation") or 0)
        boundary = record.boundary
        lines = [
            "# Context Compaction",
            "",
            f"<!-- user: {record.user_id}; session: {record.session_id}; compaction: {record.compaction_id} -->",
            "",
            "## Boundary",
            "",
            f"- Kind: {boundary.kind}",
            f"- Trigger: {boundary.trigger}",
            f"- Generation: {generation}",
            f"- Last message: `{boundary.last_message_id}`",
            f"- Last sequence: {boundary.last_sequence}",
            f"- Before: {boundary.token_count_before}",
            f"- After: {boundary.token_count_after}",
            f"- Summary mode: {record.metadata.get('summary_mode') or 'unknown'}",
            f"- Fallback reason: {record.metadata.get('fallback_reason') or 'none'}",
            "",
            "## Structured Summary",
            "",
            redact_secrets(record.summary),
            "",
            "## Retained Turns",
            "",
            f"- Count: {boundary.retained_turn_count}",
        ]
        if retained:
            for message in retained:
                lines.extend(
                    [
                        f"### {message.role} `{message.message_id}`",
                        "",
                        redact_secrets(message.content),
                        "",
                    ]
                )
        else:
            lines.extend(["- None", ""])
        lines.extend(["## Covered Messages", ""])
        lines.extend(f"- `{message_id}`" for message_id in covered_ids)
        if not covered_ids:
            lines.append("- None")
        lines.extend(
            [
                "",
                "## Attachments",
                "",
                "```json",
                redact_secrets(
                    json.dumps(
                        to_json_safe(record.attachments),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                ),
                "```",
                "",
                "## Hook Results",
                "",
                "```json",
                redact_secrets(
                    json.dumps(
                        to_json_safe(record.hook_results),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                ),
                "```",
                "",
            ]
        )
        payload = "\n".join(lines)
        immutable = self.compaction_markdown_path(
            record.user_id,
            record.session_id,
            compaction_id=record.compaction_id,
        )
        latest = self.compaction_markdown_path(record.user_id, record.session_id)
        self._atomic_write_text(immutable, payload)
        self._atomic_write_text(latest, payload)
        return latest

    def compaction_markdown_path(
        self,
        user_id: str,
        session_id: str,
        *,
        compaction_id: str | None = None,
    ) -> Path:
        self._validate_scope(user_id, session_id)
        directory = (
            self.path.parent
            / "memory_views"
            / safe_identifier(user_id, prefix="user")
            / "compactions"
            / safe_identifier(session_id, prefix="session")
        )
        filename = (
            f"{safe_identifier(compaction_id, prefix='compaction')}.md"
            if compaction_id
            else "LATEST.md"
        )
        return directory / filename

    def _atomic_write_text(self, target: Path, payload: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_key = str(target.resolve(strict=False)).casefold()
        with self._projection_lock_guard:
            projection_lock = self._projection_locks.setdefault(
                lock_key, threading.Lock()
            )
        with projection_lock:
            fd, temporary = tempfile.mkstemp(
                dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    try:
                        os.remove(temporary)
                    except OSError:
                        pass

    def _restore_compaction_projections(
        self,
        failed: CompactionRecord,
        *,
        remove_immutable: bool = True,
    ) -> None:
        immutable = self.compaction_markdown_path(
            failed.user_id,
            failed.session_id,
            compaction_id=failed.compaction_id,
        )
        latest_path = self.compaction_markdown_path(
            failed.user_id, failed.session_id
        )
        try:
            if remove_immutable:
                immutable.unlink(missing_ok=True)
            previous = self.latest_compaction(failed.user_id, failed.session_id)
            if previous is None:
                latest_path.unlink(missing_ok=True)
            else:
                self.project_compaction_markdown(previous)
            self.project_markdown(failed.user_id)
        except Exception:
            # The authoritative row was already removed. A later successful
            # projection or process restart can rebuild these diagnostic views.
            pass

    def markdown_path(self, user_id: str, *, scope: str | None = None) -> Path:
        self._validate_scope(user_id)
        target = self.path.parent / "memory_views" / safe_identifier(
            user_id, prefix="user"
        )
        if scope and scope != "user":
            target = target / safe_identifier(scope, prefix="scope")
        return target / "MEMORY.md"

    @staticmethod
    def _row_to_long_term(row: sqlite3.Row) -> LongTermMemory:
        return LongTermMemory.from_dict(
            {
                "memory_id": row["memory_id"],
                "user_id": row["user_id"],
                "content": row["content"],
                "normalized_content": row["normalized_content"],
                "kind": row["kind"],
                "scope": row["scope"],
                "confidence": float(row["confidence"]),
                "provenance": _loads(row["provenance_json"], {}),
                "status": row["status"],
                "memory_key": row["memory_key"],
                "workflow_id": row["workflow_id"],
                "session_id": row["session_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
                "superseded_at": row["superseded_at"],
                "superseded_by": row["superseded_by"],
                "deleted_at": row["deleted_at"],
                "metadata": _loads(row["metadata_json"], {}),
                "value": _loads(row["memory_value_json"], None),
                "label": row["label"],
                "importance": float(row["importance"] or 1.0),
                "decay_class": row["decay_class"] or "medium",
                "last_reinforced_at": row["last_reinforced_at"],
                "reinforcement_count": int(row["reinforcement_count"] or 0),
                "source_message_ids": _loads(row["source_message_ids_json"], []),
                "sensitivity": row["sensitivity"] or "normal",
                "extractor_version": row["extractor_version"],
                "tags": _loads(row["tags_json"], []),
            }
        )


__all__ = [
    "MemoryScopeError",
    "MemoryStore",
    "MemoryStoreError",
    "MessageIdConflictError",
    "SecretDetectedError",
]
