#!/usr/bin/env python3
"""Create Weixin Channels bridge payload templates for authorized test devices.

This script does not read WeChat cookies, browser state, certificates, or local
WeChat data. It only refreshes the public dynamicExportId and writes JSON
templates for the WeixinJSBridge finderH5Auth/finderH5ExtTransfer calls that the
H5 replay page uses.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "outputs" / "weixin_bridge_payload_packet.json"
FEED_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"


def snake(value: Any) -> Any:
    if isinstance(value, list):
        return [snake(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        converted = []
        for ch in key:
            if ch.isupper():
                converted.append("_")
                converted.append(ch.lower())
            else:
                converted.append(ch)
        out["".join(converted)] = snake(item)
    return out


def request_scene(short_uri: str) -> dict[str, Any]:
    rid = str(int(time.time() * 1000))
    page_url = "https://channels.weixin.qq.com/finder-preview/pages/sph"
    url = FEED_API + "?" + urllib.parse.urlencode({"_rid": rid, "_pageUrl": page_url})
    payload = json.dumps({"baseReq": {"generalToken": ""}, "shortUri": short_uri}, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={short_uri}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    scene = data.get("data", {}).get("sceneInfo") or {}
    if not scene.get("dynamicExportId"):
        raise RuntimeError(f"No dynamicExportId in response: {data}")
    return scene


def request_id() -> str:
    return f"{int(time.time() * 1000)}{random.randint(0, 999999):06d}"


def transfer_payload(name: str, url: str, cmdid: int, data: dict[str, Any], h5_auth_token: str = "") -> dict[str, Any]:
    return {
        "bridge": "WeixinJSBridge.invoke",
        "method": "finderH5ExtTransfer",
        "name": name,
        "params": {
            "req_json": json.dumps(snake(data), ensure_ascii=False, separators=(",", ":")),
            "url": url,
            "cgi_cmdid": cmdid,
            "h5AuthToken": h5_auth_token,
            "is_security_check": False,
            "scope": "finderLive",
        },
    }


def build_packet(short_uri: str, scene: dict[str, Any]) -> dict[str, Any]:
    export_id = scene["dynamicExportId"]
    comment_detail_data = {
        "finderBasereq": {
            "exptFlag": 1,
            "requestId": request_id(),
        },
        "platformScene": 2,
        "encryptedObjectid": export_id,
        "needObject": 1,
        "scene": 141,
        "direction": 2,
        "identityScene": 2,
        "pullScene": 1,
    }
    live_info_template = {
        "finderBasereq": {},
        "liveId": "<fill from finderGetCommentDetail response: data.object.liveInfo.liveId>",
    }
    return {
        "short_uri": short_uri,
        "generated_at": int(time.time()),
        "scene_info": scene,
        "boundary": "Use only on authorized company/test devices. Do not use a personal WeChat account.",
        "h5_auth": {
            "bridge": "WeixinJSBridge.invoke",
            "method": "finderH5Auth",
            "params": {"h5Version": 3774873601, "scope": "finderLive"},
            "returns": "h5AuthToken, required by finderH5ExtTransfer in real Weixin client context",
        },
        "step_1_finder_get_comment_detail": transfer_payload(
            "FinderGetCommentDetail",
            "/cgi-bin/micromsg-bin/pc_findergetcommentdetail",
            5259,
            comment_detail_data,
        ),
        "step_2_finder_get_live_info_template": transfer_payload(
            "FinderGetLiveInfo",
            "/cgi-bin/micromsg-bin/pc_findergetliveinfo",
            10064,
            live_info_template,
        ),
        "target_media_fields": [
            "data.liveInfo.replayInfo.renderReplayUrl",
            "data.liveInfo.replayInfo.renderReplayHlsUrl",
        ],
        "conversion": (
            "Save the authorized bridge response as HAR/JSON/text, then run "
            "extract_media_from_artifact.py --output outputs/weixin_video_channel.mp3"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--short-uri", default="AtKXhlaKjL")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    scene = request_scene(args.short_uri)
    packet = build_packet(args.short_uri, scene)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote bridge payload packet to {output}")
    print(f"dynamicExportId expires at {scene.get('expiredTime')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
