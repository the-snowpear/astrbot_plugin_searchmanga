import base64
import json

import aiohttp

from .constants import DEFAULT_UA


SOUTUBOT_BASE = "https://soutubot.moe"
SEARCH_ENDPOINT = f"{SOUTUBOT_BASE}/api/search"
OK_STATUSES = {"ok", "partial"}


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_hit(hit: dict) -> dict | None:
    """把 soutubot v2 的命中条目转换成插件内部沿用的旧结构。

    v2 的 results[] 结构为::

        {"score": 33.67, "path_segments": [
            {"source_key": "nhentai",
             "source_url": "https://nhentai.net/g/192882",
             "thumbnail_url": "https://.../028.webp",
             "metadata": {"source": {...}, "title": {"primary": "..."}}}], ...}

    旧结构为 {title, source, similarity, pagePath, previewImageUrl}，
    main.py / parsing.py 仍按旧结构读取，这里做一次归一化。
    """
    segments = hit.get("path_segments")
    if not isinstance(segments, list):
        return None
    segment = next((s for s in segments if isinstance(s, dict)), None)
    if segment is None:
        return None

    metadata = _as_dict(segment.get("metadata"))
    source_metadata = _as_dict(metadata.get("source"))
    title_metadata = _as_dict(metadata.get("title"))

    source = str(segment.get("source_key") or source_metadata.get("key") or "unknown")
    title = str(
        title_metadata.get("primary")
        or title_metadata.get("japanese_or_alias")
        or title_metadata.get("romanized")
        or ""
    ).strip()
    if not title:
        external_id = str(segment.get("external_id") or "").strip()
        title = f"{source} {external_id}".strip()

    url = (
        source_metadata.get("url")
        or segment.get("source_url")
        or segment.get("page_url")
        or segment.get("chapter_url")
        or ""
    )

    try:
        similarity = round(float(hit.get("score") or 0), 2)
    except (TypeError, ValueError):
        similarity = 0.0

    return {
        "title": title or "(无标题)",
        "source": source,
        "similarity": similarity,
        "pagePath": str(url),
        "previewImageUrl": str(segment.get("thumbnail_url") or ""),
    }


def normalize_results(results: object) -> list[dict]:
    """归一化 + 按作品去重（同一画册的多页命中只保留相似度最高的一条）。"""
    if not isinstance(results, list):
        return []

    deduped: dict[str, dict] = {}
    for hit in results:
        if not isinstance(hit, dict):
            continue
        item = normalize_hit(hit)
        if not item:
            continue
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
        from pathlib import Path

        return Path(file_path).read_bytes()

    from pathlib import Path

    try:
        local_p = Path(url_clean)
        if local_p.exists() and local_p.is_file():
            return local_p.read_bytes()
    except Exception:
        pass

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
        async with sess.get(url_clean, headers={"User-Agent": DEFAULT_UA}, proxy=proxy) as resp:
            resp.raise_for_status()
            return await resp.read()


async def search_soutubot(
    image_bytes: bytes,
    factor: float,
    timeout: int,
    proxy: str | None = None,
) -> dict:
    """调用 soutubot v2 检索接口。

    v2 取消了首页 challenge 参数与 X-API-KEY 鉴权，直接 multipart POST
    /api/search 即可，响应中的命中结果位于 ``results`` 字段。
    返回值为兼容旧版的结构：``{"data": [归一化后的条目...]}``。
    """
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
