from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse, urlunparse
import uuid

import aiohttp

from .constants import DEFAULT_HEADERS, NHENTAI_EXT
from .models import ParsedMedia
from .packaging import temp_root, verify_image_bytes
from .parsing import guess_ext

NHENTAI_GALLERY_RE = re.compile(
    r"https?://(?:www\.)?(nhentai\.(?:net|xxx|to))/g/(\d+)",
    re.I,
)
MEDIA_GALLERY_FALLBACK_RE = re.compile(r"galleries/(\d+)/(\d+)\.(jpg|jpeg|png|webp|gif)", re.I)
GALLERY_JSON_RE = re.compile(r"JSON\.parse\((['\"])(.+?)\1\)", re.S)


@dataclass(slots=True)
class NhDownloadResult:
    media: ParsedMedia
    image_paths: list[Path]


def nhentai_url_for_host(url: str, host: str) -> str:
    """Canonicalize an nHentai URL onto the specified host, dropping page numbers, queries, and fragments."""
    raw = str(url or "").strip()
    clean_host = (host or "nhentai.net").strip().removeprefix("https://").removeprefix("http://").strip("/")
    m = re.search(r"/g/(\d+)", raw, re.I)
    if m:
        return f"https://{clean_host}/g/{m.group(1)}/"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    return urlunparse(parsed._replace(scheme="https", netloc=clean_host, query="", fragment=""))


def parse_nhentai_target(url: str) -> tuple[str, str] | None:
    m = NHENTAI_GALLERY_RE.search(str(url or "").strip())
    if not m:
        return None
    return (m.group(1).lower(), m.group(2))


def _normalize_server_base(server: str) -> str:
    s = str(server or "").strip().rstrip("/")
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        return f"https://{s}"
    return s


async def _http_get_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    referer: str = "https://nhentai.net/",
    proxy: str | None = None,
    timeout: int = 30,
) -> tuple[int, bytes, str]:
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = referer
    async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
        raw = await resp.read()
        ct = resp.headers.get("Content-Type", "")
        if resp.status not in (403, 503):
            return resp.status, raw, ct

    # Cloudflare fallback via curl_cffi if available
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return resp.status, raw, ct  # type: ignore[misc]

    async with AsyncSession(impersonate="chrome120", timeout=timeout) as cf_session:
        cf_resp = await cf_session.get(url, headers=headers, proxy=proxy)
        cf_ct = cf_resp.headers.get("content-type", "") if hasattr(cf_resp, "headers") else ""
        return cf_resp.status_code, bytes(cf_resp.content or b""), str(cf_ct or "")


async def _fetch_cdn_servers(
    session: aiohttp.ClientSession,
    *,
    proxy: str | None,
    timeout: int,
) -> tuple[list[str], list[str]]:
    default_img = ["https://i1.nhentai.net", "https://i2.nhentai.net", "https://i3.nhentai.net"]
    default_thumb = ["https://t1.nhentai.net", "https://t2.nhentai.net"]
    try:
        status, raw, _ = await _http_get_bytes(
            session,
            "https://nhentai.net/api/v2/cdn",
            proxy=proxy,
            timeout=timeout,
        )
        if status < 400 and raw:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            img_servers = [_normalize_server_base(x) for x in data.get("image_servers") or []]
            thumb_servers = [_normalize_server_base(x) for x in data.get("thumb_servers") or []]
            img_servers = [x for x in img_servers if x] or default_img
            thumb_servers = [x for x in thumb_servers if x] or default_thumb
            return img_servers, thumb_servers
    except Exception:
        pass
    return default_img, default_thumb


def _extract_title(title_obj: Any, gid: str) -> str:
    if isinstance(title_obj, str):
        return title_obj.strip() or f"NH_{gid}"
    if isinstance(title_obj, dict):
        return str(
            title_obj.get("japanese")
            or title_obj.get("english")
            or title_obj.get("pretty")
            or f"NH_{gid}"
        ).strip()
    return f"NH_{gid}"


