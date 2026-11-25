"""Bounded scan history on the camera lab's capture volume."""
from datetime import datetime, timezone
import sqlite3
import threading
import time


class DetectionHistory:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, value TEXT, symbology TEXT,
            stage TEXT, method TEXT, path TEXT, trial_ms INTEGER, frame_ms INTEGER)''')

    def record(self, reads, started, captured, center_first):
        if not reads:
            return
        method = 'Center first' if center_first else 'Full frame'
        offset = time.time() - time.monotonic()
        rows = []
        for item in reads:
            measured = item['read_at']
            at = datetime.fromtimestamp(measured + offset, timezone.utc).isoformat(timespec='milliseconds')
            stage = {'Confirmed': 'Confirmed', 'Duplicate suppressed': 'Repeat'}.get(item['reason'], 'First read')
            path = item['pipeline'].removeprefix('ZXing-C++ / ')
            rows.append((at, item['value'], item['symbology'], stage, method, path,
                         round((measured - started) * 1000), round((measured - captured) * 1000)))
        with self.lock, self.db:
            self.db.executemany('''INSERT INTO readings
                (at,value,symbology,stage,method,path,trial_ms,frame_ms) VALUES (?,?,?,?,?,?,?,?)''', rows)
            last = self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
            if last % 100 < len(rows):
                for condition, keep in [("stage = 'Repeat'", 1000), ("stage != 'Repeat'", 500)]:
                    self.db.execute(f'''DELETE FROM readings WHERE {condition} AND id <= (
                        SELECT id FROM readings WHERE {condition} ORDER BY id DESC LIMIT 1 OFFSET ?)''', (keep,))

    def recent(self, repeats=False):
        condition = '' if repeats else "WHERE stage != 'Repeat'"
        with self.lock:
            return [dict(row) for row in self.db.execute(
                f'SELECT * FROM readings {condition} ORDER BY id DESC LIMIT 100')]
