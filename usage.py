"""Persistent usage records: what was requested and every provider call that was made.

Two tables, linked by request_id:

  requests           one row per logical request (an idea expanded, or a batch of images)
  provider_attempts  one row per real call to a provider, retries included

Usage and spending are always derived from provider_attempts. A rejected request
has no attempts, so it can never count as usage. A retry adds an attempt to the
same request instead of creating a new request.

Nothing sensitive is stored: no API keys, no prompt text, no raw provider error
messages. Only counts, ids, timestamps, model names, token counts and short error
categories. Prompts are referred to by their numeric id in the prompt file.

UsageStore is the interface; SqliteUsageStore is the only implementation. Other
modules should depend on UsageStore so it can be replaced (for example by
PostgreSQL) later.
"""
import logging
import sqlite3
import uuid
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)

ANONYMOUS_USER = "local"  # replaced by a real user id once there is authentication

PROMPT_GENERATION = "prompt_generation"
IMAGE_GENERATION = "image_generation"
OPERATIONS = (PROMPT_GENERATION, IMAGE_GENERATION)

PENDING = "pending"
SUCCEEDED = "succeeded"
PARTIALLY_SUCCEEDED = "partially_succeeded"
FAILED = "failed"
REJECTED = "rejected"
OUTCOMES = (PENDING, SUCCEEDED, PARTIALLY_SUCCEEDED, FAILED, REJECTED)

# attempt status: "started" means no result was recorded (still running, or the app stopped);
# it is treated as a call that may have been billed.
ATTEMPT_STARTED = "started"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"

