"""Explicit state schema upgrades use disposable databases only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_earn.state_storage import StateStorage, StateStorageError


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(directory)
        else:
            directory.mkdir(mode=0o700)
        self.path = directory / "state.sqlite3"
        self.backup = directory / "before.sqlite3"
        self.store = StateStorage(self.path, create=True, confirmed=True)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO state_meta VALUES ('marker', 'original')")

    def version(self, store):
        with store.transaction() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def test_explicit_backup_first_upgrade_and_restore(self):
        self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertEqual(self.version(StateStorage(self.path)), 2)
        before = StateStorage(self.backup)
        self.assertEqual(self.version(before), 1)
        with self.store.transaction() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT version FROM state_migrations ORDER BY version")], [1, 2])
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='marker'").fetchone()[0], 'original')
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations").fetchone()[0], 0)
        restored_path = self.path.parent / "restored.sqlite3"
        before.backup_to(restored_path, confirmed=True)
        self.assertEqual(self.version(StateStorage(restored_path)), 1)
        StateStorage(restored_path).migrate_to_v2(self.path.parent / "restored-backup.sqlite3", confirmed=True)
        self.assertEqual(self.version(StateStorage(restored_path)), 2)

    def test_migration_works_with_wal_journal_mode(self):
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertEqual(self.version(self.store), 2)
        self.assertEqual(self.version(StateStorage(self.backup)), 1)

    def test_v2_backup_and_restore_remain_usable(self):
        self.store.migrate_to_v2(self.backup, confirmed=True)
        v2_backup = self.path.parent / "v2.sqlite3"
        self.store.backup_to(v2_backup, confirmed=True)
        restored = self.path.parent / "v2-restored.sqlite3"
        StateStorage(v2_backup).backup_to(restored, confirmed=True)
        with StateStorage(restored).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='marker'").fetchone()[0], 'original')

    def test_v2_with_modified_constraints_is_refused(self):
        self.store.migrate_to_v2(self.backup, confirmed=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TABLE state_audit")
            conn.execute("""CREATE TABLE state_audit (
                id INTEGER PRIMARY KEY, occurred_at INTEGER NOT NULL,
                actor_id TEXT NOT NULL, event TEXT NOT NULL,
                resource_type TEXT NOT NULL, resource_id TEXT NOT NULL
            )""")
        with self.assertRaises(StateStorageError):
            StateStorage(self.path)
        self.assertEqual(self.version(StateStorage(self.backup)), 1)

    def test_upgrade_requires_confirmation_and_never_overwrites_backup(self):
        for confirmed in (False, 1):
            with self.assertRaises(StateStorageError):
                self.store.migrate_to_v2(self.backup, confirmed=confirmed)
            self.assertFalse(self.backup.exists())
        self.backup.write_bytes(b"do not overwrite")
        with self.assertRaises(StateStorageError):
            self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertEqual(self.backup.read_bytes(), b"do not overwrite")
        self.assertEqual(self.version(self.store), 1)
        with self.assertRaises(StateStorageError):
            self.store.migrate_to_v2(self.path, confirmed=True)
        self.assertEqual(self.version(self.store), 1)

    def test_no_repeat_upgrade_or_backup_for_up_to_date_database(self):
        self.store.migrate_to_v2(self.backup, confirmed=True)
        second = self.path.parent / "second.sqlite3"
        with self.assertRaises(StateStorageError):
            self.store.migrate_to_v2(second, confirmed=True)
        self.assertFalse(second.exists())
        self.assertEqual(self.version(self.store), 2)

    def test_backup_failure_does_not_upgrade_source(self):
        with patch("ai_earn.state_storage._copy_database", side_effect=sqlite3.OperationalError("copy failed")):
            with self.assertRaises(StateStorageError):
                self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.version(self.store), 1)

    def test_failed_backup_verification_prevents_migration(self):
        with patch("ai_earn.state_storage._check_integrity", side_effect=[None, StateStorageError("bad backup")]):
            with self.assertRaises(StateStorageError):
                self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.version(self.store), 1)

    def test_migration_error_rolls_back_ddl_and_preserves_backup(self):
        def fail(conn):
            conn.execute("CREATE TABLE state_audit (id INTEGER)")
            raise RuntimeError("simulated failure")
        with patch("ai_earn.state_storage._apply_v2", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertEqual(self.version(self.store), 1)
        self.assertEqual(self.version(StateStorage(self.backup)), 1)
        with self.store.transaction() as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='state_audit'").fetchone())
            self.assertEqual(conn.execute("SELECT count(*) FROM state_migrations").fetchone()[0], 1)

    def test_process_crash_leaves_recoverable_pre_migration_backup(self):
        # The child exits after uncommitted DDL, not through Python cleanup.
        code = '''
import os, sys
from pathlib import Path
import ai_earn.state_storage as storage

def crash(conn):
    conn.execute("CREATE TABLE state_audit (id INTEGER)")
    os._exit(37)

storage._apply_v2 = crash
storage.StateStorage(Path(sys.argv[1])).migrate_to_v2(Path(sys.argv[2]), confirmed=True)
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.path), str(self.backup)], timeout=20)
        self.assertEqual(result.returncode, 37)
        backup = StateStorage(self.backup)
        self.assertEqual(self.version(backup), 1)
        restored = self.path.parent / "crash-restored.sqlite3"
        backup.backup_to(restored, confirmed=True)
        with StateStorage(restored).transaction() as conn:
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='marker'").fetchone()[0], 'original')

    def test_unknown_source_schema_never_creates_backup(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("PRAGMA user_version = 999")
        with self.assertRaises(StateStorageError):
            self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertFalse(self.backup.exists())
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_bad_or_missing_source_never_creates_backup(self):
        self.path.unlink()
        with self.assertRaises(StateStorageError):
            self.store.migrate_to_v2(self.backup, confirmed=True)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.backup.exists())

    def test_schema_constraints_and_duplicate_idempotency_keys(self):
        self.store.migrate_to_v2(self.backup, confirmed=True)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", ('a'*32, 'request-1', b'x'*32, 'task', 'b'*32))
        with self.assertRaises(StateStorageError):
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", ('a'*32, 'request-1', b'x'*32, 'task', 'b'*32))
        with self.assertRaises(StateStorageError):
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (-1, 'a'*32, 'created', 'task', 'b'*32))
        with self.assertRaises(StateStorageError):
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", ('a'*32, 'request-2', b'x', 'task', 'b'*32))

    def test_writer_waits_for_backup_and_migration(self):
        from ai_earn.state_storage import _copy_database
        copying = threading.Event()
        release = threading.Event()
        writer_started = threading.Event()
        writer_done = threading.Event()

        def hold_copy(src, dst):
            copying.set()
            if not release.wait(5):
                raise RuntimeError("timed out waiting for test")
            _copy_database(src, dst)

        def write():
            writer_started.set()
            with self.store.transaction() as conn:
                conn.execute("UPDATE state_meta SET value='after' WHERE key='marker'")
            writer_done.set()

        with patch("ai_earn.state_storage._copy_database", side_effect=hold_copy):
            with ThreadPoolExecutor(max_workers=2) as pool:
                migration = pool.submit(self.store.migrate_to_v2, self.backup, confirmed=True)
                try:
                    self.assertTrue(copying.wait(5))
                    writer = pool.submit(write)
                    self.assertTrue(writer_started.wait(5))
                    self.assertFalse(writer_done.wait(0.1))
                finally:
                    release.set()
                migration.result(timeout=10)
                writer.result(timeout=10)
        with StateStorage(self.backup).transaction() as conn:
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='marker'").fetchone()[0], 'original')
        with self.store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='marker'").fetchone()[0], 'after')
