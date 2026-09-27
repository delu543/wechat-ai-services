from __future__ import annotations

import json
import urllib.parse
from typing import Any


def normalize_speed(value: Any, default: float = 8.0) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = default
    if speed < 0.25:
        return 0.25
    if speed > 16:
        return 16.0
    return speed


def media_speed_script(speed: float, preserve_pitch: bool = True) -> str:
    speed = normalize_speed(speed)
    preserve = "true" if preserve_pitch else "false"
    rate = json.dumps(speed)
    return f"""(() => {{
  const targetRate = Number({rate});
  const preservePitch = {preserve};
  const controllerKey = "__codexMediaSpeedController";

  function tune(el) {{
    if (!el || !(el instanceof HTMLMediaElement)) return false;
    try {{
      el.defaultPlaybackRate = targetRate;
      el.playbackRate = targetRate;
      if ("preservesPitch" in el) el.preservesPitch = preservePitch;
      if ("webkitPreservesPitch" in el) el.webkitPreservesPitch = preservePitch;
      if ("mozPreservesPitch" in el) el.mozPreservesPitch = preservePitch;
      el.dataset.codexForcedRate = String(targetRate);
      if (!el.__codexRateGuarded) {{
        el.addEventListener("ratechange", () => {{
          if (Math.abs(Number(el.playbackRate || 0) - targetRate) > 0.01) {{
            setTimeout(() => tune(el), 0);
          }}
        }});
        el.__codexRateGuarded = true;
      }}
      return true;
    }} catch (_) {{
      return false;
    }}
  }}

  function tuneAll() {{
    let count = 0;
    document.querySelectorAll("video,audio").forEach(el => {{
      if (tune(el)) count += 1;
    }});
    window.__codexForcedRate = targetRate;
    window.__codexMediaCount = count;
    return count;
  }}

  if (!window[controllerKey]) {{
    const originalPlay = HTMLMediaElement.prototype.play;
    if (!HTMLMediaElement.prototype.__codexPlayWrapped) {{
      HTMLMediaElement.prototype.play = function(...args) {{
        tune(this);
        return originalPlay.apply(this, args);
      }};
      HTMLMediaElement.prototype.__codexPlayWrapped = true;
    }}
    const observer = new MutationObserver(tuneAll);
    observer.observe(document.documentElement || document.body, {{
      childList: true,
      subtree: true
    }});
    const timer = setInterval(tuneAll, 500);
    window[controllerKey] = {{ observer, timer }};
  }}

  const count = tuneAll();
  console.log(`[Codex] forced media speed ${{targetRate}}x on ${{count}} element(s).`);
  return {{ targetRate, count }};
}})();"""


def media_speed_bookmarklet(speed: float, preserve_pitch: bool = True) -> str:
    script = media_speed_script(speed, preserve_pitch=preserve_pitch)
    return "javascript:" + urllib.parse.quote(script, safe="()[]{}!~*'\";:,.?/+=&$#-")


