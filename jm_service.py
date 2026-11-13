from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import inspect
from io import BytesIO
from pathlib import Path
import re
from typing import Any
import uuid

from PIL import Image

from .models import ParsedMedia
from .packaging import temp_root, verify_image_bytes
from .parsing import extract_jm_id_from_url

JM_URL_ALBUM_RE = re.compile(r"/album/(\d+)", re.I)
JM_URL_PHOTO_RE = re.compile(r"/photo/(\d+)", re.I)
JM_QUERY_ID_RE = re.compile(r"[?&]id=(\d+)", re.I)
JM_PLAIN_ID_RE = re.compile(r"^(?:jm)?(\d{3,})$", re.I)

_BOM_BYTES = b"\xef\xbb\xbf"


@dataclass(slots=True)
class JmDownloadResult:
    media: ParsedMedia
    image_paths: list[Path]


def parse_jm_target(target: str) -> tuple[str, str] | None:
    """Resolve a JMComic URL or ID into ``("album" | "photo", numeric_id)``."""
    raw = str(target or "").strip()
    if not raw:
        return None
    if m := JM_URL_PHOTO_RE.search(raw):
        return ("photo", m.group(1))
    if m := JM_URL_ALBUM_RE.search(raw):
        return ("album", m.group(1))
    if m := JM_QUERY_ID_RE.search(raw):
        return ("album", m.group(1))
    if m := JM_PLAIN_ID_RE.match(raw):
        return ("album", m.group(1))
    jm_id = extract_jm_id_from_url(raw)
    if jm_id:
        return ("album", jm_id)
    return None


def canonical_jm_url(kind: str, jm_id: str) -> str:
    return f"https://18comic.vip/{kind}/{jm_id}"


def _strip_bom_on_response(resp: Any) -> None:
    """Strip leading UTF-8 BOM from JM response payloads before JSON decoding."""
    targets = [resp]
    inner = getattr(resp, "resp", None)
    if inner is not None and inner is not resp:
        targets.append(inner)

    for obj in targets:
        raw_content = getattr(obj, "_content", None)
        if isinstance(raw_content, (bytes, bytearray)):
            data = bytes(raw_content)
            if data.startswith(_BOM_BYTES):
                while data.startswith(_BOM_BYTES):
                    data = data[len(_BOM_BYTES) :]
                try:
                    obj._content = data
                except (AttributeError, TypeError):
                    pass

        try:
            content = getattr(obj, "content", None)
        except Exception:
            content = None
        if isinstance(content, (bytes, bytearray)) and bytes(content).startswith(_BOM_BYTES):
            # curl_cffi / requests respect utf-8-sig when decoding `.text`
            try:
                if hasattr(obj, "encoding"):
                    obj.encoding = "utf-8-sig"
            except (AttributeError, TypeError):
                pass

        raw_text = getattr(obj, "_text", None)
        if isinstance(raw_text, str) and raw_text.startswith("\ufeff"):
            try:
                obj._text = raw_text.lstrip("\ufeff")
            except (AttributeError, TypeError):
                pass


def install_jmcomic_bom_tolerance() -> bool:
    """Patch ``AbstractJmClient.raise_if_resp_should_retry`` once to tolerate UTF-8 BOM."""
    try:
        from jmcomic.jm_client_interface import AbstractJmClient
    except ImportError:
        return False

    if getattr(AbstractJmClient, "_searchmanga_bom_patched", False):
        return True

    orig = AbstractJmClient.raise_if_resp_should_retry

    def _patched_raise_if_resp_should_retry(self: Any, resp: Any, *args: Any, **kwargs: Any) -> Any:
        _strip_bom_on_response(resp)
        return orig(self, resp, *args, **kwargs)

    AbstractJmClient.raise_if_resp_should_retry = _patched_raise_if_resp_should_retry  # type: ignore[method-assign]
    AbstractJmClient._searchmanga_bom_patched = True  # type: ignore[attr-defined]
    return True


def _build_jm_option(
    work_dir: Path,
    *,
    timeout: int,
    concurrency: int,
    proxy: str | None = None,
) -> Any:
    import jmcomic

    if hasattr(jmcomic, "disable_jm_log"):
        try:
            jmcomic.disable_jm_log()
        except Exception:
            pass

    install_jmcomic_bom_tolerance()
    option = jmcomic.JmOption.default()
    option.dir_rule.base_dir = str(work_dir)
    option.client.impl = "api"
    option.client.retry_times = max(3, int(getattr(option.client, "retry_times", 3) or 3))

    meta_data = getattr(option.client.postman, "meta_data", None)
    if not isinstance(meta_data, dict):
        meta_data = {}
        option.client.postman.meta_data = meta_data

    meta_data["timeout"] = max(10.0, float(timeout))
    if proxy:
        meta_data["proxies"] = {"http": proxy, "https": proxy, "all": proxy}

    if hasattr(option, "download") and hasattr(option.download, "threading"):
        option.download.threading.image = max(1, min(int(concurrency), 16))
        option.download.threading.photo = max(1, min(int(concurrency), 4))
    return option


