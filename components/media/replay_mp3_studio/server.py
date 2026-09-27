from __future__ import annotations

import argparse
import html
import json
import mimetypes
import platform
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Union
from urllib.parse import parse_qs, quote_plus, quote, urlparse

from .config import PROJECT_ROOT, STATIC_ROOT, WORK_ROOT, ensure_layout
from .extractors import generate_weixin_open_packet, list_blackbox_audio_devices
from .jobs import JobStore, state_payload
from .speed_control import speed_snippet_payload
from .utils import parse_weixin_short_uri, slugify, timestamp_slug
from .weixin_runtime_capture import runtime_capture_snippet


def json_bytes(payload: object, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8"


def content_disposition_for_file(path: Path, download: bool = False) -> Optional[str]:
    if not download:
        return None
    filename = path.name.replace("\\", "").replace('"', "")
    if filename.isascii():
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=UTF-8''{quote(filename)}"


def reveal_path_in_finder(path: Path) -> None:
    if platform.system() == "Windows":
        subprocess.run(["explorer.exe", f"/select,{path}"], check=True)
        return
    subprocess.run(["open", "-R", str(path)], check=True)


def local_lan_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        _name, _aliases, host_addresses = socket.gethostbyname_ex(socket.gethostname())
        addresses.update(host_addresses)
    except OSError:
        pass
    return sorted(
        address
        for address in addresses
        if address
        and not address.startswith("127.")
        and not address.startswith("169.254.")
        and address != "0.0.0.0"
    )


def split_host_port(host_header: str, fallback_port: int) -> tuple[str, int]:
    parsed = urlparse(f"//{host_header}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or fallback_port
    return host, port


def bridge_host_candidates(
    host_header: str,
    fallback_port: int,
    lan_addresses: list[str] | None = None,
) -> list[dict[str, str]]:
    host, port = split_host_port(host_header, fallback_port)
    raw_candidates = [("current", host)]
    lan_source = lan_addresses if lan_addresses is not None else local_lan_addresses()
    if host in {"127.0.0.1", "localhost", "0.0.0.0"} or host.startswith("127."):
        raw_candidates.extend(("lan", address) for address in lan_source)

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for kind, address in raw_candidates:
        if not address:
            continue
        netloc = f"{address}:{port}"
        if netloc in seen:
            continue
        seen.add(netloc)
        candidates.append(
            {
                "kind": kind,
                "host": netloc,
                "base_url": f"http://{netloc}",
                "label": "局域网地址" if kind == "lan" else "当前地址",
            }
        )
    return candidates


def bridge_launcher_manifest(
    host_header: str,
    fallback_port: int,
    query: str,
    lan_addresses: list[str] | None = None,
) -> dict[str, object]:
    normalized_query = query.lstrip("?")
    if "autorun=" not in normalized_query:
        normalized_query = f"autorun=1&{normalized_query}" if normalized_query else "autorun=1"
    snippet_query = normalized_query.replace("autorun=1&", "").replace("autorun=1", "").strip("&")
    candidates = []
    for host in bridge_host_candidates(host_header, fallback_port, lan_addresses=lan_addresses):
        base_url = host["base_url"]
        page_url = f"{base_url}/weixin-bridge-autopost?{normalized_query}"
        snippet_url = f"{base_url}/api/weixin/bridge-autopost-snippet?{snippet_query}" if snippet_query else f"{base_url}/api/weixin/bridge-autopost-snippet"
        runtime_snippet_url = f"{base_url}/api/weixin/runtime-capture-snippet"
        candidates.append(
            {
                **host,
                "bridge_page_url": page_url,
                "bridge_snippet_url": snippet_url,
                "runtime_capture_snippet_url": runtime_snippet_url,
                "receive_artifact_url": f"{base_url}/api/receive-artifact",
                "qr_url": "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data="
                + quote_plus(page_url),
            }
        )
    return {
        "query": normalized_query,
        "snippet_query": snippet_query,
        "candidates": candidates,
        "notes": [
            "在授权的视频号播放页或微信 WebView 测试环境中使用。",
            "如果 127.0.0.1 在微信内置浏览器不可达，尝试局域网地址。",
            "Bridge 响应只回传到本机 Studio 的 /api/receive-artifact。",
        ],
    }


def bridge_launcher_html(manifest: dict[str, object]) -> bytes:
    candidates = manifest.get("candidates") if isinstance(manifest.get("candidates"), list) else []
    rows = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        label = html.escape(str(item.get("label") or item.get("kind") or "地址"))
        host = html.escape(str(item.get("host") or ""))
        page_url = html.escape(str(item.get("bridge_page_url") or ""))
        snippet_url = html.escape(str(item.get("bridge_snippet_url") or ""))
        runtime_snippet_url = html.escape(str(item.get("runtime_capture_snippet_url") or ""))
        qr_url = html.escape(str(item.get("qr_url") or ""))
        rows.append(
            f"""
<section>
  <h2>{label} <small>{host}</small></h2>
  <p><a href="{page_url}" target="_blank" rel="noreferrer">打开 Bridge 页面</a></p>
  <p><a href="{runtime_snippet_url}" target="_blank" rel="noreferrer">打开 Runtime Capture JS</a></p>
  <p><a href="{snippet_url}" target="_blank" rel="noreferrer">打开 Bridge JS</a></p>
  <input readonly value="{page_url}" onclick="this.select()">
  <img alt="Bridge page QR" src="{qr_url}">
</section>
"""
        )
    notes = "".join(f"<li>{html.escape(str(note))}</li>" for note in manifest.get("notes", []))
    body = "\n".join(rows) or "<p>没有可用 Bridge 地址。</p>"
    page = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>Weixin Bridge Launcher</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:880px;margin:24px auto;line-height:1.5}}
