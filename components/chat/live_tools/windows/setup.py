"""Explicit Windows setup entry point; not imported by the export client."""
from __future__ import annotations

import argparse
import json
import sys
import time

from live_tools.windows.router import bind_active_account, require_same
from live_tools.windows.setup_support import SetupError, consent_plan, inventory
from live_tools.windows.state import (account_directory, load_initialization, read_private,
                                     store_initialization, write_json)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise SetupError("setup 参数无效；不接受数据库路径、账号、密钥或任意命令")


def parser():
    result = SafeParser(description="Windows 微信当前账号显式初始化")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--types", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--confirm-capture", action="store_true", required=True)
    retain = sub.add_parser("snapshot")
    retain.add_argument("--confirm-retain", action="store_true", required=True)
    return result


def _pending(binding):
    try:
        pending = json.loads(read_private(account_directory(binding.account_ref) / "setup-consent.json"))
        if not 0 <= time.time() - pending["prepared_at"] <= 900:
            raise SetupError("准备结果已超过 15 分钟；请重新 prepare 并确认")
        expected = consent_plan(binding, pending["plan"]["types"])
        if expected != pending["plan"]:
            raise SetupError("账号、进程、版本或目标数据库已变化；原确认失效")
        return pending
    except (OSError, ValueError, KeyError, TypeError):
        raise SetupError("缺少安全的初始化准备结果；请先 prepare") from None


def run(args):
    binding = bind_active_account()
    if args.command == "doctor":
        found = inventory(binding)
        initialized = False
        try:
            state = load_initialization(binding.account_ref)
            initialized = bool(state.get("targets")) and all(
                name in found and found[name]["salt"] == value["salt"]
                for name, value in state["targets"].items())
        except Exception:
            pass
        return {"status": "checked", "platform": "windows-preview", "writes_performed": False,
                "current_account": binding.public_report(), "wechat_version": binding.version,
                "database_count": len(found), "initialization_present": initialized,
                "next_action": "显式指定内容类型并 prepare；捕获和快照保留分别确认"}
    if args.command == "prepare":
        types = args.types.split(",")
        if len(types) != len(set(types)):
            raise SetupError("内容类型不能重复")
        plan = consent_plan(binding, types)
        pending = {"prepared_at": time.time(), "plan": plan, "captured": False}
        write_json(account_directory(binding.account_ref, create=True) / "setup-consent.json", pending)
        return {"status": "prepared", "types": plan["types"],
                "database_count": len(plan["targets"]),
                "target_aliases": [name.rsplit("/", 1)[-1][:-3] for name in plan["targets"]],
                "source_writes": False, "keys_captured": False,
                "next_action": "请用户确认本次当前账号只读内存初始化；有效期 15 分钟"}
    pending = _pending(binding)
    plan = pending["plan"]
    if args.command == "capture":
        if pending["captured"]:
            raise SetupError("本次捕获确认已使用；不会重复读取进程内存")
        from live_tools.windows.key_capture import capture_keys
        keys = capture_keys(binding, plan)
        require_same(binding)
        if consent_plan(binding, plan["types"]) != plan:
            raise SetupError("捕获期间目标发生变化；未保存")
        state = {"schema_version": 1, "account_ref": binding.account_ref,
                 "db_base": plan["db_base"], "types": plan["types"],
                 "snapshot_retention_approved": False,
                 "targets": {name: {"salt": entry["salt"], "key": keys[name].hex()}
                             for name, entry in plan["targets"].items()}}
        store_initialization(binding.account_ref, state)
        pending["captured"] = True
        write_json(account_directory(binding.account_ref) / "setup-consent.json", pending)
        return {"status": "initialized", "database_count": len(keys),
                "storage": "current-user-dpapi-and-private-acl", "snapshot_retained": False,
                "next_action": "单独确认保留该账号私有明文快照后，再运行 snapshot"}
    if not pending["captured"]:
        raise SetupError("尚未完成本次明确确认的初始化")
    state = load_initialization(binding.account_ref)
    if state["types"] != plan["types"] or set(state["targets"]) != set(plan["targets"]):
        raise SetupError("初始化与本次保留确认范围不匹配")
    # This consent explicitly permits the initial private snapshot and subsequent
    # normal on-demand refreshes for these initialized content categories.
    state["snapshot_retention_approved"] = True
    store_initialization(binding.account_ref, state)
    from live_tools.windows.snapshot import create_snapshot
    return create_snapshot(binding, state)


def main(argv=None):
    try:
        if sys.platform != "win32" or sys.maxsize <= 2**32:
            raise SetupError("此入口仅支持 Windows 10/11 x64；Mac 请用现有 setup")
        report = run(parser().parse_args(argv))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except SetupError as error:
        print(json.dumps({"status": "blocked", "detail": str(error)}, ensure_ascii=False))
        return 2
    except Exception:
        # Native errors often contain account paths, process identifiers or bytes.
        print(json.dumps({"status": "blocked", "detail": "Windows 初始化检查未通过；没有输出私有详情，请检查安装、登录状态及权限后重试"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
