import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .constants import DEFAULT_UA


SOUTUBOT_BASE = "https://soutubot.moe"
SEARCH_ENDPOINT = f"{SOUTUBOT_BASE}/api/search"
OK_STATUSES = {"ok", "partial"}

GALLERY_PRIORITY = {
    "ehentai": 0,
    "nhentai": 1,
    "panda": 2,
    "jmcomic": 3,
}
BOORU_PRIORITY = {
    "gelbooru": 0,
    "danbooru": 1,
}
GALLERY_FAMILIES = {"nhentai", "ehentai", "panda", "jmcomic"}


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _clean_url(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith(("http://", "https://", "jmcomic://")):
        return text
    return ""


def _segment_source_key(segment: dict) -> str:
    metadata = _as_dict(segment.get("metadata"))
    source_meta = _as_dict(metadata.get("source"))
    post_meta = _as_dict(metadata.get("post"))
    key = (
        source_meta.get("key")
        or post_meta.get("source_key")
        or metadata.get("source_key")
        or segment.get("source_key")
        or "unknown"
    )
    return str(key).strip().lower() or "unknown"


def _segment_source_id(segment: dict) -> str:
    metadata = _as_dict(segment.get("metadata"))
    source_meta = _as_dict(metadata.get("source"))
    post_meta = _as_dict(metadata.get("post"))
    value = (
        source_meta.get("id")
        or post_meta.get("post_id")
        or metadata.get("external_id")
        or segment.get("external_id")
        or ""
    )
    return str(value).strip()


def _segment_bucket(segment: dict) -> str:
    family = str(segment.get("source_family") or "").strip().lower()
    metadata = _as_dict(segment.get("metadata"))
    kind = str(metadata.get("display_kind") or "").strip().lower()
    key = _segment_source_key(segment)
    if family == "booru" or kind == "booru":
        return "booru"
    if family in GALLERY_FAMILIES or key in GALLERY_PRIORITY or kind == "doujinshi":
        return "gallery"
    return "other"


def _segment_has_metadata(segment: dict) -> bool:
    return segment.get("metadata_status") == "found" and bool(segment.get("metadata"))


def _segment_urls(segment: dict) -> tuple[str, str]:
    """返回 (首选作品/页面链接, 预览缩略图链接)。"""
    metadata = _as_dict(segment.get("metadata"))
    source_meta = _as_dict(metadata.get("source"))
    links = _as_dict(segment.get("links"))

    source_url = (
        _clean_url(source_meta.get("url"))
        or _clean_url(links.get("source_url"))
        or _clean_url(segment.get("source_url"))
    )
    chapter_url = _clean_url(links.get("chapter_url")) or _clean_url(segment.get("chapter_url"))
    page_url = _clean_url(links.get("page_url")) or _clean_url(segment.get("page_url"))

    source_key = _segment_source_key(segment)
    external_id = _segment_source_id(segment)

    # 优先使用画册详情页 URL（source_url），没有时回退章节页/单页，最后按 source_key 兜底。
    url = source_url or chapter_url or page_url
    if not url and external_id:
        if source_key == "jmcomic" and external_id.isdigit():
            url = f"https://18comic.vip/album/{external_id}"
        elif source_key == "nhentai" and external_id.isdigit():
            url = f"https://nhentai.net/g/{external_id}/"
        elif source_key == "panda":
            url = f"https://panda.chaika.moe/archive/{external_id}/"

    thumb = (
        _clean_url(segment.get("thumbnail_url"))
        or _clean_url(links.get("thumbnail_url"))
    )
    return url, thumb


def _segment_title(segment: dict, source: str, external_id: str) -> str:
    metadata = _as_dict(segment.get("metadata"))
    raw_title = metadata.get("title")
    title = ""
    if isinstance(raw_title, dict):
        title = str(
            raw_title.get("primary")
            or raw_title.get("japanese_or_alias")
            or raw_title.get("romanized")
            or ""
        ).strip()
    elif isinstance(raw_title, str):
        title = raw_title.strip()

    if not title:
        title = str(metadata.get("work_title") or "").strip()
    if not title:
        if source == "jmcomic" and external_id:
            title = f"JM{external_id}"
        elif external_id:
            title = f"{source} #{external_id}".strip()
        else:
            title = source or "(无标题)"
    return title or "(无标题)"


def _is_proven_same_image_hit(hit: dict) -> bool:
    segments = hit.get("path_segments")
    if not isinstance(segments, list) or len(segments) < 2:
        return False
    group = hit.get("same_image_group")
    if isinstance(group, dict):
        return group.get("proven") is True
    return "\x1e" in str(hit.get("raw_path") or "")


def _segment_sort_key(segment: dict, bucket: str, orig_idx: int) -> tuple:
    key = _segment_source_key(segment)
    url, _ = _segment_urls(segment)
    has_url = 0 if url else 1
    has_meta = 0 if _segment_has_metadata(segment) else 1
    if bucket == "gallery":
        return (has_url, has_meta, GALLERY_PRIORITY.get(key, 99), orig_idx)
    if bucket == "booru":
        return (has_url, BOORU_PRIORITY.get(key, 99), has_meta, orig_idx)
    return (has_url, has_meta, orig_idx)


def _item_from_segment(
    hit: dict,
    segment: dict,
    sibling_segments: list[dict] | None = None,
) -> dict | None:
    if not isinstance(segment, dict):
        return None

    source = _segment_source_key(segment)
    external_id = _segment_source_id(segment)
    url, thumb = _segment_urls(segment)

    if not thumb and sibling_segments:
        for sib in sibling_segments:
            _, sib_thumb = _segment_urls(sib)
            if sib_thumb:
                thumb = sib_thumb
                break

    title = _segment_title(segment, source, external_id)
    try:
        similarity = round(float(hit.get("score") or 0), 2)
    except (TypeError, ValueError):
        similarity = 0.0

    alternatives: list[tuple[str, str]] = []
    seen_alt = {(source, url)}
    for sib in sibling_segments or ():
        if not isinstance(sib, dict):
            continue
        sib_source = _segment_source_key(sib)
        sib_url, _ = _segment_urls(sib)
        if sib_url and (sib_source, sib_url) not in seen_alt:
            seen_alt.add((sib_source, sib_url))
            alternatives.append((sib_source, sib_url))

    return {
        "title": title,
        "source": source,
        "similarity": similarity,
        "pagePath": url,
        "previewImageUrl": thumb,
        "alternatives": alternatives,
    }


def normalize_hit(hit: dict) -> dict | None:
    """把 soutubot v2 单条命中转换为插件内部结构，支持 links 与多段优先级挑选。"""
    segments = hit.get("path_segments")
    if not isinstance(segments, list):
        return None
    dict_segments = [s for s in segments if isinstance(s, dict)]
    if not dict_segments:
        return None

    # 优先选可下载的画廊分段，并保证有可用 URL 与元数据。
    ranked = sorted(
        enumerate(dict_segments),
        key=lambda pair: (
            0 if _segment_bucket(pair[1]) == "gallery" else 1,
            _segment_sort_key(pair[1], _segment_bucket(pair[1]), pair[0]),
        ),
    )
    primary = ranked[0][1]
    return _item_from_segment(hit, primary, dict_segments)


def normalize_results(results: object) -> list[dict]:
    """对齐 soutubot v2 result_grouping 逻辑：分段展开/分组、按相似度降序并去重。"""
    if not isinstance(results, list):
        return []

    raw_items: list[dict] = []
    for hit in results:
        if not isinstance(hit, dict):
            continue
        segments = hit.get("path_segments")
        if not isinstance(segments, list):
            continue
        dict_segments = [s for s in segments if isinstance(s, dict)]
        if not dict_segments:
            continue

        if not _is_proven_same_image_hit(hit):
            for seg in dict_segments:
                item = _item_from_segment(hit, seg, [seg])
                if item:
                    raw_items.append(item)
            continue

        buckets: dict[str, list[tuple[int, dict]]] = {}
        for idx, seg in enumerate(dict_segments):
            bucket = _segment_bucket(seg)
            group_key = f"other:{idx}" if bucket == "other" else bucket
            buckets.setdefault(group_key, []).append((idx, seg))

        for group_key, pairs in buckets.items():
            bucket = "other" if group_key.startswith("other:") else group_key
            pairs.sort(key=lambda pair: _segment_sort_key(pair[1], bucket, pair[0]))
            ordered_segs = [pair[1] for pair in pairs]
            item = _item_from_segment(hit, ordered_segs[0], ordered_segs)
            if item:
                raw_items.append(item)

    deduped: dict[str, dict] = {}
    for item in raw_items:
        key = item["pagePath"] or f'{item["source"]}:{item["title"]}'
        current = deduped.get(key)
        if current is None or item["similarity"] > current["similarity"]:
            deduped[key] = item

    return sorted(deduped.values(), key=lambda i: i["similarity"], reverse=True)


async def fetch_image_bytes(url: str, timeout: int, proxy: str | None = None) -> bytes:
    url_clean = url.strip()
    if url_clean.startswith("data:image/") and ";base64," in url_clean:
        b64_data = url_clean.split(";base64,", 1)[1]
        return base64.b64decode(b64_data)
    if url_clean.startswith("base64://"):
        return base64.b64decode(url_clean[len("base64://") :])
    if url_clean.startswith("file://"):
        file_path = url_clean[len("file://") :]
        if file_path.startswith("/") and len(file_path) > 3 and file_path[2] == ":":
            file_path = file_path.lstrip("/")
        return Path(file_path).read_bytes()

    try:
        local_p = Path(url_clean)
        if local_p.exists() and local_p.is_file():
            return local_p.read_bytes()
    except Exception:
        pass

    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
    }
    host = (urlparse(url_clean).hostname or "").lower()
    if host == "soutubot.moe" or host.endswith(".soutubot.moe"):
        headers["Referer"] = f"{SOUTUBOT_BASE}/"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
        async with sess.get(url_clean, headers=headers, proxy=proxy) as resp:
            resp.raise_for_status()
            return await resp.read()


