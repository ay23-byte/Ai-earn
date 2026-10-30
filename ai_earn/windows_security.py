"""Experimental Windows ACL checks for Phase 2 verification.

This module is not a substitute for OS isolation and does not enable Windows
account storage. It fails closed on unfamiliar ACEs, principals and reparse
points. The caller must not use it for an untrusted ancestor directory.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re


class WindowsSecurityError(Exception):
    """A private Windows directory or file cannot be established or verified."""


SYSTEM_SID = "S-1-5-18"
FULL_ACCESS = 0x001F01FF
OBJECT_INHERIT = 0x01
CONTAINER_INHERIT = 0x02
INHERITED = 0x10


@dataclass(frozen=True)
class AccessEntry:
    sid: str
    mask: int
    flags: int
    ace_type: int


@dataclass(frozen=True)
class SecuritySnapshot:
    owner: str
    protected: bool
    entries: tuple[AccessEntry, ...]


def validate_snapshot(snapshot: SecuritySnapshot, user_sid: str, *, directory: bool) -> None:
    """Reject anything except full access for the current user and SYSTEM."""
    if not re.fullmatch(r"S-1-(?:\d+-)*\d+", user_sid) or user_sid == SYSTEM_SID:
        raise WindowsSecurityError("A non-SYSTEM current user is required.")
    if snapshot.owner != user_sid or (directory and not snapshot.protected):
        raise WindowsSecurityError("Windows ownership or ACL protection is unsafe.")
    allowed = {user_sid, SYSTEM_SID}
    seen: set[str] = set()
    for entry in snapshot.entries:
        if entry.ace_type != 0 or entry.sid not in allowed or entry.sid in seen:
            raise WindowsSecurityError("Unexpected Windows ACL entry.")
        # Disallow inherit-only, no-propagate, audit, and unknown flags.
        if entry.flags & ~(OBJECT_INHERIT | CONTAINER_INHERIT | INHERITED):
            raise WindowsSecurityError("Unsupported Windows ACL flags.")
        if directory and (entry.flags != (OBJECT_INHERIT | CONTAINER_INHERIT)):
            raise WindowsSecurityError("Directory entries must be explicit and inheritable.")
        if entry.mask != FULL_ACCESS:
            raise WindowsSecurityError("Unexpected Windows ACL permissions.")
        seen.add(entry.sid)
    if seen != allowed:
        raise WindowsSecurityError("Windows ACL is incomplete.")


def _api():
    if os.name != "nt":
        raise WindowsSecurityError("Windows security APIs are unavailable on this platform.")
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.restype = wintypes.BOOL
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.IsValidSid.argtypes = [ctypes.c_void_p]
    advapi.IsValidSid.restype = wintypes.BOOL
    advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi.GetLengthSid.restype = wintypes.DWORD
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    return ctypes, wintypes, advapi, kernel


def _sid_text(advapi, kernel, ctypes, sid) -> str:
    from ctypes import wintypes
    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise WindowsSecurityError("Cannot inspect Windows identity.")
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def current_user_sid() -> str:
    ctypes, wintypes, advapi, kernel = _api()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise WindowsSecurityError("Cannot inspect current Windows identity.")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise WindowsSecurityError("Cannot inspect current Windows identity.")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise WindowsSecurityError("Cannot inspect current Windows identity.")
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is PSID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return _sid_text(advapi, kernel, ctypes, sid)
    finally:
        kernel.CloseHandle(token)


def _check_local_ancestors(path: Path, ctypes, wintypes, kernel) -> None:
    # No UNC/network paths, removable drives, filesystems without persistent ACLs,
    # or existing reparse-point ancestors. The final component is checked separately.
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive):
        raise WindowsSecurityError("An absolute local Windows drive path is required.")
    root = path.anchor
    flags = wintypes.DWORD()
    if kernel.GetDriveTypeW(root) != 3 or not kernel.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) or not flags.value & 0x8:
        raise WindowsSecurityError("Windows storage requires a fixed local drive with persistent ACLs.")
    for parent in path.parents:
        attrs = kernel.GetFileAttributesW(str(parent))
        if attrs == 0xFFFFFFFF or attrs & 0x400 or not attrs & 0x10:
            raise WindowsSecurityError("Windows ancestor is unavailable or a reparse point.")


def inspect_path(path: Path, *, directory: bool) -> SecuritySnapshot:
    ctypes, wintypes, advapi, kernel = _api()
    _check_local_ancestors(path, ctypes, wintypes, kernel)
    attributes = kernel.GetFileAttributesW(str(path))
    if attributes == 0xFFFFFFFF or attributes & 0x400:
        raise WindowsSecurityError("Path is unavailable or a reparse point.")
    if bool(attributes & 0x10) != directory:
        raise WindowsSecurityError("Windows path has an unexpected type.")
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
    error = advapi.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error != 0:
        raise WindowsSecurityError(f"Cannot inspect Windows security descriptor (Windows error {error}).")
    try:
        if not owner.value or not dacl.value:
            raise WindowsSecurityError("A Windows owner and non-null DACL are required.")
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise WindowsSecurityError("Cannot inspect Windows ACL protection.")
        class AclSize(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]
        class AceHeader(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", wintypes.WORD)]
        class AllowedAce(ctypes.Structure):
            _fields_ = [("header", AceHeader), ("mask", wintypes.DWORD), ("sid_start", wintypes.DWORD)]
        size = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise WindowsSecurityError("Cannot inspect Windows ACL entries.")
        if size.count != 2:
            raise WindowsSecurityError("Unexpected number of Windows ACL entries.")
        entries = []
        for index in range(size.count):
            ace_pointer = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise WindowsSecurityError("Cannot inspect a Windows ACL entry.")
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(AllowedAce)).contents
            if ace.header.type != 0 or ace.header.size < 12:
                raise WindowsSecurityError("Unsupported Windows ACL entry type.")
            sid_pointer = ctypes.c_void_p(ace_pointer.value + AllowedAce.sid_start.offset)
            if not advapi.IsValidSid(sid_pointer) or advapi.GetLengthSid(sid_pointer) > ace.header.size - AllowedAce.sid_start.offset:
                raise WindowsSecurityError("Invalid Windows SID in ACL.")
            entries.append(AccessEntry(_sid_text(advapi, kernel, ctypes, sid_pointer), ace.mask, ace.header.flags, ace.header.type))
        return SecuritySnapshot(_sid_text(advapi, kernel, ctypes, owner), bool(control.value & 0x1000), tuple(entries))
    finally:
        kernel.LocalFree(descriptor)


def verify_private_path(path: Path, *, directory: bool) -> None:
    validate_snapshot(inspect_path(path, directory=directory), current_user_sid(), directory=directory)


def create_private_directory(path: Path) -> None:
    """Create a new protected directory; never modify or replace an existing one."""
    ctypes, wintypes, advapi, kernel = _api()
    _check_local_ancestors(path, ctypes, wintypes, kernel)
    sid = current_user_sid()
    if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid) or sid == SYSTEM_SID:
        raise WindowsSecurityError("A non-SYSTEM current user is required.")
    sddl = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise WindowsSecurityError("Cannot construct a private Windows security descriptor.")
    try:
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit_handle", wintypes.BOOL)]
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attrs)):
            raise WindowsSecurityError(f"Cannot create a new private Windows directory (Windows error {ctypes.get_last_error()}).")
    finally:
        kernel.LocalFree(descriptor)
    verify_private_path(path, directory=True)