def _safe_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _resolve_album_cover_urls(album_id: str, client: Any, first_photo: Any = None) -> list[str]:
    urls: list[str] = []
    seen_domains: set[str] = set()

    def _add_domain(domain: str | None) -> None:
        d = str(domain or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
        if d and d not in seen_domains:
            seen_domains.add(d)
            urls.append(f"https://{d}/media/albums/{album_id}_3x4.jpg")

    if first_photo is not None:
        _add_domain(getattr(first_photo, "data_original_domain", None))

    try:
        from jmcomic import JmModuleConfig

        for d in getattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", []) or []:
            _add_domain(d)
    except Exception:
        pass

    if hasattr(client, "get_domain_list"):
        try:
            for d in client.get_domain_list() or []:
                _add_domain(d)
        except Exception:
            pass

    _add_domain("cdn-msp.jmapiproxy3.cc")
    _add_domain("cdn-msp.jmapiproxy2.cc")
    return urls


def _download_cover_bytes(
    album_id: str,
    client: Any,
    first_photo: Any = None,
) -> bytes | None:
    for url in _resolve_album_cover_urls(album_id, client, first_photo)[:4]:
        try:
            resp = client.get_jm_image(url)
            content = bytes(getattr(resp, "content", b"") or b"")
            if content:
                verify_image_bytes(content)
                return content
        except Exception:
            continue
    return None


def _decode_and_save_bytes(raw_bytes: bytes, scramble_id: Any, img_url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    verify_image_bytes(raw_bytes)

    num = 0
    try:
        from jmcomic import JmImageTool

        num = int(JmImageTool.get_num_by_url(scramble_id, img_url) or 0)
    except Exception:
        num = 0

    with Image.open(BytesIO(raw_bytes)) as img_src:
        if num > 0:
            from jmcomic import JmImageTool

            JmImageTool.decode_and_save(num, img_src, str(dest))
            return dest

        out_img = img_src.convert("RGB") if img_src.mode not in ("RGB", "RGBA") else img_src
        if dest.suffix.lower() in (".jpg", ".jpeg") and out_img.mode == "RGBA":
            out_img = out_img.convert("RGB")
        out_img.save(dest)
    return dest


def _download_single_image(client: Any, img_detail: Any, dest: Path) -> Path:
    img_url = str(getattr(img_detail, "download_url", None) or getattr(img_detail, "img_url", "") or "")
    scramble_id = getattr(img_detail, "scramble_id", 0)

    # Prefer raw fetch + explicit JmImageTool decode so multi-chapter ordering and validation stay unified
    if img_url and hasattr(client, "get_jm_image"):
        try:
            resp = client.get_jm_image(img_url)
            raw = bytes(getattr(resp, "content", b"") or b"")
            if raw:
                return _decode_and_save_bytes(raw, scramble_id, img_url, dest)
        except Exception:
            if not hasattr(client, "download_by_image_detail"):
                raise

    if hasattr(client, "download_by_image_detail"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download_by_image_detail(img_detail, str(dest), decode_image=True)
        if dest.exists() and dest.stat().st_size > 0:
            verify_image_bytes(dest.read_bytes())
            return dest

    raise RuntimeError(f"JM 图片下载失败: {img_url or dest.name}")


def _extract_photo_sequence(client: Any, kind: str, jm_id: str) -> tuple[Any | None, list[Any]]:
    if kind == "photo":
        try:
            sig = inspect.signature(client.get_photo_detail)
            has_fetch_album = "fetch_album" in sig.parameters
        except (TypeError, ValueError):
            has_fetch_album = False
        photo = client.get_photo_detail(jm_id, False) if has_fetch_album else client.get_photo_detail(jm_id)
        album = getattr(photo, "from_album", None)
        if album is None:
            album_id = str(getattr(photo, "album_id", "") or jm_id)
            if album_id:
                try:
                    album = client.get_album_detail(album_id)
                except Exception:
                    album = None
        return album, [photo]

    album = client.get_album_detail(jm_id)
    photos: list[Any] = []
    episode_items = list(album) if album is not None else []
    for item in episode_items:
        pid = str(getattr(item, "photo_id", "") or getattr(item, "id", "") or "")
        if not pid:
            continue
        if getattr(item, "page_arr", None):
            photos.append(item)
        else:
            photos.append(client.get_photo_detail(pid))

    if not photos:
        # Fallback: some single-chapter albums use the album ID as photo ID
        photos.append(client.get_photo_detail(jm_id))
    return album, photos


def _run_jm_sync(
    kind: str,
    jm_id: str,
    work_dir: Path,
    *,
    timeout: int,
    concurrency: int,
    proxy: str | None,
    download_images: bool,
    preview_limit: int,
    max_pages: int,
) -> JmDownloadResult:
    option = _build_jm_option(work_dir, timeout=timeout, concurrency=concurrency, proxy=proxy)
    client = option.build_jm_client()
    album, photos = _extract_photo_sequence(client, kind, jm_id)

    album_id = str(getattr(album, "album_id", "") or getattr(photos[0], "album_id", "") or jm_id)
    title = str(
        getattr(album, "name", "")
        or getattr(album, "title", "")
        or getattr(photos[0], "name", "")
        or getattr(photos[0], "title", "")
        or f"JM{jm_id}"
    ).strip()

    authors = _safe_str_list(getattr(album, "authors", None)) or _safe_str_list(getattr(photos[0], "author", None))
    tags = _safe_str_list(getattr(album, "tags", None))
    works = _safe_str_list(getattr(album, "works", None))
    description = str(getattr(album, "description", "") or "").strip()

    total_pages = sum(len(list(photo)) for photo in photos)
    reported_pages = int(getattr(album, "page_count", 0) or 0) if album is not None else 0
    effective_pages = max(total_pages, reported_pages)

    cover_bytes = _download_cover_bytes(album_id, client, photos[0] if photos else None)

    skipped_reason = ""
    tasks: list[tuple[int, int, Any, Path]] = []
    if download_images:
        if max_pages > 0 and effective_pages > max_pages and kind == "album":
            skipped_reason = f"总页数 {effective_pages} 超过上限 {max_pages}，已跳过全本下载"
        else:
            for ep_idx, photo in enumerate(photos, start=1):
                pid = str(getattr(photo, "photo_id", "") or ep_idx)
                for page_idx, img_detail in enumerate(photo, start=1):
                    dest = work_dir / "pages" / f"{ep_idx:04d}_{pid}" / f"{page_idx:05d}.jpg"
                    tasks.append((ep_idx, page_idx, img_detail, dest))
    elif preview_limit > 0:
        for ep_idx, photo in enumerate(photos, start=1):
            pid = str(getattr(photo, "photo_id", "") or ep_idx)
            for page_idx, img_detail in enumerate(photo, start=1):
                if len(tasks) >= preview_limit:
                    break
                dest = work_dir / "pages" / f"{ep_idx:04d}_{pid}" / f"{page_idx:05d}.jpg"
                tasks.append((ep_idx, page_idx, img_detail, dest))
            if len(tasks) >= preview_limit:
                break

    ordered_results: dict[tuple[int, int], Path] = {}
    if tasks:
        worker_count = max(1, min(int(concurrency), len(tasks), 12))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(_download_single_image, client, img_detail, dest): (ep_idx, page_idx)
                for ep_idx, page_idx, img_detail, dest in tasks
            }
            for fut, key in future_map.items():
                ordered_results[key] = fut.result()

    image_paths = [ordered_results[k] for k in sorted(ordered_results)]
    if cover_bytes is None and image_paths:
        try:
            cover_bytes = image_paths[0].read_bytes()
        except Exception:
            cover_bytes = None

    chapter_note = f"共 {len(photos)} 话 / {effective_pages or len(image_paths)} 页"
    extra_parts = [f"ID: JM{jm_id}"]
    if album_id != jm_id:
        extra_parts.append(f"所属专辑: JM{album_id}")
    extra_parts.append(chapter_note)
    if works:
        extra_parts.append(f"原作: {', '.join(works[:4])}")
    if tags:
        extra_parts.append(f"标签: {', '.join(tags[:12])}")

    media = ParsedMedia(
        platform="jmcomic",
        title=title,
        url=canonical_jm_url(kind, jm_id),
        author=", ".join(authors),
        summary=description,
        extra_info="\n".join(extra_parts),
        cover_bytes=cover_bytes,
        image_paths=image_paths,
        page_count=effective_pages or len(image_paths),
        is_nsfw=True,
        skipped_download_reason=skipped_reason,
        work_dir=str(work_dir),
    )
    return JmDownloadResult(media=media, image_paths=image_paths)


async def fetch_jm_media(
    target: str,
    *,
    timeout: int = 120,
    concurrency: int = 8,
    proxy: str | None = None,
    download_images: bool = True,
    preview_limit: int = 0,
    max_pages: int = 0,
    work_dir: Path | None = None,
) -> JmDownloadResult:
    """Fetch JMComic metadata/cover and optionally download ordered decoded pages."""
    parsed = parse_jm_target(target)
    if parsed is None:
        raise ValueError(f"无法识别 JM 链接或编号: {target}")
    kind, jm_id = parsed

    if work_dir is None:
        work_dir = temp_root() / f"jm_{kind}_{jm_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    return await asyncio.to_thread(
        _run_jm_sync,
        kind,
        jm_id,
        work_dir,
        timeout=timeout,
        concurrency=concurrency,
        proxy=proxy,
        download_images=download_images,
        preview_limit=preview_limit,
        max_pages=max_pages,
    )
