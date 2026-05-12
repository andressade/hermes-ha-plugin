from __future__ import annotations

import asyncio
from typing import Any


def render_payload(value: Any, response: str) -> Any:
    if isinstance(value, str):
        return value.replace("{response}", response)
    if isinstance(value, list):
        return [render_payload(item, response) for item in value]
    if isinstance(value, dict):
        return {key: render_payload(val, response) for key, val in value.items()}
    return value


async def deliver_to_platform(
    target: str,
    content: str,
    *,
    adapters: dict[Any, Any] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    target = str(target or "").strip()
    if not target:
        raise RuntimeError("delivery response sink missing target")
    platform_name, sep, target_ref = target.partition(":")
    platform_name = platform_name.strip().lower()
    target_ref = target_ref.strip() if sep else ""

    from gateway.config import Platform, load_gateway_config
    from gateway.platforms.base import BasePlatformAdapter
    from tools.send_message_tool import _parse_target_ref, _send_to_platform

    config = load_gateway_config()
    platform = Platform(platform_name)
    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        raise RuntimeError(f"Platform '{platform_name}' is not configured/enabled")

    chat_id = ""
    thread_id = None
    if target_ref:
        chat_id, thread_id, explicit = _parse_target_ref(platform_name, target_ref)
        if not explicit:
            chat_id = target_ref
            thread_id = None
            try:
                from gateway.channel_directory import resolve_channel_name

                resolved = resolve_channel_name(platform_name, target_ref)
                if resolved:
                    resolved_chat_id, resolved_thread_id, resolved_explicit = _parse_target_ref(platform_name, resolved)
                    chat_id = resolved_chat_id if resolved_explicit else resolved
                    thread_id = resolved_thread_id if resolved_explicit else None
            except Exception:
                pass
        if not chat_id:
            raise RuntimeError(f"Could not resolve delivery target: {target}")
    else:
        home = config.get_home_channel(platform)
        if not home:
            raise RuntimeError(f"No home channel configured for {platform_name}")
        chat_id = home.chat_id
        thread_id = getattr(home, "thread_id", None)

    media_files, cleaned_content = BasePlatformAdapter.extract_media(content)
    runtime_adapter = (adapters or {}).get(platform)
    if runtime_adapter is not None and not media_files:
        metadata = {"thread_id": thread_id} if thread_id else None
        try:
            send_coro = runtime_adapter.send(chat_id, cleaned_content, metadata=metadata)
            if loop is not None and loop.is_running() and loop is not asyncio.get_running_loop():
                result = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(send_coro, loop))
            else:
                result = await send_coro
            if not getattr(result, "success", False):
                raise RuntimeError(getattr(result, "error", None) or "live adapter delivery failed")
            return
        except Exception:
            pass

    result = await _send_to_platform(
        platform,
        pconfig,
        chat_id,
        cleaned_content,
        thread_id=thread_id,
        media_files=media_files,
    )
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(str(result["error"]))


async def post_webhook(url: str, payload: Any) -> None:
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError("aiohttp is not installed") from exc
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(url, json=payload) as resp:
            if resp.status >= 300:
                body = await resp.text()
                raise RuntimeError(f"webhook HTTP {resp.status}: {body[:500]}")
