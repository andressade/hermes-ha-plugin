import asyncio
import unittest
from types import SimpleNamespace

from test_adapter import adapter


class HttpForbidden(Exception):
    status = 403


def make_adapter():
    ha = adapter.HomeAssistantAgentAdapter(
        SimpleNamespace(
            token="token",
            extra={
                "url": "http://localhost:8123",
                "listen_events": ["state_changed"],
                "triggers": [],
            },
        )
    )

    def set_fatal_error(code, message, *, retryable):
        ha.fatal_error_code = code
        ha.fatal_error_message = message
        ha.fatal_error_retryable = retryable

    async def notify_fatal_error():
        ha.fatal_notified = True

    ha._set_fatal_error = set_fatal_error
    ha._notify_fatal_error = notify_fatal_error
    return ha


class AuthFailureTest(unittest.TestCase):
    def test_ws_connect_403_is_classified_as_auth_error(self):
        class FakeSession:
            closed = False

            def __init__(self, *args, **kwargs):
                pass

            async def ws_connect(self, *args, **kwargs):
                raise HttpForbidden("Invalid response status")

            async def close(self):
                self.closed = True

        class FakeAiohttp:
            ClientTimeout = staticmethod(lambda total: ("timeout", total))
            ClientSession = FakeSession

        ha = make_adapter()
        old_aiohttp = adapter.aiohttp
        old_available = adapter.AIOHTTP_AVAILABLE
        adapter.aiohttp = FakeAiohttp
        adapter.AIOHTTP_AVAILABLE = True
        self.addCleanup(setattr, adapter, "aiohttp", old_aiohttp)
        self.addCleanup(setattr, adapter, "AIOHTTP_AVAILABLE", old_available)

        with self.assertRaises(adapter.HomeAssistantAuthError):
            asyncio.run(ha._connect_ws())
        asyncio.run(ha._cleanup_ws())

    def test_connect_marks_ha_403_as_non_retryable_fatal(self):
        ha = make_adapter()
        old_available = adapter.AIOHTTP_AVAILABLE
        adapter.AIOHTTP_AVAILABLE = True
        self.addCleanup(setattr, adapter, "AIOHTTP_AVAILABLE", old_available)

        async def reject_auth():
            raise adapter.HomeAssistantAuthError("HTTP 403 from ws://localhost:8123/api/websocket")

        ha._connect_ws = reject_auth

        self.assertIs(asyncio.run(ha.connect()), False)
        self.assertEqual(ha.fatal_error_code, "homeassistant_auth_rejected")
        self.assertIs(ha.fatal_error_retryable, False)
        self.assertIn("stop reconnecting", ha.fatal_error_message)

    def test_listen_loop_stops_reconnect_after_ha_403(self):
        ha = make_adapter()
        ha._BACKOFF_STEPS = [0]
        ha._mark_connected()

        async def read_events_once():
            return None

        async def reject_auth():
            raise adapter.HomeAssistantAuthError("HTTP 403 from ws://localhost:8123/api/websocket")

        ha._read_events = read_events_once
        ha._connect_ws = reject_auth

        asyncio.run(ha._listen_loop())

        self.assertEqual(ha.fatal_error_code, "homeassistant_auth_rejected")
        self.assertIs(ha.fatal_error_retryable, False)
        self.assertIs(ha.fatal_notified, True)
