"""Unit tests for the SQLite state, including upgrades from the 0.2.0 database."""
import os
import sqlite3
import stat

import pytest

from smsgw.pdu import decode
from smsgw.store import Store
from tests.pdu_encode import deliver

DAY = 86400

SCHEMA_0_2_0 = """
CREATE TABLE raw_parts(hash TEXT PRIMARY KEY, sender TEXT, ts TEXT, text TEXT, ref INTEGER, total INTEGER,
  seq INTEGER, seen_at REAL NOT NULL, composed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, ts TEXT, text TEXT,
  incomplete INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, sent_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    yield s
    s.close()


def add(store, sender="+1555", text="hello", now=0.0, notify=True, concat=None):
    pdu = deliver(sender, text, concat=concat)
    return store.add(pdu, decode(pdu), notify=notify, now=now)


def test_upgrade_from_0_2_0_keeps_undelivered_messages(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_0_2_0)
    db.execute("INSERT INTO raw_parts(hash,sender,ts,text,seen_at,composed) VALUES('h1','+1','t','x',0,1)")
    db.execute("INSERT INTO outbox(sender,ts,text,created_at,attempts,next_try,done) VALUES('+1','t','stuck',0,131,9e9,2)")
    db.execute("INSERT INTO meta VALUES('initialized','1')")
    db.commit()
    db.close()

    s = Store(path)
    assert s.get_meta("initialized") == "1"
    assert s.pending_count() == 1
    assert s.due(now=100) == []                      # still inside the old 30 minute backoff
    s.reset_retries()
    m = s.due(now=100)[0]
    assert (m.text, m.attempts, m.done) == ("stuck", 0, 2)  # channels already delivered are remembered
    assert s.db.execute("SELECT notify, last_seen FROM raw_parts").fetchone() == (1, None)
    s.close()
    Store(path).close()                              # opening again is harmless


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_database_is_private(tmp_path):
    os.chmod(tmp_path, 0o755)
    path = tmp_path / "s.db"
    Store(str(path)).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_permission_failure_is_not_fatal(tmp_path, monkeypatch):
    def denied(*_a):
        raise PermissionError("not the owner")

    monkeypatch.setattr(os, "chmod", denied)
    Store(str(tmp_path / "s.db")).close()


def test_add_is_idempotent(store):
    assert add(store) is True
    assert add(store) is False
    assert store.pending_count() == 1


def test_failed_insert_leaves_no_partial_row(store, monkeypatch):
    def explode(*_a, **_k):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(store, "_try_compose", explode)
    long_pdu = deliver("+1555", "part", concat=(1, 2, 1))
    with pytest.raises(sqlite3.OperationalError):
        store.add(long_pdu, decode(long_pdu), now=0)
    assert store.seen(long_pdu, now=0) is False      # will be read again on the next poll


def test_seen_refreshes_last_seen_at_most_daily(store):
    add(store, now=0)
    pdu = deliver("+1555", "hello")
    last_seen = lambda: store.db.execute("SELECT last_seen FROM raw_parts").fetchone()[0]
    assert store.seen(pdu, now=10) is True and last_seen() == 0
    assert store.seen(pdu, now=DAY + 1) is True and last_seen() == DAY + 1
    assert store.seen(deliver("+1555", "unknown"), now=1) is False


def test_long_message_needs_all_parts(store):
    add(store, text="B", concat=(5, 3, 2), now=0)
    add(store, text="C", concat=(5, 3, 3), now=0)
    assert store.pending_count() == 0
    add(store, text="A", concat=(5, 3, 1), now=0)
    assert store.due(now=0)[0].text == "ABC"


def test_same_reference_from_different_senders_is_kept_apart(store):
    add(store, sender="+1111", text="one", concat=(5, 2, 1))
    add(store, sender="+2222", text="two", concat=(5, 2, 2))
    assert store.pending_count() == 0


def test_flush_incomplete_keeps_order_and_ignores_duplicates(store):
    add(store, text="C", concat=(5, 4, 3), now=0)
    add(store, text="A", concat=(5, 4, 1), now=0)
    add(store, text="A2", concat=(5, 4, 1), now=1)
    assert store.flush_incomplete(120, now=100) == 0
    assert store.flush_incomplete(120, now=121) == 1
    m = store.due(now=121)[0]
    assert m.text == "AC" and m.incomplete
    assert store.flush_incomplete(120, now=9999) == 0


def test_due_filters_by_missing_channel_and_backoff(store):
    add(store, text="a")
    add(store, text="b")
    first, second = store.due(now=0)
    store.mark_channel(first.id, 2, 3, now=0)
    assert [m.text for m in store.due(now=0, channels=2)] == ["b"]
    assert [m.text for m in store.due(now=0, channels=1)] == ["a", "b"]
    store.mark_failed(second.id, now=0)
    assert [m.text for m in store.due(now=10)] == ["a"]
    assert [m.text for m in store.due(now=31)] == ["a", "b"]
    assert [m.text for m in store.due(limit=1, now=31)] == ["a"]


def test_message_is_sent_only_when_every_channel_delivered(store):
    add(store)
    m = store.due(now=0)[0]
    store.mark_channel(m.id, 1, 3, now=0)
    assert store.pending_count() == 1
    store.mark_channel(m.id, 2, 3, now=0)
    assert store.pending_count() == 0


def test_finalize_closes_messages_when_a_channel_was_removed(store):
    add(store)
    store.mark_channel(store.due(now=0)[0].id, 1, 3, now=0)
    assert store.finalize(3, now=0) == 0
    assert store.finalize(1, now=0) == 1
    assert store.pending_count() == 0


def test_mark_failed_backoff_is_capped_even_after_thousands_of_attempts(store):
    add(store)
    mid = store.due(now=0)[0].id
    store.db.execute("UPDATE outbox SET attempts=5000 WHERE id=?", (mid,))
    store.mark_failed(mid, now=0)
    assert store.db.execute("SELECT next_try FROM outbox").fetchone()[0] == 1800


def test_oldest_pending_and_mark_sent(store):
    assert store.oldest_pending() is None
    add(store, text="a", now=5)
    add(store, text="b", now=9)
    assert store.oldest_pending() == 5
    store.mark_sent(store.due(now=9)[0].id, now=9)
    assert store.oldest_pending() == 9


def test_prune_blanks_sent_content_but_never_touches_undelivered(store):
    add(store, text="delivered", now=0)
    add(store, text="undelivered", now=0)
    store.mark_channel(store.due(now=0)[0].id, 1, 1, now=0)
    store.prune(14, now=100 * DAY)
    assert [m.text for m in store.due(now=100 * DAY)] == ["undelivered"]
    assert store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    assert {r[0] for r in store.db.execute("SELECT text FROM raw_parts")} == {""}
    assert store.seen(deliver("+1555", "delivered"), now=100 * DAY) is True


def test_prune_forgets_a_hash_only_after_the_message_left_the_modem(store):
    add(store, text="gone", now=0)
    add(store, text="still there", now=0)
    store.seen(deliver("+1555", "still there"), now=395 * DAY)
    store.prune(14, now=401 * DAY)
    assert store.seen(deliver("+1555", "gone"), now=401 * DAY) is False
    assert store.seen(deliver("+1555", "still there"), now=401 * DAY) is True


def test_meta_set_get_delete(store):
    assert store.get_meta("k") is None
    store.set_meta("k", "1")
    store.set_meta("k", "2")
    assert store.get_meta("k") == "2"
    store.del_meta("k")
    assert store.get_meta("k") is None
