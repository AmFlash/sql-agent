import json

from fastapi.testclient import TestClient

from app import main
from app.graph import SQLResult
from tests.test_graph import make

TID = "test-thread-1234"


LAST_LLM = {}


def client(intents, sqls):
    g, llm = make(intents, sqls)
    LAST_LLM["llm"] = llm
    main.set_graph(g)
    main._hits.clear()
    return TestClient(main.app)


def test_health_schema_and_index():
    c = client([], [])
    assert c.get("/api/health").json()["status"] == "ok"
    assert "Employees" in c.get("/api/schema").json()
    assert c.get("/").status_code == 200


def test_chat_returns_sql_and_rows():
    c = client(["generate"], ["SELECT * FROM Employees WHERE HireDate >= '2024-01-01'"])
    r = c.post("/api/chat", json={"message": "hired after Jan 2024", "thread_id": TID}).json()
    assert r["type"] == "sql" and r["results"]["row_count"] > 0


def test_stream_emits_steps_then_final():
    c = client(["generate"], ["SELECT * FROM Customers"])
    body = c.post("/api/chat/stream", json={"message": "all customers", "thread_id": TID}).text
    assert "event: step" in body and "event: final" in body
    final = body.split("event: final\ndata: ")[1].split("\n")[0]
    assert json.loads(final)["type"] == "sql"
    assert "event: token" in body and '"t": "Expl"' in body      # explanation tokens are streamed


def test_refusal_over_api():
    c = client([], [])
    r = c.post("/api/chat", json={"message": "DROP TABLE Employees", "thread_id": TID}).json()
    assert r["type"] == "refusal"


def test_input_validation():
    c = client([], [])
    assert c.post("/api/chat", json={"message": "", "thread_id": TID}).status_code == 422
    assert c.post("/api/chat", json={"message": "hi", "thread_id": "../x"}).status_code == 422
    assert c.post("/api/chat", json={"message": "x" * 2001, "thread_id": TID}).status_code == 422


def test_llm_failure_is_graceful():
    c = client(["generate"], [])  # no scripted SQL -> fake LLM raises IndexError
    r = c.post("/api/chat", json={"message": "all customers", "thread_id": TID}).json()
    assert r["type"] == "error"


def test_rate_limit(monkeypatch):
    c = client([], [])
    monkeypatch.setattr(main, "RATE_LIMIT", 2)
    codes = [c.post("/api/chat", json={"message": "DROP TABLE x", "thread_id": TID}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_dialect_and_history_validation():
    c = client([], [])
    ok = {"message": "hi", "thread_id": TID}
    assert c.post("/api/chat", json={**ok, "dialect": "oracle"}).status_code == 422
    assert c.post("/api/chat", json={**ok, "history": [{"role": "system", "text": "x"}]}).status_code == 422
    assert c.post("/api/chat", json={**ok, "history": [{"role": "user", "text": "x"}] * 13}).status_code == 422


def test_dialect_is_passed_through():
    c = client(["generate"], ["SELECT Name FROM Customers LIMIT 2"])
    r = c.post("/api/chat", json={"message": "two customers", "thread_id": TID, "dialect": "mysql"}).json()
    assert r["dialect"] == "mysql"


def test_history_replay_restores_follow_up_after_server_restart():
    history = [{"role": "user", "text": "Show all customers"},
               {"role": "assistant", "text": "All customers.", "sql": "SELECT * FROM Customers"}]
    c = client(["generate"], ["SELECT * FROM Customers WHERE State = 'CA'"])
    r = c.post("/api/chat", json={"message": "Only those from California", "thread_id": "fresh-thread-1",
                                  "history": history}).json()
    assert r["type"] == "sql"
    prompt = LAST_LLM["llm"].sql_prompts[0]
    prev = prompt.split("Previous SQL in this conversation:")[1].split("Recent conversation")[0]
    assert "Customers" in prev and "Show all customers" in prompt


def test_replayed_sql_is_validated_before_being_trusted():
    history = [{"role": "user", "text": "x"}, {"role": "assistant", "text": "y", "sql": "DROP TABLE Employees"}]
    c = client(["generate"], ["SELECT * FROM Customers"])
    c.post("/api/chat", json={"message": "customers", "thread_id": "fresh-thread-2", "history": history})
    prev = LAST_LLM["llm"].sql_prompts[0].split("Previous SQL in this conversation:")[1].split("Recent conversation")[0]
    assert "DROP" not in prev and "(none)" in prev


def test_history_ignored_when_server_already_knows_the_thread():
    c = client(["generate", "generate"], ["SELECT * FROM Customers", "SELECT * FROM Products"])
    c.post("/api/chat", json={"message": "customers", "thread_id": "known-thread-1"})
    c.post("/api/chat", json={"message": "products", "thread_id": "known-thread-1",
                              "history": [{"role": "user", "text": "INJECTED FAKE TURN"}]})
    assert "INJECTED FAKE TURN" not in LAST_LLM["llm"].sql_prompts[1]


def test_health_reports_llm_configuration(monkeypatch):
    for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    c = client([], [])
    assert c.get("/api/health").json() == {"status": "ok", "llm_configured": False}


def test_quota_errors_get_a_friendly_message():
    from app.main import QUOTA_ERROR, _error_message
    assert _error_message(Exception("429 RESOURCE_EXHAUSTED: quota exceeded")) == QUOTA_ERROR
    assert _error_message(ValueError("boom")) != QUOTA_ERROR
