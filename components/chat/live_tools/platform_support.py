"""Small filesystem boundary; Windows privacy means ACLs, never chmod emulation.

Windows imports are lazy so the existing Mac runtime has no new dependencies.
Only newly created output paths are secured; source WeChat paths are read-only.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

WINDOWS = sys.platform == "win32"


def support_root() -> Path:
    if WINDOWS:
        import win32com.shell.shell as shell
        import win32com.shell.shellcon as shellcon
        # Known-folder API, not a prompt-supplied/environment-controlled root.
        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_LOCAL_APPDATA, None, 0)) / "WeChatAIServicesChat"
    return Path.home() / "Library/Application Support/WeChatAIServicesChat"


def require_no_reparse(path: Path) -> None:
    """Reject symlinks/junctions, network shares and alternate data streams."""
    if not WINDOWS:
        if path.is_symlink():
            raise OSError("符号链接不受支持")
        return
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise OSError("仅支持本机绝对路径")
    if any(":" in part for part in path.parts[1:]):
        raise OSError("不支持备用数据流")
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("路径包含重解析点；已停止")


def current_sid():
    import win32api
    import win32con
    import win32security as security
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()


def _fd_path(descriptor):
    import msvcrt
    import win32file
    value = win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(descriptor), 0)
    prefix = chr(92) * 2 + "?" + chr(92)
    if not value.startswith(prefix) or value[4:].startswith("UNC" + chr(92)):
        raise OSError("不能解析本机输出句柄")
    path = Path(value[4:])
    require_no_reparse(path)
    opened, named = os.fstat(descriptor), path.stat()
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise OSError("输出句柄与路径不一致")
    return path


def _security_descriptor(target):
    import win32security as security
    flags = security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION
    if isinstance(target, int):
        import msvcrt
        return security.GetSecurityInfo(msvcrt.get_osfhandle(target), security.SE_FILE_OBJECT, flags)
    require_no_reparse(Path(target))
    return security.GetNamedSecurityInfo(str(target), security.SE_FILE_OBJECT, flags)


def is_private(target, info=None, *, exact=False) -> bool:
    if not WINDOWS:
        info = info or (os.fstat(target) if isinstance(target, int) else Path(target).lstat())
        expected = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        return info.st_uid == os.geteuid() and (
            stat.S_IMODE(info.st_mode) == expected if exact else not (info.st_mode & 0o077)
        )
    import win32security as security
    descriptor = _security_descriptor(target)
    owner = current_sid()
    if descriptor.GetSecurityDescriptorOwner() != owner:
        return False
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None or not dacl.GetAceCount():
        return False
    allowed = {security.ConvertSidToStringSid(owner), "S-1-5-18"}
    owner_access = False
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        # Fail closed on unfamiliar ACEs; a private path only needs two simple grants.
        if ace[0][0] != security.ACCESS_ALLOWED_ACE_TYPE:
            return False
        sid = security.ConvertSidToStringSid(ace[2])
        if sid not in allowed:
            return False
        if ace[2] == owner and not (ace[0][1] & 8):  # INHERIT_ONLY_ACE
            owner_access = bool(ace[1] & 0x1F01FF == 0x1F01FF)
    return owner_access


def private_chmod(target, mode: int) -> None:
    if not WINDOWS:
        (os.fchmod if isinstance(target, int) else os.chmod)(target, mode)
        return
    import win32security as security
    descriptor = _security_descriptor(target)
    owner = current_sid()
    existing_owner = descriptor.GetSecurityDescriptorOwner()
    if existing_owner != owner:
        # An elevated Windows test/installer token may create files with its
        # default owner group. Only that token's own default owner is eligible.
        import win32api
        import win32con
        token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            default_owner = security.GetTokenInformation(token, security.TokenOwner)
        finally:
            token.Close()
        if existing_owner != default_owner:
            raise OSError("输出不属于当前用户；拒绝修改权限")
    dacl = security.ACL()
    for sid in (owner, security.ConvertStringSidToSid("S-1-5-18")):
        dacl.AddAccessAllowedAceEx(security.ACL_REVISION, 3, 0x1F01FF, sid)
    flags = (security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION
             | security.OWNER_SECURITY_INFORMATION)
    # CRT-created handles lack WRITE_DAC. Resolve the held handle to its exact
    # non-reparse path, then let the native security API open a WRITE_DAC handle.
    path = _fd_path(target) if isinstance(target, int) else Path(target)
    security.SetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
                                  flags, owner, None, dacl, None)
    if isinstance(target, int):
        _fd_path(target)
    if not is_private(target):
        raise OSError("私有 ACL 验证失败")


def private_mkdir(path: Path) -> Path:
    require_no_reparse(path)
    if path.exists():
        if not path.is_dir() or not is_private(path):
            raise OSError("现有目录不是当前用户的私有目录")
        return path
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        private_chmod(item, 0o700)
    return path


def read_at(descriptor: int, size: int, offset: int) -> bytes:
    if not WINDOWS:
        return os.pread(descriptor, size, offset)
    # These are private, unshared descriptors, never SQLite-owned handles.
    previous = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.read(descriptor, size)
    finally:
        os.lseek(descriptor, previous, os.SEEK_SET)


def write_at(descriptor: int, data: bytes, offset: int) -> int:
    if not WINDOWS:
        return os.pwrite(descriptor, data, offset)
    previous = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.write(descriptor, data)
    finally:
        os.lseek(descriptor, previous, os.SEEK_SET)
