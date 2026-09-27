#!/usr/bin/env python3
"""Local receiver for an authorized Weixin playback-page media-stream capture."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def js_payload(rate: float, max_seconds: float, media_index: int) -> str:
    return f"""
(async () => {{
  const ENDPOINT = "http://127.0.0.1:8798";
  const RATE = {rate};
  const MAX_SECONDS = {max_seconds};
  const MEDIA_INDEX = {media_index};
  const postJson = (path, payload) => fetch(ENDPOINT + path, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(payload)
  }}).catch(() => {{}});

  const media = Array.from(document.querySelectorAll("video,audio"));
  await postJson("/report", {{
    kind: "probe",
    href: location.href,
    title: document.title,
    mediaCount: media.length,
    media: media.map((m, i) => ({{
      i,
      tag: m.tagName,
      currentTime: m.currentTime,
      duration: m.duration,
      paused: m.paused,
      readyState: m.readyState,
      src: m.currentSrc || m.src || "",
      muted: m.muted,
      volume: m.volume
    }}))
  }});

  const target = media[MEDIA_INDEX] || media[0];
  if (!target) {{
    await postJson("/report", {{ kind: "error", message: "no media element found" }});
    return;
  }}
  const capture = target.captureStream || target.mozCaptureStream;
  if (!capture) {{
    await postJson("/report", {{ kind: "error", message: "captureStream unavailable" }});
    return;
  }}

  target.muted = false;
  target.volume = 1;
  target.playbackRate = RATE;
  try {{
    await target.play();
  }} catch (err) {{
    await postJson("/report", {{ kind: "play-error", message: String(err) }});
  }}

  const stream = capture.call(target);
  const mimeChoices = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
    "audio/webm;codecs=opus",
    "audio/webm"
  ];
  const mimeType = mimeChoices.find(t => MediaRecorder.isTypeSupported(t)) || "";
  const recorder = new MediaRecorder(stream, mimeType ? {{ mimeType }} : undefined);
  const chunks = [];
  recorder.ondataavailable = ev => {{
    if (ev.data && ev.data.size) chunks.push(ev.data);
  }};
  recorder.onerror = ev => postJson("/report", {{
    kind: "record-error",
    message: ev.error ? String(ev.error.message || ev.error) : "unknown"
  }});
  recorder.onstop = async () => {{
    const blob = new Blob(chunks, {{ type: recorder.mimeType || "video/webm" }});
    await postJson("/report", {{
      kind: "stopped",
      size: blob.size,
      mimeType: recorder.mimeType || "video/webm",
      rate: RATE,
      currentTime: target.currentTime,
      duration: target.duration
    }});
    await fetch(`${{ENDPOINT}}/upload?rate=${{encodeURIComponent(RATE)}}`, {{
      method: "POST",
      headers: {{ "Content-Type": recorder.mimeType || "video/webm" }},
      body: blob
    }}).catch(() => {{}});
  }};

  recorder.start(1000);
  await postJson("/report", {{
    kind: "started",
    mimeType: recorder.mimeType,
    rate: RATE,
    maxSeconds: MAX_SECONDS
  }});
  const stop = () => {{
    if (recorder.state !== "inactive") recorder.stop();
  }};
  target.addEventListener("ended", stop, {{ once: true }});
  setTimeout(stop, Math.max(1, MAX_SECONDS) * 1000);
}})();
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WeixinPageCapture/1.0"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/inject.js":
            self.send_error(404)
            return
        payload = js_payload(self.server.rate, self.server.max_seconds, self.server.media_index)
        data = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        if parsed.path == "/report":
            self.server.report_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                payload = {"kind": "raw-report", "bytes": len(body)}
            payload["received_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            with self.server.report_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            print(f"report: {payload.get('kind')} mediaCount={payload.get('mediaCount', '')}", flush=True)
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/upload":
            self.server.raw_path.parent.mkdir(parents=True, exist_ok=True)
            self.server.raw_path.write_bytes(body)
            print(f"uploaded raw capture: {self.server.raw_path} ({len(body)} bytes)", flush=True)
            self.send_response(204)
            self.end_headers()
            self.convert_upload()
            return
        self.send_error(404)

    def convert_upload(self) -> None:
        recover = ROOT / "outputs" / "capture_accelerator" / "recover_audio.py"
        cmd = [
            sys.executable,
            str(recover),
            str(self.server.raw_path),
            "--speed",
            str(self.server.rate),
            "--output",
            str(self.server.output_path),
        ]
        print("+ " + " ".join(cmd), flush=True)
        subprocess.run(cmd, check=False)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--rate", type=float, default=12)
    parser.add_argument("--max-seconds", type=float, default=600)
    parser.add_argument("--media-index", type=int, default=0)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "weixin_video_channel.mp3"))
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.rate = args.rate
    server.max_seconds = args.max_seconds
    server.media_index = args.media_index
    server.raw_path = ROOT / "work" / "weixin-page-captures" / f"weixin_speed{args.rate:g}_{stamp}.webm"
    server.report_path = ROOT / "work" / "weixin-page-captures" / f"weixin_capture_{stamp}.jsonl"
    server.output_path = Path(args.output).expanduser().resolve()
    print(f"Serving inject script at http://{args.host}:{args.port}/inject.js", flush=True)
    print(f"Raw capture will be saved to {server.raw_path}", flush=True)
    print(f"Recovered MP3 target is {server.output_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
