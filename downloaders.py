import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import aiohttp

from astrbot.api import logger

from .constants import DEFAULT_HEADERS, IMAGE_EXTS, JMCOMIC_HOSTS, NHENTAI_EXT
from .models import GalleryDownload
from .parsing import guess_ext, is_zip_bytes, normalize_host, sanitize_filename

DOWNLOAD_RETRY_ATTEMPTS = 4
DOWNLOAD_RETRY_BASE_DELAY = 0.8
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
EHENTAI_IMAGE_RELOAD_ATTEMPTS = 6
TRANSIENT_DOWNLOAD_ERRORS = (
    aiohttp.ClientPayloadError,
    aiohttp.ClientConnectionError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientOSError,
    asyncio.TimeoutError,
)


def _load_bs4():
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        raise RuntimeError("缺少 beautifulsoup4，请先安装插件依赖。") from exc
    return BeautifulSoup


async def _read_text(resp: aiohttp.ClientResponse) -> str:
    return await resp.text(errors="ignore")


async def _get_text(
    sess: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> str:
    async with sess.get(url, headers=headers or DEFAULT_HEADERS, proxy=proxy) as resp:
        text = await _read_text(resp)
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {text[:160]}")
        return text


async def _get_json(
    sess: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> Any:
    async with sess.get(url, headers=headers or DEFAULT_HEADERS, proxy=proxy) as resp:
        text = await _read_text(resp)
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {text[:160]}")
        return json.loads(text)


async def _download_bytes(
    sess: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    attempts: int = DOWNLOAD_RETRY_ATTEMPTS,
) -> tuple[bytes, str]:
    last_exc: BaseException | None = None
    max_attempts = max(1, attempts)
    data = bytearray()
    content_type = ""
    for attempt in range(1, max_attempts + 1):
        try:
            request_headers = dict(headers or DEFAULT_HEADERS)
            request_headers.setdefault("Accept-Encoding", "identity")
            if data:
                request_headers["Range"] = f"bytes={len(data)}-"
            async with sess.get(url, headers=request_headers, proxy=proxy) as resp:
                if resp.status >= 400:
                    preview_data = await resp.read()
                    preview = preview_data[:160].decode("utf-8", errors="ignore")
                    message = f"HTTP {resp.status}: {preview}"
                    if resp.status not in RETRYABLE_HTTP_STATUSES or attempt >= max_attempts:
                        raise RuntimeError(message)
                    last_exc = RuntimeError(message)
                else:
                    if data and resp.status != 206:
                        data.clear()
                    content_type = resp.headers.get("Content-Type", content_type)
                    expected_size = resp.content_length
                    start_size = len(data)
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                    if expected_size is not None and len(data) - start_size < expected_size:
                        raise aiohttp.ClientPayloadError(
                            f"incomplete payload: got {len(data) - start_size}/{expected_size} bytes"
                        )
                    return bytes(data), content_type
        except RuntimeError as exc:
            if "Not enough data to satisfy content length header" not in str(exc):
                raise
            last_exc = exc
        except TRANSIENT_DOWNLOAD_ERRORS as exc:
            last_exc = exc
        if attempt < max_attempts:
            delay = DOWNLOAD_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if data:
                logger.warning(
                    f"[搜本子下载] 下载中断，已接收 {_format_bytes(len(data))}，"
                    f"{delay:.1f}s 后断点续传 ({attempt}/{max_attempts}): {url}"
                )
            else:
                logger.warning(f"[搜本子下载] 下载中断，{delay:.1f}s 后重试 ({attempt}/{max_attempts}): {url}")
            await asyncio.sleep(delay)

    raise RuntimeError(f"下载失败，已重试 {max_attempts} 次: {last_exc}") from last_exc


def _safe_zip_path(title: str) -> str:
    base = sanitize_filename(title, "doujin")
    root = Path(tempfile.gettempdir()) / "astrbot_searchmanga"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{base}.zip"
    if not path.exists():
        return str(path)
    suffix = int(time.time())
    return str(root / f"{base}_{suffix}.zip")


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def _natural_sort_key(path: Path) -> list[int | str]:
    parts: list[int | str] = []
    for text in path.as_posix().split("/"):
        for item in re.split(r"(\d+)", text):
            if item.isdigit():
                parts.append(int(item))
            elif item:
                parts.append(item.lower())
    return parts


def _validate_zip(zip_path: str) -> None:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad_name = zf.testzip()
            if bad_name:
                raise RuntimeError(f"ZIP CRC 校验失败: {bad_name}")
            if not zf.infolist():
                raise RuntimeError("ZIP 文件为空")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ZIP 文件损坏: {zip_path}") from exc


def _looks_like_image(data: bytes) -> bool:
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return True
    return False


async def download_images_to_zip(
    sess: aiohttp.ClientSession,
    title: str,
    image_urls: list[str],
    *,
    headers_for: Any = None,
    concurrency: int = 4,
    proxy: str | None = None,
) -> GalleryDownload:
    if not image_urls:
        raise RuntimeError("没有解析到图片地址")

    zip_path = _safe_zip_path(title)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    total = len(image_urls)
    completed = 0
    logger.info(
        f"[搜本子下载] 开始逐页下载: 标题={title}, 页数={total}, "
        f"并发={max(1, concurrency)}, 代理={'启用' if proxy else '未启用'}"
    )

    async def fetch_one(idx: int, image_url: str) -> tuple[int, str, bytes]:
        nonlocal completed
        async with semaphore:
            headers = headers_for(image_url) if callable(headers_for) else headers_for
            try:
                data, content_type = await _download_bytes(
                    sess,
                    image_url,
                    headers=headers,
                    proxy=proxy,
                )
            except Exception as exc:
                raise RuntimeError(f"第 {idx} 页下载失败: {exc}") from exc
            if not data:
                raise RuntimeError(f"第 {idx} 页下载为空")
            if not _looks_like_image(data):
                raise RuntimeError(f"第 {idx} 页下载内容不是有效图片")
            ext = guess_ext(image_url, content_type)
            completed += 1
            logger.info(
                f"[搜本子下载] {title}: 已下载 {completed}/{total} 页 "
                f"(第 {idx} 页, {_format_bytes(len(data))})"
            )
            return idx, ext, data

    tasks = [fetch_one(idx, url) for idx, url in enumerate(image_urls, 1)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda item: item[0])

    logger.info(f"[搜本子下载] {title}: 开始打包 {len(results)} 页 -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, ext, data in results:
            zf.writestr(f"{idx:03d}.{ext}", data)

    logger.info(f"[搜本子下载] {title}: 打包完成 -> {zip_path}")
    _validate_zip(zip_path)
    return GalleryDownload(title=title, zip_path=zip_path)


def repack_zip_flat(source_zip: str, title: str) -> GalleryDownload:
    zip_path = _safe_zip_path(title)
    with zipfile.ZipFile(source_zip, "r") as src, zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as dst:
        infos = [
            info
            for info in src.infolist()
            if not info.is_dir() and Path(info.filename).suffix.lower() in IMAGE_EXTS
        ]
        if not infos:
            raise RuntimeError("归档里没有可识别的图片")
        infos.sort(key=lambda info: info.filename)
        logger.info(f"[搜本子下载] {title}: 开始重打包归档 {len(infos)} 张图片 -> {zip_path}")
        for idx, info in enumerate(infos, 1):
            ext = Path(info.filename).suffix.lower().lstrip(".")
            ext = "jpg" if ext == "jpeg" else ext
            dst.writestr(f"{idx:03d}.{ext}", src.read(info))
            logger.info(f"[搜本子下载] {title}: 已打包 {idx}/{len(infos)} 张")
    logger.info(f"[搜本子下载] {title}: 归档重打包完成 -> {zip_path}")
    _validate_zip(zip_path)
    return GalleryDownload(title=title, zip_path=zip_path)


def repack_image_dir_flat(source_dir: str, title: str) -> GalleryDownload:
    root = Path(source_dir)
    image_paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS
    ]
    if not image_paths:
        raise RuntimeError("未找到下载后的图片文件")

    image_paths.sort(key=lambda path: _natural_sort_key(path.relative_to(root)))
    zip_path = _safe_zip_path(title)
    logger.info(f"[搜本子下载] {title}: 开始打包本地图片 {len(image_paths)} 张 -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, image_path in enumerate(image_paths, 1):
            ext = image_path.suffix.lower().lstrip(".")
            ext = "jpg" if ext == "jpeg" else ext
            zf.write(image_path, f"{idx:03d}.{ext}")
            logger.info(f"[搜本子下载] {title}: 已打包 {idx}/{len(image_paths)} 张")
    logger.info(f"[搜本子下载] {title}: 本地图片打包完成 -> {zip_path}")
    _validate_zip(zip_path)
    return GalleryDownload(title=title, zip_path=zip_path)


def _extract_panda_archive_id(url: str) -> str | None:
    parsed = urlparse(url)
    query_archive = parse_qs(parsed.query).get("archive")
    if query_archive:
        return query_archive[0]
    match = re.search(r"/archive/([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def _extract_jm_target(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme == "jmcomic":
        if parsed.netloc == "album":
            album_id = parsed.path.strip("/")
            return ("album", album_id) if album_id.isdigit() else None
        if parsed.netloc == "photo":
            photo_id = parsed.path.strip("/")
            return ("photo", photo_id) if photo_id.isdigit() else None
        match = re.search(r"/(album|photo)/(\d+)", parsed.path, re.I)
        if match:
            return match.group(1).lower(), match.group(2)
        return None
    host = normalize_host(parsed.netloc)
    if host not in JMCOMIC_HOSTS and "18comic" not in host and "jmcomic" not in host:
        return None
    match = re.search(r"/(album|photo)/(\d+)(?:[/?#]|$)", parsed.path, re.I)
    if match:
        return match.group(1).lower(), match.group(2)
    return None


def _extract_jm_id(url: str) -> str | None:
    target = _extract_jm_target(url)
    return target[1] if target else None


def _load_jmcomic():
    try:
        import jmcomic
    except Exception as exc:
        raise RuntimeError("缺少 jmcomic，请先安装插件依赖。") from exc
    return jmcomic


def _jm_option_config(download_dir: str, concurrency: int, proxy: str | None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "log": False,
        "dir_rule": {
            "base_dir": download_dir,
            "rule": "Bd_Aid_Pindex",
        },
        "download": {
            "cache": False,
            "image": {
                "decode": True,
                "suffix": None,
            },
            "threading": {
                "image": max(1, concurrency),
                "photo": max(1, min(concurrency, 4)),
            },
        },
    }
    if proxy:
        config["client"] = {
            "postman": {
                "meta_data": {
                    "proxies": {
                        "http": proxy,
                        "https": proxy,
                    }
                }
            }
        }
    return config


def _create_jm_option(jmcomic: Any, download_dir: str, concurrency: int, proxy: str | None) -> Any:
    config = _jm_option_config(download_dir, concurrency, proxy)
    if hasattr(jmcomic, "create_option_by_str"):
        return jmcomic.create_option_by_str(json.dumps(config, ensure_ascii=False), "json")

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as fp:
        json.dump(config, fp, ensure_ascii=False)
        option_file = fp.name
    try:
        if hasattr(jmcomic, "create_option_by_file"):
            return jmcomic.create_option_by_file(option_file)
    finally:
        try:
            os.remove(option_file)
        except OSError:
            pass
    raise RuntimeError("当前 jmcomic 版本不支持 create_option")


def _download_jmcomic_sync(
    target_kind: str,
    target_id: str,
    concurrency: int,
    proxy: str | None,
) -> GalleryDownload:
    jmcomic = _load_jmcomic()
    download_dir = tempfile.mkdtemp(prefix=f"astrbot_jmcomic_{target_kind}_{target_id}_")
    try:
        logger.info(
            f"[搜本子下载] 开始 JMComic 下载: {target_kind}={target_id}, "
            f"并发={max(1, concurrency)}, 代理={'启用' if proxy else '未启用'}"
        )
        option = _create_jm_option(jmcomic, download_dir, concurrency, proxy)
        album = None
        if target_kind == "photo":
            try:
                client = option.new_jm_client()
                photo_detail = client.get_photo_detail(target_id)
                album_id = getattr(photo_detail, "album_id", None)
                if album_id:
                    logger.info(
                        f"[搜本子下载] JMComic 章节 {target_id} 成功解析到所属画册 ID: {album_id}，开始下载全本"
                    )
                    album = jmcomic.download_album(str(album_id), option)
                else:
                    logger.info(
                        f"[搜本子下载] JMComic 未获取到画册 ID，下载章节: photo_id={target_id}"
                    )
                    album = jmcomic.download_photo(target_id, option)
            except Exception as exc:
                logger.warning(
                    f"[搜本子下载] JMComic 章节解析画册失败 ({exc})，回退到章节下载: photo_id={target_id}"
                )
                album = jmcomic.download_photo(target_id, option)
        else:
            album = jmcomic.download_album(target_id, option)

        title = f"jmcomic_{target_id}"
        if album is not None:
            title = str(
                getattr(album, "title", None)
                or getattr(album, "name", None)
                or getattr(album, "album_id", None)
                or getattr(album, "photo_id", None)
                or title
            )
        logger.info(f"[搜本子下载] JMComic 下载完成，开始整理图片: {target_kind}={target_id}, 标题={title}")
        return repack_image_dir_flat(download_dir, title)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"JMComic 下载失败或网络不可达: {exc}") from exc
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)


async def _download_jmcomic(
    url: str,
    *,
    concurrency: int,
    proxy: str | None,
) -> GalleryDownload:
    target = _extract_jm_target(url)
    if not target:
        raise RuntimeError("无法识别 JM 号或链接")
    target_kind, target_id = target
    return await asyncio.to_thread(
        _download_jmcomic_sync, target_kind, target_id, concurrency, proxy
    )


async def _download_panda(
    sess: aiohttp.ClientSession,
    url: str,
    *,
    concurrency: int,
    proxy: str | None,
) -> GalleryDownload:
    archive_id = _extract_panda_archive_id(url)
    if not archive_id:
        raise RuntimeError("无法从 Panda 链接中解析 archive id")

    title = f"panda_{archive_id}"
    logger.info(
        f"[搜本子下载] 开始 Panda 归档下载: archive_id={archive_id}, "
        f"代理={'启用' if proxy else '未启用'}"
    )
    api_url = f"https://panda.chaika.moe/api?archive={archive_id}"
    try:
        payload = await _get_json(
            sess,
            api_url,
            headers={**DEFAULT_HEADERS, "Referer": url},
            proxy=proxy,
        )
        if isinstance(payload, dict):
            title = str(payload.get("title") or payload.get("name") or title)
    except Exception as exc:
        logger.debug(f"Panda API 读取失败，将使用兜底标题: {exc}")

    download_url = f"https://panda.chaika.moe/archive/{archive_id}/download/"
    data, content_type = await _download_bytes(
        sess,
        download_url,
        headers={**DEFAULT_HEADERS, "Referer": url},
        proxy=proxy,
    )
    logger.info(f"[搜本子下载] Panda 归档下载完成: {title}, 大小={_format_bytes(len(data))}")
    if not (is_zip_bytes(data) or "zip" in content_type.lower()):
        raise RuntimeError("Panda 下载接口没有返回 zip 归档")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as fp:
        fp.write(data)
        source_zip = fp.name
    try:
        return await asyncio.to_thread(repack_zip_flat, source_zip, title)
    finally:
        try:
            os.remove(source_zip)
        except OSError:
            pass


def _extract_nhentai_id(url: str) -> str | None:
    match = re.search(r"/g/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


def _nhentai_url_for_host(url: str, host: str) -> str:
    gallery_id = _extract_nhentai_id(url)
    if gallery_id:
        return f"https://{host}/g/{gallery_id}/"
    parsed = urlparse(url)
    return urlunparse(parsed._replace(scheme="https", netloc=host, query="", fragment=""))


def _nhentai_title_from_api(payload: dict) -> str:
    title = payload.get("title") or {}
    if isinstance(title, dict):
        return str(title.get("pretty") or title.get("english") or title.get("japanese") or "nhentai")
    return str(title or "nhentai")


async def _try_nhentai_api(
    sess: aiohttp.ClientSession,
    gallery_id: str,
    host: str,
    proxy: str | None,
) -> tuple[str, list[str]] | None:
    api_url = f"https://{host}/api/gallery/{gallery_id}"
    payload = await _get_json(
        sess,
        api_url,
        headers={**DEFAULT_HEADERS, "Referer": f"https://{host}/g/{gallery_id}/"},
        proxy=proxy,
    )
    if not isinstance(payload, dict):
        return None
    media_id = payload.get("media_id")
    pages = (payload.get("images") or {}).get("pages") if isinstance(payload.get("images"), dict) else None
    if not media_id or not isinstance(pages, list):
        return None
    image_urls: list[str] = []
    for idx, page in enumerate(pages, 1):
        page_type = page.get("t") if isinstance(page, dict) else ""
        ext = NHENTAI_EXT.get(str(page_type), "jpg")
        image_urls.append(f"https://i.nhentai.net/galleries/{media_id}/{idx}.{ext}")
    return _nhentai_title_from_api(payload), image_urls


async def _parse_nhentai_page(
    sess: aiohttp.ClientSession,
    gallery_url: str,
    proxy: str | None,
) -> tuple[str, list[str]]:
    BeautifulSoup = _load_bs4()
    html_text = await _get_text(
        sess,
        gallery_url,
        headers={**DEFAULT_HEADERS, "Referer": gallery_url},
        proxy=proxy,
    )
    soup = BeautifulSoup(html_text, "html.parser")
    title_node = soup.select_one("h1.title") or soup.select_one("h1") or soup.select_one("title")
    title = title_node.get_text(" ", strip=True) if title_node else "nhentai"

    page_links: list[str] = []
    for anchor in soup.select("a.gallerythumb, .thumb-container a, a[href*='/g/']"):
        href = anchor.get("href")
        if not href:
            continue
        full = urljoin(gallery_url, href)
        if re.search(r"/g/\d+/\d+/?$", urlparse(full).path) and full not in page_links:
            page_links.append(full)

    if not page_links:
        media_match = re.search(r"galleries/(\d+)/(\d+)t?\.(?:jpg|png|webp|gif)", html_text)
        count_match = re.search(r"Pages:\s*</span>\s*<span[^>]*>(\d+)", html_text, re.I)
        if media_match and count_match:
            media_id = media_match.group(1)
            count = int(count_match.group(1))
            return title, [
                f"https://i.nhentai.net/galleries/{media_id}/{idx}.jpg"
                for idx in range(1, count + 1)
            ]
        raise RuntimeError("无法解析 nHentai 图片页列表")

    semaphore = asyncio.Semaphore(4)

    async def fetch_page_image(page_url: str) -> str:
        async with semaphore:
            page_html = await _get_text(
                sess,
                page_url,
                headers={**DEFAULT_HEADERS, "Referer": gallery_url},
                proxy=proxy,
            )
            page_soup = BeautifulSoup(page_html, "html.parser")
            img = (
                page_soup.select_one("#image-container img")
                or page_soup.select_one("#fimg")
                or page_soup.select_one("img[data-src*='nhentaimg']")
                or page_soup.select_one("img[src*='nhentaimg']")
                or page_soup.select_one("img[src*='galleries/']")
            )
            if not img:
                raise RuntimeError(f"无法解析图片地址: {page_url}")
            src = img.get("data-src") or img.get("src")
            if not src:
                raise RuntimeError(f"图片地址为空: {page_url}")
            return urljoin(page_url, src)

    image_urls = await asyncio.gather(*(fetch_page_image(link) for link in page_links))
    return title, image_urls


async def _download_nhentai(
    sess: aiohttp.ClientSession,
    url: str,
    *,
    concurrency: int,
    proxy: str | None,
) -> GalleryDownload:
    gallery_id = _extract_nhentai_id(url)
    if not gallery_id:
        raise RuntimeError("无法从 nHentai 链接中解析作品 id")

    parsed_host = normalize_host(urlparse(url).netloc)
    logger.info(
        f"[搜本子下载] 开始 nHentai 下载: gallery_id={gallery_id}, "
        f"host={parsed_host}, 代理={'启用' if proxy else '未启用'}"
    )
    host_candidates = [parsed_host]
    for host in ("nhentai.net", "nhentai.xxx"):
        if host not in host_candidates:
            host_candidates.append(host)

    last_error: Exception | None = None
    for host in host_candidates:
        try:
            result = await _try_nhentai_api(sess, gallery_id, host, proxy=proxy)
            if result:
                title, image_urls = result
                logger.info(f"[搜本子下载] nHentai API 解析成功: {title}, 页数={len(image_urls)}")
                return await download_images_to_zip(
                    sess,
                    title,
                    image_urls,
                    headers_for=lambda image_url: {
                        **DEFAULT_HEADERS,
                        "Referer": f"https://{host}/g/{gallery_id}/",
                    },
                    concurrency=concurrency,
                    proxy=proxy,
                )
        except Exception as exc:
            last_error = exc
            logger.debug(f"nHentai API 失败({host}): {exc}")

    for host in host_candidates:
        try:
            gallery_url = _nhentai_url_for_host(url, host)
            title, image_urls = await _parse_nhentai_page(sess, gallery_url, proxy=proxy)
            logger.info(f"[搜本子下载] nHentai 页面解析成功: {title}, 页数={len(image_urls)}")
            return await download_images_to_zip(
                sess,
                title,
                image_urls,
                headers_for=lambda image_url: {
                    **DEFAULT_HEADERS,
                    "Referer": gallery_url,
                },
                concurrency=concurrency,
                proxy=proxy,
            )
        except Exception as exc:
            last_error = exc
            logger.debug(f"nHentai 页面解析失败({host}): {exc}")

    raise RuntimeError(f"nHentai 下载失败: {last_error}")


def _ehentai_cookie_for_url(url: str, ehentai_cookie: str, exhentai_cookie: str) -> str:
    host = normalize_host(urlparse(url).netloc)
    if host == "exhentai.org":
        return exhentai_cookie or ehentai_cookie
    return ehentai_cookie


def _ehentai_headers(url: str, cookie: str = "", referer: str | None = None) -> dict[str, str]:
    headers = {**DEFAULT_HEADERS, "Referer": referer or url}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _extract_ehentai_gid_token(url: str) -> tuple[str, str] | None:
    match = re.search(r"/g/(\d+)/([0-9a-fA-F]+)/?", urlparse(url).path)
    if not match:
        return None
    return match.group(1), match.group(2)


def _ehentai_title(soup: Any) -> str:
    for selector in ("#gj", "#gn", "title"):
        node = soup.select_one(selector)
        if node:
            title = node.get_text(" ", strip=True)
            if title:
                return title
    return "ehentai"


async def _try_ehentai_archive(
    sess: aiohttp.ClientSession,
    gallery_url: str,
    gallery_html: str,
    title: str,
    cookie: str,
    proxy: str | None,
) -> GalleryDownload | None:
    if not cookie:
        return None
    gid_token = _extract_ehentai_gid_token(gallery_url)
    if not gid_token:
        return None
    gid, token = gid_token
    BeautifulSoup = _load_bs4()
    soup = BeautifulSoup(gallery_html, "html.parser")
    archive_candidates: list[str] = [
        urljoin(gallery_url, href)
        for href in (a.get("href") for a in soup.select("a[href*='archiver.php']"))
        if href
    ]
    archive_candidates.append(urljoin(gallery_url, f"/archiver.php?gid={gid}&token={token}"))

    seen: set[str] = set()
    for archive_url in archive_candidates:
        if archive_url in seen:
            continue
        seen.add(archive_url)
        try:
            page = await _get_text(
                sess,
                archive_url,
                headers=_ehentai_headers(gallery_url, cookie, gallery_url),
                proxy=proxy,
            )
            page_soup = BeautifulSoup(page, "html.parser")
            download_candidates = [
                urljoin(archive_url, href)
                for href in (a.get("href") for a in page_soup.select("a[href]"))
                if href and ("archiver.php" in href or ".zip" in href.lower())
            ]
            if not download_candidates:
                download_candidates = [archive_url + ("&or=1" if "?" in archive_url else "?or=1")]
            for download_url in download_candidates:
                data, content_type = await _download_bytes(
                    sess,
                    download_url,
                    headers=_ehentai_headers(gallery_url, cookie, archive_url),
                    proxy=proxy,
                )
                if is_zip_bytes(data) or "zip" in content_type.lower():
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as fp:
                        fp.write(data)
                        source_zip = fp.name
                    try:
                        return await asyncio.to_thread(repack_zip_flat, source_zip, title)
                    finally:
                        try:
                            os.remove(source_zip)
                        except OSError:
                            pass
        except Exception as exc:
            logger.debug(f"E-Hentai 归档下载尝试失败: {exc}")
    return None


def _extract_ehentai_page_links(soup: Any, gallery_url: str) -> list[str]:
    links: list[str] = []
    for anchor in soup.select(".gdtm a, .gdtl a, a[href*='/s/']"):
        href = anchor.get("href")
        if href:
            full = urljoin(gallery_url, href)
            if "/s/" in urlparse(full).path and full not in links:
                links.append(full)
    return links


def _ehentai_gallery_page_urls(soup: Any, gallery_url: str, html_text: str) -> list[str]:
    urls = [gallery_url]
    parsed = urlparse(gallery_url)
    max_page = 0
    for anchor in soup.select("table.ptt a[href], .ptt a[href]"):
        href = anchor.get("href") or ""
        query = parse_qs(urlparse(href).query)
        page = query.get("p", [""])[0]
        if str(page).isdigit():
            max_page = max(max_page, int(page))
    if max_page == 0:
        count_match = re.search(r"Showing\s+\d+\s*-\s*\d+\s+of\s+(\d+)\s+images", html_text, re.I)
        if count_match:
            total = int(count_match.group(1))
            max_page = max(0, (total - 1) // 40)
    for page in range(1, max_page + 1):
        query = parse_qs(parsed.query)
        query["p"] = [str(page)]
        query_text = "&".join(f"{key}={value[0]}" for key, value in query.items())
        urls.append(urlunparse(parsed._replace(query=query_text)))
    return urls


async def _collect_ehentai_page_links(
    sess: aiohttp.ClientSession,
    gallery_url: str,
    gallery_html: str,
    cookie: str,
    proxy: str | None,
) -> tuple[str, list[str]]:
    BeautifulSoup = _load_bs4()
    soup = BeautifulSoup(gallery_html, "html.parser")
    title = _ehentai_title(soup)
    gallery_pages = _ehentai_gallery_page_urls(soup, gallery_url, gallery_html)
    links = _extract_ehentai_page_links(soup, gallery_url)

    for page_url in gallery_pages[1:]:
        page_html = await _get_text(
            sess,
            page_url,
            headers=_ehentai_headers(gallery_url, cookie, gallery_url),
            proxy=proxy,
        )
        page_soup = BeautifulSoup(page_html, "html.parser")
        for link in _extract_ehentai_page_links(page_soup, page_url):
            if link not in links:
                links.append(link)
    return title, links


async def _parse_ehentai_image_url(
    sess: aiohttp.ClientSession,
    page_url: str,
    cookie: str,
    gallery_url: str,
    proxy: str | None,
) -> str:
    BeautifulSoup = _load_bs4()
    page_html = await _get_text(
        sess,
        page_url,
        headers=_ehentai_headers(gallery_url, cookie, gallery_url),
        proxy=proxy,
    )
    soup = BeautifulSoup(page_html, "html.parser")
    img = soup.select_one("#img")
    if not img:
        raise RuntimeError(f"无法解析图片页: {page_url}")
    src = img.get("src")
    if not src:
        raise RuntimeError(f"图片地址为空: {page_url}")
    return urljoin(page_url, src)


def _ehentai_reload_url(soup: Any, page_url: str) -> str | None:
    def url_with_nl(nl: str) -> str:
        parsed = urlparse(page_url)
        query = parse_qs(parsed.query)
        query["nl"] = [nl]
        query_text = "&".join(f"{key}={value[0]}" for key, value in query.items())
        return urlunparse(parsed._replace(query=query_text))

    def extract_nl(text: str) -> str | None:
        match = re.search(r"(?:nl|load_image)\(['\"]?([A-Za-z0-9_-]{8,})['\"]?\)", text)
        return match.group(1) if match else None

    for anchor in soup.select("a"):
        text = anchor.get_text(" ", strip=True).lower()
        href = anchor.get("href") or ""
        onclick = anchor.get("onclick") or ""
        nl = extract_nl(onclick)
        if nl:
            return url_with_nl(nl)
        if "nl=" in href:
            return urljoin(page_url, href)
        if ("reload" in text or "show another" in text) and href:
            return urljoin(page_url, href)
    for script in soup.select("script"):
        script_text = script.get_text(" ", strip=True)
        nl = extract_nl(script_text)
        if nl:
            return url_with_nl(nl)
    return None


async def _parse_ehentai_image_page(
    sess: aiohttp.ClientSession,
    page_url: str,
    cookie: str,
    gallery_url: str,
    proxy: str | None,
) -> tuple[str, str | None]:
    BeautifulSoup = _load_bs4()
    page_html = await _get_text(
        sess,
        page_url,
        headers=_ehentai_headers(gallery_url, cookie, gallery_url),
        proxy=proxy,
    )
    soup = BeautifulSoup(page_html, "html.parser")
    img = soup.select_one("#img")
    if not img:
        raise RuntimeError(f"Cannot parse E/ExHentai image page: {page_url}")
    src = img.get("src")
    if not src:
        raise RuntimeError(f"E/ExHentai image URL is empty: {page_url}")
    return urljoin(page_url, src), _ehentai_reload_url(soup, page_url)


async def download_ehentai_pages_to_zip(
    sess: aiohttp.ClientSession,
    title: str,
    page_links: list[str],
    *,
    gallery_url: str,
    cookie: str,
    concurrency: int = 4,
    proxy: str | None = None,
) -> GalleryDownload:
    if not page_links:
        raise RuntimeError("No E/ExHentai image page links parsed")

    zip_path = _safe_zip_path(title)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    total = len(page_links)
    completed = 0
    logger.info(
        f"[搜本子下载] 开始 E/ExHentai 按页下载: 标题={title}, 页数={total}, "
        f"并发={max(1, concurrency)}, 代理={'启用' if proxy else '未启用'}"
    )

    async def fetch_one(idx: int, page_url: str) -> tuple[int, str, bytes]:
        nonlocal completed
        async with semaphore:
            current_page_url = page_url
            last_exc: BaseException | None = None
            tried_image_urls: set[str] = set()
            for attempt in range(1, EHENTAI_IMAGE_RELOAD_ATTEMPTS + 1):
                image_url, reload_url = await _parse_ehentai_image_page(
                    sess,
                    current_page_url,
                    cookie,
                    gallery_url,
                    proxy=proxy,
                )
                try:
                    if image_url in tried_image_urls and reload_url:
                        current_page_url = reload_url
                        continue
                    tried_image_urls.add(image_url)
                    data, content_type = await _download_bytes(
                        sess,
                        image_url,
                        headers=_ehentai_headers(gallery_url, cookie, current_page_url),
                        proxy=proxy,
                    )
                    if not data:
                        raise RuntimeError(f"第 {idx} 页下载为空")
                    if not _looks_like_image(data):
                        raise RuntimeError(f"第 {idx} 页下载内容不是有效图片")
                    ext = guess_ext(image_url, content_type)
                    completed += 1
                    logger.info(
                        f"[搜本子下载] {title}: 已下载 {completed}/{total} 页 "
                        f"(第 {idx} 页, {_format_bytes(len(data))})"
                    )
                    return idx, ext, data
                except Exception as exc:
                    last_exc = exc
                    if not reload_url or attempt >= EHENTAI_IMAGE_RELOAD_ATTEMPTS:
                        break
                    logger.warning(
                        f"[搜本子下载] 第 {idx} 页直链失效，切换 E/ExHentai 备用源 "
                        f"({attempt}/{EHENTAI_IMAGE_RELOAD_ATTEMPTS}): {image_url}"
                    )
                    current_page_url = reload_url
                    await asyncio.sleep(0.4 * attempt)
            raise RuntimeError(f"第 {idx} 页下载失败，已尝试刷新图片源: {last_exc}") from last_exc

    tasks = [fetch_one(idx, url) for idx, url in enumerate(page_links, 1)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda item: item[0])

    logger.info(f"[搜本子下载] {title}: 开始打包 {len(results)} 页 -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, ext, data in results:
            zf.writestr(f"{idx:03d}.{ext}", data)

    logger.info(f"[搜本子下载] {title}: 打包完成 -> {zip_path}")
    _validate_zip(zip_path)
    return GalleryDownload(title=title, zip_path=zip_path)


async def _download_ehentai(
    sess: aiohttp.ClientSession,
    url: str,
    *,
    concurrency: int,
    ehentai_cookie: str,
    exhentai_cookie: str,
    prefer_archive: bool,
    proxy: str | None,
) -> GalleryDownload:
    cookie = _ehentai_cookie_for_url(url, ehentai_cookie, exhentai_cookie)
    logger.info(
        f"[搜本子下载] 开始 E/ExHentai 下载: url={url}, "
        f"cookie={'已配置' if cookie else '未配置'}, "
        f"优先归档={'是' if prefer_archive else '否'}, 代理={'启用' if proxy else '未启用'}"
    )
    gallery_html = await _get_text(
        sess,
        url,
        headers=_ehentai_headers(url, cookie, url),
        proxy=proxy,
    )
    BeautifulSoup = _load_bs4()
    title = _ehentai_title(BeautifulSoup(gallery_html, "html.parser"))

    if prefer_archive:
        logger.info(f"[搜本子下载] {title}: 尝试 E/ExHentai 归档下载")
        archive = await _try_ehentai_archive(
            sess,
            url,
            gallery_html,
            title,
            cookie,
            proxy=proxy,
        )
        if archive:
            return archive
        logger.info(f"[搜本子下载] {title}: 归档下载不可用，切换为逐页下载")

    title, page_links = await _collect_ehentai_page_links(
        sess,
        url,
        gallery_html,
        cookie,
        proxy=proxy,
    )
    if not page_links:
        raise RuntimeError("没有解析到 E/ExHentai 图片页链接，可能需要登录 cookie")
    logger.info(f"[搜本子下载] E/ExHentai 图片页解析完成: {title}, 页数={len(page_links)}")

    return await download_ehentai_pages_to_zip(
        sess,
        title,
        page_links,
        gallery_url=url,
        cookie=cookie,
        concurrency=concurrency,
        proxy=proxy,
    )


async def download_gallery(
    url: str,
    *,
    timeout: int,
    concurrency: int,
    ehentai_cookie: str,
    exhentai_cookie: str,
    prefer_archive: bool,
    proxy: str | None = None,
) -> GalleryDownload:
    host = normalize_host(urlparse(url).netloc)
    logger.info(
        f"[搜本子下载] 收到下载任务: url={url}, host={host or urlparse(url).scheme}, "
        f"timeout={timeout}s, 并发={max(1, concurrency)}, 代理={'启用' if proxy else '未启用'}"
    )
    if urlparse(url).scheme == "jmcomic" or _extract_jm_id(url):
        return await _download_jmcomic(
            url,
            concurrency=concurrency,
            proxy=proxy,
        )

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as sess:
        if host == "panda.chaika.moe":
            return await _download_panda(
                sess,
                url,
                concurrency=concurrency,
                proxy=proxy,
            )
        if host in ("nhentai.net", "nhentai.xxx"):
            return await _download_nhentai(
                sess,
                url,
                concurrency=concurrency,
                proxy=proxy,
            )
        if host in ("e-hentai.org", "exhentai.org"):
            return await _download_ehentai(
                sess,
                url,
                concurrency=concurrency,
                ehentai_cookie=ehentai_cookie,
                exhentai_cookie=exhentai_cookie,
                prefer_archive=prefer_archive,
                proxy=proxy,
            )
    raise RuntimeError(f"暂不支持这个站点: {host}")
