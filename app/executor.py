"""Read-only SQLite execution (defence in depth: validator + read-only connection)."""
from __future__ import annotations

import sqlite3
import time


def run_readonly(db_path: str, sql: str, limit: int = 100, timeout_s: float = 5.0) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + timeout_s
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        return {"columns": columns, "rows": [list(r) for r in rows[:limit]],
                "row_count": min(len(rows), limit), "truncated": truncated, "error": None}
    except sqlite3.Error as e:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False, "error": str(e)}
    finally:
        con.close()
