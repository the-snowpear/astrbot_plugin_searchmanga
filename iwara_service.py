from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

import aiohttp

from .constants import DEFAULT_HEADERS, NSFW_POLICY_BLUR, NSFW_POLICY_IGNORE, NSFW_POLICY_SEND
from .models import ParsedMedia
from .packaging import blur_image_file, format_bytes, safe_output_path, temp_root, verify_image_bytes
from .parsing import guess_ext

IWARA_TARGET_RE = re.compile(
    r"https?://(?:www\.|ecchi\.)?iwara\.tv/(video|image)/([A-Za-z0-9_-]+)",
    re.I,
)
IWARA_SALT = "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"


@dataclass(slots=True)
class IwaraDownloadResult:
    media: ParsedMedia
    image_paths: list[Path]
    video_path: Path | None = None


def parse_iwara_target(url: str) -> tuple[str, str] | None:
    m = IWARA_TARGET_RE.search(str(url or "").strip())
    if not m:
        return None
    return (m.group(1).lower(), m.group(2))


def compute_iwara_x_version(file_id: str, expires: str) -> str:
    raw = f"{file_id}_{expires}_{IWARA_SALT}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _normalize_iwara_url(url: str) -> str:
    u = str(url or "").strip()
    if u.startswith("//"):
        return f"https:{u}"
    return u


