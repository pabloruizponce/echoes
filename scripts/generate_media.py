#!/usr/bin/env python3
"""Generate and download NotebookLM media for processed papers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from notebooklm import ArtifactDownloadError, ArtifactNotReadyError, NotebookLMClient


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLL_INTERVAL = 5
DEFAULT_RETRY_INITIAL_DELAY = 30
DEFAULT_RETRY_MAX_DELAY = 300
DEFAULT_MISSING_POLLS_BEFORE_RESUBMIT = 3
DEFAULT_MISSING_NOTEBOOK_POLLS_BEFORE_FAIL = 3
DEFAULT_MAX_SNAPSHOT_ERRORS = 5
DEFAULT_MAX_TRANSIENT_POLL_ERRORS = 5
DEFAULT_MAX_TRANSIENT_GENERATION_FAILURES = 5
DEFAULT_NOTEBOOKLM_RPC_TIMEOUT = 120.0
LOCK_FILE_NAME = "media.lock.json"
DEFAULT_NOTEBOOKLM_LANGUAGE = "en"
LEGACY_UNTAGGED_AUDIO_LANGUAGE = "es"
# Backwards-compatible alias for existing tests/importers. Runtime code should use
# the requested media language carried by each MediaPaperResult.
NOTEBOOKLM_LANGUAGE = DEFAULT_NOTEBOOKLM_LANGUAGE
MEDIA_LANGUAGE_ALIASES = {
    "es": "es",
    "spanish": "es",
    "en": "en",
    "english": "en",
}
VIDEO_FORMAT = "explainer"
VIDEO_STYLE = "whiteboard"
VIDEO_FORMAT_CODE = 1
# NotebookLM's undocumented RPC style enum has drifted from notebooklm-py's labels.
# Empirically, code 3 currently produces the whiteboard-like visual style.
VIDEO_WHITEBOARD_STYLE_CODE = 3
VIDEO_OFFICIAL_WHITEBOARD_STYLE_CODE = 4
VIDEO_API_GENERATION_METHOD = "api_raw_style_code"
VIDEO_OFFICIAL_FALLBACK_METHOD = "cli_official_style"
ARTIFACT_STATUS_NAMES = {
    1: "in_progress",
    2: "pending",
    3: "completed",
    4: "failed",
}
ARTIFACT_TYPE_NAMES = {
    1: "audio",
    2: "report",
    3: "video",
    4: "quiz",
    5: "mind_map",
    7: "infographic",
    8: "slide_deck",
    9: "data_table",
}
TRANSIENT_ERROR_HINTS = (
    "rate limit",
    "quota",
    "too many requests",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "internal error",
    "try again",
    "backend error",
    "server error",
    "null result data",
)
UNRECOVERABLE_ERROR_HINTS = (
    "auth",
    "login",
    "unauthorized",
    "forbidden",
    "not found",
    "unknown notebook",
    "unknown source",
    "invalid notebook",
    "invalid source",
    "missing notebook",
    "missing source",
    "no longer available",
    "notebooklm notebook",
)


def normalize_media_language(value: str | None) -> str:
    text = str(value or DEFAULT_NOTEBOOKLM_LANGUAGE).strip().lower()
    language = MEDIA_LANGUAGE_ALIASES.get(text)
    if language:
        return language
    raise argparse.ArgumentTypeError(
        "Unsupported media language. Use one of: es, spanish, en, english."
    )


def config_dir() -> Path:
    override = os.environ.get("ECHOES_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return ROOT / ".echoes"


def ensure_notebooklm_home() -> Path:
    override = os.environ.get("NOTEBOOKLM_HOME")
    home = Path(override).expanduser() if override else config_dir() / "notebooklm"
    os.environ.setdefault("NOTEBOOKLM_HOME", str(home))
    return home


def notebooklm_binary() -> str:
    if os.name == "nt":
        candidate = ROOT / ".venv" / "Scripts" / "notebooklm.exe"
    else:
        candidate = ROOT / ".venv" / "bin" / "notebooklm"
    if candidate.exists():
        return str(candidate)
    return "notebooklm"


def latest_manifest_file(directory: Path) -> Path:
    candidates = sorted(
        directory.glob("*/manifest.json"),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if not candidates:
        raise SystemExit(f"No processed manifest.json files found in {directory}")
    return candidates[-1]


def choose_manifest_path(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit(f"Processed manifest not found: {path}")
        return path
    return latest_manifest_file(config_dir() / "processed")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def slugify(text: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "paper"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} does not contain a JSON object.")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compute_retry_delay(attempt: int) -> int:
    delay = DEFAULT_RETRY_INITIAL_DELAY * (2 ** max(0, attempt - 1))
    return min(delay, DEFAULT_RETRY_MAX_DELAY)


def notebooklm_rpc_timeout(timeout: float | None = None) -> float:
    return max(DEFAULT_NOTEBOOKLM_RPC_TIMEOUT, float(timeout or 0.0))


def run_json_command(
    command: list[str],
    *,
    allow_failure_json: bool = False,
    allow_empty_success: bool = False,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()

    parsed: dict[str, Any] | None = None
    if stdout:
        try:
            candidate = json.loads(stdout)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict):
            parsed = candidate

    if completed.returncode != 0 and not (allow_failure_json and parsed is not None):
        raise RuntimeError(stderr or stdout or "Command failed.")

    if parsed is None:
        if completed.returncode == 0 and allow_empty_success:
            return {}
        if completed.returncode == 0:
            raise RuntimeError("Command returned empty or non-JSON output.")
        raise RuntimeError(stderr or stdout or "Command failed.")
    return parsed


def artifact_status_from_payload(payload: dict[str, Any]) -> str:
    if payload.get("error") is True:
        return "failed"
    status = payload.get("status")
    if isinstance(status, str) and status.strip():
        return status
    return "unknown"


def normalize_artifact_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.endswith(".audio"):
        return "audio"
    if text.endswith(".video"):
        return "video"
    return text


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def raw_artifact_status_name(value: Any) -> str:
    code = int_or_none(value)
    if code is not None:
        return ARTIFACT_STATUS_NAMES.get(code, "unknown")
    text = str(value or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    normalized = text.lower()
    if normalized == "processing":
        return "in_progress"
    return normalized or "unknown"


def raw_artifact_type_name(value: Any) -> str:
    code = int_or_none(value)
    if code is not None:
        return ARTIFACT_TYPE_NAMES.get(code, str(code))
    return normalize_artifact_type(value)


def prompt_fingerprint(prompt: str) -> str:
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def flatten_source_ids(value: Any) -> list[str]:
    source_ids: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str) and item.strip():
            source_ids.append(item.strip())
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return source_ids


def parse_raw_timestamp(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    seconds = int_or_none(value[0])
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OSError, ValueError):
        return None


def list_item(value: list[Any], index: int) -> Any:
    return value[index] if len(value) > index else None


def extract_video_request_metadata(raw_artifact: list[Any]) -> dict[str, Any]:
    video_block = list_item(raw_artifact, 8)
    if not isinstance(video_block, list):
        return {}
    request = list_item(video_block, 2)
    if not isinstance(request, list):
        return {}

    prompt = str(list_item(request, 2) or "")
    return {
        "source_ids": flatten_source_ids(list_item(request, 0)),
        "language": str(list_item(request, 1) or "").strip() or None,
        "prompt": prompt,
        "prompt_fingerprint": prompt_fingerprint(prompt),
        "raw_format_code": int_or_none(list_item(request, 4)),
        "raw_style_code": int_or_none(list_item(request, 5)),
    }


def artifact_payload_from_raw(raw_artifact: Any) -> dict[str, Any] | None:
    if not isinstance(raw_artifact, list) or not raw_artifact:
        return None

    type_name = raw_artifact_type_name(list_item(raw_artifact, 2))
    payload: dict[str, Any] = {
        "id": str(list_item(raw_artifact, 0) or "").strip(),
        "status": raw_artifact_status_name(list_item(raw_artifact, 4)),
        "type_id": type_name,
        "title": str(list_item(raw_artifact, 1) or "").strip(),
        "created_at": parse_raw_timestamp(list_item(raw_artifact, 15)),
        "raw_type_code": int_or_none(list_item(raw_artifact, 2)),
        "raw_status_code": int_or_none(list_item(raw_artifact, 4)),
    }
    if type_name == "video":
        payload.update(extract_video_request_metadata(raw_artifact))
    return payload if payload["id"] else None


def error_text_from_payload(payload: dict[str, Any]) -> str:
    parts = []
    for key in ("message", "error", "code"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " | ".join(parts)


def is_unrecoverable_error(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in UNRECOVERABLE_ERROR_HINTS)


def is_transient_error(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in TRANSIENT_ERROR_HINTS)


def trailing_history_count(
    artifact: "MediaArtifactResult",
    *,
    event: str,
    predicate: Any,
) -> int:
    count = 0
    for item in reversed(artifact.history):
        if item.get("event") != event:
            break
        if not predicate(item):
            break
        count += 1
    return count


def trailing_transient_poll_error_count(artifact: "MediaArtifactResult") -> int:
    def is_matching(item: dict[str, Any]) -> bool:
        message = str(item.get("message") or "").strip()
        return bool(message) and is_transient_error(message)

    return trailing_history_count(artifact, event="poll_error", predicate=is_matching)


def trailing_transient_generate_failure_count(artifact: "MediaArtifactResult") -> int:
    def is_matching(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "").strip()
        artifact_id = str(item.get("artifact_id") or "").strip()
        payload = item.get("payload")
        message = error_text_from_payload(payload) if isinstance(payload, dict) else ""
        return status == "failed" and not artifact_id and bool(message) and is_transient_error(message)

    return trailing_history_count(artifact, event="generate", predicate=is_matching)


def mark_terminal_artifact_failure(
    paper: "MediaPaperResult",
    artifact: "MediaArtifactResult",
    message: str,
) -> None:
    artifact.generation_status = "failed"
    artifact.last_error = message
    artifact.completed_at = artifact.completed_at or iso_now()
    artifact.history.append(
        {
            "event": "terminal_failure",
            "at": iso_now(),
            "message": message,
        }
    )
    paper.status = "failed"
    paper.error = message
    paper.completed_at = paper.completed_at or iso_now()


class MediaRunLockedError(RuntimeError):
    """Raised when a media run is already active for the same output root."""


def build_generate_command(
    *,
    media_type: str,
    notebook_id: str,
    source_id: str,
    prompt: str,
    language: str = DEFAULT_NOTEBOOKLM_LANGUAGE,
) -> list[str]:
    language = normalize_media_language(language)
    command = [
        notebooklm_binary(),
        "generate",
        media_type,
        "-n",
        notebook_id,
        "-s",
        source_id,
        "--language",
        language,
        "--json",
    ]
    if media_type == "audio":
        command.extend(["--format", "deep-dive", "--length", "short"])
    else:
        command.extend(["--format", VIDEO_FORMAT, "--style", VIDEO_STYLE])
    command.append(prompt)
    return command


def build_video_generate_params(
    *,
    notebook_id: str,
    source_id: str,
    prompt: str,
    language: str = DEFAULT_NOTEBOOKLM_LANGUAGE,
    raw_style_code: int = VIDEO_WHITEBOARD_STYLE_CODE,
) -> list[Any]:
    language = normalize_media_language(language)
    source_ids_triple = [[[source_id]]]
    source_ids_double = [[source_id]]
    return [
        [2],
        notebook_id,
        [
            None,
            None,
            3,
            source_ids_triple,
            None,
            None,
            None,
            None,
            [
                None,
                None,
                [
                    source_ids_double,
                    language,
                    prompt,
                    None,
                    VIDEO_FORMAT_CODE,
                    raw_style_code,
                ],
            ],
        ],
    ]


def generation_status_payload(status: Any) -> dict[str, Any]:
    if isinstance(status, dict):
        return dict(status)
    payload: dict[str, Any] = {
        "task_id": str(getattr(status, "task_id", "") or "").strip() or None,
        "status": str(getattr(status, "status", "") or "").strip() or "unknown",
    }
    error = getattr(status, "error", None)
    if error:
        payload["error"] = True
        payload["message"] = str(error)
    error_code = getattr(status, "error_code", None)
    if error_code:
        payload["code"] = str(error_code)
    return payload


async def _generate_video_via_api_async(
    *,
    notebook_id: str,
    source_id: str,
    prompt: str,
    language: str = DEFAULT_NOTEBOOKLM_LANGUAGE,
    raw_style_code: int = VIDEO_WHITEBOARD_STYLE_CODE,
) -> dict[str, Any]:
    language = normalize_media_language(language)
    params = build_video_generate_params(
        notebook_id=notebook_id,
        source_id=source_id,
        prompt=prompt,
        language=language,
        raw_style_code=raw_style_code,
    )
    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout()) as client:
        status = await client.artifacts._call_generate(notebook_id, params)
    payload = generation_status_payload(status)
    payload.update(
        {
            "generation_method": VIDEO_API_GENERATION_METHOD,
            "requested_language": language,
            "requested_format": VIDEO_FORMAT,
            "requested_style": VIDEO_STYLE,
            "requested_source_id": source_id,
            "prompt_fingerprint": prompt_fingerprint(prompt),
            "raw_format_code": VIDEO_FORMAT_CODE,
            "raw_style_code": raw_style_code,
        }
    )
    return payload


def generate_video_via_api(
    *,
    notebook_id: str,
    source_id: str,
    prompt: str,
    language: str = DEFAULT_NOTEBOOKLM_LANGUAGE,
    raw_style_code: int = VIDEO_WHITEBOARD_STYLE_CODE,
) -> dict[str, Any]:
    language = normalize_media_language(language)
    try:
        return asyncio.run(
            _generate_video_via_api_async(
                notebook_id=notebook_id,
                source_id=source_id,
                prompt=prompt,
                language=language,
                raw_style_code=raw_style_code,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "error": True,
            "status": "failed",
            "message": str(exc),
            "generation_method": VIDEO_API_GENERATION_METHOD,
            "requested_language": language,
            "requested_format": VIDEO_FORMAT,
            "requested_style": VIDEO_STYLE,
            "requested_source_id": source_id,
            "prompt_fingerprint": prompt_fingerprint(prompt),
            "raw_format_code": VIDEO_FORMAT_CODE,
            "raw_style_code": raw_style_code,
        }


def build_download_command(
    *,
    media_type: str,
    notebook_id: str,
    artifact_id: str,
    output_path: Path,
) -> list[str]:
    return [
        notebooklm_binary(),
        "download",
        media_type,
        "-n",
        notebook_id,
        "-a",
        artifact_id,
        "--json",
        "--force",
        str(output_path),
    ]


@dataclass
class MediaArtifactResult:
    media_type: str
    prompt: str
    artifact_id: str | None = None
    generation_status: str = "pending"
    requested_language: str | None = None
    requested_format: str | None = None
    requested_style: str | None = None
    requested_source_id: str | None = None
    prompt_fingerprint: str | None = None
    raw_format_code: int | None = None
    raw_style_code: int | None = None
    generation_method: str | None = None
    fallback_used: bool = False
    style_warning: str | None = None
    attempts: int = 0
    wait_attempts: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    download_path: str | None = None
    last_error: str | None = None
    last_retry_delay_seconds: int | None = None
    missing_polls: int = 0
    missing_notebook_polls: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MediaPaperResult:
    title: str
    notebook_id: str | None
    source_id: str | None
    work_dir: str | None
    processing_status: str
    media_language: str = DEFAULT_NOTEBOOKLM_LANGUAGE
    status: str = "pending"
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    media_dir: str | None = None
    audio: MediaArtifactResult = field(default_factory=lambda: MediaArtifactResult(media_type="audio", prompt=""))
    video: MediaArtifactResult = field(default_factory=lambda: MediaArtifactResult(media_type="video", prompt=""))


def load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else None


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_run_lock(out_root: Path, manifest_path: Path) -> Path:
    lock_path = out_root / LOCK_FILE_NAME
    current_pid = os.getpid()
    existing = load_optional_json(lock_path)
    if existing:
        existing_pid = existing.get("pid")
        if isinstance(existing_pid, int) and existing_pid != current_pid and process_is_alive(existing_pid):
            raise MediaRunLockedError(
                f"Media generation is already running for {manifest_path.parent} under pid {existing_pid}."
            )
        lock_path.unlink(missing_ok=True)

    write_json(
        lock_path,
        {
            "pid": current_pid,
            "manifest_path": str(manifest_path),
            "started_at": iso_now(),
        },
    )
    return lock_path


def release_run_lock(lock_path: Path | None) -> None:
    if lock_path is not None:
        lock_path.unlink(missing_ok=True)


def default_download_path(paper: MediaPaperResult, artifact: MediaArtifactResult) -> Path:
    if not paper.media_dir:
        raise RuntimeError("Paper media_dir is missing.")
    extension = ".mp3" if artifact.media_type == "audio" else ".mp4"
    return Path(paper.media_dir) / f"{artifact.media_type}{extension}"


def accepted_video_style_codes(artifact: MediaArtifactResult) -> set[int]:
    codes = {VIDEO_WHITEBOARD_STYLE_CODE}
    if artifact.fallback_used:
        codes.add(VIDEO_OFFICIAL_WHITEBOARD_STYLE_CODE)
    return codes


def current_media_language(paper: MediaPaperResult) -> str:
    return normalize_media_language(paper.media_language)


def artifact_language_matches_current_request(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
) -> bool:
    language = current_media_language(paper)
    if artifact.requested_language == language:
        return True
    return (
        artifact.media_type == "audio"
        and language == LEGACY_UNTAGGED_AUDIO_LANGUAGE
        and artifact.requested_language is None
    )


def record_audio_generation_options(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
) -> None:
    if artifact.media_type == "audio":
        artifact.requested_language = current_media_language(paper)


def record_video_generation_options(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    *,
    raw_style_code: int = VIDEO_WHITEBOARD_STYLE_CODE,
    generation_method: str = VIDEO_API_GENERATION_METHOD,
    fallback_used: bool = False,
) -> None:
    if artifact.media_type != "video":
        return
    artifact.requested_language = current_media_language(paper)
    artifact.requested_format = VIDEO_FORMAT
    artifact.requested_style = VIDEO_STYLE
    artifact.requested_source_id = paper.source_id
    artifact.prompt_fingerprint = prompt_fingerprint(artifact.prompt)
    artifact.raw_format_code = VIDEO_FORMAT_CODE
    artifact.raw_style_code = raw_style_code
    artifact.generation_method = generation_method
    artifact.fallback_used = fallback_used
    artifact.style_warning = None
    if raw_style_code != VIDEO_WHITEBOARD_STYLE_CODE:
        artifact.style_warning = (
            f"Video generated with fallback style code {raw_style_code}; "
            f"expected observed whiteboard style code {VIDEO_WHITEBOARD_STYLE_CODE}."
        )


def video_saved_options_match_current_request(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
) -> bool:
    if artifact.media_type != "video":
        return True
    return (
        artifact_language_matches_current_request(paper, artifact)
        and artifact.requested_format == VIDEO_FORMAT
        and artifact.requested_style == VIDEO_STYLE
        and artifact.requested_source_id == paper.source_id
        and artifact.prompt_fingerprint == prompt_fingerprint(artifact.prompt)
        and artifact.raw_format_code == VIDEO_FORMAT_CODE
        and artifact.raw_style_code in accepted_video_style_codes(artifact)
    )


def artifact_saved_options_match_current_request(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
) -> bool:
    if not artifact_language_matches_current_request(paper, artifact):
        return False
    if artifact.media_type == "video":
        return video_saved_options_match_current_request(paper, artifact)
    return True


def payload_has_video_request_metadata(payload: dict[str, Any]) -> bool:
    return any(
        payload.get(key) is not None
        for key in ("language", "prompt_fingerprint", "raw_format_code", "raw_style_code")
    ) or bool(payload.get("source_ids"))


def video_payload_matches_current_request(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    payload: dict[str, Any],
) -> bool:
    if artifact.media_type != "video":
        return True
    if normalize_artifact_type(payload.get("type_id")) != "video":
        return False

    if not payload_has_video_request_metadata(payload):
        return bool(
            artifact.artifact_id
            and payload.get("id") == artifact.artifact_id
            and artifact.attempts > 0
            and artifact_language_matches_current_request(paper, artifact)
        )

    source_ids = payload.get("source_ids")
    if not isinstance(source_ids, list) or paper.source_id not in source_ids:
        return False
    return (
        payload.get("language") == current_media_language(paper)
        and payload.get("prompt_fingerprint") == prompt_fingerprint(artifact.prompt)
        and int_or_none(payload.get("raw_format_code")) == VIDEO_FORMAT_CODE
        and int_or_none(payload.get("raw_style_code")) in accepted_video_style_codes(artifact)
    )


def audio_payload_matches_current_request(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    payload: dict[str, Any],
) -> bool:
    if normalize_artifact_type(payload.get("type_id")) != "audio":
        return False
    payload_language = payload.get("language")
    if payload_language is not None:
        return payload_language == current_media_language(paper)
    if artifact.artifact_id and payload.get("id") == artifact.artifact_id:
        return artifact_language_matches_current_request(paper, artifact)
    return current_media_language(paper) == LEGACY_UNTAGGED_AUDIO_LANGUAGE


def artifact_payload_matches_current_request(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    payload: dict[str, Any],
) -> bool:
    if artifact.media_type == "video":
        return video_payload_matches_current_request(paper, artifact, payload)
    if artifact.media_type == "audio":
        return audio_payload_matches_current_request(paper, artifact, payload)
    return normalize_artifact_type(payload.get("type_id")) == artifact.media_type


def apply_video_payload_metadata(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    payload: dict[str, Any],
) -> None:
    if artifact.media_type != "video":
        return
    source_ids = payload.get("source_ids")
    if isinstance(source_ids, list) and source_ids:
        artifact.requested_source_id = str(source_ids[0])
    else:
        artifact.requested_source_id = paper.source_id
    artifact.requested_language = str(payload.get("language") or current_media_language(paper))
    artifact.requested_format = VIDEO_FORMAT
    artifact.requested_style = VIDEO_STYLE
    artifact.prompt_fingerprint = str(
        payload.get("prompt_fingerprint") or prompt_fingerprint(artifact.prompt)
    )
    artifact.raw_format_code = int_or_none(payload.get("raw_format_code")) or VIDEO_FORMAT_CODE
    style_code = int_or_none(payload.get("raw_style_code"))
    if style_code is not None:
        artifact.raw_style_code = style_code
    artifact.style_warning = None
    if artifact.raw_style_code is not None and artifact.raw_style_code != VIDEO_WHITEBOARD_STYLE_CODE:
        artifact.style_warning = (
            f"Video artifact reports style code {artifact.raw_style_code}; "
            f"expected observed whiteboard style code {VIDEO_WHITEBOARD_STYLE_CODE}."
        )


def archive_stale_media_file(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    output_path: Path,
    reason: str,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = output_path.with_name(f"{output_path.stem}.stale-{stamp}{output_path.suffix}")
    counter = 1
    while archive_path.exists():
        archive_path = output_path.with_name(
            f"{output_path.stem}.stale-{stamp}-{counter}{output_path.suffix}"
        )
        counter += 1
    output_path.rename(archive_path)
    artifact.history.append(
        {
            "event": "archive_local_file",
            "at": iso_now(),
            "path": str(output_path),
            "archive_path": str(archive_path),
            "reason": reason,
        }
    )
    return archive_path


def reset_mismatched_artifact_state(paper: MediaPaperResult, artifact: MediaArtifactResult) -> None:
    output_path = default_download_path(paper, artifact)
    has_saved_state = any(
        [
            artifact.artifact_id,
            artifact.download_path,
            artifact.attempts,
            artifact.requested_language,
            artifact.prompt_fingerprint,
            artifact.raw_format_code is not None,
            artifact.raw_style_code is not None,
            output_path.exists(),
        ]
    )
    if not has_saved_state or artifact_saved_options_match_current_request(paper, artifact):
        return

    reason = (
        f"{artifact.media_type.capitalize()} artifact does not match the current "
        "media language or generation options."
    )
    if output_path.exists() and output_path.stat().st_size > 0:
        archive_stale_media_file(paper, artifact, output_path, reason)
    artifact.artifact_id = None
    artifact.generation_status = "pending"
    artifact.completed_at = None
    artifact.download_path = None
    artifact.last_error = reason
    artifact.requested_language = None
    artifact.requested_format = None
    artifact.requested_style = None
    artifact.requested_source_id = None
    artifact.prompt_fingerprint = None
    artifact.raw_format_code = None
    artifact.raw_style_code = None
    artifact.generation_method = None
    artifact.fallback_used = False
    artifact.style_warning = None
    artifact.attempts = 0
    artifact.wait_attempts = 0
    artifact.missing_polls = 0
    artifact.missing_notebook_polls = 0
    artifact.history.append({"event": "reset_stale_media", "at": iso_now(), "message": reason})


def reset_mismatched_video_state(paper: MediaPaperResult, artifact: MediaArtifactResult) -> None:
    if artifact.media_type == "video":
        reset_mismatched_artifact_state(paper, artifact)


def sync_artifact_from_disk(paper: MediaPaperResult, artifact: MediaArtifactResult) -> None:
    output_path = default_download_path(paper, artifact)
    if output_path.exists() and output_path.stat().st_size > 0:
        if not artifact_saved_options_match_current_request(paper, artifact):
            reset_mismatched_artifact_state(paper, artifact)
            return
        if artifact.requested_language is None:
            artifact.requested_language = current_media_language(paper)
        artifact.download_path = str(output_path)
        artifact.generation_status = "completed"
        artifact.completed_at = artifact.completed_at or iso_now()
        artifact.last_error = None


def hydrate_artifact_from_saved(
    artifact: MediaArtifactResult,
    saved: dict[str, Any] | None,
) -> MediaArtifactResult:
    if not isinstance(saved, dict):
        return artifact

    return MediaArtifactResult(
        media_type=artifact.media_type,
        prompt=str(saved.get("prompt") or artifact.prompt),
        artifact_id=str(saved.get("artifact_id")).strip() if saved.get("artifact_id") else None,
        generation_status=str(saved.get("generation_status") or artifact.generation_status),
        requested_language=(
            str(saved.get("requested_language")).strip() if saved.get("requested_language") else None
        ),
        requested_format=str(saved.get("requested_format")).strip() if saved.get("requested_format") else None,
        requested_style=str(saved.get("requested_style")).strip() if saved.get("requested_style") else None,
        requested_source_id=(
            str(saved.get("requested_source_id")).strip() if saved.get("requested_source_id") else None
        ),
        prompt_fingerprint=(
            str(saved.get("prompt_fingerprint")).strip() if saved.get("prompt_fingerprint") else None
        ),
        raw_format_code=int_or_none(saved.get("raw_format_code")),
        raw_style_code=int_or_none(saved.get("raw_style_code")),
        generation_method=(
            str(saved.get("generation_method")).strip() if saved.get("generation_method") else None
        ),
        fallback_used=bool(saved.get("fallback_used")),
        style_warning=str(saved.get("style_warning")).strip() if saved.get("style_warning") else None,
        attempts=int(saved.get("attempts") or 0),
        wait_attempts=int(saved.get("wait_attempts") or 0),
        started_at=str(saved.get("started_at")).strip() if saved.get("started_at") else None,
        completed_at=str(saved.get("completed_at")).strip() if saved.get("completed_at") else None,
        download_path=str(saved.get("download_path")).strip() if saved.get("download_path") else None,
        last_error=str(saved.get("last_error")).strip() if saved.get("last_error") else None,
        last_retry_delay_seconds=(
            int(saved.get("last_retry_delay_seconds"))
            if saved.get("last_retry_delay_seconds") is not None
            else None
        ),
        missing_polls=int(saved.get("missing_polls") or 0),
        missing_notebook_polls=int(saved.get("missing_notebook_polls") or 0),
        history=list(saved.get("history") or []),
    )


def hydrate_paper_from_saved(paper: MediaPaperResult) -> MediaPaperResult:
    if not paper.media_dir:
        return paper
    result_path = Path(paper.media_dir) / "result.json"
    saved = load_optional_json(result_path)
    if not isinstance(saved, dict):
        sync_artifact_from_disk(paper, paper.audio)
        sync_artifact_from_disk(paper, paper.video)
        return paper

    saved_notebook_id = str(saved.get("notebook_id") or "").strip() or None
    saved_source_id = str(saved.get("source_id") or "").strip() or None
    if saved_notebook_id != paper.notebook_id or saved_source_id != paper.source_id:
        sync_artifact_from_disk(paper, paper.audio)
        sync_artifact_from_disk(paper, paper.video)
        return paper

    paper.status = str(saved.get("status") or paper.status)
    paper.error = str(saved.get("error")).strip() if saved.get("error") else None
    paper.started_at = str(saved.get("started_at")).strip() if saved.get("started_at") else paper.started_at
    paper.completed_at = (
        str(saved.get("completed_at")).strip() if saved.get("completed_at") else paper.completed_at
    )
    paper.audio = hydrate_artifact_from_saved(paper.audio, saved.get("audio"))
    paper.video = hydrate_artifact_from_saved(paper.video, saved.get("video"))
    sync_artifact_from_disk(paper, paper.audio)
    sync_artifact_from_disk(paper, paper.video)
    return paper


def artifact_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    created_at = item.get("created_at")
    if isinstance(created_at, str):
        return (created_at, str(item.get("id") or ""))
    return ("", str(item.get("id") or ""))


def normalize_plan_entries(media_plan: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw_entries = media_plan.get("papers")
    if not isinstance(raw_entries, list):
        raise SystemExit("Media plan is missing a papers list.")

    entries: dict[str, dict[str, str]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        title = str(raw_entry.get("title") or "").strip()
        audio_prompt = str(raw_entry.get("audio_prompt") or "").strip()
        video_prompt = str(raw_entry.get("video_prompt") or "").strip()
        if not title:
            raise SystemExit("Media plan contains a paper without a title.")
        if not audio_prompt or not video_prompt:
            raise SystemExit(f"Media plan is missing prompts for paper: {title}")
        if title in entries:
            raise SystemExit(f"Media plan contains duplicate paper title: {title}")
        entries[title] = {
            "audio_prompt": audio_prompt,
            "video_prompt": video_prompt,
        }
    return entries


def load_media_plan(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"Media plan not found: {path}")
    return normalize_plan_entries(load_json(path))


def validate_media_plan(
    plan_entries: dict[str, dict[str, str]],
    processed_entries: list[dict[str, Any]],
) -> None:
    successful_titles = {
        str(entry.get("title") or "").strip()
        for entry in processed_entries
        if isinstance(entry, dict) and entry.get("status") == "ok" and str(entry.get("title") or "").strip()
    }
    plan_titles = set(plan_entries)
    missing = sorted(successful_titles - plan_titles)
    unknown = sorted(plan_titles - successful_titles)
    if missing:
        raise SystemExit("Media plan is missing successful processed paper(s): " + "; ".join(missing))
    if unknown:
        raise SystemExit("Media plan contains unknown paper(s): " + "; ".join(unknown))
    for title in sorted(plan_titles & successful_titles):
        entry = plan_entries[title]
        if not str(entry.get("audio_prompt") or "").strip() or not str(entry.get("video_prompt") or "").strip():
            raise SystemExit(
                "Media plan entry must include non-empty audio_prompt and video_prompt: "
                + title
            )


def paper_result_from_manifest(
    entry: dict[str, Any],
    out_root: Path,
    plan_entry: dict[str, str] | None,
    media_language: str = DEFAULT_NOTEBOOKLM_LANGUAGE,
) -> MediaPaperResult:
    title = str(entry.get("title") or "").strip() or "Untitled paper"
    notebook_id = str(entry.get("notebook_id") or "").strip() or None
    source_id = str(entry.get("source_id") or "").strip() or None
    work_dir_value = str(entry.get("work_dir") or "").strip()
    work_dir = Path(work_dir_value).expanduser() if work_dir_value else None
    media_dir = (work_dir / "media") if work_dir else (out_root / slugify(title) / "media")
    paper = MediaPaperResult(
        title=title,
        notebook_id=notebook_id,
        source_id=source_id,
        work_dir=str(work_dir) if work_dir else None,
        processing_status=str(entry.get("status") or "").strip() or "unknown",
        media_language=normalize_media_language(media_language),
        media_dir=str(media_dir),
    )
    if paper.processing_status == "ok":
        if plan_entry is None:
            raise SystemExit(f"Media plan is missing successful processed paper: {title}")
        paper.audio.prompt = plan_entry["audio_prompt"]
        paper.video.prompt = plan_entry["video_prompt"]
    hydrated = hydrate_paper_from_saved(paper)
    if paper.processing_status == "ok" and plan_entry is not None:
        hydrated.audio.prompt = plan_entry["audio_prompt"]
        hydrated.video.prompt = plan_entry["video_prompt"]
        reset_mismatched_artifact_state(hydrated, hydrated.audio)
        reset_mismatched_artifact_state(hydrated, hydrated.video)
        sync_artifact_from_disk(hydrated, hydrated.audio)
        sync_artifact_from_disk(hydrated, hydrated.video)
    return hydrated


def submit_generation(paper: MediaPaperResult, artifact: MediaArtifactResult) -> bool:
    if not paper.notebook_id or not paper.source_id:
        mark_terminal_artifact_failure(
            paper,
            artifact,
            "Processed manifest entry is missing notebook_id or source_id.",
        )
        return False

    artifact.attempts += 1
    artifact.started_at = artifact.started_at or iso_now()
    language = current_media_language(paper)
    if artifact.media_type == "video":
        record_video_generation_options(paper, artifact)
        payload = generate_video_via_api(
            notebook_id=paper.notebook_id,
            source_id=paper.source_id,
            prompt=artifact.prompt,
            language=language,
        )
        status = artifact_status_from_payload(payload)
        message = error_text_from_payload(payload)
        if (
            status == "failed"
            and not (payload.get("task_id") or payload.get("artifact_id"))
            and message
            and not is_transient_error(message)
            and not is_unrecoverable_error(message)
        ):
            fallback_payload = run_json_command(
                build_generate_command(
                    media_type=artifact.media_type,
                    notebook_id=paper.notebook_id,
                    source_id=paper.source_id,
                    prompt=artifact.prompt,
                    language=language,
                ),
                allow_failure_json=True,
            )
            fallback_payload.setdefault("fallback_reason", payload)
            fallback_payload.update(
                {
                    "generation_method": VIDEO_OFFICIAL_FALLBACK_METHOD,
                    "requested_language": language,
                    "requested_format": VIDEO_FORMAT,
                    "requested_style": VIDEO_STYLE,
                    "requested_source_id": paper.source_id,
                    "prompt_fingerprint": prompt_fingerprint(artifact.prompt),
                    "raw_format_code": VIDEO_FORMAT_CODE,
                    "raw_style_code": VIDEO_OFFICIAL_WHITEBOARD_STYLE_CODE,
                    "fallback_used": True,
                }
            )
            record_video_generation_options(
                paper,
                artifact,
                raw_style_code=VIDEO_OFFICIAL_WHITEBOARD_STYLE_CODE,
                generation_method=VIDEO_OFFICIAL_FALLBACK_METHOD,
                fallback_used=True,
            )
            payload = fallback_payload
    else:
        record_audio_generation_options(paper, artifact)
        command = build_generate_command(
            media_type=artifact.media_type,
            notebook_id=paper.notebook_id,
            source_id=paper.source_id,
            prompt=artifact.prompt,
            language=language,
        )
        payload = run_json_command(command, allow_failure_json=True)
    status = artifact_status_from_payload(payload)
    artifact_id = payload.get("task_id") or payload.get("artifact_id")
    artifact.artifact_id = str(artifact_id).strip() if artifact_id else None
    artifact.generation_status = status
    artifact.last_error = error_text_from_payload(payload) or None
    if artifact.media_type == "video":
        artifact.raw_format_code = int_or_none(payload.get("raw_format_code")) or artifact.raw_format_code
        artifact.raw_style_code = int_or_none(payload.get("raw_style_code")) or artifact.raw_style_code
        artifact.generation_method = str(payload.get("generation_method") or artifact.generation_method)
        artifact.fallback_used = bool(payload.get("fallback_used") or artifact.fallback_used)
        if artifact.raw_style_code != VIDEO_WHITEBOARD_STYLE_CODE:
            artifact.style_warning = (
                f"Video generated with fallback style code {artifact.raw_style_code}; "
                f"expected observed whiteboard style code {VIDEO_WHITEBOARD_STYLE_CODE}."
            )
    artifact.missing_polls = 0
    artifact.missing_notebook_polls = 0
    artifact.history.append(
        {
            "event": "generate",
            "attempt": artifact.attempts,
            "at": iso_now(),
            "status": status,
            "artifact_id": artifact.artifact_id,
            "payload": payload,
        }
    )
    if status == "failed" and artifact.last_error and is_unrecoverable_error(artifact.last_error):
        mark_terminal_artifact_failure(paper, artifact, artifact.last_error)
        return False
    if (
        status == "failed"
        and not artifact.artifact_id
        and artifact.last_error
        and is_transient_error(artifact.last_error)
        and trailing_transient_generate_failure_count(artifact) >= DEFAULT_MAX_TRANSIENT_GENERATION_FAILURES
    ):
        mark_terminal_artifact_failure(
            paper,
            artifact,
            f"{artifact.media_type.capitalize()} generation failed after "
            f"{DEFAULT_MAX_TRANSIENT_GENERATION_FAILURES} transient attempt(s): {artifact.last_error}",
        )
        return False
    return True


def refresh_artifact_status(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    active_notebook_ids: set[str],
    missing_notebook_ids: set[str],
    notebook_errors: dict[str, str],
    artifact_errors: dict[str, str],
    artifact_cache: dict[str, list[dict[str, Any]]],
) -> str:
    if not paper.notebook_id or not artifact.artifact_id:
        artifact.generation_status = "failed"
        artifact.last_error = "Missing notebook_id or artifact_id while checking status."
        return "failed"

    artifact.wait_attempts += 1
    notebook_error = notebook_errors.get(paper.notebook_id)
    if notebook_error:
        artifact.generation_status = "unknown"
        artifact.last_error = notebook_error
        artifact.history.append(
            {
                "event": "poll_error",
                "attempt": artifact.wait_attempts,
                "at": iso_now(),
                "message": notebook_error,
            }
        )
        if trailing_transient_poll_error_count(artifact) >= DEFAULT_MAX_TRANSIENT_POLL_ERRORS:
            mark_terminal_artifact_failure(
                paper,
                artifact,
                f"{artifact.media_type.capitalize()} status polling failed after "
                f"{DEFAULT_MAX_TRANSIENT_POLL_ERRORS} transient error(s): {notebook_error}",
            )
            return "failed"
        return "unknown"

    if paper.notebook_id in missing_notebook_ids or paper.notebook_id not in active_notebook_ids:
        artifact.missing_notebook_polls += 1
        if artifact.missing_notebook_polls >= DEFAULT_MISSING_NOTEBOOK_POLLS_BEFORE_FAIL:
            artifact.generation_status = "failed"
            artifact.last_error = f"NotebookLM notebook {paper.notebook_id} is no longer available."
            artifact.history.append(
                {
                    "event": "poll",
                    "attempt": artifact.wait_attempts,
                    "at": iso_now(),
                    "status": "failed",
                    "payload": {
                        "notebook_id": paper.notebook_id,
                        "status": "missing_notebook",
                        "missing_notebook_polls": artifact.missing_notebook_polls,
                    },
                }
            )
            return "failed"

        artifact.generation_status = "unknown"
        artifact.last_error = f"NotebookLM notebook {paper.notebook_id} was not visible in the latest snapshot."
        artifact.history.append(
            {
                "event": "poll",
                "attempt": artifact.wait_attempts,
                "at": iso_now(),
                "status": "missing_notebook_retrying",
                "payload": {
                    "notebook_id": paper.notebook_id,
                    "status": "missing_notebook",
                    "missing_notebook_polls": artifact.missing_notebook_polls,
                },
            }
        )
        return "unknown"

    artifact.missing_notebook_polls = 0

    artifact_error = artifact_errors.get(paper.notebook_id)
    if artifact_error:
        artifact.generation_status = "unknown"
        artifact.last_error = artifact_error
        artifact.history.append(
            {
                "event": "poll_error",
                "attempt": artifact.wait_attempts,
                "at": iso_now(),
                "message": artifact_error,
            }
        )
        if trailing_transient_poll_error_count(artifact) >= DEFAULT_MAX_TRANSIENT_POLL_ERRORS:
            mark_terminal_artifact_failure(
                paper,
                artifact,
                f"{artifact.media_type.capitalize()} status polling failed after "
                f"{DEFAULT_MAX_TRANSIENT_POLL_ERRORS} transient error(s): {artifact_error}",
            )
            return "failed"
        return "unknown"

    notebook_artifacts = artifact_cache.get(paper.notebook_id, [])
    payload = next((item for item in notebook_artifacts if item.get("id") == artifact.artifact_id), None)
    if payload is not None and not artifact_payload_matches_current_request(paper, artifact, payload):
        artifact.history.append(
            {
                "event": "reject_artifact",
                "at": iso_now(),
                "artifact_id": artifact.artifact_id,
                "message": "Artifact metadata does not match current media generation options.",
                "payload": payload,
            }
        )
        payload = None
    if payload is None:
        candidates = [
            item
            for item in notebook_artifacts
            if normalize_artifact_type(item.get("type_id")) == artifact.media_type
            and artifact_payload_matches_current_request(paper, artifact, item)
        ]
        if candidates:
            payload = sorted(candidates, key=artifact_sort_key, reverse=True)[0]
            previous_artifact_id = artifact.artifact_id
            artifact.artifact_id = str(payload.get("id") or artifact.artifact_id)
            artifact.missing_polls = 0
            apply_video_payload_metadata(paper, artifact, payload)
            artifact.history.append(
                {
                    "event": "adopt_artifact",
                    "at": iso_now(),
                    "previous_artifact_id": previous_artifact_id,
                    "artifact_id": artifact.artifact_id,
                    "payload": payload,
                }
            )
        else:
            artifact.missing_polls += 1
            artifact.generation_status = "missing"
            artifact.last_error = f"Artifact {artifact.artifact_id} is not present in notebook {paper.notebook_id}."
            artifact.history.append(
                {
                    "event": "poll",
                    "attempt": artifact.wait_attempts,
                    "at": iso_now(),
                    "status": "missing",
                    "payload": {
                        "artifact_id": artifact.artifact_id,
                        "status": "missing",
                        "missing_polls": artifact.missing_polls,
                    },
                }
            )
            return "missing"

    status = str(payload.get("status") or "").strip() or "pending"
    apply_video_payload_metadata(paper, artifact, payload)
    if artifact.media_type == "audio" and payload.get("language") is not None:
        artifact.requested_language = str(payload.get("language"))

    artifact.generation_status = status
    artifact.last_error = error_text_from_payload(payload) or None
    artifact.missing_polls = 0
    artifact.missing_notebook_polls = 0
    artifact.history.append(
        {
            "event": "poll",
            "attempt": artifact.wait_attempts,
            "at": iso_now(),
            "status": status,
            "payload": payload,
        }
    )
    if status == "completed":
        artifact.completed_at = iso_now()
        artifact.last_error = None
    return status


def download_artifact(paper: MediaPaperResult, artifact: MediaArtifactResult) -> None:
    if not paper.notebook_id or not paper.media_dir or not artifact.artifact_id:
        raise RuntimeError("Missing notebook_id, media_dir, or artifact_id for download.")
    if not artifact_saved_options_match_current_request(paper, artifact):
        raise RuntimeError(
            f"{artifact.media_type.capitalize()} artifact metadata does not match "
            "the current media generation options."
        )

    output_path = default_download_path(paper, artifact)
    ensure_dir(output_path.parent)
    if output_path.exists() and output_path.stat().st_size > 0:
        sync_artifact_from_disk(paper, artifact)
        if artifact.download_path and Path(artifact.download_path).exists():
            return

    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.unlink(missing_ok=True)

    payload = download_artifact_via_api(
        notebook_id=paper.notebook_id,
        media_type=artifact.media_type,
        artifact_id=artifact.artifact_id,
        output_path=output_path,
    )
    if artifact.requested_language is None:
        artifact.requested_language = current_media_language(paper)
    artifact.download_path = str(output_path)
    artifact.generation_status = "completed"
    artifact.completed_at = artifact.completed_at or iso_now()
    artifact.last_error = None
    artifact.history.append(
        {
            "event": "download",
            "at": iso_now(),
            "status": "downloaded",
            "payload": payload,
        }
    )


def finalize_paper_status(paper: MediaPaperResult) -> None:
    if paper.error and paper.status == "failed":
        paper.completed_at = paper.completed_at or iso_now()
        return
    artifacts = [paper.audio, paper.video]
    if all(item.generation_status == "completed" and item.download_path for item in artifacts):
        paper.status = "ok"
        paper.completed_at = iso_now()
        return
    if any(
        item.generation_status == "failed"
        and item.last_error
        and is_unrecoverable_error(item.last_error)
        for item in artifacts
    ):
        paper.status = "failed"
        if not paper.error:
            paper.error = next(
                item.last_error
                for item in artifacts
                if item.generation_status == "failed" and item.last_error
            )
        paper.completed_at = iso_now()
        return
    paper.status = "pending"


def persist_paper_result(paper: MediaPaperResult) -> None:
    if not paper.media_dir:
        return
    write_json(Path(paper.media_dir) / "result.json", asdict(paper))


def eligible_papers(
    manifest_payload: dict[str, Any],
    *,
    out_root: Path,
    limit: int | None,
    plan_entries: dict[str, dict[str, str]],
    media_language: str = DEFAULT_NOTEBOOKLM_LANGUAGE,
) -> list[MediaPaperResult]:
    raw_results = manifest_payload.get("results")
    if not isinstance(raw_results, list):
        raise SystemExit("Processed manifest is missing results.")

    selected: list[MediaPaperResult] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        selected.append(
            paper_result_from_manifest(
                entry,
                out_root,
                plan_entries.get(title),
                media_language=media_language,
            )
        )

    if limit is not None:
        selected = selected[:limit]
    return selected


async def _snapshot_notebook_state_async(
    notebook_ids: list[str],
    timeout: float,
) -> dict[str, Any]:
    if not notebook_ids:
        return {
            "active_notebook_ids": set(),
            "missing_notebook_ids": set(),
            "notebook_errors": {},
            "artifact_errors": {},
            "artifacts": {},
        }

    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout(timeout)) as client:
        active_notebook_ids: set[str] = set()
        missing_notebook_ids: set[str] = set()
        notebook_errors: dict[str, str] = {}
        artifact_errors: dict[str, str] = {}
        artifact_map: dict[str, list[dict[str, Any]]] = {}
        for notebook_id in notebook_ids:
            try:
                await client.notebooks.get(notebook_id)
                active_notebook_ids.add(notebook_id)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if is_unrecoverable_error(message):
                    missing_notebook_ids.add(notebook_id)
                else:
                    notebook_errors[notebook_id] = message
                artifact_map[notebook_id] = []
                continue
            try:
                raw_artifacts = await client.artifacts._list_raw(notebook_id)
                artifact_map[notebook_id] = [
                    payload
                    for item in raw_artifacts
                    if (payload := artifact_payload_from_raw(item)) is not None
                ]
            except Exception as exc:  # noqa: BLE001
                artifact_errors[notebook_id] = str(exc)
                artifact_map[notebook_id] = [
                    {
                        "id": "",
                        "status": "unknown",
                        "type_id": "",
                        "title": "",
                        "created_at": None,
                        "error": str(exc),
                    }
                ]
        return {
            "active_notebook_ids": active_notebook_ids,
            "missing_notebook_ids": missing_notebook_ids,
            "notebook_errors": notebook_errors,
            "artifact_errors": artifact_errors,
            "artifacts": artifact_map,
        }


def snapshot_notebook_state(
    notebook_ids: list[str],
    timeout: float = DEFAULT_NOTEBOOKLM_RPC_TIMEOUT,
) -> dict[str, Any]:
    return asyncio.run(_snapshot_notebook_state_async(notebook_ids, timeout))


async def _download_artifact_via_api_async(
    *,
    notebook_id: str,
    media_type: str,
    artifact_id: str,
    output_path: Path,
) -> dict[str, Any]:
    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout()) as client:
        if media_type == "audio":
            saved_path = await client.artifacts.download_audio(
                notebook_id,
                str(output_path),
                artifact_id=artifact_id,
            )
        else:
            saved_path = await client.artifacts.download_video(
                notebook_id,
                str(output_path),
                artifact_id=artifact_id,
            )
    return {"artifact_id": artifact_id, "path": saved_path, "status": "downloaded"}


def download_artifact_via_api(
    *,
    notebook_id: str,
    media_type: str,
    artifact_id: str,
    output_path: Path,
) -> dict[str, Any]:
    try:
        return asyncio.run(
            _download_artifact_via_api_async(
                notebook_id=notebook_id,
                media_type=media_type,
                artifact_id=artifact_id,
                output_path=output_path,
            )
        )
    except (ArtifactNotReadyError, ArtifactDownloadError) as exc:
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def adopt_latest_matching_artifact(
    paper: MediaPaperResult,
    artifact: MediaArtifactResult,
    notebook_artifacts: list[dict[str, Any]],
) -> bool:
    candidates = [
        item
        for item in notebook_artifacts
        if normalize_artifact_type(item.get("type_id")) == artifact.media_type
        and artifact_payload_matches_current_request(paper, artifact, item)
    ]
    if not candidates:
        return False
    payload = sorted(candidates, key=artifact_sort_key, reverse=True)[0]
    artifact.artifact_id = str(payload.get("id") or artifact.artifact_id)
    artifact.generation_status = str(payload.get("status") or artifact.generation_status or "pending")
    artifact.last_error = error_text_from_payload(payload) or None
    artifact.missing_polls = 0
    artifact.missing_notebook_polls = 0
    apply_video_payload_metadata(paper, artifact, payload)
    if artifact.media_type == "audio" and payload.get("language") is not None:
        artifact.requested_language = str(payload.get("language"))
    artifact.history.append(
        {
            "event": "adopt_artifact",
            "at": iso_now(),
            "artifact_id": artifact.artifact_id,
            "payload": payload,
        }
    )
    return True


def process_media(
    papers: list[MediaPaperResult],
    *,
    poll_interval: int,
) -> None:
    pending: list[tuple[MediaPaperResult, MediaArtifactResult]] = []
    consecutive_snapshot_errors = 0

    for paper in papers:
        paper.started_at = iso_now()
        if paper.processing_status != "ok":
            paper.status = "failed"
            paper.error = f"Skipping media generation because paper processing status is {paper.processing_status!r}."
            finalize_paper_status(paper)
            persist_paper_result(paper)
            continue

        # A rerun should retry incomplete media even if an earlier run marked the paper failed.
        paper.status = "pending"
        paper.error = None
        paper.completed_at = None

        for artifact in (paper.audio, paper.video):
            reset_mismatched_artifact_state(paper, artifact)
            sync_artifact_from_disk(paper, artifact)
            if artifact.download_path and Path(artifact.download_path).exists():
                artifact.generation_status = "completed"
                continue

            if artifact.artifact_id and artifact.generation_status in {"pending", "in_progress", "unknown", "completed"}:
                pending.append((paper, artifact))
                continue

            accepted = submit_generation(paper, artifact)
            if paper.status == "failed" and paper.error:
                continue
            if accepted or (artifact.last_error and is_transient_error(artifact.last_error)):
                pending.append((paper, artifact))
            else:
                paper.status = "failed"
                if not paper.error:
                    paper.error = artifact.last_error or "Artifact generation failed."
        finalize_paper_status(paper)
        persist_paper_result(paper)

    while pending:
        next_pending: list[tuple[MediaPaperResult, MediaArtifactResult]] = []
        notebook_ids = sorted(
            {
                paper.notebook_id
                for paper, _artifact in pending
                if paper.notebook_id
            }
        )
        try:
            notebook_snapshot = snapshot_notebook_state([item for item in notebook_ids if item])
        except Exception as exc:  # noqa: BLE001
            consecutive_snapshot_errors += 1
            message = str(exc)
            for paper, artifact in pending:
                artifact.last_error = message
                artifact.history.append(
                    {
                        "event": "poll_error",
                        "at": iso_now(),
                        "message": message,
                    }
                )
                persist_paper_result(paper)
            if consecutive_snapshot_errors >= DEFAULT_MAX_SNAPSHOT_ERRORS:
                raise RuntimeError(
                    "NotebookLM artifact polling failed repeatedly: "
                    f"{message}"
                ) from exc
            time.sleep(poll_interval)
            continue
        consecutive_snapshot_errors = 0
        active_notebook_ids = set(notebook_snapshot.get("active_notebook_ids") or set())
        missing_notebook_ids = set(notebook_snapshot.get("missing_notebook_ids") or set())
        notebook_errors = dict(notebook_snapshot.get("notebook_errors") or {})
        artifact_errors = dict(notebook_snapshot.get("artifact_errors") or {})
        artifact_caches = notebook_snapshot.get("artifacts") or {}
        for paper, artifact in pending:
            if paper.status == "failed" and paper.error:
                finalize_paper_status(paper)
                persist_paper_result(paper)
                continue

            sync_artifact_from_disk(paper, artifact)
            if artifact.download_path and Path(artifact.download_path).exists():
                artifact.generation_status = "completed"
                finalize_paper_status(paper)
                persist_paper_result(paper)
                continue

            if (
                not artifact.artifact_id
                and paper.notebook_id
                and adopt_latest_matching_artifact(
                    paper,
                    artifact,
                    artifact_caches.get(paper.notebook_id, []),
                )
            ):
                next_pending.append((paper, artifact))
                finalize_paper_status(paper)
                persist_paper_result(paper)
                continue

            if artifact.artifact_id and artifact.generation_status == "completed":
                try:
                    download_artifact(paper, artifact)
                except RuntimeError as exc:
                    artifact.generation_status = "pending"
                    artifact.last_error = str(exc)
                    next_pending.append((paper, artifact))
                finalize_paper_status(paper)
                persist_paper_result(paper)
                continue

            if artifact.artifact_id and artifact.generation_status in {"pending", "in_progress", "unknown", "missing"}:
                status = refresh_artifact_status(
                    paper,
                    artifact,
                    active_notebook_ids,
                    missing_notebook_ids,
                    notebook_errors,
                    artifact_errors,
                    artifact_caches,
                )
                if status == "completed":
                    try:
                        download_artifact(paper, artifact)
                    except RuntimeError as exc:
                        artifact.generation_status = "pending"
                        artifact.last_error = str(exc)
                        next_pending.append((paper, artifact))
                elif status == "failed":
                    if paper.status == "failed" and paper.error:
                        pass
                    elif artifact.last_error and is_unrecoverable_error(artifact.last_error):
                        paper.status = "failed"
                        paper.error = artifact.last_error
                    else:
                        delay = compute_retry_delay(artifact.attempts + 1)
                        artifact.last_retry_delay_seconds = delay
                        time.sleep(delay)
                        submit_generation(paper, artifact)
                        if paper.status != "failed" or not paper.error:
                            next_pending.append((paper, artifact))
                elif status == "missing":
                    if artifact.missing_polls < DEFAULT_MISSING_POLLS_BEFORE_RESUBMIT:
                        next_pending.append((paper, artifact))
                    else:
                        delay = compute_retry_delay(artifact.attempts + 1)
                        artifact.last_retry_delay_seconds = delay
                        time.sleep(delay)
                        submit_generation(paper, artifact)
                        if paper.status != "failed" or not paper.error:
                            next_pending.append((paper, artifact))
                else:
                    next_pending.append((paper, artifact))
            else:
                if paper.status == "failed" and paper.error:
                    pass
                elif artifact.last_error and is_unrecoverable_error(artifact.last_error):
                    paper.status = "failed"
                    paper.error = artifact.last_error
                else:
                    delay = compute_retry_delay(artifact.attempts + 1)
                    artifact.last_retry_delay_seconds = delay
                    time.sleep(delay)
                    submit_generation(paper, artifact)
                    if paper.status != "failed" or not paper.error:
                        next_pending.append((paper, artifact))

            finalize_paper_status(paper)
            persist_paper_result(paper)
        if next_pending:
            time.sleep(poll_interval)
        pending = [
            (paper, artifact)
            for paper, artifact in next_pending
            if paper.status != "failed" or not paper.error
        ]


def build_media_manifest(
    *,
    manifest_path: Path,
    media_plan_path: Path,
    out_root: Path,
    processed_manifest: dict[str, Any],
    papers: list[MediaPaperResult],
    media_language: str = DEFAULT_NOTEBOOKLM_LANGUAGE,
) -> dict[str, Any]:
    ok_count = sum(1 for paper in papers if paper.status == "ok")
    failed_count = sum(1 for paper in papers if paper.status != "ok")
    return {
        "generated_at": iso_now(),
        "processed_manifest_path": str(manifest_path),
        "media_plan_path": str(media_plan_path),
        "processed_digest_date": processed_manifest.get("digest_date"),
        "digest_path": processed_manifest.get("digest_path"),
        "ranking_path": processed_manifest.get("ranking_path"),
        "out_dir": str(out_root),
        "media_language": normalize_media_language(media_language),
        "selected_count": len(papers),
        "ok_count": ok_count,
        "failed_count": failed_count,
        "status": "ok" if failed_count == 0 else "partial_failure",
        "results": [asdict(paper) for paper in papers],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        help="Processed manifest path. Defaults to the latest <repo>/.echoes/processed/*/manifest.json.",
    )
    parser.add_argument("--media-plan", required=True, help="Codex-authored media-plan.json path.")
    parser.add_argument(
        "--out-dir",
        help="Override output directory. Defaults to the processed run directory that owns the manifest.",
    )
    parser.add_argument(
        "--language",
        type=normalize_media_language,
        default=DEFAULT_NOTEBOOKLM_LANGUAGE,
        metavar="{es,en,spanish,english}",
        help="NotebookLM media language. Defaults to English (en); use es/spanish for Spanish.",
    )
    parser.add_argument("--limit", type=int, help="Optional paper cap for debugging.")
    parser.add_argument(
        "--wait-timeout",
        type=int,
        help=(
            "Deprecated compatibility flag. Status polling now uses notebook artifact lists "
            "so the run can finish shortly after media is actually ready."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds to wait before polling timed-out artifacts again (default: {DEFAULT_POLL_INTERVAL}).",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_notebooklm_home()
    media_language = normalize_media_language(args.language)

    manifest_path = choose_manifest_path(args.manifest)
    processed_manifest = load_json(manifest_path)
    media_plan_path = Path(args.media_plan).expanduser()
    plan_entries = load_media_plan(media_plan_path)
    raw_results = processed_manifest.get("results")
    if not isinstance(raw_results, list):
        raise SystemExit("Processed manifest is missing results.")
    validate_media_plan(plan_entries, [entry for entry in raw_results if isinstance(entry, dict)])
    out_root = Path(args.out_dir).expanduser() if args.out_dir else manifest_path.parent
    ensure_dir(out_root)
    lock_path: Path | None = None

    try:
        lock_path = acquire_run_lock(out_root, manifest_path)
        papers = eligible_papers(
            processed_manifest,
            out_root=out_root,
            limit=args.limit,
            plan_entries=plan_entries,
            media_language=media_language,
        )
        if papers:
            process_media(papers, poll_interval=args.poll_interval)
    except MediaRunLockedError as exc:
        payload = {
            "error": True,
            "status": "already_running",
            "message": str(exc),
            "processed_manifest_path": str(manifest_path),
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(payload["message"])
        return 1
    except Exception as exc:  # noqa: BLE001
        payload = {
            "error": True,
            "status": "failed",
            "message": str(exc),
            "processed_manifest_path": str(manifest_path),
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(payload["message"])
        return 1
    finally:
        release_run_lock(lock_path)

    media_manifest = build_media_manifest(
        manifest_path=manifest_path,
        media_plan_path=media_plan_path,
        out_root=out_root,
        processed_manifest=processed_manifest,
        papers=papers,
        media_language=media_language,
    )
    media_manifest_path = out_root / "media-manifest.json"
    write_json(media_manifest_path, media_manifest)

    if args.json:
        print(json.dumps(media_manifest, indent=2, ensure_ascii=False))
    else:
        print(
            f"Generated media for {media_manifest['ok_count']} of {len(papers)} processed paper(s). "
            f"Manifest: {media_manifest_path}"
        )
        for paper in papers:
            if paper.status == "ok":
                print(f"- OK: {paper.title}")
            else:
                print(f"- FAILED: {paper.title} -> {paper.error or 'unknown error'}")

    return 0 if media_manifest["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
