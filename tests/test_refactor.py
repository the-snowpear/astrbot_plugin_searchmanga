from __future__ import annotations

import asyncio
from io import BytesIO
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image


def _ensure_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        comp_mod = sys.modules.get("astrbot.api.message_components")
        if comp_mod is not None:
            class _BaseComp:
                def __init__(self, *args, **kwargs):
                    if args:
                        self.nodes = args[0]
                    for k, v in kwargs.items():
                        setattr(self, k, v)

            class _MediaComp(_BaseComp):
                @classmethod
                def fromURL(cls, url: str):
                    return cls(url=url, file=url)

                @classmethod
                def fromFileSystem(cls, path: str):
                    return cls(file=path, path=path)

            for attr in ("Forward", "Node", "Nodes", "File", "Json"):
                existing = getattr(comp_mod, attr, None)
                if existing is None or not isinstance(existing, type):
                    setattr(comp_mod, attr, _BaseComp)
            for attr in ("Image", "Video"):
                existing = getattr(comp_mod, attr, None)
                if existing is None or not isinstance(existing, type):
                    setattr(comp_mod, attr, _MediaComp)
                else:
                    if not hasattr(existing, "fromURL"):
                        existing.fromURL = classmethod(lambda cls, url: cls(url=url, file=url))
                    if not hasattr(existing, "fromFileSystem"):
                        existing.fromFileSystem = classmethod(lambda cls, path: cls(file=path, path=path))
        return

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    core_mod = types.ModuleType("astrbot.core")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    utils_mod = types.ModuleType("astrbot.core.utils")
    comp_mod = types.ModuleType("astrbot.api.message_components")

    class _DummyLogger:
        def info(self, *a, **kw):
            pass

        def warning(self, *a, **kw):
            pass

        def debug(self, *a, **kw):
            pass

        def exception(self, *a, **kw):
            pass

    class _Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def event_message_type(*a, **kw):
            return lambda fn: fn

        @staticmethod
        def command(*a, **kw):
            return lambda fn: fn

        @staticmethod
        def llm_tool(*a, **kw):
            return lambda fn: fn

    class _Star:
        def __init__(self, context=None):
            self.context = context

    class _BaseComp:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _Image(_BaseComp):
        @classmethod
        def fromURL(cls, url: str):
            return cls(url=url, file=url)

        @classmethod
        def fromFileSystem(cls, path: str):
            return cls(file=path, path=path)

    class _Video(_BaseComp):
        @classmethod
        def fromFileSystem(cls, path: str):
            return cls(file=path, path=path)

    class _Plain(_BaseComp):
        def __init__(self, text: str = ""):
            super().__init__(text=text)

    class _Reply(_BaseComp):
        pass

    class _Forward(_BaseComp):
        pass

    class _Node(_BaseComp):
        pass

    class _Nodes(_BaseComp):
        def __init__(self, nodes=None, **kwargs):
            super().__init__(nodes=nodes, **kwargs)

    class _File(_BaseComp):
        pass

    class _Json(_BaseComp):
        pass

    api_mod.AstrBotConfig = dict
    api_mod.logger = _DummyLogger()
    event_mod.AstrMessageEvent = object
    event_mod.filter = _Filter
    star_mod.Context = object
    star_mod.Star = _Star
    core_mod.astrbot_config = {}
    core_mod.file_token_service = types.SimpleNamespace(register_file=AsyncMock(return_value="tok"))
    path_mod.get_astrbot_temp_path = lambda: str(Path.cwd())
    comp_mod.Image = _Image
    comp_mod.Video = _Video
    comp_mod.Plain = _Plain
    comp_mod.Reply = _Reply
    comp_mod.Forward = _Forward
    comp_mod.Node = _Node
    comp_mod.Nodes = _Nodes
    comp_mod.File = _File
    comp_mod.Json = _Json

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.utils"] = utils_mod
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod
    sys.modules["astrbot.api.message_components"] = comp_mod


_ensure_astrbot_stubs()

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT.parent))

PKG_NAME = PKG_ROOT.name
soutubot = importlib.import_module(f"{PKG_NAME}.soutubot")
parsing = importlib.import_module(f"{PKG_NAME}.parsing")
packaging = importlib.import_module(f"{PKG_NAME}.packaging")
jm_service = importlib.import_module(f"{PKG_NAME}.jm_service")
ehentai_service = importlib.import_module(f"{PKG_NAME}.ehentai_service")
nhentai_service = importlib.import_module(f"{PKG_NAME}.nhentai_service")
iwara_service = importlib.import_module(f"{PKG_NAME}.iwara_service")
main_mod = importlib.import_module(f"{PKG_NAME}.main")
Comp = importlib.import_module("astrbot.api.message_components")