section{{border:1px solid #ddd;border-radius:8px;padding:16px;margin:14px 0}}
small{{color:#666;font-weight:400}}
input{{box-sizing:border-box;width:100%;padding:8px;border:1px solid #bbb;border-radius:6px;font:12px ui-monospace,Menlo,monospace}}
img{{display:block;margin-top:12px;width:160px;height:160px}}
a{{color:#1677ff}}
</style>
<h1>Weixin Bridge Launcher</h1>
<p>用于把授权播放页里的媒体响应回传到本机 Studio。优先用当前地址；如果微信内置浏览器无法访问本机环回地址，再用局域网地址。</p>
<ul>{notes}</ul>
{body}
</html>
"""
    return page.encode("utf-8")


def bridge_autopost_js(endpoint: str, default_eid: str = "", noprompt: bool = False) -> bytes:
    endpoint_json = json.dumps(endpoint)
    default_eid_json = json.dumps(default_eid)
    noprompt_json = json.dumps(noprompt)
    script = f"""
(async function () {{
  const STUDIO_ENDPOINT = {endpoint_json};
  const DEFAULT_EID = {default_eid_json};
  const NO_PROMPT = {noprompt_json};
  const warn = "Only use on authorized WeChat test/creator playback pages. Stop if this is not authorized.";
  const eid = NO_PROMPT ? DEFAULT_EID : prompt(warn + "\\n\\nPaste encrypted_objectid / dynamicExportId:", new URL(location.href).searchParams.get("eid") || DEFAULT_EID || "");
  if (!eid) return;
  const out = document.createElement("textarea");
  out.style.cssText = "position:fixed;z-index:2147483647;left:8px;right:8px;top:8px;width:calc(100% - 16px);height:55vh;background:#111;color:#0f0;font:12px monospace";
  document.body.appendChild(out);
  const log = data => {{ out.value = typeof data === "string" ? data : JSON.stringify(data, null, 2); }};
  const postArtifact = async payload => {{
    const body = {{
      platform: "weixin",
      url: location.href,
      artifact_ext: ".json",
      artifact_text: JSON.stringify(payload)
    }};
    const resp = await fetch(STUDIO_ENDPOINT, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify(body)
    }});
    const text = await resp.text();
    return {{ status: resp.status, body: text }};
  }};
  const currentShortUri = () => {{
    try {{
      const u = new URL(location.href);
      const id = u.searchParams.get("id") || u.searchParams.get("short_uri") || "";
      if (id) return id;
      const match = u.pathname.match(/\\/sph\\/([A-Za-z0-9_-]+)/);
      return match && match[1] ? match[1] : "";
    }} catch (_) {{
      return "";
    }}
  }};
  const cleanMediaUrl = raw => {{
    const value = raw ? String(raw).trim() : "";
    if (!value) return "";
    try {{
      const parsed = new URL(value, location.origin);
      const filekey = (parsed.searchParams.get("encfilekey") || "").trim();
      const token = (parsed.searchParams.get("token") || "").trim();
      if (!filekey || !token) return value;
      const cleaned = new URL(parsed.origin + parsed.pathname);
      cleaned.searchParams.set("encfilekey", filekey);
      cleaned.searchParams.set("token", token);
      return cleaned.toString();
    }} catch (_) {{
      return value;
    }}
  }};
  const mediaUrlsFrom = value => {{
    const urls = [];
    const seen = new Set();
    const add = raw => {{
      const cleaned = cleanMediaUrl(raw);
      const lower = cleaned.toLowerCase();
      if (!cleaned || seen.has(cleaned)) return;
      if (
        /\\.(m3u8|mp4|m4a|mp3|aac|webm)(\\?|$)/i.test(cleaned) ||
        lower.includes("stodownload") ||
        lower.includes("snsvideodownload") ||
        lower.includes("snscosdownload")
      ) {{
        seen.add(cleaned);
        urls.push(cleaned);
      }}
    }};
    const walk = item => {{
      if (!item) return;
      if (typeof item === "string") {{
        add(item);
        return;
      }}
      if (Array.isArray(item)) {{
        item.forEach(walk);
        return;
      }}
      if (typeof item === "object") {{
        Object.keys(item).forEach(key => {{
          const lowerKey = key.toLowerCase();
          const child = item[key];
          if (typeof child === "string" && /(url|media|video|audio|hls|raw)/.test(lowerKey)) add(child);
          walk(child);
        }});
      }}
    }};
    walk(value);
    return urls;
  }};
  const feedApiCandidates = () => {{
    const urls = ["/finder-preview/api/feed/get_feed_info"];
    if (location.origin !== "https://channels.weixin.qq.com") {{
      urls.push("https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info");
    }}
    return urls;
  }};
  const fetchSharedFeedInfo = async req => {{
    let lastErr = null;
    for (const url of feedApiCandidates()) {{
      try {{
        const resp = await fetch(url, {{
          method: "POST",
          credentials: "include",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(req)
        }});
        const text = await resp.text();
        let data = null;
        try {{ data = JSON.parse(text); }} catch (_) {{ data = {{ raw: text }}; }}
        return {{ ok: resp.ok, status: resp.status, url, data }};
      }} catch (err) {{
        lastErr = err;
      }}
    }}
    throw lastErr || new Error("feed api unavailable");
  }};
  const tryDirectFeedPost = async () => {{
    const shortUri = currentShortUri();
    const requests = [];
    if (shortUri) requests.push({{ baseReq: {{ generalToken: "" }}, shortUri }});
    if (DEFAULT_EID) requests.push({{ baseReq: {{ generalToken: "" }}, exportId: DEFAULT_EID }});
    for (const req of requests) {{
      const feed = await fetchSharedFeedInfo(req);
      const urls = mediaUrlsFrom(feed.data);
      if (!urls.length) continue;
      const payload = {{
        source: "weixin_bridge_feed_info",
        page: location.href,
        shortUri,
        request: Object.keys(req).filter(k => k !== "baseReq"),
        feed,
        media_urls: urls
      }};
      log({{ status: "posting-direct-feed-to-local-studio", media_url_count: urls.length, payload }});
      const posted = await postArtifact(payload);
      log({{ status: "posted-direct-feed-to-local-studio", posted, media_url_count: urls.length, payload }});
      return true;
    }}
    return false;
  }};
  const requestId = () => String(Date.now()) + String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const invoke = (name, params) => new Promise((resolve, reject) => {{
    if (!window.WeixinJSBridge || !window.WeixinJSBridge.invoke) return reject(new Error("WeixinJSBridge.invoke unavailable"));
    window.WeixinJSBridge.invoke(name, params, resolve);
  }});
  const parseTransfer = resp => {{
    const raw = resp && resp.jsapi_resp && resp.jsapi_resp.resp_json;
    if (!raw) return resp;
    try {{ return JSON.parse(raw); }} catch (_) {{ return resp; }}
  }};
  const transfer = (url, cmdid, req, token) => invoke("finderH5ExtTransfer", {{
    req_json: JSON.stringify(req),
    url,
    cgi_cmdid: cmdid,
    h5AuthToken: token || "",
    is_security_check: false,
    scope: "finderLive"
  }}).then(parseTransfer);
  const liveIdFrom = detail => detail && detail.object && detail.object.liveInfo && detail.object.liveInfo.liveId ||
    detail && detail.data && detail.data.object && detail.data.object.liveInfo && detail.data.object.liveInfo.liveId || "";
  const replayFrom = info => {{
    const liveInfo = info && info.liveInfo || info && info.data && info.data.liveInfo || {{}};
    const replay = liveInfo.replayInfo || {{}};
    return {{ renderReplayHlsUrl: replay.renderReplayHlsUrl || "", renderReplayUrl: replay.renderReplayUrl || "" }};
  }};
  let auth = null;
  let detail = null;
  let liveInfo = null;
  let liveId = "";
  try {{
    log("Trying finder-preview feed API in current page context...");
    if (await tryDirectFeedPost()) return;
    log("Running finderH5Auth...");
    auth = await invoke("finderH5Auth", {{ h5Version: 3774873601, scope: "finderLive" }});
    const token = auth && auth.h5AuthToken || "";
    log("Running FinderGetCommentDetail...");
    detail = await transfer("/cgi-bin/micromsg-bin/pc_findergetcommentdetail", 5259, {{
      finder_basereq: {{ expt_flag: 1, request_id: requestId() }},
      platform_scene: 2,
      encrypted_objectid: eid,
      need_object: 1,
      scene: 141,
      direction: 2,
      identity_scene: 2,
      pull_scene: 1
    }}, token);
    liveId = liveIdFrom(detail);
    if (!liveId) throw new Error("No liveId in FinderGetCommentDetail response");
    log("Running FinderGetLiveInfo...");
    liveInfo = await transfer("/cgi-bin/micromsg-bin/pc_findergetliveinfo", 10064, {{ finder_basereq: {{}}, live_id: liveId }}, token);
    const payload = {{ source: "weixin_bridge_autopost", page: location.href, auth: {{ hasToken: !!token }}, liveId, detail, liveInfo, replay: replayFrom(liveInfo) }};
    log({{ status: "posting-to-local-studio", payload }});
    const posted = await postArtifact(payload);
    log({{ status: "posted-to-local-studio", posted, payload }});
  }} catch (err) {{
    const payload = {{ source: "weixin_bridge_autopost", page: location.href, error: err && err.message ? err.message : String(err), auth: auth ? {{ hasToken: !!auth.h5AuthToken, keys: Object.keys(auth) }} : null, liveId, detail, liveInfo }};
    try {{
      const posted = await postArtifact(payload);
      log({{ status: "posted-error-to-local-studio", posted, payload }});
    }} catch (postErr) {{
      log({{ error: payload.error, postError: postErr && postErr.message ? postErr.message : String(postErr) }});
    }}
  }}
}})();
"""
    return script.encode("utf-8")


def refreshed_weixin_export_id(short_uri: str) -> str:
    safe_short_uri = parse_weixin_short_uri(short_uri)
    refresh_dir = (
        WORK_ROOT
        / "sensitive-artifacts"
        / "weixin-bridge-refresh"
        / f"{timestamp_slug()}-{slugify(safe_short_uri, 'weixin')}"
    )
    packet_info = generate_weixin_open_packet(
        f"https://weixin.qq.com/sph/{safe_short_uri}",
        refresh_dir,
        lambda _message: None,
    )
    packet = packet_info.get("packet") or {}
    scene = packet.get("scene_info") if isinstance(packet, dict) else {}
    export_id = scene.get("dynamicExportId") if isinstance(scene, dict) else ""
    if not export_id:
        raise RuntimeError("refreshed Weixin packet did not include dynamicExportId")
    return str(export_id)


def bridge_autopost_html(endpoint: str, default_eid: str = "", noprompt: bool = False) -> bytes:
    script = bridge_autopost_js(endpoint, default_eid, noprompt=noprompt).decode("utf-8")
    html = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>Weixin Bridge Autopost</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:760px;margin:24px auto;line-height:1.5}}
button{{font:inherit;border:0;border-radius:6px;padding:10px 14px;background:#1677ff;color:#fff}}
pre{{white-space:pre-wrap;background:#111;color:#0f0;padding:12px;border-radius:6px;min-height:160px}}
</style>
<h1>Weixin Bridge Autopost</h1>
<p>Only use on authorized WeChat test/creator playback pages.</p>
<button id="run">Run</button>
<pre id="log">Ready.</pre>
<script>
document.getElementById("run").onclick = () => {{
  const s = document.createElement("script");
  s.textContent = {json.dumps(script)};
  document.body.appendChild(s);
}};
if (new URL(location.href).searchParams.get("autorun") === "1") {{
  document.getElementById("run").click();
}}
</script>
</html>
"""
    return html.encode("utf-8")


class StudioHandler(BaseHTTPRequestHandler):
    store: JobStore

    def log_message(self, fmt: str, *args) -> None:
        print(f"[studio] {self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        try:
            routed = self.route_get()
        except Exception as exc:
            routed = json_bytes({"error": str(exc)}, 500)
        status, body, content_type, headers = self.normalize_response(routed)
        self.respond(status, body, content_type, headers=headers)

    def do_POST(self) -> None:  # noqa: N802
        try:
            routed = self.route_post()
        except Exception as exc:
            routed = json_bytes({"error": str(exc)}, 500)
        status, body, content_type, headers = self.normalize_response(routed)
        self.respond(status, body, content_type, headers=headers)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.respond(204, b"", "text/plain; charset=utf-8")

    def normalize_response(
        self,
        routed: Union[tuple[int, bytes, str], tuple[int, bytes, str, dict[str, str]]],
    ) -> tuple[int, bytes, str, dict[str, str]]:
        if len(routed) == 4:
            status, body, content_type, headers = routed
            return status, body, content_type, headers
        status, body, content_type = routed
        return status, body, content_type, {}

    def route_get(self) -> tuple[int, bytes, str]:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.static_file("index.html")
        if path.startswith("/static/"):
            return self.static_file(path.removeprefix("/static/"))
        if path == "/api/state":
            return json_bytes(state_payload(self.store))
        if path == "/api/audio-devices":
            return json_bytes(list_blackbox_audio_devices())
        if path == "/api/speed-snippet":
            query = parse_qs(parsed.query)
            speed = query.get("speed", ["8"])[0]
            preserve_pitch = query.get("preserve_pitch", ["1"])[0] != "0"
            return json_bytes(speed_snippet_payload(speed, preserve_pitch=preserve_pitch))
        if path == "/api/weixin/bridge-snippet":
            snippet = PROJECT_ROOT / "outputs" / "authorized_fetchers" / "weixin_bridge_runner_snippet.js"
            if not snippet.exists():
                return json_bytes({"error": "snippet not found"}, 404)
            return 200, snippet.read_bytes(), "application/javascript; charset=utf-8"
        if path == "/api/weixin/bridge-autopost-snippet":
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
            query = parse_qs(parsed.query)
            default_eid = query.get("eid", [""])[0]
            short_uri = query.get("short_uri", [""])[0]
            if short_uri and not default_eid:
                default_eid = refreshed_weixin_export_id(short_uri)
            noprompt = query.get("noprompt", ["0"])[0] == "1"
            return 200, bridge_autopost_js(f"http://{host}/api/receive-artifact", default_eid, noprompt=noprompt), "application/javascript; charset=utf-8"
        if path == "/api/weixin/runtime-capture-snippet":
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
            return 200, runtime_capture_snippet(f"http://{host}/api/receive-artifact").encode("utf-8"), "application/javascript; charset=utf-8"
        if path == "/api/weixin/bridge-launcher":
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
            manifest = bridge_launcher_manifest(host, self.server.server_address[1], parsed.query)
            return json_bytes(manifest)
        if path == "/weixin-bridge-launcher":
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
            manifest = bridge_launcher_manifest(host, self.server.server_address[1], parsed.query)
            return 200, bridge_launcher_html(manifest), "text/html; charset=utf-8"
        if path == "/weixin-bridge-autopost":
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
            query = parse_qs(parsed.query)
            default_eid = query.get("eid", [""])[0]
            short_uri = query.get("short_uri", [""])[0]
            if short_uri and not default_eid:
                default_eid = refreshed_weixin_export_id(short_uri)
            noprompt = query.get("noprompt", ["0"])[0] == "1"
            return 200, bridge_autopost_html(f"http://{host}/api/receive-artifact", default_eid, noprompt=noprompt), "text/html; charset=utf-8"
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[3]
            if path.endswith("/log"):
                return 200, self.store.read_log(job_id).encode("utf-8"), "text/plain; charset=utf-8"
            return json_bytes(self.store.get_job(job_id))
        if path == "/api/file":
            query = parse_qs(parsed.query)
            target = Path(query.get("path", [""])[0]).expanduser().resolve()
            if not target.exists() or not target.is_file():
                return json_bytes({"error": "file not found"}, 404)
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            headers: dict[str, str] = {}
            disposition = content_disposition_for_file(
                target,
                download=query.get("download", ["0"])[0] == "1",
            )
            if disposition:
                headers["Content-Disposition"] = disposition
            return 200, target.read_bytes(), content_type, headers
        return json_bytes({"error": "not found"}, 404)

    def route_post(self) -> tuple[int, bytes, str]:
        payload = self.read_json()
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            return json_bytes(self.store.create_job(payload), 201)
        if parsed.path == "/api/jobs/pause":
            return json_bytes(self.store.pause_jobs(payload.get("job_ids") or []))
        if parsed.path == "/api/jobs/delete":
            return json_bytes(self.store.delete_jobs(payload.get("job_ids") or []))
        if parsed.path == "/api/open":
            return json_bytes(
                self.store.open_target(
                    str(payload.get("url", "")).strip(),
                    str(payload.get("platform") or "auto"),
                )
            )
        if parsed.path == "/api/reveal":
            target = Path(str(payload.get("path") or "")).expanduser().resolve()
            if not target.exists():
                return json_bytes({"error": "file not found"}, 404)
            reveal_path_in_finder(target)
            return json_bytes({"ok": True, "path": str(target)})
        if parsed.path == "/api/receive-artifact":
            if not payload.get("artifact_text"):
                payload["artifact_text"] = json.dumps(payload, ensure_ascii=False)
            payload.setdefault("artifact_ext", ".json")
            payload.setdefault("platform", "weixin")
            return json_bytes(self.store.create_job(payload), 201)
        return json_bytes({"error": "not found"}, 404)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def static_file(self, name: str) -> tuple[int, bytes, str]:
        target = (STATIC_ROOT / name).resolve()
        if not str(target).startswith(str(STATIC_ROOT.resolve())):
            return json_bytes({"error": "invalid path"}, 400)
        if not target.exists() or not target.is_file():
            return json_bytes({"error": "not found"}, 404)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        return 200, target.read_bytes(), content_type

    def respond(self, status: int, body: bytes, content_type: str, headers: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    ensure_layout()
    handler = StudioHandler
    handler.store = JobStore()
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Replay MP3 Studio: http://{host}:{port}")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Replay MP3 Studio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
