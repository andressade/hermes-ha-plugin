"""Home Assistant event-trigger plugin for Hermes Agent."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

try:
    from .schemas import (
        FETCH_MEDIA_SCHEMA,
        LIST_EVENTS_SCHEMA,
        RECENT_EVENTS_SCHEMA,
    )
    from .media import fetch_media as _fetch_media
    from .sinks import deliver_to_platform as _deliver_to_platform
    from .sinks import post_webhook as _post_webhook
    from .sinks import render_payload as _render_payload
except ImportError:
    from schemas import (
        FETCH_MEDIA_SCHEMA,
        LIST_EVENTS_SCHEMA,
        RECENT_EVENTS_SCHEMA,
    )
    from media import fetch_media as _fetch_media
    from sinks import deliver_to_platform as _deliver_to_platform
    from sinks import post_webhook as _post_webhook
    from sinks import render_payload as _render_payload

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://homeassistant.local:8123"
MAX_RECENT_EVENTS = 200
RECENT_EVENTS: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_EVENTS)
LAST_SETTINGS: dict[str, str] = {}
_AUTH_ERROR_STATUSES = {401, 403}


class HomeAssistantAuthError(RuntimeError):
    """Home Assistant rejected authentication; retrying can trigger IP bans."""


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE and bool(os.getenv("HASS_TOKEN"))


def check_tool_requirements() -> bool:
    return AIOHTTP_AVAILABLE and bool(os.getenv("HASS_TOKEN") or LAST_SETTINGS.get("token"))


def _extra(config: Any) -> dict[str, Any]:
    return getattr(config, "extra", None) or {}


def _ha_settings(config: Any | None = None) -> tuple[str, str]:
    extra = _extra(config) if config is not None else {}
    url = extra.get("url") or os.getenv("HASS_URL") or LAST_SETTINGS.get("url") or DEFAULT_URL
    token = (
        getattr(config, "token", None)
        or extra.get("token")
        or os.getenv("HASS_TOKEN")
        or LAST_SETTINGS.get("token")
        or ""
    )
    return str(url).rstrip("/"), str(token)


def _get_path(data: Any, path: str) -> Any:
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def _match_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if "exists" in expected:
            return (actual is not None) is bool(expected["exists"])
        if "equals" in expected and actual != expected["equals"]:
            return False
        if "not_equals" in expected and actual == expected["not_equals"]:
            return False
        if "in" in expected and actual not in expected["in"]:
            return False
        if "regex" in expected and not re.search(str(expected["regex"]), str(actual or "")):
            return False
        if "glob" in expected and not fnmatch.fnmatch(str(actual or ""), str(expected["glob"])):
            return False
        return True
    if isinstance(expected, list):
        return actual in expected
    if isinstance(expected, str) and any(ch in expected for ch in "*?[]"):
        return fnmatch.fnmatch(str(actual or ""), expected)
    return actual == expected


def event_matches(event: dict[str, Any], trigger: dict[str, Any]) -> bool:
    if trigger.get("enabled") is False:
        return False
    event_type = trigger.get("event_type")
    if event_type and event.get("event_type") != event_type:
        return False
    for path, expected in (trigger.get("match") or {}).items():
        if not _match_value(_get_path(event, str(path)), expected):
            return False
    return True


def render_template(template: str, event: dict[str, Any], trigger: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key == "json":
            return json.dumps(event, ensure_ascii=False, sort_keys=True)
        if key.startswith("event."):
            value = _get_path(event, key[6:])
            return "" if value is None else str(value)
        if key.startswith("trigger."):
            value = _get_path(trigger, key[8:])
            return "" if value is None else str(value)
        return match.group(0)

    return re.sub(r"\{([^{}]+)\}", replace, str(template or ""))


async def _request(method: str, path: str, *, config: Any | None = None, json_body: Any = None) -> Any:
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is not installed")
    url, token = _ha_settings(config)
    if not token:
        raise RuntimeError("HASS_TOKEN is not configured")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.request(method, f"{url}{path}", headers=headers, json=json_body) as resp:
            body = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(f"HA HTTP {resp.status}: {body[:500]}")
            if not body:
                return None
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body


_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BLOCKED_DOMAINS = frozenset({
    "shell_command",
    "command_line",
    "python_script",
    "pyscript",
    "hassio",
    "rest_command",
})


async def _call_service(
    domain: str,
    service: str,
    service_data: dict | None = None,
    target: dict | None = None,
) -> Any:
    if not _SERVICE_NAME_RE.match(domain):
        raise RuntimeError(f"Invalid HA service domain: {domain!r}")
    if not _SERVICE_NAME_RE.match(service):
        raise RuntimeError(f"Invalid HA service name: {service!r}")
    if domain in _BLOCKED_DOMAINS:
        raise RuntimeError(f"HA service domain is blocked: {domain}")
    payload: dict[str, Any] = {}
    if service_data:
        payload.update(service_data)
    if target:
        payload["target"] = target
    return await _request("POST", f"/api/services/{domain}/{service}", json_body=payload)


async def ha_list_events(args: dict[str, Any], **_: Any) -> str:
    return json.dumps(await _request("GET", "/api/events"), ensure_ascii=False)


async def ha_recent_events(args: dict[str, Any], **_: Any) -> str:
    event_type = str(args.get("event_type") or "")
    limit = max(1, min(int(args.get("limit") or 20), MAX_RECENT_EVENTS))
    events = [event for event in reversed(RECENT_EVENTS) if not event_type or event.get("event_type") == event_type]
    return json.dumps(events[:limit], ensure_ascii=False)


async def ha_fetch_media(args: dict[str, Any], **_: Any) -> str:
    url, token = _ha_settings()
    return await _fetch_media(args, base_url=url, token=token)


class HomeAssistantAgentAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 8192
    _BACKOFF_STEPS = [5, 10, 30, 60]

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("homeassistant"))
        extra = _extra(config)
        self._hass_url, self._hass_token = _ha_settings(config)
        LAST_SETTINGS.update({"url": self._hass_url, "token": self._hass_token})
        self._sync_ha_tool_settings()
        self._triggers = list(extra.get("triggers") or [])
        self._listen_events = sorted({str(t.get("event_type")) for t in self._triggers if t.get("event_type")})
        self._listen_events += [e for e in extra.get("listen_events", []) if e not in self._listen_events]
        self._default_response = extra.get("response") or {"type": "none"}
        self._response_by_chat_id: dict[str, dict[str, Any]] = {}
        self._session = None
        self._ws = None
        self._listen_task: asyncio.Task | None = None
        self._msg_id = 0
        self._last_trigger_time: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "Home Assistant"

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _sync_ha_tool_settings(self) -> None:
        os.environ["HASS_URL"] = self._hass_url
        if self._hass_token:
            os.environ["HASS_TOKEN"] = self._hass_token
        try:
            from tools import homeassistant_tool
            from tools.registry import invalidate_check_fn_cache

            homeassistant_tool._HASS_URL = self._hass_url
            homeassistant_tool._HASS_TOKEN = self._hass_token
            invalidate_check_fn_cache()
        except Exception:
            logger.debug("[%s] Could not sync built-in HA tool settings", self.name, exc_info=True)

    def _mark_auth_fatal(self, exc: BaseException) -> str:
        message = (
            f"Home Assistant authentication rejected: {exc}. "
            "Hermes will stop reconnecting to avoid HA IP bans. "
            "Check HASS_TOKEN or HA auth, and remove the host from HA ip_bans.yaml before restarting."
        )
        self._set_fatal_error("homeassistant_auth_rejected", message, retryable=False)
        return message

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed. Install hermes-agent[homeassistant].", self.name)
            return False
        if not self._hass_token:
            logger.warning("[%s] HASS_TOKEN is not configured", self.name)
            return False
        if not self._triggers:
            logger.warning("[%s] No extra.triggers configured; events will be recorded only.", self.name)
        try:
            await self._connect_ws()
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._mark_connected()
            logger.info("[%s] Connected to %s", self.name, self._hass_url)
            return True
        except HomeAssistantAuthError as exc:
            logger.error("[%s] Failed to connect: %s", self.name, self._mark_auth_fatal(exc))
            await self._cleanup_ws()
            return False
        except Exception as exc:
            logger.error("[%s] Failed to connect: %s", self.name, exc)
            await self._cleanup_ws()
            return False

    async def _connect_ws(self) -> None:
        ws_url = self._hass_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            self._ws = await self._session.ws_connect(ws_url, heartbeat=30, timeout=30)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status in _AUTH_ERROR_STATUSES:
                raise HomeAssistantAuthError(f"HTTP {status} from {ws_url}") from exc
            raise
        msg = await self._ws.receive_json()
        if msg.get("type") == "auth_invalid":
            raise HomeAssistantAuthError(msg.get("message") or msg)
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got {msg.get('type')}")
        await self._ws.send_json({"type": "auth", "access_token": self._hass_token})
        msg = await self._ws.receive_json()
        if msg.get("type") == "auth_invalid":
            raise HomeAssistantAuthError(msg.get("message") or msg)
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {msg}")
        event_types = self._listen_events or [None]
        for event_type in event_types:
            sub_id = self._next_id()
            payload = {"id": sub_id, "type": "subscribe_events"}
            if event_type:
                payload["event_type"] = event_type
            await self._ws.send_json(payload)
            ack = await self._receive_result(sub_id)
            if not ack.get("success"):
                raise RuntimeError(f"HA subscribe failed: {ack}")

    async def _receive_result(self, message_id: int) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("HA WebSocket is not connected")
        while True:
            msg = await self._ws.receive_json()
            if msg.get("type") == "result" and msg.get("id") == message_id:
                return msg
            if msg.get("type") == "event":
                logger.debug("Ignoring HA event received during subscription setup")

    async def _cleanup_ws(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        self._listen_task = None
        await self._cleanup_ws()

    async def _listen_loop(self) -> None:
        backoff_idx = 0
        while self.is_connected:
            try:
                await self._read_events()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[%s] WebSocket error: %s", self.name, exc)
            if not self.is_connected:
                return
            delay = self._BACKOFF_STEPS[min(backoff_idx, len(self._BACKOFF_STEPS) - 1)]
            await asyncio.sleep(delay)
            backoff_idx += 1
            try:
                await self._cleanup_ws()
                await self._connect_ws()
                backoff_idx = 0
            except HomeAssistantAuthError as exc:
                logger.error("[%s] Reconnect stopped: %s", self.name, self._mark_auth_fatal(exc))
                await self._cleanup_ws()
                await self._notify_fatal_error()
                return
            except Exception as exc:
                logger.warning("[%s] Reconnect failed: %s", self.name, exc)

    async def _read_events(self) -> None:
        if self._ws is None or self._ws.closed:
            return
        async for ws_msg in self._ws:
            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(ws_msg.data)
                if data.get("type") == "event":
                    await self._handle_ha_event(data.get("event") or {})
            elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _handle_ha_event(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("time_fired", datetime.now().isoformat())
        RECENT_EVENTS.append(event)
        for trigger in self._triggers:
            if event_matches(event, trigger) and self._cooldown_ok(trigger):
                await self._dispatch_trigger(event, trigger)

    def _cooldown_ok(self, trigger: dict[str, Any]) -> bool:
        seconds = int(trigger.get("cooldown_seconds") or 0)
        if seconds <= 0:
            return True
        key = str(trigger.get("name") or trigger.get("event_type") or id(trigger))
        now = time.time()
        if now - self._last_trigger_time.get(key, 0) < seconds:
            return False
        self._last_trigger_time[key] = now
        return True

    async def _dispatch_trigger(self, event: dict[str, Any], trigger: dict[str, Any]) -> None:
        prompt = render_template(str(trigger.get("prompt") or ""), event, trigger).strip()
        if not prompt:
            logger.warning("[%s] Trigger %s matched but has no prompt", self.name, trigger.get("name"))
            return
        message_id = f"ha_{uuid.uuid4().hex[:12]}"
        base_chat_id = str(trigger.get("chat_id") or f"ha_event:{trigger.get('name') or 'trigger'}")
        chat_id = base_chat_id if trigger.get("shared_session") is True else f"{base_chat_id}:{message_id}"
        response = trigger.get("response")
        if isinstance(response, dict):
            self._response_by_chat_id[chat_id] = response
        source = self.build_source(
            chat_id=chat_id,
            chat_name=str(trigger.get("chat_name") or "Home Assistant Events"),
            chat_type="channel",
            user_id="homeassistant",
            user_name="Home Assistant",
        )
        msg_event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={"ha_event": event, "ha_trigger": trigger},
            message_id=message_id,
            timestamp=datetime.now(),
        )
        await self.handle_message(msg_event)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        chat_key = str(chat_id)
        response = dict(self._default_response)
        trigger_response = self._response_by_chat_id.get(chat_key)
        if isinstance(trigger_response, dict):
            response.update(trigger_response)
        try:
            await self._deliver_response(response, content)
            self._response_by_chat_id.pop(chat_key, None)
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def _deliver_response(self, response: dict[str, Any], content: str) -> None:
        sink_type = str(response.get("type") or "none")
        if sink_type in {"none", "ignore"}:
            return
        if sink_type == "persistent_notification":
            await _call_service(
                "persistent_notification",
                "create",
                {"title": response.get("title") or "Hermes Agent", "message": content[: self.MAX_MESSAGE_LENGTH]},
            )
            return
        if sink_type == "service":
            domain = str(response.get("domain") or "")
            service = str(response.get("service") or "")
            data = dict(response.get("service_data") or {})
            key = str(response.get("message_key") or "message")
            data[key] = str(data.get(key) or "{response}").replace("{response}", content)
            await _call_service(domain, service, data, response.get("target") or {})
            return
        if sink_type == "delivery":
            gateway_runner = getattr(self, "gateway_runner", None)
            await _deliver_to_platform(
                str(response.get("target") or ""),
                content,
                adapters=getattr(gateway_runner, "adapters", None),
                loop=getattr(gateway_runner, "_gateway_loop", None),
            )
            return
        if sink_type == "webhook":
            url = str(response.get("url") or os.getenv(str(response.get("url_env") or "")) or "")
            if not url:
                raise RuntimeError("webhook response sink missing url/url_env")
            payload = response.get("payload") or {"text": "{response}"}
            body = _render_payload(payload, content)
            await _post_webhook(url, body)
            return
        raise RuntimeError(f"Unsupported response sink: {sink_type}")

    async def send_typing(self, chat_id: str, metadata: dict | None = None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": "Home Assistant Events", "type": "channel", "url": self._hass_url}




def validate_config(config: Any) -> bool:
    url, token = _ha_settings(config)
    return bool(url and token)


def is_connected(config: Any) -> bool:
    return validate_config(config)


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="homeassistant",
        label="Home Assistant",
        adapter_factory=lambda cfg: HomeAssistantAgentAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["HASS_TOKEN"],
        install_hint="Install hermes-agent[homeassistant] or aiohttp; set HASS_TOKEN.",
        emoji="HA",
        platform_hint=(
            "You were triggered by a Home Assistant event. Follow the configured prompt. "
            "Use the built-in Home Assistant tools for service/state access, "
            "ha_list_events or ha_recent_events for event-bus context, and ha_fetch_media "
            "when you need Home Assistant camera/image media."
        ),
    )
    _register_tools(ctx)


def _register_tools(ctx: Any) -> None:
    ctx.register_tool(
        "ha_list_events", "homeassistant", LIST_EVENTS_SCHEMA, ha_list_events,
        check_tool_requirements, ["HASS_TOKEN"], True, "List Home Assistant event types.", "HA",
    )
    ctx.register_tool(
        "ha_recent_events", "homeassistant", RECENT_EVENTS_SCHEMA, ha_recent_events,
        lambda: True, [], True, "Read recent HA events seen by this plugin.", "HA",
    )
    ctx.register_tool(
        "ha_fetch_media", "homeassistant", FETCH_MEDIA_SCHEMA, ha_fetch_media,
        check_tool_requirements, ["HASS_TOKEN"], True, "Fetch HA media to a local file.", "HA",
    )