def _build_page_urls_from_gallery(
    gallery: dict[str, Any],
    image_servers: list[str],
    thumb_servers: list[str],
) -> tuple[list[str], str]:
    media_id = str(gallery.get("media_id") or "").strip()
    page_urls: list[str] = []

    pages = gallery.get("pages")
    if not isinstance(pages, list):
        images = gallery.get("images")
        pages = images.get("pages") if isinstance(images, dict) else []

    if isinstance(pages, list):
        for idx, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                continue
            server = image_servers[(idx - 1) % len(image_servers)]
            rel_path = str(page.get("path") or "").lstrip("/")
            if rel_path:
                page_urls.append(f"{server}/{rel_path}")
                continue
            ext = NHENTAI_EXT.get(str(page.get("t") or "j").lower(), "jpg")
            if media_id:
                page_urls.append(f"{server}/galleries/{media_id}/{idx}.{ext}")

    cover_url = ""
    cover = gallery.get("cover")
    if not isinstance(cover, dict):
        images = gallery.get("images")
        cover = images.get("cover") if isinstance(images, dict) else None
    t_server = thumb_servers[0]
    if isinstance(cover, dict):
        cover_path = str(cover.get("path") or "").lstrip("/")
        if cover_path:
            cover_url = f"{t_server}/{cover_path}"
        elif media_id:
            cext = NHENTAI_EXT.get(str(cover.get("t") or "j").lower(), "jpg")
            cover_url = f"{t_server}/galleries/{media_id}/cover.{cext}"
    return page_urls, cover_url


async def _fetch_gallery_payload(
    session: aiohttp.ClientSession,
    host: str,
    gid: str,
    *,
    proxy: str | None,
    timeout: int,
) -> tuple[dict[str, Any] | None, str]:
    # 1. Primary: nhentai.net API v2
    for api_url in (
        f"https://nhentai.net/api/v2/galleries/{gid}",
        f"https://nhentai.net/api/gallery/{gid}",
    ):
        try:
            status, raw, _ = await _http_get_bytes(session, api_url, proxy=proxy, timeout=timeout)
            if status < 400 and raw:
                data = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(data, dict) and not data.get("error"):
                    return data, "nhentai.net"
        except Exception:
            continue

    # 2. HTML fallback across nHentai mirrors
    hosts = [host] + [h for h in ("nhentai.net", "nhentai.xxx", "nhentai.to") if h != host]
    for mirror in hosts:
        page_url = f"https://{mirror}/g/{gid}/"
        try:
            status, raw, _ = await _http_get_bytes(
                session,
                page_url,
                referer=f"https://{mirror}/",
                proxy=proxy,
                timeout=timeout,
            )
            if status >= 400 or not raw:
                continue
            html = raw.decode("utf-8", errors="replace")
            for m in GALLERY_JSON_RE.finditer(html):
                try:
                    decoded = bytes(m.group(2), "utf-8").decode("unicode_escape")
                    parsed = json.loads(decoded)
                    if isinstance(parsed, dict) and ("pages" in parsed or "images" in parsed):
                        return parsed, mirror
                except Exception:
                    continue
            # Minimal HTML scrape fallback if JSON script wasn't found
            hits = MEDIA_GALLERY_FALLBACK_RE.findall(html)
            if hits:
                media_id = hits[0][0]
                ext_by_page = {int(p): ext for _, p, ext in hits}
                max_p = max(ext_by_page)
                pages = [{"t": ("p" if ext_by_page.get(i) == "png" else "w" if ext_by_page.get(i) == "webp" else "j")} for i in range(1, max_p + 1)]
                title_m = re.search(r"<title>(.+?)</title>", html, re.I | re.S)
                return {
                    "id": gid,
                    "media_id": media_id,
                    "title": {"pretty": title_m.group(1).strip() if title_m else f"NH_{gid}"},
                    "images": {"pages": pages},
                    "tags": [],
                }, mirror
        except Exception:
            continue

    return None, host


