import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = Path(os.getenv("HERMES_AGENT_ROOT", "/Users/andres/workspace/oss/hermes-agent"))


def install_gateway_stubs():
    gateway = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    platforms_mod = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class Platform(str):
        def __new__(cls, value):
            return str.__new__(cls, value)

        @property
        def value(self):
            return str(self)

    class PlatformConfig:
        pass

    class SendResult:
        def __init__(self, success, message_id=None, error=None):
            self.success = success
            self.message_id = message_id
            self.error = error

    class MessageType:
        TEXT = "text"

    class MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self._connected = False

        @property
        def is_connected(self):
            return self._connected

        def _mark_connected(self):
            self._connected = True

        def _mark_disconnected(self):
            self._connected = False

        def build_source(self, **kwargs):
            return SimpleNamespace(**kwargs)

        async def handle_message(self, event):
            self.last_event = event

        @staticmethod
        def extract_media(content):
            return [], content

    config_mod.Platform = Platform
    config_mod.PlatformConfig = PlatformConfig
    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.SendResult = SendResult
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    sys.modules.setdefault("gateway", gateway)
    sys.modules.setdefault("gateway.config", config_mod)
    sys.modules.setdefault("gateway.platforms", platforms_mod)
    sys.modules.setdefault("gateway.platforms.base", base_mod)


