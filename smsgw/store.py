"""SQLite state: dedupe, long-message assembly and the outbox for Chat delivery.

Message bodies are kept only for ``retention_days``; after that the content is
blanked but the hash stays (so a message still sitting in the modem is never
announced twice).
"""

from __future__ import annotations

import hashlib
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


@dataclass
class OutMsg:
    id: int
    sender: str
    ts: str
    text: str
    incomplete: bool
    attempts: int
    done: int = 0


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}
        if "done" not in cols:  # bitmask of channels that already delivered this message
            self.db.execute("ALTER TABLE outbox ADD COLUMN done INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    # -- meta -------------------------------------------------------------
    def get_meta(self, k: str) -> Optional[str]:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
        self.db.commit()

    # -- ingest -----------------------------------------------------------
    @staticmethod
    def pdu_hash(pdu_hex: str) -> str:
        return hashlib.sha256(pdu_hex.strip().upper().encode()).hexdigest()

    def seen(self, pdu_hex: str) -> bool:
        h = self.pdu_hash(pdu_hex)
        return self.db.execute("SELECT 1 FROM raw_parts WHERE hash=?", (h,)).fetchone() is not None

    def add(self, pdu_hex: str, sms: Sms, notify: bool = True, now: Optional[float] = None) -> bool:
        """Record a decoded message. Returns False if it was already known."""
        now = time.time() if now is None else now
        h = self.pdu_hash(pdu_hex)
        if self.db.execute("SELECT 1 FROM raw_parts WHERE hash=?", (h,)).fetchone():
            return False
        ref = total = seq = None
        if sms.concat:
            ref, total, seq = sms.concat
        composed = 0 if (sms.concat and total and total > 1) else 1
        self.db.execute(
            "INSERT INTO raw_parts(hash,sender,ts,text,ref,total,seq,seen_at,composed) VALUES(?,?,?,?,?,?,?,?,?)",
            (h, sms.sender, sms.timestamp, sms.text, ref, total, seq, now, composed),
        )
        if composed == 1 and notify:
            self.db.execute(
                "INSERT INTO outbox(sender,ts,text,created_at) VALUES(?,?,?,?)",
                (sms.sender, sms.timestamp, sms.text, now),
            )
        elif composed == 0:
            self._try_compose(sms.sender, ref, total, notify, now)
        self.db.commit()
        return True

    def _try_compose(self, sender: str, ref: int, total: int, notify: bool, now: float) -> None:
        rows = self.db.execute(
            "SELECT hash, seq, text, ts FROM raw_parts WHERE sender=? AND ref=? AND total=? AND composed=0 ORDER BY seq",
            (sender, ref, total),
        ).fetchall()
        by_seq = {}
        for h, seq, text, ts in rows:
            by_seq.setdefault(seq, (h, text, ts))
        if len(by_seq) >= total and all(i in by_seq for i in range(1, total + 1)):
            text = "".join(by_seq[i][1] for i in range(1, total + 1))
            self._finish(sender, [v[0] for v in by_seq.values()], by_seq[1][2], text, False, notify, now)

    def _finish(self, sender, hashes, ts, text, incomplete, notify, now) -> None:
        self.db.executemany("UPDATE raw_parts SET composed=1 WHERE hash=?", [(h,) for h in hashes])
        if notify:
            self.db.execute(
                "INSERT INTO outbox(sender,ts,text,incomplete,created_at) VALUES(?,?,?,?,?)",
                (sender, ts, text, 1 if incomplete else 0, now),
            )

    def flush_incomplete(self, timeout: float, now: Optional[float] = None) -> int:
        """Emit long messages whose remaining parts never arrived."""
        now = time.time() if now is None else now
        keys = self.db.execute(
            "SELECT sender, ref, total FROM raw_parts WHERE composed=0 GROUP BY sender, ref, total "
            "HAVING MIN(seen_at) < ?",
            (now - timeout,),
        ).fetchall()
        n = 0
        for sender, ref, total in keys:
            rows = self.db.execute(
                "SELECT hash, seq, text, ts FROM raw_parts WHERE sender=? AND ref=? AND total=? AND composed=0 ORDER BY seq",
                (sender, ref, total),
            ).fetchall()
            text = "".join(r[2] for r in rows)
            self._finish(sender, [r[0] for r in rows], rows[0][3], text, True, True, now)
            n += 1
        self.db.commit()
        return n

    # -- outbox -----------------------------------------------------------
    def due(self, limit: int = 20, now: Optional[float] = None) -> List[OutMsg]:
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT id,sender,ts,text,incomplete,attempts,done FROM outbox "
            "WHERE sent_at IS NULL AND next_try<=? ORDER BY id LIMIT ?",
            (now, limit),
        ).fetchall()
        return [OutMsg(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6]) for r in rows]

    def mark_sent(self, mid: int, now: Optional[float] = None) -> None:
        self.db.execute("UPDATE outbox SET sent_at=? WHERE id=?", (time.time() if now is None else now, mid))
        self.db.commit()

    def mark_channel(self, mid: int, bit: int, full_mask: int, now: Optional[float] = None) -> None:
        """Record that one channel delivered; the message is sent once every channel has."""
        self.db.execute("UPDATE outbox SET done=done|? WHERE id=?", (bit, mid))
        self.db.execute("UPDATE outbox SET sent_at=? WHERE id=? AND (done & ?)=?",
                        (time.time() if now is None else now, mid, full_mask, full_mask))
        self.db.commit()

    def mark_failed(self, mid: int, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        row = self.db.execute("SELECT attempts FROM outbox WHERE id=?", (mid,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        delay = min(30 * 2 ** (attempts - 1), 1800)
        self.db.execute("UPDATE outbox SET attempts=?, next_try=? WHERE id=?", (attempts, now + delay, mid))
        self.db.commit()

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]

    # -- retention --------------------------------------------------------
    def prune(self, retention_days: int, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - retention_days * 86400
        self.db.execute(
            "UPDATE raw_parts SET sender='', text='', ts='' WHERE seen_at<? AND composed=1", (cutoff,)
        )
        self.db.execute("DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at<?", (cutoff,))
        self.db.execute("DELETE FROM raw_parts WHERE seen_at<?", (now - 400 * 86400,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
