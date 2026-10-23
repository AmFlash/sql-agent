"""Code-level guardrails. These never depend on the LLM behaving.

validate_sql() checks, in order:
  1. exactly one statement, parses under the target dialect (sqlite / postgres / mysql)
  2. read-only: root is a SELECT/UNION/WITH and no DML/DDL node appears anywhere
  3. every table exists (CTE names are allowed)
  4. every column resolves against the schema (via sqlglot's scope-aware qualifier)
  5. every join follows a declared foreign key (an ERROR in strict mode, so the agent retries)

Also here: input-side guards (destructive-request / prompt-injection detection with Unicode
normalisation), SQL extraction from free text, and validation of suggested CREATE INDEX statements.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify

from .schema_utils import Schema, to_sqlglot_schema

# UI / API dialect names -> sqlglot dialect names
DIALECTS = {"sqlite": "sqlite", "postgresql": "postgres", "postgres": "postgres", "mysql": "mysql"}


def sg_dialect(name: str | None) -> str:
    return DIALECTS.get((name or "sqlite").lower(), "sqlite")


_FORBIDDEN_NAMES = ["Insert", "Update", "Delete", "Drop", "Alter", "Create", "Command", "Merge",
                    "TruncateTable", "Into", "Attach", "Pragma", "Set", "Use", "Commit",
                    "Rollback", "Transaction", "Grant", "Copy", "LoadData"]
FORBIDDEN = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))
QUERY_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sql: str = ""            # cleaned, pretty-printed SQL (only set when ok)
    destructive: bool = False  # True when the statement was not a read-only query


def validate_sql(sql: str, schema: Schema, dialect: str = "sqlite", strict_joins: bool = True) -> ValidationResult:
    dialect = sg_dialect(dialect)
    if not sql or not sql.strip():
        return ValidationResult(False, ["Empty SQL."])

    # 1. parse
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as e:
        return ValidationResult(False, [f"Syntax error: {str(e).splitlines()[0]}"])
    if len(statements) != 1:
        multi_bad = any(not isinstance(s, QUERY_ROOTS) for s in statements)
        return ValidationResult(False, ["Exactly one SQL statement is allowed."], destructive=multi_bad)
    root = statements[0]

    # 2. read-only
    if not isinstance(root, QUERY_ROOTS):
        return ValidationResult(False, [
            f"Only read-only SELECT queries are allowed (got {type(root).__name__.upper()})."], destructive=True)
    bad = {type(n).__name__.upper() for n in root.walk() if isinstance(n, FORBIDDEN)}
    if bad:
        return ValidationResult(False, [f"Forbidden operation(s): {', '.join(sorted(bad))}."], destructive=True)

    # 3. tables
    known = {t.lower(): t for t in schema}
    ctes = {c.alias.lower() for c in root.find_all(exp.CTE)}
    errors: list[str] = []
    for tbl in root.find_all(exp.Table):
        name = tbl.name.lower()
        if name and name not in known and name not in ctes:
            hint = difflib.get_close_matches(tbl.name, list(schema), n=1, cutoff=0.5)
            errors.append(f"Table '{tbl.name}' does not exist."
                          + (f" Did you mean '{hint[0]}'?" if hint else ""))
    if errors:
        errors.append("Available tables: " + ", ".join(schema))
        return ValidationResult(False, errors)

    # 4. columns
    try:
        qualified = qualify(root.copy(), schema=to_sqlglot_schema(schema), dialect=dialect,
                            validate_qualify_columns=True, identify=False)
    except OptimizeError as e:
        used = sorted({known[t.name.lower()] for t in root.find_all(exp.Table)
                       if t.name.lower() in known})
        cols = "; ".join(f"{t}({', '.join(schema[t]['columns'])})" for t in used)
        return ValidationResult(False, [f"Column error: {e}", f"Columns available: {cols}"])
    except SqlglotError as e:
        return ValidationResult(False, [f"Could not validate query: {e}"])

    # 5. joins
    join_problems = _check_joins(qualified, schema)
    if join_problems and strict_joins:
        paths = "; ".join(sorted({f"{t}.{c} = {rt}.{rc}" for t, m in schema.items() for c, rt, rc in m["fks"]}))
        return ValidationResult(False, join_problems + [f"Valid join paths (foreign keys): {paths}"])
    return ValidationResult(True, [], join_problems, root.sql(dialect=dialect, pretty=True))


def qualify_query(sql: str, schema: Schema, dialect: str = "sqlite") -> exp.Expression | None:
    """Parsed + qualified AST for an already-validated query (used by the analyzer)."""
    try:
        root = sqlglot.parse_one(sql, read=sg_dialect(dialect))
        return qualify(root, schema=to_sqlglot_schema(schema), dialect=sg_dialect(dialect),
                       validate_qualify_columns=False, identify=False)
    except SqlglotError:
        return None


def _fk_pairs(schema: Schema) -> set[tuple[str, str, str, str]]:
    pairs = set()
    for t, meta in schema.items():
        for col, rt, rc in meta["fks"]:
            pairs.add((t.lower(), col.lower(), rt.lower(), rc.lower()))
            pairs.add((rt.lower(), rc.lower(), t.lower(), col.lower()))
    return pairs


def _check_joins(q: exp.Expression, schema: Schema) -> list[str]:
    problems: list[str] = []
    fks = _fk_pairs(schema)
    alias_to_table = {t.alias_or_name.lower(): t.name.lower() for t in q.find_all(exp.Table)}
    canon = {t.lower(): t for t in schema}
    col_canon = {c.lower(): c for m in schema.values() for c in m["columns"]}
    for join in q.find_all(exp.Join):
        if join.args.get("kind") == "CROSS" or join.args.get("method") == "NATURAL":
            continue
        on = join.args.get("on")
        trivially_true = on is not None and (
            (isinstance(on, exp.Boolean) and on.this is True)
            or (isinstance(on, exp.EQ) and isinstance(on.left, exp.Literal) and isinstance(on.right, exp.Literal)
                and on.left.this == on.right.this))
        if (on is None or trivially_true) and not join.args.get("using"):
            problems.append("A join has no real ON/USING condition (cartesian product).")
            continue
        for eq in (on.find_all(exp.EQ) if on is not None else []):
            l, r = eq.left, eq.right
            if isinstance(l, exp.Column) and isinstance(r, exp.Column):
                lt, rt = alias_to_table.get(l.table.lower()), alias_to_table.get(r.table.lower())
                if lt and rt and (lt, l.name.lower(), rt, r.name.lower()) not in fks:
                    problems.append(
                        f"Join {canon.get(lt, lt)}.{col_canon.get(l.name.lower(), l.name)} = "
                        f"{canon.get(rt, rt)}.{col_canon.get(r.name.lower(), r.name)} "
                        "does not follow a declared foreign key.")
    return problems


# ---------------------------------------------------------------- SQL extraction from free text
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.I | re.S)
_SQL_START = re.compile(r"\b(select|with)\b[\s\S]*", re.I)


def extract_sql(text: str) -> str | None:
    """Best-effort: pull the SQL out of a message like 'optimize this: SELECT ...'."""
    m = _FENCE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = _SQL_START.search(text)
    if not m:
        return None
    sql = m.group(0).strip()
    if ";" in sql:
        sql = sql[: sql.index(";") + 1]
    # plain-English requests such as "select the customers from CA" have no FROM-like structure
    return sql if re.search(r"\bfrom\b|\bselect\b\s+[\w*(]", sql, re.I) and len(sql.split()) >= 3 else None


def looks_like_sql(text: str) -> bool:
    """True when the message contains something shaped like a SQL query to work on."""
    return extract_sql(text) is not None


# ---------------------------------------------------------------- suggested index validation
def validate_index_ddl(ddl: str, schema: Schema) -> str | None:
    """Return a cleaned CREATE INDEX statement if it only touches real tables/columns, else None.
    Index suggestions are advice shown to the user; they are never executed."""
    try:
        e = sqlglot.parse_one(ddl.strip().rstrip(";"), read="sqlite")
    except SqlglotError:
        return None
    if not isinstance(e, exp.Create) or str(e.args.get("kind")).upper() != "INDEX":
        return None
    idx = e.this
    table = idx.args.get("table")
    params = idx.args.get("params")
    if table is None or params is None:
        return None
    known = {t.lower(): t for t in schema}
    t = known.get(table.name.lower())
    if not t:
        return None
    cols = {c.lower() for c in schema[t]["columns"]}
    for o in params.args.get("columns", []):
        col = o.find(exp.Column)
        if col is None or col.name.lower() not in cols:
            return None
    return ddl.strip().rstrip(";") + ";"


def index_signature(ddl: str) -> tuple[str, tuple[str, ...]] | None:
    """(table, columns) of a CREATE INDEX, so differently-named duplicates can be merged."""
    m = re.search(r"\bON\s+[\"`\[]?(\w+)[\"`\]]?\s*\(([^)]*)\)", ddl, re.I)
    if not m:
        return None
    cols = tuple(re.sub(r"\s+(ASC|DESC)$", "", c.strip(), flags=re.I).strip('"`[] ').lower()
                 for c in m.group(2).split(","))
    return m.group(1).lower(), cols


# ---------------------------------------------------------------- input-side guardrails
def normalize_text(text: str) -> str:
    """NFKC-normalise, drop zero-width / control characters, collapse whitespace.
    Stops trivial evasion such as 'd​elete from' (zero-width space) or full-width letters."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Cc") or ch in "\n\t")
    return re.sub(r"\s+", " ", t).strip()


