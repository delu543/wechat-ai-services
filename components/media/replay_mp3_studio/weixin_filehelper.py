from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import WORK_ROOT


FILE_TRANSFER_ASSISTANT_NAME = "文件传输助手"
FILEHELPER_ICON_SIGNATURE = "green_filetransfer_icon_v1"
PROTECTED_FILEHELPER_SIGNATURE = "protected_ax_window_title_scan_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PINNED_CHAT_GROUP_RE = re.compile(r"^\d+个置顶聊天$")


@dataclass(frozen=True)
class WeixinWindowMetadata:
    title: str
    x: int
    y: int
    width: int
    height: int
    minimized: bool = False

    @property
    def title_kind(self) -> str:
        compact = re.sub(r"\s+", "", self.title or "")
        if "视频号" in compact or compact in {"微信(窗口)", "WeChat(Window)"}:
            return "video"
        if compact in {"微信", "WeChat"}:
            return "main"
        return "other"


@dataclass(frozen=True)
class WeixinRuntimeStatus:
    state: str
    app_running: bool
    renderer_running: bool
    screen_locked: bool
    windows: tuple[WeixinWindowMetadata, ...]
    capture_strategy: str

    def to_safe_dict(self) -> dict[str, Any]:
        title_kinds = [window.title_kind for window in self.windows]
        return {
            "state": self.state,
            "app_running": self.app_running,
            "renderer_running": self.renderer_running,
            "screen_locked": self.screen_locked,
            "window_count": len(self.windows),
            "window_title_kinds": title_kinds,
            "capture_strategy": self.capture_strategy,
        }


