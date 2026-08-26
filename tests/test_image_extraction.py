import asyncio
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

# Mock astrbot if not present in test env
if "astrbot" not in sys.modules or not hasattr(sys.modules["astrbot"], "__path__"):
    mock_astrbot = ModuleType("astrbot")
    mock_astrbot.__path__ = []

    mock_api = ModuleType("astrbot.api")
    mock_api.__path__ = []
    mock_logger = MagicMock()
    mock_api.logger = mock_logger
    mock_api.AstrBotConfig = dict

    mock_event = ModuleType("astrbot.api.event")
    mock_event.AstrMessageEvent = MagicMock
    mock_event.filter = MagicMock()
    mock_api.event = mock_event

    mock_star = ModuleType("astrbot.api.star")
    mock_star.Context = MagicMock
    mock_star.Star = MagicMock
    mock_star.register = lambda *args, **kwargs: (lambda cls: cls)
    mock_api.star = mock_star

    mock_core = ModuleType("astrbot.core")
    mock_core.astrbot_config = {}
    mock_core.file_token_service = MagicMock()
    mock_astrbot_path = ModuleType("astrbot.core.utils.astrbot_path")
    mock_astrbot_path.get_astrbot_temp_path = lambda: "/tmp"
    mock_core_utils = ModuleType("astrbot.core.utils")
    mock_core_utils.astrbot_path = mock_astrbot_path
    mock_core.utils = mock_core_utils

    mock_comp = ModuleType("astrbot.api.message_components")

    class MockImage:
        def __init__(self, url=None, file=None, path=None):
            self.url = url
            self.file = file
            self.path = path

    class MockReply:
        def __init__(self, id=None, message_id=None, chain=None):
            self.id = id
            self.message_id = message_id
            self.chain = chain or []

    class MockPlain:
        def __init__(self, text=""):
            self.text = text

    mock_comp.Image = MockImage
    mock_comp.Reply = MockReply
    mock_comp.Plain = MockPlain
    mock_comp.Node = MagicMock()
    mock_comp.Nodes = MagicMock()
    mock_api.message_components = mock_comp

    sys.modules["astrbot"] = mock_astrbot
    sys.modules["astrbot.api"] = mock_api
    sys.modules["astrbot.api.event"] = mock_event
    sys.modules["astrbot.api.star"] = mock_star
    sys.modules["astrbot.api.message_components"] = mock_comp
    sys.modules["astrbot.core"] = mock_core
    sys.modules["astrbot.core.utils"] = mock_core_utils
    sys.modules["astrbot.core.utils.astrbot_path"] = mock_astrbot_path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

import astrbot.api.message_components as Comp
from astrbot_plugin_searchmanga.main import (
    _extract_image_url,
    _resolve_image_url_async,
)
from astrbot_plugin_searchmanga.soutubot import (
    fetch_image_bytes,
)


class MockEvent:
    def __init__(self, messages=None, raw_message=None, bot=None):
        self._messages = messages or []
        self.message_obj = MagicMock()
        self.message_obj.message = self._messages
        self.message_obj.raw_message = raw_message
        self.bot = bot

    def get_messages(self):
        return self._messages

    def get_platform_name(self):
        return "aiocqhttp"


class TestImageExtraction(unittest.IsolatedAsyncioTestCase):
    def test_extract_image_from_components(self):
        img_comp = Comp.Image(url="https://example.com/test.jpg")
        event = MockEvent(messages=[Comp.Plain("/搜本子"), img_comp])
        url = _extract_image_url(event)
        self.assertEqual(url, "https://example.com/test.jpg")

    def test_extract_image_from_file_attribute(self):
        img_comp = Comp.Image(file="https://example.com/from_file.png")
        event = MockEvent(messages=[img_comp])
        url = _extract_image_url(event)
        self.assertEqual(url, "https://example.com/from_file.png")

    def test_extract_image_from_reply_chain(self):
        replied_img = Comp.Image(url="https://example.com/replied.jpg")
        reply_comp = Comp.Reply(id="1001", chain=[replied_img])
        event = MockEvent(messages=[reply_comp, Comp.Plain("/搜本子")])
        url = _extract_image_url(event)
        self.assertEqual(url, "https://example.com/replied.jpg")

    def test_extract_image_from_raw_message_dict(self):
        raw = [
            {"type": "text", "data": {"text": "/搜本子"}},
            {"type": "image", "data": {"url": "https://example.com/raw.jpg"}},
        ]
        event = MockEvent(messages=[Comp.Plain("/搜本子")], raw_message=raw)
        url = _extract_image_url(event)
        self.assertEqual(url, "https://example.com/raw.jpg")

    async def test_resolve_image_async_with_get_msg_fallback(self):
        mock_bot = MagicMock()
        mock_bot.call_action = AsyncMock(
            return_value={
                "message": [
                    {"type": "image", "data": {"url": "https://example.com/fetched_history.jpg"}}
                ]
            }
        )
        reply_comp = Comp.Reply(id="88888", chain=[])
        event = MockEvent(messages=[reply_comp, Comp.Plain("/搜本子")], bot=mock_bot)

        # Sync extract returns None because chain is empty
        self.assertIsNone(_extract_image_url(event))

        # Async resolver calls get_msg and resolves the image
        resolved_url = await _resolve_image_url_async(event)
        self.assertEqual(resolved_url, "https://example.com/fetched_history.jpg")
        mock_bot.call_action.assert_awaited_once_with("get_msg", message_id="88888")

    async def test_fetch_image_bytes_base64_and_file(self):
        b64_url = "base64://dGVzdF9pbWFnZV9kYXRh"
        data = await fetch_image_bytes(b64_url, timeout=10)
        self.assertEqual(data, b"test_image_data")

        data_uri = "data:image/png;base64,dGVzdF9pbWFnZV9kYXRh"
        data2 = await fetch_image_bytes(data_uri, timeout=10)
        self.assertEqual(data2, b"test_image_data")


if __name__ == "__main__":
    unittest.main()
