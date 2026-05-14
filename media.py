"""Authenticated Home Assistant media fetch helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

try:
    import aiohttp
except ImportError:  # pragma: no cover - adapter-level checks cover this
    aiohttp = None  # type: ignore[assignment]


_ALLOWED_MEDIA_PREFIXES = (
    "/api/camera_proxy/",
    "/api/image_proxy/",
)
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_CACHE_BYTES = 50 * 1024 * 1024
_MAX_CACHE_FILES = 50
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _is_allowed_ha_media_url(base_url: str, media_url: str) -> bool:
    base = urlparse(base_url)
    parsed = urlparse(urljoin(f"{base_url}/", media_url))
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc != base.netloc:
        return False
    return any(parsed.path.startswith(prefix) for prefix in _ALLOWED_MEDIA_PREFIXES)


def _media_url(base_url: str, media_ref: str) -> str:
    if not _is_allowed_ha_media_url(base_url, media_ref):
        raise RuntimeError(f"Unsupported HA media URL/path: {media_ref}")
    return urljoin(f"{base_url}/", media_ref)


def _cache_dir() -> Path:
    hermes_home = os.getenv("HERMES_HOME")
    root = Path(hermes_home).expanduser() if hermes_home else Path(tempfile.gettempdir())
    return root / "hermes-ha-media"


def _cleanup_cache(path: Path) -> None:
    cutoff = time.time() - _CACHE_TTL_SECONDS
    for item in path.glob("ha_media_*"):
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass
    _prune_cache(path)


def _prune_cache(path: Path) -> None:
    files = []
    total = 0
    for item in path.glob("ha_media_*"):
        try:
            stat = item.stat()
        except OSError:
            continue
        if not item.is_file():
            continue
        files.append((stat.st_mtime, stat.st_size, item))
        total += stat.st_size

    files.sort()
    while files and (len(files) > _MAX_CACHE_FILES or total > _MAX_CACHE_BYTES):
        _, size, item = files.pop(0)
        try:
            item.unlink()
            total -= size
        except OSError:
            pass


def _ensure_image_content_type(content_type: str) -> None:
    if not content_type.lower().split(";", 1)[0].strip().startswith("image/"):
        raise RuntimeError(f"HA media is not an image: {content_type or 'unknown content-type'}")


def _reject_redirect(status: int, location: str | None = None) -> None:
    if 300 <= status < 400:
        target = f" to {location}" if location else ""
        raise RuntimeError(f"HA media redirect rejected{target}")


def _check_content_length(value: str | None) -> None:
    if not value:
        return
    try:
        length = int(value)
    except ValueError:
        return
    if length > _MAX_IMAGE_BYTES:
        raise RuntimeError(f"HA media is too large: {length} bytes")


def _extension(content_type: str, url: str) -> str:
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


async def fetch_media(args: dict[str, Any], *, base_url: str, token: str) -> str:
    if aiohttp is None:
        raise RuntimeError("aiohttp is not installed")
    if not token:
        raise RuntimeError("HASS_TOKEN is not configured")

    entity_id = str(args.get("entity_id") or "").strip()
    media_ref = str(args.get("path") or args.get("url") or "").strip()
    state: dict[str, Any] | None = None

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        if entity_id:
            state_url = f"{base_url}/api/states/{quote(entity_id, safe='')}"
            async with session.get(state_url, headers=_headers(token)) as resp:
                body = await resp.text()
                if resp.status >= 300:
                    raise RuntimeError(f"HA state HTTP {resp.status}: {body[:500]}")
                state = json.loads(body)
            attrs = state.get("attributes") or {}
            media_ref = media_ref or str(attrs.get("entity_picture") or "")

        if not media_ref:
            raise RuntimeError("ha_fetch_media requires entity_id with entity_picture, path, or url")

        url = _media_url(base_url, media_ref)
        async with session.get(url, headers=_headers(token), allow_redirects=False) as resp:
            _reject_redirect(resp.status, resp.headers.get("location"))
            if resp.status >= 300:
                body = await resp.read()
                text = body[:500].decode("utf-8", errors="replace")
                raise RuntimeError(f"HA media HTTP {resp.status}: {text}")
            content_type = resp.headers.get("content-type", "")
            _ensure_image_content_type(content_type)
            _check_content_length(resp.headers.get("content-length"))

            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > _MAX_IMAGE_BYTES:
                    raise RuntimeError(f"HA media is too large: over {_MAX_IMAGE_BYTES} bytes")
                chunks.append(chunk)
            body = b"".join(chunks)

    out_dir = _cache_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_cache(out_dir)
    path = out_dir / f"ha_media_{uuid.uuid4().hex[:12]}{_extension(content_type, url)}"
    path.write_bytes(body)
    _prune_cache(out_dir)
    return json.dumps(
        {
            "success": True,
            "path": str(path),
            "entity_id": entity_id or None,
            "content_type": content_type or None,
            "bytes": len(body),
            "source_path": urlparse(url).path,
        },
        ensure_ascii=False,
    )
