from contextlib import contextmanager
import json
import sqlite3
import time
from pathlib import Path


class Cache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, updated REAL NOT NULL, data TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT updated,data FROM cache WHERE key=?", (key,)
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def set(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                (key, time.time(), json.dumps(value, ensure_ascii=False)),
            )
            db.execute(
                "DELETE FROM cache WHERE updated < ?", (time.time() - 100 * 86400,)
            )
