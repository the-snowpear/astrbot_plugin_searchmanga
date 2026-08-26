import asyncio
import base64
import hashlib
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core import astrbot_config, file_token_service
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
import astrbot.api.message_components as Comp

from .downloaders import download_gallery
from .models import DownloadIntent, SearchEntry
from .onebot import (
    call_onebot_action,
    collect_forward_ids_from_components,
    get_forward_msg,
    iter_reply_components,
)
from .parsing import (
    extract_text_from_forward_payload,
    is_supported_url,
    parse_download_intent,
    parse_search_entries_from_text,
    sanitize_filename,
    search_entry_from_item,
    source_from_url,
)
from .soutubot import fetch_image_bytes, search_soutubot
from .constants import (
    PROXY_POLICY_ALL,
    PROXY_POLICY_DISABLED,
    PROXY_POLICY_DOWNLOAD_ONLY,
    PROXY_POLICY_SEARCH_ONLY,
)


RECENT_IMAGE_TTL = 600
RECENT_SEARCH_TTL = 1800
SEND_FILE_TTL = 3600
DOWNLOAD_DEDUP_MIN_TTL = 300
DOWNLOAD_DEDUP_GRACE = 120
DOWNLOAD_HANDLED_EXTRA = "searchmanga_download_handled"
DEFAULT_BASE64_FILE_LIMIT_MB = 48
DEFAULT_STREAM_CHUNK_KB = 256
STREAM_FILE_RETENTION_MS = 5 * 60 * 1000
ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
GROUP_SETTING_TEMPLATE_KEY = "group"


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


def _prepare_send_file(source_path: str, title: str) -> tuple[str, str]:
    source = Path(source_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"下载文件不存在: {source}")
    _assert_valid_zip(source)
    send_dir = Path(get_astrbot_temp_path()) / "searchmanga_downloads"
    send_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title, "doujin")
    file_name = f"{safe_title}.zip"
    unique_suffix = uuid.uuid4().hex[:8]
    send_path = send_dir / f"{safe_title}_{unique_suffix}.zip"
    if source.resolve() != send_path.resolve():
        shutil.copy2(source, send_path)
    _assert_valid_zip(send_path)
    return file_name, str(send_path.resolve())


async def _assert_download_url_returns_zip(url: str) -> None:
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=12)
    headers = {"Range": "bytes=0-511", "User-Agent": "AstrBot-SearchManga/0.2"}
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
    if not sample.startswith(ZIP_MAGIC_PREFIXES):
        preview = sample.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "文件回调地址返回的不是 ZIP 数据，已阻止发送，避免 NapCat 把错误页保存成 .zip。"
            f" status={status}, content-type={content_type or '(空)'}, preview={preview[:160]}"
        )


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


def _extract_image_url(event: AstrMessageEvent) -> Optional[str]:
    """Look at the current message chain first, then the Reply target's chain."""
    chain = getattr(event.message_obj, "message", None) or []
    for seg in chain:
        if isinstance(seg, Comp.Image):
            url = getattr(seg, "url", None) or getattr(seg, "file", None)
            if url and isinstance(url, str) and url.startswith("http"):
                return url
    for seg in chain:
        if isinstance(seg, Comp.Reply):
            reply_chain = getattr(seg, "chain", None) or []
            for s in reply_chain:
                if isinstance(s, Comp.Image):
                    url = getattr(s, "url", None) or getattr(s, "file", None)
                    if url and isinstance(url, str) and url.startswith("http"):
                        return url
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


