import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .constants import (
    EHENTAI_HOSTS,
    IMAGE_EXTS,
    IWARA_HOSTS,
    JMCOMIC_HOSTS,
    NHENTAI_HOSTS,
    SOURCE_HOST,
    SUPPORTED_HOSTS,
)
from .models import DownloadIntent, SearchEntry


URL_RE = re.compile(r"https?://[^\s<>\"'\\]+", re.I)
ANY_URL_OR_SCHEME_RE = re.compile(r"(?:https?|jmcomic)://[^\s<>\"'\\]+", re.I)
JM_ID_RE = re.compile(r"(?<![a-zA-Z0-9])jm\s*[-_:：]?\s*(\d{3,})(?![a-zA-Z0-9])", re.I)
AUTO_JM_ID_RE = re.compile(r"(?<![a-zA-Z0-9])jm(\d{4,})(?![a-zA-Z0-9])", re.I)
JM_ALBUM_PATH_RE = re.compile(r"/(?:album|photo)/(\d+)(?:[/?#]|$)", re.I)
EHENTAI_PATH_RE = re.compile(r"/(?:g/(\d+)/([0-9a-fA-F]{10})|s/([0-9a-fA-F]+)/(\d+)-(\d+))")
IWARA_PATH_RE = re.compile(r"/(video|image)/([A-Za-z0-9]+)")
DOWNLOAD_COMMAND_RE = re.compile(
    r"^/?(?:下载本子|下载|下本(?:子)?|下(?=第))\s*(?:第\s*)?([0-9一二两三四五六七八九十百]+)\s*(?:个本子|个本|个|本|项|号)?[。.!！~～\s]*$",
    re.I,
)
DIRECT_DOWNLOAD_RE = re.compile(r"^/?(?:下载本子|下载|下本(?:子)?)\s+(.+)$", re.I)
SEARCH_ENTRY_HEADER_RE = re.compile(r"^\s*#\s*(\d+)\s*(?:\[([^\]]+)\])?.*$")
STRICT_SEARCH_ENTRY_HEADER_RE = re.compile(
    r"^\s*#\s*(\d+)\s*\[([^\]]+)\]\s*(?:⚠\s*低相似度\s*)?相似度\s*([0-9.]+)%?\s*$"
)
WINDOWS_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def strip_url_token(text: str) -> str:
    return text.strip().strip(" \t\r\n<>\"'，。；;、)）】]」』")


def normalize_host(host: str) -> str:
    host = host.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_jm_host(host: str) -> bool:
    return host in JMCOMIC_HOSTS or "18comic" in host or "jmcomic" in host


def extract_jm_id_from_url(url: str) -> str | None:
    parsed = urlparse(strip_url_token(url))
    if parsed.scheme == "jmcomic":
        if parsed.netloc in ("album", "photo"):
            target_id = parsed.path.strip("/")
            return target_id if target_id.isdigit() else None
        match = re.search(r"/(?:album|photo)/(\d+)(?:[/?#]|$)", parsed.path, re.I)
        return match.group(1) if match else None
    host = normalize_host(parsed.netloc)
    path = unquote(parsed.path or "")
    match = JM_ALBUM_PATH_RE.search(path)
    if match and is_jm_host(host):
        return match.group(1)
    return None


def is_supported_url(url: str) -> bool:
    try:
        cleaned = strip_url_token(url)
        parsed = urlparse(cleaned)
        if parsed.scheme == "jmcomic":
            return extract_jm_id_from_url(cleaned) is not None
        host = normalize_host(parsed.netloc)
        if host in EHENTAI_HOSTS:
            return EHENTAI_PATH_RE.search(parsed.path or "") is not None
        if host in NHENTAI_HOSTS:
            return bool(re.search(r"/g/\d+", parsed.path or ""))
        if host == "panda.chaika.moe":
            return "/archive/" in (parsed.path or "") or "archive=" in (parsed.query or "")
        if host in IWARA_HOSTS:
            return IWARA_PATH_RE.search(parsed.path or "") is not None
        if is_jm_host(host):
            return extract_jm_id_from_url(cleaned) is not None
        return host in SUPPORTED_HOSTS
    except Exception:
        return False


