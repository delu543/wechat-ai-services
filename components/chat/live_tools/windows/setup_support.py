"""Pure setup selection/consent contracts and bounded SQLCipher key validation."""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import re
import struct

from live_tools.platform_support import require_no_reparse
from live_tools.wechat_safe_snapshot import PAGE_SIZE, _decrypt_page, _validate_first_plain_page

SUPPORTED_TYPES = frozenset({"text", "image", "voice", "file", "sticker", "video", "all",
                           "contact_card", "location", "link", "mini_program", "quote",
                           "forwarded_record", "app_message", "call", "system", "unknown"})
RAW_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64})([0-9a-fA-F]{32})?'")


class SetupError(RuntimeError):
    pass


def inventory(binding):
    from live_tools.windows.router import TARGET
    result = {}
    for directory in (binding.db_base / "contact", binding.db_base / "message"):
        require_no_reparse(directory)
        entries = list(directory.iterdir())
        if len(entries) > 1024:
            raise SetupError("当前账号数据库目录超出安全检查上限")
        for path in entries:
            relative = f"{directory.name}/{path.name}"
            if not TARGET.fullmatch(relative):
                continue
            require_no_reparse(path)
            info = path.stat()
            if not path.is_file() or info.st_size < PAGE_SIZE or info.st_size % PAGE_SIZE:
                raise SetupError("目标数据库不是完整的本地 SQLCipher 文件")
            with path.open("rb") as handle:
                salt = handle.read(16)
            if salt == b"SQLite format 3\0":
                raise SetupError("当前数据不是受支持的加密格式")
            result[relative] = {"salt": salt.hex(), "file_id": [info.st_dev, info.st_ino]}
    return result


def target_set(discovered, types):
    requested = set(types)
    if not requested or requested - SUPPORTED_TYPES or ("all" in requested and len(requested) != 1):
        raise SetupError("必须明确指定有效的内容类型，all 不能混用")
    result = [name for name in discovered if name == "contact/contact.db" or
              re.fullmatch(r"message/message_\d+\.db", name)]
    if "contact/contact.db" not in result or len(result) < 2:
        raise SetupError("当前账号缺少 contact 或消息分片")
    if requested & {"all", "voice"}:
        media = [name for name in discovered if re.fullmatch(r"message/media_\d+\.db", name)]
        if not media:
            raise SetupError("语音初始化缺少 media 分片")
        result += media
    if requested & {"all", "image", "file", "sticker", "video"} and "message/message_resource.db" in discovered:
        result.append("message/message_resource.db")
    return sorted(set(result))


def consent_plan(binding, types):
    found = inventory(binding)
    return {"schema_version": 1, "account_ref": binding.account_ref, "pid": binding.pid,
            "started": binding.started, "executable": str(binding.executable), "version": binding.version,
            "db_base": str(binding.db_base), "types": sorted(types),
            "targets": {name: found[name] for name in target_set(found, types)}}


def valid_page_key(page: bytes, key: bytes) -> bool:
    """SQLCipher 4 first-page HMAC plus structural check, never a header guess."""
    if len(key) != 32 or len(page) != PAGE_SIZE:
        return False
    mac_salt = bytes(value ^ 0x3A for value in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, 32)
    expected = hmac.new(mac_key, page[16:4032] + struct.pack("<I", 1), "sha512").digest()
    if not hmac.compare_digest(expected, page[4032:4096]):
        return False
    try:
        _validate_first_plain_page(_decrypt_page(page, 1, key))
    except Exception:
        return False
    return True


def raw_candidates(data: bytes):
    for match in RAW_LITERAL.finditer(data):
        yield bytes.fromhex(match[1].decode("ascii")), (
            bytes.fromhex(match[2].decode("ascii")) if match[2] else None)


def string_pointers(data: bytes):
    """Read candidate addresses from the public MSVC x64 std::string ABI.

    A 16-byte union is followed by size/capacity. Only size=32 external strings,
    bounded capacity and canonical user addresses qualify; no arbitrary dump.
    """
    marker = struct.pack("<Q", 32)
    offset = 16
    while True:
        offset = data.find(marker, offset)
        if offset < 0 or offset + 16 > len(data):
            return
        if offset >= 16:
            pointer = struct.unpack_from("<Q", data, offset - 16)[0]
            capacity = struct.unpack_from("<Q", data, offset + 8)[0]
            if 32 <= capacity <= 63 and 0x10000 <= pointer < 0x0000800000000000:
                yield pointer
        offset += 1
