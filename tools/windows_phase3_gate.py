"""Run disposable Windows Phase 3 tests and report checkout identity.

A passing run is evidence for one checkout, not Phase 3 completion, real-data
recovery, a security audit, or authorization to execute or receive payments.
Tests create and remove synthetic temporary data; existing user data is not used.
"""
from pathlib import Path
import re
import subprocess
import sys
import unittest


_ROOT = Path(__file__).resolve().parent.parent
_FOCUS = (
    "tests.test_windows_security",
    "tests.test_windows_auth",
    "tests.test_windows_phase3_gate",
    "tests.test_state_storage",
    "tests.test_state_audit",
    "tests.test_opportunity_drafts",
    "tests.test_task_plans",
    "tests.test_order_drafts",
    "tests.test_invoice_drafts",
    "tests.test_state_authorization",
    "tests.test_state_migration",
    "tests.test_state_operation_integrity",
    "tests.test_state_snapshot_integrity",
    "tests.test_state_recovery_comparison",
    "tests.test_phase3_recovery_rehearsal",
    "tests.test_state_crash_recovery",
    "tests.test_task_queue",
    "tests.test_task_workflows",
    "tests.test_task_associations",
    "tests.test_paired_completion",
    "tests.test_managed_pairs",
    "tests.test_payment_observations",
    "tests.test_financial_draft_workflows",
    "tests.test_financial_assertions",
    "tests.test_tracker_cli",
    "tests.test_payment_sandbox",
    "tests.test_durable_sandbox_payments",
    "tests.test_opportunities",
    "tests.test_dashboard_metrics",
    "tests.test_task_runner",
    "tests.test_services",
    "tests.test_products",
    "tests.test_customers",
    "tests.test_communications",
    "tests.test_ledger",
    "tests.test_followups",
    "tests.test_invoices",
    "tests.test_payment_providers",
    "tests.test_ai_assistant",
    "tests.test_analytics",
    "tests.test_connectors",
    "tests.test_frontend_spa",
    "tests.test_health",
    "tests.test_opportunities_hardening",
    "tests.test_phase3_completion",
    "tests.test_phase5_completion",
    "tests.test_service_workspace",
)
_WINDOWS_AUTH = "tests.test_windows_auth.WindowsAuthenticationTests."
# Explicitly POSIX-specific tests; any newly skipped required test fails closed.
_ALLOWED_FOCUS_SKIPS = frozenset({
    "tests.test_state_storage.StateStorageTests.test_unsafe_parent_and_symlink_refused",
    "tests.test_state_authorization.StateAuthorizationTests.test_same_database_hard_link_is_refused",
    "tests.test_state_recovery_comparison.StateRecoveryComparisonTests.test_hardlinked_copies_are_not_treated_as_distinct",
    "tests.test_state_recovery_comparison.StateRecoveryComparisonTests.test_record_comparison_refuses_hardlinked_copies",
})


def _is_windows():
    return sys.platform == "win32"


def _repository_state():
    """Return (commit, dirty) without printing paths or Git diagnostics."""
    try:
        if Path.cwd().resolve() != _ROOT:
            return None
        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=_ROOT, check=True, capture_output=True,
                text=True, timeout=15,
            ).stdout
        top = Path(git("rev-parse", "--show-toplevel").strip()).resolve()
        if top != _ROOT:
            return None
        commit = git("rev-parse", "--verify", "HEAD").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            return None
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal").strip())
        return commit, dirty
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _case_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _case_ids(test)
        else:
            yield test.id()


def _valid_result(expected_ids, result, required_prefixes, no_skip_prefixes, allowed_skips=()):
    """Require actual, non-skipped cases for each required test group."""
    if (not result.wasSuccessful() or result.expectedFailures or result.unexpectedSuccesses
            or not expected_ids or result.testsRun != len(expected_ids)):
        return False
    skipped = {test.id() for test, _reason in result.skipped}
    for prefix in required_prefixes:
        matching = [ident for ident in expected_ids if ident.startswith(prefix)]
        if not matching or not any(ident not in skipped for ident in matching):
            return False
    for prefix in no_skip_prefixes:
        if any(ident.startswith(prefix) and ident not in allowed_skips for ident in skipped):
            return False
    return True


def _run_suite(suite, label, required, strict, allowed_skips, out):
    expected = list(_case_ids(suite))
    out.write(f"Running {label}: {len(expected)} test cases.\n")
    out.flush()
    result = unittest.TextTestRunner(stream=out, verbosity=1).run(suite)
    if result.skipped:
        out.write(f"Skipped tests in {label}:\n")
        for test, _reason in result.skipped:
            ident = test.id()
            # Do not print arbitrary skip reasons, paths, or test-generated data.
            if re.fullmatch(r"tests(?:\.[A-Za-z_][A-Za-z_0-9]*)+", ident):
                out.write(f"  {ident}\n")
            else:
                out.write("  [test identifier unavailable]\n")
    if not _valid_result(expected, result, required, strict, allowed_skips):
        out.write(f"{label} failed or required tests were missing/skipped.\n")
        return False
    return True


def _run_checks(out):
    try:
        focused = unittest.TestLoader().loadTestsFromNames(_FOCUS)
        required = tuple(name + "." for name in _FOCUS) + (_WINDOWS_AUTH,)
        strict = tuple(name + "." for name in _FOCUS)
        if not _run_suite(focused, "Focused Windows and Phase 3 tests", required, strict, _ALLOWED_FOCUS_SKIPS, out):
            return False
        full = unittest.TestLoader().discover(start_dir=str(_ROOT / "tests"), top_level_dir=str(_ROOT))
        return _run_suite(full, "Full discovery", required, strict, _ALLOWED_FOCUS_SKIPS, out)
    except Exception:
        # A discovery/runner failure is not a pass; avoid echoing environment data.
        out.write("Test discovery or runner failed.\n")
        return False


def main(out=None, err=None):
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if not _is_windows():
        err.write("Windows Phase 3 verification requires actual Windows.\n")
        return 2
    if sys.version_info < (3, 11):
        err.write("Python 3.11 or newer is required.\n")
        return 2
    before = _repository_state()
    if before is None or before[1]:
        err.write("Run from a clean Git checkout at the repository root; Git must be available.\n")
        return 2
    commit = before[0]
    out.write(f"Commit: {commit}\nPython: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n")
    out.write("Running disposable tests only; no real data or credentials are needed.\n")
    ok = _run_checks(out)
    after = _repository_state()
    if not ok or after != before:
        err.write("Windows Phase 3 verification failed or the checkout changed during the run.\n")
        return 1
    out.write("WINDOWS PHASE 3 TESTS PASSED for this checkout; not Phase 3 completion or production approval.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