SCHEMA_VERSION = 1

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS requests (
    request_id          TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    finished_at         TEXT,
    operation           TEXT NOT NULL CHECK (operation IN ({", ".join(repr(o) for o in OPERATIONS)})),
    provider            TEXT,
    model               TEXT,
    items_requested     INTEGER NOT NULL CHECK (items_requested >= 0),
    items_succeeded     INTEGER NOT NULL DEFAULT 0 CHECK (items_succeeded >= 0),
    outcome             TEXT NOT NULL CHECK (outcome IN ({", ".join(repr(o) for o in OUTCOMES)})),
    estimated_cost_usd  REAL,
    actual_cost_usd     REAL,
    error_category      TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests (created_at);

CREATE TABLE IF NOT EXISTS provider_attempts (
    attempt_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT NOT NULL REFERENCES requests (request_id),
    item_ref            INTEGER NOT NULL DEFAULT 0,
    attempt_number      INTEGER NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    error_category      TEXT,
    http_status         INTEGER,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    estimated_cost_usd  REAL,
    actual_cost_usd     REAL,
    UNIQUE (request_id, item_ref, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_attempts_started ON provider_attempts (started_at);
CREATE INDEX IF NOT EXISTS idx_attempts_provider ON provider_attempts (provider, started_at);
"""


def now_iso() -> str:
    """Timestamps are stored in UTC; quota windows convert to QUOTA_TIMEZONE when they are queried."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def outcome_for(requested: int, succeeded: int) -> str:
    """Final outcome of a request that ran to the end."""
    if succeeded <= 0:
        return FAILED
    return SUCCEEDED if succeeded >= requested else PARTIALLY_SUCCEEDED


def categorize_error(e: Exception) -> str:
    """Short, safe label for an error. The message is deliberately not stored."""
    code = getattr(getattr(e, "response", None), "status_code", None)
    if not isinstance(code, int):
        code = getattr(e, "code", None)
    if isinstance(code, int):
        if code in (401, 403):
            return "auth"
        if code == 402:
            return "credits_exhausted"
        if code == 429:
            return "rate_limited"
        if code >= 500:
            return "server_error"
        if code >= 400:
            return "client_error"
    if isinstance(e, (httpx.TransportError, TimeoutError, ConnectionError)) or type(e).__name__.endswith("TimeoutError"):
        return "timeout_or_network"
    return "unknown"


def http_status_of(e: Exception) -> Optional[int]:
    code = getattr(getattr(e, "response", None), "status_code", None)
    if not isinstance(code, int):
        code = getattr(e, "code", None)
    return code if isinstance(code, int) else None


class UsageStore(ABC):
    """What the rest of the application may ask of the usage database."""

    @abstractmethod
    def begin_request(self, operation, provider, model, items_requested, user_id=ANONYMOUS_USER) -> str:
        """Record a new pending request and return its request_id."""

    @abstractmethod
    def reject_request(self, operation, items_requested, error_category, provider=None, model=None,
                       user_id=ANONYMOUS_USER) -> str:
        """Record a request that was refused before any provider call (for diagnostics only)."""

    @abstractmethod
    def finish_request(self, request_id, outcome, items_succeeded, error_category=None) -> bool:
        """Close a pending request. Returns False (and changes nothing) if it was already closed."""

    @abstractmethod
    def start_attempt(self, request_id, provider, model=None, item_ref=0) -> int:
        """Record, before the call is made, that a provider call is starting. Returns attempt_id."""

    @abstractmethod
    def finish_attempt(self, attempt_id, status, error_category=None, http_status=None,
                       input_tokens=None, output_tokens=None,
                       estimated_cost_usd=None, actual_cost_usd=None) -> None:
        """Record the result of an attempt."""

    @abstractmethod
    def get_request(self, request_id) -> Optional[dict]:
        ...

    @abstractmethod
    def list_attempts(self, request_id) -> list:
        ...

    @abstractmethod
    def list_requests(self, limit=50) -> list:
        """Most recent requests first."""


class SqliteUsageStore(UsageStore):
    """SQLite file store. Each operation opens its own short-lived connection, so records
    survive restarts and the store can be used from several threads or processes
    (writes are serialised by SQLite)."""

    def __init__(self, path):
        self.path = str(path)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # autocommit; we BEGIN explicitly
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_schema(self):
        with closing(self._connect()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Usage database {self.path} was made by a newer version of the app.")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _write(self, fn):
        """Run fn(conn) inside one write transaction."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

    def _read(self, sql, params=()):
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def begin_request(self, operation, provider, model, items_requested, user_id=ANONYMOUS_USER) -> str:
        request_id = uuid.uuid4().hex

        def insert(conn):
            conn.execute(
                "INSERT INTO requests (request_id, user_id, created_at, operation, provider, model,"
                " items_requested, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user_id, now_iso(), operation, provider, model, items_requested, PENDING))
        self._write(insert)
        return request_id

    def reject_request(self, operation, items_requested, error_category, provider=None, model=None,
                       user_id=ANONYMOUS_USER) -> str:
        request_id = uuid.uuid4().hex
        stamp = now_iso()

        def insert(conn):
            conn.execute(
                "INSERT INTO requests (request_id, user_id, created_at, finished_at, operation, provider, model,"
                " items_requested, outcome, error_category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user_id, stamp, stamp, operation, provider, model,
                 max(int(items_requested or 0), 0), REJECTED, error_category))
        self._write(insert)
        return request_id

    def finish_request(self, request_id, outcome, items_succeeded, error_category=None) -> bool:
        if outcome in (PENDING, REJECTED):
            raise ValueError(f"finish_request cannot set outcome {outcome!r}.")

        def update(conn):
            cur = conn.execute(
                "UPDATE requests SET outcome = ?, items_succeeded = ?, error_category = ?, finished_at = ?"
                " WHERE request_id = ? AND outcome = ?",
                (outcome, items_succeeded, error_category, now_iso(), request_id, PENDING))
            return cur.rowcount == 1
        return self._write(update)

    def start_attempt(self, request_id, provider, model=None, item_ref=0) -> int:
        def insert(conn):
            row = conn.execute(
                "SELECT outcome FROM requests WHERE request_id = ?", (request_id,)).fetchone()
            if row is None or row["outcome"] != PENDING:
                raise ValueError("Attempts can only be added to a pending request.")
            number = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM provider_attempts"
                " WHERE request_id = ? AND item_ref = ?", (request_id, item_ref)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO provider_attempts (request_id, item_ref, attempt_number, provider, model,"
                " started_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (request_id, item_ref, number, provider, model, now_iso(), ATTEMPT_STARTED))
            return cur.lastrowid
        return self._write(insert)

    def finish_attempt(self, attempt_id, status, error_category=None, http_status=None,
                       input_tokens=None, output_tokens=None,
                       estimated_cost_usd=None, actual_cost_usd=None) -> None:
        if status not in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED):
            raise ValueError(f"Invalid attempt status {status!r}.")

        def update(conn):
            conn.execute(
                "UPDATE provider_attempts SET status = ?, finished_at = ?, error_category = ?, http_status = ?,"
                " input_tokens = ?, output_tokens = ?, estimated_cost_usd = ?, actual_cost_usd = ?"
                " WHERE attempt_id = ? AND status = 'started'",
                (status, now_iso(), error_category, http_status, input_tokens, output_tokens,
                 estimated_cost_usd, actual_cost_usd, attempt_id))
        self._write(update)

    def get_request(self, request_id) -> Optional[dict]:
        rows = self._read("SELECT * FROM requests WHERE request_id = ?", (request_id,))
        return rows[0] if rows else None

    def list_attempts(self, request_id) -> list:
        return self._read("SELECT * FROM provider_attempts WHERE request_id = ?"
                          " ORDER BY item_ref, attempt_number", (request_id,))

    def list_requests(self, limit=50) -> list:
        return self._read("SELECT * FROM requests ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))


def get_store() -> UsageStore:
    """The store configured by USAGE_DB_PATH."""
    return SqliteUsageStore(config.get_settings().usage_db_path)


class AttemptObserver:
    """Passed to provider code, which calls started() before and finished() after every real
    provider call (retries included). It knows nothing about the database layout.

    started() raises if the attempt cannot be recorded, so a call that could not be accounted
    for is never made. finished() never raises: the call already happened, so a failure to
    record its result is logged instead of masking the real outcome.
    """

    def __init__(self, store: UsageStore, request_id, provider, model=None, item_ref=0):
        self.store, self.request_id = store, request_id
        self.provider, self.model, self.item_ref = provider, model, item_ref
        self.attempts = 0
        self.last_error_category = None

    def started(self):
        attempt_id = self.store.start_attempt(self.request_id, self.provider, self.model, self.item_ref)
        self.attempts += 1
        return attempt_id

    def finished(self, attempt_id, error=None, input_tokens=None, output_tokens=None):
        if error is None:
            status, category, http = ATTEMPT_SUCCEEDED, None, None
        else:
            status, category, http = ATTEMPT_FAILED, categorize_error(error), http_status_of(error)
            self.last_error_category = category
        try:
            self.store.finish_attempt(attempt_id, status, error_category=category, http_status=http,
                                      input_tokens=input_tokens, output_tokens=output_tokens)
        except Exception:
            logger.error("Could not record the result of provider attempt %s", attempt_id)
