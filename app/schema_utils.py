"""Schema retrieval: introspect the database so we never hard-code tables.

Besides tables/columns/keys we also collect row counts, existing indexes and a few sample values per
text column. Sample values are what let the model map "California" -> State = 'CA' or
"pending orders" -> Status = 'Pending' instead of guessing.
"""
from __future__ import annotations

import os
import re
import sqlite3

Schema = dict  # {table: {"columns": {col: type}, "pk": [..], "fks": [(col, ref_table, ref_col)],
#                         "rows": int, "indexes": [[cols]], "samples": {col: [values]}}}

# Columns that look like personal / secret data are never sampled (values would be sent to the LLM provider).
# Set SCHEMA_SAMPLES=0 to disable sampling of ALL values, e.g. when pointing the agent at a real database.
_PII = re.compile(r"(e-?mail|phone|mobile|ssn|passport|address|first_?name|last_?name|full_?name|surname|"
                  r"password|secret|token|birth|dob|card|iban)", re.I)
MAX_ENUM = 12   # columns with <= this many distinct values are shown as an enum
N_EXAMPLES = 3


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_schema(db_path: str) -> Schema:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        schema: Schema = {}
        for t in tables:
            info = con.execute(f"PRAGMA table_info({_q(t)})").fetchall()
            fks = con.execute(f"PRAGMA foreign_key_list({_q(t)})").fetchall()
            indexes = []
            for idx in con.execute(f"PRAGMA index_list({_q(t)})").fetchall():
                cols = [r[2] for r in con.execute(f"PRAGMA index_info({_q(idx[1])})").fetchall()]
                if cols:
                    indexes.append(cols)
            samples: dict[str, list] = {}
            sampling = os.getenv("SCHEMA_SAMPLES", "1") != "0"
            for _, col, ctype, *_rest in info:
                if not sampling or _PII.search(col):
                    continue
                if "DATE" in (ctype or "").upper():
                    lo, hi = con.execute(f"SELECT MIN({_q(col)}), MAX({_q(col)}) FROM {_q(t)}").fetchone()
                    if lo is not None:
                        samples[col] = {"enum": False, "range": True, "values": [lo, hi]}
                    continue
                if (ctype or "TEXT").upper() not in ("TEXT", "VARCHAR", "CHAR", "") and "CHAR" not in (ctype or "").upper():
                    continue
                vals = [r[0] for r in con.execute(
                    f"SELECT DISTINCT {_q(col)} FROM {_q(t)} WHERE {_q(col)} IS NOT NULL LIMIT {MAX_ENUM + 1}")]
                if vals:
                    samples[col] = vals if len(vals) <= MAX_ENUM else vals[:N_EXAMPLES]
                    samples[col] = {"enum": len(vals) <= MAX_ENUM, "values": samples[col]}
            schema[t] = {
                "columns": {r[1]: (r[2] or "TEXT") for r in info},
                "pk": [r[1] for r in info if r[5]],
                "fks": [(r[3], r[2], r[4]) for r in fks],
                "rows": con.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0],
                "indexes": indexes,
                "samples": samples,
            }
        return schema
    finally:
        con.close()


def schema_to_prompt(schema: Schema) -> str:
    """Compact text rendering used inside LLM prompts."""
    lines = []
    for table, meta in schema.items():
        cols = ", ".join(
            f"{c} {t}" + (" PK" if c in meta["pk"] else "") for c, t in meta["columns"].items())
        lines.append(f"TABLE {table} ({cols})  -- {meta.get('rows', '?')} rows")
        for col, ref_t, ref_c in meta["fks"]:
            lines.append(f"  FK {table}.{col} -> {ref_t}.{ref_c}")
        for col, s in meta.get("samples", {}).items():
            vals = ", ".join(repr(v) for v in s["values"])
            if s.get("range"):
                lines.append(f"  RANGE {table}.{col}: {s['values'][0]!r} .. {s['values'][1]!r} (ISO dates)")
            elif s["enum"]:
                lines.append(f"  VALUES {table}.{col} is one of: {vals}")
            elif col.lower() not in ("email",) and not col.lower().endswith("name"):
                lines.append(f"  EXAMPLES {table}.{col}: {vals}")
    return "\n".join(lines)


def to_sqlglot_schema(schema: Schema) -> dict:
    return {t: dict(m["columns"]) for t, m in schema.items()}
