from .sqlite_store import SQLiteStore
BACKENDS={"sqlite":SQLiteStore}
def get(name="sqlite"): return BACKENDS[name]