_DESTRUCTIVE = re.compile(
    r"\b(delete\s+from|drop\s+(table|database|view|index|schema)|truncate\s+(table\s+)?\w+|"
    r"alter\s+table|insert\s+into|update\s+\w+\s+set|create\s+(table|database|index|view)|"
    r"grant\s+|revoke\s+)\b", re.I)

_NOUNS = r"(?:rows?|records?|entries|data|tables?|databases?|columns?|everything)"


def _nl_destructive(tables: tuple[str, ...]) -> re.Pattern:
    names = "|".join(sorted({re.escape(t.lower().rstrip("s")) + "s?" for t in tables}, key=len, reverse=True))
    obj = f"(?:{_NOUNS}|{names})" if names else _NOUNS
    strong = (r"\b(?:delete|erase|wipe(?:\s+out)?|purge|destroy|truncate|drop)\b\s+"
              rf"(?:all\s+|every\s+|each\s+|the\s+|my\s+|these\s+|those\s+|any\s+|some\s+)?(?:\w+\s+){{0,2}}?{obj}\b")
    remove = (r"\bremove\b\s+(?:all\s+|every\s+|the\s+|these\s+|those\s+)?(?:\w+\s+){0,2}?" + obj +
              r"\s+(?:from|in)\s+(?:the\s+)?(?:database|db|table|" + (names or "table") + r")\b")
    return re.compile(f"{strong}|{remove}", re.I)


_INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\s+(all\s+|any\s+|your\s+|the\s+|these\s+)*(previous|prior|above|earlier|safety|security)?\s*"
    r"(instructions?|rules?|prompts?|guidelines?|restrictions?|guardrails?|filters?)|"
    r"(reveal|show|print|repeat|leak|display|output|tell\s+me)\s+(me\s+)?(your\s+|the\s+)?(system\s+|hidden\s+|initial\s+)?(prompt|instructions|rules)|"
    r"what\s+(is|are)\s+your\s+(system\s+)?(prompt|instructions|rules)|"
    r"you\s+are\s+now\s+|act\s+as\s+(a|an)\s+(?!sql)|pretend\s+(to\s+be|you\s+are)|from\s+now\s+on[, ]+(you|ignore|act|answer)|"
    r"new\s+(instructions|rules)\s*:|developer\s+mode|jailbreak|\bDAN\b|"
    r"<\s*/?\s*(system|assistant|user_request|user_message|instructions?)\s*>|\[\s*/?\s*(system|inst)\s*\]", re.I)


def looks_destructive(text: str, tables: tuple[str, ...] = ()) -> bool:
    t = normalize_text(text)
    return bool(_DESTRUCTIVE.search(t) or _nl_destructive(tuple(tables)).search(t))


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(normalize_text(text)))
