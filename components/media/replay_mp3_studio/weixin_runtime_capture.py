from __future__ import annotations

import json
import time
from typing import Any


MEDIA_MARKERS = (
    "stodownload",
    "snsvideodownload",
    "snscosdownload",
    ".m3u8",
    ".mp4",
    ".m4a",
    ".mp3",
    ".aac",
    ".webm",
)
KEY_FIELDS = ("decodeKey", "decode_key", "decryptKey", "decrypt_key", "mediaDecodeKey", "key")
URL_FIELDS = ("url", "videoUrl", "video_url", "downloadUrl", "download_url", "mediaUrl", "media_url", "streamUrl")


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_numeric_key(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    text = _clean(value)
    return text.isdigit() and int(text) > 0


def _media_url(value: Any) -> str:
    text = _clean(value)
    lower = text.lower()
    if not text.startswith(("http://", "https://")):
        return ""
    return text if any(marker in lower for marker in MEDIA_MARKERS) else ""


def _join_url_token(url: str, token: Any) -> str:
    token_text = _clean(token)
    if not token_text:
        return url
    if token_text.startswith(("http://", "https://")):
        return _media_url(token_text) or url
    if token_text.startswith(("?", "&")):
        return url + token_text
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{token_text}"


def _first_url(record: dict[str, Any]) -> str:
    for field in URL_FIELDS:
        url = _media_url(record.get(field))
        if url:
            return _join_url_token(url, record.get("urlToken") or record.get("url_token"))
    return ""


def _first_key(record: dict[str, Any]) -> tuple[str, Any]:
    for field in KEY_FIELDS:
        value = record.get(field)
        if value not in (None, ""):
            return field, value
    return "", None


def _title_from_profile(profile: dict[str, Any]) -> str:
    object_desc = profile.get("objectDesc") if isinstance(profile.get("objectDesc"), dict) else {}
    flow_card = object_desc.get("flowCardDesc") if isinstance(object_desc.get("flowCardDesc"), dict) else {}
    newlife = object_desc.get("finderNewlifeDesc") if isinstance(object_desc.get("finderNewlifeDesc"), dict) else {}
    return (
        _clean(profile.get("title"))
        or _clean(object_desc.get("description"))
        or _clean(flow_card.get("description"))
        or _clean(newlife.get("richTextTitle"))
        or _clean(profile.get("description"))
        or _clean(profile.get("id"))
    )


def _contact_from_profile(profile: dict[str, Any]) -> dict[str, str]:
    contact = profile.get("contact") if isinstance(profile.get("contact"), dict) else {}
    anchor = profile.get("anchorContact") if isinstance(profile.get("anchorContact"), dict) else {}
    source = contact or anchor
    return {
        "id": _clean(source.get("username") or source.get("id")),
        "nickname": _clean(source.get("nickname") or source.get("bizNickname")),
    }


def _append_item(
    items: list[dict[str, Any]],
    *,
    profile: dict[str, Any],
    media: dict[str, Any],
    source_path: str,
) -> None:
    url = _first_url(media) or _first_url(profile)
    if not url:
        return
    key_field, key_value = _first_key(media)
    if key_value in (None, ""):
        key_field, key_value = _first_key(profile)

    item: dict[str, Any] = {
        "source_path": source_path,
        "type": _clean(profile.get("type") or "media"),
        "id": _clean(profile.get("id")),
        "nonce_id": _clean(profile.get("nonce_id") or profile.get("objectNonceId")),
        "title": _title_from_profile(profile),
        "url": url,
        "contact": _contact_from_profile(profile),
    }
    if media.get("spec") not in (None, ""):
        item["spec"] = media.get("spec")
    if media.get("fileSize") not in (None, ""):
        item["size"] = media.get("fileSize")
    if key_value not in (None, ""):
        item["key_field"] = key_field
        if _is_numeric_key(key_value):
            item["key"] = int(key_value)
            item["encLimit"] = int(media.get("encLimit") or media.get("enc_limit") or 131072)
        else:
            item["decodeKey"] = _clean(key_value)
    items.append(item)


def runtime_capture_items_from_profile(profile: Any, *, source_path: str = "$") -> list[dict[str, Any]]:
    if not isinstance(profile, dict):
        return []
    items: list[dict[str, Any]] = []

    object_desc = profile.get("objectDesc") if isinstance(profile.get("objectDesc"), dict) else {}
    media_list = object_desc.get("media") if isinstance(object_desc.get("media"), list) else []
    for index, media in enumerate(media_list):
        if isinstance(media, dict):
            _append_item(items, profile=profile, media=media, source_path=f"{source_path}.objectDesc.media[{index}]")

    live_info = profile.get("liveInfo") if isinstance(profile.get("liveInfo"), dict) else {}
    if live_info.get("streamUrl"):
        _append_item(items, profile={**profile, "type": "live"}, media={"url": live_info.get("streamUrl")}, source_path=f"{source_path}.liveInfo")

    _append_item(items, profile=profile, media=profile, source_path=source_path)
    return items


def runtime_capture_artifact_from_profiles(
    profiles: list[Any],
    *,
    page_url: str,
    captured_at: int | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, profile in enumerate(profiles):
        for item in runtime_capture_items_from_profile(profile, source_path=f"profiles[{index}]"):
            key_identity = _clean(item.get("decodeKey") or item.get("key"))
            dedupe_key = (_clean(item.get("url")), key_identity, _clean(item.get("source_path")))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(item)

    return {
        "source": "weixin_runtime_profile_capture",
        "capture_method": "ltaoo_compatible_runtime_profile",
        "page": page_url,
        "captured_at": int(captured_at if captured_at is not None else time.time()),
        "profile_count": len(profiles),
        "item_count": len(items),
        "items": items,
    }


def runtime_capture_snippet(endpoint: str) -> str:
    endpoint_json = json.dumps(endpoint)
    return f"""
(async function () {{
  const STUDIO_ENDPOINT = {endpoint_json};
  const SOURCE = "weixin_runtime_profile_capture";
  const mediaMarkers = ["stodownload", "snsvideodownload", "snscosdownload", ".m3u8", ".mp4", ".m4a", ".mp3", ".aac", ".webm"];
  const clean = value => value === undefined || value === null ? "" : String(value).trim();
  const mediaUrl = value => {{
    const text = clean(value);
    const lower = text.toLowerCase();
    if (!/^https?:\\/\\//.test(text)) return "";
    return mediaMarkers.some(marker => lower.includes(marker)) ? text : "";
  }};
  const joinUrlToken = (url, token) => {{
    const value = clean(token);
    if (!value) return url;
    if (/^https?:\\/\\//.test(value)) return mediaUrl(value) || url;
    if (value.startsWith("?") || value.startsWith("&")) return url + value;
    return url + (url.includes("?") ? "&" : "?") + value;
  }};
  const firstUrl = record => {{
    if (!record || typeof record !== "object") return "";
    for (const field of ["url", "videoUrl", "video_url", "downloadUrl", "download_url", "mediaUrl", "media_url", "streamUrl"]) {{
      const url = mediaUrl(record[field]);
      if (url) return joinUrlToken(url, record.urlToken || record.url_token || "");
    }}
    return "";
  }};
  const firstKey = record => {{
    if (!record || typeof record !== "object") return ["", ""];
    for (const field of ["decodeKey", "decode_key", "decryptKey", "decrypt_key", "mediaDecodeKey", "key"]) {{
      if (record[field] !== undefined && record[field] !== null && record[field] !== "") return [field, record[field]];
    }}
    return ["", ""];
  }};
  const isNumericKey = value => /^\\d+$/.test(clean(value)) && Number(clean(value)) > 0;
  const titleFrom = profile => {{
    const desc = profile && profile.objectDesc || {{}};
    return clean(profile && profile.title) || clean(desc.description) ||
      clean(desc.flowCardDesc && desc.flowCardDesc.description) ||
      clean(desc.finderNewlifeDesc && desc.finderNewlifeDesc.richTextTitle) ||
      clean(profile && profile.description) || clean(profile && profile.id);
  }};
  const addItem = (items, profile, media, sourcePath) => {{
    const url = firstUrl(media) || firstUrl(profile);
    if (!url) return;
    let [keyField, keyValue] = firstKey(media);
    if (!keyValue) [keyField, keyValue] = firstKey(profile);
    const item = {{
      source_path: sourcePath,
      type: clean(profile && profile.type) || "media",
      id: clean(profile && profile.id),
      nonce_id: clean(profile && (profile.nonce_id || profile.objectNonceId)),
      title: titleFrom(profile || {{}}),
      url
    }};
    if (media && media.spec !== undefined) item.spec = media.spec;
    if (keyValue) {{
      item.key_field = keyField;
      if (isNumericKey(keyValue)) {{
        item.key = Number(clean(keyValue));
        item.encLimit = Number(media && (media.encLimit || media.enc_limit)) || 131072;
      }} else {{
        item.decodeKey = clean(keyValue);
      }}
    }}
    items.push(item);
  }};
  const itemsFromProfile = (profile, sourcePath) => {{
    const items = [];
    if (!profile || typeof profile !== "object") return items;
    const mediaList = profile.objectDesc && Array.isArray(profile.objectDesc.media) ? profile.objectDesc.media : [];
    mediaList.forEach((media, index) => addItem(items, profile, media, `${{sourcePath}}.objectDesc.media[${{index}}]`));
    if (profile.liveInfo && profile.liveInfo.streamUrl) addItem(items, Object.assign({{}}, profile, {{type: "live"}}), {{url: profile.liveInfo.streamUrl}}, `${{sourcePath}}.liveInfo`);
    addItem(items, profile, profile, sourcePath);
    return items;
  }};
  const profiles = [];
  if (window.__wx_channels_store__ && window.__wx_channels_store__.profile) profiles.push(window.__wx_channels_store__.profile);
  if (window.__wx_channels_live_store__ && window.__wx_channels_live_store__.profile) profiles.push(window.__wx_channels_live_store__.profile);
  const items = [];
  const seen = new Set();
  profiles.forEach((profile, index) => {{
    itemsFromProfile(profile, `profiles[${{index}}]`).forEach(item => {{
      const key = [item.url || "", item.decodeKey || item.key || "", item.source_path || ""].join("\\n");
      if (seen.has(key)) return;
      seen.add(key);
      items.push(item);
    }});
  }});
  const artifact = {{
    source: SOURCE,
    capture_method: "ltaoo_compatible_runtime_profile",
    page: location.href,
    captured_at: Math.floor(Date.now() / 1000),
    profile_count: profiles.length,
    item_count: items.length,
    items
  }};
  if (!items.length) {{
    console.log(SOURCE, {{status: "no-runtime-media-profile", item_count: 0, artifact}});
    return;
  }}
  const body = {{
    platform: "weixin",
    url: location.href,
    artifact_ext: ".json",
    artifact_text: JSON.stringify(artifact)
  }};
  const resp = await fetch(STUDIO_ENDPOINT, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(body)
  }});
  console.log(SOURCE, {{status: resp.status, item_count: items.length, response: await resp.text()}});
}})();
""".strip()
