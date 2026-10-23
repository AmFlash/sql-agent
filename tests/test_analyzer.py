from app.analyzer import analyze, same_results, to_sqlite
from app.schema_utils import load_schema, schema_to_prompt

DB = "db/sample.db"
SCHEMA = load_schema(DB)


def test_primary_key_lookup_is_cheap_and_needs_no_index():
    r = analyze("SELECT * FROM Employees WHERE EmployeeID = 5", "sqlite", SCHEMA, DB)
    assert r["cost"]["level"] == "low" and r["cost"]["full_scans"] == [] and r["indexes"] == []


def test_full_scan_detected_and_index_suggested():
    r = analyze("SELECT * FROM Customers WHERE State = 'CA'", "sqlite", SCHEMA, DB)
    assert r["cost"]["full_scans"] == ["Customers"]
    assert r["indexes"] == ["CREATE INDEX idx_customers_state ON Customers(State);"]


def test_no_index_suggested_on_function_wrapped_column():
    r = analyze("SELECT * FROM Customers WHERE lower(City) = 'austin'", "sqlite", SCHEMA, DB, "optimize")
    assert r["indexes"] == [] and any("LOWER" in t for t in r["tips"])


def test_equality_before_range_columns():
    r = analyze("SELECT * FROM Orders WHERE OrderDate >= '2024-01-01' AND Status = 'Pending'", "sqlite", SCHEMA, DB)
    assert r["indexes"] == ["CREATE INDEX idx_orders_status_orderdate ON Orders(Status, OrderDate);"]


def test_lint_tips_only_in_optimize_mode():
    sql = "SELECT * FROM Customers WHERE Name LIKE '%son'"
    assert analyze(sql, "sqlite", SCHEMA, DB, "generate")["tips"] == []
    tips = analyze(sql, "sqlite", SCHEMA, DB, "optimize")["tips"]
    assert any("SELECT *" in t for t in tips) and any("wildcard" in t for t in tips)


def test_not_in_subquery_and_unused_join_tips():
    t1 = analyze("SELECT Name FROM Customers WHERE CustomerID NOT IN (SELECT CustomerID FROM Orders)",
                 "sqlite", SCHEMA, DB, "optimize")["tips"]
    assert any("NOT EXISTS" in t for t in t1)
    t2 = analyze("SELECT e.FirstName FROM Employees e LEFT JOIN Departments d ON e.DepartmentID = d.DepartmentID",
                 "sqlite", SCHEMA, DB, "optimize")["tips"]
    assert any("Departments is joined but none of its columns are used" in t for t in t2)


def test_used_join_is_not_flagged():
    t = analyze("SELECT e.FirstName, d.Name FROM Employees e JOIN Departments d ON e.DepartmentID = d.DepartmentID",
                "sqlite", SCHEMA, DB, "optimize")["tips"]
    assert not any("none of its columns" in x for x in t)


def test_cost_grows_with_nested_scans():
    cheap = analyze("SELECT * FROM Products", "sqlite", SCHEMA, DB)["cost"]["est_rows_examined"]
    pricey = analyze("SELECT * FROM OrderItems oi JOIN Orders o ON o.OrderID = oi.OrderID", "sqlite", SCHEMA, DB)["cost"]
    assert pricey["est_rows_examined"] > cheap and pricey["summary"]


def test_dialect_bridge_and_analysis_for_postgres():
    assert to_sqlite("SELECT * FROM Customers WHERE Name ILIKE 'a%'", "postgresql")
    r = analyze("SELECT * FROM Customers WHERE State = 'CA'", "postgresql", SCHEMA, DB)
    assert r["cost"] and r["indexes"]


def test_same_results():
    assert same_results(DB, "SELECT * FROM Customers WHERE State='CA'",
                        "SELECT CustomerID,Name,Email,City,State FROM Customers WHERE State IN ('CA')") is True
    assert same_results(DB, "SELECT * FROM Customers", "SELECT * FROM Customers WHERE State='CA'") is False
    assert same_results(DB, "SELECT * FROM Nope", "SELECT 1") is None


def test_schema_prompt_contains_real_values_for_accuracy():
    text = schema_to_prompt(SCHEMA)
    assert "Customers.State is one of: 'CA'" in text and "Orders.Status is one of" in text
    assert "RANGE Employees.HireDate" in text and "30 rows" in text


def test_pii_columns_and_opt_out_keep_values_out_of_the_prompt(monkeypatch):
    text = schema_to_prompt(SCHEMA)
    assert "LastName is one of" not in text and "Email" in text and "cust1@mail.com" not in text
    monkeypatch.setenv("SCHEMA_SAMPLES", "0")
    bare = schema_to_prompt(load_schema(DB))
    assert "VALUES" not in bare and "RANGE" not in bare and "EXAMPLES" not in bare and "TABLE Customers" in bare


def test_uncorrelated_subquery_is_not_costed_as_a_nested_loop():
    r = analyze("SELECT * FROM Products WHERE ProductID NOT IN (SELECT ProductID FROM OrderItems)",
                "sqlite", SCHEMA, DB)
    assert r["cost"]["level"] == "low" and r["cost"]["est_rows_examined"] < 400


def test_sample_data_has_empty_set_cases_for_demo_and_eval():
    from app.executor import run_readonly
    never = run_readonly(DB, "SELECT Name FROM Products WHERE ProductID NOT IN (SELECT ProductID FROM OrderItems)")
    assert never["row_count"] >= 1
    idle = run_readonly(DB, "SELECT * FROM Customers WHERE CustomerID NOT IN (SELECT CustomerID FROM Orders)")
    assert idle["row_count"] >= 1
