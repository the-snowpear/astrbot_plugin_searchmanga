import asyncio
import base64
import hashlib
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import astrbot_config, file_token_service
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
import astrbot.api.message_components as Comp

from .constants import (
    NSFW_POLICY_BLUR,
    NSFW_POLICY_IGNORE,
    NSFW_POLICY_SEND,
    OUTPUT_FORMAT_MERGE,
    OUTPUT_FORMAT_PDF,
    OUTPUT_FORMAT_ZIP,
    PROXY_POLICY_ALL,
    PROXY_POLICY_DISABLED,
    PROXY_POLICY_DOWNLOAD_ONLY,
    PROXY_POLICY_SEARCH_ONLY,
    VALID_NSFW_POLICIES,
    VALID_OUTPUT_FORMATS,
)
from .downloaders import download_gallery, parse_media_target
from .models import DownloadIntent, ParsedMedia, SearchEntry
from .onebot import (
    ArbiterContext,
    EmojiLikeArbiter,
    call_onebot_action,
    collect_forward_ids_from_components,
    extract_forward_ids_from_onebot_message,
    get_forward_msg,
    get_msg,
    iter_reply_components,
)
from .packaging import apply_nsfw_to_cover, render_info_card, temp_root, try_blur_image_bytes
from .parsing import (
    extract_json_url,
    extract_parse_targets,
    extract_text_from_forward_payload,
    extract_text_from_onebot_segments,
    is_supported_url,
    parse_download_intent,
    parse_search_entries_from_text,
    sanitize_filename,
    search_entry_from_item,
    source_from_url,
)
from .soutubot import fetch_image_bytes, search_soutubot


RECENT_IMAGE_TTL = 600
RECENT_SEARCH_TTL = 1800
SEND_FILE_TTL = 3600
DOWNLOAD_DEDUP_MIN_TTL = 300
DOWNLOAD_DEDUP_GRACE = 120
DOWNLOAD_HANDLED_EXTRA = "searchmanga_download_handled"
PARSE_HANDLED_EXTRA = "searchmanga_parse_handled"
DEFAULT_BASE64_FILE_LIMIT_MB = 48
DEFAULT_STREAM_CHUNK_KB = 256
STREAM_FILE_RETENTION_MS = 5 * 60 * 1000
# LLM 工具调用（call_local_llm_tool）会先把异步生成器消费完，再在工具返回后
# 才真正读取图片文件；因此临时卡片/预览图不能在被 yield 之后立刻删除，
# 否则会出现 "image file URI does not point to an existing file"。
TEMP_FILE_GRACE_SECONDS = 600
TEMP_FILE_SWEEP_INTERVAL = 30
ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
PDF_MAGIC_PREFIX = b"%PDF-"
GROUP_SETTING_TEMPLATE_KEY = "group"
PROXY_SOURCE_SOUTUBOT = "soutubot"
# source_proxies 中 proxy_url 填这些值表示“该来源强制直连”，用于覆盖默认代理。
DIRECT_PROXY_TOKENS = {"", "direct", "none", "off", "disabled", "no"}
_PROXY_UNSET = object()
_RESERVED_COMMAND_PREFIXES = (
    "/搜本子",
    "/搜本",
    "/soutu",
    "/下载本子",
    "/下载本",
    "/解析",
    "/jx",
    "/parse",
    "/开启解析",
    "/关闭解析",
)


def _normalize_proxy_url(raw) -> str:
    url = str(raw or "").strip()
    if url and "://" not in url:
        url = f"http://{url}"
    return url


def _assert_valid_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad_name = zf.testzip()
            if bad_name:
                raise RuntimeError(f"ZIP CRC 校验失败: {bad_name}")
            if not zf.infolist():
                raise RuntimeError("ZIP 文件为空")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ZIP 文件损坏: {path}") from exc


def _assert_valid_pdf(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"PDF 文件为空: {path}")
    with path.open("rb") as fp:
        head = fp.read(16)
    if not head.startswith(PDF_MAGIC_PREFIX):
        raise RuntimeError(f"文件不是有效的 PDF 格式: {path}")


def _assert_valid_send_file(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".zip":
        _assert_valid_zip(path)
    elif suffix == ".pdf":
        _assert_valid_pdf(path)
    elif not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"待发送文件为空或不存在: {path}")


def _prepare_send_file(source_path: str, title: str) -> tuple[str, str]:
    source = Path(source_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"下载文件不存在: {source}")
    _assert_valid_send_file(source)
    ext = source.suffix.lower() or ".zip"
    send_dir = Path(get_astrbot_temp_path()) / "searchmanga_downloads"
    send_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title, "doujin")
    file_name = f"{safe_title}{ext}"
    unique_suffix = uuid.uuid4().hex[:8]
    send_path = send_dir / f"{safe_title}_{unique_suffix}{ext}"
    if source.resolve() != send_path.resolve():
        shutil.copy2(source, send_path)
    _assert_valid_send_file(send_path)
    return file_name, str(send_path.resolve())


async def _assert_download_url_returns_supported_file(url: str, ext: str = ".zip") -> None:
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=12)
    headers = {"Range": "bytes=0-511", "User-Agent": "AstrBot-SearchManga/0.3"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers=headers) as resp:
                sample = await resp.content.read(512)
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
    except Exception as exc:
        raise RuntimeError(f"文件回调地址不可访问: {url} ({exc})") from exc

    if status not in {200, 206}:
        preview = sample.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"文件回调地址返回 HTTP {status}，不是可下载文件: {url}; {preview[:160]}"
        )
    norm_ext = (ext or ".zip").lower()
    if norm_ext == ".zip" and not sample.startswith(ZIP_MAGIC_PREFIXES):
        preview = sample.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "文件回调地址返回的不是 ZIP 数据，已阻止发送，避免 NapCat 把错误页保存成 .zip。"
            f" status={status}, content-type={content_type or '(空)'}, preview={preview[:160]}"
        )
    if norm_ext == ".pdf" and not sample.startswith(PDF_MAGIC_PREFIX):
        preview = sample.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "文件回调地址返回的不是 PDF 数据，已阻止发送。"
            f" status={status}, content-type={content_type or '(空)'}, preview={preview[:160]}"
        )


async def _assert_download_url_returns_zip(url: str) -> None:
    await _assert_download_url_returns_supported_file(url, ".zip")


def _file_to_base64_resource(file_path: str) -> str:
    data = Path(file_path).read_bytes()
    return "base64://" + base64.b64encode(data).decode("ascii")


