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
            {"ha_list_events", "ha_recent_events"},
        )

    def test_tool_schemas_are_raw_hermes_schema_shape(self):
        for schema in (adapter.LIST_EVENTS_SCHEMA, adapter.RECENT_EVENTS_SCHEMA):
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
