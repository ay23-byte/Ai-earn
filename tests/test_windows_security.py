"""ACL policy tests on every OS; real Win32 tests are separated below."""
import unittest

from ai_earn.windows_security import (
    AccessEntry, SecuritySnapshot, WindowsSecurityError, validate_snapshot,
    SYSTEM_SID, FULL_ACCESS, OBJECT_INHERIT, CONTAINER_INHERIT, INHERITED,
)


class AclPolicyTests(unittest.TestCase):
    def setUp(self):
        self.user = "S-1-5-21-123-456-789-1001"
        self.flags = OBJECT_INHERIT | CONTAINER_INHERIT
        self.user_entry = AccessEntry(self.user, FULL_ACCESS, self.flags, 0)
        self.system_entry = AccessEntry(SYSTEM_SID, FULL_ACCESS, self.flags, 0)

    def test_accepts_only_private_protected_directory(self):
        validate_snapshot(SecuritySnapshot(self.user, True, (self.user_entry, self.system_entry)), self.user, directory=True)

    def test_accepts_only_expected_inherited_file_acl(self):
        entries = (AccessEntry(self.user, FULL_ACCESS, INHERITED, 0), AccessEntry(SYSTEM_SID, FULL_ACCESS, INHERITED, 0))
        validate_snapshot(SecuritySnapshot(self.user, False, entries), self.user, directory=False)

    def test_rejects_broad_foreign_and_incomplete_acl(self):
        bad_entries = [
            (self.user_entry,),
            (self.user_entry, self.system_entry, AccessEntry("S-1-1-0", FULL_ACCESS, self.flags, 0)),
            (self.user_entry, self.user_entry),
            (self.user_entry, AccessEntry(SYSTEM_SID, FULL_ACCESS, self.flags, 1)),
            (self.user_entry, AccessEntry(SYSTEM_SID, FULL_ACCESS, self.flags | 0x08, 0)),
            (self.user_entry, AccessEntry(SYSTEM_SID, FULL_ACCESS - 1, self.flags, 0)),
            (self.user_entry, AccessEntry(SYSTEM_SID, FULL_ACCESS, INHERITED | self.flags, 0)),
        ]
        for entries in bad_entries:
            with self.subTest(entries=entries), self.assertRaises(WindowsSecurityError):
                validate_snapshot(SecuritySnapshot(self.user, True, entries), self.user, directory=True)

    def test_rejects_wrong_owner_unprotected_directory_and_invalid_sid(self):
        for snapshot, sid in [
            (SecuritySnapshot(SYSTEM_SID, True, (self.user_entry, self.system_entry)), self.user),
            (SecuritySnapshot(self.user, False, (self.user_entry, self.system_entry)), self.user),
            (SecuritySnapshot(self.user, True, (self.user_entry, self.system_entry)), "S-1-5-18"),
            (SecuritySnapshot(self.user, True, (self.user_entry, self.system_entry)), "S-1-5-21)D:(A;;FA;;;WD)"),
        ]:
            with self.subTest(sid=sid), self.assertRaises(WindowsSecurityError):
                validate_snapshot(snapshot, sid, directory=True)
