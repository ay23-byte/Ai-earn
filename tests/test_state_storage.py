"""Phase 3 storage tests use disposable databases only."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class StateStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        self.path = self.directory / "state.sqlite3"

    def create(self):
        return StateStorage(self.path, create=True, confirmed=True)

    def test_create_requires_explicit_confirmation(self):
        with self.assertRaises(StateStorageError):
            StateStorage(self.path, create=True)
        self.assertFalse(self.path.exists())
        with self.assertRaises(StateStorageError):
            StateStorage(self.path, create=True, confirmed=1)
        self.assertFalse(self.path.exists())

    def test_failed_bootstrap_rolls_back_and_is_not_silently_reinitialized(self):
        with patch("ai_earn.state_storage.time.time", side_effect=RuntimeError("test failure")):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                self.create()
        self.assertTrue(self.path.exists())
        with self.assertRaises(StateStorageError):
            StateStorage(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])

    def test_new_store_is_versioned_and_can_be_reopened(self):
        store = self.create()
        with store.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT version FROM state_migrations").fetchone()[0], 1)
        with StateStorage(self.path).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_create_never_overwrites_existing_file(self):
        self.path.write_bytes(b"preserve this file")
        with self.assertRaises(StateStorageError):
            self.create()
        self.assertEqual(self.path.read_bytes(), b"preserve this file")

    def test_creation_refuses_existing_sqlite_sidecars(self):
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            sidecar.write_bytes(b"preserve sidecar")
            with self.assertRaises(StateStorageError):
                self.create()
            self.assertFalse(self.path.exists())
            self.assertEqual(sidecar.read_bytes(), b"preserve sidecar")
            sidecar.unlink()

    def test_open_missing_and_uninitialized_files_is_non_destructive(self):
        with self.assertRaises(StateStorageError):
            StateStorage(self.path)
        self.assertFalse(self.path.exists())
        self.path.touch(mode=0o600)
        with self.assertRaises(StateStorageError):
            StateStorage(self.path)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_auth_database_is_not_treated_as_business_storage(self):
        auth_path = self.directory / "auth.sqlite3"
        AuthStorage(auth_path)
        with self.assertRaises(StateStorageError):
            StateStorage(auth_path)
        AuthStorage(auth_path, create=False)

    def test_unknown_version_and_foreign_database_are_not_modified(self):
        self.create()
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA user_version = 999")
        with self.assertRaises(StateStorageError):
            StateStorage(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 999)
        other = self.directory / "foreign.sqlite3"
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE important(value TEXT)")
            conn.execute("INSERT INTO important VALUES ('preserve')")
            conn.execute("PRAGMA user_version = 1")
        if os.name == "posix":
            os.chmod(other, 0o600)
        with self.assertRaises(StateStorageError):
            StateStorage(other)
        with sqlite3.connect(other) as conn:
            self.assertEqual(conn.execute("SELECT value FROM important").fetchone()[0], "preserve")

    def test_incomplete_history_and_missing_schema_are_refused(self):
        self.create()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM state_migrations")
        with self.assertRaises(StateStorageError):
            StateStorage(self.path)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO state_migrations VALUES (1, 0)")
            conn.execute("DROP TABLE state_meta")
        with self.assertRaises(StateStorageError):
            StateStorage(self.path)

    def test_changed_version_is_refused_before_transaction(self):
        store = self.create()
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA user_version = 2")
        with self.assertRaises(StateStorageError):
            with store.transaction():
                self.fail("Must not expose incompatible state")

    def test_transaction_rolls_back_after_exception(self):
        store = self.create()
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with store.transaction() as conn:
                conn.execute("INSERT INTO state_meta(key,value) VALUES (?,?)", ("test", "value"))
                raise RuntimeError("test failure")
        with store.transaction() as conn:
            self.assertIsNone(conn.execute("SELECT value FROM state_meta WHERE key='test'").fetchone())

    def test_concurrent_transactions_serialize_and_commit(self):
        store = self.create()

        def write(key):
            with store.transaction() as conn:
                conn.execute("INSERT INTO state_meta(key,value) VALUES (?,?)", (key, "value"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, key) for key in ("one", "two")]
            for future in futures:
                future.result(timeout=10)
        with store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_meta").fetchone()[0], 2)

    def test_backup_and_restore_create_separate_checked_copies(self):
        store = self.create()
        with store.transaction() as conn:
            conn.execute("INSERT INTO state_meta(key,value) VALUES (?,?)", ("test", "value"))
        backup = self.directory / "backup # 100%.sqlite3"
        store.backup_to(backup, confirmed=True)
        restored = self.directory / "restore.sqlite3"
        StateStorage(backup).backup_to(restored, confirmed=True)
        with StateStorage(restored).transaction() as conn:
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='test'").fetchone()[0], "value")
        self.assertNotEqual(self.path, backup)
        self.assertNotEqual(backup, restored)

    def test_backup_requires_confirmation_and_never_overwrites(self):
        store = self.create()
        backup = self.directory / "backup.sqlite3"
        with self.assertRaises(StateStorageError):
            store.backup_to(backup)
        with self.assertRaises(StateStorageError):
            store.backup_to(backup, confirmed=1)
        self.assertFalse(backup.exists())
        backup.write_bytes(b"existing backup")
        with self.assertRaises(StateStorageError):
            store.backup_to(backup, confirmed=True)
        self.assertEqual(backup.read_bytes(), b"existing backup")
        with self.assertRaises(StateStorageError):
            store.backup_to(self.path, confirmed=True)

    def test_backup_refuses_existing_sidecar_without_modifying_it(self):
        store = self.create()
        backup = self.directory / "backup.sqlite3"
        sidecar = Path(str(backup) + "-journal")
        sidecar.write_bytes(b"preserve sidecar")
        with self.assertRaises(StateStorageError):
            store.backup_to(backup, confirmed=True)
        self.assertFalse(backup.exists())
        self.assertEqual(sidecar.read_bytes(), b"preserve sidecar")

    def test_failed_backup_leaves_new_file_for_inspection(self):
        store = self.create()
        backup = self.directory / "partial.sqlite3"
        with patch("ai_earn.state_storage._copy_database", side_effect=sqlite3.OperationalError("test failure")):
            with self.assertRaises(StateStorageError):
                store.backup_to(backup, confirmed=True)
        self.assertTrue(backup.exists())
        with store.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_integrity_failure_in_copy_is_reported_and_left_for_inspection(self):
        store = self.create()
        backup = self.directory / "partial.sqlite3"
        with patch("ai_earn.state_storage._check_integrity", side_effect=[None, StateStorageError("bad copy")]):
            with self.assertRaises(StateStorageError):
                store.backup_to(backup, confirmed=True)
        self.assertTrue(backup.exists())

    def test_corrupt_source_is_not_backed_up(self):
        store = self.create()
        with self.path.open("r+b") as f:
            f.truncate(100)
        backup = self.directory / "backup.sqlite3"
        with self.assertRaises(StateStorageError):
            store.backup_to(backup, confirmed=True)
        self.assertFalse(backup.exists())

    def test_deleted_between_check_and_open_is_not_recreated(self):
        store = self.create()
        original_connect = sqlite3.connect

        def delete_then_connect(*args, **kwargs):
            self.path.unlink()
            return original_connect(*args, **kwargs)

        with patch("ai_earn.auth_storage.sqlite3.connect", side_effect=delete_then_connect):
            with self.assertRaises(StateStorageError):
                with store.transaction():
                    pass
        self.assertFalse(self.path.exists())

    def test_missing_file_during_use_is_not_recreated(self):
        store = self.create()
        self.path.unlink()
        with self.assertRaises(StateStorageError):
            with store.transaction():
                pass
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX mode and symlink check")
    def test_unsafe_parent_and_symlink_refused(self):
        directory = Path(self.tmp.name) / "public"
        directory.mkdir(mode=0o755)
        os.chmod(directory, 0o755)
        unsafe = directory / "state.sqlite3"
        with self.assertRaises(StateStorageError):
            StateStorage(unsafe, create=True, confirmed=True)
        self.assertFalse(unsafe.exists())
        self.create()
        link = self.directory / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(StateStorageError):
            StateStorage(link)

    def test_relative_paths_rejected(self):
        with self.assertRaises(StateStorageError):
            StateStorage(Path("relative.sqlite3"), create=True, confirmed=True)
