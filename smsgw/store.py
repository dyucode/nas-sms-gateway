"""SQLite state: dedupe, long-message assembly and the outbox for Chat delivery.

Message bodies are kept only for ``retention_days``; after that the content is
blanked but the hash stays for as long as the message is still in the modem (so
it is never announced twice). Undelivered messages are never removed.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional

from .pdu import Sms

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_parts(
  hash TEXT PRIMARY KEY,
  sender TEXT, ts TEXT, text TEXT,
  ref INTEGER, total INTEGER, seq INTEGER,
  seen_at REAL NOT NULL,
  composed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS raw_by_key ON raw_parts(sender, ref, total);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT, ts TEXT, text TEXT,
  incomplete INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  sent_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# A hash may be forgotten only when the message is old AND has left the modem.
HASH_MIN_AGE_DAYS = 400
HASH_GONE_DAYS = 30
LAST_SEEN_RESOLUTION = 86400  # refresh last_seen at most daily to limit disk writes


@dataclass
class OutMsg:
    id: int
    sender: str
    ts: str
    text: str
    incomplete: bool
    attempts: int
    done: int = 0


def _restrict_permissions(path: str) -> None:
    """Message bodies are private: keep the database readable by its owner only."""
    if path == ":memory:":
        return
    for target, mode in ((path, 0o600), (os.path.dirname(path) or ".", 0o700)):
        try:
            os.chmod(target, mode)
        except OSError:  # e.g. a directory owned by someone else; the file still counts
            pass


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.executescript(SCHEMA)
        self._add_column("outbox", "done", "INTEGER NOT NULL DEFAULT 0")  # delivered-channels bitmask
        self._add_column("raw_parts", "last_seen", "REAL")
        self._add_column("raw_parts", "notify", "INTEGER NOT NULL DEFAULT 1")
        self._add_column("raw_parts", "outbox_id", "INTEGER")  # the message this part was delivered in
        self._add_column("raw_parts", "keep", "INTEGER NOT NULL DEFAULT 0")  # never delete from the modem
        self.db.commit()
        _restrict_permissions(path)

    def _add_column(self, table: str, column: str, decl: str) -> None:
        cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def rollback(self) -> None:
        self.db.rollback()

    # -- meta -------------------------------------------------------------
    def get_meta(self, k: str) -> Optional[str]:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))

    def del_meta(self, k: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM meta WHERE k=?", (k,))

    # -- ingest -----------------------------------------------------------
    @staticmethod
    def pdu_hash(pdu_hex: str) -> str:
        return hashlib.sha256(pdu_hex.strip().upper().encode()).hexdigest()

    def seen(self, pdu_hex: str, now: Optional[float] = None) -> bool:
        """True if the PDU is known. Also records that it is still in the modem."""
        now = time.time() if now is None else now
        h = self.pdu_hash(pdu_hex)
        row = self.db.execute("SELECT last_seen FROM raw_parts WHERE hash=?", (h,)).fetchone()
        if row is None:
            return False
        if row[0] is None or now - row[0] >= LAST_SEEN_RESOLUTION:
            with self.db:
                self.db.execute("UPDATE raw_parts SET last_seen=? WHERE hash=?", (now, h))
        return True

    def can_delete(self, pdu_hex: str) -> bool:
        """True only if deleting this PDU from the modem can lose nothing.

        Every channel must have confirmed the message. Parts of an unfinished long
        message, messages deliberately not forwarded (first-run skip) and messages
        marked ``keep`` (undecodable: the modem has the only copy) always stay.
        """
        row = self.db.execute(
            "SELECT composed, notify, keep, outbox_id, sender FROM raw_parts WHERE hash=?",
            (self.pdu_hash(pdu_hex),),
        ).fetchone()
        if row is None:
            return False
        composed, notify, keep, outbox_id, sender = row
        if not composed or not notify or keep:
            return False
        if outbox_id is not None:
            sent = self.db.execute("SELECT sent_at FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            return sent is None or sent[0] is not None  # no row: it was delivered and pruned
        # Stored before 0.2.2, so there is no link to the message: require that nothing
        # at all from this sender is still waiting.
        return self.db.execute(
            "SELECT 1 FROM outbox WHERE sent_at IS NULL AND sender=? LIMIT 1", (sender,)
        ).fetchone() is None

    def add(self, pdu_hex: str, sms: Sms, notify: bool = True, now: Optional[float] = None,
            keep: bool = False) -> bool:
        """Record a decoded message. Returns False if it was already known."""
        now = time.time() if now is None else now
        h = self.pdu_hash(pdu_hex)
        if self.db.execute("SELECT 1 FROM raw_parts WHERE hash=?", (h,)).fetchone():
            return False
        ref = total = seq = None
        if sms.concat:
            ref, total, seq = sms.concat
        composed = 0 if (sms.concat and total and total > 1) else 1
        with self.db:  # the part and its outbox row are stored atomically
            self.db.execute(
                "INSERT INTO raw_parts(hash,sender,ts,text,ref,total,seq,seen_at,composed,last_seen,notify,keep) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (h, sms.sender, sms.timestamp, sms.text, ref, total, seq, now, composed, now, int(notify), int(keep)),
            )
            if composed == 1 and notify:
                self._finish([h], sms.sender, sms.timestamp, sms.text, False, True, now)
            elif composed == 0:
                self._try_compose(sms.sender, ref, total, notify, now)
        return True

    def _parts(self, sender: str, ref: int, total: int) -> list:
        return self.db.execute(
            "SELECT hash, seq, text, ts, notify FROM raw_parts "
            "WHERE sender=? AND ref=? AND total=? AND composed=0 ORDER BY seq, seen_at",
            (sender, ref, total),
        ).fetchall()

    def _try_compose(self, sender: str, ref: int, total: int, notify: bool, now: float) -> None:
        rows = self._parts(sender, ref, total)
        by_seq = {}
        for _h, seq, text, ts, _n in rows:
            by_seq.setdefault(seq, (text, ts))
        if all(i in by_seq for i in range(1, total + 1)):
            text = "".join(by_seq[i][0] for i in range(1, total + 1))
            # Finish every row, including duplicates of a part, so none is left behind.
            self._finish([r[0] for r in rows], sender, by_seq[1][1], text, False, notify, now)

    def _finish(self, hashes, sender, ts, text, incomplete, notify, now) -> None:
        outbox_id = None
        if notify:
            outbox_id = self.db.execute(
                "INSERT INTO outbox(sender,ts,text,incomplete,created_at) VALUES(?,?,?,?,?)",
                (sender, ts, text, 1 if incomplete else 0, now),
            ).lastrowid
        self.db.executemany("UPDATE raw_parts SET composed=1, outbox_id=? WHERE hash=?",
                            [(outbox_id, h) for h in hashes])

    def flush_incomplete(self, timeout: float, now: Optional[float] = None) -> int:
        """Emit long messages whose remaining parts never arrived."""
        now = time.time() if now is None else now
        keys = self.db.execute(
            "SELECT sender, ref, total FROM raw_parts WHERE composed=0 GROUP BY sender, ref, total "
            "HAVING MIN(seen_at) < ?",
            (now - timeout,),
        ).fetchall()
        n = 0
        with self.db:
            for sender, ref, total in keys:
                rows = self._parts(sender, ref, total)
                by_seq = {}
                for _h, seq, text, _ts, _n in rows:
                    by_seq.setdefault(seq, text)
                text = "".join(by_seq[s] for s in sorted(by_seq))
                notify = any(r[4] for r in rows)
                self._finish([r[0] for r in rows], sender, rows[0][3], text, True, notify, now)
                n += 1
        return n

    # -- outbox -----------------------------------------------------------
    def due(self, limit: int = 100, now: Optional[float] = None, channels: Optional[int] = None) -> List[OutMsg]:
        """Unsent messages ready to retry; with ``channels``, only those still missing one of them."""
        now = time.time() if now is None else now
        sql = "SELECT id,sender,ts,text,incomplete,attempts,done FROM outbox WHERE sent_at IS NULL AND next_try<=?"
        args: list = [now]
        if channels is not None:
            sql += " AND (done & ?) != ?"
            args += [channels, channels]
        rows = self.db.execute(sql + " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
        return [OutMsg(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6]) for r in rows]

    def mark_sent(self, mid: int, now: Optional[float] = None) -> None:
        with self.db:
            self.db.execute("UPDATE outbox SET sent_at=? WHERE id=?", (time.time() if now is None else now, mid))

    def mark_channel(self, mid: int, bit: int, full_mask: int, now: Optional[float] = None) -> None:
        """Record that one channel delivered; the message is sent once every channel has."""
        with self.db:
            self.db.execute("UPDATE outbox SET done=done|? WHERE id=?", (bit, mid))
            self.db.execute("UPDATE outbox SET sent_at=? WHERE id=? AND (done & ?)=?",
                            (time.time() if now is None else now, mid, full_mask, full_mask))

    def finalize(self, full_mask: int, now: Optional[float] = None) -> int:
        """Close messages already delivered to every active channel (e.g. a channel was removed)."""
        with self.db:
            return self.db.execute(
                "UPDATE outbox SET sent_at=? WHERE sent_at IS NULL AND (done & ?)=?",
                (time.time() if now is None else now, full_mask, full_mask),
            ).rowcount

    def mark_failed(self, mid: int, now: Optional[float] = None) -> None:
        """A channel refused this message: only this message backs off."""
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT attempts FROM outbox WHERE id=?", (mid,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = min(30 * 2 ** min(attempts - 1, 20), 1800)
            self.db.execute("UPDATE outbox SET attempts=?, next_try=? WHERE id=?", (attempts, now + delay, mid))

    def reset_retries(self) -> None:
        """Forget per-message backoff, e.g. at startup: the cause may be gone by now."""
        with self.db:
            self.db.execute("UPDATE outbox SET attempts=0, next_try=0 WHERE sent_at IS NULL")

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]

    def oldest_pending(self) -> Optional[float]:
        """``created_at`` of the oldest undelivered message, or None when the queue is empty."""
        return self.db.execute("SELECT MIN(created_at) FROM outbox WHERE sent_at IS NULL").fetchone()[0]

    # -- retention --------------------------------------------------------
    def prune(self, retention_days: int, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - retention_days * 86400
        with self.db:
            self.db.execute(
                "UPDATE raw_parts SET sender='', text='', ts='' WHERE seen_at<? AND composed=1", (cutoff,)
            )
            self.db.execute("DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at<?", (cutoff,))
            self.db.execute(
                "DELETE FROM raw_parts WHERE seen_at<? AND COALESCE(last_seen, seen_at)<?",
                (now - HASH_MIN_AGE_DAYS * 86400, now - HASH_GONE_DAYS * 86400),
            )

    def close(self) -> None:
        self.db.close()