def first_supported_url(text: str) -> str | None:
    for match in ANY_URL_OR_SCHEME_RE.finditer(text):
        url = strip_url_token(match.group(0))
        if is_supported_url(url):
            return url
    return None


def first_jm_url(text: str) -> str | None:
    for match in ANY_URL_OR_SCHEME_RE.finditer(text):
        url = strip_url_token(match.group(0))
        if extract_jm_id_from_url(url):
            return url
    return None


def first_jm_id(text: str) -> str | None:
    url = first_jm_url(text)
    if url:
        return extract_jm_id_from_url(url)
    # 剔除所有 URL 后再找纯 jm 号，避免其他站点 URL 参数/路径偶然含 jm123 被误劫持。
    text_without_urls = ANY_URL_OR_SCHEME_RE.sub(" ", text)
    match = JM_ID_RE.search(text_without_urls)
    if match:
        return match.group(1)
    return None


def extract_parse_targets(text: str) -> list[str]:
    """从消息文本中提取所有可自动解析的目标（支持链接与 jm 车号），去重保持顺序。"""
    if not text:
        return []
    targets: list[str] = []
    seen: set[str] = set()

    for match in ANY_URL_OR_SCHEME_RE.finditer(text):
        url = strip_url_token(match.group(0))
        if is_supported_url(url) and url not in seen:
            seen.add(url)
            targets.append(url)

    text_without_urls = ANY_URL_OR_SCHEME_RE.sub(" ", text)
    for match in AUTO_JM_ID_RE.finditer(text_without_urls):
        jm_id = match.group(1).lstrip("0")
        if not jm_id:
            continue
        url = f"jmcomic://album/{jm_id}"
        if url not in seen:
            seen.add(url)
            targets.append(url)

    return targets


def parse_chinese_number(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    total = 0
    current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    total += current
    return total or None


def _intent_from_fragment(fragment: str) -> DownloadIntent | None:
    # 先匹配明确受支持的 URL，避免 URL 里带 jm+数字 被误当成 JM 号。
    url = first_supported_url(fragment)
    if url:
        jm_id = extract_jm_id_from_url(url)
        if jm_id and urlparse(url).scheme == "jmcomic":
            return DownloadIntent(jm_id=jm_id)
        return DownloadIntent(url=url)
    jm_id = first_jm_id(fragment)
    if jm_id:
        return DownloadIntent(jm_id=jm_id)
    return None


def parse_download_intent(text: str) -> DownloadIntent | None:
    text = text.strip()
    if not text:
        return None

    cmd_match = DIRECT_DOWNLOAD_RE.match(text)
    if cmd_match:
        intent = _intent_from_fragment(cmd_match.group(1))
        if intent:
            return intent

    if any(word in text for word in ("下载", "下本")):
        intent = _intent_from_fragment(text)
        if intent:
            return intent

    match = DOWNLOAD_COMMAND_RE.match(text)
    if not match:
        return None
    index = parse_chinese_number(match.group(1))
    if not index or index < 1:
        return None
    return DownloadIntent(index=index)


def sanitize_filename(name: str, fallback: str = "download") -> str:
    cleaned = html.unescape(name or "").strip()
    cleaned = WINDOWS_FORBIDDEN_CHARS_RE.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:120].strip(" .") or fallback


def guess_ext(url: str, content_type: str = "") -> str:
    ext = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if ext in {item.lstrip(".") for item in IMAGE_EXTS}:
        return "jpg" if ext == "jpeg" else ext
    content_type = content_type.lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/avif": "avif",
        "image/bmp": "bmp",
    }
    return mapping.get(content_type, "jpg")


def is_zip_bytes(data: bytes) -> bool:
    return data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06")


