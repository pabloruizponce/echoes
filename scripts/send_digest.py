#!/usr/bin/env python3
"""Send a generated echoes bundle to Telegram."""

from __future__ import annotations

import argparse
import asyncio
import html
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

try:
    from telegram import Bot, InputFile
    from telegram.request import HTTPXRequest
except ImportError:  # pragma: no cover - exercised only when deps are not installed yet
    Bot = Any  # type: ignore[misc,assignment]
    HTTPXRequest = None  # type: ignore[assignment]

    class InputFile:  # type: ignore[no-redef]
        def __init__(self, handle: Any, filename: str | None = None) -> None:
            self.handle = handle
            self.filename = filename

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR_NAME = ".echoes"
DEFAULT_TELEGRAM_RETRIES = 3
DEFAULT_TELEGRAM_RETRY_DELAY = 5.0
DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org/bot"
DEFAULT_TELEGRAM_API_FILE_BASE_URL = "https://api.telegram.org/file/bot"
DEFAULT_TELEGRAM_API_HOST = "api.telegram.org"
DEFAULT_TELEGRAM_CONNECT_TIMEOUT = 15.0
DEFAULT_TELEGRAM_READ_TIMEOUT = 30.0
DEFAULT_TELEGRAM_WRITE_TIMEOUT = 30.0
DEFAULT_TELEGRAM_MEDIA_WRITE_TIMEOUT = 180.0
DEFAULT_TELEGRAM_POOL_TIMEOUT = 5.0
DEFAULT_TELEGRAM_CONNECTION_POOL_SIZE = 4
HOSTED_BOT_API_UPLOAD_LIMIT_BYTES = 50_000_000
TELEGRAM_SAFE_VIDEO_TARGET_BYTES = 48_000_000
TELEGRAM_SAFE_VIDEO_AUDIO_BITRATE = 64_000
TELEGRAM_SAFE_VIDEO_MIN_VIDEO_BITRATE = 300_000
TELEGRAM_PARSE_MODE = "HTML"
TELEGRAM_SECRET_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
EMOJI_PATTERN = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
INTRO_EMOJI = "\U0001F4DA"
PAPER_MESSAGE_EMOJI = "\U0001F4C4"
PDF_EMOJI = "\U0001F4CE"
AUDIO_EMOJI = "\U0001F3A7"
VIDEO_EMOJI = "\U0001F3AC"
PDF_ATTACHMENT_NOTE_PATTERN = re.compile(
    (
        rf"^\s*(?:{re.escape(PDF_EMOJI)}\s*)?(?:<i>)?\s*"
        r"(?:te env[i\u00ed]o el pdf como archivo nativo a continuaci[o\u00f3]n\.?"
        r"|the pdf follows as a native attachment\.?)"
        r"\s*(?:</i>)?\s*$"
    ),
    re.IGNORECASE,
)


