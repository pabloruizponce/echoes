#!/usr/bin/env python3
"""Requests helpers that can survive a broken local DNS resolver."""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator
from urllib.parse import urlparse

import requests


DNS_ERROR_MARKERS = (
    "NameResolutionError",
    "Temporary failure in name resolution",
    "Name or service not known",
    "nodename nor servname",
    "Failed to resolve",
    "getaddrinfo failed",
    "gaierror",
)
DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://1.0.0.1/dns-query",
)
DEFAULT_DOH_TIMEOUT = 5.0
STATIC_HOST_IPS = {
    "api.scholar-inbox.com": ("104.21.78.3", "172.67.214.65"),
    "www.scholar-inbox.com": ("104.21.78.3", "172.67.214.65"),
}


def dns_fallback_enabled() -> bool:
    value = os.environ.get("ECHOES_DNS_FALLBACK", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


def is_dns_resolution_error(exc: BaseException) -> bool:
    text = exception_chain_text(exc)
    return any(marker in text for marker in DNS_ERROR_MARKERS)


def resolve_host_with_doh(hostname: str, *, timeout: float = DEFAULT_DOH_TIMEOUT) -> list[str]:
    errors: list[str] = []
    for endpoint in DOH_ENDPOINTS:
        try:
            response = requests.get(
                endpoint,
                params={"name": hostname, "type": "A"},
                headers={"accept": "application/dns-json"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            errors.append(f"{endpoint}: {exc}")
            continue
        answers = payload.get("Answer") if isinstance(payload, dict) else None
        if not isinstance(answers, list):
            continue
        ips = [
            str(answer.get("data"))
            for answer in answers
            if isinstance(answer, dict) and answer.get("type") == 1 and answer.get("data")
        ]
        if ips:
            return ips
    if errors:
        raise RuntimeError("; ".join(errors))
    return []


def static_host_ips(hostname: str) -> list[str]:
    return list(STATIC_HOST_IPS.get(hostname, ()))


def fallback_ips_for_host(hostname: str) -> list[str]:
    try:
        ips = resolve_host_with_doh(hostname)
    except Exception:
        ips = []
    return ips or static_host_ips(hostname)


@contextlib.contextmanager
def patched_getaddrinfo(hostname: str, ips: list[str]) -> Iterator[None]:
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(
        host: str | bytes | None,
        port: str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]]:
        requested = host.decode() if isinstance(host, bytes) else host
        if requested != hostname:
            return original_getaddrinfo(host, port, family, type, proto, flags)

        results = []
        last_error: socket.gaierror | None = None
        for ip in ips:
            try:
                results.extend(original_getaddrinfo(ip, port, family, type, proto, flags))
            except socket.gaierror as exc:
                last_error = exc
        if results:
            return results
        if last_error is not None:
            raise last_error
        return original_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def request_with_dns_fallback(method: str, url: str, **kwargs: object) -> requests.Response:
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        if not dns_fallback_enabled() or not is_dns_resolution_error(exc):
            raise

        hostname = urlparse(url).hostname
        if not hostname:
            raise

        ips = fallback_ips_for_host(hostname)
        if not ips:
            raise exc

        with patched_getaddrinfo(hostname, ips):
            return requests.request(method, url, **kwargs)
