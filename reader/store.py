"""SQLite storage (WAL mode) with idempotent upserts. Deleted rows are overwritten on disk."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    subreddit TEXT NOT NULL,
    title TEXT,
    selftext TEXT,
    author_hash TEXT,
    created_utc REAL NOT NULL,
    score INTEGER,
    num_comments INTEGER,
    permalink TEXT,
    flair TEXT,
    retrieved_at REAL NOT NULL,
    comments_fetched_at REAL,
    rechecked_at REAL,
    missing_since REAL
);
CREATE INDEX IF NOT EXISTS posts_sub_created ON posts (subreddit, created_utc);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    parent_id TEXT,
    subreddit TEXT NOT NULL,
    body TEXT,
    author_hash TEXT,
    created_utc REAL NOT NULL,
    score INTEGER,
    permalink TEXT,
    retrieved_at REAL NOT NULL,
    rechecked_at REAL,
    missing_since REAL
);
CREATE INDEX IF NOT EXISTS comments_post ON comments (post_id);

CREATE TABLE IF NOT EXISTS subreddit_state (
    name TEXT PRIMARY KEY,
    last_seen_id TEXT,
    last_seen_utc REAL,
    last_collected_at REAL,
    fail_count INTEGER NOT NULL DEFAULT 0,
    dead_since REAL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    detail TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT,
    posts_added INTEGER NOT NULL DEFAULT 0,
    comments_added INTEGER NOT NULL DEFAULT 0,
    items_deleted INTEGER NOT NULL DEFAULT 0
);

-- One row per HTTP request sent to Reddit, for the rolling 24 hour budget.
CREATE TABLE IF NOT EXISTS requests (
    ts REAL NOT NULL,
    run_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts);

-- Set when Reddit seems to block the account or app as a whole; cleared by `reader reset-block`.
CREATE TABLE IF NOT EXISTS sitewide_block (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    since REAL NOT NULL,
    reason TEXT
);
"""

POST_COLS = (
    "id",
    "subreddit",
    "title",
    "selftext",
    "author_hash",
    "created_utc",
    "score",
    "num_comments",
    "permalink",
    "flair",
    "retrieved_at",
)
COMMENT_COLS = (
    "id",
    "post_id",
    "parent_id",
    "subreddit",
    "body",
    "author_hash",
    "created_utc",
    "score",
    "permalink",
    "retrieved_at",
)
STATE_COLS = {
    "last_seen_id",
    "last_seen_utc",
    "last_collected_at",
    "fail_count",
    "dead_since",
    "last_error",
}
TABLES = {"posts", "comments"}
REQUEST_LOG_DAYS = 7  # request timestamps older than this are dropped


