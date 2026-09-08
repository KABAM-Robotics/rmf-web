import sqlite3
import tempfile
import unittest
from pathlib import Path

from api_server.task_log_migration import KEYS, migrate, missing_keys


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(self.path) as c:
            c.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE taskeventlog(task_id TEXT PRIMARY KEY);
                CREATE TABLE taskstate(id TEXT PRIMARY KEY, data TEXT);
                CREATE TABLE taskeventlogphases(id INTEGER PRIMARY KEY,task_id TEXT REFERENCES taskeventlog(task_id),phase TEXT);
                CREATE TABLE taskeventlogphasesevents(id INTEGER PRIMARY KEY,phase_id INTEGER REFERENCES taskeventlogphases(id),event TEXT);
                INSERT INTO taskeventlog VALUES ('task');
                INSERT INTO taskstate VALUES ('task','untouched');
                INSERT INTO taskeventlogphases VALUES (1,'task','1'),(2,'task','1');
                INSERT INTO taskeventlogphasesevents VALUES (1,1,'1'),(2,2,'1');
            """)
            for table, parent, target in [
                ("taskeventloglog", "task_id", "taskeventlog(task_id)"),
                ("taskeventlogphaseslog", "phase_id", "taskeventlogphases(id)"),
                ("taskeventlogphaseseventslog", "event_id", "taskeventlogphasesevents(id)"),
            ]:
                c.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY,seq INTEGER,unix_millis_time INTEGER,tier TEXT,text TEXT,{parent} REFERENCES {target},UNIQUE(id,seq))")
                c.execute(f"INSERT INTO {table} VALUES (1,0,123,'info','original',?)", ("task" if parent=="task_id" else 1,))
                c.execute(f"INSERT INTO {table} VALUES (2,0,123,'info','original',?)", ("task" if parent=="task_id" else 2,))
                c.execute(f"INSERT INTO {table} VALUES (3,1,124,'info','new',?)", ("task" if parent=="task_id" else 2,))

    def dump(self):
        with sqlite3.connect(self.path) as c:
            return list(c.iterdump())

    def test_merge_exact_duplicates_preserves_all_distinct_history(self):
        original = self.dump()
        result = migrate(self.path)
        self.assertTrue(result["changed"])
        backup = Path(result["backup"])
        self.assertEqual(0o600, backup.stat().st_mode & 0o777)
        with sqlite3.connect(backup) as c:
            self.assertEqual(original, list(c.iterdump()))
        with sqlite3.connect(self.path) as c:
            self.assertFalse(missing_keys(c))
            self.assertEqual([], c.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual([('task', 'untouched')], c.execute("SELECT * FROM taskstate").fetchall())
            self.assertEqual(1, c.execute("SELECT count(*) FROM taskeventlogphases").fetchone()[0])
            self.assertEqual(1, c.execute("SELECT count(*) FROM taskeventlogphasesevents").fetchone()[0])
            for table in ("taskeventloglog", "taskeventlogphaseslog", "taskeventlogphaseseventslog"):
                self.assertEqual([(0,'original'),(1,'new')], c.execute(f"SELECT seq,text FROM {table} ORDER BY seq").fetchall())
        migrated = self.dump()
        self.assertFalse(migrate(self.path)["changed"])
        self.assertEqual(migrated, self.dump())

    def test_conflicting_text_rolls_back_prior_changes(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE taskeventlogphaseseventslog SET text='conflict' WHERE id=2")
        original = self.dump()
        with self.assertRaisesRegex(RuntimeError, "Conflicting task logs"):
            migrate(self.path)
        self.assertEqual(original, self.dump())
        self.assertEqual(1, len(list(self.path.parent.glob('*.pre-log-v1-*'))))

    def test_conflicting_time_or_tier_is_not_silently_discarded(self):
        for column,value in [("unix_millis_time",999),("tier","error")]:
            with self.subTest(column=column):
                with sqlite3.connect(self.path) as c:
                    c.execute(f"UPDATE taskeventlogphaseseventslog SET {column}=? WHERE id=2",(value,))
                original = self.dump()
                with self.assertRaisesRegex(RuntimeError, "Conflicting task logs"):
                    migrate(self.path)
                self.assertEqual(original, self.dump())

    def test_constraints_reject_duplicate_future_writes(self):
        migrate(self.path)
        with sqlite3.connect(self.path) as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO taskeventlogphaseseventslog VALUES (10,0,123,'info','original',1)")

    def test_wal_backup_contains_committed_history(self):
        c = sqlite3.connect(self.path)
        self.addCleanup(c.close)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("INSERT INTO taskstate VALUES ('wal','retained')")
        c.commit()
        result = migrate(self.path)
        with sqlite3.connect(result["backup"]) as saved:
            self.assertEqual(('retained',), saved.execute("SELECT data FROM taskstate WHERE id='wal'").fetchone())

    def test_restore_backup_into_separate_database_recovers_original_schema_and_data(self):
        original = self.dump()
        result = migrate(self.path)
        restored = self.path.parent / 'restored.sqlite3'
        source = sqlite3.connect('file:'+result['backup']+'?mode=ro', uri=True)
        target = sqlite3.connect(restored)
        try:
            source.backup(target)
            self.assertEqual(original, list(target.iterdump()))
            self.assertEqual([('ok',)], target.execute('PRAGMA integrity_check').fetchall())
            self.assertTrue(missing_keys(target))  # This really restored the old schema.
            target.execute("INSERT INTO taskeventlogphaseseventslog VALUES (4,2,125,'info','after rollback',1)")
            target.commit()
            self.assertEqual(('after rollback',), target.execute('SELECT text FROM taskeventlogphaseseventslog WHERE id=4').fetchone())
        finally:
            source.close()
            target.close()

    def test_invalid_database_and_symlink_refused(self):
        missing = self.path.parent / 'absent.sqlite3'
        with self.assertRaises(ValueError):
            migrate(missing)
        self.assertFalse(missing.exists())
        link = self.path.parent / 'link.sqlite3'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            migrate(link)

    def test_foreign_key_corruption_refused(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE taskeventlogphasesevents SET phase_id=999 WHERE id=2")
        original = self.dump()
        with self.assertRaisesRegex(RuntimeError, "foreign-key violations"):
            migrate(self.path)
        self.assertEqual(original, self.dump())


if __name__ == '__main__':
    unittest.main()