def entry_url(item: dict) -> str:
    path = item.get("subjectPath") or item.get("pagePath") or ""
    host = SOURCE_HOST.get(item.get("source", ""), "soutubot.moe")
    if not path:
        return ""
    if path.startswith(("http://", "https://", "jmcomic://")):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"https://{host}{path}"


def search_entry_from_item(index: int, item: dict) -> SearchEntry:
    raw_alts = item.get("alternatives") or []
    alternatives = [
        (str(src), str(u))
        for src, u in raw_alts
        if isinstance(src, str) and isinstance(u, str) and u
    ]
    return SearchEntry(
        index=index,
        title=str(item.get("title") or "(无标题)"),
        source=str(item.get("source") or "unknown"),
        url=entry_url(item),
        similarity=float(item.get("similarity", 0) or 0),
        preview_url=str(item.get("previewImageUrl") or ""),
        alternatives=alternatives,
    )


def line_value_after_label(line: str, labels: tuple[str, ...]) -> str | None:
    stripped = line.strip()
    for label in labels:
        for separator in (":", "："):
            prefix = f"{label}{separator}"
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
    return None


def parse_search_entries_from_text(text: str) -> list[SearchEntry]:
    entries: list[SearchEntry] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if not current:
            return
        url = current.get("url") or first_supported_url(current.get("raw", ""))
        if url:
            entries.append(
                SearchEntry(
                    index=int(current["index"]),
                    title=current.get("title") or "(无标题)",
                    source=current.get("source") or source_from_url(url),
                    url=url,
                    similarity=float(current.get("similarity") or 0),
                )
            )
        current = None

    for line in text.splitlines():
        strict_header = STRICT_SEARCH_ENTRY_HEADER_RE.match(line)
        header = strict_header or SEARCH_ENTRY_HEADER_RE.match(line)
        # 防止多行标题里的 "#数字" 误拆条目：仅在严格匹配，或当前尚未记录链接/来源时开启新条目
        is_new_entry = bool(strict_header) or (
            header is not None
            and (
                current is None
                or bool(current.get("url"))
                or bool(current.get("has_explicit_source"))
                or "相似度" in line
            )
        )
        if header and is_new_entry:
            flush()
            current = {
                "index": int(header.group(1)),
                "source": (header.group(2) or "").strip(),
                "raw": line + "\n",
                "has_explicit_source": False,
            }
            if strict_header:
                current["similarity"] = float(strict_header.group(3))
            else:
                similarity_match = re.search(r"相似度\s*([0-9.]+)", line)
                if similarity_match:
                    current["similarity"] = float(similarity_match.group(1))
            continue
        if current is None:
            continue
        current["raw"] += line + "\n"
        title_value = line_value_after_label(line, ("标题",))
        source_value = line_value_after_label(line, ("来源",))
        link_value = line_value_after_label(line, ("链接",))
        if title_value is not None:
            current["title"] = title_value
        elif link_value is not None:
            url_match = ANY_URL_OR_SCHEME_RE.search(link_value)
            if url_match:
                current["url"] = strip_url_token(url_match.group(0))
        elif source_value is not None:
            current["source"] = source_value
            current["has_explicit_source"] = True
    flush()
    return entries


def source_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = normalize_host(parsed.netloc)
    if parsed.scheme == "jmcomic" or is_jm_host(host) or extract_jm_id_from_url(url):
        return "jmcomic"
    if host in NHENTAI_HOSTS:
        return "nhentai"
    if host == "e-hentai.org":
        return "ehentai"
    if host == "exhentai.org":
        return "exhentai"
    if host == "panda.chaika.moe":
        return "panda"
    if host in IWARA_HOSTS:
        return "iwara"
    return host or "unknown"


