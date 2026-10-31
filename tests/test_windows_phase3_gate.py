"""Gate logic tests; mocks are not evidence of a real Windows run."""
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import windows_phase3_gate as gate


class FakeTest:
    def __init__(self, ident):
        self.ident = ident
    def id(self):
        return self.ident


def result(*, success=True, skipped=(), count=1, expected_failures=(), unexpected_successes=()):
    return SimpleNamespace(wasSuccessful=lambda: success, skipped=list(skipped), testsRun=count,
                           expectedFailures=list(expected_failures), unexpectedSuccesses=list(unexpected_successes))


class WindowsPhase3GateTests(unittest.TestCase):
    def test_refuses_non_windows_without_running_anything(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(gate, "_is_windows", return_value=False), patch.object(gate, "_repository_state") as state, patch.object(gate, "_run_checks") as run:
            self.assertEqual(gate.main(out, err), 2)
        state.assert_not_called()
        run.assert_not_called()
        self.assertNotIn("PASS", out.getvalue())

    def test_refuses_dirty_or_unknown_checkout(self):
        for state in (("a"*40, True), None):
            with self.subTest(state=state), patch.object(gate, "_is_windows", return_value=True), \
                 patch.object(gate, "_repository_state", return_value=state), patch.object(gate, "_run_checks") as run:
                self.assertEqual(gate.main(io.StringIO(), io.StringIO()), 2)
                run.assert_not_called()

    def test_fails_if_required_suites_absent_skipped_or_failing(self):
        windows = "tests.test_windows_auth.WindowsAuthenticationTests.test_acl"
        phase3 = "tests.test_task_associations.TaskAssociationTests.test_pair"
        expected = [windows, phase3]
        required = ("tests.test_windows_auth.WindowsAuthenticationTests.", "tests.test_task_associations.")
        strict = ("tests.test_windows_auth.WindowsAuthenticationTests.",)
        self.assertTrue(gate._valid_result(expected, result(count=2), required, strict))
        self.assertFalse(gate._valid_result(expected, result(success=False, count=2), required, strict))
        self.assertFalse(gate._valid_result(expected, result(skipped=[(FakeTest(windows), "skip")], count=2), required, strict))
        self.assertFalse(gate._valid_result(expected, result(skipped=[(FakeTest(phase3), "skip")], count=2), required, strict))
        self.assertFalse(gate._valid_result([windows], result(), required, strict))
        self.assertFalse(gate._valid_result(expected, result(count=0), required, strict))
        self.assertFalse(gate._valid_result(expected, result(count=2, expected_failures=[("case", "reason")]), required, strict))
        self.assertFalse(gate._valid_result(expected, result(count=2, unexpected_successes=["case"]), required, strict))

    def test_only_explicit_posix_skips_are_permitted(self):
        windows = "tests.test_windows_auth.WindowsAuthenticationTests.test_acl"
        skipped = "tests.test_state_storage.StateStorageTests.test_unsafe_parent_and_symlink_refused"
        ids = [windows, skipped]
        actual = result(skipped=[(FakeTest(skipped), "POSIX")], count=2)
        required = ("tests.test_windows_auth.", "tests.test_state_storage.")
        strict = ("tests.test_windows_auth.", "tests.test_state_storage.")
        # A required module cannot be entirely skipped, even if an exception is listed.
        self.assertFalse(gate._valid_result(ids, actual, required, strict, gate._ALLOWED_FOCUS_SKIPS))
        ids.append("tests.test_state_storage.StateStorageTests.test_other")
        actual.testsRun = 3
        self.assertTrue(gate._valid_result(ids, actual, required, strict, gate._ALLOWED_FOCUS_SKIPS))
        self.assertFalse(gate._valid_result(ids, actual, required, strict, ()))
        unexpected = result(skipped=[(FakeTest(ids[2]), "unexpected")], count=3)
        self.assertFalse(gate._valid_result(ids, unexpected, required, strict, gate._ALLOWED_FOCUS_SKIPS))

    def test_runner_reports_skipped_ids_without_reasons_and_preserves_gate_policy(self):
        class Demo(unittest.TestCase):
            @unittest.skip("sensitive reason or path")
            def test_skip(self):
                pass
            def test_ok(self):
                pass
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Demo)
        skipped_id = next(test.id() for test in suite if test._testMethodName == "test_skip")
        out = io.StringIO()
        with patch.object(gate.re, "fullmatch", wraps=gate.re.fullmatch):
            self.assertTrue(gate._run_suite(suite, "Example", (), (), (), out))
        self.assertIn("Skipped tests in Example:", out.getvalue())
        # Test IDs outside the repository test namespace are sanitized.
        self.assertIn("[test identifier unavailable]", out.getvalue())
        self.assertNotIn("sensitive reason or path", out.getvalue())
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Demo)
        out = io.StringIO()
        self.assertFalse(gate._run_suite(suite, "Example", (), (skipped_id,), (), out))

    def test_runner_reports_repository_test_id(self):
        class Demo(unittest.TestCase):
            @unittest.skip("private")
            def test_skip(self):
                pass
            def id(self):
                return "tests.test_example.Example.test_skip"
        out = io.StringIO()
        self.assertTrue(gate._run_suite(unittest.TestSuite([Demo("test_skip")]), "Example", (), (), (), out))
        self.assertIn("tests.test_example.Example.test_skip", out.getvalue())
        self.assertNotIn("private", out.getvalue())

    def test_repository_state_is_checked_without_printing_paths_or_file_names(self):
        root = str(gate._ROOT)
        responses = [SimpleNamespace(stdout=root + "\n"), SimpleNamespace(stdout="a"*40 + "\n"), SimpleNamespace(stdout="?? sensitive-name\n")]
        with patch.object(gate.subprocess, "run", side_effect=responses) as run:
            self.assertEqual(gate._repository_state(), ("a"*40, True))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[2].args[0], ["git", "status", "--porcelain", "--untracked-files=normal"])
        self.assertTrue(all(call.kwargs["capture_output"] for call in run.call_args_list))

    def test_failed_checks_and_changed_checkout_do_not_pass(self):
        clean = ("a"*40, False)
        for checks, states in ((False, [clean, clean]), (True, [clean, ("b"*40, False)]), (True, [clean, ("a"*40, True)]), (True, [clean, None])):
            with self.subTest(checks=checks, states=states), patch.object(gate, "_is_windows", return_value=True), \
                 patch.object(gate, "_repository_state", side_effect=states), patch.object(gate, "_run_checks", return_value=checks):
                out = io.StringIO()
                self.assertEqual(gate.main(out, io.StringIO()), 1)
                self.assertNotIn("WINDOWS PHASE 3 TESTS PASSED", out.getvalue())

    def test_clean_pass_is_only_evidence_not_phase_completion(self):
        clean = ("a"*40, False)
        with patch.object(gate, "_is_windows", return_value=True), patch.object(gate, "_repository_state", side_effect=[clean, clean]), \
             patch.object(gate, "_run_checks", return_value=True):
            out = io.StringIO()
            self.assertEqual(gate.main(out, io.StringIO()), 0)
        self.assertIn("a"*40, out.getvalue())
        self.assertIn("WINDOWS PHASE 3 TESTS PASSED", out.getvalue())
        self.assertIn("not Phase 3 completion", out.getvalue())

    def test_repository_errors_are_sanitized(self):
        with patch.object(gate.subprocess, "run", side_effect=OSError("sensitive diagnostic")):
            self.assertIsNone(gate._repository_state())


if __name__ == "__main__":
    unittest.main()
