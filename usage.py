"""Persistent usage records, plus the atomic checks that enforce limits against them.

Tables (all in one SQLite file):

  requests           one row per logical request (an idea expanded, or a batch of images)
  provider_attempts  one row per real call to a provider, retries included
  cost_adjustments   manual corrections to a provider's spend, entered by an administrator

Money is whole microdollars (integers; 1 USD = 1,000,000). Nothing in here uses floating
point for money, so ceilings are compared exactly, with no tolerance.

How usage and money are counted (single source of truth, so nothing is counted twice):

  * Spending is derived from provider_attempts. Every attempt, whatever its status
    (succeeded, failed, or still "started" because the app stopped), costs its
    estimated_cost_micros, unless an actual_cost_micros was recorded for it. A failed or
    interrupted call is never assumed to be free.
  * A pending image request holds a reservation of one first attempt per image
    (reserved_cost_micros = cost of one attempt x reserved_images). Each image's share is
    released the moment that image gets its first attempt, because that attempt now holds the
    money. Retries are never covered by a reservation: every retry needs additional budget
    and is checked just before it is made. So an attempt is only ever counted as the attempt,
    and a reservation share is only ever counted until it is replaced by that attempt.
  * The daily image quota counts images, not attempts: an active request counts the images
    it reserved; a finished request counts the images that actually got an attempt.
    Retries of one image never count as extra images.
  * Provider request limits count every real attempt, retries included.
  * Rejected requests have no attempts and are excluded from every count.

Limits are checked and the new reservation or attempt is written inside one
BEGIN IMMEDIATE transaction, so concurrent requests cannot both claim the same
remaining quota or budget.

A request that stays pending with no activity for stale_after_seconds is treated as
abandoned (the app crashed): it is closed as failed/"abandoned", its reservation is
released, and its attempts keep counting as possibly billed. Requests with recent
activity are left alone. config.MIN_STALE_REQUEST_SECONDS guarantees the threshold is
longer than any single provider call or retry pause.

Nothing sensitive is stored: no API keys, no prompt text, no raw provider error messages.

UsageStore is the interface; SqliteUsageStore is the only implementation.
"""
import logging
import math
import sqlite3
import time as _time
import uuid
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import httpx

import config
import money
from errors import AppError, AuthorizationRequiredError, LimitExceededError

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

SCHEMA_VERSION = 3

# metrics a Quota can limit. COST_MICROS is in whole microdollars; the others are counts.
REQUESTS, ATTEMPTS, IMAGES, COST_MICROS = "requests", "attempts", "images", "cost_micros"

