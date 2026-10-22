import sys
from importlib import import_module
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_ROOT.parent
# 插件在 AstrBot 中位于 <AstrBot>/data/plugins/<plugin>，此时父目录的上两级就是
# AstrBot 根目录；在独立 clone 的仓库里层级不够，退化为插件父目录即可。
CORE_ROOT = PLUGINS_ROOT.parents[1] if len(PLUGINS_ROOT.parents) > 1 else PLUGINS_ROOT

for path in (str(CORE_ROOT), str(PLUGINS_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

_nhentai_url_for_host = import_module(
    f"{PLUGIN_ROOT.name}.downloaders"
)._nhentai_url_for_host


def test_nhentai_url_for_host_drops_reader_page_number():
    assert (
        _nhentai_url_for_host("https://nhentai.net/g/639838/3", "nhentai.net")
        == "https://nhentai.net/g/639838/"
    )


def test_nhentai_url_for_host_drops_query_and_fragment():
    assert (
        _nhentai_url_for_host("https://nhentai.xxx/g/639838/3?x=1#y", "nhentai.net")
        == "https://nhentai.net/g/639838/"
    )


def test_nhentai_url_for_host_keeps_gallery_entry_shape():
    assert (
        _nhentai_url_for_host("https://nhentai.net/g/639838/", "nhentai.net")
        == "https://nhentai.net/g/639838/"
    )
