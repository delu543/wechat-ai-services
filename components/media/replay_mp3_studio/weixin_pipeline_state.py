from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .utils import parse_weixin_short_uri


PIPELINE_STATE_SCHEMA_VERSION = 1
PIPELINE_STATE_FILENAME = "weixin_pipeline_state.json"
PIPELINE_MAX_RETRIES_PER_PHASE = 2
PIPELINE_PHASES = (
    "existing_output_checked",
    "source_vault_checked",
    "direct_probe_checked",
    "target_opened",
    "playback_verified",
    "causal_capture_complete",
    "source_converted",
    "output_verified",
)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _target_fingerprint(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]


def pipeline_state_path(artifacts: Path) -> Path:
    return Path(artifacts) / PIPELINE_STATE_FILENAME


def _next_phase(completed_phases: list[str]) -> str:
    completed = set(completed_phases)
    return next((phase for phase in PIPELINE_PHASES if phase not in completed), "complete")


def _new_state(url: str, mode: str) -> dict[str, Any]:
    now = _timestamp()
    return {
        "schema_version": PIPELINE_STATE_SCHEMA_VERSION,
        "target_short_uri": parse_weixin_short_uri(url),
        "target_fingerprint": _target_fingerprint(url),
        "mode": mode,
        "status": "running",
        "current_phase": PIPELINE_PHASES[0],
        "completed_phases": [],
        "phase_data": {},
        "retry_count_by_phase": {},
        "max_retries_per_phase": PIPELINE_MAX_RETRIES_PER_PHASE,
        "created_at": now,
        "updated_at": now,
    }


def save_pipeline_state(path: Path, state: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _timestamp()
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(target)


def load_or_create_pipeline_state(
    artifacts: Path,
    *,
    url: str,
    mode: str,
) -> tuple[Path, dict[str, Any]]:
    path = pipeline_state_path(artifacts)
    if not path.exists():
        state = _new_state(url, mode)
        save_pipeline_state(path, state)
        return path, state

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Weixin pipeline state is unreadable: {path}: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"Weixin pipeline state is not an object: {path}")
    if int(state.get("schema_version") or 0) != PIPELINE_STATE_SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported Weixin pipeline state schema: {path}")
    if state.get("target_short_uri") != parse_weixin_short_uri(url):
        raise RuntimeError("Weixin pipeline state belongs to a different short link.")
    if state.get("target_fingerprint") != _target_fingerprint(url):
        raise RuntimeError("Weixin pipeline state target fingerprint does not match.")
    if state.get("mode") != mode:
        raise RuntimeError("Weixin pipeline state belongs to a different execution mode.")
    return path, state


def pipeline_phase_completed(state: dict[str, Any], phase: str) -> bool:
    return phase in set(state.get("completed_phases") or [])


def mark_pipeline_phase_complete(
    path: Path,
    state: dict[str, Any],
    phase: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    if phase not in PIPELINE_PHASES:
        raise ValueError(f"Unknown Weixin pipeline phase: {phase}")
    completed = list(state.get("completed_phases") or [])
    if phase not in completed:
        completed.append(phase)
        completed.sort(key=PIPELINE_PHASES.index)
    state["completed_phases"] = completed
    if details:
        state.setdefault("phase_data", {})[phase] = dict(details)
    state["current_phase"] = _next_phase(completed)
    if phase == "output_verified":
        state["status"] = "completed"
        state["current_phase"] = "complete"
    elif state.get("status") == "blocked":
        state["status"] = "running"
    save_pipeline_state(path, state)


def mark_pipeline_phase_failure(
    path: Path,
    state: dict[str, Any],
    phase: str,
    *,
    error_code: str,
) -> int:
    if phase not in PIPELINE_PHASES:
        raise ValueError(f"Unknown Weixin pipeline phase: {phase}")
    retry_counts = state.setdefault("retry_count_by_phase", {})
    count = int(retry_counts.get(phase) or 0) + 1
    retry_counts[phase] = count
    state.setdefault("phase_data", {})[phase] = {"last_error_code": error_code}
    state["current_phase"] = phase
    if count >= int(state.get("max_retries_per_phase") or PIPELINE_MAX_RETRIES_PER_PHASE):
        state["status"] = "blocked"
        state["blocked_reason"] = error_code
    save_pipeline_state(path, state)
    return count


def pipeline_resume_action(state: dict[str, Any]) -> str:
    if pipeline_phase_completed(state, "output_verified"):
        return "reuse_verified_output"
    if pipeline_phase_completed(state, "causal_capture_complete") and not pipeline_phase_completed(
        state, "source_converted"
    ):
        return "resume_frozen_conversion"
    if pipeline_phase_completed(state, "target_opened") and not pipeline_phase_completed(
        state, "causal_capture_complete"
    ):
        return "reuse_verified_message"
    return "continue_fast_path"


def mark_existing_pipeline_phase(
    artifacts: Path,
    *,
    target_short_uri: str,
    phase: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Advance an existing state file without creating or retargeting one."""

    path = pipeline_state_path(artifacts)
    if not path.is_file() or not target_short_uri:
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(state, dict) or state.get("target_short_uri") != target_short_uri:
        return False
    mark_pipeline_phase_complete(path, state, phase, details=details)
    return True
