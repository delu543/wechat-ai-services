from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List


def write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _event_current(event: Dict) -> Dict:
    return event.get("current") or event.get("previous") or {}


def write_audit_csv(path: Path, events: Iterable[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time",
        "event",
        "path",
        "size",
        "size_before",
        "mtime",
        "ctime",
        "inode",
        "sha256",
        "header_hex",
        "classification",
        "artifact_role",
        "ffprobe_recognized",
        "has_audio",
        "has_video",
        "duration",
        "audio_codecs",
        "video_codecs",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for event in events:
            current = _event_current(event)
            previous = event.get("previous") or {}
            probe = event.get("ffprobe") or {}
            audio_codecs = ",".join(stream.get("codec_name", "") for stream in probe.get("audio_streams", []))
            video_codecs = ",".join(stream.get("codec_name", "") for stream in probe.get("video_streams", []))
            writer.writerow(
                {
                    "time": event.get("time", ""),
                    "event": event.get("event", ""),
                    "path": current.get("path", event.get("path", "")),
                    "size": current.get("size", ""),
                    "size_before": previous.get("size", ""),
                    "mtime": current.get("mtime", ""),
                    "ctime": current.get("ctime", ""),
                    "inode": current.get("inode", ""),
                    "sha256": current.get("sha256", ""),
                    "header_hex": current.get("header_hex", ""),
                    "classification": current.get("classification", {}).get("kind", ""),
                    "artifact_role": current.get("artifact_role", ""),
                    "ffprobe_recognized": probe.get("recognized", ""),
                    "has_audio": probe.get("has_audio", ""),
                    "has_video": probe.get("has_video", ""),
                    "duration": probe.get("duration", ""),
                    "audio_codecs": audio_codecs,
                    "video_codecs": video_codecs,
                }
            )


def _bool_text(value: object) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def _first_event_answer(event: Dict) -> List[str]:
    current = _event_current(event)
    probe = event.get("ffprobe") or {}
    classification = current.get("classification", {})
    lines = [
        f"1. 第一次抓到的文件路径是什么？ `{current.get('path', event.get('path', '无'))}`",
        f"2. 它的文件大小是多少？ `{current.get('size', '未知')}` bytes",
        f"3. 它的文件头是什么？ `{current.get('header_hex', '')[:512]}`",
        f"4. 它被识别为什么类型？ `{classification.get('kind', 'unknown')}`，置信度 `{classification.get('confidence', 'unknown')}`",
        f"5. ffprobe 是否能识别？ {_bool_text(probe.get('recognized'))}",
        f"6. 是否包含音频流？ {_bool_text(probe.get('has_audio'))}",
        f"7. 它更可能是什么？ `{current.get('artifact_role', 'unknown')}`",
    ]
    can_convert = bool(probe.get("has_audio"))
    lines.append(f"8. 能不能作为后续转 MP3 的输入？ {'可以' if can_convert else '不能直接作为 MP3 输入'}")
    return lines


def write_audit_markdown(path: Path, report: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = report.get("events", [])
    media_events = [
        event
        for event in events
        if (event.get("ffprobe") or {}).get("has_audio") or (event.get("ffprobe") or {}).get("has_video")
    ]
    audio_events = [event for event in events if (event.get("ffprobe") or {}).get("has_audio")]
    first_event = events[0] if events else None

    lines: List[str] = [
        "# Cache Audit Report",
        "",
        "## Scope",
        "",
        f"- Started: `{report.get('started_at', '')}`",
        f"- Finished: `{report.get('finished_at', '')}`",
        f"- Duration: `{report.get('duration_seconds', '')}` seconds",
        f"- Directories: `{', '.join(report.get('directories', []))}`",
        f"- Baseline files: `{report.get('baseline_count', 0)}`",
        f"- Events: `{len(events)}`",
        "",
        "## First Changed File",
        "",
    ]
    if first_event:
        lines.extend(_first_event_answer(first_event))
    else:
        lines.append("本次实验没有记录到新增、删除、大小变化或时间戳变化文件。")

    lines.extend(
        [
            "",
            "## Evidence Summary",
            "",
            f"- ffprobe/ffmpeg 可识别的候选媒体变化: `{len(media_events)}`",
            f"- 包含音频流的候选变化: `{len(audio_events)}`",
            "- 如果变化文件是 LevelDB/SQLite/日志/图片，它不能直接说明已经抓到可转 MP3 的媒体。",
            "- 如果变化文件只有匿名临时 FD 或路径已删除，普通目录扫描会漏掉，需要结合进程 FD 观察或使用黑箱兜底。",
            "",
            "## Second-Run Miss Diagnosis",
            "",
        ]
    )
    if audio_events:
        lines.append("- 本次出现可识别音频流，第二次抓不到更可能与缓存命中、目录差异、扫描窗口或播放链路变化有关。")
    elif media_events:
        lines.append("- 本次出现可识别媒体容器但没有音频流，可能抓到的是视频分片、缩略媒体或不完整片段。")
    elif events:
        roles = sorted({(_event_current(event).get("artifact_role") or "unknown") for event in events})
        lines.append(f"- 本次变化主要类型: `{', '.join(roles)}`。没有证据表明这些文件能直接转 MP3。")
        lines.append("- 候选原因包括：内存缓存、已有缓存命中、变化目录不在监控范围、临时文件过快删除、私有/加密分片、WebView 层不暴露真实媒体流。")
    else:
        lines.append("- 没有变化事件。候选原因包括：目录选错、播放已完全命中缓存、扫描权限不足、播放链路没有落盘。")

    cache_success = "成功" if audio_events else "失败"
    cache_evidence = "ffprobe found audio stream" if audio_events else "no changed file with audio stream"
    lines.extend(
        [
            "",
            "## Conclusion Table",
            "",
            "| 方法 | 是否成功 | 证据 | 输出文件 | 风险 | 建议 |",
            "| --- | --- | --- | --- | --- | --- |",
            "| 网络媒体流 | 未运行 | 使用 `probe-url` 单独验证 | - | 低/中 | 优先验证 |",
            f"| 缓存文件 | {cache_success} | {cache_evidence} | - | 低 | {'可继续转码' if audio_events else '继续扩大目录或进入兜底'} |",
            "| 黑箱录制 | 未运行 | 需用户显式启动 | - | 中 | 兜底 |",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_audit_reports(out_prefix: Path, report: Dict) -> Dict[str, str]:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_prefix.with_suffix(".json")
    csv_path = out_prefix.with_suffix(".csv")
    md_path = out_prefix.with_suffix(".md")
    write_json(json_path, report)
    write_audit_csv(csv_path, report.get("events", []))
    write_audit_markdown(md_path, report)
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}