async def fetch_nh_media(
    target_url: str,
    *,
    timeout: int = 120,
    concurrency: int = 6,
    proxy: str | None = None,
    download_images: bool = True,
    preview_limit: int = 0,
    max_pages: int = 0,
    work_dir: Path | None = None,
) -> NhDownloadResult:
    """Fetch nHentai metadata/cover via API v2 (+v1/HTML fallback) and download ordered pages."""
    parsed = parse_nhentai_target(target_url)
    if parsed is None:
        raise ValueError(f"无法识别 nHentai 链接: {target_url}")
    req_host, gid = parsed

    if work_dir is None:
        work_dir = temp_root() / f"nh_{gid}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    client_timeout = aiohttp.ClientTimeout(total=max(20, int(timeout)))
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        gallery, resolved_host = await _fetch_gallery_payload(
            session,
            req_host,
            gid,
            proxy=proxy,
            timeout=timeout,
        )
        if not gallery:
            raise RuntimeError(f"未能获取 nHentai 画廊信息: {target_url}")

        image_servers, thumb_servers = await _fetch_cdn_servers(
            session,
            proxy=proxy,
            timeout=timeout,
        )
        page_urls, cover_url = _build_page_urls_from_gallery(gallery, image_servers, thumb_servers)
        total_pages = int(gallery.get("num_pages") or len(page_urls) or 0)
        title = _extract_title(gallery.get("title"), gid)

        artists: list[str] = []
        tags: list[str] = []
        for tag in gallery.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            tname = str(tag.get("name") or "").strip()
            ttype = str(tag.get("type") or "").strip().lower()
            if not tname:
                continue
            if ttype == "artist":
                artists.append(tname)
            elif ttype in ("tag", "parody", "character", "language"):
                tags.append(tname)

        cover_bytes: bytes | None = None
        if cover_url:
            try:
                c_status, c_raw, _ = await _http_get_bytes(
                    session,
                    cover_url,
                    referer=f"https://{resolved_host}/",
                    proxy=proxy,
                    timeout=timeout,
                )
                if c_status < 400 and c_raw:
                    verify_image_bytes(c_raw)
                    cover_bytes = c_raw
            except Exception:
                cover_bytes = None

        skipped_reason = ""
        target_urls: list[tuple[int, str]] = []
        if download_images:
            if max_pages > 0 and total_pages > max_pages:
                skipped_reason = f"总页数 {total_pages} 超过上限 {max_pages}，已跳过全本下载"
            else:
                target_urls = list(enumerate(page_urls, start=1))
        elif preview_limit > 0:
            target_urls = list(enumerate(page_urls[:preview_limit], start=1))

        image_paths: list[Path] = []
        if target_urls:
            pages_dir = work_dir / "pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            sem = asyncio.Semaphore(max(1, min(int(concurrency), 8)))

            async def _dl_page(p_idx: int, img_url: str) -> tuple[int, Path]:
                async with sem:
                    last_err: Exception | None = None
                    # Try primary server first, then rotate to other CDN servers if mirror fails
                    candidates = [img_url]
                    parsed_img = urlparse(img_url)
                    for alt_server in image_servers:
                        alt_host = urlparse(alt_server).netloc
                        if alt_host and alt_host != parsed_img.netloc:
                            candidates.append(urlunparse(parsed_img._replace(netloc=alt_host)))

                    for cand in candidates:
                        try:
                            st, raw, ct = await _http_get_bytes(
                                session,
                                cand,
                                referer=f"https://{resolved_host}/g/{gid}/{p_idx}/",
                                proxy=proxy,
                                timeout=timeout,
                            )
                            if st >= 400:
                                raise RuntimeError(f"HTTP {st}")
                            raw_ext = verify_image_bytes(raw) or guess_ext(cand, ct)
                            ext = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
                            out = pages_dir / f"{p_idx:05d}{ext}"
                            out.write_bytes(raw)
                            return p_idx, out
                        except Exception as exc:
                            last_err = exc
                            continue
                    raise RuntimeError(f"nHentai 第 {p_idx} 页下载失败: {last_err}")

            done = await asyncio.gather(*(_dl_page(idx, u) for idx, u in target_urls))
            done.sort(key=lambda item: item[0])
            image_paths = [p for _, p in done]

        if cover_bytes is None and image_paths:
            try:
                cover_bytes = image_paths[0].read_bytes()
            except Exception:
                cover_bytes = None

        extra_parts = [f"ID: #{gid}", f"页数: {total_pages}"]
        if tags:
            extra_parts.append(f"标签: {', '.join(tags[:12])}")

        media = ParsedMedia(
            platform="nhentai",
            title=title,
            url=f"https://{resolved_host}/g/{gid}/",
            author=", ".join(artists),
            extra_info="\n".join(extra_parts),
            cover_bytes=cover_bytes,
            cover_url=cover_url,
            image_paths=image_paths,
            page_count=total_pages or len(image_paths),
            is_nsfw=True,
            skipped_download_reason=skipped_reason,
            work_dir=str(work_dir),
        )
        return NhDownloadResult(media=media, image_paths=image_paths)
