"""Make `pytest` work on a fresh clone: build db/sample.db if it does not exist yet."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "db" / "sample.db").exists():
    spec = importlib.util.spec_from_file_location("seed", ROOT / "db" / "seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()
