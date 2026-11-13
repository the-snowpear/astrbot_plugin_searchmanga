from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urljoin, urlparse
import uuid
import zipfile

import aiohttp

from .constants import (
    DEFAULT_HEADERS,
    IMAGE_EXTS,
    NSFW_POLICY_BLUR,
    NSFW_POLICY_IGNORE,
    NSFW_POLICY_SEND,
    OUTPUT_FORMAT_MERGE,
    OUTPUT_FORMAT_PDF,
    OUTPUT_FORMAT_ZIP,
)
from .ehentai_service import (
    fetch_eh_media,
    parse_gallery_url as parse_eh_gallery_url,
    parse_reader_url as parse_eh_reader_url,
)
from .iwara_service import fetch_iwara_media, parse_iwara_target
from .jm_service import fetch_jm_media, parse_jm_target
from .models import GalleryDownload, ParsedMedia
from .nhentai_service import (
    fetch_nh_media,
    nhentai_url_for_host,
    parse_nhentai_target,
)
from .packaging import (
    blur_image_file,
    images_to_pdf,
    images_to_zip,
    natural_sort_key,
    repack_image_dir_flat,
    repack_zip_flat,
    safe_output_path,
    temp_root,
    validate_zip,
    verify_image_bytes,
)
from .parsing import guess_ext, is_zip_bytes

# Legacy test-imported symbol aliases
_nhentai_url_for_host = nhentai_url_for_host
_extract_jm_target = parse_jm_target

PANDA_ARCHIVE_RE = re.compile(
    r"https?://(?:www\.)?panda\.chaika\.moe/(archive|gallery)/(\d+)",
    re.I,
)
PANDA_DL_LINK_RE = re.compile(r'href=["\']([^"\']+/archive/\d+/download/?[^"\']*)["\']', re.I)
PANDA_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)


def _cleanup_dir(path_or_str: Path | str | None) -> None:
    if not path_or_str:
        return
    try:
        shutil.rmtree(Path(path_or_str), ignore_errors=True)
    except Exception:
        pass


def _finalize_gallery_package(
    image_paths: list[Path],
    title: str,
    *,
    archive_zip_path: Path | None = None,
    output_format: str = OUTPUT_FORMAT_ZIP,
) -> GalleryDownload:
    fmt = (output_format or OUTPUT_FORMAT_ZIP).strip().lower()
    if archive_zip_path is not None and archive_zip_path.exists():
        if fmt != OUTPUT_FORMAT_PDF:
            return repack_zip_flat(str(archive_zip_path), title)
        extract_dir = archive_zip_path.parent / "_extracted_pdf"
        extract_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        with zipfile.ZipFile(archive_zip_path, "r") as zf:
            names = sorted(
                (n for n in zf.namelist() if not n.endswith("/") and Path(n).suffix.lower() in IMAGE_EXTS),
                key=natural_sort_key,
            )
            for idx, name in enumerate(names, start=1):
                ext = Path(name).suffix.lower() or ".jpg"
                dest = extract_dir / f"{idx:05d}{ext}"
                dest.write_bytes(zf.read(name))
                extracted.append(dest)
        return images_to_pdf(extracted, title)

    if fmt == OUTPUT_FORMAT_PDF:
        return images_to_pdf(image_paths, title)
    return images_to_zip(image_paths, title)


