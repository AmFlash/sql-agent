"""Measure SQL-generation accuracy with execution-based comparison.

    python eval/run_eval.py            # live: uses your configured LLM (needs an API key)
    python eval/run_eval.py --mock     # offline: scripted LLM answers with the reference SQL (tests the harness)
    python eval/run_eval.py -v         # print every failing case

Exit code is non-zero when overall accuracy < --min (default 0.8).
"""
from __future__ import annotations

import argparse
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.executor import run_readonly  # noqa: E402
from app.graph import IntentResult, SQLResult, ask, build_graph  # noqa: E402
from eval import cases  # noqa: E402

DB = str(ROOT / "db" / "sample.db")


def norm(rows):
    return [tuple(round(v, 4) if isinstance(v, float) else v for v in r) for r in rows]


def same_rows(agent_results, ref_sql: str, ordered: bool) -> bool:
    ref = run_readonly(DB, ref_sql, limit=100)
    if not agent_results or agent_results.get("error") or ref["error"]:
        return False
    a, b = norm(agent_results["rows"]), norm(ref["rows"])
    return a == b if ordered else Counter(a) == Counter(b)


class ScriptedLLM:
    """Offline stand-in: classifies per the case and 'generates' the reference SQL."""
    def __init__(self, intents, sqls):
        self.intents, self.sqls = list(intents), list(sqls)

    def with_structured_output(self, model):
        outer = self

        class R:
            def invoke(self, _):
                return IntentResult(intent=outer.intents.pop(0)) if model is IntentResult else outer.sqls.pop(0)
        return R()

    def invoke(self, _):
        from langchain_core.messages import AIMessage
        return AIMessage(content="(mock explanation)")


def make_graph(mock: bool, intents=(), sqls=()):
    if not mock:
        return build_graph(DB)
    return build_graph(DB, llm=ScriptedLLM(intents, [SQLResult(sql=s) for s in sqls]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--min", type=float, default=0.8)
    args = ap.parse_args()

    live_graph = None if args.mock else build_graph(DB)
    score: dict[str, list[bool]] = defaultdict(list)
    failures: list[str] = []

    def record(cat: str, ok: bool, label: str, detail: str = ""):
        score[cat].append(ok)
        if not ok:
            failures.append(f"[{cat}] {label}  {detail}")

    for q, ref, ordered in cases.SQL:
        g = live_graph or make_graph(True, ["generate"], [ref])
        r = ask(g, q, str(uuid.uuid4()))
        record("text-to-sql", r["type"] == "sql" and same_rows(r.get("results"), ref, ordered), q,
               r.get("sql", r.get("message", "")).replace("\n", " ")[:160])

    for convo in cases.FOLLOWUPS:
        g = live_graph or make_graph(True, ["generate"] * len(convo), [t[1] for t in convo])
        tid = str(uuid.uuid4())
        for msg, ref, ordered in convo:
            r = ask(g, msg, tid)
            record("follow-ups", r["type"] == "sql" and same_rows(r.get("results"), ref, ordered), msg,
                   r.get("sql", r.get("message", "")).replace("\n", " ")[:160])

    for q, ref, ordered in cases.DEBUG:
        g = live_graph or make_graph(True, ["debug"], [ref])
        r = ask(g, q, str(uuid.uuid4()))
        record("debugging", r["type"] == "sql" and same_rows(r.get("results"), ref, ordered), q,
               r.get("sql", r.get("message", "")).replace("\n", " ")[:160])

    for q in cases.OPTIMIZE:
        from app.validator import extract_sql
        orig = extract_sql(q)
        g = live_graph or make_graph(True, ["optimize"], [orig])
        r = ask(g, q, str(uuid.uuid4()))
        record("optimizing", r["type"] == "sql" and r.get("equivalent") is True, q, f"equivalent={r.get('equivalent')}")

    for q, reason in cases.REFUSE:
        g = live_graph or make_graph(True, [reason], [])
        r = ask(g, q, str(uuid.uuid4()))
        record("refusals", r["type"] == "refusal" and r.get("reason") == reason, q, f"got {r['type']}")

    for q, expect in cases.TYPES:
        if live_graph:
            g = live_graph
        elif expect == "clarification":
            g = build_graph(DB, llm=_Clarifier())
        else:
            g = make_graph(True, ["sql_help"])
        r = ask(g, q, str(uuid.uuid4()))
        record("clarify/help", r["type"] == expect, q, f"got {r['type']}")

    total = sum(len(v) for v in score.values())
    passed = sum(sum(v) for v in score.values())
    print(f"\n{'mode: MOCK (harness self-test)' if args.mock else 'mode: LIVE'}\n")
    print(f"{'category':<16}{'passed':>8}{'total':>7}{'accuracy':>10}")
    for cat, v in score.items():
        print(f"{cat:<16}{sum(v):>8}{len(v):>7}{sum(v) / len(v):>10.0%}")
    print(f"{'OVERALL':<16}{passed:>8}{total:>7}{passed / total:>10.0%}")
    if failures and (args.verbose or not args.mock):
        print("\nFailures:")
        print("\n".join("  " + f for f in failures))
    sys.exit(0 if passed / total >= args.min else 1)


class _Clarifier(ScriptedLLM):
    def __init__(self):
        super().__init__(["generate"], [])

    def with_structured_output(self, model):
        class R:
            def invoke(self, _):
                return IntentResult(intent="generate") if model is IntentResult else \
                    SQLResult(clarification="Which sales metric do you mean?")
        return R()


if __name__ == "__main__":
    main()
