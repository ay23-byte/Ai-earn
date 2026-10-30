"""Guard and POSIX rehearsal of the candidate utility; not Windows evidence."""
import io
import os
import unittest
from unittest.mock import patch

from tools import windows_candidate_smoke as smoke


class CandidateSmokeTests(unittest.TestCase):
    def test_refuses_non_windows_and_disabled_gate_before_storage(self):
        for windows, enabled in ((False, True), (True, False)):
            with self.subTest(windows=windows, enabled=enabled), \
                    patch.object(smoke, "_is_windows", return_value=windows), \
                    patch.object(smoke.auth_storage, "_windows_auth_verified", return_value=enabled), \
                    patch.object(smoke, "create_private_directory") as create:
                self.assertEqual(smoke.main(io.StringIO(), io.StringIO()), 2)
                create.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX rehearsal only")
    def test_real_auth_flow_and_temporary_cleanup_on_posix(self):
        directories = []
        def create(path):
            directories.append(path)
            path.mkdir(mode=0o700)
        out, err = io.StringIO(), io.StringIO()
        # These substitutions are for the POSIX rehearsal only. The Windows
        # candidate run makes no gate or storage override.
        with patch.object(smoke, "_is_windows", return_value=True), \
                patch.object(smoke.auth_storage, "_windows_auth_verified", return_value=True), \
                patch.object(smoke, "create_private_directory", side_effect=create):
            self.assertEqual(smoke.main(out, err), 0)
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())
        self.assertIn("CANDIDATE TEST COMPLETE", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(smoke._PASSWORD, out.getvalue())
        self.assertNotIn(smoke._NEW_PASSWORD, out.getvalue())

    def test_failure_does_not_leak_diagnostics_and_cleans_up(self):
        directories = []
        def create(path):
            directories.append(path)
            path.mkdir()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(smoke, "_is_windows", return_value=True), \
                patch.object(smoke.auth_storage, "_windows_auth_verified", return_value=True), \
                patch.object(smoke, "create_private_directory", side_effect=create), \
                patch.object(smoke, "_exercise", side_effect=RuntimeError("sensitive diagnostic")):
            self.assertEqual(smoke.main(out, err), 1)
        self.assertFalse(directories[0].exists())
        self.assertNotIn("sensitive diagnostic", out.getvalue() + err.getvalue())
        self.assertNotIn("CANDIDATE TEST COMPLETE", out.getvalue())