def _iter_json_strings(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_json_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_strings(item)
    elif isinstance(obj, str):
        yield obj


def extract_json_url(data: dict | str) -> str | None:
    """从 QQ JSON 分享卡片中提取目标链接。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    priority_paths = (
        ("miniapp", "jumpUrl"),
        ("miniapp", "url"),
        ("detail_1", "qqdocurl"),
        ("detail_1", "jumpUrl"),
        ("detail_1", "url"),
        ("news", "jumpUrl"),
        ("news", "url"),
    )
    for key1, key2 in priority_paths:
        node = meta.get(key1)
        if isinstance(node, dict) and isinstance(node.get(key2), str):
            cleaned = unquote(node[key2].strip().replace("\\/", "/"))
            found = first_supported_url(cleaned)
            if found:
                return found

    for value in _iter_json_strings(data):
        cleaned = unquote(value.strip().replace("\\/", "/"))
        found = first_supported_url(cleaned)
        if found:
            return found
    return None


def extract_text_from_onebot_segments(segments: Any) -> str:
    if isinstance(segments, str):
        raw = segments.strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except Exception:
            # 去掉 CQ 码噪音但保留文本
            return re.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
        return extract_text_from_onebot_segments(parsed)
    if isinstance(segments, dict):
        seg_type = segments.get("type")
        data = segments.get("data") if isinstance(segments.get("data"), dict) else {}
        if seg_type in ("text", "plain"):
            return str(data.get("text") or segments.get("text") or "")
        if seg_type in ("node", "nodes", "forward"):
            inline_nodes = data.get("content") or data.get("messages") or segments.get("content")
            if isinstance(inline_nodes, list):
                return extract_text_from_forward_nodes(inline_nodes)
        if seg_type == "json":
            card_url = extract_json_url(data.get("data") or segments.get("data") or "")
            return card_url or ""
        if "message" in segments or "content" in segments:
            return extract_text_from_onebot_segments(
                segments.get("message") or segments.get("content")
            )
        return ""
    if not isinstance(segments, list):
        if hasattr(segments, "text") and isinstance(getattr(segments, "text"), str):
            return getattr(segments, "text")
        return ""
    parts: list[str] = []
    for seg in segments:
        if isinstance(seg, str):
            parts.append(seg)
            continue
        if not isinstance(seg, dict):
            if hasattr(seg, "text") and isinstance(getattr(seg, "text"), str):
                parts.append(getattr(seg, "text"))
            elif hasattr(seg, "data"):
                raw_data = getattr(seg, "data")
                if isinstance(raw_data, (dict, str)):
                    card_url = extract_json_url(raw_data)
                    if card_url:
                        parts.append(card_url)
            continue
        seg_type = seg.get("type")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type in ("text", "plain"):
            parts.append(str(data.get("text") or seg.get("text") or ""))
        elif seg_type in ("node", "nodes", "forward"):
            nested_raw = data.get("content") or data.get("messages") or seg.get("content")
            nested = (
                extract_text_from_forward_nodes(nested_raw)
                if isinstance(nested_raw, list)
                else extract_text_from_onebot_segments(nested_raw)
            )
            if nested:
                parts.append(nested)
        elif seg_type == "json":
            card_url = extract_json_url(data.get("data") or "")
            if card_url:
                parts.append(card_url)
    return "".join(parts).strip()


def unwrap_onebot_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def extract_text_from_forward_nodes(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    texts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        content = node.get("message")
        if content is None:
            content = node.get("content")
        if content is None:
            node_data = node.get("data") if isinstance(node.get("data"), dict) else {}
            content = node_data.get("message") or node_data.get("content")
        text = extract_text_from_onebot_segments(content)
        if text:
            texts.append(text)
    return "\n".join(texts)


def extract_text_from_forward_payload(payload: Any) -> str:
    data = unwrap_onebot_payload(payload)
    nodes = (
        data.get("messages")
        or data.get("message")
        or data.get("nodes")
        or data.get("nodeList")
    )
    if isinstance(nodes, list):
        return extract_text_from_forward_nodes(nodes)
    return extract_text_from_onebot_segments(nodes)
