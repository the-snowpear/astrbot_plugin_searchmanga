import sys
import unittest
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

# Mock astrbot if not present in test env
if "astrbot" not in sys.modules:
    mock_astrbot = ModuleType("astrbot")
    mock_api = ModuleType("astrbot.api")
    mock_logger = ModuleType("astrbot.api.logger")
    mock_logger.info = lambda *args, **kwargs: None
    mock_logger.debug = lambda *args, **kwargs: None
    mock_logger.warning = lambda *args, **kwargs: None
    mock_logger.error = lambda *args, **kwargs: None
    mock_logger.exception = lambda *args, **kwargs: None
    mock_api.logger = mock_logger
    mock_astrbot.api = mock_api
    sys.modules["astrbot"] = mock_astrbot
    sys.modules["astrbot.api"] = mock_api

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_searchmanga.parsing import (
    entry_url,
    search_entry_from_item,
    extract_jm_id_from_url,
    first_supported_url,
    parse_search_entries_from_text,
)
from astrbot_plugin_searchmanga.downloaders import (
    _nhentai_url_for_host,
    _extract_jm_target,
)


class TestSearchMangaFixes(unittest.TestCase):
    def test_entry_url_prioritizes_subject_path(self):
        # soutubot item where pagePath is single photo and subjectPath is album
        item = {
            "title": "测试本子",
            "source": "jmcomic",
            "pagePath": "/photo/654321",
            "subjectPath": "/album/123456",
        }
        url = entry_url(item)
        self.assertEqual(url, "https://18comic.vip/album/123456")

        entry = search_entry_from_item(1, item)
        self.assertEqual(entry.url, "https://18comic.vip/album/123456")

    def test_entry_url_fallback_to_page_path_if_no_subject(self):
        item = {
            "title": "单页本子",
            "source": "nhentai",
            "pagePath": "/g/123456/1",
        }
        url = entry_url(item)
        self.assertEqual(url, "https://nhentai.net/g/123456/1")

    def test_extract_jm_target(self):
        self.assertEqual(_extract_jm_target("https://18comic.vip/album/123456"), ("album", "123456"))
        self.assertEqual(_extract_jm_target("https://18comic.vip/photo/654321"), ("photo", "654321"))
        self.assertEqual(_extract_jm_target("jmcomic://album/123456"), ("album", "123456"))
        self.assertEqual(_extract_jm_target("jmcomic://photo/654321"), ("photo", "654321"))

    def test_nhentai_url_normalization(self):
        self.assertEqual(
            _nhentai_url_for_host("https://nhentai.net/g/639838/3", "nhentai.net"),
            "https://nhentai.net/g/639838/",
        )
        self.assertEqual(
            _nhentai_url_for_host("https://nhentai.xxx/g/639838/3?x=1#y", "nhentai.net"),
            "https://nhentai.net/g/639838/",
        )


if __name__ == "__main__":
    unittest.main()