class WeixinWindowCaptureUnavailable(RuntimeError):
    """Raised when WeChat is healthy but protected pixels cannot be inspected."""

    def __init__(
        self,
        message: str,
        *,
        runtime_status: WeixinRuntimeStatus | None = None,
        capture_state: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.runtime_status = runtime_status or inspect_weixin_runtime_status()
        self.capture_state = dict(capture_state or {})

    def safe_diagnostics(self) -> dict[str, Any]:
        payload = self.runtime_status.to_safe_dict()
        if self.capture_state:
            payload["capture_state"] = {
                key: self.capture_state.get(key)
                for key in ("found", "sharing_state", "alpha", "layer", "onscreen", "match_score")
                if key in self.capture_state
            }
        payload["failure_kind"] = "weixin_window_capture_unavailable"
        return payload


class FilehelperLatestMessageMismatch(RuntimeError):
    """The exact File Transfer Assistant window is open, but its newest message differs."""


def _parse_window_rows(output: str) -> list[WeixinWindowMetadata]:
    windows: list[WeixinWindowMetadata] = []
    for raw_line in output.splitlines():
        parts = raw_line.split("\t")
        if len(parts) != 6:
            continue
        try:
            windows.append(
                WeixinWindowMetadata(
                    title=parts[0],
                    x=int(parts[1]),
                    y=int(parts[2]),
                    width=int(parts[3]),
                    height=int(parts[4]),
                    minimized=parts[5].strip().lower() == "true",
                )
            )
        except ValueError:
            continue
    return windows


def _command_output(command: list[str], *, timeout: int = 8) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _read_ax_weixin_windows() -> list[WeixinWindowMetadata]:
    script = r'''
set outputText to ""
tell application "System Events"
    if exists (first application process whose bundle identifier is "com.tencent.xinWeChat") then
        set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
        tell wechatProcess
            repeat with currentWindow in windows
                set windowTitle to name of currentWindow as text
                set windowPosition to position of currentWindow
                set windowSize to size of currentWindow
                set isMinimized to value of attribute "AXMinimized" of currentWindow
                set outputText to outputText & windowTitle & tab & (item 1 of windowPosition) & tab & (item 2 of windowPosition) & tab & (item 1 of windowSize) & tab & (item 2 of windowSize) & tab & isMinimized & linefeed
            end repeat
        end tell
    end if
end tell
return outputText
'''
    return _parse_window_rows(_command_output(["osascript", "-e", script]))


def _read_windowserver_weixin_windows() -> list[WeixinWindowMetadata]:
    script = r'''
import Foundation
import CoreGraphics
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
guard let rows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
    exit(0)
}
for row in rows {
    let owner = row[kCGWindowOwnerName as String] as? String ?? ""
    guard owner == "微信" || owner == "WeChat" else { continue }
    let title = row[kCGWindowName as String] as? String ?? ""
    let layer = row[kCGWindowLayer as String] as? Int ?? -1
    guard !title.isEmpty, layer == 0,
          let bounds = row[kCGWindowBounds as String] as? [String: Any],
          let x = bounds["X"] as? NSNumber,
          let y = bounds["Y"] as? NSNumber,
          let width = bounds["Width"] as? NSNumber,
          let height = bounds["Height"] as? NSNumber else { continue }
    print("\(title)\t\(x.intValue)\t\(y.intValue)\t\(width.intValue)\t\(height.intValue)\tfalse")
}
'''
    return _parse_window_rows(_command_output(["swift", "-e", script]))


def inspect_weixin_runtime_status() -> WeixinRuntimeStatus:
    if sys.platform != "darwin":
        return WeixinRuntimeStatus(
            state="unsupported",
            app_running=False,
            renderer_running=False,
            screen_locked=False,
            windows=(),
            capture_strategy="none",
        )
    process_output = _command_output(["ps", "-axo", "pid=,ppid=,command="])
    app_running = any(
        "/微信.app/Contents/MacOS/WeChat" in line and "WeChatAppEx.app" not in line
        for line in process_output.splitlines()
    )
    renderer_running = any(
        "/WeChatAppEx.app/Contents/MacOS/WeChatAppEx" in line and "Helper" not in line
        for line in process_output.splitlines()
    )
    lock_output = _command_output(["ioreg", "-n", "Root", "-d1"])
    screen_locked = (
        '"IOConsoleLocked" = Yes' in lock_output
        or '"CGSSessionScreenIsLocked"=Yes' in lock_output
    )
    ax_windows = _read_ax_weixin_windows() if app_running else []
    windows = ax_windows
    capture_strategy = "outer_ax_plus_renderer_process"
    if app_running and not windows:
        windows = _read_windowserver_weixin_windows()
        capture_strategy = "windowserver_metadata_after_missing_ax"

    if not app_running:
        state = "not_running"
        capture_strategy = "none"
    elif screen_locked:
        state = "screen_locked"
        capture_strategy = "deferred_until_unlock"
    elif not renderer_running:
        state = "starting"
        capture_strategy = "outer_window_only"
    elif not windows:
        state = "no_visible_window"
        capture_strategy = "renderer_process_probe"
    else:
        state = "ready"
    return WeixinRuntimeStatus(
        state=state,
        app_running=app_running,
        renderer_running=renderer_running,
        screen_locked=screen_locked,
        windows=tuple(windows),
        capture_strategy=capture_strategy,
    )


def parse_weixin_playback_assertions(output: str) -> dict[str, Any]:
    """Summarize active WeChat audio/video playback assertions.

    `pmset -g assertions` is metadata-only and remains readable when WeChat
    protects its window pixels. Requiring both assertions avoids treating a
    successful synthetic click or an idle renderer process as playback proof.
    """

    lines = [line.strip() for line in (output or "").splitlines()]
    wechat_lines = [
        line
        for line in lines
        if re.search(r"\bWeChatAppEx\b", line, flags=re.I)
    ]
    playing_audio = any("Playing audio" in line for line in wechat_lines)
    video_wake_lock = any("Video Wake Lock" in line for line in wechat_lines)
    return {
        "playing_audio": playing_audio,
        "video_wake_lock": video_wake_lock,
        "playback_verified": playing_audio and video_wake_lock,
        "evidence_source": "pmset_assertion_metadata",
    }


def inspect_weixin_playback_assertions(*, timeout: int = 5) -> dict[str, Any]:
    output = _command_output(["pmset", "-g", "assertions"], timeout=timeout)
    return parse_weixin_playback_assertions(output)


def wait_for_weixin_playback_assertions(
    *,
    timeout: float = 6.0,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.2, timeout)
    latest = inspect_weixin_playback_assertions()
    while not latest["playback_verified"] and time.monotonic() < deadline:
        time.sleep(max(0.05, poll_interval))
        latest = inspect_weixin_playback_assertions()
    return latest


def current_weixin_video_window() -> WeixinWindowMetadata | None:
    windows = _read_ax_weixin_windows()
    if not windows:
        windows = _read_windowserver_weixin_windows()
    return next(
        (
            window
            for window in windows
            if window.title_kind == "video" and not window.minimized
        ),
        None,
    )


def wait_for_weixin_video_window(
    *,
    timeout: float = 6.0,
    poll_interval: float = 0.2,
) -> WeixinWindowMetadata | None:
    deadline = time.monotonic() + max(0.2, timeout)
    window = current_weixin_video_window()
    while window is None and time.monotonic() < deadline:
        time.sleep(max(0.05, poll_interval))
        window = current_weixin_video_window()
    return window


def trigger_weixin_video_playback(
    *,
    timeout: float = 6.0,
    before_activation: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Start the visible Channels player and prove playback without pixels."""

    window = wait_for_weixin_video_window(timeout=timeout)
    if window is None:
        return {
            "video_window_visible": False,
            "playback_verified": False,
            "error": "weixin_video_window_not_found",
        }

    initial = inspect_weixin_playback_assertions()
    if initial["playback_verified"]:
        return {
            **initial,
            "video_window_visible": True,
            "activation_method": "autoplay",
            "click_attempt_count": 0,
        }

    if before_activation is not None:
        before_activation()

    raise_script = r'''
tell application id "com.tencent.xinWeChat" to activate
tell application "System Events"
  set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
  tell wechatProcess
    set frontmost to true
    if exists window "微信 (窗口)" then
      try
        perform action "AXRaise" of window "微信 (窗口)"
      end try
    end if
  end tell
end tell
'''
    _command_output(["osascript", "-e", raise_script], timeout=5)

    # The first point is the geometric center. The second stays inside the
    # vertical video canvas seen in the official Channels window while avoiding
    # the bottom reaction/share controls.
    points = [
        (window.x + window.width // 2, window.y + window.height // 2),
        (window.x + min(window.width // 2, 220), window.y + window.height // 2),
    ]
    latest = initial
    for index, (x, y) in enumerate(dict.fromkeys(points), start=1):
        _click_screen_point(x, y, label="Weixin video playback", timeout=5)
        latest = wait_for_weixin_playback_assertions(
            timeout=max(1.0, timeout / len(points)),
            poll_interval=0.2,
        )
        if latest["playback_verified"]:
            return {
                **latest,
                "video_window_visible": True,
                "activation_method": "metadata_guided_canvas_click",
                "click_attempt_count": index,
            }
    return {
        **latest,
        "video_window_visible": True,
        "activation_method": "metadata_guided_canvas_click",
        "click_attempt_count": len(points),
        "error": "weixin_playback_assertions_not_observed",
    }


def _window_menu_reveal_snippet() -> str:
    return '''
      if (count of windows) = 0 then
        try
          click menu item "微信" of menu 1 of menu bar item "窗口" of menu bar 1
          delay 0.8
        end try
        if (count of windows) = 0 then
          try
            click menu item "微信 (窗口)" of menu 1 of menu bar item "窗口" of menu bar 1
            delay 0.8
          end try
        end if
      end if
'''


def contains_filehelper_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return FILE_TRANSFER_ASSISTANT_NAME in compact


def contains_pinned_chat_group_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return bool(PINNED_CHAT_GROUP_RE.match(compact))


def _region_arg(region: tuple[int, int, int, int]) -> str:
    return ",".join(str(max(0, int(value))) for value in region)


def _ensure_image_crop() -> Path:
    tool = WORK_ROOT / "helper-tools" / "image_crop"
    source = PROJECT_ROOT / "tools" / "image_crop.swift"
    if not source.exists():
        raise RuntimeError("Image crop helper is missing.")
    if tool.exists() and tool.stat().st_mtime >= source.stat().st_mtime:
        return tool
    tool.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", str(source), "-o", str(tool)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to build image crop helper.")[-1000:])
    return tool


def _capture_fullscreen_with_screencapture(
    image: Path,
    *,
    timeout: int,
) -> None:
    try:
        capture = subprocess.run(
            ["screencapture", "-x", str(image)],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WeixinWindowCaptureUnavailable(
            "WeChat full-screen capture timed out while the app and renderer may still be healthy. "
            "Treat protected pixels as unavailable and use the pinned-chat AX-title path."
        ) from exc
    if capture.returncode != 0:
        raise RuntimeError((capture.stderr or capture.stdout or "screencapture failed")[-1000:])
    if not image.exists() or image.stat().st_size <= 0:
        raise RuntimeError("screencapture returned without creating a usable image.")


def _crop_fullscreen_image_region(
    fullscreen: Path,
    region: tuple[int, int, int, int],
    image: Path,
    *,
    timeout: int,
) -> None:
    tool = _ensure_image_crop()
    proc = subprocess.run(
        [str(tool), str(fullscreen), *_region_arg(region).split(","), str(image)],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Image crop failed.")[-1000:])
    if not image.exists() or image.stat().st_size <= 0:
        raise RuntimeError("Image crop returned without creating a usable image.")


def _capture_screen_region(region: tuple[int, int, int, int], image: Path, *, timeout: int = 15) -> None:
    with tempfile.TemporaryDirectory(prefix="weixin-fullscreen-capture-") as tmp:
        fullscreen = Path(tmp) / "fullscreen.png"
        _capture_fullscreen_with_screencapture(fullscreen, timeout=min(max(1, timeout), 8))
        _crop_fullscreen_image_region(fullscreen, region, image, timeout=min(max(2, timeout), 8))


def _ensure_window_capture_state() -> Path:
    tool = WORK_ROOT / "helper-tools" / "window_capture_state"
    source = PROJECT_ROOT / "tools" / "window_capture_state.swift"
    if not source.exists():
        raise RuntimeError("Window capture-state helper is missing.")
    if tool.exists() and tool.stat().st_mtime >= source.stat().st_mtime:
        return tool
    tool.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", str(source), "-o", str(tool)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to build window capture-state helper.")[-1000:])
    return tool


def weixin_window_capture_state(window: dict[str, Any], *, timeout: int = 5) -> dict[str, Any]:
    tool = _ensure_window_capture_state()
    region = (
        int(window.get("x") or 0),
        int(window.get("y") or 0),
        int(window.get("width") or 0),
        int(window.get("height") or 0),
    )
    proc = subprocess.run(
        [str(tool), *_region_arg(region).split(",")],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr[-1000:] or stdout[-1000:] or "Window capture-state probe failed.")
    try:
        payload = json.loads(stdout)
    except Exception as exc:
        raise RuntimeError(f"Window capture-state JSON parse failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Window capture-state result was not an object.")
    return payload


def require_weixin_window_capture_visible(window: dict[str, Any], *, timeout: int = 5) -> dict[str, Any]:
    payload = weixin_window_capture_state(window, timeout=timeout)
    try:
        sharing_state = int(payload.get("sharing_state"))
    except (TypeError, ValueError):
        sharing_state = -1
    if sharing_state == 0:
        raise WeixinWindowCaptureUnavailable(
            "WeChat screenshot problem: window_sharing_state=0, so full-screen screenshots omit the WeChat layer. "
            "This is a protected-window state, not proof that WeChat exited. "
            "Use process/window metadata plus the protected pinned-chat AX-title route instead.",
            capture_state=payload,
        )
    return payload


def _ensure_vision_ocr() -> Path:
    tool = WORK_ROOT / "helper-tools" / "vision_ocr"
    if tool.exists():
        return tool
    source = PROJECT_ROOT / "tools" / "vision_ocr.swift"
    if not source.exists():
        raise RuntimeError("macOS Vision OCR helper is missing.")
    tool.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", "-parse-as-library", str(source), "-o", str(tool)],
        text=True,
        capture_output=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to build macOS Vision OCR helper.")[-1000:])
    return tool


def ocr_screen_region(region: tuple[int, int, int, int], *, timeout: int = 15) -> str:
    tool = _ensure_vision_ocr()
    with tempfile.TemporaryDirectory(prefix="weixin-filehelper-ocr-") as tmp:
        image = Path(tmp) / "region.png"
        _capture_screen_region(region, image, timeout=timeout)
        proc = subprocess.run(
            [str(tool), str(image)],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Vision OCR failed")[-1000:])
    return (proc.stdout or "").strip()


def ocr_screen_region_observations(region: tuple[int, int, int, int], *, timeout: int = 15) -> list[dict[str, Any]]:
    tool = _ensure_vision_ocr()
    with tempfile.TemporaryDirectory(prefix="weixin-filehelper-ocr-") as tmp:
        image = Path(tmp) / "region.png"
        _capture_screen_region(region, image, timeout=timeout)
        proc = subprocess.run(
            [str(tool), "--json", str(image)],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Vision OCR failed")[-1000:])
    try:
        decoded = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Vision OCR JSON parse failed: {exc}") from exc
    if not isinstance(decoded, list):
        raise RuntimeError("Vision OCR JSON result was not a list.")
    return _normalize_ocr_observations([item for item in decoded if isinstance(item, dict)], region)


def _normalize_ocr_observations(
    observations: list[dict[str, Any]],
    region: tuple[int, int, int, int],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    region_width = float(region[2] or 0)
    region_height = float(region[3] or 0)
    for item in observations:
        row = dict(item)
        try:
            image_width = float(row.get("imageWidth") or 0)
            image_height = float(row.get("imageHeight") or 0)
        except (TypeError, ValueError):
            normalized.append(row)
            continue
        if image_width <= 0 or image_height <= 0 or region_width <= 0 or region_height <= 0:
            normalized.append(row)
            continue
        scale_x = image_width / region_width
        scale_y = image_height / region_height
        if scale_x <= 0 or scale_y <= 0:
            normalized.append(row)
            continue
        for key, scale in (("x", scale_x), ("width", scale_x), ("y", scale_y), ("height", scale_y)):
            try:
                row[key] = float(row.get(key) or 0) / scale
            except (TypeError, ValueError):
                row[key] = 0.0
        normalized.append(row)
    return normalized


def filehelper_chat_list_region(window: dict[str, Any]) -> tuple[int, int, int, int]:
    x = int(float(window.get("x", 0)))
    y = int(float(window.get("y", 0)))
    height = int(float(window.get("height", 861)))
    return (x + 72, y + 60, 245, max(180, height - 118))


def filehelper_click_point_from_ocr(
    region: tuple[int, int, int, int],
    observations: list[dict[str, Any]],
) -> tuple[int, int]:
    item = filehelper_observation_from_ocr(observations)
    x = float(item.get("x") or 0)
    y = float(item.get("y") or 0)
    width = float(item.get("width") or 0)
    height = float(item.get("height") or 0)
    return (
        int(region[0] + x + width / 2),
        int(region[1] + y + height / 2),
    )


def filehelper_observation_from_ocr(observations: list[dict[str, Any]]) -> dict[str, Any]:
    for item in observations:
        text = str(item.get("text") or "")
        if contains_filehelper_text(text):
            return item
    raise RuntimeError(
        "File Transfer Assistant was not visible in the current pinned/chat list region."
    )


def filehelper_green_icon_region_from_observation(
    region: tuple[int, int, int, int],
    observation: dict[str, Any],
) -> tuple[int, int, int, int]:
    text_x = float(observation.get("x") or 0)
    text_y = float(observation.get("y") or 0)
    text_height = float(observation.get("height") or 0)
    icon_size = int(max(36, min(52, text_height * 1.8)))
    icon_x = int(region[0] + max(8, text_x - icon_size - 14))
    icon_y = int(region[1] + text_y + text_height / 2 - icon_size / 2)
    return (icon_x, icon_y, icon_size, icon_size)


def filehelper_green_icon_probe_passes(payload: dict[str, Any]) -> bool:
    try:
        green_pixels = int(payload.get("green_pixels") or 0)
        white_pixels = int(payload.get("white_pixels") or 0)
        green_ratio = float(payload.get("green_ratio") or 0.0)
        white_ratio = float(payload.get("white_ratio") or 0.0)
        green_colored_ratio = float(payload.get("green_colored_ratio") or 0.0)
    except (TypeError, ValueError):
        return False
    green_passes = green_pixels >= 180 and (green_ratio >= 0.08 or green_colored_ratio >= 0.45)
    white_shape_passes = white_pixels >= 20 and white_ratio >= 0.01
    return green_passes and white_shape_passes


def _ensure_green_icon_probe() -> Path:
    tool = WORK_ROOT / "helper-tools" / "green_icon_probe"
    source = PROJECT_ROOT / "tools" / "green_icon_probe.swift"
    if not source.exists():
        raise RuntimeError("File Transfer Assistant green icon probe helper is missing.")
    if tool.exists() and tool.stat().st_mtime >= source.stat().st_mtime:
        return tool
    tool.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", str(source), "-o", str(tool)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to build green icon probe helper.")[-1000:])
    return tool


def verify_filehelper_green_icon(
    region: tuple[int, int, int, int],
    observations: list[dict[str, Any]],
    *,
    timeout: int = 10,
) -> dict[str, Any]:
    observation = filehelper_observation_from_ocr(observations)
    icon_region = filehelper_green_icon_region_from_observation(region, observation)
    tool = _ensure_green_icon_probe()
    with tempfile.TemporaryDirectory(prefix="weixin-filehelper-icon-") as tmp:
        image = Path(tmp) / "icon.png"
        _capture_screen_region(icon_region, image, timeout=timeout)
        proc = subprocess.run(
            [str(tool), str(image)],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "File Transfer Assistant icon probe failed.")[-1000:])
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"File Transfer Assistant icon probe JSON parse failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("File Transfer Assistant icon probe result was not an object.")
    payload["icon_region"] = icon_region
    payload["filehelper_icon_verified"] = filehelper_green_icon_probe_passes(payload)
    if payload["filehelper_icon_verified"]:
        payload["filehelper_icon_signature"] = FILEHELPER_ICON_SIGNATURE
    payload["filehelper_name_ocr_text"] = str(observation.get("text") or "")
    payload["filehelper_name_verified"] = contains_filehelper_text(payload["filehelper_name_ocr_text"])
    if not payload["filehelper_icon_verified"]:
        raise RuntimeError(
            "File Transfer Assistant green icon verification failed; "
            f"icon_probe={payload!r}"
        )
    return payload


def pinned_chat_group_click_point_from_ocr(
    region: tuple[int, int, int, int],
    observations: list[dict[str, Any]],
) -> tuple[int, int]:
    for item in observations:
        text = str(item.get("text") or "")
        if not contains_pinned_chat_group_text(text):
            continue
        x = float(item.get("x") or 0)
        y = float(item.get("y") or 0)
        height = float(item.get("height") or 0)
        return (
            int(region[0] + max(0, region[2] - 80)),
            int(region[1] + y + height / 2),
        )
    raise RuntimeError("Collapsed pinned-chat group was not visible in the current chat list region.")


def filehelper_header_region(window: dict[str, Any]) -> tuple[int, int, int, int]:
    x = int(float(window.get("x", 0)))
    y = int(float(window.get("y", 0)))
    width = int(float(window.get("width", 624)))
    return (x + 260, y + 0, max(320, width - 270), 95)


def weixin_filehelper_applescript(click_after_send: bool = True) -> str:
    """Return AppleScript that sends one URL to the already verified chat.

    The caller must select and verify File Transfer Assistant before this
    script runs. The script only uses UI automation and the clipboard. It does
    not read WeChat local databases, cookies, tokens, or account files.
    """

    click_latest = "true" if click_after_send else "false"
    return f"""
on jsonEscape(valueText)
  set valueText to valueText as text
  set AppleScript's text item delimiters to "\\\\"
  set parts to every text item of valueText
  set AppleScript's text item delimiters to "\\\\\\\\"
  set valueText to parts as text
  set AppleScript's text item delimiters to "\\""
  set parts to every text item of valueText
  set AppleScript's text item delimiters to "\\\\\\""
  set valueText to parts as text
  set AppleScript's text item delimiters to ""
  return valueText
end jsonEscape

on run argv
  if (count of argv) < 1 then error "missing Weixin URL"
  set targetUrl to item 1 of argv
  set assistantName to "{FILE_TRANSFER_ASSISTANT_NAME}"
  set shouldClickLatest to {click_latest}
  set previousClipboard to missing value
  try
    set previousClipboard to the clipboard
  end try

  tell application "WeChat" to activate
  delay 0.8

  tell application "System Events"
    if not (exists application process "WeChat") then error "WeChat process not found"
    tell application process "WeChat"
      set frontmost to true
      delay 0.3
{_window_menu_reveal_snippet()}
      set targetWindow to missing value
      if exists window assistantName then
        set targetWindow to window assistantName
      else if exists window "微信" then
        set targetWindow to window "微信"
      else if (count of windows) > 0 then
        set targetWindow to window 1
      else
        error "WeChat window not found"
      end if
      try
        set value of attribute "AXMinimized" of targetWindow to false
      end try
      try
        perform action "AXRaise" of targetWindow
      end try
      delay 0.3

      set winPos to position of targetWindow
      set winSize to size of targetWindow
      set winX to item 1 of winPos
      set winY to item 2 of winPos
      set winW to item 1 of winSize
      set winH to item 2 of winSize

      -- Focus the message input area, paste the target URL, and send it. This
      -- runs only after Python verified the exact conversation target.
      click at {{winX + (winW / 2), winY + winH - 98}}
      delay 0.2
      keystroke "a" using {{command down}}
      delay 0.1
      set the clipboard to targetUrl
      keystroke "v" using {{command down}}
      delay 0.2
      key code 36
      delay 1.3

      -- Content-area link clicking is handled by a CoreGraphics helper in
      -- Python because AppleScript click-at can land on the menu layer.
    end tell
  end tell

  try
    if previousClipboard is not missing value then set the clipboard to previousClipboard
  end try

  return "{{\\"ok\\":true,\\"method\\":\\"file_transfer_assistant\\",\\"clicked_latest\\":false,\\"requested_click_latest\\":" & shouldClickLatest & ",\\"window_x\\":" & (winX as integer) & ",\\"window_y\\":" & (winY as integer) & ",\\"window_width\\":" & (winW as integer) & ",\\"window_height\\":" & (winH as integer) & "}}"
end run
"""


def _verified_bool(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.lower() == "true"
    return False


def require_filehelper_target_verified(payload: dict[str, Any]) -> None:
    title = str(payload.get("selected_chat_title") or "")
    name_text = str(payload.get("filehelper_name_ocr_text") or "")
    header_text = str(payload.get("selected_header_ocr_text") or "")
    name_verified = _verified_bool(payload.get("filehelper_name_verified")) and contains_filehelper_text(name_text)
    header_verified = _verified_bool(payload.get("filehelper_header_verified")) and contains_filehelper_text(header_text)
    icon_verified = _verified_bool(payload.get("filehelper_icon_verified"))
    icon_signature = str(payload.get("filehelper_icon_signature") or "")
    pixel_gate_verified = (
        title == FILE_TRANSFER_ASSISTANT_NAME
        and _verified_bool(payload.get("selected_chat_verified"))
        and name_verified
        and header_verified
        and icon_verified
        and icon_signature == FILEHELPER_ICON_SIGNATURE
    )
    protected_gate_verified = (
        title == FILE_TRANSFER_ASSISTANT_NAME
        and _verified_bool(payload.get("selected_chat_verified"))
        and _verified_bool(payload.get("protected_window_metadata_verified"))
        and _verified_bool(payload.get("exact_window_title_verified"))
        and str(payload.get("protected_filehelper_signature") or "")
        == PROTECTED_FILEHELPER_SIGNATURE
    )
    if pixel_gate_verified or protected_gate_verified:
        return
    raise RuntimeError(
        "File Transfer Assistant target verification failed; "
        f"selected_chat_title={title or '<unknown>'!r}; "
        f"name_ocr_text={name_text or '<missing>'!r}; "
        f"header_ocr_text={header_text or '<missing>'!r}; "
        f"name_verified={name_verified!r}; "
        f"header_verified={header_verified!r}; "
        f"green_icon_verified={icon_verified!r}; "
        f"green_icon_signature={icon_signature or '<missing>'!r}; "
        f"protected_window_metadata_verified="
        f"{_verified_bool(payload.get('protected_window_metadata_verified'))!r}; "
        f"exact_window_title_verified="
        f"{_verified_bool(payload.get('exact_window_title_verified'))!r}; "
        f"protected_filehelper_signature="
        f"{str(payload.get('protected_filehelper_signature') or '<missing>')!r}"
    )


def activate_weixin_main_window(*, timeout: int = 10) -> dict[str, int]:
    script = r'''
tell application id "com.tencent.xinWeChat" to activate
tell application "System Events"
  set wechatProcess to missing value
  repeat 20 times
    try
      set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
      exit repeat
    end try
    delay 0.25
  end repeat
  if wechatProcess is missing value then error "WeChat process not found by bundle identifier"
  tell wechatProcess
    set frontmost to true
__WINDOW_REVEAL_SNIPPET__
    set targetWindow to missing value
    if exists window "微信" then
      set targetWindow to window "微信"
    else if (count of windows) > 0 then
      set targetWindow to window 1
    else
      error "WeChat window not found"
    end if
    try
      set value of attribute "AXMinimized" of targetWindow to false
    end try
    try
      perform action "AXRaise" of targetWindow
    end try
    set winPos to position of targetWindow
    set winSize to size of targetWindow
    return "{\"x\":" & ((item 1 of winPos) as integer) & ",\"y\":" & ((item 2 of winPos) as integer) & ",\"width\":" & ((item 1 of winSize) as integer) & ",\"height\":" & ((item 2 of winSize) as integer) & "}"
  end tell
end tell
'''.replace("__WINDOW_REVEAL_SNIPPET__", _window_menu_reveal_snippet())
    proc = subprocess.run(["osascript", "-e", script], text=True, capture_output=True, timeout=timeout)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr[-1000:] or stdout[-1000:] or "Failed to activate WeChat main window.")
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Failed to parse WeChat window geometry: {exc}") from exc
    return {
        "x": int(payload.get("x") or 0),
        "y": int(payload.get("y") or 0),
        "width": int(payload.get("width") or 0),
        "height": int(payload.get("height") or 0),
    }


def _click_screen_point(
    x: int,
    y: int,
    *,
    label: str,
    count: int = 1,
    button: str = "left",
    timeout: int = 5,
) -> None:
    if button not in {"left", "right"}:
        raise ValueError(f"Unsupported mouse button: {button}")
    tool = _ensure_macos_click()
    click = subprocess.run(
        [str(tool), str(x), str(y), str(max(1, count)), button],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if click.returncode != 0:
        raise RuntimeError((click.stderr or click.stdout or f"{label} click failed.")[-1000:])


def _ensure_sck_display_exact_text() -> Path:
    tool = WORK_ROOT / "helper-tools" / "sck_display_exact_text"
    source = PROJECT_ROOT / "tools" / "sck_display_exact_text.swift"
    if not source.exists():
        raise RuntimeError("ScreenCaptureKit exact-text helper is missing.")
    if tool.exists() and tool.stat().st_mtime >= source.stat().st_mtime:
        return tool
    tool.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", str(source), "-o", str(tool)],
        text=True,
        capture_output=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or "Failed to build ScreenCaptureKit exact-text helper.")[-1000:]
        )
    return tool


def display_exact_text_click_point(
    target: str,
    region: tuple[int, int, int, int],
    *,
    expected_point: tuple[int, int] | None = None,
    timeout: int = 12,
) -> tuple[int, int]:
    """Locate an exact visible menu label in display-composition pixels.

    Protected WeChat content may be omitted from screenshots, while the native
    context-menu layer remains visible. Binding the match to a caller-supplied
    region prevents unrelated text elsewhere on the display from authorizing a
    click.
    """

    compact_target = re.sub(r"\s+", "", target or "")
    if not compact_target:
        raise ValueError("An exact menu label is required.")
    tool = _ensure_sck_display_exact_text()
    with tempfile.TemporaryDirectory(prefix="weixin-menu-text-") as tmp:
        crop = Path(tmp) / "region.png"
        full = Path(tmp) / "display.png"
        proc = subprocess.run(
            [
                str(tool),
                target,
                str(crop),
                str(full),
                *[str(int(value)) for value in region],
            ],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr[-1000:] or stdout[-1000:] or f"Exact menu label {target!r} was not captured.")
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Exact menu label capture JSON parse failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Exact menu label capture result was not an object.")
    matches = [
        item
        for item in payload.get("matches", [])
        if isinstance(item, dict)
        and re.sub(r"\s+", "", str(item.get("text") or "")) == compact_target
    ]
    if not matches:
        raise RuntimeError(f"Exact visible menu label {target!r} was not found in the bounded WeChat region.")
    if expected_point is None:
        selected = matches[0]
    else:
        selected = min(
            matches,
            key=lambda item: (
                int(item.get("center_x") or 0) - expected_point[0]
            )
            ** 2
            + (
                int(item.get("center_y") or 0) - expected_point[1]
            )
            ** 2,
        )
    return (int(selected.get("center_x") or 0), int(selected.get("center_y") or 0))


def _ensure_macos_scroll() -> Path:
    tool = WORK_ROOT / "helper-tools" / "macos_scroll"
    if tool.exists():
        return tool
    source = PROJECT_ROOT / "tools" / "macos_scroll.swift"
    if not source.exists():
        raise RuntimeError("macOS scroll helper is missing.")
    tool.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", str(source), "-o", str(tool)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to build macOS scroll helper.")[-1000:])
    return tool


def _scroll_chat_list(region: tuple[int, int, int, int], dy: int, *, timeout: int = 5) -> None:
    tool = _ensure_macos_scroll()
    x = int(region[0] + region[2] / 2)
    y = int(region[1] + region[3] / 2)
    proc = subprocess.run(
        [str(tool), str(x), str(y), str(dy), "3"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Chat list scroll failed.")[-1000:])


def _try_click_filehelper_from_observations(
    window: dict[str, Any],
    chat_region: tuple[int, int, int, int],
    observations: list[dict[str, Any]],
    *,
    timeout: int,
) -> dict[str, Any] | None:
    try:
        x, y = filehelper_click_point_from_ocr(chat_region, observations)
    except RuntimeError:
        return None
    icon_payload = verify_filehelper_green_icon(chat_region, observations, timeout=timeout)
    opened_by_existing_header = False
    header_text = ocr_screen_region(filehelper_header_region(window), timeout=min(8, max(2, timeout)))
    if contains_filehelper_text(header_text):
        opened_by_existing_header = True
    else:
        _click_screen_point(x, y, label="File Transfer Assistant row", count=1, timeout=timeout)
        time.sleep(1.5)
        header_text = wait_for_filehelper_header(window, timeout=timeout)
    header_verified = contains_filehelper_text(header_text)
    return {
        "selected_chat_title": FILE_TRANSFER_ASSISTANT_NAME,
        "selected_chat_verified": True,
        "chat_list_ocr_text": "\n".join(str(item.get("text") or "") for item in observations),
        "selected_header_ocr_text": header_text,
        "filehelper_header_verified": header_verified,
        "window_x": window["x"],
        "window_y": window["y"],
        "window_width": window["width"],
        "window_height": window["height"],
        "filehelper_click_x": x,
        "filehelper_click_y": y,
        "filehelper_name_verified_by": "left_list_ocr_and_header_ocr",
        "filehelper_opened_by_existing_header": opened_by_existing_header,
        **icon_payload,
    }


def wait_for_filehelper_header(window: dict[str, Any], *, timeout: int = 15) -> str:
    deadline = time.monotonic() + max(2, timeout)
    header_region = filehelper_header_region(window)
    last_text = ""
    while time.monotonic() < deadline:
        last_text = ocr_screen_region(header_region, timeout=min(8, max(2, timeout)))
        if contains_filehelper_text(last_text):
            return last_text
        time.sleep(0.5)
    if not last_text.strip():
        raise WeixinWindowCaptureUnavailable(
            "WeChat header capture returned no readable pixels while the runtime remained available."
        )
    raise RuntimeError(
        "File Transfer Assistant target verification failed after visible-list click; "
        f"header_ocr_text={last_text!r}"
    )


def keyboard_select_filehelper(*, timeout: int = 10) -> dict[str, Any]:
    script = f'''
on run
  set previousClipboard to missing value
  try
    set previousClipboard to the clipboard
  end try
  tell application "WeChat" to activate
  delay 0.4
  tell application "System Events"
    if not (exists application process "WeChat") then error "WeChat process not found"
    tell application process "WeChat"
      set frontmost to true
{_window_menu_reveal_snippet()}
      if exists window "微信" then
        set targetWindow to window "微信"
      else if (count of windows) > 0 then
        set targetWindow to window 1
      else
        error "WeChat window not found"
      end if
      try
        set value of attribute "AXMinimized" of targetWindow to false
      end try
      try
        perform action "AXRaise" of targetWindow
      end try
      delay 0.2
      keystroke "f" using {{command down}}
      delay 0.2
      set the clipboard to "{FILE_TRANSFER_ASSISTANT_NAME}"
      keystroke "v" using {{command down}}
      delay 0.4
      key code 36
      delay 1.0
      set winPos to position of targetWindow
      set winSize to size of targetWindow
      set winX to item 1 of winPos
      set winY to item 2 of winPos
      set winW to item 1 of winSize
      set winH to item 2 of winSize
    end tell
  end tell
  try
    if previousClipboard is not missing value then set the clipboard to previousClipboard
  end try
  return "{{\\"method\\":\\"keyboard_search\\",\\"selected_chat_title\\":\\"{FILE_TRANSFER_ASSISTANT_NAME}\\",\\"window_x\\":" & (winX as integer) & ",\\"window_y\\":" & (winY as integer) & ",\\"window_width\\":" & (winW as integer) & ",\\"window_height\\":" & (winH as integer) & "}}"
end run
'''
    proc = subprocess.run(["osascript", "-e", script], text=True, capture_output=True, timeout=timeout)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr[-1000:] or stdout[-1000:] or "Keyboard File Transfer Assistant selection failed.")
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Keyboard File Transfer Assistant selection JSON parse failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Keyboard File Transfer Assistant selection result was not an object.")
    return payload


def _try_select_filehelper_by_keyboard(
    window: dict[str, Any],
    chat_region: tuple[int, int, int, int],
    *,
    timeout: int,
) -> dict[str, Any] | None:
    try:
        keyboard_payload = keyboard_select_filehelper(timeout=timeout)
        time.sleep(0.5)
        observations = ocr_screen_region_observations(chat_region, timeout=timeout)
        icon_payload = verify_filehelper_green_icon(chat_region, observations, timeout=timeout)
        header_text = wait_for_filehelper_header(window, timeout=timeout)
    except WeixinWindowCaptureUnavailable:
        raise
    except Exception:
        return None
    return {
        "selected_chat_title": FILE_TRANSFER_ASSISTANT_NAME,
        "selected_chat_verified": True,
        "selected_header_ocr_text": header_text,
        "filehelper_header_verified": contains_filehelper_text(header_text),
        "chat_list_ocr_text": "\n".join(str(item.get("text") or "") for item in observations),
        "window_x": window["x"],
        "window_y": window["y"],
        "window_width": window["width"],
        "window_height": window["height"],
        "filehelper_name_verified_by": "keyboard_search_header_ocr_and_green_icon",
        "filehelper_opened_by_keyboard_search": True,
        **keyboard_payload,
        **icon_payload,
    }


def _protected_filehelper_payload(
    window: WeixinWindowMetadata,
    *,
    source: str,
    capture_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "selected_chat_title": FILE_TRANSFER_ASSISTANT_NAME,
        "selected_chat_verified": True,
        "protected_window_metadata_verified": True,
        "exact_window_title_verified": window.title.strip() == FILE_TRANSFER_ASSISTANT_NAME,
        "protected_filehelper_signature": PROTECTED_FILEHELPER_SIGNATURE,
        "filehelper_name_verified_by": source,
        "window_x": window.x,
        "window_y": window.y,
        "window_width": window.width,
        "window_height": window.height,
    }
    if capture_state:
        payload["window_capture_state"] = dict(capture_state)
    return payload


def raise_exact_filehelper_window(*, timeout: int = 8) -> WeixinWindowMetadata | None:
    """Raise only an AX window whose title is exactly File Transfer Assistant."""

    script = f'''
tell application id "com.tencent.xinWeChat" to activate
tell application "System Events"
  if not (exists (first application process whose bundle identifier is "com.tencent.xinWeChat")) then
    error "WeChat process not found"
  end if
  set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
  tell wechatProcess
    if not (exists window "{FILE_TRANSFER_ASSISTANT_NAME}") then return ""
    set targetWindow to window "{FILE_TRANSFER_ASSISTANT_NAME}"
    try
      set value of attribute "AXMinimized" of targetWindow to false
    end try
    try
      perform action "AXRaise" of targetWindow
    end try
    set frontmost to true
    set winPos to position of targetWindow
    set winSize to size of targetWindow
    return "{{\\"x\\":" & ((item 1 of winPos) as integer) & ",\\"y\\":" & ((item 2 of winPos) as integer) & ",\\"width\\":" & ((item 1 of winSize) as integer) & ",\\"height\\":" & ((item 2 of winSize) as integer) & "}}"
  end tell
end tell
'''
    proc = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr[-1000:] or stdout[-1000:] or "Failed to inspect File Transfer Assistant window.")
    if not stdout:
        return None
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Exact File Transfer Assistant window JSON parse failed: {exc}") from exc
    return WeixinWindowMetadata(
        title=FILE_TRANSFER_ASSISTANT_NAME,
        x=int(payload.get("x") or 0),
        y=int(payload.get("y") or 0),
        width=int(payload.get("width") or 0),
        height=int(payload.get("height") or 0),
    )


def _dismiss_weixin_context_menu(*, timeout: int = 5) -> None:
    _command_output(
        [
            "osascript",
            "-e",
            'tell application "System Events" to tell process "WeChat" to key code 53',
        ],
        timeout=timeout,
    )


def _close_new_auxiliary_weixin_window(
    window: WeixinWindowMetadata,
    *,
    timeout: int = 5,
) -> None:
    """Close only a newly opened, non-target auxiliary chat window."""

    if (
        not window.title.strip()
        or window.title.strip() == FILE_TRANSFER_ASSISTANT_NAME
        or window.title_kind in {"main", "video"}
    ):
        return
    script = r'''
on run argv
  set targetTitle to item 1 of argv
  set targetX to item 2 of argv as integer
  set targetY to item 3 of argv as integer
  set targetWidth to item 4 of argv as integer
  set targetHeight to item 5 of argv as integer
  tell application "System Events"
    set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
    tell wechatProcess
      repeat with candidateWindow in windows
        if (name of candidateWindow as text) is targetTitle then
          set candidatePosition to position of candidateWindow
          set candidateSize to size of candidateWindow
          if (item 1 of candidatePosition as integer) is targetX then
            if (item 2 of candidatePosition as integer) is targetY then
              if (item 1 of candidateSize as integer) is targetWidth then
                if (item 2 of candidateSize as integer) is targetHeight then
                  try
                    click button 1 of candidateWindow
                  end try
                  return "closed"
                end if
              end if
            end if
          end if
        end if
      end repeat
    end tell
  end tell
  return "not_found"
end run
'''
    subprocess.run(
        [
            "osascript",
            "-e",
            script,
            window.title,
            str(window.x),
            str(window.y),
            str(window.width),
            str(window.height),
        ],
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def select_protected_filehelper_from_pinned_rows(
    window: dict[str, Any],
    *,
    capture_state: dict[str, Any] | None = None,
    timeout: int = 15,
    max_rows: int = 8,
) -> dict[str, Any]:
    """Resolve a protected pinned chat through exact AX window-title proof.

    The protected chat pixels are never treated as readable. Each bounded row
    is opened through its visible native context menu, and no message can be
    sent until the resulting independent window title is exactly
    ``文件传输助手``.
    """

    existing = raise_exact_filehelper_window(timeout=min(timeout, 8))
    if existing is not None:
        return _protected_filehelper_payload(
            existing,
            source="existing_exact_ax_window_title",
            capture_state=capture_state,
        )

    _ensure_sck_display_exact_text()
    win_x = int(window.get("x") or 0)
    win_y = int(window.get("y") or 0)
    win_width = int(window.get("width") or 0)
    win_height = int(window.get("height") or 0)
    if win_width < 480 or win_height < 360:
        raise RuntimeError("Protected pinned-chat scan requires valid WeChat main-window geometry.")
    menu_region = (win_x, win_y, win_width, win_height)
    row_x = win_x + 150
    first_row_y = win_y + 72
    bounded_rows = max(1, min(max_rows, max(1, (win_height - 180) // 68)))

    for row_index in range(bounded_rows):
        row_y = first_row_y + row_index * 68
        if row_y >= win_y + win_height - 150:
            break
        activate_weixin_main_window(timeout=min(timeout, 8))
        before = {
            (item.title, item.x, item.y, item.width, item.height)
            for item in _read_ax_weixin_windows()
        }
        _click_screen_point(
            row_x,
            row_y,
            label=f"Protected pinned chat row {row_index + 1}",
            button="right",
            timeout=min(timeout, 8),
        )
        time.sleep(0.25)
        try:
            menu_x, menu_y = display_exact_text_click_point(
                "独立窗口显示",
                menu_region,
                expected_point=(row_x + 120, row_y),
                timeout=min(timeout, 12),
            )
        except Exception:
            _dismiss_weixin_context_menu()
            continue
        _click_screen_point(
            menu_x,
            menu_y,
            label="Open pinned chat in independent window",
            timeout=min(timeout, 8),
        )
        time.sleep(0.45)
        exact = raise_exact_filehelper_window(timeout=min(timeout, 8))
        if exact is not None:
            payload = _protected_filehelper_payload(
                exact,
                source="protected_pinned_row_exact_ax_window_title",
                capture_state=capture_state,
            )
            payload["protected_pinned_row_index"] = row_index
            return payload

        after = _read_ax_weixin_windows()
        new_auxiliary = [
            item
            for item in after
            if (item.title, item.x, item.y, item.width, item.height) not in before
            and item.title_kind == "other"
            and item.title.strip() != FILE_TRANSFER_ASSISTANT_NAME
        ]
        for candidate in new_auxiliary:
            _close_new_auxiliary_weixin_window(candidate, timeout=min(timeout, 8))

    raise WeixinWindowCaptureUnavailable(
        "Protected WeChat pixels were unavailable and no pinned row produced an exact "
        "File Transfer Assistant AX window title. No message was sent.",
        capture_state=capture_state,
    )


def select_visible_filehelper(*, timeout: int = 15) -> dict[str, Any]:
    existing = raise_exact_filehelper_window(timeout=min(timeout, 8))
    if existing is not None:
        return _protected_filehelper_payload(
            existing,
            source="existing_exact_ax_window_title",
        )

    window = activate_weixin_main_window(timeout=timeout)
    try:
        capture_state = require_weixin_window_capture_visible(window, timeout=min(5, max(2, timeout)))
    except WeixinWindowCaptureUnavailable as exc:
        return select_protected_filehelper_from_pinned_rows(
            window,
            capture_state=exc.capture_state,
            timeout=timeout,
        )
    chat_region = filehelper_chat_list_region(window)
    all_seen_text: list[str] = []
    expanded_pinned_group = False

    observations = ocr_screen_region_observations(chat_region, timeout=timeout)
    if not observations:
        raise WeixinWindowCaptureUnavailable(
            "WeChat chat-list capture returned no readable pixels while the runtime remained available."
        )
    all_seen_text.extend(str(item.get("text") or "") for item in observations)
    selected = _try_click_filehelper_from_observations(window, chat_region, observations, timeout=timeout)
    if selected:
        selected["expanded_pinned_chat_group"] = expanded_pinned_group
        selected["window_capture_state"] = capture_state
        selected["chat_list_ocr_text"] = "\n".join(all_seen_text)
        return selected

    try:
        group_x, group_y = pinned_chat_group_click_point_from_ocr(chat_region, observations)
    except RuntimeError:
        group_x = group_y = 0
    if group_x and group_y:
        _click_screen_point(group_x, group_y, label="Collapsed pinned-chat group", timeout=timeout)
        expanded_pinned_group = True
        time.sleep(0.8)
        observations = ocr_screen_region_observations(chat_region, timeout=timeout)
        all_seen_text.extend(str(item.get("text") or "") for item in observations)
        selected = _try_click_filehelper_from_observations(window, chat_region, observations, timeout=timeout)
        if selected:
            selected["expanded_pinned_chat_group"] = expanded_pinned_group
            selected["window_capture_state"] = capture_state
            selected["chat_list_ocr_text"] = "\n".join(all_seen_text)
            return selected

    raise RuntimeError(
        "File Transfer Assistant was not found in the visible pinned-chat list after bounded OCR verification. "
        f"seen_chat_list_text={'; '.join(all_seen_text[-30:])!r}"
    )


def latest_filehelper_link_click_point(window: dict[str, Any]) -> tuple[int, int]:
    """Return the verified click point for the newest sent link bubble."""

    x = int(float(window.get("x", 0)))
    y = int(float(window.get("y", 0)))
    width = int(float(window.get("width", 0)))
    height = int(float(window.get("height", 0)))
    if width <= 0 or height <= 0:
        raise RuntimeError("Missing WeChat window geometry for latest link click.")
    return (x + max(120, width - 174), y + max(120, height - 167))


def filehelper_message_region(window: dict[str, Any]) -> tuple[int, int, int, int]:
    x = int(float(window.get("x", 0)))
    y = int(float(window.get("y", 0)))
    width = int(float(window.get("width", 0)))
    height = int(float(window.get("height", 0)))
    if width <= 320 or height <= 260:
        raise RuntimeError("Missing WeChat window geometry for message OCR.")
    return (x + 250, y + 90, width - 260, height - 180)


def exact_filehelper_link_click_point_from_ocr(
    window: dict[str, Any],
    url: str,
    observations: list[dict[str, Any]],
) -> tuple[int, int]:
    match = re.search(r"(?:weixin\.qq\.com/sph/|sph/)([A-Za-z0-9_-]+)", url or "")
    if not match:
        raise RuntimeError("Cannot identify the supplied Weixin short-link ID.")
    short_uri = match.group(1)
    matching: list[dict[str, Any]] = []
    for observation in observations:
        compact = re.sub(r"\s+", "", str(observation.get("text") or ""))
        if short_uri in compact:
            matching.append(observation)
    if not matching:
        raise RuntimeError(
            "The exact newly sent Weixin short-link ID was not readable in File Transfer Assistant."
        )
    newest = max(
        matching,
        key=lambda item: (
            float(item.get("y") or 0),
            float(item.get("x") or 0),
        ),
    )
    region = filehelper_message_region(window)
    return (
        int(region[0] + float(newest.get("x") or 0) + float(newest.get("width") or 0) / 2),
        int(region[1] + float(newest.get("y") or 0) + float(newest.get("height") or 0) / 2),
    )


def _window_geometry_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "x": int(payload.get("window_x") or 0),
        "y": int(payload.get("window_y") or 0),
        "width": int(payload.get("window_width") or 0),
        "height": int(payload.get("window_height") or 0),
    }


def _ensure_macos_click() -> Path:
    tool = WORK_ROOT / "helper-tools" / "macos_click"
    if tool.exists():
        return tool
    source = PROJECT_ROOT / "tools" / "macos_click.swift"
    if not source.exists():
        raise RuntimeError("macOS click helper is missing.")
    tool.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", str(source), "-o", str(tool)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Failed to build macOS click helper.")[-1000:])
    return tool


def click_latest_filehelper_link(
    window: dict[str, Any],
    *,
    url: str = "",
    timeout: int = 5,
) -> dict[str, Any]:
    if url:
        region = filehelper_message_region(window)
        observations = ocr_screen_region_observations(region, timeout=max(5, timeout))
        x, y = exact_filehelper_link_click_point_from_ocr(window, url, observations)
        click_method = "coregraphics_exact_short_uri_ocr"
    else:
        x, y = latest_filehelper_link_click_point(window)
        click_method = "coregraphics_legacy_fixed_point"
    tool = _ensure_macos_click()
    proc = subprocess.run(
        [str(tool), str(x), str(y), "1", "left"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    payload: dict[str, Any] = {
        "click_method": click_method,
        "click_x": x,
        "click_y": y,
        "click_returncode": proc.returncode,
    }
    if proc.stderr:
        payload["click_stderr_tail"] = proc.stderr[-1000:]
    if proc.stdout:
        payload["click_stdout_tail"] = proc.stdout[-1000:]
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:] or proc.stdout[-1000:] or "Latest Weixin link click failed.")
    return payload


def _read_plain_clipboard(*, timeout: int = 3) -> str | None:
    proc = subprocess.run(
        ["pbpaste"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _write_plain_clipboard(value: str, *, timeout: int = 3) -> None:
    subprocess.run(
        ["pbcopy"],
        input=value,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def verify_and_click_latest_filehelper_link(
    window: dict[str, Any],
    url: str,
    *,
    timeout: int = 12,
) -> dict[str, Any]:
    """Copy back the newest message and click only after an exact URL match."""

    if not url:
        raise RuntimeError("A target URL is required for latest-message verification.")
    _ensure_sck_display_exact_text()
    x, y = latest_filehelper_link_click_point(window)
    region = (
        int(window.get("x") or 0),
        int(window.get("y") or 0),
        int(window.get("width") or 0),
        int(window.get("height") or 0),
    )
    previous_clipboard = _read_plain_clipboard()
    copied_text: str | None = None
    try:
        _click_screen_point(
            x,
            y,
            label="Latest File Transfer Assistant message context menu",
            button="right",
            timeout=min(timeout, 8),
        )
        time.sleep(0.25)
        copy_x, copy_y = display_exact_text_click_point(
            "复制",
            region,
            expected_point=(x, y),
            timeout=timeout,
        )
        _click_screen_point(
            copy_x,
            copy_y,
            label="Copy latest File Transfer Assistant message",
            timeout=min(timeout, 8),
        )
        time.sleep(0.2)
        copied_text = _read_plain_clipboard()
    except Exception:
        _dismiss_weixin_context_menu()
        raise
    finally:
        if previous_clipboard is not None:
            _write_plain_clipboard(previous_clipboard)

    if copied_text is None or not copied_text.strip():
        raise RuntimeError(
            "Latest File Transfer Assistant message could not be copied back; "
            "refusing both a blind click and a blind resend."
        )
    if copied_text.strip() != url.strip():
        raise FilehelperLatestMessageMismatch(
            "Latest File Transfer Assistant message did not match the supplied URL; "
            "refusing to click a stale or wrong link."
        )

    _click_screen_point(
        x,
        y,
        label="Verified latest Weixin link",
        timeout=min(timeout, 8),
    )
    short_match = re.search(r"/sph/([A-Za-z0-9_-]+)", url)
    return {
        "message_copyback_verified": True,
        "message_copyback_match": True,
        "verified_short_uri": short_match.group(1) if short_match else "",
        "click_method": "latest_message_copyback_exact_url_gate",
        "click_x": x,
        "click_y": y,
        "clicked_latest": True,
    }


def close_existing_weixin_video_windows(*, timeout: int = 5) -> dict[str, Any]:
    script = (
        'tell application "System Events" to tell process "WeChat" to '
        'if exists window "微信 (窗口)" then click button 1 of window "微信 (窗口)"'
    )
    proc = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    payload: dict[str, Any] = {
        "attempted": True,
        "method": "ax_close_weixin_video_window",
        "returncode": proc.returncode,
    }
    if proc.stdout:
        payload["stdout_tail"] = proc.stdout[-1000:]
    if proc.stderr:
        payload["stderr_tail"] = proc.stderr[-1000:]
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:] or proc.stdout[-1000:] or "Failed to close existing Weixin video window.")
    return payload


def reopen_verified_filehelper_link(
    url: str,
    *,
    timeout: int = 20,
) -> dict[str, Any]:
    """Retry an already sent link after a network/VPN change without resending."""

    exact_window = raise_exact_filehelper_window(timeout=min(timeout, 8))
    if exact_window is None:
        raise RuntimeError(
            "Network retry requires the already verified File Transfer Assistant window; "
            "the link was not resent."
        )
    selection_payload = _protected_filehelper_payload(
        exact_window,
        source="network_retry_existing_exact_ax_window_title",
    )
    require_filehelper_target_verified(selection_payload)
    click_payload = verify_and_click_latest_filehelper_link(
        _window_geometry_from_payload(selection_payload),
        url,
        timeout=min(timeout, 12),
    )
    return {
        "method": "file_transfer_assistant_verified_message_retry",
        "opened": url,
        "sent_new_message": False,
        "reused_verified_message": True,
        **selection_payload,
        **click_payload,
    }


def open_weixin_filehelper(
    url: str,
    *,
    click_after_send: bool = True,
    timeout: int = 25,
) -> dict[str, Any]:
    if not url:
        raise RuntimeError("A Weixin URL is required for File Transfer Assistant opening.")

    # A retry may arrive after a VPN switch or interrupted conversion.  Reuse
    # the already-sent message only when the independent AX window title and
    # an exact clipboard copyback both prove that it is the same URL.  A
    # readable mismatch is the only condition that permits a new send; every
    # other verification failure remains fail-closed.
    exact_window = raise_exact_filehelper_window(timeout=min(timeout, 8))
    if exact_window is not None:
        existing_payload = _protected_filehelper_payload(
            exact_window,
            source="existing_exact_ax_window_title_before_send",
        )
        require_filehelper_target_verified(existing_payload)
        try:
            click_payload = verify_and_click_latest_filehelper_link(
                _window_geometry_from_payload(existing_payload),
                url,
                timeout=min(timeout, 12),
            )
        except FilehelperLatestMessageMismatch:
            pass
        else:
            return {
                "method": "file_transfer_assistant_verified_message_reuse",
                "opened": url,
                "sent_new_message": False,
                "reused_verified_message": True,
                "closed_existing_video_window": False,
                **existing_payload,
                **click_payload,
            }

    selection_payload = select_visible_filehelper(timeout=timeout)
    require_filehelper_target_verified(selection_payload)
    script = weixin_filehelper_applescript(click_after_send=click_after_send)
    command = ["osascript", "-e", script, url]
    proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    payload: dict[str, Any] = {
        "method": "file_transfer_assistant",
        "opened": url,
        "clicked_latest": False,
        "sent_new_message": True,
        "reused_verified_message": False,
        "returncode": proc.returncode,
        "closed_existing_video_window": False,
        **selection_payload,
    }
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stdout:
        try:
            decoded = json.loads(stdout.splitlines()[-1])
            if isinstance(decoded, dict):
                payload.update(decoded)
        except json.JSONDecodeError:
            payload["stdout_tail"] = stdout[-1000:]
    if stderr:
        payload["stderr_tail"] = stderr[-1000:]
    if proc.returncode != 0:
        raise RuntimeError(stderr[-1000:] or stdout[-1000:] or "WeChat File Transfer Assistant open failed.")
    require_filehelper_target_verified(payload)
    if click_after_send:
        click_payload = verify_and_click_latest_filehelper_link(
            _window_geometry_from_payload(payload),
            url,
            timeout=min(timeout, 12),
        )
        payload.update(click_payload)
    return payload
