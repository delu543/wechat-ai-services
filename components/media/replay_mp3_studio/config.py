from __future__ import annotations

import os
from pathlib import Path

from .user_storage import ensure_private_dir, user_data_root


PROJECT_ROOT = Path(__file__).resolve().parents[1]
USER_DATA_ROOT = user_data_root()
LIBRARY_ROOT_OVERRIDE = str(os.environ.get("REPLAY_MP3_LIBRARY") or "").strip()
WORK_ROOT_OVERRIDE = str(os.environ.get("REPLAY_MP3_WORK_ROOT") or "").strip()
LIBRARY_ROOT = Path(LIBRARY_ROOT_OVERRIDE or USER_DATA_ROOT / "library").expanduser().resolve()
WORK_ROOT = Path(WORK_ROOT_OVERRIDE or USER_DATA_ROOT / "studio-work").expanduser().resolve()
AUTHORIZED_FETCHERS = PROJECT_ROOT / "outputs" / "authorized_fetchers"
STATIC_ROOT = Path(__file__).resolve().parent / "static"

PLATFORMS = {
    "xiaohongshu": {
        "label": "小红书",
        "folder": "xiaohongshu",
        "accent": "#d9413d",
    },
    "weixin": {
        "label": "视频号",
        "folder": "weixin",
        "accent": "#10a37f",
    },
    "third_party": {
        "label": "第三方网站",
        "folder": "third_party",
        "accent": "#4f46e5",
    },
    "other": {
        "label": "其他",
        "folder": "other",
        "accent": "#64748b",
    },
}


def platform_folder(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform: {platform}")
    return LIBRARY_ROOT / PLATFORMS[platform]["folder"]


def ensure_layout() -> None:
    ensure_private_dir(USER_DATA_ROOT)
    if LIBRARY_ROOT_OVERRIDE:
        LIBRARY_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        ensure_private_dir(LIBRARY_ROOT)
    if WORK_ROOT_OVERRIDE:
        WORK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        ensure_private_dir(WORK_ROOT)
    for name in PLATFORMS:
        folder = platform_folder(name)
        if LIBRARY_ROOT_OVERRIDE:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            ensure_private_dir(folder)
