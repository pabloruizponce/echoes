#!/usr/bin/env python3
"""Process shortlisted papers into per-paper NotebookLM notebooks."""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from notebooklm import NotebookLMClient
from notebooklm.types import Notebook, Source, source_status_to_str

from dns_fallback import request_with_dns_fallback


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 120.0
DOWNLOAD_TIMEOUT = 120.0
DEFAULT_COMPRESSION_PROFILE = "printer"
COMPRESSION_PROFILES = {"screen", "ebook", "printer", "prepress"}
DEFAULT_NOTEBOOKLM_RETRIES = 3
DEFAULT_RETRY_DELAY = 5.0
DEFAULT_NOTEBOOKLM_RPC_TIMEOUT = 120.0


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


def latest_json_file(directory: Path) -> Path:
    candidates = sorted(directory.glob("*.json"), key=lambda path: (path.stat().st_mtime, path.name))
    if not candidates:
        raise SystemExit(f"No JSON artifacts found in {directory}")
    return candidates[-1]


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} does not contain a JSON object.")
    return payload


def slugify(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-")
    return lowered or "paper"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def choose_ranking_path(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit(f"Ranking artifact not found: {path}")
        return path
    return latest_json_file(config_dir() / "rankings")


def choose_digest_path(explicit: str | None, ranking_payload: dict[str, Any]) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit(f"Digest artifact not found: {path}")
        return path

    digest_path = ranking_payload.get("digest_path")
    if isinstance(digest_path, str) and digest_path.strip():
        path = Path(digest_path).expanduser()
        if path.exists():
            return path
        raise SystemExit(f"Digest artifact referenced by ranking does not exist: {path}")

    return latest_json_file(config_dir() / "digests")


def derive_out_dir(explicit: str | None, digest_payload: dict[str, Any]) -> Path:
    if explicit:
        return Path(explicit).expanduser()

    digest_date = str(digest_payload.get("effective_digest_date") or "").strip()
    if not digest_date:
        raise SystemExit("Digest payload is missing effective_digest_date.")
    return config_dir() / "processed" / digest_date


def shortlisted_papers(
    ranking_payload: dict[str, Any],
    *,
    limit: int | None,
) -> list[dict[str, Any]]:
    shortlist_titles = ranking_payload.get("shortlist_titles")
    ranked_entries = ranking_payload.get("ranked")
    if not isinstance(shortlist_titles, list) or not isinstance(ranked_entries, list):
        raise SystemExit("Ranking artifact is missing shortlist_titles or ranked.")

    title_to_paper: dict[str, dict[str, Any]] = {}
    for entry in ranked_entries:
        if not isinstance(entry, dict):
            continue
        paper = entry.get("paper")
        if not isinstance(paper, dict):
            continue
        title = str(paper.get("title") or "").strip()
        if title:
            title_to_paper[title] = paper

    papers: list[dict[str, Any]] = []
    for raw_title in shortlist_titles:
        title = str(raw_title or "").strip()
        if not title:
            continue
        if title not in title_to_paper:
            raise SystemExit(f"Shortlisted paper not found in ranked entries: {title}")
        papers.append(title_to_paper[title])

    if limit is not None:
        papers = papers[:limit]
    return papers


def require_agent_review(ranking_payload: dict[str, Any], ranking_path: Path) -> None:
    agent_review = ranking_payload.get("agent_review")
    if not isinstance(agent_review, dict):
        raise SystemExit(
            f"Ranking artifact must be Codex-reviewed before processing papers: {ranking_path}"
        )
    selected_titles = agent_review.get("selected_titles")
    if not isinstance(selected_titles, list):
        raise SystemExit(
            f"Ranking artifact has invalid agent_review metadata: {ranking_path}"
        )


def pdf_url_for_paper(paper: dict[str, Any]) -> str:
    url = str(paper.get("url") or "").strip()
    if not url:
        raise ValueError("Paper is missing a URL.")
    return url


def download_pdf(url: str, destination: Path) -> dict[str, Any]:
    headers = {
        "User-Agent": "echoes-process/0.1",
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    }
    with request_with_dns_fallback(
        "GET",
        url,
        headers=headers,
        timeout=DOWNLOAD_TIMEOUT,
        stream=True,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        ensure_dir(destination.parent)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    handle.write(chunk)

    return {
        "url": url,
        "path": str(destination),
        "content_type": content_type,
        "size_bytes": destination.stat().st_size,
    }


def compress_pdf(input_path: Path, output_path: Path, profile: str) -> dict[str, Any]:
    gs_binary = shutil.which("gs")
    input_size = input_path.stat().st_size
    result: dict[str, Any] = {
        "attempted": True,
        "tool": "ghostscript",
        "profile": profile,
        "available": bool(gs_binary),
        "input_path": str(input_path),
        "output_path": str(output_path),
        "input_size_bytes": input_size,
        "output_size_bytes": None,
        "used_output": False,
        "status": "skipped",
        "reason": None,
    }

    if not gs_binary:
        result["reason"] = "Ghostscript is not installed."
        return result

    ensure_dir(output_path.parent)
    command = [
        gs_binary,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{profile}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-sOutputFile={output_path}",
        str(input_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    result["command"] = command
    result["returncode"] = completed.returncode
    stderr = completed.stderr.strip()
    stdout = completed.stdout.strip()
    if completed.returncode != 0:
        result["status"] = "failed"
        result["reason"] = stderr or stdout or "Ghostscript compression failed."
        if output_path.exists():
            output_path.unlink()
        return result

    if not output_path.exists():
        result["status"] = "failed"
        result["reason"] = "Ghostscript reported success but no compressed file was created."
        return result

    output_size = output_path.stat().st_size
    result["output_size_bytes"] = output_size
    if output_size >= input_size:
        result["status"] = "not_smaller"
        result["reason"] = "Compressed PDF was not smaller than the original."
        return result

    result["status"] = "ok"
    result["used_output"] = True
    return result


def run_json_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Command failed.")
    payload = completed.stdout.strip()
    if not payload:
        raise RuntimeError("Command returned empty output.")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Command returned non-JSON output: {payload}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected a JSON object but received: {type(parsed).__name__}")
    return parsed


def notebooklm_rpc_timeout(timeout: float | None = None) -> float:
    return max(DEFAULT_NOTEBOOKLM_RPC_TIMEOUT, float(timeout or 0.0))


def notebook_to_payload(notebook: Notebook) -> dict[str, Any]:
    return {
        "id": notebook.id,
        "title": notebook.title,
        "created_at": notebook.created_at.isoformat() if notebook.created_at else None,
    }


def source_to_payload(source: Source) -> dict[str, Any]:
    return {
        "id": source.id,
        "title": source.title,
        "status": source_status_to_str(source.status),
        "status_code": int(source.status),
    }


async def _list_notebooks_async(timeout: float) -> list[Notebook]:
    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout(timeout)) as client:
        return await client.notebooks.list()


def list_notebooks(timeout: float) -> list[Notebook]:
    return asyncio.run(_list_notebooks_async(timeout))


def find_latest_notebook_by_title(
    title: str,
    *,
    timeout: float,
    exclude_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    exclude = exclude_ids or set()
    candidates = [
        notebook
        for notebook in list_notebooks(timeout)
        if notebook.title.strip() == title and notebook.id not in exclude
    ]
    if not candidates:
        return None
    chosen = max(
        candidates,
        key=lambda notebook: (
            notebook.created_at.isoformat() if notebook.created_at else "",
            notebook.id,
        ),
    )
    return notebook_to_payload(chosen)


async def _create_notebook_async(title: str, timeout: float) -> dict[str, Any]:
    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout(timeout)) as client:
        return notebook_to_payload(await client.notebooks.create(title))


def create_notebook(title: str, timeout: float) -> dict[str, Any]:
    return asyncio.run(_create_notebook_async(title, timeout))


def create_notebook_with_recovery(
    title: str,
    *,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    known_ids: set[str] = set()
    try:
        known_ids = {item.id for item in list_notebooks(timeout)}
    except Exception:
        known_ids = set()

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return create_notebook(title, timeout)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            try:
                adopted = find_latest_notebook_by_title(
                    title,
                    timeout=timeout,
                    exclude_ids=known_ids,
                )
            except Exception:
                adopted = None
            if adopted is not None:
                return adopted
            if attempt < retries:
                time.sleep(retry_delay)

    try:
        adopted = find_latest_notebook_by_title(title, timeout=timeout, exclude_ids=known_ids)
    except Exception:
        adopted = None
    if adopted is not None:
        return adopted
    assert last_exc is not None
    raise RuntimeError(f"NotebookLM create notebook failed after {retries} attempt(s): {last_exc}") from last_exc


async def _list_sources_async(notebook_id: str, timeout: float) -> list[Source]:
    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout(timeout)) as client:
        return await client.sources.list(notebook_id)


def list_sources(notebook_id: str, timeout: float) -> list[Source]:
    return asyncio.run(_list_sources_async(notebook_id, timeout))


def find_latest_source_by_title(
    notebook_id: str,
    title: str,
    *,
    timeout: float,
    exclude_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    exclude = exclude_ids or set()
    candidates = [
        source
        for source in list_sources(notebook_id, timeout)
        if (source.title or "").strip() == title and source.id not in exclude
    ]
    if not candidates:
        return None
    chosen = max(
        candidates,
        key=lambda source: (
            source.created_at.isoformat() if source.created_at else "",
            source.id,
        ),
    )
    return source_to_payload(chosen)


async def _add_file_source_async(notebook_id: str, file_path: Path, timeout: float) -> dict[str, Any]:
    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout(timeout)) as client:
        source = await client.sources.add_file(
            notebook_id,
            file_path,
            mime_type="application/pdf",
            wait=False,
        )
        return source_to_payload(source)


def add_file_source(notebook_id: str, file_path: Path, timeout: float) -> dict[str, Any]:
    return asyncio.run(_add_file_source_async(notebook_id, file_path, timeout))


def add_file_source_with_recovery(
    notebook_id: str,
    file_path: Path,
    *,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    known_ids: set[str] = set()
    try:
        known_ids = {item.id for item in list_sources(notebook_id, timeout)}
    except Exception:
        known_ids = set()

    last_exc: Exception | None = None
    source_title = file_path.name
    for attempt in range(1, retries + 1):
        try:
            return add_file_source(notebook_id, file_path, timeout)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            try:
                adopted = find_latest_source_by_title(
                    notebook_id,
                    source_title,
                    timeout=timeout,
                    exclude_ids=known_ids,
                )
            except Exception:
                adopted = None
            if adopted is not None:
                return adopted
            if attempt < retries:
                time.sleep(retry_delay)

    try:
        adopted = find_latest_source_by_title(
            notebook_id,
            source_title,
            timeout=timeout,
            exclude_ids=known_ids,
        )
    except Exception:
        adopted = None
    if adopted is not None:
        return adopted
    assert last_exc is not None
    raise RuntimeError(f"NotebookLM add source failed after {retries} attempt(s): {last_exc}") from last_exc


async def _wait_for_source_async(notebook_id: str, source_id: str, timeout: float) -> dict[str, Any]:
    async with await NotebookLMClient.from_storage(timeout=notebooklm_rpc_timeout(timeout)) as client:
        source = await client.sources.wait_until_ready(notebook_id, source_id, timeout=timeout)
        return source_to_payload(source)


def wait_for_source(notebook_id: str, source_id: str, timeout: float) -> dict[str, Any]:
    return asyncio.run(_wait_for_source_async(notebook_id, source_id, timeout))


@dataclass
class PaperResult:
    title: str
    slug: str
    paper_url: str
    status: str
    notebook_id: str | None = None
    notebook_title: str | None = None
    source_id: str | None = None
    ranking_path: str | None = None
    digest_path: str | None = None
    digest_date: str | None = None
    work_dir: str | None = None
    original_pdf_path: str | None = None
    compressed_pdf_path: str | None = None
    uploaded_pdf_path: str | None = None
    download: dict[str, Any] | None = None
    compression: dict[str, Any] | None = None
    wait_result: dict[str, Any] | None = None
    error: str | None = None
    processed_at: str | None = None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def result_from_dict(entry: dict[str, Any]) -> PaperResult:
    fields = {field.name for field in PaperResult.__dataclass_fields__.values()}
    payload = {key: entry.get(key) for key in fields}
    return PaperResult(**payload)


def load_existing_results(manifest_path: Path) -> dict[str, PaperResult]:
    if not manifest_path.exists():
        return {}
    payload = load_json(manifest_path)
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return {}
    results: dict[str, PaperResult] = {}
    for raw_entry in raw_results:
        if not isinstance(raw_entry, dict):
            continue
        title = str(raw_entry.get("title") or "").strip()
        if title:
            results[title] = result_from_dict(raw_entry)
    return results


def file_exists(path_value: str | None) -> bool:
    return bool(path_value) and Path(path_value).expanduser().exists()


def preferred_upload_path(result: PaperResult) -> Path:
    if file_exists(result.uploaded_pdf_path):
        return Path(str(result.uploaded_pdf_path)).expanduser()
    if result.compression and result.compression.get("used_output") and file_exists(result.compressed_pdf_path):
        return Path(str(result.compressed_pdf_path)).expanduser()
    if file_exists(result.original_pdf_path):
        return Path(str(result.original_pdf_path)).expanduser()
    raise FileNotFoundError("No uploaded PDF path is available to resume this paper.")


def run_notebooklm_step(
    action: str,
    fn: Any,
    *args: Any,
    retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(retry_delay)
    assert last_exc is not None
    raise RuntimeError(f"{action} failed after {retries} attempt(s): {last_exc}") from last_exc


def should_refresh_notebook_binding(message: str) -> bool:
    lowered = message.lower()
    hints = (
        "unknown notebook",
        "invalid notebook",
        "missing notebook",
        "no longer available",
        "returned null result data",
    )
    return any(hint in lowered for hint in hints)


def should_refresh_source_binding(message: str) -> bool:
    lowered = message.lower()
    hints = (
        "unknown source",
        "invalid source",
        "missing source",
    )
    return any(hint in lowered for hint in hints)


def resume_existing_paper(
    existing: PaperResult,
    *,
    timeout: float,
    notebooklm_retries: int,
    retry_delay: float,
) -> PaperResult | None:
    if existing.notebook_id and existing.source_id:
        try:
            existing.wait_result = run_notebooklm_step(
                "NotebookLM source wait",
                wait_for_source,
                existing.notebook_id,
                existing.source_id,
                timeout,
                retries=notebooklm_retries,
                retry_delay=retry_delay,
            )
            existing.status = "ok"
            existing.error = None
            existing.processed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            work_dir = Path(str(existing.work_dir)).expanduser()
            write_json(work_dir / "result.json", asdict(existing))
            return existing
        except Exception as exc:  # noqa: BLE001
            existing.error = str(exc)
            existing.status = "pending"
            existing.wait_result = None
            if should_refresh_notebook_binding(existing.error):
                existing.notebook_id = None
                existing.notebook_title = None
                existing.source_id = None
            elif should_refresh_source_binding(existing.error):
                existing.source_id = None
    return None


def process_paper(
    paper: dict[str, Any],
    *,
    ranking_path: Path,
    digest_path: Path,
    digest_date: str,
    out_dir: Path,
    timeout: float,
    compression_profile: str,
    existing: PaperResult | None = None,
    notebooklm_retries: int,
    retry_delay: float,
) -> PaperResult:
    title = str(paper.get("title") or "").strip()
    if not title:
        raise ValueError("Shortlisted paper is missing a title.")
    slug = slugify(title)
    work_dir = out_dir / slug
    ensure_dir(work_dir)

    original_pdf_path = work_dir / "original.pdf"
    compressed_pdf_path = work_dir / "compressed.pdf"
    result = existing or PaperResult(
        title=title,
        slug=slug,
        paper_url=pdf_url_for_paper(paper),
        status="pending",
    )
    result.title = title
    result.slug = slug
    result.paper_url = pdf_url_for_paper(paper)
    result.ranking_path = str(ranking_path)
    result.digest_path = str(digest_path)
    result.digest_date = digest_date
    result.work_dir = str(work_dir)
    result.original_pdf_path = str(original_pdf_path)
    result.compressed_pdf_path = str(compressed_pdf_path)
    result.processed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        if result.notebook_id or result.source_id or result.status != "pending":
            resumed = resume_existing_paper(
                result,
                timeout=timeout,
                notebooklm_retries=notebooklm_retries,
                retry_delay=retry_delay,
            )
            if resumed is not None:
                return resumed

        have_original_pdf = file_exists(result.original_pdf_path)
        have_compressed_pdf = file_exists(result.compressed_pdf_path)
        have_uploaded_pdf = file_exists(result.uploaded_pdf_path)

        if not have_original_pdf and not have_compressed_pdf and not have_uploaded_pdf:
            result.download = download_pdf(result.paper_url, original_pdf_path)
            have_original_pdf = True

        if have_original_pdf and (
            result.compression is None
            or (result.compression.get("used_output") and not have_compressed_pdf)
        ):
            result.compression = compress_pdf(
                original_pdf_path,
                compressed_pdf_path,
                compression_profile,
            )

        upload_path = preferred_upload_path(result)
        result.uploaded_pdf_path = str(upload_path)

        if not result.notebook_id:
            notebook = create_notebook_with_recovery(
                title,
                timeout=timeout,
                retries=notebooklm_retries,
                retry_delay=retry_delay,
            )
            result.notebook_id = str(notebook["id"])
            result.notebook_title = str(notebook.get("title") or title)

        if not result.source_id:
            source = add_file_source_with_recovery(
                result.notebook_id,
                upload_path,
                timeout=timeout,
                retries=notebooklm_retries,
                retry_delay=retry_delay,
            )
            result.source_id = str(source["id"])

        result.wait_result = run_notebooklm_step(
            "NotebookLM source wait",
            wait_for_source,
            result.notebook_id,
            result.source_id,
            timeout,
            retries=notebooklm_retries,
            retry_delay=retry_delay,
        )
        result.status = "ok"
        result.error = None
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.error = str(exc)

    write_json(work_dir / "result.json", asdict(result))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranking", help="Ranking JSON artifact path. Defaults to the latest saved ranking.")
    parser.add_argument("--digest", help="Digest JSON artifact path. Defaults to the ranking's digest_path.")
    parser.add_argument(
        "--out-dir",
        help="Output directory for processed papers. Defaults to <repo>/.echoes/processed/<digest-date>/",
    )
    parser.add_argument("--limit", type=int, help="Optional shortlist cap for debugging.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"NotebookLM source wait timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--compression-profile",
        choices=sorted(COMPRESSION_PROFILES),
        default=DEFAULT_COMPRESSION_PROFILE,
        help=(
            "Ghostscript PDFSETTINGS profile to use before upload "
            f"(default: {DEFAULT_COMPRESSION_PROFILE})."
        ),
    )
    parser.add_argument(
        "--notebooklm-retries",
        type=int,
        default=DEFAULT_NOTEBOOKLM_RETRIES,
        help=f"Retry count for transient NotebookLM RPC failures (default: {DEFAULT_NOTEBOOKLM_RETRIES}).",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY,
        help=f"Seconds to wait between NotebookLM retries (default: {DEFAULT_RETRY_DELAY}).",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_notebooklm_home()

    ranking_path = choose_ranking_path(args.ranking)
    ranking_payload = load_json(ranking_path)
    require_agent_review(ranking_payload, ranking_path)
    digest_path = choose_digest_path(args.digest, ranking_payload)
    digest_payload = load_json(digest_path)
    digest_date = str(digest_payload.get("effective_digest_date") or "").strip()
    if not digest_date:
        raise SystemExit("Digest payload is missing effective_digest_date.")

    out_dir = derive_out_dir(args.out_dir, digest_payload)
    ensure_dir(out_dir)

    papers = shortlisted_papers(ranking_payload, limit=args.limit)
    manifest_path = out_dir / "manifest.json"
    existing_results = load_existing_results(manifest_path)

    manifest: dict[str, Any] = {
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ranking_path": str(ranking_path),
        "digest_path": str(digest_path),
        "digest_date": digest_date,
        "compression_profile": args.compression_profile,
        "shortlist_titles": ranking_payload.get("shortlist_titles", []),
        "selected_count": len(papers),
        "results": [],
        "ok_count": 0,
        "failed_count": 0,
        "status": "ok",
    }

    if not papers:
        write_json(manifest_path, manifest)
        if args.json:
            print(json.dumps(manifest, indent=2))
        else:
            print("No important-now papers to process.")
        return 0

    results: list[PaperResult] = []
    for paper in papers:
        title = str(paper.get("title") or "").strip()
        result = process_paper(
            paper,
            ranking_path=ranking_path,
            digest_path=digest_path,
            digest_date=digest_date,
            out_dir=out_dir,
            timeout=args.timeout,
            compression_profile=args.compression_profile,
            existing=existing_results.get(title),
            notebooklm_retries=max(1, args.notebooklm_retries),
            retry_delay=max(0.0, args.retry_delay),
        )
        results.append(result)
        manifest["results"] = [asdict(item) for item in results]
        manifest["ok_count"] = sum(1 for item in results if item.status == "ok")
        manifest["failed_count"] = sum(1 for item in results if item.status != "ok")
        manifest["status"] = "ok" if manifest["failed_count"] == 0 else "partial_failure"
        write_json(manifest_path, manifest)

    manifest["results"] = [asdict(item) for item in results]
    manifest["ok_count"] = sum(1 for item in results if item.status == "ok")
    manifest["failed_count"] = sum(1 for item in results if item.status != "ok")
    manifest["status"] = "ok" if manifest["failed_count"] == 0 else "partial_failure"
    write_json(manifest_path, manifest)

    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(
            f"Processed {manifest['ok_count']} of {len(results)} shortlisted paper(s). "
            f"Manifest: {manifest_path}"
        )
        for item in results:
            if item.status == "ok":
                print(f"- OK: {item.title} -> {item.notebook_id}")
            else:
                print(f"- FAILED: {item.title} -> {item.error}")

    return 0 if manifest["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
