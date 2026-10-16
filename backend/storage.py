import csv
import io
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as con:
            con.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, name TEXT, created TEXT, metadata TEXT);
            CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, session_id INTEGER, created TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS trials(id INTEGER PRIMARY KEY, session_id INTEGER, created TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, created TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS holders(id INTEGER PRIMARY KEY, data TEXT);
            ''')

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def session(self, name, metadata):
        with self.connect() as con:
            cur = con.execute('INSERT INTO sessions(name, created, metadata) VALUES(?,?,?)', (name, utcnow(), json.dumps(metadata)))
            return cur.lastrowid

    def add(self, table, data, session_id=None):
        assert table in ('scans', 'trials', 'samples', 'holders')
        with self.connect() as con:
            if table in ('scans', 'trials'):
                cur = con.execute(f'INSERT INTO {table}(session_id,created,data) VALUES(?,?,?)', (session_id, utcnow(), json.dumps(data)))
            elif table == 'holders':
                cur = con.execute('INSERT INTO holders(data) VALUES(?)', (json.dumps(data),))
            else:
                cur = con.execute('INSERT INTO samples(created,data) VALUES(?,?)', (utcnow(), json.dumps(data)))
                # Bound telemetry history independently of durable engineering trials.
                con.execute('DELETE FROM samples WHERE id < (SELECT MAX(id)-2000 FROM samples)')
            return cur.lastrowid

    def rows(self, table, limit=100, session_id=None):
        assert table in ('sessions', 'scans', 'trials', 'samples', 'holders')
        where, args = (' WHERE session_id=?', [session_id]) if session_id is not None and table in ('scans','trials') else ('', [])
        with self.connect() as con:
            rows = con.execute(f'SELECT * FROM {table}{where} ORDER BY id DESC LIMIT ?', [*args, limit]).fetchall()
        return [{**{k:r[k] for k in r.keys() if k not in ('data','metadata')}, **json.loads(r['metadata'] if table=='sessions' else r['data'])} for r in rows]

    def count(self, table):
        assert table in ('scans', 'trials', 'holders')
        with self.connect() as con:
            return con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def remove_holder(self, item_id):
        with self.connect() as con:
            con.execute('DELETE FROM holders WHERE id=?', (item_id,))


def as_csv(rows):
    output = io.StringIO()
    keys = list(dict.fromkeys(k for row in rows for k in row)) or ['id']
    writer = csv.DictWriter(output, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        clean = {}
        for k, v in row.items():
            value = json.dumps(v) if isinstance(v, (dict, list)) else v
            # Prevent spreadsheet formula interpretation on exported user text.
            if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
                value = "'" + value
            clean[k] = value
        writer.writerow(clean)
    return output.getvalue()