_COST_ADJUSTMENTS_DDL = """
CREATE TABLE IF NOT EXISTS cost_adjustments (
    adjustment_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    provider            TEXT NOT NULL,
    amount_micros       INTEGER NOT NULL,
    note                TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
"""

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
    estimated_cost_micros INTEGER,
    actual_cost_micros  INTEGER,
    error_category      TEXT,
    reserved_images     INTEGER NOT NULL DEFAULT 0,
    reserved_cost_micros INTEGER,
    last_activity_at    TEXT
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
    estimated_cost_micros INTEGER,
    actual_cost_micros  INTEGER,
    UNIQUE (request_id, item_ref, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_attempts_started ON provider_attempts (started_at);
CREATE INDEX IF NOT EXISTS idx_attempts_provider ON provider_attempts (provider, started_at);
""" + _COST_ADJUSTMENTS_DDL

# Columns added since schema version 1 (and the old floating-point cost columns they replace).
# Existing databases are upgraded in place; the old columns are left behind, unused.
_ADDED_COLUMNS = {
    "requests": {
        "reserved_images": "INTEGER NOT NULL DEFAULT 0",
        "reserved_cost_micros": "INTEGER",
        "last_activity_at": "TEXT",
        "estimated_cost_micros": "INTEGER",
        "actual_cost_micros": "INTEGER",
    },
    "provider_attempts": {
        "estimated_cost_micros": "INTEGER",
        "actual_cost_micros": "INTEGER",
    },
}
_LEGACY_COPIES = (
    ("requests", "estimated_cost_usd", "estimated_cost_micros"),
    ("requests", "actual_cost_usd", "actual_cost_micros"),
    ("requests", "reserved_cost_usd", "reserved_cost_micros"),
    ("provider_attempts", "estimated_cost_usd", "estimated_cost_micros"),
    ("provider_attempts", "actual_cost_usd", "actual_cost_micros"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """Timestamps are stored as UTC ISO strings, which sort in time order."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def outcome_for(requested: int, succeeded: int) -> str:
    """Final outcome of a request that ran to the end."""
    if succeeded <= 0:
        return FAILED
    return SUCCEEDED if succeeded >= requested else PARTIALLY_SUCCEEDED


def http_status_of(e: Exception) -> Optional[int]:
    code = getattr(getattr(e, "response", None), "status_code", None)
    if not isinstance(code, int):
        code = getattr(e, "code", None)
    return code if isinstance(code, int) else None


def categorize_error(e: Exception) -> str:
    """Short, safe label for an error. The message is deliberately not stored."""
    code = http_status_of(e)
    if code is not None:
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


@dataclass(frozen=True)
class Quota:
    """A ceiling on something counted in the usage database.

    Checked as: current + adding <= ceiling, where current is measured inside the same
    transaction that then records the new request or attempt. Counts are ints; for the
    COST_MICROS metric both numbers are whole microdollars.

    The window is the last `window_seconds`, or the current calendar day in `day_timezone`,
    or (neither) all time. `provider_prefix` limits requests/attempts/cost to one provider
    family ('huggingface' matches 'huggingface/fal-ai'). `message` may use {current},
    {ceiling} and {remaining}.
    """
    code: str
    message: str
    metric: str
    ceiling: int
    adding: int = 0
    provider_prefix: Optional[str] = None
    window_seconds: Optional[int] = None
    day_timezone: Optional[str] = None


class UsageStore(ABC):
    """What the rest of the application may ask of the usage database. Money is whole microdollars."""

    @abstractmethod
    def begin_request(self, operation, provider, model, items_requested, user_id=ANONYMOUS_USER,
                      quotas=(), reserved_images=0, reserved_cost_micros=None) -> str:
        """Atomically check every quota, then record a new pending request holding the given
        reservations. Raises LimitExceededError (and records nothing) if any quota would be exceeded.
        reserved_cost_micros is the cost of one first attempt per reserved image, in total."""

    @abstractmethod
    def reject_request(self, operation, items_requested, error_category, provider=None, model=None,
                       user_id=ANONYMOUS_USER) -> str:
        """Record a request that was refused before any provider call (for diagnostics only)."""

    @abstractmethod
    def finish_request(self, request_id, outcome, items_succeeded, error_category=None) -> bool:
        """Close a pending request and release its reservation. False if it was already closed."""

    @abstractmethod
    def start_attempt(self, request_id, provider, model=None, item_ref=0, estimated_cost_micros=None,
                      quotas=()) -> int:
        """Atomically check the quotas, then record, before the call is made, that a provider call
        is starting. Raises LimitExceededError (and records nothing) if a quota would be exceeded."""

    @abstractmethod
    def finish_attempt(self, attempt_id, status, error_category=None, http_status=None,
                       input_tokens=None, output_tokens=None,
                       estimated_cost_micros=None, actual_cost_micros=None) -> None:
        """Record the result of an attempt."""

    @abstractmethod
    def sweep_stale(self) -> int:
        """Close pending requests with no recent activity as abandoned. Returns how many."""

    @abstractmethod
    def measure(self, quota) -> int:
        """Current value of the quantity a quota limits."""

    @abstractmethod
    def cost_summary(self, provider_prefix) -> dict:
        """Reserved, estimated, confirmed-actual and adjusted spend for a provider family (microdollars)."""

    @abstractmethod
    def add_cost_adjustment(self, provider, amount_micros, note) -> int:
        ...

    @abstractmethod
    def record_actual_cost(self, attempt_id, actual_cost_micros) -> bool:
        ...

    @abstractmethod
    def get_request(self, request_id) -> Optional[dict]:
        ...

    @abstractmethod
    def list_attempts(self, request_id) -> list:
        ...

    @abstractmethod
    def list_requests(self, limit=50) -> list:
        """Most recent requests first."""


def _provider_clause(column, prefix):
    return f"({column} = ? OR {column} LIKE ?)", [prefix, prefix + "/%"]


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


class SqliteUsageStore(UsageStore):
    """SQLite file store. Each operation opens its own short-lived connection, so records
    survive restarts and the store can be used from several threads or processes
    (writes are serialised by SQLite)."""

    def __init__(self, path, clock: Optional[Callable[[], datetime]] = None, stale_after_seconds=900):
        self.path = str(path)
        self._clock = clock or _utc_now
        self.stale_after_seconds = stale_after_seconds
        self._init_schema()

    # ------------------------------------------------------------ plumbing
    def _now(self) -> datetime:
        return self._clock()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # autocommit; we BEGIN explicitly
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_schema(self):
        """Create or upgrade the tables. Does nothing (and writes nothing) if they are current, and
        retries briefly if another process is creating them at the same moment."""
        for attempt in range(40):
            try:
                self._init_schema_once()
                return
            except sqlite3.OperationalError as e:
                if ("locked" not in str(e) and "busy" not in str(e)) or attempt == 39:
                    raise
                _time.sleep(0.05)

    def _init_schema_once(self):
        with closing(self._connect()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Usage database {self.path} was made by a newer version of the app.")
            if version == SCHEMA_VERSION:
                return
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            self._upgrade(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")  # last, so a half-finished upgrade is redone

    @staticmethod
    def _upgrade(conn):
        """Bring a database made by an older version up to the current columns. Safe to repeat."""
        for table, columns in _ADDED_COLUMNS.items():
            existing = _columns(conn, table)
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        for table, old, new in _LEGACY_COPIES:
            if old in _columns(conn, table):
                conn.execute(f"UPDATE {table} SET {new} = CAST(ROUND({old} * 1000000) AS INTEGER)"
                             f" WHERE {old} IS NOT NULL AND {new} IS NULL")
        adjustment_columns = _columns(conn, "cost_adjustments")
        if "amount_usd" in adjustment_columns and "amount_micros" not in adjustment_columns:
            # one transaction, so the old rows are never lost if this is interrupted
            conn.executescript(
                "BEGIN;"
                "ALTER TABLE cost_adjustments RENAME TO cost_adjustments_legacy;"
                + _COST_ADJUSTMENTS_DDL +
                "INSERT INTO cost_adjustments (provider, amount_micros, note, created_at)"
                " SELECT provider, CAST(ROUND(amount_usd * 1000000) AS INTEGER), note, created_at"
                " FROM cost_adjustments_legacy;"
                "COMMIT;")

    def _write(self, fn):
        """Run fn(conn) inside one write transaction; everything is undone if it raises."""
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

    # ------------------------------------------------------------ measuring
    @staticmethod
    def _since_op(quota):
        """Rolling windows exclude an event exactly window_seconds old; a day includes its first instant."""
        return ">" if quota.window_seconds else ">="

    @staticmethod
    def _window(quota, now):
        """(start of the counting window, or None; seconds until the window frees up, or None)."""
        if quota.window_seconds:
            return now - timedelta(seconds=quota.window_seconds), None
        if quota.day_timezone:
            tz = ZoneInfo(quota.day_timezone)
            local = now.astimezone(tz)
            start = datetime.combine(local.date(), time.min, tzinfo=tz)
            next_start = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=tz)
            return start.astimezone(timezone.utc), max(1, math.ceil((next_start - now).total_seconds()))
        return None, None

    def _measure(self, conn, quota, now) -> int:
        since, _ = self._window(quota, now)
        since_iso = _iso(since) if since else None
        op = self._since_op(quota)
        if quota.metric == REQUESTS:
            sql, args = "SELECT COUNT(*) FROM requests WHERE outcome != 'rejected'", []
            if since_iso:
                sql, args = sql + f" AND created_at {op} ?", [since_iso]
            return conn.execute(sql, args).fetchone()[0]
        if quota.metric == ATTEMPTS:
            sql, args = "SELECT COUNT(*) FROM provider_attempts WHERE 1=1", []
            if since_iso:
                sql, args = sql + f" AND started_at {op} ?", args + [since_iso]
            if quota.provider_prefix:
                clause, cargs = _provider_clause("provider", quota.provider_prefix)
                sql, args = sql + " AND " + clause, args + cargs
            return conn.execute(sql, args).fetchone()[0]
        if quota.metric == IMAGES:
            sql = (
                "SELECT COALESCE(SUM(CASE WHEN r.outcome = 'pending' THEN r.reserved_images ELSE"
                " (SELECT COUNT(DISTINCT a.item_ref) FROM provider_attempts a WHERE a.request_id = r.request_id)"
                " END), 0) FROM requests r WHERE r.operation = 'image_generation' AND r.outcome != 'rejected'")
            args = []
            if since_iso:
                sql, args = sql + f" AND r.created_at {op} ?", [since_iso]
            return conn.execute(sql, args).fetchone()[0]
        if quota.metric == COST_MICROS:
            return self._committed_cost(conn, quota.provider_prefix)["committed"]
        raise ValueError(f"Unknown quota metric {quota.metric!r}.")

    def _committed_cost(self, conn, prefix) -> dict:
        """Money already spent or held for a provider family, all time (no monthly reset)."""
        a_clause, a_args = _provider_clause("a.provider", prefix)
        estimated = conn.execute(
            f"SELECT COALESCE(SUM(COALESCE(a.estimated_cost_micros, 0)), 0) FROM provider_attempts a"
            f" WHERE a.actual_cost_micros IS NULL AND {a_clause}", a_args).fetchone()[0]
        actual = conn.execute(
            f"SELECT COALESCE(SUM(a.actual_cost_micros), 0) FROM provider_attempts a"
            f" WHERE a.actual_cost_micros IS NOT NULL AND {a_clause}", a_args).fetchone()[0]
        # Pending requests still hold one first-attempt share for every image not yet attempted.
        r_clause, r_args = _provider_clause("r.provider", prefix)
        reserved = conn.execute(
            "SELECT COALESCE(SUM((r.reserved_cost_micros / r.reserved_images) * MAX(r.reserved_images -"
            " (SELECT COUNT(DISTINCT a.item_ref) FROM provider_attempts a WHERE a.request_id = r.request_id), 0)), 0)"
            f" FROM requests r WHERE r.outcome = 'pending' AND r.reserved_images > 0"
            f" AND r.reserved_cost_micros IS NOT NULL AND {r_clause}", r_args).fetchone()[0]
        j_clause, j_args = _provider_clause("provider", prefix)
        adjustments = conn.execute(
            f"SELECT COALESCE(SUM(amount_micros), 0) FROM cost_adjustments WHERE {j_clause}", j_args).fetchone()[0]
        return {"reserved_remaining": reserved, "estimated_consumed": estimated, "confirmed_actual": actual,
                "adjustments": adjustments, "committed": reserved + estimated + actual + adjustments}

    def _check(self, conn, quota, now, adding):
        """Raise LimitExceededError if adding `adding` to the quota's quantity would pass its ceiling."""
        if quota.metric == COST_MICROS and adding <= 0:
            return  # already covered by a reservation that was authorised earlier
        current = self._measure(conn, quota, now)
        if current + adding <= quota.ceiling:
            return
        fmt = money.format_usd if quota.metric == COST_MICROS else (lambda v: f"{v:g}")
        message = quota.message.format(current=fmt(current), ceiling=fmt(quota.ceiling),
                                       remaining=fmt(max(quota.ceiling - current, 0)))
        raise LimitExceededError(quota.code, message, retry_after_seconds=self._retry_after(conn, quota, now),
                                 metric=quota.metric, current=current, ceiling=quota.ceiling)

    def _retry_after(self, conn, quota, now) -> Optional[int]:
        since, until_reset = self._window(quota, now)
        if until_reset is not None:
            return until_reset
        if since is None or quota.metric not in (REQUESTS, ATTEMPTS):
            return None
        if quota.metric == REQUESTS:
            oldest = conn.execute("SELECT MIN(created_at) FROM requests WHERE outcome != 'rejected'"
                                  " AND created_at > ?", [_iso(since)]).fetchone()[0]
        else:
            sql, args = "SELECT MIN(started_at) FROM provider_attempts WHERE started_at > ?", [_iso(since)]
            if quota.provider_prefix:
                clause, cargs = _provider_clause("provider", quota.provider_prefix)
                sql, args = sql + " AND " + clause, args + cargs
            oldest = conn.execute(sql, args).fetchone()[0]
        if not oldest:
            return None
        frees_at = datetime.fromisoformat(oldest) + timedelta(seconds=quota.window_seconds)
        return max(1, math.ceil((frees_at - now).total_seconds()))

    # ------------------------------------------------------------ requests
    def begin_request(self, operation, provider, model, items_requested, user_id=ANONYMOUS_USER,
                      quotas=(), reserved_images=0, reserved_cost_micros=None) -> str:
        if reserved_cost_micros is not None:
            money.require_micros(reserved_cost_micros, "reserved_cost_micros")
            if reserved_images <= 0 or reserved_cost_micros % reserved_images:
                raise ValueError("A cost reservation must be a whole number of microdollars per reserved image.")
        self.sweep_stale()  # release reservations of crashed requests before checking limits
        request_id = uuid.uuid4().hex

        def insert(conn):
            now = self._now()
            for quota in quotas:
                self._check(conn, quota, now, quota.adding)
            conn.execute(
                "INSERT INTO requests (request_id, user_id, created_at, operation, provider, model,"
                " items_requested, outcome, reserved_images, reserved_cost_micros, last_activity_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user_id, _iso(now), operation, provider, model, items_requested, PENDING,
                 reserved_images, reserved_cost_micros, _iso(now)))
        self._write(insert)
        return request_id

    def reject_request(self, operation, items_requested, error_category, provider=None, model=None,
                       user_id=ANONYMOUS_USER) -> str:
        request_id = uuid.uuid4().hex
        stamp = _iso(self._now())

        def insert(conn):
            conn.execute(
                "INSERT INTO requests (request_id, user_id, created_at, finished_at, operation, provider, model,"
                " items_requested, outcome, error_category, last_activity_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user_id, stamp, stamp, operation, provider, model,
                 max(int(items_requested or 0), 0), REJECTED, error_category, stamp))
        self._write(insert)
        return request_id

    def _close(self, conn, request_id, outcome, items_succeeded, error_category) -> bool:
        """Close a pending request: set the outcome, release its reservation, total its costs."""
        totals = conn.execute(
            "SELECT SUM(COALESCE(estimated_cost_micros, 0)), SUM(actual_cost_micros)"
            " FROM provider_attempts WHERE request_id = ?", (request_id,)).fetchone()
        stamp = _iso(self._now())
        cur = conn.execute(
            "UPDATE requests SET outcome = ?, items_succeeded = ?, error_category = ?, finished_at = ?,"
            " last_activity_at = ?, estimated_cost_micros = ?, actual_cost_micros = ?"
            " WHERE request_id = ? AND outcome = ?",
            (outcome, items_succeeded, error_category, stamp, stamp, totals[0], totals[1], request_id, PENDING))
        return cur.rowcount == 1

    def finish_request(self, request_id, outcome, items_succeeded, error_category=None) -> bool:
        if outcome in (PENDING, REJECTED):
            raise ValueError(f"finish_request cannot set outcome {outcome!r}.")
        return self._write(lambda conn: self._close(conn, request_id, outcome, items_succeeded, error_category))

    def sweep_stale(self) -> int:
        """Close requests that were left 'pending' by a crash, and release what they were holding.

        A request that is genuinely running keeps touching `last_activity_at` (when each provider
        call starts and ends). One that has been silent for longer than `stale_after_seconds` can
        only be a request whose process died, so it is closed as failed / 'abandoned'. Closing it
        frees its reservation (its daily images and unused budget shares), which would otherwise
        stay locked forever. Called before every new request is authorised, and by
        `admin.py sweep-stale`. Returns how many requests were closed.
        """
        def sweep(conn):
            # anything idle since before this moment is stale; a live request is newer than this
            cutoff = _iso(self._now() - timedelta(seconds=self.stale_after_seconds))
            # only 'pending' requests are candidates, so finished ones and rejected ones are never touched.
            # COALESCE covers rows written before last_activity_at existed: fall back to created_at.
            stale = conn.execute(
                "SELECT request_id FROM requests WHERE outcome = 'pending'"
                " AND COALESCE(last_activity_at, created_at) < ?", (cutoff,)).fetchall()
            for row in stale:
                rid = row["request_id"]
                # record how many images had really succeeded before the crash (one per distinct image)
                done = conn.execute(
                    "SELECT COUNT(DISTINCT item_ref) FROM provider_attempts WHERE request_id = ?"
                    " AND status = 'succeeded'", (rid,)).fetchone()[0]
                # Attempts still "started" (the call never reported back) are deliberately left as they
                # are: their result is unknown, so they keep counting as possibly billed. Closing the
                # request only releases the reservation, never the cost of calls that may have been made.
                # _close only updates rows that are still 'pending', so a finished request's result is
                # never overwritten.
                self._close(conn, rid, FAILED, done, "abandoned")
            return len(stale)
        # one write transaction: concurrent sweeps and new authorisations can't see a half-swept state
        return self._write(sweep)

    # ------------------------------------------------------------ attempts
    def start_attempt(self, request_id, provider, model=None, item_ref=0, estimated_cost_micros=None,
                      quotas=()) -> int:
        if estimated_cost_micros is not None:
            money.require_micros(estimated_cost_micros, "estimated_cost_micros")

        def insert(conn):
            now = self._now()
            row = conn.execute("SELECT outcome, reserved_images, reserved_cost_micros FROM requests"
                               " WHERE request_id = ?", (request_id,)).fetchone()
            if row is None or row["outcome"] != PENDING:
                raise ValueError("Attempts can only be added to a pending request.")
            tries = conn.execute("SELECT COUNT(*) FROM provider_attempts WHERE request_id = ? AND item_ref = ?",
                                 (request_id, item_ref)).fetchone()[0]
            items_started = conn.execute("SELECT COUNT(DISTINCT item_ref) FROM provider_attempts"
                                         " WHERE request_id = ?", (request_id,)).fetchone()[0]
            number = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM provider_attempts"
                " WHERE request_id = ? AND item_ref = ?", (request_id, item_ref)).fetchone()[0]
            # The reservation holds one first attempt per image. This attempt uses that share only if it is
            # the first for its image; a retry is never covered and needs fresh budget.
            cost = estimated_cost_micros or 0
            share = 0
            if row["reserved_images"] > 0 and row["reserved_cost_micros"] is not None:
                if tries == 0 and items_started < row["reserved_images"]:
                    share = row["reserved_cost_micros"] // row["reserved_images"]
            extra_cost = max(cost - share, 0)
            for quota in quotas:
                self._check(conn, quota, now, extra_cost if quota.metric == COST_MICROS else quota.adding)
            cur = conn.execute(
                "INSERT INTO provider_attempts (request_id, item_ref, attempt_number, provider, model,"
                " started_at, status, estimated_cost_micros) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, item_ref, number, provider, model, _iso(now), ATTEMPT_STARTED, estimated_cost_micros))
            conn.execute("UPDATE requests SET last_activity_at = ? WHERE request_id = ?", (_iso(now), request_id))
            return cur.lastrowid
        return self._write(insert)

    def finish_attempt(self, attempt_id, status, error_category=None, http_status=None,
                       input_tokens=None, output_tokens=None,
                       estimated_cost_micros=None, actual_cost_micros=None) -> None:
        if status not in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED):
            raise ValueError(f"Invalid attempt status {status!r}.")
        for name, value in (("estimated_cost_micros", estimated_cost_micros),
                            ("actual_cost_micros", actual_cost_micros)):
            if value is not None:
                money.require_micros(value, name)

        def update(conn):
            stamp = _iso(self._now())
            conn.execute(
                "UPDATE provider_attempts SET status = ?, finished_at = ?, error_category = ?, http_status = ?,"
                " input_tokens = ?, output_tokens = ?,"
                " estimated_cost_micros = COALESCE(?, estimated_cost_micros), actual_cost_micros = ?"
                " WHERE attempt_id = ? AND status = 'started'",
                (status, stamp, error_category, http_status, input_tokens, output_tokens,
                 estimated_cost_micros, actual_cost_micros, attempt_id))
            conn.execute(
                "UPDATE requests SET last_activity_at = ? WHERE request_id ="
                " (SELECT request_id FROM provider_attempts WHERE attempt_id = ?) AND outcome = 'pending'",
                (stamp, attempt_id))
        self._write(update)

    # ------------------------------------------------------------ money and administration
    def measure(self, quota) -> int:
        with closing(self._connect()) as conn:
            return self._measure(conn, quota, self._now())

    def cost_summary(self, provider_prefix) -> dict:
        with closing(self._connect()) as conn:
            return self._committed_cost(conn, provider_prefix)

    def add_cost_adjustment(self, provider, amount_micros, note) -> int:
        money.require_micros(amount_micros, "amount_micros")
        if not note or not note.strip():
            raise ValueError("A cost adjustment needs a note explaining why.")

        def insert(conn):
            cur = conn.execute("INSERT INTO cost_adjustments (provider, amount_micros, note, created_at)"
                               " VALUES (?, ?, ?, ?)", (provider, amount_micros, note.strip(), _iso(self._now())))
            return cur.lastrowid
        return self._write(insert)

    def record_actual_cost(self, attempt_id, actual_cost_micros) -> bool:
        money.require_micros(actual_cost_micros, "actual_cost_micros")
        if actual_cost_micros < 0:
            raise ValueError("The actual cost must be at least 0.")

        def update(conn):
            row = conn.execute("SELECT request_id FROM provider_attempts WHERE attempt_id = ?",
                               (attempt_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE provider_attempts SET actual_cost_micros = ? WHERE attempt_id = ?",
                         (actual_cost_micros, attempt_id))
            conn.execute(
                "UPDATE requests SET actual_cost_micros = (SELECT SUM(actual_cost_micros) FROM provider_attempts"
                " WHERE request_id = ?) WHERE request_id = ?", (row["request_id"], row["request_id"]))
            return True
        return self._write(update)

    # ------------------------------------------------------------ reading
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
    settings = config.get_settings()
    return SqliteUsageStore(settings.usage_db_path, stale_after_seconds=settings.stale_request_seconds)


class AttemptObserver:
    """Passed to provider code, which calls started() before and finished() after every real
    provider call (retries included). It knows nothing about the database layout.

    started() raises if the attempt cannot be recorded or would pass a limit, so a call that
    could not be accounted for is never made. finished() never raises: the call already
    happened, so a failure to record its result is logged instead of masking the real outcome.
    """

    def __init__(self, store: UsageStore, request_id, provider, model=None, item_ref=0,
                 estimated_cost_micros=None, quotas=()):
        self.store, self.request_id = store, request_id
        self.provider, self.model, self.item_ref = provider, model, item_ref
        self.estimated_cost_micros, self.quotas = estimated_cost_micros, tuple(quotas)
        self.attempts = 0
        self.last_error_category = None

    def started(self):
        try:
            attempt_id = self.store.start_attempt(
                self.request_id, self.provider, self.model, self.item_ref,
                estimated_cost_micros=self.estimated_cost_micros, quotas=self.quotas)
        except AppError as e:
            self.last_error_category = e.code
            raise
        except ValueError:
            self.last_error_category = "request_closed"
            raise
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


def require_observer(observer):
    """Provider-calling code uses this to refuse to run without a request that the service layer
    authorised (limits checked, reservation made, usage recorded)."""
    if not isinstance(observer, AttemptObserver):
        raise AuthorizationRequiredError()
