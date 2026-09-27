#!/usr/bin/env python3
"""Receive authorized artifacts/recordings from phones or test devices."""

from __future__ import annotations

import argparse
import cgi
import html
import http.server
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INCOMING = ROOT / "incoming"
DEFAULT_OUTPUTS = ROOT / "outputs"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._@+=\\-\u4e00-\u9fff]+")


def local_addresses(port: int) -> list[str]:
    addresses = [f"http://127.0.0.1:{port}/"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and ip != "127.0.0.1":
                addresses.append(f"http://{ip}:{port}/")
    except OSError:
        pass
    return addresses


def sanitize_name(name: str, fallback: str) -> str:
    cleaned = SAFE_NAME_RE.sub("_", Path(name).name).strip("._")
    return cleaned or fallback


def with_target_hint(filename: str, target: str, speed: str) -> str:
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    lower = filename.lower()
    if target == "weixin" and not any(hint in lower for hint in ("weixin", "wechat", "wx", "sph", "视频号")):
        stem = "weixin_" + stem
    if target == "songy" and not any(hint in lower for hint in ("songy", "bandu", "784", "松一")):
        stem = "songy_" + stem
    if speed:
        speed_clean = re.sub(r"[^0-9.]", "", speed)
        if speed_clean and f"{speed_clean}x" not in stem.lower():
            stem = f"{stem}_speed{speed_clean}x"
    return stem + suffix


def unique_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = target.stem
    suffix = target.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}_{stamp}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique filename for {filename}")


def field_value(form: cgi.FieldStorage, name: str, default: str = "") -> str:
    item = form[name] if name in form else None
    if item is None or isinstance(item, list):
        return default
    value = item.value
    return value if isinstance(value, str) else default


def form_checked(form: cgi.FieldStorage, name: str) -> bool:
    return field_value(form, name).lower() in {"1", "true", "on", "yes"}


def write_upload(item: cgi.FieldStorage, incoming: Path, target: str, speed: str) -> Path | None:
    if not item.filename:
        return None
    filename = sanitize_name(item.filename, f"upload_{int(time.time())}.bin")
    filename = with_target_hint(filename, target, speed)
    path = unique_path(incoming, filename)
    with path.open("wb") as handle:
        while True:
            chunk = item.file.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    return path


def write_text_artifact(text: str, incoming: Path, target: str, extension: str) -> Path | None:
    if not text.strip():
        return None
    ext = extension if extension.startswith(".") else "." + extension
    if ext.lower() not in {".json", ".har", ".txt", ".log", ".html", ".xml"}:
        ext = ".txt"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    prefix = target if target in {"weixin", "songy"} else "artifact"
    path = unique_path(incoming, f"{prefix}_pasted_{stamp}{ext}")
    path.write_text(text, encoding="utf-8")
    return path


def process_incoming(incoming: Path, outputs: Path) -> tuple[int, str]:
    script = Path(__file__).with_name("process_incoming.py")
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--incoming",
            str(incoming),
            "--outputs",
            str(outputs),
            "--continue-on-error",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.returncode, result.stdout


def html_page(addresses: list[str], incoming: Path, outputs: Path, message: str = "") -> bytes:
    address_list = "".join(f"<li><code>{html.escape(url)}</code></li>" for url in addresses)
    body = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MP3 Authorized Receiver</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 860px; margin: 28px auto; line-height: 1.5; padding: 0 16px; }}
