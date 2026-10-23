from pathlib import Path

from tests.test_graph import make

ROOT = Path(__file__).resolve().parent.parent


def test_workflow_diagram_matches_the_compiled_graph():
    g, _ = make([], [])
    expected = g.get_graph().draw_mermaid()
    actual = (ROOT / "docs" / "workflow.mmd").read_text()
    assert actual == expected, (
        "docs/workflow.mmd is out of date. Regenerate with:\n"
        "  python -c \"from tests.test_graph import make; open('docs/workflow.mmd','w').write(make([],[])[0].get_graph().draw_mermaid())\"")


def test_every_graph_node_is_documented_in_readme():
    g, _ = make([], [])
    readme = (ROOT / "README.md").read_text()
    missing = [n for n in g.get_graph().nodes if not n.startswith("__") and n not in readme]
    assert not missing, f"README does not mention graph nodes: {missing}"
