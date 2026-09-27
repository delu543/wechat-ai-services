#!/usr/bin/env python3
"""Capture Songy test-browser artifacts and try converting course 784 to MP3.

With --fast-record, fall back to accelerated HTMLMediaElement recording when
no direct media URL or token route is captured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "https://webapp.songy.info/#/courses/details?course_id=784"
MEDIA_EXTS = (".m3u8", ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".mp4", ".webm")
INTERESTING_URL_PARTS = (
    "/v2/courses/784",
    "/v2/courses/784/contents",
    "raw_url",
    "m3u8",
    "mp4",
    "m4a",
    "mp3",
)


def is_media_content_type(content_type: str) -> bool:
    lower = content_type.lower()
    return lower.startswith(("audio/", "video/")) or "mpegurl" in lower or "octet-stream" in lower


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "cookie", "set-cookie"}:
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def is_media_url(url: str) -> bool:
    lower = url.lower().split("?", 1)[0]
    return lower.endswith(MEDIA_EXTS) or any(ext in url.lower() for ext in (".m3u8", ".mp4"))


def is_interesting(url: str) -> bool:
    lower = url.lower()
    return is_media_url(url) or any(part.lower() in lower for part in INTERESTING_URL_PARTS)


async def storage_snapshot(page: Any) -> dict[str, Any]:
    return await page.evaluate(
        """async () => {
          const keep = /(token|auth|login|user|account|bandu|songy|course|784)/i;
          const media = /(raw_url|download|media|m3u8|mp4|m4a|mp3|aac|opus|audio|video|784)/i;
          const limit = 80;

          function safeJson(value) {
            try {
              if (value === undefined) return null;
              if (value === null) return null;
              if (typeof value === "string") return value;
              return JSON.parse(JSON.stringify(value));
            } catch (_) {
              return String(value);
            }
          }

          function shouldKeep(key, value) {
            const text = `${key || ""} ${typeof value === "string" ? value : JSON.stringify(value || "")}`;
            return keep.test(text) || media.test(text);
          }

          function collect(storage) {
            const out = {};
            for (let i = 0; i < storage.length; i += 1) {
              const key = storage.key(i);
              const value = storage.getItem(key);
              if (key && value && shouldKeep(key, value)) out[key] = value;
            }
            return out;
          }

          async function collectIndexedDB() {
            const out = [];
            if (!("indexedDB" in window) || !indexedDB.databases) return out;
            const dbs = await indexedDB.databases().catch(() => []);
            for (const dbInfo of dbs || []) {
              if (!dbInfo.name) continue;
              const record = { name: dbInfo.name, version: dbInfo.version, stores: [] };
              const db = await new Promise(resolve => {
                const req = indexedDB.open(dbInfo.name);
                req.onerror = () => resolve(null);
                req.onsuccess = () => resolve(req.result);
              });
              if (!db) continue;
              try {
                for (const storeName of Array.from(db.objectStoreNames || [])) {
                  const tx = db.transaction(storeName, "readonly");
                  const store = tx.objectStore(storeName);
                  const values = await new Promise(resolve => {
                    const req = store.getAll ? store.getAll() : null;
                    if (!req) return resolve([]);
                    req.onerror = () => resolve([]);
                    req.onsuccess = () => resolve(req.result || []);
                  });
                  const kept = [];
                  for (const value of values || []) {
                    if (kept.length >= limit) break;
                    if (shouldKeep(storeName, value)) kept.push(safeJson(value));
                  }
                  if (kept.length) record.stores.push({ name: storeName, records: kept });
                }
              } catch (err) {
                record.error = String(err);
              } finally {
                db.close();
              }
              if (record.stores.length || shouldKeep(record.name, "")) out.push(record);
            }
            return out;
          }

          async function collectCaches() {
            const out = [];
            if (!("caches" in window)) return out;
            const names = await caches.keys().catch(() => []);
            for (const name of names || []) {
              const cache = await caches.open(name).catch(() => null);
              if (!cache) continue;
              const requests = await cache.keys().catch(() => []);
              const entries = [];
              for (const request of requests || []) {
                if (entries.length >= limit) break;
                const response = await cache.match(request).catch(() => null);
                const contentType = response ? (response.headers.get("content-type") || "") : "";
                const row = { url: request.url, content_type: contentType, status: response ? response.status : null };
                if (shouldKeep(request.url, contentType)) {
                  if (/json|text|javascript|mpegurl|vnd\\.apple\\.mpegurl/i.test(contentType)) {
                    row.body = await response.clone().text().catch(() => "");
                    if (row.body.length > 200000) row.body = row.body.slice(0, 200000);
                  }
                  entries.push(row);
                }
              }
              if (entries.length) out.push({ name, entries });
            }
            return out;
          }

          return {
            localStorage: collect(localStorage),
            sessionStorage: collect(sessionStorage),
            indexedDB: await collectIndexedDB(),
            cacheStorage: await collectCaches()
          };
        }"""
    )


async def launch_context(playwright: Any, profile_dir: Path, headless: bool, mobile: bool) -> Any:
    options = {
        "headless": headless,
        "args": ["--autoplay-policy=no-user-gesture-required"],
    }
    if mobile:
        options.update(
            {
                "viewport": {"width": 390, "height": 844},
                "device_scale_factor": 3,
                "is_mobile": True,
                "has_touch": True,
                "user_agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                    "Mobile/15E148 Safari/604.1"
                ),
            }
        )
    try:
        return await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            **options,
        )
    except Exception as exc:
        print(f"Chrome channel unavailable, falling back to bundled Chromium: {exc}")
        return await playwright.chromium.launch_persistent_context(str(profile_dir), **options)


async def main_async(args: argparse.Namespace) -> int:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise SystemExit("Playwright is not installed. Run with work/venv/bin/python if needed.") from exc

    artifact_path = Path(args.artifact).expanduser().resolve()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Any] = {
        "source": "songy_browser_capture.py",
        "url": args.url,
        "generated_at": int(time.time()),
        "boundary": "Use only with an authorized Songy company/test account.",
        "media_urls": [],
        "responses": [],
        "storage": {},
    }
    seen_media: set[str] = set()

    async with async_playwright() as p:
        context = await launch_context(p, profile_dir, args.headless, args.mobile)
        page = context.pages[0] if context.pages else await context.new_page()

        async def on_response(response: Any) -> None:
            url = response.url
            headers = safe_headers(await response.all_headers())
            content_type = headers.get("content-type", "")
            media_response = is_media_url(url) or is_media_content_type(content_type)
            if media_response and url not in seen_media:
                seen_media.add(url)
                captured["media_urls"].append(url)
            if not media_response and not is_interesting(url):
                return
            entry = {
                "url": url,
                "status": response.status,
                "headers": headers,
            }
            try:
                content_type = entry["headers"].get("content-type", "")
                if "json" in content_type or "/v2/courses/784" in url:
                    entry["body"] = await response.text()
            except Exception as exc:
                entry["body_error"] = str(exc)
            captured["responses"].append(entry)

        page.on("response", on_response)
        await page.goto(args.url, wait_until="domcontentloaded")
        print(f"Opened: {args.url}")
        print(f"Profile: {profile_dir}")
        print(f"Artifact: {artifact_path}")
        print("Use an authorized Songy test account, then open/play course 784 if needed.")

        deadline = time.time() + args.wait_seconds
        while time.time() < deadline:
            captured["storage"] = await storage_snapshot(page)
            artifact_path.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
            if captured["media_urls"] and args.stop_on_media:
                break
            await asyncio.sleep(args.poll_interval)

        captured["storage"] = await storage_snapshot(page)
        artifact_path.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        await context.close()

    print(f"Wrote artifact: {artifact_path}")
    if args.no_convert:
        return 0

    converter = Path(__file__).with_name("songy_artifact_to_mp3.py")
    result = subprocess.run(
        [sys.executable, str(converter), str(artifact_path), "--output", str(output)],
        text=True,
    )
    if result.returncode == 0:
        print(f"Created: {output}")
        return 0
    if args.fast_record:
        fast_capture = ROOT / "outputs" / "capture_accelerator" / "web_fast_capture.py"
        raw_output = (
            Path(args.raw_output).expanduser().resolve()
            if args.raw_output
            else output.with_suffix(".fast.webm")
        )
        cmd = [
            sys.executable,
            str(fast_capture),
            args.url,
            "--rate",
            str(args.rate),
            "--output",
            str(output),
            "--profile-dir",
            str(profile_dir),
            "--media-index",
            str(args.media_index),
            "--pitch",
            args.pitch,
            "--raw-output",
            str(raw_output),
        ]
        if args.max_wall_seconds:
            cmd.extend(["--max-wall-seconds", str(args.max_wall_seconds)])
        if args.headless:
            cmd.append("--headless")
        if args.mobile:
            cmd.append("--mobile")
        print("Direct artifact conversion failed; trying fast browser recording fallback.")
        return subprocess.run(cmd).returncode
    print("No MP3 created from captured artifact yet. Keep the artifact and retry after playback/login.")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--artifact", default=str(ROOT / "incoming" / "songy_browser_capture.json"))
    parser.add_argument("--output", default=str(ROOT / "outputs" / "songy_course_784.mp3"))
    parser.add_argument("--profile-dir", default=str(ROOT / "work" / "songy-browser-profile"))
    parser.add_argument("--wait-seconds", type=float, default=180)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--stop-on-media", action="store_true")
    parser.add_argument("--no-convert", action="store_true")
    parser.add_argument("--fast-record", action="store_true", help="Fallback to fast browser recording if artifact conversion fails")
    parser.add_argument("--rate", type=float, default=12.0, help="Playback rate for --fast-record")
    parser.add_argument("--max-wall-seconds", type=float, default=0, help="Stop fast recording after this many real seconds")
    parser.add_argument("--media-index", type=int, default=-1, help="Media element index for --fast-record")
    parser.add_argument("--pitch", choices=["preserved", "chipmunked"], default="preserved")
    parser.add_argument("--raw-output", default="", help="Raw WebM output for --fast-record")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--mobile", action="store_true", help="Use an iPhone-like browser profile")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
