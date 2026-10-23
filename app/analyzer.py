"""SQL optimisation analysis (the workflow's SQL Optimization step) and query cost estimation.

Everything here is deterministic code, not LLM output:
  * cost estimate   - SQLite EXPLAIN QUERY PLAN + table row counts (full scans, temp b-trees, est. rows examined)
  * index advice    - CREATE INDEX statements derived from WHERE / JOIN columns of tables that are full-scanned
  * lint tips       - SELECT *, leading-wildcard LIKE, functions on columns, NOT IN (subquery), unused joins
  * dialect bridge  - transpile PostgreSQL / MySQL to SQLite so non-SQLite queries can still be previewed
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .executor import run_readonly
from .schema_utils import Schema
from .validator import qualify_query, sg_dialect


# ---------------------------------------------------------------- dialect bridge
def to_sqlite(sql: str, dialect: str) -> str | None:
    """SQL in the user's dialect -> SQLite SQL (for execution/analysis on the sample DB), or None."""
    d = sg_dialect(dialect)
    if d == "sqlite":
        return sql
    try:
        return sqlglot.transpile(sql, read=d, write="sqlite")[0]
    except SqlglotError:
        return None


# ---------------------------------------------------------------- cost estimation
def explain_plan(db_path: str, sql: str) -> list[tuple[int, int, str]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        return [(r[0], r[1], r[3]) for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    except sqlite3.Error:
        return []
    finally:
        con.close()


_SCAN = re.compile(r"^SCAN (?:TABLE )?(\S+)(?: AS (\S+))?", re.I)
_SEARCH = re.compile(r"^SEARCH (?:TABLE )?(\S+)(?: AS (\S+))?", re.I)


def estimate_cost(plan: list[tuple[int, int, str]], schema: Schema, alias_to_table: dict[str, str]) -> dict:
    # ids of plan rows that are uncorrelated sub-queries: their children execute once, not once per outer row
    once_ids = {i for i, _, d in plan if re.match(r"(LIST|SCALAR|MATERIALIZE|CO-ROUTINE)", d, re.I)}
    canon = {t.lower(): t for t in schema}
    full_scans: list[str] = []
    inner_scans: list[str] = []   # scanned inside another loop -> a join-column index would help
    auto_index: list[str] = []
    loops, examined = 1.0, 0.0
    temp_btree = any("TEMP B-TREE" in d.upper() for _, _, d in plan)

    def resolve(name: str, alias: str | None) -> str | None:
        key = (alias or name).lower()
        t = alias_to_table.get(key) or alias_to_table.get(name.lower()) or name.lower()
        return canon.get(t)

    for _id, parent, detail in plan:
        in_once = parent in once_ids
        m = _SCAN.match(detail)
        if m and not re.match(r"(CONSTANT|SUBQUERY|CO-ROUTINE)", m.group(1), re.I):
            t = resolve(m.group(1), m.group(2))
            if t:
                rows = max(schema[t].get("rows", 1), 1)
                full_scans.append(t)
                if in_once:
                    examined += rows            # sub-query scan: runs once
                    continue
                if loops > 1:
                    inner_scans.append(t)
                examined += loops * rows
                loops *= rows
            continue
        m = _SEARCH.match(detail)
        if m:
            t = resolve(m.group(1), m.group(2))
            if t:
                rows = max(schema[t].get("rows", 1), 1)
                examined += loops * math.log2(rows + 1)
                if "AUTOMATIC" in detail.upper():
                    auto_index.append(t)
                    examined += rows  # building the transient index
    examined = int(min(examined, 10**12))
    level = "low" if examined < 5_000 else "medium" if examined < 500_000 else "high"
    parts = []
    if full_scans:
        parts.append("full scan of " + ", ".join(dict.fromkeys(full_scans)))
    if auto_index:
        parts.append("SQLite builds a temporary index for " + ", ".join(dict.fromkeys(auto_index)))
    if temp_btree:
        parts.append("needs a temporary sort/group structure")
    summary = ("; ".join(parts) if parts else "uses indexes / primary keys only") + \
        f". Estimated ~{examined:,} row lookups."
    return {"level": level, "est_rows_examined": examined, "full_scans": list(dict.fromkeys(full_scans)),
            "auto_index_tables": list(dict.fromkeys(auto_index)),
            "inner_scans": list(dict.fromkeys(inner_scans)), "temp_btree": temp_btree,
            "summary": summary, "plan": [d for _, _, d in plan]}


# ---------------------------------------------------------------- index advice
def _indexed_leading(schema: Schema, table: str) -> set[str]:
    meta = schema[table]
    lead = {c.lower() for c in meta["pk"]}
    lead |= {cols[0].lower() for cols in meta.get("indexes", []) if cols}
    return lead


def _wrapped_in_function(col: exp.Column) -> bool:
    """True if the column sits inside a scalar function/cast, which defeats index use."""
    node = col.parent
    while node is not None and not isinstance(node, (exp.Where, exp.Join, exp.Select)):
        if isinstance(node, (exp.Func, exp.Cast)) and not isinstance(node, (exp.AggFunc, exp.Connector, exp.Binary)):
            return True
        node = node.parent
    return False


def suggest_indexes(q: exp.Expression, schema: Schema, scanned: set[str], join_tables: set[str]) -> list[str]:
    """Index advice from the query's own predicates.
    scanned     - tables the plan full-scans: index their WHERE columns (equality first, then one range column)
    join_tables - tables scanned inside a join loop / auto-indexed: index their join columns instead."""
    if q is None or not (scanned or join_tables):
        return []
    alias_to_table = {t.alias_or_name.lower(): t.name for t in q.find_all(exp.Table)}
    canon = {t.lower(): t for t in schema}
    where_eq: dict[str, list[str]] = {}
    where_rng: dict[str, list[str]] = {}
    join_cols: dict[str, list[str]] = {}

    def add(store: dict, col: exp.Column, allowed: set[str]):
        t = canon.get(alias_to_table.get(col.table.lower(), "").lower())
        real = next((c for c in schema[t]["columns"] if c.lower() == col.name.lower()), None) if t else None
        if t in allowed and real and not _wrapped_in_function(col) \
                and real.lower() not in _indexed_leading(schema, t) and real not in store.setdefault(t, []):
            store[t].append(real)

    for where in q.find_all(exp.Where):
        for node in where.find_all(exp.EQ, exp.In):
            for col in node.find_all(exp.Column):
                add(where_eq, col, scanned)
        for node in where.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
            for col in node.find_all(exp.Column):
                add(where_rng, col, scanned)
    for join in q.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            for col in on.find_all(exp.Column):
                add(join_cols, col, join_tables)

    out = []
    for t in sorted(scanned | join_tables):
        cols = where_eq.get(t, []) + [c for c in where_rng.get(t, []) if c not in where_eq.get(t, [])][:1]
        if not cols and t in join_tables:
            cols = join_cols.get(t, [])
        cols = cols[:3]
        if cols:
            out.append(f"CREATE INDEX idx_{t.lower()}_{'_'.join(c.lower() for c in cols)} ON {t}({', '.join(cols)});")
    return out


# ---------------------------------------------------------------- lint tips (optimize mode)
def lint_tips(sql: str, dialect: str, qualified: exp.Expression | None, schema: Schema | None = None) -> list[str]:
    tips: list[str] = []
    try:
        tree = sqlglot.parse_one(sql, read=sg_dialect(dialect))
    except SqlglotError:
        return tips
    if any(isinstance(s, exp.Star) for s in tree.find_all(exp.Star)):
        tips.append("Replace SELECT * with only the columns you need: less data moves and indexes can cover the query.")
    for like in tree.find_all(exp.Like, exp.ILike):
        pat = like.expression
        if isinstance(pat, exp.Literal) and pat.is_string and pat.name.startswith("%"):
            tips.append(f"LIKE '{pat.name}' starts with a wildcard, so an index on that column cannot be used.")
    for where in tree.find_all(exp.Where):
        for fn in where.find_all(exp.Func):
            if isinstance(fn, (exp.AggFunc, exp.Connector, exp.Binary)):
                continue
            if fn.find(exp.Column) is not None and not isinstance(fn, (exp.Coalesce, exp.If)):
                tips.append(f"Wrapping a column in {fn.sql_name()}() inside WHERE prevents index use; "
                            "compare the raw column to a range or constant instead.")
                break
    for n in tree.find_all(exp.Not):
        if isinstance(n.this, exp.In) and n.this.args.get("query"):
            tips.append("NOT IN (subquery) is slow and misbehaves with NULLs; prefer NOT EXISTS or a LEFT JOIN ... IS NULL.")
    if qualified is not None:
        for sel in qualified.find_all(exp.Select):
            for join in sel.args.get("joins") or []:
                tbl = join.this
                if not isinstance(tbl, exp.Table):
                    continue
                alias = tbl.alias_or_name
                used = any(c.table == alias and c.find_ancestor(exp.Join) is not join for c in sel.find_all(exp.Column))
                if not used:
                    kind = (join.args.get("side") or join.args.get("kind") or "INNER").upper()
                    name = next((t for t in (schema or {}) if t.lower() == tbl.name.lower()), tbl.name)
                    tips.append(f"Table {name} is joined but none of its columns are used"
                                + (" - a LEFT JOIN to a unique key can simply be removed."
                                   if kind == "LEFT" else " - remove it unless it is there to filter rows."))
    return list(dict.fromkeys(tips))


# ---------------------------------------------------------------- orchestrator used by the graph
def analyze(sql: str, dialect: str, schema: Schema, db_path: str, intent: str = "generate") -> dict:
    """Returns {"cost": {...}|None, "indexes": [...], "tips": [...]}. Never raises."""
    out = {"cost": None, "indexes": [], "tips": []}
    try:
        sqlite_sql = to_sqlite(sql, dialect)
        q = qualify_query(sql, schema, dialect)
        if intent == "optimize":
            out["tips"] = lint_tips(sql, dialect, q, schema)
        if sqlite_sql:
            q_sqlite = qualify_query(sqlite_sql, schema, "sqlite")
            alias_to_table = {t.alias_or_name.lower(): t.name.lower()
                              for t in (q_sqlite.find_all(exp.Table) if q_sqlite else [])}
            cost = estimate_cost(explain_plan(db_path, sqlite_sql), schema, alias_to_table)
            out["cost"] = cost
            out["indexes"] = suggest_indexes(q_sqlite, schema, set(cost["full_scans"]),
                                             set(cost["inner_scans"]) | set(cost["auto_index_tables"]))
    except Exception:  # analysis is advisory; it must never break the main answer
        pass
    return out


def same_results(db_path: str, sql_a: str, sql_b: str, limit: int = 5000) -> bool | None:
    """Do two SQLite queries return the same multiset of rows? None = could not tell."""
    a, b = run_readonly(db_path, sql_a, limit=limit), run_readonly(db_path, sql_b, limit=limit)
    if a["error"] or b["error"] or a["truncated"] or b["truncated"]:
        return None
    if len(a["columns"]) != len(b["columns"]):
        return False
    key = lambda rows: Counter(tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in rows)  # noqa: E731
    return key(a["rows"]) == key(b["rows"])
