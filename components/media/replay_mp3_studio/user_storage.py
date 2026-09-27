"""Private, deterministic storage roots for one local user/profile."""

from __future__ import annotations

import hashlib
import os
import platform
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .platform_support import application_root as platform_application_root


PROFILE_ENV = "WEIXIN_REPLAY_PROFILE"
DATA_ROOT_ENV = "WEIXIN_REPLAY_DATA_ROOT"
OUTPUT_ROOT_ENV = "WEIXIN_REPLAY_OUTPUT_ROOT"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
STORAGE_SCHEMA = "v1"


def home_path(home: Path | str | None = None) -> Path:
    return Path(home if home is not None else Path.home()).expanduser().resolve()


def profile_name(profile: str | None = None) -> str:
    raw = str(profile or os.environ.get(PROFILE_ENV) or "default").strip()
    if not PROFILE_RE.fullmatch(raw):
        raise ValueError(
            "Profile must be 1-64 ASCII letters, numbers, dots, underscores, or hyphens."
        )
    return raw


def activate_profile(profile: str | None = None) -> str:
    """Bind child imports/processes to the same validated local profile."""

    resolved = profile_name(profile)
    os.environ[PROFILE_ENV] = resolved
    return resolved


def local_principal(home: Path | str | None = None, uid: int | None = None) -> str:
    resolved_home = home_path(home)
    if uid is not None or hasattr(os, "getuid"):
        resolved_uid = int(uid if uid is not None else os.getuid())
        return f"uid={resolved_uid}\0home={resolved_home}"
    # Windows has no os.getuid().  The user-profile path is already an OS
    # boundary; adding the local account label prevents two unusual profiles
    # that resolve to the same home from receiving the same namespace.
    account = "\\".join(
        part
        for part in (
            str(os.environ.get("USERDOMAIN") or "").strip(),
            str(os.environ.get("USERNAME") or "").strip(),
        )
        if part
    )
    return f"account={account or 'unknown'}\0home={resolved_home}"


def storage_namespace(
    profile: str | None = None,
    *,
    home: Path | str | None = None,
    uid: int | None = None,
) -> str:
    material = (
        f"weixin-replay-to-mp3\0{STORAGE_SCHEMA}\0"
        f"{local_principal(home, uid)}\0profile={profile_name(profile)}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"user-{digest}"


def application_root(home: Path | str | None = None) -> Path:
    return platform_application_root(home, system=platform.system())


def _base_root(env_name: str, default: Path) -> tuple[Path, bool]:
    override = str(os.environ.get(env_name) or "").strip()
    if override:
        return Path(override).expanduser().resolve(), True
    return default.resolve(), False


def user_data_root(
    profile: str | None = None,
    *,
    home: Path | str | None = None,
    uid: int | None = None,
) -> Path:
    base, _ = _base_root(DATA_ROOT_ENV, application_root(home) / "data")
    return base / "profiles" / storage_namespace(profile, home=home, uid=uid)


def user_output_root(
    profile: str | None = None,
    *,
    home: Path | str | None = None,
    uid: int | None = None,
) -> Path:
    default = home_path(home) / "Downloads" / "WeixinReplayMP3"
    base, _ = _base_root(OUTPUT_ROOT_ENV, default)
    return base / storage_namespace(profile, home=home, uid=uid)


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def ensure_profile_layout(profile: str | None = None) -> dict[str, str]:
    namespace = storage_namespace(profile)
    data = user_data_root(profile)
    output = user_output_root(profile)

    data_override = bool(str(os.environ.get(DATA_ROOT_ENV) or "").strip())
    if not data_override:
        app = application_root()
        for owned in (app, app / "data", app / "data" / "profiles"):
            ensure_private_dir(owned)
    ensure_private_dir(data)

    output_override = bool(str(os.environ.get(OUTPUT_ROOT_ENV) or "").strip())
    if not output_override:
        ensure_private_dir(home_path() / "Downloads" / "WeixinReplayMP3")
    ensure_private_dir(output)

    return {
        "schema": STORAGE_SCHEMA,
        "namespace": namespace,
        "data_root": str(data),
        "output_root": str(output),
    }


@contextmanager
def private_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)
