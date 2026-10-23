"""Create db/sample.db from schema.sql and fill it with sample data.

    python db/seed.py                       # built-in sample schema + generated data
    python db/seed.py --sql my_schema.sql   # load YOUR schema (CREATE TABLE ... [+ INSERTs]) instead
"""
import argparse
import random
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "sample.db"


def load_custom(sql_path: str) -> None:
    """Build the database from a user-supplied SQL script (e.g. the schema provided with the assignment)."""
    DB.unlink(missing_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(Path(sql_path).read_text())
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
    con.close()
    print(f"Created {DB} from {sql_path} ({n} tables)")


def main():
    random.seed(42)
    DB.unlink(missing_ok=True)
    con = sqlite3.connect(DB)
    con.executescript((HERE / "schema.sql").read_text())

    depts = [(1, "Engineering", "San Francisco"), (2, "Sales", "New York"),
             (3, "HR", "Austin"), (4, "Support", "Denver")]
    con.executemany("INSERT INTO Departments VALUES (?,?,?)", depts)

    first = ["Ava", "Liam", "Maya", "Noah", "Zoe", "Ethan", "Priya", "Omar", "Lena", "Jack",
             "Sara", "Ravi", "Mia", "Leo", "Nina", "Tom", "Ivy", "Sam", "Dana", "Raj"]
    last = ["Smith", "Patel", "Garcia", "Chen", "Brown", "Khan", "Lopez", "Kim", "Nguyen", "Davis"]
    hire_dates = ["2021-03-15", "2022-07-01", "2023-05-20", "2023-11-30", "2024-01-01",
                  "2024-02-14", "2024-06-10", "2024-09-23", "2025-01-15", "2025-04-02"]
    for i in range(1, 21):
        mgr = None if i <= 3 else random.randint(1, 3)
        con.execute(
            "INSERT INTO Employees VALUES (?,?,?,?,?,?,?,?)",
            (i, first[i - 1], random.choice(last), f"{first[i-1].lower()}@corp.com",
             hire_dates[(i - 1) % len(hire_dates)], random.randint(55, 160) * 1000,
             random.randint(1, 4), mgr),
        )

    states = [("Los Angeles", "CA"), ("San Diego", "CA"), ("San Jose", "CA"), ("Austin", "TX"),
              ("Houston", "TX"), ("New York", "NY"), ("Buffalo", "NY"), ("Seattle", "WA"),
              ("Miami", "FL"), ("Denver", "CO")]
    for i in range(1, 31):
        city, st = states[(i - 1) % len(states)]
        con.execute("INSERT INTO Customers VALUES (?,?,?,?,?)",
                    (i, f"Customer {i}", f"cust{i}@mail.com", city, st))

    products = [("Laptop", "Electronics", 1200), ("Phone", "Electronics", 800),
                ("Headphones", "Electronics", 150), ("Desk", "Furniture", 300),
                ("Chair", "Furniture", 180), ("Notebook", "Stationery", 5),
                ("Pen Pack", "Stationery", 12), ("Monitor", "Electronics", 350),
                # the last two are deliberately never ordered (for "products never ordered" style questions)
                ("Webcam", "Electronics", 90), ("Standing Mat", "Furniture", 45)]
    for i, p in enumerate(products, 1):
        con.execute("INSERT INTO Products VALUES (?,?,?,?)", (i, *p))

    statuses = ["Completed", "Completed", "Completed", "Pending", "Cancelled"]
    item_id = 1
    for oid in range(1, 81):
        month, day = random.randint(1, 12), random.randint(1, 28)
        year = random.choice([2023, 2024, 2025])
        con.execute("INSERT INTO Orders VALUES (?,?,?,?,?)",
                    (oid, random.randint(1, 26), random.randint(1, 20),   # customers 27-30 never order
                     f"{year}-{month:02d}-{day:02d}", random.choice(statuses)))
        for pid in random.sample(range(1, 9), random.randint(1, 3)):  # products 9-10 never ordered
            price = products[pid - 1][2]
            con.execute("INSERT INTO OrderItems VALUES (?,?,?,?,?)",
                        (item_id, oid, pid, random.randint(1, 5), price))
            item_id += 1
    con.commit()
    con.close()
    print(f"Created {DB}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql", help="path to a .sql script that defines (and optionally fills) your own schema")
    args = ap.parse_args()
    load_custom(args.sql) if args.sql else main()