@register(
    "astrbot_plugin_searchmanga",
    "the_snowpear",
    "通过 soutubot.moe 以图搜本子",
    "0.2.0",
)
class SearchMangaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._recent_images: dict[str, tuple[str, float]] = {}
        self._recent_searches: dict[str, tuple[list[SearchEntry], float]] = {}
        self._download_files: dict[str, tuple[str, float]] = {}
        self._download_claims: dict[str, float] = {}

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
            should_call_llm(True)

    def _download_event_handled(self, event: AstrMessageEvent) -> bool:
        get_extra = getattr(event, "get_extra", None)
        if not callable(get_extra):
            return False
        return bool(get_extra(DOWNLOAD_HANDLED_EXTRA, False))

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

    def _group_setting(self, event: AstrMessageEvent) -> dict | None:
        group_id = event.get_group_id()
        if not group_id:
            return None
        group_id = str(group_id).strip()
        settings = self.config.get("group_settings", []) or []
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
        url = str(self.config.get("proxy_url", "http://127.0.0.1:7897") or "").strip()
        if url and "://" not in url:
            url = f"http://{url}"
        return url

    def _proxy_for(self, phase: str) -> str | None:
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
        await _assert_download_url_returns_zip(probe_url)
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

    async def _upload_file_stream_to_onebot_legacy(
        self,
        event: AstrMessageEvent,
        file_path: str,
        file_name: str,
    ) -> str:
        path = Path(file_path)
        file_id = f"searchmanga_{uuid.uuid4().hex}"
        file_size = path.stat().st_size
        sha256 = hashlib.sha256()
        chunk_size = self._stream_chunk_bytes()
        offset = 0

        with path.open("rb") as fp:
            while True:
                chunk = fp.read(chunk_size)
                if not chunk:
                    break
                sha256.update(chunk)
                await call_onebot_action(
                    event,
                    "upload_file_stream",
                    file_id=file_id,
                    name=file_name,
                    total_size=file_size,
                    offset=offset,
                    chunk=_b64encode_chunk(chunk),
                )
                offset += len(chunk)

        result = await call_onebot_action(
            event,
            "upload_file_stream",
            file_id=file_id,
            name=file_name,
            total_size=file_size,
            offset=file_size,
            sha256=sha256.hexdigest(),
            ret_path=True,
            ret_path_with_upload_id=True,
            file_retention_time=STREAM_FILE_RETENTION_MS,
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"upload_file_stream 返回异常: {result!r}")
        stream_path = result.get("path") or result.get("file") or result.get("file_path")
        if not stream_path:
            raise RuntimeError(f"upload_file_stream 未返回可上传文件路径: {result!r}")
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
                logger.warning(f"[搜本子下载] base64 发送失败，改用 HTTP 回调: {exc}", exc_info=True)

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

    def _node_uin(self, event: AstrMessageEvent) -> int:
        self_id = getattr(event.message_obj, "self_id", None)
        try:
            if self_id:
                return int(self_id)
        except (TypeError, ValueError):
            logger.warning(f"无法解析机器人 self_id: {self_id}")
        return int(self.config.get("node_uin", 10000))

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", None) or event.get_session_id())

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
        self._recent_searches[self._session_key(event)] = (entries, time.time())

    def _recent_search_entry(self, event: AstrMessageEvent, index: int) -> SearchEntry | None:
        item = self._recent_searches.get(self._session_key(event))
        if not item:
            return None
        entries, saved_at = item
        if time.time() - saved_at > RECENT_SEARCH_TTL:
            self._recent_searches.pop(self._session_key(event), None)
            return None
        for entry in entries:
            if entry.index == index:
                return entry
        return None

    def _build_forward_nodes(self, event: AstrMessageEvent, entries: list[SearchEntry]) -> list[Comp.Node]:
        threshold = self._threshold()
        node_uin = self._node_uin(event)
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
            content: list = []
            if self._send_preview_image(event) and entry.preview_url:
                content.append(Comp.Image.fromURL(entry.preview_url))
            content.append(Comp.Plain(text))
            nodes.append(Comp.Node(uin=node_uin, name=self._node_name(), content=content))
        return nodes

    async def _entry_from_quoted_forward(
        self,
        event: AstrMessageEvent,
        index: int,
    ) -> SearchEntry | None:
        for reply in iter_reply_components(event):
            reply_text = getattr(reply, "message_str", None) or ""
            if reply_text:
                for entry in parse_search_entries_from_text(reply_text):
                    if entry.index == index:
                        return entry

            reply_chain = getattr(reply, "chain", None) or []
            plain_text = "\n".join(
                getattr(seg, "text", "")
                for seg in reply_chain
                if isinstance(seg, Comp.Plain)
            )
            for entry in parse_search_entries_from_text(plain_text):
                if entry.index == index:
                    return entry

            for forward_id in collect_forward_ids_from_components(reply_chain):
                payload = await get_forward_msg(event, forward_id)
                text = extract_text_from_forward_payload(payload)
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
        if quoted:
            return quoted
        return self._recent_search_entry(event, intent.index)

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
        if event.get_platform_name() != "aiocqhttp":
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

        if not is_supported_url(target.url):
            yield event.plain_result(f"暂不支持这个链接: {target.url}").stop_event()
            return

        if not self._claim_download_once(event, intent, target):
            yield event.plain_result("已跳过重复下载：这条消息的下载任务正在处理或已经处理过。").stop_event()
            return

        logger.info(f"[搜本子下载] 命中下载目标: 标题={target.title}, 来源={target.source}, url={target.url}")
        yield event.plain_result(f"开始下载: {target.title}")
        try:
            downloaded = await download_gallery(
                target.url,
                timeout=self._download_timeout(),
                concurrency=self._download_concurrency(),
                ehentai_cookie=self._ehentai_cookie(),
                exhentai_cookie=self._exhentai_cookie(),
                prefer_archive=self._prefer_archive_download(),
                proxy=self._proxy_for("download"),
            )
        except asyncio.TimeoutError:
            yield event.plain_result("下载超时，请稍后重试或调大 download_timeout。").stop_event()
            return
        except Exception as exc:
            logger.exception("本子下载失败")
            yield event.plain_result(f"下载失败: {exc}").stop_event()
            return

        try:
            file_name, send_path = _prepare_send_file(downloaded.zip_path, downloaded.title)
        except Exception as exc:
            logger.exception("下载 ZIP 校验失败")
            yield event.plain_result(f"下载完成，但 ZIP 校验失败: {exc}").stop_event()
            return
        if hasattr(event, "track_temporary_local_file"):
            event.track_temporary_local_file(downloaded.zip_path)

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

        image_url = image_url or _extract_image_url(event) or self._recent_image_url(event)
        if not image_url:
            yield event.plain_result(
                "请发送或回复一张图片后再使用 /搜本子 (也可在同一条消息里附带图片)。"
            )
            return

        if send_start_notice:
            yield event.plain_result("好的，我去找找这个本子")

        try:
            search_proxy = self._proxy_for("search")
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
                proxy=self._proxy_for("search"),
            )
        except asyncio.TimeoutError:
            yield event.plain_result("soutubot 请求超时，请稍后重试。")
            return
        except Exception as e:
            logger.exception("soutubot 搜索失败")
            yield event.plain_result(
                f"搜索失败: {e}\n提示: soutubot 对代理敏感，请确认机器人未走代理。"
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
        yield event.chain_result([Comp.Nodes(self._build_forward_nodes(event, entries))])
        if stop_agent_after_result:
            event.set_extra("agent_stop_requested", True)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cache_recent_image(self, event: AstrMessageEvent):
        self._remember_image(event)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_download_text(self, event: AstrMessageEvent):
        text = _event_text(event)
        intent = parse_download_intent(text)
        if not intent:
            return
        if not text.lstrip().startswith("/") and not await self._has_download_context(event, intent):
            return
        self._mark_download_event_handled(event)
        async for result in self._do_download(event, intent):
            yield result

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
        """下载本子并发送 zip 文件。可用于用户要求下载本子、下载某个搜索结果、下载 nHentai/E-Hentai/ExHentai/Panda/JMComic 链接时。

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

    async def terminate(self):
        pass
