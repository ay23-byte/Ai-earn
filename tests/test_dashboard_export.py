import json
import os
from pathlib import Path
import tempfile
import unittest
from ai_earn.dashboard_cli import write_snapshot

@unittest.skipUnless(os.name in {'posix', 'nt'}, 'supported filesystem required')
class DashboardSnapshotWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'private'
        if os.name == 'nt':
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)

    def test_writes_valid_json(self):
        path = self.directory / 'snapshot.json'
        payload = {'format': 'ai-earn-dashboard-snapshot', 'version': 1, 'metrics': {'read_only': True}}
        write_snapshot(path, payload)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), payload)
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_file_is_not_overwritten(self):
        path = self.directory / 'snapshot.json'
        path.write_text('keep', encoding='utf-8')
        with self.assertRaises(OSError):
            write_snapshot(path, {'format': 'ai-earn-dashboard-snapshot'})
        self.assertEqual(path.read_text(encoding='utf-8'), 'keep')

    def test_bad_payload_does_not_create_file(self):
        path = self.directory / 'snapshot.json'
        with self.assertRaises(TypeError):
            write_snapshot(path, {'bad': object()})
        self.assertFalse(path.exists())

if __name__ == '__main__':
    unittest.main()
