"""Explicit-setup-only, bounded read-only Windows process-memory observation.

No injection, debug privilege, process writes, memory dumps, logging of candidate
bytes, external service, or cross-account fallback. Unsupported formats stop.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import time

from live_tools.windows.setup_support import SetupError, raw_candidates, string_pointers, valid_page_key


class MemoryReader:
    def __init__(self, pid):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.api.ReadProcessMemory.restype = wintypes.BOOL
        self.api.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self.api.VirtualQueryEx.restype = ctypes.c_size_t
        self.handle = self.api.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ only
        if not self.handle:
            raise SetupError("无法只读访问同一用户的微信；不自动提权，请检查两者权限级别")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, size):
        data = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, data, size, ctypes.byref(count)):
            return b""
        return data.raw[:count.value]

    def chunks(self, deadline, budget=1024 * 1024 * 1024):
        class Region(ctypes.Structure):
            _fields_ = [("base", ctypes.c_void_p), ("allocation", ctypes.c_void_p),
                        ("allocation_protect", wintypes.DWORD), ("partition", wintypes.WORD),
                        ("size", ctypes.c_size_t), ("state", wintypes.DWORD),
                        ("protect", wintypes.DWORD), ("kind", wintypes.DWORD)]
        if ctypes.sizeof(Region) != 48:
            raise SetupError("仅支持 Windows x64 内存布局")
        address, used, regions = 0, 0, 0
        while address < 0x0000800000000000:
            if time.monotonic() >= deadline or used >= budget or regions >= 200000:
                raise SetupError("初始化达到时间或内存读取上限；未写入不完整密钥")
            region = Region()
            if not self.api.VirtualQueryEx(self.handle, address, ctypes.byref(region), ctypes.sizeof(region)):
                return
            base = region.base or 0
            end = base + region.size
            if end <= address:
                raise SetupError("微信内存映射变化；已停止")
            regions += 1
            if region.state == 0x1000 and region.kind == 0x20000 and region.protect in (0x02, 0x04, 0x08):
                previous = b""
                for cursor in range(base, end, 1024 * 1024):
                    if time.monotonic() >= deadline or used >= budget:
                        raise SetupError("初始化达到时间或读取上限；请重新准备后确认")
                    length = min(1024 * 1024, end - cursor, budget - used)
                    chunk = self.read(cursor, length)
                    used += length
                    if chunk:
                        yield previous + chunk
                        previous = chunk[-128:]
                    else:
                        previous = b""
            address = end


def capture_keys(binding, plan):
    from live_tools.windows.router import require_same
    require_same(binding)
    pages = {}
    for relative, expected in plan["targets"].items():
        with (binding.db_base / relative).open("rb") as handle:
            page = handle.read(4096)
        if page[:16].hex() != expected["salt"]:
            raise SetupError("数据库加密状态变化；原确认失效")
        pages[relative] = page
    reader = MemoryReader(binding.pid)
    deadline = time.monotonic() + 90
    matched, seen, candidates = {}, set(), 0
    try:
        for chunk in reader.chunks(deadline):
            for candidate, salt in raw_candidates(chunk):
                for relative, page in pages.items():
                    if relative not in matched and (salt is None or salt == page[:16]) and valid_page_key(page, candidate):
                        matched[relative] = candidate
            for address in string_pointers(chunk):
                candidate = reader.read(address, 32)
                if len(candidate) != 32 or candidate in seen:
                    continue
                seen.add(candidate)
                candidates += 1
                if candidates > 512:
                    raise SetupError("初始化候选达到上限；当前微信格式暂不受支持")
                # Raw page keys and SQLCipher's documented 256000-round passphrase
                # derivation are verified independently against each requested salt.
                for relative, page in pages.items():
                    if relative in matched:
                        continue
                    if time.monotonic() >= deadline:
                        raise SetupError("初始化超时；没有写入不完整结果")
                    key = candidate
                    if not valid_page_key(page, key):
                        key = hashlib.pbkdf2_hmac("sha512", candidate, page[:16], 256000, 32)
                    if valid_page_key(page, key):
                        matched[relative] = key
            if len(matched) == len(pages):
                require_same(binding)
                return matched
        raise SetupError("未找到全部匹配密钥；此微信构建或内存格式尚未适配，不会发布部分初始化")
    finally:
        reader.close()
        seen.clear()