def _b64encode_chunk(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _normalize_callback_base(value: str) -> str:
    base = value.strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base


def _extract_components_from_event(event: AstrMessageEvent) -> list[Any]:
    components: list[Any] = []
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        try:
            res = getter()
            if isinstance(res, list):
                components.extend(res)
        except Exception:
            pass
    message_obj = getattr(event, "message_obj", None)
    if message_obj:
        chain = getattr(message_obj, "message", None)
        if isinstance(chain, list):
            for item in chain:
                if item not in components:
                    components.append(item)
    return components


def _safe_isinstance(obj: Any, cls: Any) -> bool:
    return isinstance(cls, type) and isinstance(obj, cls)


def _extract_image_url_from_component(comp: Any) -> Optional[str]:
    if _safe_isinstance(comp, getattr(Comp, "Image", None)):
        url = (
            getattr(comp, "url", None)
            or getattr(comp, "file", None)
            or getattr(comp, "path", None)
        )
        if url and isinstance(url, str):
            return url.strip()
    elif isinstance(comp, dict):
        if comp.get("type") == "image":
            data = comp.get("data")
            if isinstance(data, dict):
                url = data.get("url") or data.get("file") or data.get("path")
                if url and isinstance(url, str):
                    return url.strip()
            elif isinstance(comp.get("file"), str):
                return comp["file"].strip()
    return None


def _extract_image_url(event: AstrMessageEvent) -> Optional[str]:
    """同步提取当前消息中的图片（用于快速记录和回退）"""
    components = _extract_components_from_event(event)
    for comp in components:
        url = _extract_image_url_from_component(comp)
        if url:
            return url

    reply_cls = getattr(Comp, "Reply", None)
    for comp in components:
        if _safe_isinstance(comp, reply_cls):
            for field in ("chain", "message", "content", "origin"):
                sub_chain = getattr(comp, field, None)
                if isinstance(sub_chain, list):
                    for s in sub_chain:
                        url = _extract_image_url_from_component(s)
                        if url:
                            return url
                elif sub_chain:
                    url = _extract_image_url_from_component(sub_chain)
                    if url:
                        return url

    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw_message:
        from .onebot import extract_images_from_onebot_message

        images = extract_images_from_onebot_message(raw_message)
        if images:
            return images[0]

    return None


def _collect_reply_message_ids(event: AstrMessageEvent) -> list[str | int]:
    reply_ids: list[str | int] = []

    def _add(val: Any) -> None:
        if val is not None and str(val).strip() and val not in reply_ids:
            reply_ids.append(val)

    reply_cls = getattr(Comp, "Reply", None)
    for comp in [*iter_reply_components(event), *_extract_components_from_event(event)]:
        if _safe_isinstance(comp, reply_cls):
            _add(getattr(comp, "id", None) or getattr(comp, "message_id", None))
        elif isinstance(comp, dict) and comp.get("type") == "reply":
            data = comp.get("data")
            if isinstance(data, dict):
                _add(data.get("id") or data.get("message_id"))

    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw_message, dict):
        raw_reply = raw_message.get("reply")
        if isinstance(raw_reply, dict):
            _add(raw_reply.get("message_id") or raw_reply.get("id"))
        raw_segs = raw_message.get("message")
        if isinstance(raw_segs, list):
            for seg in raw_segs:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    data = seg.get("data")
                    if isinstance(data, dict):
                        _add(data.get("id") or data.get("message_id"))
    elif isinstance(raw_message, list):
        for seg in raw_message:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                data = seg.get("data")
                if isinstance(data, dict):
                    _add(data.get("id") or data.get("message_id"))
    return reply_ids


async def _resolve_image_url_async(event: AstrMessageEvent) -> Optional[str]:
    """异步完整提取图片（含 OneBot 引用消息回查）"""
    url = _extract_image_url(event)
    if url:
        return url

    reply_ids = _collect_reply_message_ids(event)
    if reply_ids:
        from .onebot import extract_images_from_onebot_message

        for r_id in reply_ids:
            try:
                msg_payload = await get_msg(event, r_id)
                images = extract_images_from_onebot_message(msg_payload)
                if images:
                    return images[0]
            except Exception as exc:
                logger.debug(f"获取引用消息图片失败: reply_id={r_id}, {exc}")

    return None


def _event_text(event: AstrMessageEvent) -> str:
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, str):
                return value.strip()
        except Exception:
            pass
    value = getattr(event, "message_str", None)
    if isinstance(value, str):
        return value.strip()
    value = getattr(event.message_obj, "message_str", None)
    if isinstance(value, str):
        return value.strip()
    return ""


def _coerce_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    return default


def _is_under_dir(path_str: str | None, root: Path) -> bool:
    if not path_str:
        return False
    try:
        Path(path_str).resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


class SearchMangaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._recent_images: dict[str, tuple[str, float]] = {}
        self._recent_searches: dict[str, tuple[list[SearchEntry], float]] = {}
        self._download_files: dict[str, tuple[str, float]] = {}
        self._download_claims: dict[str, float] = {}
        self._parse_debounce: dict[str, float] = {}
        self._session_parse_overrides: dict[str, bool] = {}
        self._arbiter = EmojiLikeArbiter()
        # 延迟清理队列：path -> 到期时间戳。用于修复 LLM 工具调用路径下
        # 异步生成器被“先消费完、后发送”导致的临时图片提前删除问题。
        self._deferred_cleanup: dict[str, float] = {}
        self._cleanup_task: asyncio.Task | None = None

    def _cleanup_grace_seconds(self) -> float:
        try:
            value = float(self.config.get("cleanup_grace_seconds", TEMP_FILE_GRACE_SECONDS))
        except (TypeError, ValueError):
            return float(TEMP_FILE_GRACE_SECONDS)
        return max(30.0, value)

    def _ensure_cleanup_task(self) -> None:
        task = self._cleanup_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cleanup_task = loop.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TEMP_FILE_SWEEP_INTERVAL)
                self._sweep_deferred_cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("搜本子临时文件清理循环异常退出", exc_info=True)

    def _defer_cleanup(self, *targets: str | Path | None, ttl: float | None = None) -> None:
        """把临时文件/目录延迟 ttl 秒后删除，保留发送窗口。"""
        deadline = time.time() + (self._cleanup_grace_seconds() if ttl is None else ttl)
        for target in targets:
            if not target:
                continue
            path_str = str(target)
            if not _is_under_dir(path_str, temp_root()):
                # 只清理插件自己的临时目录，绝不碰下载到用户目录的文件。
                continue
            try:
                key = str(Path(path_str).resolve())
            except OSError:
                key = path_str
            self._deferred_cleanup[key] = deadline
        self._sweep_deferred_cleanup()
        if self._deferred_cleanup:
            self._ensure_cleanup_task()

    def _sweep_deferred_cleanup(self) -> None:
        if not self._deferred_cleanup:
            return
        now = time.time()
        for key in [k for k, expire_at in self._deferred_cleanup.items() if expire_at <= now]:
            self._deferred_cleanup.pop(key, None)
            path = Path(key)
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    logger.debug(f"清理搜本子临时目录: {key}")
                else:
                    path.unlink(missing_ok=True)
                    logger.debug(f"清理搜本子临时文件: {key}")
            except OSError:
                logger.debug(f"清理搜本子临时文件失败: {key}", exc_info=True)

    def _cleanup_download_files(self) -> None:
        now = time.time()
        expired = [
            token
            for token, (_, expires_at) in self._download_files.items()
            if expires_at < now
        ]
        for token in expired:
            item = self._download_files.pop(token, None)
            if item:
                file_path, _ = item
                try:
                    Path(file_path).unlink(missing_ok=True)
                except OSError:
                    logger.debug(f"清理搜本子下载临时文件失败: {file_path}")

    def _download_claim_ttl(self) -> int:
        return max(self._download_timeout() + DOWNLOAD_DEDUP_GRACE, DOWNLOAD_DEDUP_MIN_TTL)

    def _cleanup_download_claims(self) -> None:
        now = time.time()
        expired = [
            key
            for key, expires_at in self._download_claims.items()
            if expires_at < now
        ]
        for key in expired:
            self._download_claims.pop(key, None)

    def _mark_download_event_handled(self, event: AstrMessageEvent) -> None:
        event.set_extra(DOWNLOAD_HANDLED_EXTRA, True)
        event.set_extra("agent_stop_requested", True)
        should_call_llm = getattr(event, "should_call_llm", None)
        if callable(should_call_llm):
            should_call_llm(False)

    def _download_event_handled(self, event: AstrMessageEvent) -> bool:
        get_extra = getattr(event, "get_extra", None)
        if not callable(get_extra):
            return False
        return bool(get_extra(DOWNLOAD_HANDLED_EXTRA, False))

    def _mark_parse_event_handled(self, event: AstrMessageEvent) -> None:
        event.set_extra(PARSE_HANDLED_EXTRA, True)
        event.set_extra("agent_stop_requested", True)
        should_call_llm = getattr(event, "should_call_llm", None)
        if callable(should_call_llm):
            should_call_llm(False)

    def _parse_event_handled(self, event: AstrMessageEvent) -> bool:
        get_extra = getattr(event, "get_extra", None)
        if not callable(get_extra):
            return False
        return bool(get_extra(PARSE_HANDLED_EXTRA, False))

    def _event_message_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        if message_id:
            return str(message_id)
        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            raw_message_id = raw_message.get("message_id")
            if raw_message_id:
                return str(raw_message_id)
        raw_message_id = getattr(raw_message, "message_id", None)
        if raw_message_id:
            return str(raw_message_id)
        return f"event:{id(event)}"

    def _download_dedup_key(
        self,
        event: AstrMessageEvent,
        intent: DownloadIntent,
        target: SearchEntry,
    ) -> str:
        platform = ""
        get_platform_id = getattr(event, "get_platform_id", None)
        if callable(get_platform_id):
            platform = str(get_platform_id() or "")
        if not platform:
            get_platform_name = getattr(event, "get_platform_name", None)
            platform = str(get_platform_name() if callable(get_platform_name) else "")

        target_id = target.url or ""
        if not target_id and intent.jm_id:
            target_id = f"jm:{intent.jm_id}"
        if not target_id and intent.index:
            target_id = f"index:{intent.index}"

        raw_key = "|".join(
            (
                platform,
                self._session_key(event),
                self._event_message_id(event),
                target_id,
            )
        )
        return hashlib.sha256(raw_key.encode("utf-8", errors="ignore")).hexdigest()

    def _claim_download_once(
        self,
        event: AstrMessageEvent,
        intent: DownloadIntent,
        target: SearchEntry,
    ) -> bool:
        self._cleanup_download_claims()
        key = self._download_dedup_key(event, intent, target)
        if key in self._download_claims:
            logger.info(
                f"[搜本子下载] 跳过重复下载: message_id={self._event_message_id(event)}, url={target.url}"
            )
            return False
        self._download_claims[key] = time.time() + self._download_claim_ttl()
        return True

    def _max_results(self) -> int:
        return max(1, int(self.config.get("max_results", 5)))

    def _threshold(self) -> float:
        return float(self.config.get("similarity_threshold", 28))

    def _factor(self) -> float:
        factor = float(self.config.get("factor", 1.2))
        return factor if factor > 0 else 1.2

    def _timeout(self) -> int:
        return int(self.config.get("timeout", 60))

    def _download_timeout(self) -> int:
        return int(self.config.get("download_timeout", 600))

    def _download_concurrency(self) -> int:
        return max(1, int(self.config.get("download_concurrency", 4)))

    def _auto_parse_enabled(self) -> bool:
        return _coerce_bool(self.config.get("auto_parse_enabled", True), True)

    def _parse_enable_reply(self) -> bool:
        return _coerce_bool(self.config.get("parse_enable_reply", True), True)

    def _parse_debounce_seconds(self) -> int:
        try:
            return max(0, int(self.config.get("parse_debounce_seconds", 10)))
        except (TypeError, ValueError):
            return 10

    def _parse_arbiter_enabled(self) -> bool:
        return _coerce_bool(self.config.get("parse_arbiter", True), True)

    def _string_set_config(self, key: str) -> set[str]:
        raw = self.config.get(key, []) or []
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {str(item).strip() for item in raw if str(item or "").strip()}

    def _output_format(self) -> str:
        val = str(self.config.get("output_format", OUTPUT_FORMAT_ZIP) or "").strip().lower()
        return val if val in VALID_OUTPUT_FORMATS else OUTPUT_FORMAT_ZIP

    def _preview_count(self) -> int:
        try:
            return max(0, int(self.config.get("preview_count", 3)))
        except (TypeError, ValueError):
            return 3

    def _max_pages(self) -> int:
        try:
            return max(0, int(self.config.get("max_pages", 200)))
        except (TypeError, ValueError):
            return 200

    def _find_group_config_entry(
        self,
        event: AstrMessageEvent | None,
        config_key: str,
    ) -> dict | None:
        if event is None:
            return None
        group_id = event.get_group_id()
        if not group_id:
            return None
        group_id = str(group_id).strip()
        settings = self.config.get(config_key, []) or []
        if not isinstance(settings, list):
            return None
        for item in settings:
            if not isinstance(item, dict):
                continue
            template_key = item.get("__template_key") or item.get("template")
            if template_key and template_key != GROUP_SETTING_TEMPLATE_KEY:
                continue
            if str(item.get("group_id", "")).strip() == group_id:
                return item
        return None

    def _nsfw_policy(self, event: AstrMessageEvent | None = None) -> str:
        for config_key in ("nsfw_group_settings", "group_settings"):
            entry = self._find_group_config_entry(event, config_key)
            if entry is not None:
                group_val = str(entry.get("nsfw_policy", "") or "").strip().lower()
                if group_val in VALID_NSFW_POLICIES:
                    return group_val

        val = str(self.config.get("nsfw_policy", NSFW_POLICY_SEND) or "").strip().lower()
        return val if val in VALID_NSFW_POLICIES else NSFW_POLICY_SEND

    def _render_parse_card(self) -> bool:
        return _coerce_bool(self.config.get("render_parse_card", True), True)

    def _card_font_path(self) -> str | None:
        value = str(self.config.get("card_font_path", "") or "").strip()
        return value or None

    def _iwara_cookie(self) -> str:
        return str(self.config.get("iwara_cookie", "") or "")

    def _iwara_video_quality(self) -> str:
        return str(self.config.get("iwara_video_quality", "540") or "540").strip()

    def _iwara_max_size_mb(self) -> int:
        try:
            return max(0, int(self.config.get("iwara_max_size_mb", 100)))
        except (TypeError, ValueError):
            return 100

    def _group_setting(self, event: AstrMessageEvent) -> dict | None:
        return self._find_group_config_entry(event, "group_settings")

    def _send_preview_image(self, event: AstrMessageEvent) -> bool:
        default = _coerce_bool(self.config.get("send_preview_image", True), True)
        group_setting = self._group_setting(event)
        if group_setting is None:
            return default
        return _coerce_bool(group_setting.get("send_preview_image", default), default)

    def _allow_download(self, event: AstrMessageEvent) -> bool:
        default = _coerce_bool(self.config.get("allow_download", True), True)
        group_setting = self._group_setting(event)
        if group_setting is None:
            return default
        return _coerce_bool(group_setting.get("allow_download", default), default)

    def _base64_file_limit_bytes(self) -> int:
        value = self.config.get("base64_file_limit_mb", DEFAULT_BASE64_FILE_LIMIT_MB)
        try:
            mb = int(value)
        except (TypeError, ValueError):
            mb = DEFAULT_BASE64_FILE_LIMIT_MB
        return max(0, mb) * 1024 * 1024

    def _stream_chunk_bytes(self) -> int:
        value = self.config.get("stream_chunk_kb", DEFAULT_STREAM_CHUNK_KB)
        try:
            kb = int(value)
        except (TypeError, ValueError):
            kb = DEFAULT_STREAM_CHUNK_KB
        return max(16, kb) * 1024

    def _ehentai_cookie(self) -> str:
        return str(self.config.get("ehentai_cookie", "") or "")

    def _exhentai_cookie(self) -> str:
        return str(self.config.get("exhentai_cookie", "") or "")

    def _prefer_archive_download(self) -> bool:
        return bool(self.config.get("prefer_archive_download", True))

    def _proxy_enabled(self) -> bool:
        return bool(self.config.get("proxy_enabled", False))

    def _proxy_policy(self) -> str:
        policy = str(self.config.get("proxy_policy", PROXY_POLICY_DOWNLOAD_ONLY) or "").strip()
        valid = {
            PROXY_POLICY_DISABLED,
            PROXY_POLICY_ALL,
            PROXY_POLICY_SEARCH_ONLY,
            PROXY_POLICY_DOWNLOAD_ONLY,
        }
        return policy if policy in valid else PROXY_POLICY_DOWNLOAD_ONLY

    def _proxy_url(self) -> str:
        return _normalize_proxy_url(self.config.get("proxy_url", "http://127.0.0.1:7897"))

    def _source_proxy_map(self) -> dict[str, str]:
        """解析 source_proxies 列表，返回 {来源: 代理地址}。"""
        entries = self.config.get("source_proxies", [])
        mapping: dict[str, str] = {}
        if not isinstance(entries, list):
            return mapping
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source = str(entry.get("source", "") or "").strip().lower()
            if not source:
                continue
            raw = str(entry.get("proxy_url", "") or "").strip()
            mapping[source] = (
                "" if raw.lower() in DIRECT_PROXY_TOKENS else _normalize_proxy_url(raw)
            )
        return mapping

    def _proxy_for(self, phase: str, source: str | None = None) -> str | None:
        override = self._source_proxy_map().get(
            (source or "").strip().lower(), _PROXY_UNSET
        )
        if override is not _PROXY_UNSET:
            return override or None

        if not self._proxy_enabled():
            return None
        proxy_url = self._proxy_url()
        if not proxy_url:
            return None
        policy = self._proxy_policy()
        if policy == PROXY_POLICY_DISABLED:
            return None
        if policy == PROXY_POLICY_ALL:
            return proxy_url
        if policy == PROXY_POLICY_SEARCH_ONLY and phase == "search":
            return proxy_url
        if policy == PROXY_POLICY_DOWNLOAD_ONLY and phase == "download":
            return proxy_url
        return None

    def _file_callback_base(self) -> str:
        configured = str(self.config.get("file_callback_base", "") or "")
        if configured.strip():
            return _normalize_callback_base(configured)
        global_base = str(astrbot_config.get("callback_api_base", "") or "")
        if global_base.strip():
            return _normalize_callback_base(global_base)
        return ""

    async def _file_callback_url(self, file_path: str) -> str:
        base = self._file_callback_base()
        if not base:
            raise RuntimeError(
                "未配置文件服务地址。请在插件配置 file_callback_base 或 AstrBot 全局 callback_api_base "
                "中填写协议端可访问的 AstrBot 地址，例如 http://127.0.0.1:6185"
            )
        path = Path(file_path)
        token = await file_token_service.register_file(str(path.resolve()), timeout=SEND_FILE_TTL)
        self._cleanup_download_files()
        self._download_files[token] = (str(path), time.time() + SEND_FILE_TTL)
        return f"{base}/api/file/{token}"

    async def _validated_file_callback_url(self, file_path: str) -> str:
        probe_url = await self._file_callback_url(file_path)
        await _assert_download_url_returns_supported_file(probe_url, Path(file_path).suffix)
        return await self._file_callback_url(file_path)

    async def _send_onebot_file(
        self,
        event: AstrMessageEvent,
        file_value: str,
        file_name: str,
        label: str,
    ) -> bool:
        group_id = event.get_group_id()
        if group_id:
            await call_onebot_action(
                event,
                "upload_group_file",
                group_id=str(group_id),
                file=file_value,
                name=file_name,
            )
            logger.info(f"[搜本子下载] OneBot 群文件发送成功 ({label}): {file_name}")
            return True

        user_id = event.get_sender_id() or event.get_session_id()
        if not user_id:
            raise RuntimeError("无法获取私聊 user_id")
        await call_onebot_action(
            event,
            "upload_private_file",
            user_id=str(user_id),
            file=file_value,
            name=file_name,
        )
        logger.info(f"[搜本子下载] OneBot 私聊文件发送成功 ({label}): {file_name}")
        return True

    async def _upload_file_stream_to_onebot(
        self,
        event: AstrMessageEvent,
        file_path: str,
        file_name: str,
    ) -> str:
        path = Path(file_path)
        stream_id = f"searchmanga_{uuid.uuid4().hex}"
        file_size = path.stat().st_size
        chunk_size = self._stream_chunk_bytes()
        total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size)
        sha256 = hashlib.sha256()
        with path.open("rb") as fp:
            while True:
                chunk = fp.read(chunk_size)
                if not chunk:
                    break
                sha256.update(chunk)
        expected_sha256 = sha256.hexdigest()

        chunk_index = 0
        with path.open("rb") as fp:
            while True:
                chunk = fp.read(chunk_size)
                if not chunk:
                    break
                await call_onebot_action(
                    event,
                    "upload_file_stream",
                    stream_id=stream_id,
                    chunk_data=_b64encode_chunk(chunk),
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    file_size=file_size,
                    expected_sha256=expected_sha256,
                    filename=file_name,
                    file_retention=STREAM_FILE_RETENTION_MS,
                )
                chunk_index += 1

        result = await call_onebot_action(
            event,
            "upload_file_stream",
            stream_id=stream_id,
            is_complete=True,
        )
        if isinstance(result, dict) and "file_path" not in result and isinstance(result.get("data"), dict):
            result = result["data"]
        if not isinstance(result, dict):
            raise RuntimeError(f"upload_file_stream 返回异常: {result!r}")
        stream_path = result.get("file_path") or result.get("path") or result.get("file")
        if not stream_path:
            raise RuntimeError(f"upload_file_stream 未返回可上传文件路径: {result!r}")
        logger.info(
            f"[搜本子下载] OneBot 流式上传完成: {file_name}, chunks={total_chunks}, size={file_size}"
        )
        return str(stream_path)

    async def _send_download_file(
        self,
        event: AstrMessageEvent,
        file_name: str,
        send_path: str,
    ) -> tuple[bool, str]:
        file_size = Path(send_path).stat().st_size
        base64_limit = self._base64_file_limit_bytes()
        if base64_limit and file_size <= base64_limit:
            try:
                resource = _file_to_base64_resource(send_path)
                await self._send_onebot_file(event, resource, file_name, "base64")
                return True, "base64"
            except Exception as exc:
                logger.warning(f"[搜本子下载] base64 发送失败，改用流式上传: {exc}", exc_info=True)

        try:
            stream_path = await self._upload_file_stream_to_onebot(event, send_path, file_name)
            await self._send_onebot_file(event, stream_path, file_name, "stream")
            return True, "stream"
        except Exception as exc:
            logger.warning(f"[搜本子下载] OneBot 流式上传失败，改用 HTTP 回调: {exc}", exc_info=True)

        file_url = await self._validated_file_callback_url(send_path)
        logger.info(
            f"[搜本子下载] 文件回调地址校验通过: {file_url}, size={file_size} bytes"
        )

        try:
            await self._send_onebot_file(event, file_url, file_name, "http-url")
            return True, "http-url"
        except Exception as exc:
            logger.warning(f"[搜本子下载] OneBot 上传接口发送失败，回退到文件消息段: {exc}", exc_info=True)
            await event.send(event.chain_result([Comp.File(name=file_name, url=file_url)]))
            return True, "component-url"

    def _node_name(self) -> str:
        return str(self.config.get("node_name", "搜本子"))

    def _node_uin(self, event: AstrMessageEvent) -> str:
        # Node.uin 的字段类型是 str | None（pydantic v2 严格校验）
        self_id = getattr(event.message_obj, "self_id", None)
        if self_id:
            return str(self_id)
        return str(self.config.get("node_uin", 10000))

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", None) or event.get_session_id())

    def _user_search_key(self, event: AstrMessageEvent) -> str:
        session_key = self._session_key(event)
        sender_id = str(event.get_sender_id() or "").strip() if callable(getattr(event, "get_sender_id", None)) else ""
        return f"{session_key}:u:{sender_id}" if sender_id else session_key

    def _remember_image(self, event: AstrMessageEvent) -> None:
        image_url = _extract_image_url(event)
        if image_url:
            self._recent_images[self._session_key(event)] = (image_url, time.time())

    def _recent_image_url(self, event: AstrMessageEvent) -> Optional[str]:
        item = self._recent_images.get(self._session_key(event))
        if not item:
            return None
        image_url, saved_at = item
        if time.time() - saved_at > RECENT_IMAGE_TTL:
            self._recent_images.pop(self._session_key(event), None)
            return None
        return image_url

    def _remember_search(self, event: AstrMessageEvent, entries: list[SearchEntry]) -> None:
        now = time.time()
        session_key = self._session_key(event)
        user_key = self._user_search_key(event)
        self._recent_searches[session_key] = (entries, now)
        if user_key != session_key:
            self._recent_searches[user_key] = (entries, now)

    def _recent_search_entry(self, event: AstrMessageEvent, index: int) -> SearchEntry | None:
        now = time.time()
        keys = [self._user_search_key(event), self._session_key(event)]
        for key in keys:
            item = self._recent_searches.get(key)
            if not item:
                continue
            entries, saved_at = item
            if now - saved_at > RECENT_SEARCH_TTL:
                self._recent_searches.pop(key, None)
                continue
            for entry in entries:
                if entry.index == index:
                    return entry
        return None

    def _search_preview_proxy(self, preview_url: str, entry_source: str) -> str | None:
        host = (urlparse(preview_url).hostname or "").lower()
        if host == "soutubot.moe" or host.endswith(".soutubot.moe"):
            source = PROXY_SOURCE_SOUTUBOT
        else:
            source = source_from_url(preview_url) or entry_source or PROXY_SOURCE_SOUTUBOT
        return self._proxy_for("search", source)

    async def _prepare_blurred_search_previews(
        self,
        entries: list[SearchEntry],
    ) -> dict[int, Path]:
        """并发拉取 /搜本子 预览缩略图并做高斯模糊，失败时宁可隐藏也不回退原图。"""
        targets = [entry for entry in entries if entry.preview_url]
        if not targets:
            return {}

        sem = asyncio.Semaphore(4)
        blurred_paths: dict[int, Path] = {}

        async def _worker(entry: SearchEntry) -> None:
            proxy = self._search_preview_proxy(entry.preview_url, entry.source)
            async with sem:
                raw = await fetch_image_bytes(
                    entry.preview_url,
                    self._timeout(),
                    proxy=proxy,
                )
            blurred = try_blur_image_bytes(raw)
            if not blurred:
                logger.warning(
                    f"[搜本子] 预览图模糊处理失败（缺少 Pillow 或图片无效），已隐藏该条预览图: #{entry.index}"
                )
                return
            dest = temp_root() / f"search_blur_{uuid.uuid4().hex[:8]}.jpg"
            try:
                dest.write_bytes(blurred)
                blurred_paths[entry.index] = dest
            except OSError:
                dest.unlink(missing_ok=True)
                raise

        try:
            results = await asyncio.gather(
                *(_worker(entry) for entry in targets),
                return_exceptions=True,
            )
        except BaseException:
            for p in blurred_paths.values():
                p.unlink(missing_ok=True)
            raise

        for entry, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.warning(
                    f"[搜本子] 预览图拉取/模糊失败，已隐藏该条预览图 (#{entry.index}): {result}"
                )
        return blurred_paths

    def _build_forward_nodes(
        self,
        event: AstrMessageEvent,
        entries: list[SearchEntry],
        *,
        nsfw_policy: str | None = None,
        blurred_paths: dict[int, Path] | None = None,
    ) -> list[Comp.Node]:
        threshold = self._threshold()
        node_uin = self._node_uin(event)
        send_preview = self._send_preview_image(event)
        policy = nsfw_policy if nsfw_policy is not None else self._nsfw_policy(event)
        intro_text = f"找到了 {len(entries)} 条结果。"
        if self._allow_download(event):
            intro_text += "引用这条合并消息回复「下载第1个」，或直接发送「/下载本子 1」就能下载。"
        else:
            intro_text += "当前会话已禁用下载功能，仅展示搜索结果。"
        nodes: list[Comp.Node] = [
            Comp.Node(
                uin=node_uin,
                name=self._node_name(),
                content=[Comp.Plain(intro_text)],
            )
        ]

        for entry in entries:
            flag = "⚠ 低相似度 " if entry.similarity < threshold else ""
            text = (
                f"#{entry.index} [{entry.source}] {flag}相似度 {entry.similarity:.2f}%\n"
                f"标题: {entry.title}\n"
                f"来源: {entry.source}\n"
                f"链接: {entry.url or '(无)'}"
            )
            extra_links = [
                alt_url
                for _, alt_url in entry.alternatives
                if alt_url and alt_url != entry.url and is_supported_url(alt_url)
            ]
            if extra_links:
                text += f"\n备用: {extra_links[0]}"
            content: list = []
            if send_preview and entry.preview_url and policy != NSFW_POLICY_IGNORE:
                if policy == NSFW_POLICY_BLUR:
                    blurred_path = (blurred_paths or {}).get(entry.index)
                    if blurred_path:
                        content.append(Comp.Image.fromFileSystem(str(blurred_path)))
                else:
                    content.append(Comp.Image.fromURL(entry.preview_url))
            content.append(Comp.Plain(text))
            nodes.append(Comp.Node(uin=node_uin, name=self._node_name(), content=content))
        return nodes

    async def _collect_quoted_texts(self, event: AstrMessageEvent) -> list[str]:
        texts: list[str] = []
        seen_forwards: set[str] = set()

        async def _expand_forward(forward_id: str, depth: int = 0) -> None:
            fid = str(forward_id or "").strip()
            if not fid or fid in seen_forwards or depth > 2:
                return
            seen_forwards.add(fid)
            try:
                payload = await get_forward_msg(event, fid)
            except Exception as exc:
                logger.debug(f"拉取合并转发消息失败: forward_id={fid}, {exc}")
                return
            text = extract_text_from_forward_payload(payload)
            if text:
                texts.append(text)
            for nested_fid in extract_forward_ids_from_onebot_message(payload):
                await _expand_forward(nested_fid, depth + 1)

        replies: list[Any] = []
        reply_cls = getattr(Comp, "Reply", None)
        for comp in [*iter_reply_components(event), *_extract_components_from_event(event)]:
            if _safe_isinstance(comp, reply_cls) or (isinstance(comp, dict) and comp.get("type") == "reply"):
                if comp not in replies:
                    replies.append(comp)

        for reply in replies:
            reply_text = getattr(reply, "message_str", None) or ""
            if reply_text:
                texts.append(reply_text)

            reply_chain = getattr(reply, "chain", None) or getattr(reply, "message", None) or []
            if reply_chain:
                seg_text = extract_text_from_onebot_segments(reply_chain)
                if seg_text:
                    texts.append(seg_text)
                for fid in collect_forward_ids_from_components(reply_chain):
                    await _expand_forward(fid)

        for reply_msg_id in _collect_reply_message_ids(event):
            try:
                msg_payload = await get_msg(event, reply_msg_id)
            except Exception as exc:
                logger.debug(f"get_msg 拉取被引用消息失败: reply_id={reply_msg_id}, {exc}")
                continue
            seg_text = extract_text_from_onebot_segments(msg_payload)
            if seg_text:
                texts.append(seg_text)
            fwd_text = extract_text_from_forward_payload(msg_payload)
            if fwd_text and fwd_text != seg_text:
                texts.append(fwd_text)
            for fid in extract_forward_ids_from_onebot_message(msg_payload):
                await _expand_forward(fid)

        return texts

    async def _entry_from_quoted_forward(
        self,
        event: AstrMessageEvent,
        index: int,
    ) -> SearchEntry | None:
        for text in await self._collect_quoted_texts(event):
            for entry in parse_search_entries_from_text(text):
                if entry.index == index:
                    return entry
        return None

    async def _resolve_download_target(
        self,
        event: AstrMessageEvent,
        intent: DownloadIntent,
    ) -> SearchEntry | None:
        if intent.jm_id:
            return SearchEntry(
                index=0,
                title=f"JM{intent.jm_id}",
                source="jmcomic",
                url=f"jmcomic://album/{intent.jm_id}",
            )
        if intent.url:
            return SearchEntry(
                index=0,
                title="doujin",
                source=source_from_url(intent.url),
                url=intent.url,
            )
        if not intent.index:
            return None
        quoted = await self._entry_from_quoted_forward(event, intent.index)
        recent = self._recent_search_entry(event, intent.index)
        if quoted and recent and not quoted.alternatives and recent.alternatives:
            quoted.alternatives = list(recent.alternatives)
        return quoted or recent

    async def _has_download_context(
        self,
        event: AstrMessageEvent,
        intent: DownloadIntent,
    ) -> bool:
        if intent.url or intent.jm_id:
            return True
        if not intent.index:
            return False
        quoted = await self._entry_from_quoted_forward(event, intent.index)
        if quoted:
            return True
        return self._recent_search_entry(event, intent.index) is not None

    async def _do_download(self, event: AstrMessageEvent, intent: DownloadIntent):
        output_fmt = self._output_format()
        if output_fmt != OUTPUT_FORMAT_MERGE and event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("本插件的文件发送仅支持 QQ (aiocqhttp)。").stop_event()
            return
        if not self._allow_download(event):
            yield event.plain_result("当前会话已禁用搜本子下载功能。").stop_event()
            return

        try:
            target = await self._resolve_download_target(event, intent)
        except Exception as exc:
            logger.exception("解析引用的搜索结果失败")
            yield event.plain_result(f"解析引用消息失败: {exc}").stop_event()
            return

        if not target or not target.url:
            yield event.plain_result(
                "没有找到要下载的条目。请先 /搜本子，或引用搜索结果回复「下载第1个」。"
            ).stop_event()
            return

        candidate_urls: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        if is_supported_url(target.url):
            candidate_urls.append((target.source or source_from_url(target.url), target.url))
            seen_urls.add(target.url)
        for alt_src, alt_url in target.alternatives:
            if alt_url and alt_url not in seen_urls and is_supported_url(alt_url):
                candidate_urls.append((alt_src or source_from_url(alt_url), alt_url))
                seen_urls.add(alt_url)

        if not candidate_urls:
            yield event.plain_result(f"暂不支持这个链接: {target.url}").stop_event()
            return

        if not self._claim_download_once(event, intent, target):
            yield event.plain_result("已跳过重复下载：这条消息的下载任务正在处理或已经处理过。").stop_event()
            return

        async def _try_sources(fetcher, action_label: str):
            last_error: Exception | None = None
            for cand_source, cand_url in candidate_urls:
                try:
                    return await fetcher(cand_source, cand_url), None
                except asyncio.TimeoutError as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"[搜本子下载] {action_label}失败 ({cand_source} / {cand_url}): {exc}")
            return None, last_error

        if output_fmt == OUTPUT_FORMAT_MERGE:
            logger.info(
                f"[搜本子下载] 命中合并转发目标: 标题={target.title}, 来源={candidate_urls[0][0]}, url={candidate_urls[0][1]}"
            )
            yield event.plain_result(f"开始获取合并转发内容: {target.title}")
            nsfw_policy = self._nsfw_policy(event)
            merged_media, last_exc = await _try_sources(
                lambda cand_source, cand_url: parse_media_target(
                    cand_url,
                    timeout=self._download_timeout(),
                    concurrency=self._download_concurrency(),
                    ehentai_cookie=self._ehentai_cookie(),
                    exhentai_cookie=self._exhentai_cookie(),
                    prefer_archive=False,
                    proxy=self._proxy_for("download", cand_source),
                    output_format=OUTPUT_FORMAT_MERGE,
                    preview_count=self._preview_count(),
                    max_pages=self._max_pages(),
                    nsfw_policy=nsfw_policy,
                    iwara_cookie=self._iwara_cookie(),
                    video_quality=self._iwara_video_quality(),
                    iwara_max_size_mb=self._iwara_max_size_mb(),
                    download_gallery_file=False,
                ),
                "尝试合并转发源",
            )
            if merged_media is None:
                if isinstance(last_exc, asyncio.TimeoutError):
                    yield event.plain_result("获取合并转发超时，请稍后重试或调大 download_timeout。").stop_event()
                else:
                    logger.exception("本子合并转发获取失败")
                    yield event.plain_result(f"获取合并转发失败: {last_exc}").stop_event()
                return

            async for res in self._deliver_parsed_media(
                event,
                merged_media,
                target.url,
                nsfw_policy=nsfw_policy,
                allow_file=False,
                silent_if_ignored=False,
                stop_event_on_delivery=True,
            ):
                yield res
            return

        logger.info(
            f"[搜本子下载] 命中下载目标: 标题={target.title}, 来源={candidate_urls[0][0]}, url={candidate_urls[0][1]}"
        )
        yield event.plain_result(f"开始下载: {target.title}")

        dl_format = OUTPUT_FORMAT_PDF if output_fmt == OUTPUT_FORMAT_PDF else OUTPUT_FORMAT_ZIP
        downloaded, last_exc = await _try_sources(
            lambda cand_source, cand_url: download_gallery(
                cand_url,
                timeout=self._download_timeout(),
                concurrency=self._download_concurrency(),
                ehentai_cookie=self._ehentai_cookie(),
                exhentai_cookie=self._exhentai_cookie(),
                prefer_archive=self._prefer_archive_download(),
                proxy=self._proxy_for("download", cand_source),
                output_format=dl_format,
                max_pages=self._max_pages(),
                iwara_cookie=self._iwara_cookie(),
                video_quality=self._iwara_video_quality(),
                iwara_max_size_mb=self._iwara_max_size_mb(),
            ),
            "尝试下载源",
        )

        if downloaded is None:
            if isinstance(last_exc, asyncio.TimeoutError):
                yield event.plain_result("下载超时，请稍后重试或调大 download_timeout。").stop_event()
            else:
                logger.exception("本子下载失败")
                yield event.plain_result(f"下载失败: {last_exc}").stop_event()
            return

        try:
            file_name, send_path = _prepare_send_file(downloaded.zip_path, downloaded.title)
        except Exception as exc:
            logger.exception("下载文件校验失败")
            yield event.plain_result(f"下载完成，但文件校验失败: {exc}").stop_event()
            return
        finally:
            # 同上：延迟清理，避免工具调用路径下发送前文件已消失。
            self._defer_cleanup(downloaded.zip_path)

        try:
            sent, mode = await self._send_download_file(event, file_name, send_path)
        except Exception as exc:
            logger.exception("发送下载文件失败")
            yield event.plain_result(
                f"下载完成，但文件发送失败: {exc}\n"
                f"本地文件保留在: {send_path}"
            ).stop_event()
            return

        if sent:
            self._cleanup_download_files()
            logger.info(f"[搜本子下载] 文件发送完成: {file_name}, 方式={mode}")
            yield event.plain_result("下载完成，文件已发送。").stop_event()

    def _parse_session_allowed(self, event: AstrMessageEvent) -> bool:
        session_key = self._session_key(event)
        group_id = str(event.get_group_id() or "").strip()
        ids = {x for x in (session_key, group_id, str(event.get_session_id() or "").strip()) if x}
        for candidate in ids:
            if candidate in self._session_parse_overrides:
                return self._session_parse_overrides[candidate]

        if not self._auto_parse_enabled():
            return False

        blacklist = self._string_set_config("parse_blacklist")
        if ids & blacklist:
            return False

        whitelist = self._string_set_config("parse_whitelist")
        if whitelist and not (ids & whitelist):
            return False
        return True

    def _is_debounced_parse(self, event: AstrMessageEvent, target: str) -> bool:
        ttl = self._parse_debounce_seconds()
        if ttl <= 0:
            return False
        now = time.time()
        expired = [k for k, ts in self._parse_debounce.items() if ts < now]
        for k in expired:
            self._parse_debounce.pop(k, None)
        key = f"{self._session_key(event)}|{target}"
        if key in self._parse_debounce:
            return True
        self._parse_debounce[key] = now + ttl
        return False

    def _extract_targets_from_event_sync(self, event: AstrMessageEvent, text: str) -> list[str]:
        targets = extract_parse_targets(text)
        seen = set(targets)
        json_cls = getattr(Comp, "Json", None)
        for comp in _extract_components_from_event(event):
            raw_json = None
            if _safe_isinstance(comp, json_cls):
                raw_json = getattr(comp, "data", None)
            elif isinstance(comp, dict) and comp.get("type") == "json":
                raw_json = (comp.get("data") or {}).get("data") if isinstance(comp.get("data"), dict) else comp.get("data")
            if raw_json is not None:
                found = extract_json_url(raw_json)
                if found and found not in seen:
                    seen.add(found)
                    targets.append(found)
        return targets

    async def _extract_targets_with_reply(self, event: AstrMessageEvent, text: str) -> list[str]:
        targets = self._extract_targets_from_event_sync(event, text)
        if targets or not self._parse_enable_reply():
            return targets
        seen: set[str] = set()
        combined: list[str] = []
        for q_text in await self._collect_quoted_texts(event):
            for t in extract_parse_targets(q_text):
                if t not in seen:
                    seen.add(t)
                    combined.append(t)
        return combined

    def _format_parse_text_summary(self, media: ParsedMedia) -> str:
        lines = [f"[{media.platform.upper()}] {media.title}"]
        if media.author:
            lines.append(f"作者: {media.author}")
        if media.extra_info:
            lines.append(media.extra_info)
        if media.summary:
            summary_short = media.summary.strip().replace("\n", " ")
            if len(summary_short) > 120:
                summary_short = summary_short[:117] + "..."
            lines.append(f"简介: {summary_short}")
        lines.append(f"链接: {media.url}")
        if media.skipped_download_reason:
            lines.append(f"说明: {media.skipped_download_reason}")
        return "\n".join(lines)

    async def _deliver_parsed_media(
        self,
        event: AstrMessageEvent,
        media: ParsedMedia,
        target_label: str,
        *,
        nsfw_policy: str,
        allow_file: bool,
        silent_if_ignored: bool = True,
        stop_event_on_delivery: bool = False,
    ):
        """发送 ParsedMedia（卡片 / 合并转发预览 / 视频 / ZIP / PDF）并清理临时文件。"""
        def _maybe_stop(result):
            if stop_event_on_delivery and hasattr(result, "stop_event"):
                return result.stop_event()
            return result

        card_path: Path | None = None
        cover_tmp_path: Path | None = None
        try:
            if media.ignored_reason:
                if not silent_if_ignored:
                    yield _maybe_stop(event.plain_result(media.ignored_reason))
                return

            if self._render_parse_card():
                card_path = render_info_card(
                    media,
                    nsfw_policy=nsfw_policy,
                    font_path=self._card_font_path(),
                )

            summary_text = self._format_parse_text_summary(media)
            if card_path is None:
                safe_cover = apply_nsfw_to_cover(
                    media.cover_bytes,
                    is_nsfw=media.is_nsfw,
                    nsfw_policy=nsfw_policy,
                )
                if safe_cover:
                    cover_tmp_path = temp_root() / f"cover_{uuid.uuid4().hex[:8]}.jpg"
                    cover_tmp_path.write_bytes(safe_cover)

            # Merge 转发模式（或仅预览 / 无文件）
            if media.image_paths and not media.file_path and not media.video_path and event.get_platform_name() == "aiocqhttp":
                node_uin = self._node_uin(event)
                intro_content: list[Any] = []
                if card_path:
                    intro_content.append(Comp.Image.fromFileSystem(str(card_path)))
                elif cover_tmp_path:
                    intro_content.append(Comp.Image.fromFileSystem(str(cover_tmp_path)))
                if media.skipped_download_reason or card_path is None:
                    intro_content.append(Comp.Plain(summary_text))
                nodes = [Comp.Node(uin=node_uin, name=self._node_name(), content=intro_content)]
                for img_p in media.image_paths:
                    nodes.append(
                        Comp.Node(
                            uin=node_uin,
                            name=self._node_name(),
                            content=[Comp.Image.fromFileSystem(str(img_p))],
                        )
                    )
                yield _maybe_stop(event.chain_result([Comp.Nodes(nodes)]))
                return

            # 卡片 / 文本摘要
            if card_path:
                card_chain: list[Any] = [Comp.Image.fromFileSystem(str(card_path))]
                if media.skipped_download_reason:
                    card_chain.append(Comp.Plain(f"\n{media.skipped_download_reason}"))
                yield _maybe_stop(event.chain_result(card_chain))
            else:
                fallback_chain: list[Any] = []
                if cover_tmp_path:
                    fallback_chain.append(Comp.Image.fromFileSystem(str(cover_tmp_path)))
                fallback_chain.append(Comp.Plain(summary_text))
                yield _maybe_stop(event.chain_result(fallback_chain))

            # 发送 Iwara 视频
            if media.video_path and Path(media.video_path).exists():
                video_cls = getattr(Comp, "Video", None)
                sent_video = False
                if video_cls and hasattr(video_cls, "fromFileSystem"):
                    try:
                        yield _maybe_stop(event.chain_result([video_cls.fromFileSystem(media.video_path)]))
                        sent_video = True
                    except Exception as exc:
                        logger.warning(f"直接发送视频段失败，回退群/私聊文件上传: {exc}")
                if not sent_video and event.get_platform_name() == "aiocqhttp" and allow_file:
                    v_name, v_send = _prepare_send_file(media.video_path, media.title)
                    await self._send_download_file(event, v_name, v_send)
                return

            # 发送图包 ZIP / PDF 文件
            if media.file_path and Path(media.file_path).exists() and event.get_platform_name() == "aiocqhttp":
                f_name, f_send = _prepare_send_file(media.file_path, media.title)
                await self._send_download_file(event, f_name, f_send)
        except Exception as exc:
            logger.exception(f"发送解析结果失败: {target_label}")
            yield _maybe_stop(event.plain_result(f"解析完成，但结果发送失败: {exc}"))
        finally:
            # 这里必须延迟清理：LLM 工具路径（AstrBot call_local_llm_tool）会先把
            # 异步生成器整体消费完，再在工具返回后由 event.send(...) 读取图片文件。
            # 若在 yield 之后立刻删除，卡片图/合并转发预览图就会变成
            # "image file URI does not point to an existing file"。
            # 纯指令路径由 RespondStage 边 yield 边发图，同样兼容延迟清理。
            self._defer_cleanup(
                card_path,
                cover_tmp_path,
                media.file_path,
                media.video_path,
                media.work_dir,
            )

    async def _parse_and_send(
        self,
        event: AstrMessageEvent,
        targets: list[str],
        *,
        silent_if_ignored: bool = True,
        require_download_permission_for_files: bool = True,
    ):
        output_fmt = self._output_format()
        nsfw_policy = self._nsfw_policy(event)
        allow_file = not require_download_permission_for_files or self._allow_download(event)
        download_file_flag = (output_fmt in {OUTPUT_FORMAT_ZIP, OUTPUT_FORMAT_PDF}) and allow_file
        effective_fmt = output_fmt if download_file_flag or output_fmt == OUTPUT_FORMAT_MERGE else OUTPUT_FORMAT_MERGE

        for target in targets[:3]:
            source = source_from_url(target)

            try:
                media = await parse_media_target(
                    target,
                    timeout=self._download_timeout(),
                    concurrency=self._download_concurrency(),
                    ehentai_cookie=self._ehentai_cookie(),
                    exhentai_cookie=self._exhentai_cookie(),
                    prefer_archive=self._prefer_archive_download(),
                    proxy=self._proxy_for("download", source),
                    output_format=effective_fmt,
                    preview_count=self._preview_count(),
                    max_pages=self._max_pages(),
                    nsfw_policy=nsfw_policy,
                    iwara_cookie=self._iwara_cookie(),
                    video_quality=self._iwara_video_quality(),
                    iwara_max_size_mb=self._iwara_max_size_mb(),
                    download_gallery_file=download_file_flag,
                )
            except Exception as exc:
                logger.exception(f"解析目标失败: {target}")
                if not silent_if_ignored:
                    yield event.plain_result(f"解析失败 ({target}): {exc}")
                continue

            async for res in self._deliver_parsed_media(
                event,
                media,
                target,
                nsfw_policy=nsfw_policy,
                allow_file=allow_file,
                silent_if_ignored=silent_if_ignored,
            ):
                yield res

    async def _do_search(
        self,
        event: AstrMessageEvent,
        image_url: Optional[str],
        send_start_notice: bool = True,
        stop_agent_after_result: bool = False,
    ):
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("本插件的合并转发仅支持 QQ (aiocqhttp)。")
            return

        image_url = (
            image_url
            or await _resolve_image_url_async(event)
            or self._recent_image_url(event)
        )
        if not image_url:
            yield event.plain_result(
                "请发送或回复一张图片后再使用 /搜本子 (也可在同一条消息里附带图片)。"
            )
            return

        if send_start_notice:
            yield event.plain_result("好的，我去找找这个本子")

        try:
            search_proxy = self._proxy_for("search", PROXY_SOURCE_SOUTUBOT)
            img_bytes = await fetch_image_bytes(image_url, self._timeout(), proxy=search_proxy)
        except Exception as e:
            logger.exception("下载图片失败")
            yield event.plain_result(f"图片下载失败: {e}")
            return

        try:
            payload = await search_soutubot(
                img_bytes,
                self._factor(),
                self._timeout(),
                proxy=self._proxy_for("search", PROXY_SOURCE_SOUTUBOT),
            )
        except asyncio.TimeoutError:
            yield event.plain_result("soutubot 请求超时，请稍后重试。")
            return
        except Exception as e:
            logger.exception("soutubot 搜索失败")
            yield event.plain_result(
                f"搜索失败: {e}\n"
                "提示: soutubot 位于 Cloudflare 后方，机房 IP 常被挑战页拦截，"
                "建议为搜图单独配置代理（proxy_policy=search_only）。"
            )
            return

        data = payload.get("data") or []
        if not data:
            yield event.plain_result("没有搜到任何结果。")
            return

        entries = [
            search_entry_from_item(idx, item)
            for idx, item in enumerate(data[: self._max_results()], 1)
        ]
        self._remember_search(event, entries)
        nsfw_policy = self._nsfw_policy(event)
        blurred_paths: dict[int, Path] = {}
        try:
            if self._send_preview_image(event) and nsfw_policy == NSFW_POLICY_BLUR:
                blurred_paths = await self._prepare_blurred_search_previews(entries)
            nodes = self._build_forward_nodes(
                event,
                entries,
                nsfw_policy=nsfw_policy,
                blurred_paths=blurred_paths,
            )
            yield event.chain_result([Comp.Nodes(nodes)])
            if stop_agent_after_result:
                event.set_extra("agent_stop_requested", True)
        finally:
            # 模糊预览图写在 Component 里，发送时才真正读盘，因此同样延迟清理。
            self._defer_cleanup(*blurred_paths.values())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cache_recent_image(self, event: AstrMessageEvent):
        self._remember_image(event)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_download_text(self, event: AstrMessageEvent):
        if self._download_event_handled(event) or self._parse_event_handled(event):
            return
        text = _event_text(event)
        intent = parse_download_intent(text)
        if not intent:
            return
        if not text.lstrip().startswith("/") and not await self._has_download_context(event, intent):
            return
        self._mark_download_event_handled(event)
        async for result in self._do_download(event, intent):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_auto_parse(self, event: AstrMessageEvent):
        if self._download_event_handled(event) or self._parse_event_handled(event):
            return
        text = _event_text(event)
        stripped = text.lstrip()
        if any(stripped.lower().startswith(p.lower()) for p in _RESERVED_COMMAND_PREFIXES):
            return
        if stripped in {"开启解析", "关闭解析"}:
            return
        # 若消息是明确的“下载”指令，交由 catch_download_text 处理，避免冲突
        if parse_download_intent(text):
            return
        if not self._parse_session_allowed(event):
            return

        targets = self._extract_targets_from_event_sync(event, text)
        if not targets:
            return

        fresh_targets = [t for t in targets if not self._is_debounced_parse(event, t)]
        if not fresh_targets:
            return

        if (
            self._parse_arbiter_enabled()
            and event.get_platform_name() == "aiocqhttp"
            and event.get_group_id()
        ):
            mid_raw = self._event_message_id(event)
            self_id_raw = getattr(getattr(event, "message_obj", None), "self_id", None)
            msg_time_raw = getattr(getattr(event, "message_obj", None), "timestamp", None) or int(time.time())
            if mid_raw.isdigit() and str(self_id_raw or "").isdigit():
                ctx = ArbiterContext(
                    message_id=int(mid_raw),
                    msg_time=int(msg_time_raw),
                    self_id=int(self_id_raw),
                )
                won = await self._arbiter.compete(event, ctx)
                if not won:
                    return

        if self._download_event_handled(event) or self._parse_event_handled(event):
            return
        self._mark_parse_event_handled(event)
        async for r in self._parse_and_send(event, fresh_targets, silent_if_ignored=True):
            yield r

    @filter.command("解析", alias={"jx", "parse"})
    async def cmd_parse(self, event: AstrMessageEvent):
        """解析 JM / E-Hentai / ExHentai / nHentai / Iwara / Panda 链接或 JM 号。"""
        self._mark_parse_event_handled(event)
        text = _event_text(event)
        targets = await self._extract_targets_with_reply(event, text)
        if not targets:
            yield event.plain_result(
                "请附带或引用包含 JM号 / JM / E-Hentai / ExHentai / nHentai / Iwara / Panda 链接的消息后再使用 /解析。"
            )
            return
        async for r in self._parse_and_send(
            event,
            targets,
            silent_if_ignored=False,
            require_download_permission_for_files=False,
        ):
            yield r

    @filter.command("开启解析")
    async def cmd_enable_parse(self, event: AstrMessageEvent):
        """在当前会话开启自动链接解析。"""
        self._mark_parse_event_handled(event)
        key = str(event.get_group_id() or "").strip() or self._session_key(event)
        self._session_parse_overrides[key] = True
        yield event.plain_result("已在当前会话开启链接自动解析。")

    @filter.command("关闭解析")
    async def cmd_disable_parse(self, event: AstrMessageEvent):
        """在当前会话关闭自动链接解析。"""
        self._mark_parse_event_handled(event)
        key = str(event.get_group_id() or "").strip() or self._session_key(event)
        self._session_parse_overrides[key] = False
        yield event.plain_result("已在当前会话关闭链接自动解析。")

    @filter.command("搜本子", alias={"搜本", "soutu"})
    async def cmd_search(self, event: AstrMessageEvent):
        """以图搜本子 (soutubot.moe)。请附带或回复一张图片。"""
        async for r in self._do_search(event, None):
            yield r

    @filter.llm_tool(name="search_doujin_by_image")
    async def llm_search(self, event: AstrMessageEvent, image_url: str = ""):
        """通过 soutubot.moe 以图搜本子 (漫画/同人志)。当用户想搜索某张图片出自哪个本子时调用。

        Args:
            image_url(string): 要搜索的图片 URL；留空则自动从用户最近一条含图片的消息中提取。
        """
        async for r in self._do_search(
            event,
            image_url or None,
            send_start_notice=False,
            stop_agent_after_result=True,
        ):
            yield r

    @filter.llm_tool(name="download_doujin")
    async def llm_download(
        self,
        event: AstrMessageEvent,
        url: str = "",
        index: int = 0,
        jm_id: str = "",
    ):
        """下载本子并发送 ZIP/PDF 文件。可用于用户要求下载本子、下载某个搜索结果、下载 nHentai/E-Hentai/ExHentai/Panda/JMComic 链接时。

        Args:
            url(string): 要下载的本子链接，支持 nHentai、E-Hentai、ExHentai、Panda、JMComic 链接；没有链接时留空。
            index(number): 要下载最近一次搜本子结果中的第几个；没有明确序号时填 0。
            jm_id(string): JMComic 本子 ID，例如用户说 JM123456 时填 123456；没有 JM 号时留空。
        """
        if self._download_event_handled(event):
            yield event.plain_result("已跳过重复下载：这条消息已经由下载指令处理。").stop_event()
            return

        intent: DownloadIntent | None = None
        url = (url or "").strip()
        jm_id = (jm_id or "").strip()
        if jm_id:
            intent = DownloadIntent(jm_id=jm_id)
        elif url:
            parsed = parse_download_intent(f"下载 {url}")
            intent = parsed or DownloadIntent(url=url)
        elif index and index > 0:
            intent = DownloadIntent(index=index)

        if not intent:
            yield event.plain_result("请提供要下载的链接、JM 号，或先搜本子后指定第几个。")
            return

        async for r in self._do_download(event, intent):
            yield r

    @filter.llm_tool(name="parse_media_url")
    async def llm_parse_media(self, event: AstrMessageEvent, target: str):
        """解析 JM / E-Hentai / ExHentai / nHentai / Iwara / Panda 链接或 JM 号，生成元信息卡片并按配置发送预览、视频或文件。

        Args:
            target(string): 目标链接或 JM 编号（如 https://www.iwara.tv/video/xxx 或 JM123456）。
        """
        if self._parse_event_handled(event) or self._download_event_handled(event):
            return
        targets = extract_parse_targets(target)
        if not targets and is_supported_url(target.strip()):
            targets = [target.strip()]
        if not targets:
            yield event.plain_result(f"无法识别受支持的解析目标: {target}")
            return
        self._mark_parse_event_handled(event)
        async for r in self._parse_and_send(event, targets, silent_if_ignored=False):
            yield r

    async def terminate(self):
        task = self._cleanup_task
        self._cleanup_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("停止搜本子临时文件清理任务失败", exc_info=True)
        # 插件卸载时立即清理全部延迟队列
        self._deferred_cleanup = {key: 0.0 for key in self._deferred_cleanup}
        self._sweep_deferred_cleanup()