def _build_iwara_headers(iwara_cookie: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = "https://www.iwara.tv/"
    headers["Origin"] = "https://www.iwara.tv"
    token_or_cookie = str(iwara_cookie or "").strip()
    if token_or_cookie:
        if "=" in token_or_cookie:
            headers["Cookie"] = token_or_cookie
        else:
            auth = token_or_cookie if token_or_cookie.lower().startswith("bearer ") else f"Bearer {token_or_cookie}"
            headers["Authorization"] = auth
    if extra:
        headers.update(extra)
    return headers


async def _iwara_request(
    url: str,
    *,
    headers: dict[str, str],
    proxy: str | None = None,
    timeout: int = 60,
) -> tuple[int, bytes, dict[str, str]]:
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
            resp = await session.get(url, headers=headers, proxy=proxy)
            resp_headers = {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}
            return resp.status_code, bytes(resp.content or b""), resp_headers
    except ImportError:
        client_timeout = aiohttp.ClientTimeout(total=max(15, int(timeout)))
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
                raw = await resp.read()
                resp_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                return resp.status, raw, resp_headers


async def _iwara_download_stream(
    url: str,
    dest: Path,
    *,
    headers: dict[str, str],
    proxy: str | None = None,
    timeout: int = 120,
    max_bytes: int = 0,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
            resp = await session.get(url, headers=headers, proxy=proxy, stream=True)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
            cl = int(resp.headers.get("content-length", "0") or 0)
            if max_bytes > 0 and cl > max_bytes:
                raise ValueError(f"SIZE_EXCEEDED:{cl}")
            written = 0
            with dest.open("wb") as fp:
                async for chunk in resp.aiter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes > 0 and written > max_bytes:
                        fp.close()
                        dest.unlink(missing_ok=True)
                        raise ValueError(f"SIZE_EXCEEDED:{written}")
                    fp.write(chunk)
            return dest
    except ImportError:
        client_timeout = aiohttp.ClientTimeout(total=max(30, int(timeout)))
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                cl = int(resp.headers.get("Content-Length", "0") or 0)
                if max_bytes > 0 and cl > max_bytes:
                    raise ValueError(f"SIZE_EXCEEDED:{cl}")
                written = 0
                with dest.open("wb") as fp:
                    async for chunk in resp.content.iter_chunked(65536):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_bytes > 0 and written > max_bytes:
                            fp.close()
                            dest.unlink(missing_ok=True)
                            raise ValueError(f"SIZE_EXCEEDED:{written}")
                        fp.write(chunk)
                return dest


def _extract_cover_url(data: dict[str, Any]) -> str:
    custom = data.get("customThumbnail")
    if isinstance(custom, dict) and custom.get("id") and custom.get("name"):
        return f"https://i.iwara.tv/image/original/{custom['id']}/{custom['name']}"

    file_obj = data.get("file")
    if isinstance(file_obj, dict) and file_obj.get("id"):
        thumb_idx = int(data.get("thumbnail") or 0)
        return f"https://i.iwara.tv/image/thumbnail/{file_obj['id']}/thumbnail-{thumb_idx:02d}.jpg"
    return ""


def _order_video_candidates(
    sources: list[dict[str, Any]],
    preferred_quality: str,
) -> list[dict[str, Any]]:
    pref = (preferred_quality or "540").strip().lower()
    priority_list = [pref, "540", "source", "360", "preview"]
    ranked: list[tuple[int, dict[str, Any]]] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        name = str(src.get("name") or "").strip().lower()
        src_obj = src.get("src") if isinstance(src.get("src"), dict) else {}
        dl_url = _normalize_iwara_url(src_obj.get("download") or src_obj.get("view") or "")
        if not dl_url:
            continue
        rank = priority_list.index(name) if name in priority_list else len(priority_list)
        ranked.append((rank, {"name": name or "video", "url": dl_url, "size": int(src.get("size") or 0)}))
    ranked.sort(key=lambda item: item[0])
    return [item[1] for item in ranked]


async def _fetch_iwara_video(
    item_id: str,
    work_dir: Path,
    *,
    timeout: int,
    proxy: str | None,
    iwara_cookie: str,
    video_quality: str,
    max_size_mb: int,
    nsfw_policy: str,
    download_media: bool,
) -> IwaraDownloadResult:
    headers = _build_iwara_headers(iwara_cookie)
    status, raw, _ = await _iwara_request(
        f"https://api.iwara.tv/video/{item_id}",
        headers=headers,
        proxy=proxy,
        timeout=timeout,
    )
    if status >= 400:
        raise RuntimeError(f"Iwara 视频信息请求失败: HTTP {status}")

    data = json.loads(raw.decode("utf-8", errors="replace"))
    rating = str(data.get("rating") or "general").strip().lower()
    is_nsfw = rating == "ecchi"
    title = str(data.get("title") or f"Iwara_{item_id}").strip()
    user_obj = data.get("user") if isinstance(data.get("user"), dict) else {}
    author = str(user_obj.get("name") or user_obj.get("username") or "").strip()
    body = str(data.get("body") or "").strip()
    tags = [str(t.get("id") or "") for t in data.get("tags") or [] if isinstance(t, dict) and t.get("id")]
    canonical_url = f"https://www.iwara.tv/video/{item_id}"

    extra_parts = [f"ID: {item_id}", f"分级: {'R18 / Ecchi' if is_nsfw else '全年龄'}"]
    if tags:
        extra_parts.append(f"标签: {', '.join(tags[:12])}")

    if is_nsfw and nsfw_policy == NSFW_POLICY_IGNORE:
        media = ParsedMedia(
            platform="iwara",
            title=title,
            url=canonical_url,
            author=author,
            summary=body,
            extra_info="\n".join(extra_parts),
            is_nsfw=True,
            ignored_reason="该 Iwara 视频标记为 R18/Ecchi，当前 nsfw_policy=ignore 已忽略",
            work_dir=str(work_dir),
        )
        return IwaraDownloadResult(media=media, image_paths=[], video_path=None)

    cover_url = _extract_cover_url(data)
    cover_bytes: bytes | None = None
    if cover_url:
        try:
            c_st, c_raw, _ = await _iwara_request(cover_url, headers=headers, proxy=proxy, timeout=timeout)
            if c_st < 400 and c_raw:
                verify_image_bytes(c_raw)
                cover_bytes = c_raw
        except Exception:
            cover_bytes = None

    video_path: Path | None = None
    skipped_reason = ""
    file_url = _normalize_iwara_url(data.get("fileUrl") or "")
    file_obj = data.get("file") if isinstance(data.get("file"), dict) else {}
    file_id = str(file_obj.get("id") or "").strip()

    if download_media and file_url:
        parsed_file_url = urlparse(file_url)
        if not file_id:
            parts = [p for p in parsed_file_url.path.split("/") if p]
            file_id = parts[-1] if parts else ""
        expires = parse_qs(parsed_file_url.query).get("expires", [""])[0]
        if not (file_id and expires):
            skipped_reason = "Iwara 视频 fileUrl 缺少签名参数，无法获取清晰度列表"
        else:
            x_ver = compute_iwara_x_version(file_id, expires)
            f_headers = _build_iwara_headers(iwara_cookie, {"X-Version": x_ver})
            f_st, f_raw, _ = await _iwara_request(file_url, headers=f_headers, proxy=proxy, timeout=timeout)
            if f_st >= 400:
                raise RuntimeError(f"Iwara 视频源文件列表请求失败: HTTP {f_st}")
            sources = json.loads(f_raw.decode("utf-8", errors="replace"))
            if isinstance(sources, list):
                candidates = _order_video_candidates(sources, video_quality)
                max_bytes = max(0, int(max_size_mb)) * 1024 * 1024
                last_exceeded: str | None = None
                for cand in candidates:
                    if max_bytes > 0 and cand["size"] > max_bytes:
                        last_exceeded = f"视频 ({cand['name']}, {format_bytes(cand['size'])}) 超过上限 {max_size_mb} MB"
                        continue
                    dest = work_dir / f"{Path(safe_output_path(title, '.mp4')).stem}_{cand['name']}.mp4"
                    try:
                        video_path = await _iwara_download_stream(
                            cand["url"],
                            dest,
                            headers=headers,
                            proxy=proxy,
                            timeout=max(60, int(timeout)),
                            max_bytes=max_bytes,
                        )
                        last_exceeded = None
                        break
                    except ValueError as size_err:
                        if str(size_err).startswith("SIZE_EXCEEDED:"):
                            actual = int(str(size_err).split(":", 1)[1])
                            last_exceeded = f"视频 ({cand['name']}, {format_bytes(actual)}) 超过上限 {max_size_mb} MB"
                            continue
                        raise
                if video_path is None and last_exceeded:
                    skipped_reason = f"{last_exceeded}，已跳过下载"

    media = ParsedMedia(
        platform="iwara",
        title=title,
        url=canonical_url,
        author=author,
        summary=body,
        extra_info="\n".join(extra_parts),
        cover_bytes=cover_bytes,
        cover_url=cover_url,
        video_path=str(video_path) if video_path else None,
        is_nsfw=is_nsfw,
        skipped_download_reason=skipped_reason,
        work_dir=str(work_dir),
    )
    return IwaraDownloadResult(media=media, image_paths=[], video_path=video_path)


async def _fetch_iwara_images(
    item_id: str,
    work_dir: Path,
    *,
    timeout: int,
    proxy: str | None,
    iwara_cookie: str,
    nsfw_policy: str,
    download_media: bool,
    preview_limit: int,
    max_pages: int,
) -> IwaraDownloadResult:
    headers = _build_iwara_headers(iwara_cookie)
    status, raw, _ = await _iwara_request(
        f"https://api.iwara.tv/image/{item_id}",
        headers=headers,
        proxy=proxy,
        timeout=timeout,
    )
    if status >= 400:
        raise RuntimeError(f"Iwara 图集信息请求失败: HTTP {status}")

    data = json.loads(raw.decode("utf-8", errors="replace"))
    rating = str(data.get("rating") or "general").strip().lower()
    is_nsfw = rating == "ecchi"
    title = str(data.get("title") or f"Iwara_Image_{item_id}").strip()
    user_obj = data.get("user") if isinstance(data.get("user"), dict) else {}
    author = str(user_obj.get("name") or user_obj.get("username") or "").strip()
    body = str(data.get("body") or "").strip()
    tags = [str(t.get("id") or "") for t in data.get("tags") or [] if isinstance(t, dict) and t.get("id")]
    files = [f for f in data.get("files") or [] if isinstance(f, dict) and f.get("id")]
    canonical_url = f"https://www.iwara.tv/image/{item_id}"

    extra_parts = [
        f"ID: {item_id}",
        f"分级: {'R18 / Ecchi' if is_nsfw else '全年龄'}",
        f"图片数量: {len(files)}",
    ]
    if tags:
        extra_parts.append(f"标签: {', '.join(tags[:12])}")

    if is_nsfw and nsfw_policy == NSFW_POLICY_IGNORE:
        media = ParsedMedia(
            platform="iwara",
            title=title,
            url=canonical_url,
            author=author,
            summary=body,
            extra_info="\n".join(extra_parts),
            page_count=len(files),
            is_nsfw=True,
            ignored_reason="该 Iwara 图集标记为 R18/Ecchi，当前 nsfw_policy=ignore 已忽略",
            work_dir=str(work_dir),
        )
        return IwaraDownloadResult(media=media, image_paths=[])

    use_blur = is_nsfw and nsfw_policy == NSFW_POLICY_BLUR
    target_files = files
    skipped_reason = ""
    if download_media:
        if max_pages > 0 and len(files) > max_pages:
            skipped_reason = f"图片数量 {len(files)} 超过上限 {max_pages}，已跳过全量下载"
            target_files = []
    elif preview_limit > 0:
        target_files = files[:preview_limit]
    else:
        target_files = files[:1]

    image_paths: list[Path] = []
    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for idx, file_entry in enumerate(target_files, start=1):
        fid = str(file_entry.get("id") or "").strip()
        fname = str(file_entry.get("name") or f"{idx}.jpg").strip()
        if not fid:
            continue
        if use_blur:
            stem = Path(fname).stem or str(idx)
            img_url = f"https://i.iwara.tv/image/large/{fid}/{stem}.jpg"
        else:
            img_url = f"https://i.iwara.tv/image/original/{fid}/{fname}"
        st, img_raw, ct = await _iwara_request(img_url, headers=headers, proxy=proxy, timeout=timeout)
        if st >= 400 and use_blur:
            # Fallback to original if large JPG variant is missing
            img_url = f"https://i.iwara.tv/image/original/{fid}/{fname}"
            st, img_raw, ct = await _iwara_request(img_url, headers=headers, proxy=proxy, timeout=timeout)
        if st >= 400 or not img_raw:
            continue
        raw_ext = verify_image_bytes(img_raw) or guess_ext(img_url, ct)
        ext = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
        dest = pages_dir / f"{idx:05d}{ext}"
        dest.write_bytes(img_raw)
        if use_blur:
            dest = blur_image_file(dest)
        image_paths.append(dest)

    cover_bytes: bytes | None = None
    if image_paths:
        try:
            cover_bytes = image_paths[0].read_bytes()
        except Exception:
            cover_bytes = None

    media = ParsedMedia(
        platform="iwara",
        title=title,
        url=canonical_url,
        author=author,
        summary=body,
        extra_info="\n".join(extra_parts),
        cover_bytes=cover_bytes,
        image_paths=image_paths if (download_media or preview_limit > 0) else [],
        page_count=len(files),
        is_nsfw=is_nsfw,
        skipped_download_reason=skipped_reason,
        work_dir=str(work_dir),
    )
    return IwaraDownloadResult(media=media, image_paths=media.image_paths)


async def fetch_iwara_media(
    target_url: str,
    *,
    timeout: int = 120,
    proxy: str | None = None,
    iwara_cookie: str = "",
    video_quality: str = "540",
    max_size_mb: int = 100,
    nsfw_policy: str = NSFW_POLICY_SEND,
    download_media: bool = True,
    preview_limit: int = 0,
    max_pages: int = 0,
    work_dir: Path | None = None,
) -> IwaraDownloadResult:
    """Parse and optionally download an Iwara video or image gallery."""
    parsed = parse_iwara_target(target_url)
    if parsed is None:
        raise ValueError(f"无法识别 Iwara 链接: {target_url}")
    kind, item_id = parsed

    if nsfw_policy not in {NSFW_POLICY_SEND, NSFW_POLICY_BLUR, NSFW_POLICY_IGNORE}:
        nsfw_policy = NSFW_POLICY_SEND

    if work_dir is None:
        work_dir = temp_root() / f"iwara_{kind}_{item_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    if kind == "video":
        return await _fetch_iwara_video(
            item_id,
            work_dir,
            timeout=timeout,
            proxy=proxy,
            iwara_cookie=iwara_cookie,
            video_quality=video_quality,
            max_size_mb=max_size_mb,
            nsfw_policy=nsfw_policy,
            download_media=download_media,
        )
    return await _fetch_iwara_images(
        item_id,
        work_dir,
        timeout=timeout,
        proxy=proxy,
        iwara_cookie=iwara_cookie,
        nsfw_policy=nsfw_policy,
        download_media=download_media,
        preview_limit=preview_limit,
        max_pages=max_pages,
    )
