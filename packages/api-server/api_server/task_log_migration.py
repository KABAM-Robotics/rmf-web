"""Explicit, backed-up SQLite task-log migration; run with API writers stopped.

Usage: python -m api_server.task_log_migration /absolute/path/to/database.sqlite3
Never deletes a database. Conflicting records abort instead of choosing a value.
Application startup only verifies the schema; it does not silently migrate data.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile

KEYS = {
    "taskeventlogphases": ("task_id", "phase"),
    "taskeventlogphasesevents": ("phase_id", "event"),
    "taskeventloglog": ("task_id", "seq"),
    "taskeventlogphaseslog": ("phase_id", "seq"),
    "taskeventlogphaseseventslog": ("event_id", "seq"),
}


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def missing_keys(conn):
    missing = []
    for table, key in KEYS.items():
        indexes = conn.execute(f"PRAGMA index_list({_q(table)})").fetchall()
        found = any(row[2] and not row[4] and tuple(
            r[2] for r in conn.execute(f"PRAGMA index_info({_q(row[1])})")
        ) == key for row in indexes)
        if not found:
            missing.append(table)
    return missing


async def verify_task_log_schema():
    from tortoise import Tortoise
    conn = Tortoise.get_connection("default")
    if conn.capabilities.dialect != "sqlite":
        return  # This migration is specifically for the Waterloo SQLite runtime.
    for table, key in KEYS.items():
        found = False
        for row in await conn.execute_query_dict(f"PRAGMA index_list({_q(table)})"):
            if not row["unique"] or row["partial"]:
                continue
            columns = await conn.execute_query_dict(f"PRAGMA index_info({_q(row['name'])})")
            if tuple(c["name"] for c in columns) == key:
                found = True
                break
        if not found:
            raise RuntimeError(
                "Task-log schema needs explicit backed-up migration before startup: "
                f"{table} lacks unique {key}. Stop API writers and run "
                "python -m api_server.task_log_migration <database-path>."
            )


def migrate(database):
    path = Path(database)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("Database must be an existing absolute regular file, not a symlink")
    conn = sqlite3.connect(path.as_uri()+"?mode=rw", uri=True, timeout=30)
    backup = None
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")  # Serialize with any other attempted writer.
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Database integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Database already contains foreign-key violations")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(KEYS).issubset(tables):
            raise RuntimeError("Not a complete API-server task-log schema")
        if not missing_keys(conn):
            conn.rollback()
            return {"changed": False, "backup": None, "removed": {}}

        # The separate reader sees the committed pre-migration state while this
        # connection holds the writer reservation. SQLite backup includes WAL.
        fd, backup = tempfile.mkstemp(prefix=path.name+".pre-log-v1-", suffix=".sqlite3", dir=path.parent)
        os.close(fd)  # mkstemp creates mode 0600; never overwrite an older backup.
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(backup)) as target:
                reader.backup(target)
        with open(backup, "rb") as saved:
            os.fsync(saved.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        conn.execute("CREATE TEMP TABLE phase_map AS SELECT id AS old_id, MIN(id) OVER (PARTITION BY task_id,phase) AS new_id FROM taskeventlogphases")
        conn.execute("CREATE TEMP TABLE event_map AS SELECT e.id AS old_id, MIN(e.id) OVER (PARTITION BY p.new_id,e.event) AS new_id FROM taskeventlogphasesevents e JOIN phase_map p ON e.phase_id=p.old_id")
        for name in ("phase_map", "event_map"):
            conn.execute(f"CREATE UNIQUE INDEX {name}_old ON {name}(old_id)")

        mappings = {
            "taskeventloglog": ("task_id", None),
            "taskeventlogphaseslog": ("phase_id", "phase_map"),
            "taskeventlogphaseseventslog": ("event_id", "event_map"),
        }
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in KEYS}
        for table, (parent, mapping) in mappings.items():
            canonical = "m.new_id" if mapping else "l."+parent
            join = f"JOIN {mapping} m ON m.old_id=l.{parent}" if mapping else ""
            # Same logical sequence but different text/time/severity is NOT an
            # exact replay. Preserve everything and require explicit review.
            conflict = conn.execute(f"""SELECT parent_key,seq FROM (
                SELECT {canonical} AS parent_key,l.seq,l.unix_millis_time,l.tier,l.text
                FROM {table} l {join}
                GROUP BY {canonical},l.seq,l.unix_millis_time,l.tier,l.text
                ) GROUP BY parent_key,seq HAVING COUNT(*)>1 LIMIT 1""").fetchone()
            if conflict:
                raise RuntimeError(f"Conflicting task logs in {table}; migration rolled back; backup={backup}")
            conn.execute(f"""DELETE FROM {table} WHERE id NOT IN (
                SELECT MIN(l.id) FROM {table} l {join} GROUP BY {canonical},l.seq)""")
            if mapping:
                conn.execute(f"UPDATE {table} SET {parent}=(SELECT new_id FROM {mapping} WHERE old_id={table}.{parent})")

        conn.execute("DELETE FROM taskeventlogphasesevents WHERE id NOT IN (SELECT new_id FROM event_map)")
        conn.execute("UPDATE taskeventlogphasesevents SET phase_id=(SELECT new_id FROM phase_map WHERE old_id=taskeventlogphasesevents.phase_id)")
        conn.execute("DELETE FROM taskeventlogphases WHERE id NOT IN (SELECT new_id FROM phase_map)")
        for table, key in KEYS.items():
            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_q('waterloo_log_v1_'+table)} ON {_q(table)} ({','.join(map(_q,key))})")
        if missing_keys(conn) or conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Post-migration uniqueness or foreign-key verification failed")
        removed = {t: before[t]-conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in KEYS}
        conn.commit()
        return {"changed": True, "backup": backup, "removed": removed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    print(json.dumps(migrate(parser.parse_args().database), indent=2))