label {{ display: block; margin: 14px 0 6px; font-weight: 600; }}
input, select, textarea, button {{ font: inherit; max-width: 100%; }}
textarea {{ width: 100%; min-height: 160px; box-sizing: border-box; }}
.note {{ background: #f6f6f6; padding: 12px; border-radius: 6px; }}
.message {{ white-space: pre-wrap; border: 1px solid #ddd; padding: 12px; border-radius: 6px; }}
</style>
<h1>MP3 Authorized Receiver</h1>
<p class="note">只用于你明确授权的测试账号、测试设备、录屏、HAR/JSON、桥响应或媒体文件。不要上传个人微信 cookie、个人证书抓包材料或无授权内容。</p>
<p>可访问地址：</p>
<ul>{address_list}</ul>
<p>Incoming: <code>{html.escape(str(incoming))}</code><br>Outputs: <code>{html.escape(str(outputs))}</code></p>
{f'<h2>Result</h2><div class="message">{html.escape(message)}</div>' if message else ''}
<form method="post" enctype="multipart/form-data">
  <label>目标</label>
  <select name="target">
    <option value="weixin">微信视频号</option>
    <option value="songy">Songy course 784</option>
    <option value="auto">按文件名自动识别</option>
  </select>
  <label>录制倍速，可空，例如 2 或 3</label>
  <input name="speed" inputmode="decimal" placeholder="仅倍速录屏/录音需要">
  <label>上传文件，可多选</label>
  <input type="file" name="files" multiple>
  <label>或粘贴 HAR/JSON/XML/HTML/URL 文本</label>
  <textarea name="artifact_text" placeholder="粘贴授权响应、Songy artifact、Weixin bridge response、视频号助手源码等"></textarea>
  <label>粘贴文本扩展名</label>
  <select name="artifact_ext">
    <option value=".json">.json</option>
    <option value=".har">.har</option>
    <option value=".txt">.txt</option>
    <option value=".html">.html</option>
    <option value=".xml">.xml</option>
  </select>
  <p><label><input type="checkbox" name="process" checked> 上传后立即尝试转换</label></p>
  <button type="submit">Upload and process</button>
</form>
</html>
"""
    return body.encode("utf-8")


class Receiver(http.server.BaseHTTPRequestHandler):
    server: Any

    def do_GET(self) -> None:  # noqa: N802
        self.send_html(html_page(self.server.addresses, self.server.incoming, self.server.outputs))

    def do_POST(self) -> None:  # noqa: N802
        ctype, pdict = cgi.parse_header(self.headers.get("content-type", ""))
        if ctype != "multipart/form-data":
            self.send_html(html_page(self.server.addresses, self.server.incoming, self.server.outputs, "Unsupported form type"), 400)
            return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        target = field_value(form, "target", "auto")
        if target not in {"weixin", "songy", "auto"}:
            target = "auto"
        speed = field_value(form, "speed", "")
        saved: list[Path] = []
        files = form["files"] if "files" in form else []
        if not isinstance(files, list):
            files = [files]
        for item in files:
            path = write_upload(item, self.server.incoming, target, speed)
            if path:
                saved.append(path)
        text_path = write_text_artifact(
            field_value(form, "artifact_text", ""),
            self.server.incoming,
            target,
            field_value(form, "artifact_ext", ".json"),
        )
        if text_path:
            saved.append(text_path)
        lines = [f"Saved {len(saved)} item(s):", *(str(path) for path in saved)]
        if saved and (self.server.auto_process or form_checked(form, "process")):
            code, output = process_incoming(self.server.incoming, self.server.outputs)
            lines.extend(["", f"process_incoming exit={code}", output])
        self.send_html(html_page(self.server.addresses, self.server.incoming, self.server.outputs, "\n".join(lines)))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_html(self, content: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 only on a trusted LAN")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--incoming", default=str(DEFAULT_INCOMING))
    parser.add_argument("--outputs", default=str(DEFAULT_OUTPUTS))
    parser.add_argument("--auto-process", action="store_true")
    args = parser.parse_args()

    incoming = Path(args.incoming).expanduser().resolve()
    outputs = Path(args.outputs).expanduser().resolve()
    incoming.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), Receiver)
    httpd.incoming = incoming
    httpd.outputs = outputs
    httpd.auto_process = args.auto_process
    httpd.addresses = local_addresses(args.port)
    print("Receiver running. Use Ctrl-C to stop.")
    for url in httpd.addresses:
        print(url)
    print(f"Incoming: {incoming}")
    print(f"Outputs: {outputs}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nReceiver stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