def config_dir() -> Path:
    override = os.environ.get("ECHOES_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return ROOT / DEFAULT_CONFIG_DIR_NAME


def credentials_path() -> Path:
    return config_dir() / "credentials.env"


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def latest_media_manifest_file(directory: Path) -> Path:
    candidates = sorted(
        directory.glob("*/media-manifest.json"),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if not candidates:
        raise SystemExit(f"No media-manifest.json files found in {directory}")
    return candidates[-1]


def choose_manifest_path(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit(f"Media manifest not found: {path}")
        return path
    return latest_media_manifest_file(config_dir() / "processed")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} does not contain a JSON object.")
    return payload


def load_credentials() -> dict[str, str]:
    return parse_env_file(credentials_path())


def credential_value(values: dict[str, str], key: str, default: str = "") -> str:
    env_value = os.environ.get(key)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    return str(values.get(key) or default).strip()


def require_telegram_credentials(values: dict[str, str]) -> tuple[str, str]:
    token = credential_value(values, "TELEGRAM_BOT_TOKEN")
    chat_id = credential_value(values, "TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    missing = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    raise SystemExit(
        "Telegram configuration is missing: "
        + ", ".join(missing)
        + f". Save them in {credentials_path()} and rerun the delivery step."
    )


def float_credential(values: dict[str, str], key: str, default: float) -> float:
    raw_value = credential_value(values, key)
    if not raw_value:
        return default
    try:
        parsed = float(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{key} must be a number of seconds.") from exc
    if parsed <= 0:
        raise SystemExit(f"{key} must be greater than zero.")
    return parsed


def int_credential(values: dict[str, str], key: str, default: int) -> int:
    raw_value = credential_value(values, key)
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{key} must be a positive integer.") from exc
    if parsed <= 0:
        raise SystemExit(f"{key} must be a positive integer.")
    return parsed


def bool_credential(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw_value = credential_value(values, key)
    if not raw_value:
        return default
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise SystemExit(f"{key} must be true or false.")


def paired_file_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/bot"):
        return normalized[: -len("/bot")] + "/file/bot"
    return DEFAULT_TELEGRAM_API_FILE_BASE_URL


def redact_sensitive(text: str, secrets: list[str] | tuple[str, ...] | None = None) -> str:
    redacted = TELEGRAM_SECRET_PATTERN.sub("[redacted-telegram-token]", text)
    for secret in secrets or ():
        if len(secret) >= 8:
            redacted = redacted.replace(secret, "[redacted-telegram-token]")
    return redacted


def ensure_emoji(text: str, emoji: str) -> str:
    normalized = text.strip()
    if not normalized or EMOJI_PATTERN.search(normalized):
        return normalized
    return f"{emoji} {normalized}"


def strip_pdf_attachment_note(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if not PDF_ATTACHMENT_NOTE_PATTERN.match(line)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def generated_media_caption(*, title: str, emoji: str) -> str:
    return f"{emoji} {html.escape(title)}"


class SNIlessHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to an IP address while validating against the API host."""

    def __init__(self, api_ip: str, *, verify_hostname: str, timeout: float) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        super().__init__(api_ip, port=443, timeout=timeout, context=context)
        self.verify_hostname = verify_hostname

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=None)
        if not certificate_matches_hostname(self.sock.getpeercert(), self.verify_hostname):
            raise ssl.CertificateError(f"certificate is not valid for {self.verify_hostname}")


def certificate_matches_hostname(cert: dict[str, Any], hostname: str) -> bool:
    names = [
        value
        for key, value in cert.get("subjectAltName", ())
        if str(key).lower() == "dns" and isinstance(value, str)
    ]
    if not names:
        for subject_part in cert.get("subject", ()):
            for key, value in subject_part:
                if str(key).lower() == "commonname" and isinstance(value, str):
                    names.append(value)
    return any(hostname_matches_pattern(hostname, pattern) for pattern in names)


def hostname_matches_pattern(hostname: str, pattern: str) -> bool:
    normalized_host = hostname.lower().rstrip(".")
    normalized_pattern = pattern.lower().rstrip(".")
    if normalized_host == normalized_pattern:
        return True
    if not normalized_pattern.startswith("*."):
        return False
    suffix = normalized_pattern[1:]
    return normalized_host.endswith(suffix) and normalized_host.count(".") == normalized_pattern.count(".")


class SNIlessTelegramBot:
    """Small Bot API client for networks that reset TLS SNI for api.telegram.org."""

    def __init__(
        self,
        *,
        token: str,
        api_ip: str,
        host_header: str = DEFAULT_TELEGRAM_API_HOST,
        timeout: float = DEFAULT_TELEGRAM_READ_TIMEOUT,
        media_timeout: float = DEFAULT_TELEGRAM_MEDIA_WRITE_TIMEOUT,
    ) -> None:
        self.token = token
        self.api_ip = api_ip
        self.host_header = host_header
        self.timeout = timeout
        self.media_timeout = media_timeout

    async def send_message(self, *, chat_id: str, text: str, **kwargs: Any) -> Any:
        fields = {"chat_id": chat_id, "text": text}
        fields.update({key: value for key, value in kwargs.items() if value is not None})
        return await asyncio.to_thread(self._post, "sendMessage", fields)

    async def send_audio(self, *, chat_id: str, audio: Any, caption: str, **kwargs: Any) -> Any:
        fields = {"chat_id": chat_id, "caption": caption}
        fields.update({key: value for key, value in kwargs.items() if value is not None})
        return await asyncio.to_thread(
            self._post,
            "sendAudio",
            fields,
            {"audio": input_file_field_tuple(audio)},
            True,
        )

    async def send_voice(self, *, chat_id: str, voice: Any, caption: str, **kwargs: Any) -> Any:
        fields = {"chat_id": chat_id, "caption": caption}
        fields.update({key: value for key, value in kwargs.items() if value is not None})
        return await asyncio.to_thread(
            self._post,
            "sendVoice",
            fields,
            {"voice": input_file_field_tuple(voice)},
            True,
        )

    async def send_document(self, *, chat_id: str, document: Any, caption: str, **kwargs: Any) -> Any:
        fields = {"chat_id": chat_id, "caption": caption}
        fields.update({key: value for key, value in kwargs.items() if value is not None})
        return await asyncio.to_thread(
            self._post,
            "sendDocument",
            fields,
            {"document": input_file_field_tuple(document)},
            True,
        )

    async def send_video(self, *, chat_id: str, video: Any, caption: str, **kwargs: Any) -> Any:
        fields = {"chat_id": chat_id, "caption": caption}
        fields.update({key: value for key, value in kwargs.items() if value is not None})
        return await asyncio.to_thread(
            self._post,
            "sendVideo",
            fields,
            {"video": input_file_field_tuple(video)},
            True,
        )

    def _post(
        self,
        method: str,
        fields: dict[str, Any],
        files: dict[str, tuple[str, bytes, str]] | None = None,
        media: bool = False,
    ) -> Any:
        body, content_type = encode_multipart_form(fields, files) if files else encode_url_form(fields)
        connection = SNIlessHTTPSConnection(
            self.api_ip,
            verify_hostname=self.host_header,
            timeout=self.media_timeout if media else self.timeout,
        )
        try:
            connection.request(
                "POST",
                f"/bot{self.token}/{method}",
                body=body,
                headers={
                    "Host": self.host_header,
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()

        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Telegram Bot API returned non-JSON HTTP {response.status}.") from exc
        if response.status >= 400 or not payload.get("ok"):
            description = str(payload.get("description") or f"HTTP {response.status}")
            raise RuntimeError(f"Telegram Bot API {method} failed: {description}")
        return payload.get("result")


def encode_url_form(fields: dict[str, Any]) -> tuple[bytes, str]:
    normalized = {key: telegram_field_value(value) for key, value in fields.items()}
    return urllib.parse.urlencode(normalized).encode("utf-8"), "application/x-www-form-urlencoded"


def encode_multipart_form(
    fields: dict[str, Any],
    files: dict[str, tuple[str, bytes, str]] | None,
) -> tuple[bytes, str]:
    boundary = f"scholardigest{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                telegram_field_value(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for key, (filename, content, mimetype) in (files or {}).items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{key}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mimetype or 'application/octet-stream'}\r\n\r\n".encode("utf-8"),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def telegram_field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def input_file_field_tuple(value: Any) -> tuple[str, bytes, str]:
    field_tuple = getattr(value, "field_tuple", None)
    if isinstance(field_tuple, tuple) and len(field_tuple) == 3:
        filename, content, mimetype = field_tuple
        if isinstance(content, str):
            content = content.encode("utf-8")
        return str(filename), bytes(content), str(mimetype or "application/octet-stream")

    filename = str(getattr(value, "name", "upload.bin"))
    content = value.read()
    if isinstance(content, str):
        content = content.encode("utf-8")
    return Path(filename).name, bytes(content), "application/octet-stream"


def build_telegram_request(values: dict[str, str]) -> Any:
    if HTTPXRequest is None:  # pragma: no cover - dependency import failure is handled by the CLI.
        return None

    kwargs: dict[str, Any] = {
        "connection_pool_size": int_credential(
            values,
            "TELEGRAM_CONNECTION_POOL_SIZE",
            DEFAULT_TELEGRAM_CONNECTION_POOL_SIZE,
        ),
        "connect_timeout": float_credential(
            values,
            "TELEGRAM_CONNECT_TIMEOUT",
            DEFAULT_TELEGRAM_CONNECT_TIMEOUT,
        ),
        "read_timeout": float_credential(
            values,
            "TELEGRAM_READ_TIMEOUT",
            DEFAULT_TELEGRAM_READ_TIMEOUT,
        ),
        "write_timeout": float_credential(
            values,
            "TELEGRAM_WRITE_TIMEOUT",
            DEFAULT_TELEGRAM_WRITE_TIMEOUT,
        ),
        "pool_timeout": float_credential(
            values,
            "TELEGRAM_POOL_TIMEOUT",
            DEFAULT_TELEGRAM_POOL_TIMEOUT,
        ),
        "media_write_timeout": float_credential(
            values,
            "TELEGRAM_MEDIA_WRITE_TIMEOUT",
            DEFAULT_TELEGRAM_MEDIA_WRITE_TIMEOUT,
        ),
    }
    proxy_url = credential_value(values, "TELEGRAM_PROXY_URL")
    if proxy_url:
        kwargs["proxy"] = proxy_url

    try:
        return HTTPXRequest(**kwargs)
    except TypeError:
        # python-telegram-bot used proxy_url before proxy; support either so the
        # automation can move across devices with slightly different versions.
        if "proxy" not in kwargs:
            raise
        kwargs["proxy_url"] = kwargs.pop("proxy")
        return HTTPXRequest(**kwargs)


def build_telegram_bot(token: str, values: dict[str, str]) -> Bot:
    api_ip = credential_value(values, "TELEGRAM_API_IP")
    if api_ip:
        return SNIlessTelegramBot(
            token=token,
            api_ip=api_ip,
            host_header=credential_value(values, "TELEGRAM_API_HOST_HEADER", DEFAULT_TELEGRAM_API_HOST),
            timeout=float_credential(values, "TELEGRAM_READ_TIMEOUT", DEFAULT_TELEGRAM_READ_TIMEOUT),
            media_timeout=float_credential(
                values,
                "TELEGRAM_MEDIA_WRITE_TIMEOUT",
                DEFAULT_TELEGRAM_MEDIA_WRITE_TIMEOUT,
            ),
        )

    base_url = credential_value(values, "TELEGRAM_API_BASE_URL", DEFAULT_TELEGRAM_API_BASE_URL).rstrip("/")
    base_file_url = credential_value(values, "TELEGRAM_API_FILE_BASE_URL")
    if base_file_url:
        base_file_url = base_file_url.rstrip("/")
    else:
        base_file_url = (
            DEFAULT_TELEGRAM_API_FILE_BASE_URL
            if base_url == DEFAULT_TELEGRAM_API_BASE_URL
            else paired_file_base_url(base_url)
        )
    return Bot(
        token=token,
        base_url=base_url,
        base_file_url=base_file_url,
        request=build_telegram_request(values),
        local_mode=bool_credential(values, "TELEGRAM_LOCAL_MODE", False),
    )


def telegram_transport_summary(values: dict[str, str]) -> dict[str, Any]:
    base_url = credential_value(values, "TELEGRAM_API_BASE_URL", DEFAULT_TELEGRAM_API_BASE_URL).rstrip("/")
    return {
        "api_base_url": redact_sensitive(base_url),
        "api_ip_configured": bool(credential_value(values, "TELEGRAM_API_IP")),
        "api_host_header": credential_value(values, "TELEGRAM_API_HOST_HEADER", DEFAULT_TELEGRAM_API_HOST),
        "proxy_configured": bool(credential_value(values, "TELEGRAM_PROXY_URL")),
        "custom_api_base_url": base_url != DEFAULT_TELEGRAM_API_BASE_URL,
    }


def likely_telegram_network_block(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "telegram" not in text and "api.telegram" not in text and "send " not in text:
        return False
    return any(
        marker in text
        for marker in (
            "connection reset",
            "connecterror",
            "networkerror",
            "no route to host",
            "operation timed out",
            "timed out",
            "tls",
        )
    )


def failure_payload(exc: Exception, *, token: str | None, credentials: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "error": redact_sensitive(str(exc), [token] if token else None),
        "delivered_count": 0,
    }
    if likely_telegram_network_block(exc):
        payload["network_blocked"] = True
        payload["telegram_transport"] = telegram_transport_summary(credentials)
        payload["hint"] = (
            "Telegram Bot API was unreachable from this machine. Configure TELEGRAM_PROXY_URL "
            "or TELEGRAM_API_BASE_URL in credentials.env or the environment. On networks that reset "
            "TLS SNI for api.telegram.org, set TELEGRAM_API_IP to an official Telegram API IP."
        )
    return payload


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "paper"


def sanitize_display_date(value: str | None) -> str:
    return (value or "").strip() or "sin fecha"


def resolve_artifacts(manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    media_manifest = load_json(manifest_path)

    processed_manifest_value = str(media_manifest.get("processed_manifest_path") or "").strip()
    if not processed_manifest_value:
        raise SystemExit("Media manifest is missing processed_manifest_path.")
    processed_manifest_path = Path(processed_manifest_value).expanduser()
    if not processed_manifest_path.exists():
        raise SystemExit(f"Processed manifest referenced by media manifest does not exist: {processed_manifest_path}")
    processed_manifest = load_json(processed_manifest_path)

    ranking_path_value = str(
        media_manifest.get("ranking_path") or processed_manifest.get("ranking_path") or ""
    ).strip()
    if not ranking_path_value:
        raise SystemExit("Delivery bundle is missing ranking_path.")
    ranking_path = Path(ranking_path_value).expanduser()
    if not ranking_path.exists():
        raise SystemExit(f"Ranking artifact referenced by delivery bundle does not exist: {ranking_path}")
    ranking_payload = load_json(ranking_path)

    digest_path_value = str(
        media_manifest.get("digest_path") or processed_manifest.get("digest_path") or ""
    ).strip()
    if not digest_path_value:
        raise SystemExit("Delivery bundle is missing digest_path.")
    digest_path = Path(digest_path_value).expanduser()
    if not digest_path.exists():
        raise SystemExit(f"Digest artifact referenced by delivery bundle does not exist: {digest_path}")
    digest_payload = load_json(digest_path)

    return media_manifest, processed_manifest, ranking_payload, digest_payload


def processed_entries_by_title(processed_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_results = processed_manifest.get("results")
    if not isinstance(raw_results, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if title:
            result[title] = entry
    return result


def normalize_delivery_plan(plan: dict[str, Any]) -> dict[str, Any]:
    intro_message = ensure_emoji(str(plan.get("intro_message") or ""), INTRO_EMOJI)
    raw_papers = plan.get("papers")
    if not intro_message:
        raise SystemExit("Delivery plan is missing intro_message.")
    if not isinstance(raw_papers, list):
        raise SystemExit("Delivery plan is missing a papers list.")

    papers: dict[str, dict[str, str]] = {}
    for raw_paper in raw_papers:
        if not isinstance(raw_paper, dict):
            continue
        title = str(raw_paper.get("title") or "").strip()
        message = strip_pdf_attachment_note(str(raw_paper.get("message") or ""))
        if not title:
            raise SystemExit("Delivery plan contains a paper without a title.")
        if not message:
            raise SystemExit(f"Delivery plan is missing message for paper: {title}")
        if title in papers:
            raise SystemExit(f"Delivery plan contains duplicate paper title: {title}")
        papers[title] = {
            "message": ensure_emoji(message, PAPER_MESSAGE_EMOJI),
        }
    return {"intro_message": intro_message, "papers": papers}


def load_delivery_plan(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Delivery plan not found: {path}")
    return normalize_delivery_plan(load_json(path))


def validate_delivery_plan(
    delivery_plan: dict[str, Any],
    *,
    raw_results: list[Any],
    delivery_entries: list[dict[str, Any]],
) -> None:
    if not str(delivery_plan.get("intro_message") or "").strip():
        raise SystemExit("Delivery plan is missing intro_message.")
    if not isinstance(delivery_plan.get("papers"), dict):
        raise SystemExit("Delivery plan is missing normalized papers.")
    result_titles = {
        str(entry.get("title") or "").strip()
        for entry in raw_results
        if isinstance(entry, dict) and str(entry.get("title") or "").strip()
    }
    deliverable_titles = {
        str(entry.get("title") or "").strip()
        for entry in delivery_entries
        if str(entry.get("title") or "").strip()
    }
    plan_titles = set(delivery_plan["papers"])
    missing = sorted(deliverable_titles - plan_titles)
    unknown = sorted(plan_titles - result_titles)
    if missing:
        raise SystemExit("Delivery plan is missing deliverable paper(s): " + "; ".join(missing))
    if unknown:
        raise SystemExit("Delivery plan contains unknown paper(s): " + "; ".join(unknown))
    for title in sorted(plan_titles & deliverable_titles):
        entry = delivery_plan["papers"][title]
        if not str(entry.get("message") or "").strip():
            raise SystemExit("Delivery plan entry must include non-empty message: " + title)


def media_file_name(*, title: str, media_type: str, digest_date: str) -> str:
    extensions = {"pdf": "pdf", "audio": "mp3", "video": "mp4"}
    try:
        extension = extensions[media_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported media type: {media_type}") from exc
    return f"{slugify(title)}-{media_type}-{digest_date}.{extension}"


def voice_file_name(*, title: str, digest_date: str) -> str:
    return f"{slugify(title)}-voice-{digest_date}.ogg"


def existing_file_path(path_value: Any, suffix: str) -> Path | None:
    path_text = str(path_value or "").strip()
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    if not path.exists() or path.stat().st_size <= 0:
        return None
    if path.suffix.lower() != suffix:
        return None
    return path


def artifact_path(artifact_payload: dict[str, Any], media_type: str) -> Path | None:
    suffix = ".mp3" if media_type == "audio" else ".mp4"
    return existing_file_path(artifact_payload.get("download_path"), suffix)


def pdf_path(processed_entry: dict[str, Any]) -> Path | None:
    for key in ("uploaded_pdf_path", "compressed_pdf_path", "original_pdf_path"):
        path = existing_file_path(processed_entry.get(key), ".pdf")
        if path is not None:
            return path
    return None


def missing_media_assets(entry: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if artifact_path(entry.get("audio") or {}, "audio") is None:
        missing.append("audio")
    if artifact_path(entry.get("video") or {}, "video") is None:
        missing.append("video")
    return missing


def skip_reason(entry: dict[str, Any]) -> str:
    error = str(entry.get("error") or "").strip()
    if error:
        return error
    missing = missing_media_assets(entry)
    if missing:
        return "Media incompleta: " + ", ".join(missing) + "."
    status = str(entry.get("status") or "").strip()
    if status and status != "ok":
        return f"Estado de media: {status}."
    return "Media no completada."


def completed_media_entry(entry: dict[str, Any]) -> bool:
    return entry.get("status") == "ok" and not missing_media_assets(entry)


def split_delivery_entries(
    raw_results: list[Any],
    *,
    completed_only: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    deliverable: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for raw_entry in raw_results:
        if not isinstance(raw_entry, dict):
            continue
        title = str(raw_entry.get("title") or "").strip() or "Untitled paper"
        if completed_only and not completed_media_entry(raw_entry):
            skipped.append({"title": title, "reason": skip_reason(raw_entry)})
            continue
        deliverable.append(raw_entry)
    return deliverable, skipped


def build_skipped_message(*, skipped: list[dict[str, str]]) -> str:
    lines = ["⚠️ <b>Pendientes para revisar despues</b>"]
    for item in skipped:
        title = html.escape(item["title"])
        reason = html.escape(item["reason"].rstrip("."))
        lines.append(f"- {title}: {reason}.")
    return "\n".join(lines)


def transient_telegram_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "network",
            "connecterror",
            "connection",
            "timeout",
            "timed out",
            "temporarily",
            "bad gateway",
            "502",
            "503",
            "504",
        )
    )


async def send_with_retries(
    operation: Any,
    *,
    label: str,
    retries: int = DEFAULT_TELEGRAM_RETRIES,
    retry_delay: float = DEFAULT_TELEGRAM_RETRY_DELAY,
) -> Any:
    attempts = max(1, retries)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001 - normalize Telegram transport failures.
            last_exc = exc
            if attempt == attempts or not transient_telegram_error(exc):
                break
            await asyncio.sleep(max(0.0, retry_delay))
    assert last_exc is not None
    raise RuntimeError(
        f"Failed to send {label} after {attempts} attempt(s): {redact_sensitive(str(last_exc))}"
    ) from last_exc


async def send_media_file(
    bot: Bot,
    *,
    chat_id: str,
    title: str,
    media_type: str,
    digest_date: str,
    caption: str,
    path: Path,
) -> None:
    async def send_once() -> Any:
        if media_type == "audio":
            with tempfile.TemporaryDirectory(prefix="echoes-voice-") as tmp:
                voice_path = Path(tmp) / "voice.ogg"
                convert_audio_to_voice(path, voice_path)
                with voice_path.open("rb") as handle:
                    input_file = InputFile(
                        handle,
                        filename=voice_file_name(title=title, digest_date=digest_date),
                    )
                    return await bot.send_voice(
                        chat_id=chat_id,
                        voice=input_file,
                        caption=caption,
                        parse_mode=TELEGRAM_PARSE_MODE,
                    )

        with path.open("rb") as handle:
            filename = media_file_name(title=title, media_type=media_type, digest_date=digest_date)
            input_file = InputFile(handle, filename=filename)
            return await bot.send_document(
                    chat_id=chat_id,
                    document=input_file,
                    caption=caption,
                    parse_mode=TELEGRAM_PARSE_MODE,
                    disable_content_type_detection=False if media_type == "pdf" else None,
                )

    await send_with_retries(send_once, label=f"{media_type} for {title}")


def convert_audio_to_voice(source_path: Path, destination_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to convert MP3 audio into a Telegram voice note.")
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-map_metadata",
        "-1",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-f",
        "ogg",
        str(destination_path),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0 or not destination_path.exists() or destination_path.stat().st_size <= 0:
        error = completed.stderr.strip() or completed.stdout.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"Failed to convert audio to Telegram voice note: {error}")


def positive_int(value: Any) -> int | None:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def probe_video_metadata(path: Path) -> dict[str, int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to read video dimensions before Telegram upload.")

    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or "unknown ffprobe error"
        raise RuntimeError(f"Failed to read video metadata: {error}")

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON while reading video metadata.") from exc

    streams = payload.get("streams")
    stream = streams[0] if isinstance(streams, list) and streams and isinstance(streams[0], dict) else {}
    metadata: dict[str, int] = {}
    for key in ("width", "height"):
        value = positive_int(stream.get(key))
        if value is not None:
            metadata[key] = value

    duration = positive_int(stream.get("duration"))
    if duration is None and isinstance(payload.get("format"), dict):
        duration = positive_int(payload["format"].get("duration"))
    if duration is not None:
        metadata["duration"] = duration

    return metadata


def telegram_safe_video_path(path: Path) -> Path:
    return path.parent / "telegram-safe" / f"{path.stem}-under-50mb{path.suffix}"


def make_telegram_safe_video_copy(
    source_path: Path,
    *,
    target_path: Path | None = None,
    target_bytes: int = TELEGRAM_SAFE_VIDEO_TARGET_BYTES,
) -> Path:
    destination = target_path or telegram_safe_video_path(source_path)
    if destination.exists() and 0 < destination.stat().st_size <= target_bytes:
        return destination

    metadata = probe_video_metadata(source_path)
    duration = max(1, int(metadata.get("duration") or 1))
    total_bitrate = max(
        TELEGRAM_SAFE_VIDEO_MIN_VIDEO_BITRATE + TELEGRAM_SAFE_VIDEO_AUDIO_BITRATE,
        int((target_bytes * 8 * 0.92) / duration),
    )
    video_bitrate = max(
        TELEGRAM_SAFE_VIDEO_MIN_VIDEO_BITRATE,
        total_bitrate - TELEGRAM_SAFE_VIDEO_AUDIO_BITRATE,
    )
    maxrate = int(video_bitrate * 1.1)
    bufsize = int(maxrate * 2)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to create a Telegram-safe video copy.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    temp_path.unlink(missing_ok=True)
    for attempt in range(2):
        adjusted_video_bitrate = int(video_bitrate * (0.85 ** attempt))
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            str(adjusted_video_bitrate),
            "-maxrate",
            str(int(maxrate * (0.85 ** attempt))),
            "-bufsize",
            str(int(bufsize * (0.85 ** attempt))),
            "-c:a",
            "aac",
            "-b:a",
            str(TELEGRAM_SAFE_VIDEO_AUDIO_BITRATE),
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size <= 0:
            error = completed.stderr.strip() or completed.stdout.strip() or "unknown ffmpeg error"
            raise RuntimeError(f"Failed to create Telegram-safe video copy: {error}")
        if temp_path.stat().st_size <= target_bytes:
            temp_path.replace(destination)
            return destination
        temp_path.unlink(missing_ok=True)

    raise RuntimeError(
        "Failed to create Telegram-safe video copy under "
        f"{target_bytes} bytes from {source_path}."
    )


async def send_video_file(
    bot: Bot,
    *,
    chat_id: str,
    title: str,
    digest_date: str,
    caption: str,
    path: Path,
) -> tuple[str, dict[str, str] | None]:
    filename = media_file_name(title=title, media_type="video", digest_date=digest_date)

    async def send_video_once(upload_path: Path) -> Any:
        metadata = probe_video_metadata(upload_path)
        with upload_path.open("rb") as handle:
            input_file = InputFile(handle, filename=filename)
            return await bot.send_video(
                chat_id=chat_id,
                video=input_file,
                caption=caption,
                parse_mode=TELEGRAM_PARSE_MODE,
                supports_streaming=True,
                width=metadata.get("width"),
                height=metadata.get("height"),
                duration=metadata.get("duration"),
            )

    async def send_document_once(upload_path: Path) -> Any:
        with upload_path.open("rb") as handle:
            input_file = InputFile(handle, filename=filename)
            return await bot.send_document(
                chat_id=chat_id,
                document=input_file,
                caption=caption,
                parse_mode=TELEGRAM_PARSE_MODE,
            )

    try:
        await send_with_retries(lambda: send_video_once(path), label=f"video for {title}")
        return "video", None
    except Exception as video_exc:  # noqa: BLE001 - always fallback after inline video upload fails.
        fallback_reason = redact_sensitive(str(video_exc))

    try:
        await send_with_retries(lambda: send_document_once(path), label=f"video document fallback for {title}")
        return (
            "video_document_fallback",
            {
                "asset": "video",
                "from": "send_video",
                "to": "send_document",
                "reason": fallback_reason,
            },
        )
    except Exception as document_exc:  # noqa: BLE001 - large hosted Bot API uploads need a smaller copy.
        if path.stat().st_size <= HOSTED_BOT_API_UPLOAD_LIMIT_BYTES:
            raise
        safe_path = make_telegram_safe_video_copy(path)
        safe_reason = redact_sensitive(str(document_exc))

    try:
        await send_with_retries(lambda: send_video_once(safe_path), label=f"Telegram-safe video for {title}")
        return (
            "video_transcoded_fallback",
            {
                "asset": "video",
                "from": "send_video",
                "to": "send_video",
                "reason": fallback_reason,
                "transcoded_path": str(safe_path),
                "transcode_reason": safe_reason,
            },
        )
    except Exception:
        await send_with_retries(
            lambda: send_document_once(safe_path),
            label=f"Telegram-safe video document fallback for {title}",
        )
    return (
        "video_document_transcoded_fallback",
        {
            "asset": "video",
            "from": "send_video",
            "to": "send_document",
            "reason": fallback_reason,
            "transcoded_path": str(safe_path),
            "transcode_reason": safe_reason,
        },
    )


async def deliver_bundle(
    *,
    bot: Bot,
    chat_id: str,
    media_manifest: dict[str, Any],
    processed_manifest: dict[str, Any],
    ranking_payload: dict[str, Any],
    digest_payload: dict[str, Any],
    delivery_plan: dict[str, Any],
    completed_only: bool = False,
) -> dict[str, Any]:
    digest_date = sanitize_display_date(
        str(digest_payload.get("effective_digest_date") or media_manifest.get("processed_digest_date") or "")
    )
    processed_by_title = processed_entries_by_title(processed_manifest)
    raw_results = media_manifest.get("results")
    if not isinstance(raw_results, list):
        raise SystemExit("Media manifest is missing results.")
    delivery_entries, skipped_papers = split_delivery_entries(
        raw_results,
        completed_only=completed_only,
    )
    validate_delivery_plan(delivery_plan, raw_results=raw_results, delivery_entries=delivery_entries)

    papers: list[dict[str, Any]] = []
    await send_with_retries(
        lambda: bot.send_message(
            chat_id=chat_id,
            text=delivery_plan["intro_message"],
            parse_mode=TELEGRAM_PARSE_MODE,
        ),
        label="intro message",
    )

    for entry in delivery_entries:
        title = str(entry.get("title") or "").strip() or "Untitled paper"
        processed_entry = processed_by_title.get(title)
        if not processed_entry:
            papers.append(
                {
                    "title": title,
                    "status": "failed",
                    "error": "Processed manifest entry not found for delivered paper.",
                    "missing_assets": ["processed_entry"],
                    "fallbacks": [],
                }
            )
            continue

        planned_paper = delivery_plan["papers"][title]
        paper_record = {
            "title": title,
            "status": "ok",
            "missing_assets": [],
            "sent": ["summary"],
            "delivery_methods": {},
            "fallbacks": [],
            "error": None,
        }

        try:
            await send_with_retries(
                lambda: bot.send_message(
                    chat_id=chat_id,
                    text=planned_paper["message"],
                    parse_mode=TELEGRAM_PARSE_MODE,
                    disable_web_page_preview=False,
                ),
                label=f"summary for {title}",
            )
        except Exception as exc:  # noqa: BLE001
            paper_record["status"] = "failed"
            paper_record["error"] = f"Failed to send summary: {exc}"
            papers.append(paper_record)
            continue

        audio_path = artifact_path(entry.get("audio") or {}, "audio")
        video_path = artifact_path(entry.get("video") or {}, "video")

        if audio_path is None:
            paper_record["missing_assets"].append("audio")
        else:
            try:
                await send_media_file(
                    bot,
                    chat_id=chat_id,
                    title=title,
                    media_type="audio",
                    digest_date=digest_date,
                    caption=generated_media_caption(title=title, emoji=AUDIO_EMOJI),
                    path=audio_path,
                )
                paper_record["sent"].append("audio")
                paper_record["delivery_methods"]["audio"] = "voice"
            except Exception as exc:  # noqa: BLE001
                paper_record["status"] = "partial_failure"
                paper_record["error"] = f"Failed to send audio: {exc}"

        if video_path is None:
            paper_record["missing_assets"].append("video")
        else:
            try:
                sent_as = await send_video_file(
                    bot,
                    chat_id=chat_id,
                    title=title,
                    digest_date=digest_date,
                    caption=generated_media_caption(title=title, emoji=VIDEO_EMOJI),
                    path=video_path,
                )
                paper_record["sent"].append("video")
                paper_record["delivery_methods"]["video"] = sent_as[0]
                if sent_as[1] is not None:
                    paper_record["fallbacks"].append(sent_as[1])
            except Exception as exc:  # noqa: BLE001
                paper_record["status"] = "partial_failure"
                paper_record["error"] = f"Failed to send video: {exc}"

        if paper_record["missing_assets"] and paper_record["status"] == "ok":
            paper_record["status"] = "partial_failure"
        papers.append(paper_record)

    if skipped_papers:
        await send_with_retries(
            lambda: bot.send_message(
                chat_id=chat_id,
                text=build_skipped_message(skipped=skipped_papers),
                parse_mode=TELEGRAM_PARSE_MODE,
            ),
            label="skipped-paper message",
        )

    delivered_count = sum(1 for item in papers if item["status"] == "ok")
    return {
        "media_manifest_path": media_manifest.get("media_manifest_path"),
        "digest_date": digest_date,
        "selected_count": len(papers),
        "source_selected_count": len(raw_results),
        "delivered_count": delivered_count,
        "partial_count": sum(1 for item in papers if item["status"] == "partial_failure"),
        "failed_count": sum(1 for item in papers if item["status"] == "failed"),
        "fallback_count": sum(len(item.get("fallbacks", [])) for item in papers),
        "skipped_count": len(skipped_papers),
        "skipped_papers": skipped_papers,
        "results": papers,
        "status": "ok" if delivered_count == len(papers) and not skipped_papers else "partial_failure",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        help="Media manifest path. Defaults to the latest <repo>/.echoes/processed/*/media-manifest.json.",
    )
    parser.add_argument("--delivery-plan", required=True, help="Codex-authored delivery-plan.json path.")
    parser.add_argument(
        "--completed-only",
        action="store_true",
        help="Send only papers with completed audio and video, then report skipped papers.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    credentials: dict[str, str] = {}
    token: str | None = None

    try:
        manifest_path = choose_manifest_path(args.manifest)
        media_manifest, processed_manifest, ranking_payload, digest_payload = resolve_artifacts(manifest_path)
        media_manifest["media_manifest_path"] = str(manifest_path)
        delivery_plan = load_delivery_plan(Path(args.delivery_plan).expanduser())

        credentials = load_credentials()
        token, chat_id = require_telegram_credentials(credentials)

        bot = build_telegram_bot(token, credentials)
        payload = asyncio.run(
            deliver_bundle(
                bot=bot,
                chat_id=chat_id,
                media_manifest=media_manifest,
                processed_manifest=processed_manifest,
                ranking_payload=ranking_payload,
                digest_payload=digest_payload,
                delivery_plan=delivery_plan,
                completed_only=args.completed_only,
            )
        )
    except SystemExit as exc:
        payload = failure_payload(exc, token=token, credentials=credentials)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(payload["error"])
        return 1
    except Exception as exc:  # noqa: BLE001 - keep automation output machine-readable.
        payload = failure_payload(exc, token=token, credentials=credentials)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(payload["error"])
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Delivered {payload['delivered_count']} of {payload['selected_count']} paper(s) "
            f"from {manifest_path}."
        )
        for result in payload["results"]:
            if result["status"] == "ok":
                print(f"- OK: {result['title']}")
            else:
                missing = ", ".join(result["missing_assets"]) or result["error"] or "unknown issue"
                print(f"- INCOMPLETE: {result['title']} -> {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