async def search_soutubot(
    image_bytes: bytes,
    factor: float,
    timeout: int,
    proxy: str | None = None,
) -> dict:
    """调用 soutubot v2 检索接口，返回归一化后的结果列表。"""
    headers = {
        "User-Agent": DEFAULT_UA,
        "Origin": SOUTUBOT_BASE,
        "Referer": f"{SOUTUBOT_BASE}/",
        "Accept": "application/json",
    }
    form = aiohttp.FormData()
    form.add_field("file", image_bytes, filename="image.png", content_type="image/png")
    form.add_field("factor", str(factor))

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
        async with sess.post(
            SEARCH_ENDPOINT, headers=headers, data=form, proxy=proxy
        ) as resp:
            text = await resp.text()
            http_status = resp.status

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(
            f"soutubot 返回了非 JSON 响应（HTTP {http_status}，可能被风控拦截）: {text[:120]}"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"soutubot 响应格式异常（HTTP {http_status}）")

    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError(
            f"soutubot 响应缺少 results 字段（HTTP {http_status}）: {text[:160]}"
        )

    api_status = str(payload.get("status") or "")
    data = normalize_results(results)
    if not data and api_status not in OK_STATUSES:
        raise RuntimeError(f"soutubot 检索失败: {api_status or 'unknown'}")

    return {"data": data}
