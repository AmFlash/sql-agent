# Requirements checklist (assignment PDF → implementation)

| # | Requirement | Status | Implementation | Verified by |
|---|---|---|---|---|
| 3.1 | NL → SQL | ✅ | `generate` node; schema incl. enum values and date ranges | `eval/` text-to-SQL cases, `test_graph.py` |
| 3.2 | Validate tables, columns, joins, syntax, read-only | ✅ | `app/validator.py`; joins must follow FKs and have a real condition | `test_validator.py` (all 3 dialects) |
| 3.2 | Never generate DELETE/UPDATE/INSERT/DROP/ALTER/TRUNCATE; refuse politely | ✅ | input check (SQL + plain English) → AST validator → read-only connection; model output that is destructive becomes a refusal | `test_validator.py`, `test_graph.py::test_llm_cannot_sneak_destructive_sql` |
| 3.3 | Explain SQL in plain English | ✅ | `explain` node (streamed) | `test_api.py::test_stream_emits_steps_then_final` |
| 3.4 | Optimize: readability, performance, indexes, remove unnecessary joins | ✅ | optimize mode + `analyze` node: lint tips, plan-based index advice, schema-checked index suggestions, rewrite-equivalence check | `test_analyzer.py`, `test_graph.py` |
| 3.5 | Debug: identify, explain why, corrected version | ✅ | debug mode grounded by the validator's report; broken SQL sent to explain/optimize is routed to debug | `test_graph.py::test_debug_*` |
| 3.6 | Conversation context / follow-ups modify previous query | ✅ | `last_sql` + history in the prompt; survives server restart via client history replay | `test_follow_up_context_persists`, `test_history_replay_*` |
| 3.7 | Out-of-scope refusal with the specified message | ✅ | `detect_intent` + `scope_validation` → `refuse` | `test_out_of_scope_refused`, eval refusals |
| 4 | Frontend: chat, SQL panel, explanation panel, history, loading, errors | ✅ | `static/index.html` | jsdom E2E |
| 4 | Optional: copy, dark mode, streaming, download SQL, download results | ✅ | all present (CSV for results; tokens + step progress for streaming) | jsdom E2E |
| 5 | LangGraph, any backend, relational DB, schema-only generation | ✅ | LangGraph + FastAPI + SQLite; schema introspected | all |
| 6 | Workflow: intent → scope → schema → generate → validate → optimize → execute → explain → respond | ✅ | same node names, plus retry loops | `docs/workflow.mmd` (auto-generated, test-checked) |
| 7 | Guardrails: SQL-only, no destructive, no hallucinated tables/columns, validate, handle ambiguity | ✅ | see README "Guardrails" | `test_validator.py`, `test_graph.py` |
| 5 | LLM | ✅ | Google Gemini via `langchain-google-genai` (default `gemini-2.5-flash`, set by `LLM_MODEL`); provider is swappable | `test_llm_config.py` |
| 8 | Deliverables: code, README, architecture diagram, workflow explanation, prompts, schema, demo video, deployed link | ⚠️ | code/README/diagrams/prompts/schema done. **Demo video and deployed URL still need to be produced by you**; placeholders are at the top of the README | n/a |
| 10 | Bonus: execution, follow-ups, cost estimate, highlighting, multi-dialect, CSV, injection protection, Docker, tests | ✅ | all implemented | see README feature table |
