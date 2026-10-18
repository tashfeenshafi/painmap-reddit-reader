from reader import cli
from reader.store import Store


def test_database_uses_wal(tmp_path):
    s = Store(tmp_path / "x.db")
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"posts", "comments", "subreddit_state", "runs"} <= tables
    s.close()


def test_status_needs_no_credentials(tmp_path, capsys, monkeypatch):
    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "s.db"
    store = Store(db)
    store.update_state("gone", dead_since=1_800_000_000, last_error="NotFound")
    store.close()
    assert cli.main(["--db", str(db), "status"]) == 0
    out = capsys.readouterr().out
    assert "posts: 0" in out and "requests in the last 24 hours: 0 of 5000" in out
    assert "gone: dead since 2027-01-15" in out


def test_collect_without_credentials_fails_clearly(tmp_path, capsys, monkeypatch):
    for key in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("READER_AUTHOR_SALT", "x")
    code = cli.main(["--db", str(tmp_path / "s.db"), "--config", "subreddits.yaml", "collect"])
    assert code == 2
    assert "REDDIT_CLIENT_ID" in capsys.readouterr().err


def test_reset_dead_command(tmp_path, capsys):
    db = tmp_path / "s.db"
    store = Store(db)
    store.update_state("gone", dead_since=1.0, fail_count=5)
    store.close()
    assert cli.main(["--db", str(db), "reset-dead", "--subreddit", "gone"]) == 0
    store = Store(db)
    assert store.get_state("gone")["dead_since"] is None
    store.close()