async def download_images_to_zip(
    image_urls: list[str],
    title: str,
    *,
    timeout: int = 120,
    concurrency: int = 5,
    referer: str | None = None,
    proxy: str | None = None,
    output_format: str = OUTPUT_FORMAT_ZIP,
    work_dir: Path | None = None,
) -> GalleryDownload:
    """Download ordered image URLs and pack into ZIP or PDF."""
    if not image_urls:
        raise ValueError("没有可下载的图片链接")

    own_dir = work_dir is None
    if work_dir is None:
        safe_stem = Path(safe_output_path(title, ".tmp")).stem
        work_dir = temp_root() / f"imgs_{safe_stem}_{uuid.uuid4().hex[:6]}"
    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    client_timeout = aiohttp.ClientTimeout(total=max(20, int(timeout)))
    sem = asyncio.Semaphore(max(1, min(int(concurrency), 10)))
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer

    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:

            async def _fetch_one(idx: int, img_url: str) -> tuple[int, Path]:
                async with sem:
                    async with session.get(img_url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
                        resp.raise_for_status()
                        raw = await resp.read()
                        ct = resp.headers.get("Content-Type", "")
                    raw_ext = verify_image_bytes(raw) or guess_ext(img_url, ct)
                    ext = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
                    dest = pages_dir / f"{idx:05d}{ext}"
                    dest.write_bytes(raw)
                    return idx, dest

            done = await asyncio.gather(*(_fetch_one(i, u) for i, u in enumerate(image_urls, start=1)))
            done.sort(key=lambda item: item[0])
            ordered = [p for _, p in done]

        return _finalize_gallery_package(ordered, title, output_format=output_format)
    finally:
        if own_dir:
            _cleanup_dir(work_dir)


async def download_ehentai_pages_to_zip(
    gallery_url: str,
    *,
    timeout: int = 120,
    concurrency: int = 5,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    prefer_archive: bool = False,
    proxy: str | None = None,
    output_format: str = OUTPUT_FORMAT_ZIP,
    max_pages: int = 0,
) -> GalleryDownload:
    res = await fetch_eh_media(
        gallery_url,
        timeout=timeout,
        concurrency=concurrency,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        prefer_archive=prefer_archive,
        proxy=proxy,
        download_images=True,
        max_pages=max_pages,
    )
    try:
        if res.media.skipped_download_reason:
            raise RuntimeError(res.media.skipped_download_reason)
        return _finalize_gallery_package(
            res.image_paths,
            res.media.title,
            archive_zip_path=res.archive_zip_path,
            output_format=output_format,
        )
    finally:
        _cleanup_dir(res.media.work_dir)


async def _fetch_panda_media(
    target_url: str,
    *,
    timeout: int = 120,
    proxy: str | None = None,
    download_images: bool = True,
    max_pages: int = 0,
    work_dir: Path | None = None,
) -> tuple[ParsedMedia, Path | None]:
    m = PANDA_ARCHIVE_RE.search(target_url)
    if not m:
        raise ValueError(f"无法识别 Panda 链接: {target_url}")
    kind, panda_id = m.group(1).lower(), m.group(2)

    if work_dir is None:
        work_dir = temp_root() / f"panda_{panda_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    client_timeout = aiohttp.ClientTimeout(total=max(20, int(timeout)))
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.get(target_url, headers=DEFAULT_HEADERS, proxy=proxy, allow_redirects=True) as resp:
            resp.raise_for_status()
            html = await resp.text(errors="replace")

        title = f"Panda_{panda_id}"
        if tm := PANDA_TITLE_RE.search(html):
            clean = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
            if clean:
                title = clean

        dl_url = f"https://panda.chaika.moe/archive/{panda_id}/download/"
        if lm := PANDA_DL_LINK_RE.search(html):
            dl_url = urljoin(target_url, lm.group(1).replace("&amp;", "&"))

        zip_path: Path | None = None
        skipped_reason = ""
        if download_images:
            headers = dict(DEFAULT_HEADERS)
            headers["Referer"] = target_url
            async with session.get(dl_url, headers=headers, proxy=proxy, allow_redirects=True) as dl_resp:
                dl_resp.raise_for_status()
                raw = await dl_resp.read()
                if not is_zip_bytes(raw):
                    raise RuntimeError("Panda 返回的内容不是有效的 ZIP 压缩包")
            cand_zip = work_dir / Path(safe_output_path(title, ".zip")).name
            cand_zip.write_bytes(raw)
            validate_zip(str(cand_zip))
            with zipfile.ZipFile(cand_zip, "r") as zf:
                img_count = sum(
                    1
                    for n in zf.namelist()
                    if not n.endswith("/") and Path(n).suffix.lower() in IMAGE_EXTS
                )
            if max_pages > 0 and img_count > max_pages:
                skipped_reason = f"压缩包内图片数 {img_count} 超过上限 {max_pages}，已跳过发送"
            else:
                zip_path = cand_zip

        media = ParsedMedia(
            platform="panda",
            title=title,
            url=f"https://panda.chaika.moe/{kind}/{panda_id}/",
            extra_info=f"ID: {panda_id}",
            is_nsfw=True,
            skipped_download_reason=skipped_reason,
            work_dir=str(work_dir),
        )
        return media, zip_path


async def download_gallery(
    url: str,
    *,
    timeout: int = 120,
    concurrency: int = 5,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    prefer_archive: bool = True,
    proxy: str | None = None,
    output_format: str = OUTPUT_FORMAT_ZIP,
    max_pages: int = 0,
    iwara_cookie: str = "",
    video_quality: str = "540",
    iwara_max_size_mb: int = 100,
) -> GalleryDownload:
    """Download a manga/gallery from JM, E-Hentai/ExHentai, nHentai, Panda, or Iwara images into ZIP/PDF."""
    fmt = OUTPUT_FORMAT_PDF if str(output_format).strip().lower() == OUTPUT_FORMAT_PDF else OUTPUT_FORMAT_ZIP

    if parse_jm_target(url):
        res = await fetch_jm_media(
            url,
            timeout=timeout,
            concurrency=concurrency,
            proxy=proxy,
            download_images=True,
            max_pages=max_pages,
        )
        try:
            if res.media.skipped_download_reason:
                raise RuntimeError(res.media.skipped_download_reason)
            return _finalize_gallery_package(res.image_paths, res.media.title, output_format=fmt)
        finally:
            _cleanup_dir(res.media.work_dir)

    if parse_eh_gallery_url(url) or parse_eh_reader_url(url):
        return await download_ehentai_pages_to_zip(
            url,
            timeout=timeout,
            concurrency=concurrency,
            ehentai_cookie=ehentai_cookie,
            exhentai_cookie=exhentai_cookie,
            prefer_archive=prefer_archive,
            proxy=proxy,
            output_format=fmt,
            max_pages=max_pages,
        )

    if parse_nhentai_target(url):
        res = await fetch_nh_media(
            url,
            timeout=timeout,
            concurrency=concurrency,
            proxy=proxy,
            download_images=True,
            max_pages=max_pages,
        )
        try:
            if res.media.skipped_download_reason:
                raise RuntimeError(res.media.skipped_download_reason)
            return _finalize_gallery_package(res.image_paths, res.media.title, output_format=fmt)
        finally:
            _cleanup_dir(res.media.work_dir)

    if PANDA_ARCHIVE_RE.search(url):
        media, zip_path = await _fetch_panda_media(
            url,
            timeout=timeout,
            proxy=proxy,
            download_images=True,
            max_pages=max_pages,
        )
        try:
            if media.skipped_download_reason:
                raise RuntimeError(media.skipped_download_reason)
            if zip_path is None:
                raise RuntimeError("Panda 下载失败：未获得有效归档")
            return _finalize_gallery_package([], media.title, archive_zip_path=zip_path, output_format=fmt)
        finally:
            _cleanup_dir(media.work_dir)

    if iw := parse_iwara_target(url):
        if iw[0] == "image":
            res = await fetch_iwara_media(
                url,
                timeout=timeout,
                proxy=proxy,
                iwara_cookie=iwara_cookie,
                video_quality=video_quality,
                max_size_mb=iwara_max_size_mb,
                download_media=True,
                max_pages=max_pages,
            )
            try:
                if res.media.skipped_download_reason:
                    raise RuntimeError(res.media.skipped_download_reason)
                return _finalize_gallery_package(res.image_paths, res.media.title, output_format=fmt)
            finally:
                _cleanup_dir(res.media.work_dir)
        raise RuntimeError("Iwara 视频请使用解析功能发送视频消息，不支持打包为图包")

    host = (urlparse(url).hostname or "").lower()
    raise RuntimeError(f"暂不支持自动下载该站点: {host or url}")


def _apply_preview_nsfw_policy(media: ParsedMedia, nsfw_policy: str) -> ParsedMedia:
    if not media.is_nsfw:
        return media
    if nsfw_policy == NSFW_POLICY_IGNORE:
        media.cover_bytes = None
        media.image_paths = []
        return media
    if nsfw_policy == NSFW_POLICY_BLUR:
        media.image_paths = [blur_image_file(p) for p in media.image_paths]
    return media


def _finalize_parsed_gallery(
    media: ParsedMedia,
    image_paths: list[Path],
    *,
    archive_zip_path: Path | None = None,
    want_file: bool,
    output_format: str,
    preview_count: int,
    nsfw_policy: str,
) -> ParsedMedia:
    if want_file and not media.skipped_download_reason and (image_paths or archive_zip_path is not None):
        pkg = _finalize_gallery_package(
            image_paths,
            media.title,
            archive_zip_path=archive_zip_path,
            output_format=output_format,
        )
        media.file_path = pkg.zip_path
        media.file_name = Path(pkg.zip_path).name
        media.image_paths = image_paths[:preview_count] if preview_count > 0 else []
    elif not want_file:
        media.image_paths = list(image_paths[:preview_count]) if preview_count > 0 else []
    return _apply_preview_nsfw_policy(media, nsfw_policy)


async def parse_media_target(
    target: str,
    *,
    timeout: int = 120,
    concurrency: int = 5,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    prefer_archive: bool = True,
    proxy: str | None = None,
    output_format: str = OUTPUT_FORMAT_MERGE,
    preview_count: int = 3,
    max_pages: int = 120,
    nsfw_policy: str = NSFW_POLICY_SEND,
    iwara_cookie: str = "",
    video_quality: str = "540",
    iwara_max_size_mb: int = 100,
    download_gallery_file: bool | None = None,
) -> ParsedMedia:
    """Unified link parser for JM, E-Hentai/ExHentai, nHentai, Iwara, and Panda."""
    fmt = (output_format or OUTPUT_FORMAT_MERGE).strip().lower()
    want_file = fmt in {OUTPUT_FORMAT_ZIP, OUTPUT_FORMAT_PDF} if download_gallery_file is None else bool(download_gallery_file)
    policy = nsfw_policy if nsfw_policy in {NSFW_POLICY_SEND, NSFW_POLICY_BLUR, NSFW_POLICY_IGNORE} else NSFW_POLICY_SEND
    prev_limit = max(0, int(preview_count)) if not want_file else 0

    if parse_iwara_target(target):
        res = await fetch_iwara_media(
            target,
            timeout=timeout,
            proxy=proxy,
            iwara_cookie=iwara_cookie,
            video_quality=video_quality,
            max_size_mb=iwara_max_size_mb,
            nsfw_policy=policy,
            download_media=True,
            preview_limit=max(1, prev_limit or 6),
            max_pages=max_pages,
        )
        media = res.media
        if want_file and not media.ignored_reason and not media.skipped_download_reason and res.image_paths:
            pkg = _finalize_gallery_package(res.image_paths, media.title, output_format=fmt)
            media.file_path = pkg.zip_path
            media.file_name = Path(pkg.zip_path).name
        elif prev_limit > 0 and media.image_paths:
            media.image_paths = media.image_paths[:prev_limit]
        return media

    if parse_jm_target(target):
        res = await fetch_jm_media(
            target,
            timeout=timeout,
            concurrency=concurrency,
            proxy=proxy,
            download_images=want_file,
            preview_limit=prev_limit,
            max_pages=max_pages,
        )
        return _finalize_parsed_gallery(
            res.media,
            res.image_paths,
            want_file=want_file,
            output_format=fmt,
            preview_count=preview_count,
            nsfw_policy=policy,
        )

    if parse_eh_gallery_url(target) or parse_eh_reader_url(target):
        res = await fetch_eh_media(
            target,
            timeout=timeout,
            concurrency=concurrency,
            ehentai_cookie=ehentai_cookie,
            exhentai_cookie=exhentai_cookie,
            prefer_archive=prefer_archive if want_file else False,
            proxy=proxy,
            download_images=want_file,
            preview_limit=prev_limit,
            max_pages=max_pages,
        )
        return _finalize_parsed_gallery(
            res.media,
            res.image_paths,
            archive_zip_path=res.archive_zip_path,
            want_file=want_file,
            output_format=fmt,
            preview_count=preview_count,
            nsfw_policy=policy,
        )

    if parse_nhentai_target(target):
        res = await fetch_nh_media(
            target,
            timeout=timeout,
            concurrency=concurrency,
            proxy=proxy,
            download_images=want_file,
            preview_limit=prev_limit,
            max_pages=max_pages,
        )
        return _finalize_parsed_gallery(
            res.media,
            res.image_paths,
            want_file=want_file,
            output_format=fmt,
            preview_count=preview_count,
            nsfw_policy=policy,
        )

    if PANDA_ARCHIVE_RE.search(target):
        media, zip_path = await _fetch_panda_media(
            target,
            timeout=timeout,
            proxy=proxy,
            download_images=want_file,
            max_pages=max_pages,
        )
        return _finalize_parsed_gallery(
            media,
            [],
            archive_zip_path=zip_path,
            want_file=want_file,
            output_format=fmt,
            preview_count=preview_count,
            nsfw_policy=policy,
        )

    raise ValueError(f"不支持的解析目标: {target}")