def _make_png_bytes(color=(120, 45, 200), size=(64, 64)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _DummyResult:
    def __init__(self, payload):
        self.payload = payload
        self.stopped = False

    def stop_event(self):
        self.stopped = True
        return self


class _DummyEvent:
    def __init__(self, *, text="", sender_id="111", group_id="222", message_obj=None, bot=None):
        self._text = text
        self._sender_id = sender_id
        self._group_id = group_id
        self.message_obj = message_obj or types.SimpleNamespace(
            message=[], raw_message=None, self_id="99999", message_id="1001"
        )
        self.bot = bot
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group_id}"
        self.extras = {}
        self.llm_flag = None

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_session_id(self):
        return self.unified_msg_origin

    def get_platform_name(self):
        return "aiocqhttp"

    def set_extra(self, key, val):
        self.extras[key] = val

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def should_call_llm(self, val):
        self.llm_flag = val

    def plain_result(self, text):
        return _DummyResult(text)

    def chain_result(self, chain):
        return _DummyResult(chain)


class TestRefactorSuite(unittest.TestCase):
    def test_soutubot_normalize_grouping_and_links(self):
        raw_hits = [
            {
                "score": 92.5,
                "same_image_group": {"proven": True},
                "path_segments": [
                    {
                        "source_family": "nhentai",
                        "source_key": "nhentai",
                        "external_id": "654321",
                        "metadata_status": "found",
                        "links": {
                            "source_url": "https://nhentai.net/g/654321/",
                            "thumbnail_url": "https://cdn.soutubot.moe/thumb_nh.jpg",
                        },
                        "metadata": {
                            "display_kind": "doujinshi",
                            "title": {"primary": "Sample Work NH"},
                        },
                    },
                    {
                        "source_family": "ehentai",
                        "source_key": "ehentai",
                        "external_id": "234567",
                        "metadata_status": "found",
                        "links": {
                            "page_url": "https://e-hentai.org/g/234567/deadbeef00/",
                            "thumbnail_url": "https://cdn.soutubot.moe/thumb_eh.jpg",
                        },
                        "metadata": {
                            "display_kind": "doujinshi",
                            "title": {"primary": "Sample Work EH"},
                        },
                    },
                ],
            },
            {
                "score": 88.0,
                "path_segments": [
                    {
                        "source_family": "panda",
                        "source_key": "panda",
                        "external_id": "9988",
                        "metadata_status": "found",
                        "metadata": {
                            "display_kind": "doujinshi",
                            "title": {"primary": "Panda Work"},
                        },
                    }
                ],
            },
        ]
        results = soutubot.normalize_results(raw_hits)
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first["source"], "ehentai")
        self.assertEqual(first["pagePath"], "https://e-hentai.org/g/234567/deadbeef00/")
        self.assertEqual(first["alternatives"], [("nhentai", "https://nhentai.net/g/654321/")])
        second = results[1]
        self.assertEqual(second["source"], "panda")
        self.assertEqual(second["pagePath"], "https://panda.chaika.moe/archive/9988/")

    def test_parsing_url_before_jm_id_and_extract_targets(self):
        intent = parsing.parse_download_intent("下载 https://example.com/post/jm12345")
        self.assertIsNone(intent)

        intent2 = parsing.parse_download_intent("下载第3个")
        self.assertIsNotNone(intent2)
        self.assertEqual(intent2.index, 3)

        msg = (
            "看看这个 https://www.iwara.tv/video/AbCd1234 以及 "
            "https://e-hentai.org/g/12345/abcdef1234/ 还有 JM987654"
        )
        targets = parsing.extract_parse_targets(msg)
        self.assertEqual(
            targets,
            [
                "https://www.iwara.tv/video/AbCd1234",
                "https://e-hentai.org/g/12345/abcdef1234/",
                "jmcomic://album/987654",
            ],
        )

        card_json = '{"meta":{"detail_1":{"qqdocurl":"https:\\/\\/nhentai.net\\/g\\/555555\\/"}}}'
        self.assertEqual(parsing.extract_json_url(card_json), "https://nhentai.net/g/555555/")

    def test_quoted_forward_get_msg_resolution_and_isolation(self):
        plugin = main_mod.SearchMangaPlugin(context=object(), config={})

        async def _call_action(action, **params):
            if action == "get_msg":
                return {
                    "data": {
                        "message_id": params["message_id"],
                        "message": [{"type": "forward", "data": {"id": "fwd_abc"}}],
                    }
                }
            if action == "get_forward_msg":
                return {
                    "data": {
                        "messages": [
                            {
                                "content": [
                                    {
                                        "type": "text",
                                        "data": {
                                            "text": (
                                                "#1 [ehentai] 相似度 91.20%\n"
                                                "标题: Quoted Gallery\n"
                                                "来源: ehentai\n"
                                                "链接: https://e-hentai.org/g/222222/aabbccdd11/"
                                            )
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                }
            raise AssertionError(f"unexpected action {action}")

        bot = types.SimpleNamespace(call_action=AsyncMock(side_effect=_call_action))
        reply_comp = Comp.Reply(id="8888", chain=[])
        ev = _DummyEvent(
            text="下载第1个",
            sender_id="1001",
            message_obj=types.SimpleNamespace(message=[reply_comp], raw_message=None, self_id="99", message_id="777"),
            bot=bot,
        )

        entry = asyncio.run(plugin._entry_from_quoted_forward(ev, 1))
        self.assertIsNotNone(entry)
        self.assertEqual(entry.source, "ehentai")
        self.assertEqual(entry.url, "https://e-hentai.org/g/222222/aabbccdd11/")

        plugin._mark_download_event_handled(ev)
        self.assertIs(ev.llm_flag, False)

        # User-scoped search isolation check
        ev_a = _DummyEvent(sender_id="A", group_id="G1")
        ev_b = _DummyEvent(sender_id="B", group_id="G1")
        plugin._remember_search(
            ev_a,
            [main_mod.SearchEntry(index=1, title="A-Work", source="nhentai", url="https://nhentai.net/g/1/")],
        )
        plugin._remember_search(
            ev_b,
            [main_mod.SearchEntry(index=1, title="B-Work", source="nhentai", url="https://nhentai.net/g/2/")],
        )
        self.assertEqual(plugin._recent_search_entry(ev_a, 1).title, "A-Work")
        self.assertEqual(plugin._recent_search_entry(ev_b, 1).title, "B-Work")

    def test_jm_bom_patch_and_target_distinction(self):
        # Test parse_jm_target distinction
        self.assertEqual(jm_service.parse_jm_target("https://18comic.vip/photo/777"), ("photo", "777"))
        self.assertEqual(jm_service.parse_jm_target("https://18comic.vip/album/666"), ("album", "666"))
        self.assertEqual(jm_service.parse_jm_target("JM123456"), ("album", "123456"))

        # Test UTF-8 BOM stripping patch
        seen_raw = []

        class _FakeAbstractJmClient:
            def raise_if_resp_should_retry(self, resp):
                seen_raw.append(resp._content)

        fake_mod = types.ModuleType("jmcomic")
        fake_iface = types.ModuleType("jmcomic.jm_client_interface")
        fake_iface.AbstractJmClient = _FakeAbstractJmClient
        fake_mod.jm_client_interface = fake_iface

        with patch.dict(sys.modules, {"jmcomic": fake_mod, "jmcomic.jm_client_interface": fake_iface}):
            self.assertTrue(jm_service.install_jmcomic_bom_tolerance())
            self.assertTrue(jm_service.install_jmcomic_bom_tolerance())  # idempotent
            resp = types.SimpleNamespace(_content=b'\xef\xbb\xbf{"ok":true}', content=b'\xef\xbb\xbf{"ok":true}')
            _FakeAbstractJmClient().raise_if_resp_should_retry(resp)
            self.assertEqual(seen_raw[0], b'{"ok":true}')

    def test_ehentai_cookie_isolation_pagination_and_509(self):
        # H@H hosts must never receive EH/ExHentai site cookies
        self.assertEqual(
            ehentai_service.cookies_for_host(
                "abcd1234.hath.network",
                ehentai_cookie="ipb_member_id=1",
                exhentai_cookie="ipb_member_id=1; ipb_pass_hash=x; igneous=y",
            ),
            {},
        )
        ex_cookies = ehentai_service.cookies_for_host(
            "exhentai.org",
            exhentai_cookie="ipb_member_id=1; ipb_pass_hash=x; igneous=y",
        )
        self.assertEqual(ex_cookies["nw"], "1")
        self.assertEqual(ex_cookies["igneous"], "y")

        sample_html = """
        <html><body>
        <h1 id="gn">Test Doujin</h1>
        <table class="ptt"><tr><td><a href="https://e-hentai.org/g/100/aa/?p=2">3</a></td></tr></table>
        <div id="gdt">
          <a href="https://e-hentai.org/s/aa1122/100-2"><img src="t2.jpg"/></a>
          <a href="https://e-hentai.org/s/aa1122/100-1"><img src="t1.jpg"/></a>
        </div>
        </body></html>
        """
        pmap, max_p, meta = ehentai_service._extract_reader_links_and_max_page(
            sample_html, "e-hentai.org", "100"
        )
        self.assertEqual(max_p, 2)
        self.assertEqual(sorted(pmap.keys()), [1, 2])
        self.assertEqual(meta.get("title"), "Test Doujin")

        with self.assertRaises(ehentai_service.Quota509Error):
            ehentai_service._extract_img_and_nl('<img id="img" src="https://e-hentai.org/img/509.gif" />')

        with self.assertRaises(ehentai_service.SadPandaError):
            ehentai_service._check_html_errors("", host="exhentai.org", content_type="image/gif")

        img_u, nl_tok = ehentai_service._extract_img_and_nl(
            '<img id="img" src="https://1.2.3.4/h/abc/key/img.jpg"><a onclick="return nl(\'9988\')">'
        )
        self.assertEqual(img_u, "https://1.2.3.4/h/abc/key/img.jpg")
        self.assertEqual(nl_tok, "9988")

    def test_nhentai_v2_pages_and_iwara_signature(self):
        v2_gallery = {
            "media_id": "2900000",
            "title": {"japanese": "NH 测试"},
            "cover": {"path": "galleries/2900000/cover.jpg"},
            "pages": [
                {"path": "galleries/2900000/1.jpg"},
                {"path": "galleries/2900000/2.png"},
            ],
        }
        pages, cov = nhentai_service._build_page_urls_from_gallery(
            v2_gallery, ["https://i1.nhentai.net", "https://i2.nhentai.net"], ["https://t1.nhentai.net"]
        )
        self.assertEqual(
            pages,
            [
                "https://i1.nhentai.net/galleries/2900000/1.jpg",
                "https://i2.nhentai.net/galleries/2900000/2.png",
            ],
        )
        self.assertEqual(cov, "https://t1.nhentai.net/galleries/2900000/cover.jpg")

        xver = iwara_service.compute_iwara_x_version("file123", "1700000000")
        self.assertEqual(len(xver), 40)

        ordered = iwara_service._order_video_candidates(
            [
                {"name": "360", "src": {"download": "//dl.iwara.tv/360.mp4"}, "size": 100},
                {"name": "Source", "src": {"download": "//dl.iwara.tv/src.mp4"}, "size": 500},
                {"name": "540", "src": {"download": "//dl.iwara.tv/540.mp4"}, "size": 250},
            ],
            "540",
        )
        self.assertEqual([x["name"] for x in ordered], ["540", "source", "360"])

    def test_jm_photo_vs_album_chapter_order_and_max_pages(self):
        png_bytes = _make_png_bytes((10, 200, 150))

        class _FakeImg:
            def __init__(self, url):
                self.img_url = url
                self.download_url = url
                self.scramble_id = 0

        class _FakePhoto(list):
            def __init__(self, pid, aid, name, imgs):
                super().__init__(imgs)
                self.photo_id = pid
                self.album_id = aid
                self.name = name
                self.page_arr = [i.img_url for i in imgs]
                self.author = "Tester"
                self.data_original_domain = "cdn-msp.jmapiproxy3.cc"

        p1 = _FakePhoto("101", "100", "Ch1", [_FakeImg("https://cdn/1_1.jpg"), _FakeImg("https://cdn/1_2.jpg")])
        p2 = _FakePhoto("102", "100", "Ch2", [_FakeImg("https://cdn/2_1.jpg")])

        class _FakeAlbum(list):
            def __init__(self, aid, name, photos):
                super().__init__(photos)
                self.album_id = aid
                self.name = name
                self.authors = ["Tester"]
                self.tags = ["test"]
                self.works = []
                self.description = ""
                self.page_count = sum(len(x) for x in photos)

        album = _FakeAlbum("100", "MultiChBook", [p1, p2])

        class _FakeClient:
            def get_album_detail(self, aid):
                return album

            def get_photo_detail(self, pid, *_args):
                return p1 if str(pid) == "101" else p2

            def get_jm_image(self, url):
                return types.SimpleNamespace(content=png_bytes)

        class _FakeOption:
            def __init__(self):
                self.dir_rule = types.SimpleNamespace(base_dir="")
                self.client = types.SimpleNamespace(
                    impl="api",
                    retry_times=3,
                    postman=types.SimpleNamespace(meta_data={}),
                )
                self.download = types.SimpleNamespace(threading=types.SimpleNamespace(image=4, photo=2))

            def build_jm_client(self):
                return _FakeClient()

        class _FakeImgTool:
            @staticmethod
            def get_num_by_url(sid, url):
                return 0

        fake_mod = types.ModuleType("jmcomic")
        fake_mod.JmOption = types.SimpleNamespace(default=lambda: _FakeOption())
        fake_mod.JmImageTool = _FakeImgTool
        fake_iface = types.ModuleType("jmcomic.jm_client_interface")
        fake_iface.AbstractJmClient = type("_DummyAbstract", (), {"raise_if_resp_should_retry": lambda self, r: None})
        fake_mod.jm_client_interface = fake_iface

        with patch.dict(sys.modules, {"jmcomic": fake_mod, "jmcomic.jm_client_interface": fake_iface}):
            # Single chapter /photo/102 should ONLY download p2 (1 page), not whole album
            res_photo = asyncio.run(jm_service.fetch_jm_media("https://18comic.vip/photo/102"))
            self.assertEqual(len(res_photo.image_paths), 1)
            self.assertIn("102", str(res_photo.image_paths[0]))

            # Multi-chapter album keeps separate chapter dirs (no overwriting 00001.jpg) & respects order
            res_album = asyncio.run(jm_service.fetch_jm_media("https://18comic.vip/album/100"))
            self.assertEqual(len(res_album.image_paths), 3)

            # max_pages exceeded triggers skip guard cleanly
            res_skip = asyncio.run(jm_service.fetch_jm_media("https://18comic.vip/album/100", max_pages=2))
            self.assertEqual(len(res_skip.image_paths), 0)
            self.assertIn("超过上限", res_skip.media.skipped_download_reason)

    def test_iwara_nsfw_ignore_and_blur(self):
        png_bytes = _make_png_bytes((240, 30, 80))

        async def _fake_req(url, **kwargs):
            if "/image/abc123" in url:
                payload = json.dumps(
                    {
                        "title": "Ecchi Set",
                        "rating": "ecchi",
                        "user": {"name": "Creator"},
                        "files": [{"id": "f1", "name": "img1.png"}],
                    }
                ).encode("utf-8")
                return 200, payload, {"content-type": "application/json"}
            return 200, png_bytes, {"content-type": "image/png"}

        import json
        with patch.object(iwara_service, "_iwara_request", side_effect=_fake_req):
            res_ignored = asyncio.run(
                iwara_service.fetch_iwara_media(
                    "https://ecchi.iwara.tv/image/abc123",
                    nsfw_policy="ignore",
                )
            )
            self.assertTrue(res_ignored.media.ignored_reason)
            self.assertEqual(res_ignored.image_paths, [])

            res_blur = asyncio.run(
                iwara_service.fetch_iwara_media(
                    "https://ecchi.iwara.tv/image/abc123",
                    nsfw_policy="blur",
                )
            )
            self.assertEqual(res_blur.media.ignored_reason, "")
            self.assertEqual(len(res_blur.image_paths), 1)
            self.assertIn("_blur", res_blur.image_paths[0].name)

    def test_downloaders_and_plugin_dispatch_end_to_end(self):
        downloaders = importlib.import_module(f"{PKG_NAME}.downloaders")
        tmp_dir = Path(packaging.temp_root()) / "test_e2e_dispatch"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        page1 = tmp_dir / "00001.png"
        page2 = tmp_dir / "00002.png"
        page1.write_bytes(_make_png_bytes((40, 80, 160)))
        page2.write_bytes(_make_png_bytes((60, 140, 80)))

        fake_media = main_mod.ParsedMedia(
            platform="ehentai",
            title="E2E Gallery",
            url="https://e-hentai.org/g/333333/aabbccdd11/",
            author="AuthorA",
            extra_info="页数: 2",
            cover_bytes=page1.read_bytes(),
            image_paths=[page1, page2],
            page_count=2,
            is_nsfw=True,
            work_dir=None,
        )
        fake_res = ehentai_service.EhDownloadResult(
            media=fake_media,
            image_paths=[page1, page2],
            archive_zip_path=None,
        )

        async def _run():
            with patch.object(downloaders, "fetch_eh_media", AsyncMock(return_value=fake_res)):
                dl_pdf = await downloaders.download_gallery(
                    "https://e-hentai.org/g/333333/aabbccdd11/",
                    output_format="pdf",
                )
                try:
                    self.assertTrue(dl_pdf.zip_path.endswith(".pdf"))
                    self.assertTrue(Path(dl_pdf.zip_path).read_bytes().startswith(b"%PDF-"))
                finally:
                    Path(dl_pdf.zip_path).unlink(missing_ok=True)

                parsed_merge = await downloaders.parse_media_target(
                    "https://e-hentai.org/g/333333/aabbccdd11/",
                    output_format="merge",
                    preview_count=1,
                )
                self.assertIsNone(parsed_merge.file_path)
                self.assertEqual(len(parsed_merge.image_paths), 1)

            plugin = main_mod.SearchMangaPlugin(
                context=object(),
                config={
                    "output_format": "zip",
                    "render_parse_card": False,
                    "parse_arbiter": False,
                },
            )
            dl_calls = []

            async def _fake_dl(url, **kwargs):
                dl_calls.append(url)
                out_zip = packaging.images_to_zip([page1, page2], "TestDL")
                return out_zip

            sent_files = []

            async def _fake_send(ev, fname, fpath):
                sent_files.append((fname, fpath))
                return True, "test"

            plugin._remember_search(
                _DummyEvent(sender_id="500", group_id="G9"),
                [
                    main_mod.SearchEntry(
                        index=1,
                        title="Saved1",
                        source="ehentai",
                        url="https://e-hentai.org/g/333333/aabbccdd11/",
                    )
                ],
            )
            ev_dl = _DummyEvent(text="下载第1个", sender_id="500", group_id="G9")
            intent = parsing.parse_download_intent(ev_dl.get_message_str())
            self.assertIsNotNone(intent)
            self.assertTrue(await plugin._has_download_context(ev_dl, intent))
            plugin._mark_download_event_handled(ev_dl)
            with (
                patch.object(main_mod, "download_gallery", side_effect=_fake_dl),
                patch.object(plugin, "_send_download_file", side_effect=_fake_send),
                patch.object(main_mod, "get_astrbot_temp_path", return_value=str(tmp_dir)),
            ):
                results = [r async for r in plugin._do_download(ev_dl, intent)]
            self.assertEqual(dl_calls, ["https://e-hentai.org/g/333333/aabbccdd11/"])
            self.assertEqual(len(sent_files), 1)
            self.assertTrue(sent_files[0][0].endswith(".zip"))
            self.assertIs(ev_dl.llm_flag, False)
            self.assertTrue(any("完成" in str(r.payload) for r in results))

        asyncio.run(_run())
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_nsfw_policy_per_group_overrides_and_default_fallback(self):
        plugin = main_mod.SearchMangaPlugin(
            context=object(),
            config={
                "nsfw_policy": "send",
                "nsfw_group_settings": [
                    {"group_id": "123456", "nsfw_policy": "blur"},
                    {"__template_key": "group", "group_id": "654321", "nsfw_policy": "ignore"},
                ],
                "group_settings": [
                    {"group_id": "777888", "nsfw_policy": "blur"},
                ],
                "render_parse_card": False,
                "output_format": "merge",
            },
        )
        self.assertEqual(plugin._nsfw_policy(_DummyEvent(group_id="123456")), "blur")
        self.assertEqual(plugin._nsfw_policy(_DummyEvent(group_id="654321")), "ignore")
        self.assertEqual(plugin._nsfw_policy(_DummyEvent(group_id="777888")), "blur")
        self.assertEqual(plugin._nsfw_policy(_DummyEvent(group_id="999999")), "send")
        self.assertEqual(plugin._nsfw_policy(_DummyEvent(group_id="")), "send")
        self.assertEqual(plugin._nsfw_policy(None), "send")

        captured_policies = []

        async def _fake_parse(target, **kwargs):
            captured_policies.append(kwargs.get("nsfw_policy"))
            return main_mod.ParsedMedia(
                platform="iwara",
                title="Test",
                url=target,
                ignored_reason="ignored" if kwargs.get("nsfw_policy") == "ignore" else "",
            )

        async def _run_parse():
            with patch.object(main_mod, "parse_media_target", side_effect=_fake_parse):
                ev_blur = _DummyEvent(group_id="123456")
                [r async for r in plugin._parse_and_send(ev_blur, ["https://www.iwara.tv/video/abc"], silent_if_ignored=False)]

                ev_ignore = _DummyEvent(group_id="654321")
                res_ig = [r async for r in plugin._parse_and_send(ev_ignore, ["https://www.iwara.tv/video/abc"], silent_if_ignored=False)]

                ev_default = _DummyEvent(group_id="888000")
                [r async for r in plugin._parse_and_send(ev_default, ["https://www.iwara.tv/video/abc"], silent_if_ignored=False)]
            self.assertTrue(any("ignored" in str(r.payload) for r in res_ig))

        asyncio.run(_run_parse())
        self.assertEqual(captured_policies, ["blur", "ignore", "send"])

    def test_packaging_zip_pdf_and_prepare_send_file(self):
        tmp_dir = Path(packaging.temp_root()) / "test_pkg_suite"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        img1 = tmp_dir / "1.png"
        img2 = tmp_dir / "2.png"
        img1.write_bytes(_make_png_bytes((200, 20, 20)))
        img2.write_bytes(_make_png_bytes((20, 200, 20)))

        zip_dl = packaging.images_to_zip([img1, img2], "TestTitle")
        pdf_dl = packaging.images_to_pdf([img1, img2], "TestTitle")
        try:
            with zipfile.ZipFile(zip_dl.zip_path, "r") as zf:
                self.assertEqual(len(zf.namelist()), 2)

            self.assertTrue(Path(pdf_dl.zip_path).read_bytes().startswith(b"%PDF-"))

            z_name, z_send = main_mod._prepare_send_file(zip_dl.zip_path, "OutBook")
            p_name, p_send = main_mod._prepare_send_file(pdf_dl.zip_path, "OutBook")
            self.assertTrue(z_name.endswith(".zip"))
            self.assertTrue(p_name.endswith(".pdf"))
            self.assertTrue(Path(z_send).exists())
            self.assertTrue(Path(p_send).exists())

            Path(z_send).unlink(missing_ok=True)
            Path(p_send).unlink(missing_ok=True)
        finally:
            Path(zip_dl.zip_path).unlink(missing_ok=True)
            Path(pdf_dl.zip_path).unlink(missing_ok=True)
            img1.unlink(missing_ok=True)
            img2.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_search_manga_nsfw_policy_send_blur_ignore_and_no_leak(self):
        self.assertIsNone(packaging.try_blur_image_bytes(b"<html>not image</html>"))
        self.assertEqual(packaging.blur_image_bytes(b"<html>not image</html>"), b"<html>not image</html>")

        plugin = main_mod.SearchMangaPlugin(
            context=object(),
            config={
                "nsfw_policy": "send",
                "nsfw_group_settings": [
                    {"group_id": "123456", "nsfw_policy": "blur"},
                    {"group_id": "654321", "nsfw_policy": "ignore"},
                ],
                "send_preview_image": True,
                "max_results": 3,
                "proxy_enabled": True,
                "proxy_policy": "search_only",
                "proxy_url": "http://127.0.0.1:7897",
            },
        )
        entries = [
            main_mod.SearchEntry(
                index=1,
                title="Work 1",
                source="nhentai",
                url="https://nhentai.net/g/111/",
                similarity=95.5,
                preview_url="https://soutubot.moe/thumb/1.jpg",
            ),
            main_mod.SearchEntry(
                index=2,
                title="Work 2",
                source="jmcomic",
                url="https://18comic.vip/album/222/",
                similarity=90.0,
                preview_url="https://cdn-msp.18comic.vip/thumb/2.jpg",
            ),
        ]
        search_payload = {
            "data": [
                {
                    "title": "Work 1",
                    "source": "nhentai",
                    "pagePath": "/g/111/",
                    "similarity": 95.5,
                    "previewImageUrl": "https://soutubot.moe/thumb/1.jpg",
                },
                {
                    "title": "Work 2",
                    "source": "jmcomic",
                    "pagePath": "/album/222",
                    "similarity": 90.0,
                    "previewImageUrl": "https://cdn-msp.18comic.vip/thumb/2.jpg",
                },
            ]
        }
        # Test _build_forward_nodes fallback safety when blurred_paths omits an entry
        nodes_blur_empty = plugin._build_forward_nodes(
            _DummyEvent(group_id="123456"), entries
        )
        for node in nodes_blur_empty[1:]:
            imgs = [c for c in node.content if isinstance(c, Comp.Image)]
            self.assertEqual(imgs, [])

        raw_query = _make_png_bytes((20, 40, 60))
        raw_thumb1 = _make_png_bytes((240, 20, 20), size=(80, 80))
        fetched_urls: list[str] = []

        async def _fake_fetch(url: str, timeout: int, proxy=None):
            fetched_urls.append(url)
            if url == "https://example.com/query.png":
                return raw_query
            if url == "https://soutubot.moe/thumb/1.jpg":
                return raw_thumb1
            raise RuntimeError("thumb fetch failed")

        async def _run():
            with (
                patch.object(main_mod, "fetch_image_bytes", side_effect=_fake_fetch),
                patch.object(main_mod, "search_soutubot", new=AsyncMock(return_value=search_payload)),
            ):
                # 1. Default send: uses remote URLs without downloading thumbnails
                fetched_urls.clear()
                res_send = [
                    r async for r in plugin._do_search(
                        _DummyEvent(group_id="999999"),
                        "https://example.com/query.png",
                        send_start_notice=False,
                    )
                ]
                self.assertEqual(fetched_urls, ["https://example.com/query.png"])
                nodes_send = res_send[0].payload[0].nodes
                img1 = [c for c in nodes_send[1].content if isinstance(c, Comp.Image)][0]
                img2 = [c for c in nodes_send[2].content if isinstance(c, Comp.Image)][0]
                self.assertEqual(img1.url, "https://soutubot.moe/thumb/1.jpg")
                self.assertEqual(img2.url, "https://cdn-msp.18comic.vip/thumb/2.jpg")

                # 2. Ignore group: hides all preview images without fetching thumbnails
                fetched_urls.clear()
                res_ignore = [
                    r async for r in plugin._do_search(
                        _DummyEvent(group_id="654321"),
                        "https://example.com/query.png",
                        send_start_notice=False,
                    )
                ]
                self.assertEqual(fetched_urls, ["https://example.com/query.png"])
                nodes_ignore = res_ignore[0].payload[0].nodes
                for n in nodes_ignore[1:]:
                    self.assertEqual([c for c in n.content if isinstance(c, Comp.Image)], [])

                # 3. Blur group: downloads & blurs thumb 1 locally, omits failed thumb 2, cleans up temp files
                fetched_urls.clear()
                seen_blur_paths: list[Path] = []
                async for r in plugin._do_search(
                    _DummyEvent(group_id="123456"),
                    "https://example.com/query.png",
                    send_start_notice=False,
                ):
                    nodes_blur = r.payload[0].nodes
                    imgs_n1 = [c for c in nodes_blur[1].content if isinstance(c, Comp.Image)]
                    imgs_n2 = [c for c in nodes_blur[2].content if isinstance(c, Comp.Image)]
                    self.assertEqual(len(imgs_n1), 1)
                    self.assertEqual(len(imgs_n2), 0)
                    blur_path = Path(imgs_n1[0].path)
                    seen_blur_paths.append(blur_path)
                    self.assertTrue(blur_path.exists())
                    self.assertIsNone(getattr(imgs_n1[0], "url", None))
                    blurred_bytes = blur_path.read_bytes()
                    self.assertNotEqual(blurred_bytes, raw_thumb1)
                    with Image.open(BytesIO(blurred_bytes)) as im:
                        self.assertEqual(im.size, (80, 80))

                self.assertEqual(
                    set(fetched_urls),
                    {
                        "https://example.com/query.png",
                        "https://soutubot.moe/thumb/1.jpg",
                        "https://cdn-msp.18comic.vip/thumb/2.jpg",
                    },
                )
                self.assertEqual(len(seen_blur_paths), 1)
                # 延迟清理：生成器结束（LLM 工具路径下发图在之后）时文件必须仍在，
                # 但已经进入延迟清理队列，到点后由 _sweep_deferred_cleanup 删除。
                self.assertTrue(seen_blur_paths[0].exists())
                self.assertIn(str(seen_blur_paths[0].resolve()), plugin._deferred_cleanup)
                for key in list(plugin._deferred_cleanup):
                    plugin._deferred_cleanup[key] = 0.0
                plugin._sweep_deferred_cleanup()
                self.assertFalse(seen_blur_paths[0].exists())

        asyncio.run(_run())

    def test_search_download_output_formats_zip_pdf_and_merge(self):
        tmp_dir = Path(packaging.temp_root()) / "test_search_dl_formats"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        entry = main_mod.SearchEntry(
            index=1,
            title="MultiFmtBook",
            source="ehentai",
            url="https://e-hentai.org/g/555666/aabbccddee/",
            alternatives=[("nhentai", "https://nhentai.net/g/777888/")],
        )

        async def _run():
            # 1. PDF output format on search download
            plugin_pdf = main_mod.SearchMangaPlugin(
                context=object(),
                config={"output_format": "pdf", "parse_arbiter": False},
            )
            plugin_pdf._remember_search(_DummyEvent(sender_id="10", group_id="G1"), [entry])
            pdf_formats = []
            sent_files = []

            async def _fake_dl_pdf(url, **kwargs):
                pdf_formats.append(kwargs.get("output_format"))
                p1 = tmp_dir / "p1.png"
                p1.write_bytes(_make_png_bytes((30, 120, 200)))
                return packaging.images_to_pdf([p1], "MultiFmtBook")

            async def _fake_send_file(ev, fname, fpath):
                sent_files.append((fname, fpath))
                return True, "test"

            ev_pdf = _DummyEvent(text="下载第1个", sender_id="10", group_id="G1")
            intent = parsing.parse_download_intent(ev_pdf.get_message_str())
            with (
                patch.object(main_mod, "download_gallery", side_effect=_fake_dl_pdf),
                patch.object(plugin_pdf, "_send_download_file", side_effect=_fake_send_file),
                patch.object(main_mod, "get_astrbot_temp_path", return_value=str(tmp_dir)),
            ):
                res_pdf = [r async for r in plugin_pdf._do_download(ev_pdf, intent)]
            self.assertEqual(pdf_formats, ["pdf"])
            self.assertTrue(sent_files[0][0].endswith(".pdf"))
            self.assertTrue(any("文件已发送" in str(r.payload) for r in res_pdf))

            # 2. Merge output format on search download with source fallback & per-group NSFW blur
            plugin_merge = main_mod.SearchMangaPlugin(
                context=object(),
                config={
                    "output_format": "merge",
                    "preview_count": 2,
                    "render_parse_card": False,
                    "nsfw_policy": "send",
                    "nsfw_group_settings": [{"group_id": "G_BLUR", "nsfw_policy": "blur"}],
                    "parse_arbiter": False,
                },
            )
            plugin_merge._remember_search(_DummyEvent(sender_id="20", group_id="G_BLUR"), [entry])
            parse_calls = []

            async def _fake_parse_target(target_url, **kwargs):
                parse_calls.append((target_url, kwargs))
                if "e-hentai.org" in target_url:
                    raise RuntimeError("eh unavailable")
                work_dir = Path(packaging.temp_root()) / "test_merge_dl_work"
                work_dir.mkdir(parents=True, exist_ok=True)
                p_prev = work_dir / "00001_blur.jpg"
                p_prev.write_bytes(packaging.blur_image_bytes(_make_png_bytes((200, 30, 30))))
                return main_mod.ParsedMedia(
                    platform="nhentai",
                    title="MultiFmtBook",
                    url=target_url,
                    cover_bytes=_make_png_bytes((150, 50, 50)),
                    image_paths=[p_prev],
                    page_count=24,
                    is_nsfw=True,
                    work_dir=str(work_dir),
                )

            ev_merge = _DummyEvent(text="下载第1个", sender_id="20", group_id="G_BLUR", message_obj=types.SimpleNamespace(message=[], raw_message=None, self_id="99999", message_id="2002"))
            collected = []
            with (
                patch.object(main_mod, "parse_media_target", side_effect=_fake_parse_target),
                patch.object(main_mod, "download_gallery", AsyncMock()) as mocked_dl,
            ):
                async for r in plugin_merge._do_download(ev_merge, intent):
                    collected.append(r)
                    if isinstance(r.payload, list) and r.payload and hasattr(r.payload[0], "nodes"):
                        fwd_nodes = r.payload[0].nodes
                        self.assertEqual(len(fwd_nodes), 2)
                        self.assertTrue(r.stopped)
                        prev_imgs = [c for c in fwd_nodes[1].content if isinstance(c, Comp.Image)]
                        self.assertEqual(len(prev_imgs), 1)
                        self.assertTrue(Path(prev_imgs[0].path).exists())
                mocked_dl.assert_not_awaited()

            self.assertEqual(
                [u for u, _ in parse_calls],
                ["https://e-hentai.org/g/555666/aabbccddee/", "https://nhentai.net/g/777888/"],
            )
            self.assertEqual(parse_calls[1][1].get("output_format"), "merge")
            self.assertEqual(parse_calls[1][1].get("nsfw_policy"), "blur")
            self.assertFalse(parse_calls[1][1].get("download_gallery_file"))
            self.assertTrue(any(isinstance(r.payload, list) and r.payload and hasattr(r.payload[0], "nodes") for r in collected))
            merge_work_dir = Path(packaging.temp_root()) / "test_merge_dl_work"
            # 工作目录同样是延迟清理：生成器结束后仍在，避免 LLM 工具路径发送时预览图丢失。
            self.assertTrue(merge_work_dir.exists())
            self.assertIn(str(merge_work_dir.resolve()), plugin_merge._deferred_cleanup)
            for key in list(plugin_merge._deferred_cleanup):
                plugin_merge._deferred_cleanup[key] = 0.0
            plugin_merge._sweep_deferred_cleanup()
            self.assertFalse(merge_work_dir.exists())

        try:
            asyncio.run(_run())
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
