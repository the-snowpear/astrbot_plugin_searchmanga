"""回归测试：卡片中文字体选择 + LLM 工具路径下的临时文件延迟清理。

对应两个线上问题：
1. AstrBot Docker 镜像里只有 DejaVuSans 等纯西文字体，Pillow 选中它们后
   卡片里的中日文全部渲染成方块 □。
2. 通过 LLM 工具 download_doujin 调用时，AstrBot 会先把异步生成器消费完，
   再由 event.send(...) 读取图片文件；原来的 finally 里立刻 unlink，导致
   "image file URI does not point to an existing file"。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

PKG_ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = PKG_ROOT.name

# 注意：不能在导入期安装 astrbot 测试桩——pytest 会先收集所有测试模块，
# 提前安装会改变其它测试模块的桩安装顺序。这里延迟到 setUpModule，
# 直接复用 test_refactor 已经装好的桩与插件模块。
shared = None
main_mod = None
packaging = None
models = None


def setUpModule() -> None:
    global shared, main_mod, packaging, models
    import test_refactor as _shared

    shared = _shared
    main_mod = _shared.main_mod
    packaging = _shared.packaging
    models = importlib.import_module(f"{PKG_NAME}.models")


def _make_plugin():
    return main_mod.SearchMangaPlugin(context=None, config={})


def _temp_file(name: str | None = None, data: bytes = b"payload") -> Path:
    path = packaging.temp_root() / (name or f"tmp_{uuid.uuid4().hex[:8]}.bin")
    path.write_bytes(data)
    return path


def _make_media(work_dir: Path, image_paths: list[Path]):
    return models.ParsedMedia(
        platform="nhentai",
        title="中文标题 と日本語タイトル",
        url="https://nhentai.net/g/123456/",
        author="作者名",
        summary="简介内容",
        cover_bytes=shared._make_png_bytes(),
        image_paths=image_paths,
        page_count=12,
        work_dir=str(work_dir),
    )


def _collect_image_files(results) -> list[str]:
    files: list[str] = []
    for result in results:
        chain = getattr(result, "payload", None)
        if not isinstance(chain, list):
            continue
        for comp in chain:
            for node in getattr(comp, "nodes", None) or []:
                for item in getattr(node, "content", None) or []:
                    file_path = getattr(item, "file", None)
                    if file_path:
                        files.append(file_path)
    return files


class DeferredCleanupTests(unittest.TestCase):
    def test_defer_keeps_file_until_expiry(self):
        plugin = _make_plugin()
        path = _temp_file()
        plugin._defer_cleanup(path, ttl=600)

        self.assertTrue(path.exists(), "延迟清理不应立刻删除文件")
        self.assertIn(str(path.resolve()), plugin._deferred_cleanup)

        for key in list(plugin._deferred_cleanup):
            plugin._deferred_cleanup[key] = 0.0
        plugin._sweep_deferred_cleanup()

        self.assertFalse(path.exists(), "到期后应真正删除")
        self.assertNotIn(str(path.resolve()), plugin._deferred_cleanup)

    def test_defer_skips_paths_outside_temp_root(self):
        plugin = _make_plugin()
        outside = PKG_ROOT / f"_outside_{uuid.uuid4().hex[:8]}.tmp"
        outside.write_text("keep", encoding="utf-8")
        try:
            plugin._defer_cleanup(outside, ttl=0)
            self.assertNotIn(str(outside.resolve()), plugin._deferred_cleanup)
            self.assertTrue(outside.exists())
        finally:
            outside.unlink(missing_ok=True)

    def test_cleanup_loop_removes_expired_files(self):
        plugin = _make_plugin()
        path = _temp_file()

        async def _run():
            with patch.object(main_mod, "TEMP_FILE_SWEEP_INTERVAL", 0.01):
                plugin._defer_cleanup(path, ttl=0.01)
                self.assertIsNotNone(plugin._cleanup_task)
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if not path.exists():
                        break
            self.assertFalse(path.exists(), "后台清理任务应删除到期临时文件")
            await plugin.terminate()

        asyncio.run(_run())

    def test_terminate_flushes_pending_queue(self):
        plugin = _make_plugin()
        path = _temp_file()

        async def _run():
            plugin._defer_cleanup(path, ttl=3600)
            self.assertTrue(path.exists())
            await plugin.terminate()

        asyncio.run(_run())
        self.assertFalse(path.exists(), "插件卸载时应清理全部延迟队列")
        self.assertFalse(plugin._deferred_cleanup)

    def test_grace_seconds_config_override(self):
        plugin = main_mod.SearchMangaPlugin(context=None, config={"cleanup_grace_seconds": 42})
        self.assertEqual(plugin._cleanup_grace_seconds(), 42.0)

        plugin_bad = main_mod.SearchMangaPlugin(context=None, config={"cleanup_grace_seconds": "abc"})
        self.assertEqual(plugin_bad._cleanup_grace_seconds(), float(main_mod.TEMP_FILE_GRACE_SECONDS))

        plugin_low = main_mod.SearchMangaPlugin(context=None, config={"cleanup_grace_seconds": 1})
        self.assertEqual(plugin_low._cleanup_grace_seconds(), 30.0)


class DeliverKeepsMediaUntilSendTests(unittest.TestCase):
    """模拟 call_local_llm_tool：先消费完生成器，再读取图片。"""

    def _run_delivery(self, media):
        plugin = _make_plugin()
        event = shared._DummyEvent()

        async def _consume():
            results = []
            async for res in plugin._deliver_parsed_media(
                event,
                media,
                "https://nhentai.net/g/123456/",
                nsfw_policy="send",
                allow_file=False,
                silent_if_ignored=False,
                stop_event_on_delivery=True,
            ):
                results.append(res)
            return results

        return plugin, asyncio.run(_consume())

    def test_generator_completion_does_not_delete_card_or_previews(self):
        work_dir = packaging.temp_root() / f"work_{uuid.uuid4().hex[:8]}"
        work_dir.mkdir(parents=True, exist_ok=True)
        preview = work_dir / "001.png"
        preview.write_bytes(shared._make_png_bytes(size=(32, 32)))
        media = _make_media(work_dir, [preview])

        plugin, results = self._run_delivery(media)

        image_files = _collect_image_files(results)
        self.assertTrue(image_files, "合并转发节点里应当包含图片组件")
        for file_path in image_files:
            self.assertTrue(
                Path(file_path).exists(),
                f"生成器被消费完后图片仍须存在（LLM 工具路径发送时才读盘）: {file_path}",
            )

        # 卡片图 / 预览图目录都已进入延迟清理队列
        self.assertIn(str(work_dir.resolve()), plugin._deferred_cleanup)

        for key in list(plugin._deferred_cleanup):
            plugin._deferred_cleanup[key] = 0.0
        plugin._sweep_deferred_cleanup()

        for file_path in image_files:
            self.assertFalse(Path(file_path).exists(), "到期后临时图片应被清理")
        self.assertFalse(work_dir.exists(), "到期后工作目录应被清理")

    def test_ignored_media_is_not_queued(self):
        media = models.ParsedMedia(
            platform="nhentai",
            title="ignored",
            url="https://nhentai.net/g/1/",
            ignored_reason="按配置忽略",
        )
        plugin, results = self._run_delivery(media)
        self.assertTrue(results)
        self.assertEqual(results[0].payload, "按配置忽略")
        self.assertFalse(plugin._deferred_cleanup)


class FontSelectionTests(unittest.TestCase):
    def test_western_only_font_scores_zero(self):
        from PIL import ImageFont

        font = ImageFont.load_default()
        self.assertEqual(packaging._font_cjk_score(font), 0)

    def test_resolved_font_contains_cjk_glyphs(self):
        path = packaging._resolve_cjk_font_path(use_cache=False)
        if not path:
            self.skipTest("当前环境没有安装含中日文字形的字体")
        from PIL import ImageFont

        font = ImageFont.truetype(path, 20)
        self.assertGreater(packaging._font_cjk_score(font), 0)
        self.assertNotIn("dejavusans", Path(path).name.lower())

    def test_find_font_returns_cjk_capable_font(self):
        path = packaging._resolve_cjk_font_path(use_cache=False)
        if not path:
            self.skipTest("当前环境没有安装含中日文字形的字体")
        font = packaging._find_font(20)
        self.assertIsNotNone(font)
        self.assertGreater(packaging._font_cjk_score(font), 0)

    def test_env_font_path_takes_priority(self):
        candidate = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
        if not candidate.exists():
            self.skipTest("当前环境没有 NotoSansCJK 字体")
        packaging.reset_font_cache()
        try:
            with patch.dict(os.environ, {"SEARCHMANGA_FONT_PATH": str(candidate)}):
                self.assertEqual(packaging._resolve_cjk_font_path(), str(candidate))
        finally:
            packaging.reset_font_cache()

    def test_card_font_path_config_is_passed_through(self):
        plugin = main_mod.SearchMangaPlugin(context=None, config={"card_font_path": " /tmp/not-used.ttc "})
        self.assertEqual(plugin._card_font_path(), "/tmp/not-used.ttc")
        self.assertIsNone(_make_plugin()._card_font_path())


class ConfigSchemaTests(unittest.TestCase):
    def test_new_options_present(self):
        schema = json.loads((PKG_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("card_font_path", schema)
        self.assertEqual(schema["card_font_path"]["type"], "string")
        self.assertIn("cleanup_grace_seconds", schema)
        self.assertEqual(schema["cleanup_grace_seconds"]["default"], 600)


if __name__ == "__main__":
    unittest.main()
