"""Quick terminal chat: python cli.py [sqlite|postgresql|mysql]"""
import json
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()
from app.graph import ask, build_graph  # noqa: E402

dialect = sys.argv[1] if len(sys.argv) > 1 else None
graph, thread = build_graph(), str(uuid.uuid4())
print(f"SQL assistant ready{f' ({dialect})' if dialect else ''}. Ctrl+C to quit.\n")
while True:
    try:
        text = input("you> ")
    except (KeyboardInterrupt, EOFError):
        print()
        break
    r = ask(graph, text, thread, dialect)
    if r["type"] == "sql":
        if r.get("original_sql"):
            print(f"\nOriginal:\n{r['original_sql']}")
        print(f"\n{r['sql']}\n\n{r['explanation']}")
        if r["notes"]:
            print(f"\nNotes:\n{r['notes']}")
        if r.get("equivalent"):
            print("\nVerified: same rows as the original query.")
        for label, items in (("Tips", r.get("tips")), ("Suggested indexes", r["suggested_indexes"]),
                             ("Warnings", r["warnings"])):
            if items:
                print(f"{label}:", *items, sep="\n  ")
        if r.get("cost"):
            print(f"\nCost: {r['cost']['level']}. {r['cost']['summary']}")
        res = r["results"]
        if res and not res["error"]:
            print(f"\n{res['row_count']} row(s): {json.dumps(res['rows'][:5])}")
        elif res:
            print("Execution error:", res["error"])
    else:
        print("\n" + r["message"])
    print()