def load_adapter():
    install_gateway_stubs()
    if str(HERMES_ROOT) not in sys.path:
        sys.path.insert(0, str(HERMES_ROOT))
    spec = importlib.util.spec_from_file_location("hermes_ha_plugin_adapter", ROOT / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter = load_adapter()


class AdapterTest(unittest.TestCase):
    def install_delivery_stubs(self, *, home=None, parse=None, resolve=None, send_error=None):
        calls = []
        module_names = ["tools.send_message_tool", "gateway.channel_directory"]
        saved_modules = {name: sys.modules.get(name) for name in module_names}
        saved_load_config = getattr(sys.modules["gateway.config"], "load_gateway_config", None)

        def cleanup():
            for name, module in saved_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            if saved_load_config is None:
                delattr(sys.modules["gateway.config"], "load_gateway_config")
            else:
                sys.modules["gateway.config"].load_gateway_config = saved_load_config

        self.addCleanup(cleanup)

        from gateway.config import Platform

        class Config:
            platforms = {Platform("slack"): SimpleNamespace(enabled=True, token="token", extra={})}

            def get_home_channel(self, platform):
                return home

        def default_parse(platform_name, target_ref):
            if target_ref.startswith("C"):
                return target_ref, None, True
            if "/" in target_ref:
                chat_id, thread_id = target_ref.split("/", 1)
                return chat_id, thread_id, True
            return None, None, False

        async def fake_send_to_platform(platform, pconfig, chat_id, content, *, thread_id=None, media_files=None):
            calls.append(
                {
                    "platform": platform,
                    "chat_id": chat_id,
                    "content": content,
                    "thread_id": thread_id,
                    "media_files": media_files,
                }
            )
            return {"error": send_error} if send_error else {"ok": True}

        sys.modules["gateway.config"].load_gateway_config = lambda: Config()
        send_mod = types.ModuleType("tools.send_message_tool")
        send_mod._parse_target_ref = parse or default_parse
        send_mod._send_to_platform = fake_send_to_platform
        sys.modules["tools.send_message_tool"] = send_mod
        directory_mod = types.ModuleType("gateway.channel_directory")
        directory_mod.resolve_channel_name = resolve or (lambda platform_name, target_ref: None)
        sys.modules["gateway.channel_directory"] = directory_mod
        return calls

    def test_event_matches_exact_glob_and_operator_values(self):
        event = {
            "event_type": "state_changed",
            "data": {
                "entity_id": "binary_sensor.front_door",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            },
        }
        trigger = {
            "event_type": "state_changed",
            "match": {
                "data.entity_id": "binary_sensor.*",
                "data.new_state.state": {"in": ["on", "detected"]},
                "data.old_state.state": {"not_equals": "on"},
                "data.missing": {"exists": False},
            },
        }

        self.assertTrue(adapter.event_matches(event, trigger))

    def test_event_match_rejects_wrong_event_type(self):
        event = {"event_type": "call_service", "data": {"domain": "light"}}
        trigger = {"event_type": "state_changed", "match": {"data.domain": "light"}}

        self.assertFalse(adapter.event_matches(event, trigger))

    def test_render_template_exposes_event_trigger_and_json(self):
        event = {"event_type": "state_changed", "data": {"entity_id": "sensor.office"}}
        trigger = {"name": "office"}

        text = adapter.render_template(
            "{trigger.name}|{event.event_type}|{event.data.entity_id}|{json}",
            event,
            trigger,
        )

        self.assertTrue(text.startswith("office|state_changed|sensor.office|"))
        self.assertEqual(json.loads(text.split("|", 3)[-1]), event)

    def test_adapter_uses_trigger_response_by_chat_id(self):
        config = SimpleNamespace(
            token="token",
            extra={
                "url": "http://ha.local:8123",
                "response": {"type": "none"},
                "triggers": [
                    {
                        "name": "door",
                        "chat_id": "door-events",
                        "event_type": "state_changed",
                        "prompt": "Door: {event.data.entity_id}",
                        "response": {"type": "webhook", "url": "http://example.invalid"},
                    }
                ],
            },
        )

        ha = adapter.HomeAssistantAgentAdapter(config)
        event = {"event_type": "state_changed", "data": {"entity_id": "binary_sensor.front_door"}}

        asyncio.run(ha._dispatch_trigger(event, config.extra["triggers"][0]))

        chat_id = ha.last_event.source.chat_id
        self.assertTrue(chat_id.startswith("door-events:ha_"))
        self.assertEqual(ha._response_by_chat_id[chat_id]["type"], "webhook")

    def test_register_registers_platform_and_tools(self):
        class Ctx:
            def __init__(self):
                self.platforms = []
                self.tools = []

            def register_platform(self, **kwargs):
                self.platforms.append(kwargs)

            def register_tool(self, *args):
                self.tools.append(args)

        ctx = Ctx()

        adapter.register(ctx)

        self.assertEqual(ctx.platforms[0]["name"], "homeassistant")
        self.assertEqual(
            {tool[0] for tool in ctx.tools},
            {"ha_list_events", "ha_recent_events", "ha_fetch_media"},
        )

    def test_tool_schemas_are_raw_hermes_schema_shape(self):
        for schema in (adapter.LIST_EVENTS_SCHEMA, adapter.RECENT_EVENTS_SCHEMA, adapter.FETCH_MEDIA_SCHEMA):
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            self.assertNotIn("type", schema)
            self.assertNotIn("function", schema)

    def test_real_registry_wraps_plugin_schema_once(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(
            name="ha_recent_events",
            toolset="homeassistant",
            schema=adapter.RECENT_EVENTS_SCHEMA,
            handler=lambda args: "{}",
            check_fn=lambda: True,
        )

        [definition] = registry.get_definitions({"ha_recent_events"})

        self.assertEqual(definition["type"], "function")
        function = definition["function"]
        self.assertEqual(function["name"], "ha_recent_events")
        self.assertIn("parameters", function)
        self.assertNotIn("function", function)

    def test_default_dispatch_chat_id_is_unique_and_binds_response(self):
        config = SimpleNamespace(
            token="token",
            extra={
                "url": "http://ha.local:8123",
                "triggers": [
                    {
                        "name": "door",
                        "event_type": "state_changed",
                        "match": {"data.entity_id": "binary_sensor.front_door"},
                        "prompt": "Door: {event.data.entity_id}",
                        "response": {"type": "webhook", "url": "http://example.invalid"},
                    }
                ],
            },
        )
        ha = adapter.HomeAssistantAgentAdapter(config)
        event = {"event_type": "state_changed", "data": {"entity_id": "binary_sensor.front_door"}}

        asyncio.run(ha._handle_ha_event(event))
        first_chat = ha.last_event.source.chat_id
        asyncio.run(ha._handle_ha_event(event))
        second_chat = ha.last_event.source.chat_id

        self.assertNotEqual(first_chat, second_chat)
        self.assertEqual(ha._response_by_chat_id[first_chat]["type"], "webhook")
        self.assertEqual(ha._response_by_chat_id[second_chat]["type"], "webhook")

    def test_shared_session_is_explicit_opt_in(self):
        config = SimpleNamespace(
            token="token",
            extra={
                "url": "http://ha.local:8123",
                "triggers": [
                    {
                        "name": "door",
                        "chat_id": "door-events",
                        "shared_session": True,
                        "prompt": "Door: {event.data.entity_id}",
                    }
                ],
            },
        )
        ha = adapter.HomeAssistantAgentAdapter(config)
        event = {"event_type": "state_changed", "data": {"entity_id": "binary_sensor.front_door"}}

        asyncio.run(ha._dispatch_trigger(event, config.extra["triggers"][0]))

        self.assertEqual(ha.last_event.source.chat_id, "door-events")

    def test_trigger_response_survives_failed_send_for_retry(self):
        config = SimpleNamespace(
            token="token",
            extra={
                "url": "http://ha.local:8123",
                "response": {"type": "none"},
                "triggers": [],
            },
        )
        ha = adapter.HomeAssistantAgentAdapter(config)
        ha._response_by_chat_id["door-events"] = {"type": "webhook", "url": ""}

        result = asyncio.run(ha.send("door-events", "hello"))

        self.assertFalse(result.success)
        self.assertIn("door-events", ha._response_by_chat_id)

    def test_trigger_response_is_cleared_after_successful_send(self):
        config = SimpleNamespace(
            token="token",
            extra={
                "url": "http://ha.local:8123",
                "response": {"type": "none"},
                "triggers": [],
            },
        )
        ha = adapter.HomeAssistantAgentAdapter(config)
        ha._response_by_chat_id["door-events"] = {"type": "none"}

        result = asyncio.run(ha.send("door-events", "hello"))

        self.assertTrue(result.success)
        self.assertNotIn("door-events", ha._response_by_chat_id)

    def test_no_response_keyword_suppresses_delivery(self):
        original = adapter._deliver_to_platform
        calls = []

        async def fake_deliver(target, content, **kwargs):
            calls.append((target, content))

        adapter._deliver_to_platform = fake_deliver
        try:
            ha = adapter.HomeAssistantAgentAdapter(
                SimpleNamespace(token="token", extra={"response": {"type": "none"}})
            )
            ha._response_by_chat_id["door-events"] = {"type": "delivery", "target": "slack"}
            result = asyncio.run(ha.send("door-events", "Nothing to report. NO_RESP"))
        finally:
            adapter._deliver_to_platform = original

        self.assertTrue(result.success)
        self.assertEqual(calls, [])
        self.assertNotIn("door-events", ha._response_by_chat_id)

    def test_no_response_keyword_can_be_overridden_per_response(self):
        original = adapter._deliver_to_platform
        calls = []

        async def fake_deliver(target, content, **kwargs):
            calls.append((target, content))

        adapter._deliver_to_platform = fake_deliver
        try:
            ha = adapter.HomeAssistantAgentAdapter(
                SimpleNamespace(token="token", extra={"no_response_keyword": "GLOBAL_SKIP"})
            )
            ha._response_by_chat_id["door-events"] = {
                "type": "delivery",
                "target": "slack",
                "no_response_keyword": "SKIP_ALERT",
            }
            result = asyncio.run(ha.send("door-events", "SKIP_ALERT"))
        finally:
            adapter._deliver_to_platform = original

        self.assertTrue(result.success)
        self.assertEqual(calls, [])

    def test_no_response_keyword_false_disables_suppression(self):
        original = adapter._deliver_to_platform
        calls = []

        async def fake_deliver(target, content, **kwargs):
            calls.append((target, content))

        adapter._deliver_to_platform = fake_deliver
        try:
            ha = adapter.HomeAssistantAgentAdapter(
                SimpleNamespace(token="token", extra={"no_response_keyword": False})
            )
            ha._response_by_chat_id["door-events"] = {"type": "delivery", "target": "slack"}
            result = asyncio.run(ha.send("door-events", "NO_RESP"))
        finally:
            adapter._deliver_to_platform = original

        self.assertTrue(result.success)
        self.assertEqual(calls, [("slack", "NO_RESP")])

    def test_delivery_response_uses_hermes_delivery_sink(self):
        calls = []
        original = adapter._deliver_to_platform

        async def fake_deliver(target, content, **kwargs):
            calls.append((target, content, kwargs))

        adapter._deliver_to_platform = fake_deliver
        try:
            result = asyncio.run(
                adapter.HomeAssistantAgentAdapter(
                    SimpleNamespace(token="token", extra={"response": {"type": "none"}})
                )._deliver_response(
                    {"type": "delivery", "target": "slack:#home-alerts"},
                    "hello",
                )
            )
        finally:
            adapter._deliver_to_platform = original

        self.assertIsNone(result)
        self.assertEqual(calls[0][0:2], ("slack:#home-alerts", "hello"))
        self.assertIn("adapters", calls[0][2])

    def test_send_keeps_delivery_response_on_failure(self):
        original = adapter._deliver_to_platform

        async def fake_deliver(target, content, **kwargs):
            raise RuntimeError("boom")

        adapter._deliver_to_platform = fake_deliver
        try:
            ha = adapter.HomeAssistantAgentAdapter(
                SimpleNamespace(token="token", extra={"response": {"type": "none"}})
            )
            ha._response_by_chat_id["door-events"] = {"type": "delivery", "target": "slack"}
            result = asyncio.run(ha.send("door-events", "hello"))
        finally:
            adapter._deliver_to_platform = original

        self.assertFalse(result.success)
        self.assertIn("door-events", ha._response_by_chat_id)

    def test_delivery_sink_uses_home_thread_id_for_bare_target(self):
        calls = self.install_delivery_stubs(home=SimpleNamespace(chat_id="C_HOME", thread_id="T_HOME"))

        asyncio.run(adapter._deliver_to_platform("slack", "hello"))

        self.assertEqual(calls[0]["chat_id"], "C_HOME")
        self.assertEqual(calls[0]["thread_id"], "T_HOME")

    def test_delivery_sink_resolves_channel_name_when_available(self):
        calls = self.install_delivery_stubs(
            home=None,
            resolve=lambda platform_name, target_ref: "C0123456789" if target_ref == "#home-alerts" else None,
        )

        asyncio.run(adapter._deliver_to_platform("slack:#home-alerts", "hello"))

        self.assertEqual(calls[0]["chat_id"], "C0123456789")

    def test_delivery_sink_keeps_raw_target_when_name_is_unresolved(self):
        calls = self.install_delivery_stubs(home=None)

        asyncio.run(adapter._deliver_to_platform("slack:opaque-target", "hello"))

        self.assertEqual(calls[0]["chat_id"], "opaque-target")

    def test_delivery_sink_prefers_live_adapter(self):
        self.install_delivery_stubs(home=SimpleNamespace(chat_id="C_HOME", thread_id="T_HOME"), send_error="fallback used")

        class RuntimeAdapter:
            def __init__(self):
                self.calls = []

            async def send(self, chat_id, content, metadata=None):
                self.calls.append((chat_id, content, metadata))
                return SimpleNamespace(success=True)

        async def run_delivery():
            from gateway.config import Platform

            runtime_adapter = RuntimeAdapter()
            await adapter._deliver_to_platform(
                "slack",
                "hello",
                adapters={Platform("slack"): runtime_adapter},
                loop=asyncio.get_running_loop(),
            )
            return runtime_adapter.calls

        calls = asyncio.run(run_delivery())

        self.assertEqual(calls, [("C_HOME", "hello", {"thread_id": "T_HOME"})])

    def test_service_sink_uses_core_ha_guardrails(self):
        self.assertFalse(adapter._SERVICE_NAME_RE.match("Light"))
        self.assertFalse(adapter._SERVICE_NAME_RE.match("../light"))
        self.assertIn("command_line", adapter._BLOCKED_DOMAINS)

    def test_receive_result_waits_for_matching_ack(self):
        class FakeWs:
            def __init__(self):
                self.messages = [
                    {"type": "event", "id": 1},
                    {"type": "result", "id": 1, "success": True},
                    {"type": "result", "id": 2, "success": True},
                ]

            async def receive_json(self):
                return self.messages.pop(0)

        config = SimpleNamespace(token="token", extra={"url": "http://ha.local:8123"})
        ha = adapter.HomeAssistantAgentAdapter(config)
        ha._ws = FakeWs()

        result = asyncio.run(ha._receive_result(2))

        self.assertEqual(result["id"], 2)
        self.assertTrue(result["success"])

    def test_recent_events_filters_and_limits(self):
        adapter.RECENT_EVENTS.clear()
        adapter.RECENT_EVENTS.append({"event_type": "a", "data": {"n": 1}})
        adapter.RECENT_EVENTS.append({"event_type": "b", "data": {"n": 2}})
        adapter.RECENT_EVENTS.append({"event_type": "a", "data": {"n": 3}})

        raw = asyncio.run(adapter.ha_recent_events({"event_type": "a", "limit": 1}))

        self.assertEqual(json.loads(raw), [{"event_type": "a", "data": {"n": 3}}])


if __name__ == "__main__":
    unittest.main()
