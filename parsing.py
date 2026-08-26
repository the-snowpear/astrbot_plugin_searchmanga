import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .constants import IMAGE_EXTS, JMCOMIC_HOSTS, SOURCE_HOST, SUPPORTED_HOSTS
from .models import DownloadIntent, SearchEntry


URL_RE = re.compile(r"https?://[^\s<>\"]+", re.I)
JM_ID_RE = re.compile(r"\bjm\s*[-_:：]?\s*(\d{3,})\b", re.I)
JM_ALBUM_PATH_RE = re.compile(r"/(?:album|photo)/(\d+)(?:[/?#]|$)", re.I)
DOWNLOAD_COMMAND_RE = re.compile(
    r"^/?(?:下载本子|下载|下本(?:子)?)\s*(?:第\s*)?([0-9一二两三四五六七八九十百]+)\s*(?:个本|个|本|项)?\s*$",
    re.I,
)
DIRECT_DOWNLOAD_RE = re.compile(r"^/?(?:下载本子|下载|下本(?:子)?)\s+(.+)$", re.I)
SEARCH_ENTRY_HEADER_RE = re.compile(r"^\s*#\s*(\d+)\s*(?:\[([^\]]+)\])?.*$")
WINDOWS_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def strip_url_token(text: str) -> str:
    return text.strip().strip(" \t\r\n<>\"'，。；;、)")


def normalize_host(host: str) -> str:
    host = host.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme == "jmcomic":
            return extract_jm_id_from_url(url) is not None
        return normalize_host(parsed.netloc) in SUPPORTED_HOSTS or extract_jm_id_from_url(url) is not None
    except Exception:
        return False


def first_supported_url(text: str) -> str | None:
    for match in URL_RE.finditer(text):
        url = strip_url_token(match.group(0))
        if is_supported_url(url):
            return url
    return None


def extract_jm_id_from_url(url: str) -> str | None:
    parsed = urlparse(strip_url_token(url))
    if parsed.scheme == "jmcomic":
        if parsed.netloc == "album":
            album_id = parsed.path.strip("/")
            return album_id if album_id.isdigit() else None
        match = re.search(r"/album/(\d+)(?:[/?#]|$)", parsed.path, re.I)
        return match.group(1) if match else None
    host = normalize_host(parsed.netloc)
    path = unquote(parsed.path or "")
    match = JM_ALBUM_PATH_RE.search(path)
    if match and (host in JMCOMIC_HOSTS or "18comic" in host or "jmcomic" in host):
        return match.group(1)
    return None


def first_jm_url(text: str) -> str | None:
    for match in URL_RE.finditer(text):
        url = strip_url_token(match.group(0))
        if extract_jm_id_from_url(url):
            return url
    return None


def first_jm_id(text: str) -> str | None:
    url = first_jm_url(text)
    if url:
        return extract_jm_id_from_url(url)
    match = JM_ID_RE.search(text)
    if match:
        return match.group(1)
    return None


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


def parse_download_intent(text: str) -> DownloadIntent | None:
    text = text.strip()
    if not text:
        return None

    cmd_match = DIRECT_DOWNLOAD_RE.match(text)
    if cmd_match:
        direct_text = cmd_match.group(1)
        jm_id = first_jm_id(direct_text)
        if jm_id:
            return DownloadIntent(jm_id=jm_id)
        url = first_supported_url(direct_text)
        if url:
            return DownloadIntent(url=url)

    jm_url = first_jm_url(text)
    if jm_url and any(word in text for word in ("下载", "下本")):
        jm_id = extract_jm_id_from_url(jm_url)
        if jm_id:
            return DownloadIntent(jm_id=jm_id)

    jm_id = first_jm_id(text)
    if jm_id and any(word in text for word in ("下载", "下本")):
        return DownloadIntent(jm_id=jm_id)

    url = first_supported_url(text)
    if url and any(word in text for word in ("下载", "下本")):
        return DownloadIntent(url=url)

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
    }
    return mapping.get(content_type, "jpg")


def is_zip_bytes(data: bytes) -> bool:
    return data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06")


def entry_url(item: dict) -> str:
    path = item.get("subjectPath") or item.get("pagePath") or ""
    host = SOURCE_HOST.get(item.get("source", ""), "soutubot.moe")
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"https://{host}{path}"

def search_entry_from_item(index: int, item: dict) -> SearchEntry:
    return SearchEntry(
        index=index,
        title=str(item.get("title") or "(无标题)"),
        source=str(item.get("source") or "unknown"),
        url=entry_url(item),
        similarity=float(item.get("similarity", 0) or 0),
        preview_url=str(item.get("previewImageUrl") or ""),
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
        header = SEARCH_ENTRY_HEADER_RE.match(line)
        if header:
            flush()
            current = {
                "index": int(header.group(1)),
                "source": header.group(2) or "",
                "raw": line + "\n",
            }
            similarity_match = re.search(r"相似度\s*([0-9.]+)", line)
            if similarity_match:
                current["similarity"] = float(similarity_match.group(1))
            continue
        if current is None:
            continue
        current["raw"] += line + "\n"
        title_value = line_value_after_label(line, ("标题",))
        source_value = line_value_after_label(line, ("来源",))
        if title_value is not None:
            current["title"] = title_value
        elif line.startswith("链接:") or line.startswith("链接："):
            url = first_supported_url(line)
            if url:
                current["url"] = url
        elif source_value is not None:
            current["source"] = source_value
    flush()
    return entries


def source_from_url(url: str) -> str:
    host = normalize_host(urlparse(url).netloc)
    if urlparse(url).scheme == "jmcomic" or host in JMCOMIC_HOSTS or extract_jm_id_from_url(url):
        return "jmcomic"
    if host in ("nhentai.net", "nhentai.xxx"):
        return "nhentai"
    if host == "e-hentai.org":
        return "ehentai"
    if host == "exhentai.org":
        return "exhentai"
    if host == "panda.chaika.moe":
        return "panda"
    return host or "unknown"


def extract_text_from_onebot_segments(segments: Any) -> str:
    if isinstance(segments, str):
        raw = segments.strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw
        return extract_text_from_onebot_segments(parsed)
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type in ("text", "plain"):
            parts.append(str(data.get("text") or ""))
        elif seg_type in ("node", "nodes"):
            parts.append(extract_text_from_forward_nodes(data.get("content") or data.get("messages")))
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
        content = node.get("message") or node.get("content")
        if content is None:
            node_data = node.get("data") if isinstance(node.get("data"), dict) else {}
            content = node_data.get("message") or node_data.get("content")
        text = extract_text_from_onebot_segments(content)
        if text:
            texts.append(text)
    return "\n".join(texts)


def extract_text_from_forward_payload(payload: Any) -> str:
    data = unwrap_onebot_payload(payload)
    nodes = data.get("messages") or data.get("message") or data.get("nodes") or data.get("nodeList")
    return extract_text_from_forward_nodes(nodes)