def media_timeline_probe_script(
    speed: float,
    *,
    sample_seconds: float = 3.0,
    preserve_pitch: bool = True,
) -> str:
    speed = normalize_speed(speed)
    sample_seconds = max(0.5, min(float(sample_seconds or 3.0), 30.0))
    preserve = "true" if preserve_pitch else "false"
    rate = json.dumps(speed)
    sample_ms = json.dumps(round(sample_seconds * 1000))
    return f"""(async () => {{
  const targetRate = Number({rate});
  const preservePitch = {preserve};
  const sampleMs = Number({sample_ms});
  const media = Array.from(document.querySelectorAll("video,audio"))
    .find(el => el instanceof HTMLMediaElement && !Number.isNaN(Number(el.duration || 0)));
  if (!media) {{
    const result = {{
      ok: false,
      requested_speed: targetRate,
      observed_speed: 0,
      sample_count: 0,
      limit_point: "no_html_media_element"
    }};
    console.log("[Codex] media timeline speed probe", result);
    return result;
  }}
  try {{
    media.defaultPlaybackRate = targetRate;
    media.playbackRate = targetRate;
    if ("preservesPitch" in media) media.preservesPitch = preservePitch;
    if ("webkitPreservesPitch" in media) media.webkitPreservesPitch = preservePitch;
    if ("mozPreservesPitch" in media) media.mozPreservesPitch = preservePitch;
  }} catch (error) {{
    const result = {{
      ok: false,
      requested_speed: targetRate,
      observed_speed: 0,
      playback_rate: Number(media.playbackRate || 0),
      sample_count: 0,
      limit_point: "playback_rate_assignment_failed",
      error: error && error.message ? error.message : String(error)
    }};
    console.log("[Codex] media timeline speed probe", result);
    return result;
  }}
  const startedWall = performance.now();
  const samples = [];
  const pushSample = () => {{
    samples.push({{
      wall_ms: Math.round(performance.now() - startedWall),
      media_time: Number(media.currentTime || 0),
      playback_rate: Number(media.playbackRate || 0),
      paused: Boolean(media.paused),
      ready_state: Number(media.readyState || 0)
    }});
  }};
  pushSample();
  if (media.paused) {{
    try {{ await media.play(); }} catch (_) {{}}
  }}
  const interval = setInterval(pushSample, 250);
  await new Promise(resolve => setTimeout(resolve, sampleMs));
  clearInterval(interval);
  pushSample();
  const first = samples[0] || {{ wall_ms: 0, media_time: 0 }};
  const last = samples[samples.length - 1] || first;
  const wallSeconds = Math.max(0, Number(last.wall_ms - first.wall_ms) / 1000);
  const mediaSeconds = Math.max(0, Number(last.media_time - first.media_time));
  const observedSpeed = wallSeconds > 0 ? mediaSeconds / wallSeconds : 0;
  const stable = observedSpeed >= targetRate * 0.85;
  const result = {{
    ok: stable,
    requested_speed: targetRate,
    observed_speed: Number(observedSpeed.toFixed(3)),
    playback_rate: Number(media.playbackRate || 0),
    wall_seconds: Number(wallSeconds.toFixed(3)),
    media_seconds: Number(mediaSeconds.toFixed(3)),
    sample_count: samples.length,
    samples,
    limit_point: stable
      ? "html_media_timeline_advanced_at_requested_rate"
      : "playback_rate_clamped_or_renderer_ignored_requested_rate"
  }};
  console.log("[Codex] media timeline speed probe", result);
  return result;
}})();"""


def summarize_timeline_probe_samples(
    samples: list[dict[str, object]],
    *,
    requested_speed: float,
    tolerance_ratio: float = 0.85,
) -> dict[str, object]:
    requested = normalize_speed(requested_speed)
    valid: list[tuple[float, float]] = []
    for sample in samples:
        try:
            wall_ms = float(sample.get("wall_ms") or 0)
            media_time = float(sample.get("media_time") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        valid.append((wall_ms, media_time))
    if len(valid) < 2:
        return {
            "requested_speed": requested,
            "observed_speed": 0.0,
            "wall_seconds": 0.0,
            "media_seconds": 0.0,
            "sample_count": len(valid),
            "stable": False,
            "limit_point": "insufficient_timeline_samples",
        }
    first_wall, first_media = valid[0]
    last_wall, last_media = valid[-1]
    wall_seconds = max(0.0, (last_wall - first_wall) / 1000.0)
    media_seconds = max(0.0, last_media - first_media)
    observed = media_seconds / wall_seconds if wall_seconds > 0 else 0.0
    stable = observed >= requested * tolerance_ratio
    if stable:
        limit_point = "html_media_timeline_advanced_at_requested_rate"
    elif observed > 3.0:
        limit_point = "html_media_timeline_above_3x_but_below_requested_rate"
    else:
        limit_point = "playback_rate_clamped_or_renderer_ignored_requested_rate"
    return {
        "requested_speed": round(requested, 3),
        "observed_speed": round(observed, 3),
        "wall_seconds": round(wall_seconds, 3),
        "media_seconds": round(media_seconds, 3),
        "sample_count": len(valid),
        "stable": stable,
        "tolerance_ratio": round(float(tolerance_ratio), 3),
        "limit_point": limit_point,
    }


def speed_snippet_payload(speed: Any, preserve_pitch: bool = True) -> dict[str, Any]:
    normalized = normalize_speed(speed)
    return {
        "speed": normalized,
        "preserve_pitch": preserve_pitch,
        "snippet": media_speed_script(normalized, preserve_pitch=preserve_pitch),
        "bookmarklet": media_speed_bookmarklet(normalized, preserve_pitch=preserve_pitch),
        "timeline_probe_snippet": media_timeline_probe_script(normalized, preserve_pitch=preserve_pitch),
        "notes": [
            "Only affects standard HTML video/audio elements in the current page context.",
            "If the WeChat WebView blocks javascript URLs or the player uses a native/private renderer, this will not change playback speed.",
            "The timeline probe measures actual currentTime advance against wall-clock time; use that result instead of trusting the UI label.",
            "Before blackbox recording, visually confirm the player is actually running at the requested speed.",
            "Use the same confirmed speed as the blackbox speed so the MP3 restoration is correct.",
        ],
    }