def _upsert_sql(table: str, cols: tuple[str, ...]) -> str:
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "created_utc"))
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA secure_delete=ON")  # zero deleted text instead of leaving it
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- posts and comments -------------------------------------------------

    def upsert_post(self, row: dict) -> None:
        with self.conn:
            self.conn.execute(_upsert_sql("posts", POST_COLS), [row[c] for c in POST_COLS])

    def upsert_comments(self, rows: Iterable[dict]) -> None:
        with self.conn:
            self.conn.executemany(
                _upsert_sql("comments", COMMENT_COLS),
                ([row[c] for c in COMMENT_COLS] for row in rows),
            )

    def posts_needing_comments(self, subreddit: str, older_than: float, newer_than: float):
        """Posts at least 24h old (older_than) but not too old (newer_than), not yet fetched."""
        rows = self.conn.execute(
            "SELECT id FROM posts WHERE subreddit=? AND comments_fetched_at IS NULL "
            "AND created_utc<=? AND created_utc>=? ORDER BY created_utc DESC",
            (subreddit, older_than, newer_than),
        )
        return [r["id"] for r in rows]

    def mark_comments_fetched(self, post_id: str, ts: float) -> None:
        with self.conn:
            self.conn.execute("UPDATE posts SET comments_fetched_at=? WHERE id=?", (ts, post_id))

    def ids_to_recheck(self, table: str, since: float) -> list[str]:
        """Items created since `since`: ones already seen missing first (to confirm them),
        then the least recently rechecked."""
        assert table in TABLES
        rows = self.conn.execute(
            f"SELECT id FROM {table} WHERE created_utc>=? "
            "ORDER BY missing_since IS NULL, rechecked_at IS NOT NULL, rechecked_at, id",
            (since,),
        )
        return [r["id"] for r in rows]

    def mark_rechecked(self, table: str, ids: list[str], ts: float) -> None:
        assert table in TABLES
        with self.conn:
            self.conn.executemany(
                f"UPDATE {table} SET rechecked_at=? WHERE id=?", [(ts, i) for i in ids]
            )

    def missing_since(self, table: str, ids: list[str]) -> dict[str, float | None]:
        assert table in TABLES
        if not ids:
            return {}
        rows = self.conn.execute(
            f"SELECT id, missing_since FROM {table} WHERE id IN ({', '.join('?' * len(ids))})",
            ids,
        )
        return {r["id"]: r["missing_since"] for r in rows}

    def set_missing(self, table: str, present: list[str], missing: list[str], ts: float) -> None:
        """Clear the missing mark of items Reddit returned; keep the first time for the rest."""
        assert table in TABLES
        with self.conn:
            self.conn.executemany(
                f"UPDATE {table} SET missing_since=NULL WHERE id=?", [(i,) for i in present]
            )
            self.conn.executemany(
                f"UPDATE {table} SET missing_since=COALESCE(missing_since, ?) WHERE id=?",
                [(ts, i) for i in missing],
            )

    def delete_item(self, table: str, item_id: str) -> None:
        """Delete a post or comment. Deleting a post also deletes its stored comments."""
        assert table in TABLES
        with self.conn:
            if table == "posts":
                self.conn.execute("DELETE FROM comments WHERE post_id=?", (item_id,))
            self.conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))

    def purge_subreddit(self, name: str) -> tuple[int, int]:
        """Delete every stored post and comment of a subreddit. Returns (posts, comments)."""
        with self.conn:
            comments = self.conn.execute("DELETE FROM comments WHERE subreddit=?", (name,))
            posts = self.conn.execute("DELETE FROM posts WHERE subreddit=?", (name,))
        return posts.rowcount, comments.rowcount

    def purge_wal(self) -> None:
        """Copy the WAL into the database and empty it, so no old copies of pages remain."""
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -- subreddit state ------------------------------------------------------

    def get_state(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM subreddit_state WHERE name=?", (name,)).fetchone()

    def update_state(self, name: str, **fields) -> None:
        assert set(fields) <= STATE_COLS
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO subreddit_state (name) VALUES (?)", (name,))
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self.conn.execute(
                    f"UPDATE subreddit_state SET {sets} WHERE name=?", [*fields.values(), name]
                )

    def dead_subreddits(self):
        return self.conn.execute(
            "SELECT name, dead_since, last_error FROM subreddit_state "
            "WHERE dead_since IS NOT NULL ORDER BY name"
        ).fetchall()

    # -- sitewide block -------------------------------------------------------

    def get_block(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT since, reason FROM sitewide_block").fetchone()

    def set_block(self, since: float, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO sitewide_block (id, since, reason) VALUES (1, ?, ?)",
                (since, reason),
            )

    def clear_block(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM sitewide_block")

    # -- runs and request accounting -----------------------------------------

    def start_run(self, kind: str, detail: str, now: float) -> int:
        with self.conn:
            self.conn.execute("DELETE FROM requests WHERE ts<?", (now - REQUEST_LOG_DAYS * 86400,))
            cur = self.conn.execute(
                "INSERT INTO runs (kind, detail, started_at, status) VALUES (?, ?, ?, 'running')",
                (kind, detail, now),
            )
        return int(cur.lastrowid)

    def interrupt_stale_runs(self) -> int:
        """Close runs left 'running' by a process that was killed or lost power."""
        with self.conn:
            cur = self.conn.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
        return cur.rowcount

    def add_request(self, run_id: int, ts: float) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO requests (ts, run_id) VALUES (?, ?)", (ts, run_id))

    def finish_run(
        self, run_id: int, now: float, status: str, posts: int, comments: int, deleted: int
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, posts_added=?, comments_added=?, "
                "items_deleted=? WHERE id=?",
                (now, status, posts, comments, deleted, run_id),
            )

    def run_requests(self, run_id: int) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM requests WHERE run_id=?", (run_id,))
        return int(row.fetchone()[0])

    def requests_since(self, since: float, kind: str | None = None) -> int:
        """Requests sent since `since`, by runs of any kind or of one kind."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM requests JOIN runs ON runs.id=requests.run_id "
            "WHERE requests.ts>=? AND (? IS NULL OR runs.kind=?)",
            (since, kind, kind),
        ).fetchone()
        return int(row[0])

    def tier1_completed_since(self, since: float) -> bool:
        """True if a collect run that included tier 1 started since `since` and finished ok."""
        row = self.conn.execute(
            # the detail lists the tiers sorted, so a run with tier 1 starts with "tiers=1"
            "SELECT 1 FROM runs WHERE kind='collect' AND detail LIKE 'tiers=1%' "
            "AND status='ok' AND started_at>=?",
            (since,),
        ).fetchone()
        return row is not None

    def last_run(self):
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def counts(self) -> dict[str, int]:
        return {
            t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(TABLES)
        }
