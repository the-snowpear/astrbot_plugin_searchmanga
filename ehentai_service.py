from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin, urlparse
import uuid

import aiohttp

from .constants import DEFAULT_HEADERS
from .models import ParsedMedia
from .packaging import safe_output_path, temp_root, validate_zip, verify_image_bytes
from .parsing import guess_ext, is_zip_bytes

EH_GALLERY_RE = re.compile(
    r"https?://(?:www\.)?(e-hentai\.org|exhentai\.org)/g/(\d+)/([0-9a-f]+)",
    re.I,
)
EH_READER_RE = re.compile(
    r"https?://(?:www\.)?(e-hentai\.org|exhentai\.org)/s/([0-9a-f]+)/(\d+)-(\d+)",
    re.I,
)
ARCHIVER_POPUP_RE = re.compile(
    r"https?://(?:e-hentai\.org|exhentai\.org)/archiver\.php\?[^\"'\s)<>]+",
    re.I,
)
SHOWKEY_RE = re.compile(r'var\s+showkey\s*=\s*["\']([0-9a-zA-Z_-]+)["\']')
IMG_SRC_RE = re.compile(r'<img[^>]+id=["\']img["\'][^>]+src=["\']([^"\']+)["\']', re.I)
IMG_SRC_ALT_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\'][^>]+id=["\']img["\']', re.I)
NL_TOKEN_RE = re.compile(r"nl\(\s*['\"]([^'\"]+)['\"]\s*\)")
PAGE_LINK_RE = re.compile(r"[?&]p=(\d+)")
HREF_GALLERY_RE = re.compile(r'href=["\']([^"\']*?/g/(\d+)/([0-9a-f]+)/?)["\']', re.I)
ARCHIVER_ACTION_RE = re.compile(r'action=["\']([^"\']*archiver\.php[^"\']*)["\']', re.I)
ARCHIVER_DONE_URL_RE = re.compile(r'href=["\']([^"\']+\?start=1[^"\']*|https?://[^"\']+hath\.network[^"\']+)["\']', re.I)


class SadPandaError(RuntimeError):
    """Raised when ExHentai serves a SadPanda response or denies access."""


class Quota509Error(RuntimeError):
    """Raised when E-Hentai/ExHentai returns 509 image viewing limit."""


@dataclass(slots=True)
class EhDownloadResult:
    media: ParsedMedia
    image_paths: list[Path]
    archive_zip_path: Path | None = None


def parse_cookie_header(raw_cookie: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(raw_cookie or "").split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        k = key.strip()
        v = value.strip()
        if k and v:
            cookies[k] = v
    return cookies


def format_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)


def is_eh_site_host(host: str) -> bool:
    h = (host or "").lower().split(":", 1)[0]
    return h in {
        "e-hentai.org",
        "www.e-hentai.org",
        "api.e-hentai.org",
        "exhentai.org",
        "s.exhentai.org",
    } or h.endswith(".e-hentai.org") or h.endswith(".exhentai.org")


def cookies_for_host(
    host: str,
    *,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
) -> dict[str, str]:
    """Return site cookies ONLY for official E/ExHentai hosts (never H@H nodes)."""
    h = (host or "").lower().split(":", 1)[0]
    if not is_eh_site_host(h):
        return {}
    if "exhentai.org" in h:
        cookies = parse_cookie_header(exhentai_cookie)
        cookies.setdefault("nw", "1")
        return cookies
    cookies = parse_cookie_header(ehentai_cookie)
    cookies.setdefault("nw", "1")
    return cookies


def has_ex_auth(exhentai_cookie: str) -> bool:
    parsed = parse_cookie_header(exhentai_cookie)
    return bool(parsed.get("ipb_member_id") and parsed.get("ipb_pass_hash"))


def parse_gallery_url(url: str) -> tuple[str, str, str] | None:
    m = EH_GALLERY_RE.search(str(url or ""))
    if not m:
        return None
    return (m.group(1).lower(), m.group(2), m.group(3).lower())


def parse_reader_url(url: str) -> tuple[str, str, str, int] | None:
    m = EH_READER_RE.search(str(url or ""))
    if not m:
        return None
    return (m.group(1).lower(), m.group(2).lower(), m.group(3), int(m.group(4)))


def _site_headers(
    url: str,
    *,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    referer: str | None = None,
) -> dict[str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    cookies = cookies_for_host(
        host,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
    )
    if cookies:
        headers["Cookie"] = format_cookie_header(cookies)
    return headers


def _check_html_errors(html: str, *, host: str, content_type: str = "") -> None:
    ct = (content_type or "").lower()
    if ct.startswith("image/") and "exhentai" in host:
        raise SadPandaError("ExHentai 返回了 SadPanda 图片，Cookie 可能已失效或缺少 igneous")

    lower = (html or "").lower()
    if "your ip address has been temporarily banned" in lower or "excessive requests" in lower:
        raise RuntimeError("E-Hentai/ExHentai 提示请求过于频繁或 IP 被临时限制")
    if "you have exceeded your image viewing limits" in lower or "/img/509.gif" in lower or "/img/509s.gif" in lower:
        raise Quota509Error("E-Hentai/ExHentai 触发 509 配额限制")
    if "this gallery has been removed" in lower or "gallery not found" in lower:
        raise RuntimeError("目标画廊不存在或已被移除")
    if "kokomade" in lower and "<div id=\"gdt\"" not in lower:
        raise RuntimeError("目标画廊在当前地区或账号权限下不可见 (kokomade)")


async def _fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    proxy: str | None = None,
    referer: str | None = None,
) -> tuple[int, str, str]:
    headers = _site_headers(
        url,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        referer=referer,
    )
    async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
        ct = resp.headers.get("Content-Type", "")
        raw = await resp.read()
        if ct.lower().startswith("image/") and "exhentai" in (urlparse(str(resp.url)).hostname or ""):
            raise SadPandaError("ExHentai 返回了 SadPanda 图片，Cookie 无效或未配置 exhentai_cookie")
        text = raw.decode("utf-8", errors="replace")
        return resp.status, text, ct


async def _call_eh_api(
    session: aiohttp.ClientSession,
    host: str,
    payload: dict[str, Any],
    *,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    proxy: str | None = None,
) -> dict[str, Any]:
    api_url = "https://s.exhentai.org/api.php" if "exhentai" in host else "https://api.e-hentai.org/api.php"
    headers = _site_headers(
        api_url,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        referer=f"https://{host}/",
    )
    headers["Content-Type"] = "application/json"
    async with session.post(api_url, json=payload, headers=headers, proxy=proxy) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
        if not isinstance(data, dict):
            raise RuntimeError("E-Hentai API 返回了非 JSON 对象")
        return data


async def resolve_gallery_from_target(
    session: aiohttp.ClientSession,
    target_url: str,
    *,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    proxy: str | None = None,
) -> tuple[str, str, str]:
    if parsed := parse_gallery_url(target_url):
        return parsed

    reader = parse_reader_url(target_url)
    if not reader:
        raise ValueError(f"无法识别 E-Hentai/ExHentai 链接: {target_url}")

    host, imgkey, gid, page_num = reader
    try:
        api_data = await _call_eh_api(
            session,
            host,
            {"method": "gtoken", "pagelist": [[int(gid), imgkey, int(page_num)]]},
            ehentai_cookie=ehentai_cookie,
            exhentai_cookie=exhentai_cookie,
            proxy=proxy,
        )
        tokenlist = api_data.get("tokenlist")
        if isinstance(tokenlist, list) and tokenlist:
            token = str(tokenlist[0].get("token") or "").strip().lower()
            if token:
                return (host, gid, token)
    except Exception:
        pass

    status, html, ct = await _fetch_text(
        session,
        target_url,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        proxy=proxy,
    )
    _check_html_errors(html, host=host, content_type=ct)
    if status >= 400:
        raise RuntimeError(f"访问阅读页失败: HTTP {status}")
    if m := HREF_GALLERY_RE.search(html):
        return (host, m.group(2), m.group(3).lower())
    raise RuntimeError(f"未能从阅读页解析出画廊链接: {target_url}")


def _extract_reader_links_and_max_page(html: str, host: str, gid: str) -> tuple[dict[int, str], int, dict[str, str]]:
    page_map: dict[int, str] = {}
    max_p = 0
    meta: dict[str, str] = {}

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        gn = soup.select_one("#gn")
        gj = soup.select_one("#gj")
        if gn and gn.get_text(strip=True):
            meta["title"] = gn.get_text(strip=True)
        if gj and gj.get_text(strip=True):
            meta["title_jpn"] = gj.get_text(strip=True)

        gdt = soup.select_one("#gdt")
        anchors = gdt.select("a[href]") if gdt else soup.select("a[href*='/s/']")
        for a in anchors:
            href = urljoin(f"https://{host}/", str(a.get("href") or ""))
            parsed = parse_reader_url(href)
            if parsed and parsed[2] == gid:
                page_map[parsed[3]] = f"https://{host}/s/{parsed[1]}/{gid}-{parsed[3]}"

        for a in soup.select("table.ptt a[href], table.ptb a[href]"):
            href = str(a.get("href") or "")
            if m := PAGE_LINK_RE.search(href):
                max_p = max(max_p, int(m.group(1)))
            text = a.get_text(strip=True)
            if text.isdigit():
                max_p = max(max_p, int(text) - 1)
    except ImportError:
        pass

    if not page_map:
        for m in EH_READER_RE.finditer(html):
            if m.group(3) == gid:
                pnum = int(m.group(4))
                page_map[pnum] = f"https://{host}/s/{m.group(2).lower()}/{gid}-{pnum}"

    if max_p == 0:
        for m in PAGE_LINK_RE.finditer(html):
            max_p = max(max_p, int(m.group(1)))

    if "title" not in meta:
        if gn_m := re.search(r'<h1[^>]+id=["\']gn["\'][^>]*>(.*?)</h1>', html, re.I | re.S):
            title_val = re.sub(r"<[^>]+>", "", gn_m.group(1)).strip()
            if title_val:
                meta["title"] = title_val
    if "title_jpn" not in meta:
        if gj_m := re.search(r'<h1[^>]+id=["\']gj["\'][^>]*>(.*?)</h1>', html, re.I | re.S):
            title_jpn = re.sub(r"<[^>]+>", "", gj_m.group(1)).strip()
            if title_jpn:
                meta["title_jpn"] = title_jpn

    return page_map, max_p, meta


def _extract_img_and_nl(snippet: str) -> tuple[str | None, str | None]:
    img_url: str | None = None
    if m := IMG_SRC_RE.search(snippet):
        img_url = m.group(1)
    elif m := IMG_SRC_ALT_RE.search(snippet):
        img_url = m.group(1)

    if img_url and ("509.gif" in img_url or "509s.gif" in img_url):
        raise Quota509Error("E-Hentai/ExHentai 返回 509 配额限制图片")

    nl_token: str | None = None
    if m := NL_TOKEN_RE.search(snippet):
        nl_token = m.group(1).strip()

    return img_url, nl_token


async def _fetch_gmetadata(
    session: aiohttp.ClientSession,
    host: str,
    gid: str,
    token: str,
    *,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    proxy: str | None = None,
) -> dict[str, Any]:
    hosts_to_try = [host]
    if "exhentai" in host:
        hosts_to_try.append("e-hentai.org")
    for api_host in hosts_to_try:
        try:
            data = await _call_eh_api(
                session,
                api_host,
                {"method": "gdata", "gidlist": [[int(gid), token]], "namespace": 1},
                ehentai_cookie=ehentai_cookie,
                exhentai_cookie=exhentai_cookie,
                proxy=proxy,
            )
            glist = data.get("gmetadata")
            if isinstance(glist, list) and glist and isinstance(glist[0], dict):
                if not glist[0].get("error"):
                    return glist[0]
        except Exception:
            continue
    return {}


async def _collect_gallery_index(
    session: aiohttp.ClientSession,
    host: str,
    gid: str,
    token: str,
    *,
    expected_pages: int = 0,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    proxy: str | None = None,
    limit: int = 0,
) -> tuple[dict[int, str], dict[str, str], str]:
    base_gallery = f"https://{host}/g/{gid}/{token}/"
    status, html0, ct0 = await _fetch_text(
        session,
        f"{base_gallery}?nw=1&p=0",
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        proxy=proxy,
    )
    _check_html_errors(html0, host=host, content_type=ct0)
    if status >= 400:
        raise RuntimeError(f"访问画廊主页失败: HTTP {status}")

    all_pages, max_p, meta = _extract_reader_links_and_max_page(html0, host, gid)
    per_page = max(len(all_pages), 1)
    if max_p == 0 and expected_pages > per_page:
        max_p = (expected_pages - 1) // per_page

    p_idx = 1
    while p_idx <= max_p:
        if limit > 0 and len(all_pages) >= limit:
            break
        if expected_pages > 0 and len(all_pages) >= expected_pages:
            break

        _, html_p, ct_p = await _fetch_text(
            session,
            f"{base_gallery}?nw=1&p={p_idx}",
            ehentai_cookie=ehentai_cookie,
            exhentai_cookie=exhentai_cookie,
            proxy=proxy,
            referer=base_gallery,
        )
        _check_html_errors(html_p, host=host, content_type=ct_p)
        new_links, discovered_max_p, _ = _extract_reader_links_and_max_page(html_p, host, gid)
        if discovered_max_p > max_p:
            max_p = discovered_max_p
        before = len(all_pages)
        all_pages.update(new_links)
        if len(all_pages) == before:
            break
        p_idx += 1

    return all_pages, meta, html0


async def _try_free_archiver(
    session: aiohttp.ClientSession,
    html0: str,
    host: str,
    title: str,
    work_dir: Path,
    *,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    proxy: str | None = None,
) -> Path | None:
    cookies = cookies_for_host(
        host,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
    )
    if not (cookies.get("ipb_member_id") and cookies.get("ipb_pass_hash")):
        return None

    m = ARCHIVER_POPUP_RE.search(html0)
    if not m:
        return None
    archiver_url = m.group(0).replace("&amp;", "&")

    status, arch_html, ct = await _fetch_text(
        session,
        archiver_url,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        proxy=proxy,
        referer=f"https://{host}/",
    )
    if status >= 400 or ct.lower().startswith("image/"):
        return None

    # Strictly only auto-download if Free! (never spend GP/Credits unexpectedly)
    if "free!" not in arch_html.lower():
        return None

    post_url = archiver_url
    if action_m := ARCHIVER_ACTION_RE.search(arch_html):
        post_url = urljoin(archiver_url, action_m.group(1).replace("&amp;", "&"))

    headers = _site_headers(
        post_url,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        referer=archiver_url,
    )
    form_data = {"dltype": "org", "dlcheck": "Download Original Archive"}
    async with session.post(post_url, data=form_data, headers=headers, proxy=proxy) as resp:
        if resp.status >= 400:
            return None
        done_html = await resp.text(errors="replace")

    link_m = ARCHIVER_DONE_URL_RE.search(done_html)
    if not link_m:
        return None
    dl_url = urljoin(post_url, link_m.group(1).replace("&amp;", "&"))
    if "?start=1" not in dl_url and "hath.network" in dl_url:
        dl_url = f"{dl_url}?start=1"

    dl_headers = _site_headers(
        dl_url,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
    )
    zip_dest = work_dir / Path(safe_output_path(title, ".zip")).name
    async with session.get(dl_url, headers=dl_headers, proxy=proxy, allow_redirects=True) as resp:
        if resp.status >= 400:
            return None
        data = await resp.read()
        if not is_zip_bytes(data):
            return None
        zip_dest.write_bytes(data)

    validate_zip(str(zip_dest))
    return zip_dest


async def _download_single_reader_page(
    session: aiohttp.ClientSession,
    page_num: int,
    reader_url: str,
    dest_dir: Path,
    *,
    host: str,
    gid: str,
    showkey: str | None = None,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    proxy: str | None = None,
) -> Path:
    imgkey_match = parse_reader_url(reader_url)
    imgkey = imgkey_match[1] if imgkey_match else ""
    current_url = reader_url

    for nl_hop in range(3):
        snippet = ""
        if nl_hop == 0 and showkey and imgkey:
            try:
                sp_data = await _call_eh_api(
                    session,
                    host,
                    {
                        "method": "showpage",
                        "gid": int(gid),
                        "page": int(page_num),
                        "imgkey": imgkey,
                        "showkey": showkey,
                    },
                    ehentai_cookie=ehentai_cookie,
                    exhentai_cookie=exhentai_cookie,
                    proxy=proxy,
                )
                snippet = f"{sp_data.get('i3', '')}\n{sp_data.get('i6', '')}"
            except Exception:
                snippet = ""

        if not snippet.strip():
            status, html, ct = await _fetch_text(
                session,
                current_url,
                ehentai_cookie=ehentai_cookie,
                exhentai_cookie=exhentai_cookie,
                proxy=proxy,
                referer=f"https://{host}/g/{gid}/",
            )
            _check_html_errors(html, host=host, content_type=ct)
            if status >= 400:
                raise RuntimeError(f"第 {page_num} 页阅读页请求失败: HTTP {status}")
            snippet = html

        img_url, nl_token = _extract_img_and_nl(snippet)
        if not img_url:
            if nl_token and nl_hop < 2:
                sep = "&" if "?" in reader_url else "?"
                current_url = f"{reader_url}{sep}nl={nl_token}"
                continue
            raise RuntimeError(f"第 {page_num} 页未能提取图片地址")

        img_headers = _site_headers(
            img_url,
            ehentai_cookie=ehentai_cookie,
            exhentai_cookie=exhentai_cookie,
            referer=current_url,
        )
        try:
            async with session.get(img_url, headers=img_headers, proxy=proxy, allow_redirects=True) as resp:
                resp.raise_for_status()
                raw_bytes = await resp.read()
                ct = resp.headers.get("Content-Type", "")
            raw_ext = verify_image_bytes(raw_bytes) or guess_ext(img_url, ct)
            ext = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
            out_path = dest_dir / f"{page_num:05d}{ext}"
            out_path.write_bytes(raw_bytes)
            return out_path
        except Quota509Error:
            raise
        except Exception:
            if nl_token and nl_hop < 2:
                sep = "&" if "?" in reader_url else "?"
                current_url = f"{reader_url}{sep}nl={nl_token}"
                continue
            raise

    raise RuntimeError(f"第 {page_num} 页重试后仍下载失败")


async def _fetch_eh_on_host(
    session: aiohttp.ClientSession,
    host: str,
    gid: str,
    token: str,
    work_dir: Path,
    *,
    concurrency: int,
    ehentai_cookie: str,
    exhentai_cookie: str,
    prefer_archive: bool,
    proxy: str | None,
    download_images: bool,
    preview_limit: int,
    max_pages: int,
) -> EhDownloadResult:
    if "exhentai" in host and not has_ex_auth(exhentai_cookie):
        raise SadPandaError("访问 ExHentai 需要配置包含 ipb_member_id 与 ipb_pass_hash 的 exhentai_cookie")

    gmeta = await _fetch_gmetadata(
        session,
        host,
        gid,
        token,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        proxy=proxy,
    )
    expected_pages = int(gmeta.get("filecount", 0) or 0)

    index_limit = 0
    if not download_images:
        index_limit = max(preview_limit, 1)

    page_map, html_meta, html0 = await _collect_gallery_index(
        session,
        host,
        gid,
        token,
        expected_pages=expected_pages,
        ehentai_cookie=ehentai_cookie,
        exhentai_cookie=exhentai_cookie,
        proxy=proxy,
        limit=index_limit,
    )

    title = str(
        gmeta.get("title_jpn")
        or gmeta.get("title")
        or html_meta.get("title_jpn")
        or html_meta.get("title")
        or f"EH_{gid}"
    ).strip()
    tags = [str(t) for t in (gmeta.get("tags") or []) if str(t).strip()]
    category = str(gmeta.get("category") or "").strip()
    uploader = str(gmeta.get("uploader") or "").strip()
    thumb_url = str(gmeta.get("thumb") or "").strip()
    total_pages = max(expected_pages, len(page_map))

    cover_bytes: bytes | None = None
    if thumb_url:
        try:
            t_headers = _site_headers(
                thumb_url,
                ehentai_cookie=ehentai_cookie,
                exhentai_cookie=exhentai_cookie,
                referer=f"https://{host}/",
            )
            async with session.get(thumb_url, headers=t_headers, proxy=proxy) as resp:
                if resp.status < 400:
                    raw = await resp.read()
                    verify_image_bytes(raw)
                    cover_bytes = raw
        except Exception:
            cover_bytes = None

    archive_zip_path: Path | None = None
    image_paths: list[Path] = []
    skipped_reason = ""

    if download_images:
        if max_pages > 0 and total_pages > max_pages:
            skipped_reason = f"总页数 {total_pages} 超过上限 {max_pages}，已跳过全本下载"
        else:
            if prefer_archive:
                try:
                    archive_zip_path = await _try_free_archiver(
                        session,
                        html0,
                        host,
                        title,
                        work_dir,
                        ehentai_cookie=ehentai_cookie,
                        exhentai_cookie=exhentai_cookie,
                        proxy=proxy,
                    )
                except Exception:
                    archive_zip_path = None

            if archive_zip_path is None:
                sorted_items = sorted(page_map.items(), key=lambda item: item[0])
                if not sorted_items:
                    raise RuntimeError("未能从画廊页面提取到任何阅读页链接")

                # Extract showkey once from the first reader page for fast showpage API calls
                showkey: str | None = None
                try:
                    _, first_html, first_ct = await _fetch_text(
                        session,
                        sorted_items[0][1],
                        ehentai_cookie=ehentai_cookie,
                        exhentai_cookie=exhentai_cookie,
                        proxy=proxy,
                        referer=f"https://{host}/g/{gid}/{token}/",
                    )
                    _check_html_errors(first_html, host=host, content_type=first_ct)
                    if m := SHOWKEY_RE.search(first_html):
                        showkey = m.group(1)
                except Exception:
                    showkey = None

                pages_dir = work_dir / "pages"
                pages_dir.mkdir(parents=True, exist_ok=True)
                sem = asyncio.Semaphore(max(1, min(int(concurrency), 8)))

                async def _worker(p_num: int, r_url: str) -> tuple[int, Path]:
                    async with sem:
                        path = await _download_single_reader_page(
                            session,
                            p_num,
                            r_url,
                            pages_dir,
                            host=host,
                            gid=gid,
                            showkey=showkey,
                            ehentai_cookie=ehentai_cookie,
                            exhentai_cookie=exhentai_cookie,
                            proxy=proxy,
                        )
                        return p_num, path

                downloaded = await asyncio.gather(*(_worker(p, u) for p, u in sorted_items))
                downloaded.sort(key=lambda item: item[0])
                image_paths = [p for _, p in downloaded]
    elif preview_limit > 0 and page_map:
        sorted_items = sorted(page_map.items(), key=lambda item: item[0])[:preview_limit]
        pages_dir = work_dir / "previews"
        pages_dir.mkdir(parents=True, exist_ok=True)
        for p_num, r_url in sorted_items:
            path = await _download_single_reader_page(
                session,
                p_num,
                r_url,
                pages_dir,
                host=host,
                gid=gid,
                ehentai_cookie=ehentai_cookie,
                exhentai_cookie=exhentai_cookie,
                proxy=proxy,
            )
            image_paths.append(path)

    if cover_bytes is None and image_paths:
        try:
            cover_bytes = image_paths[0].read_bytes()
        except Exception:
            cover_bytes = None

    extra_parts = [f"画廊: {gid}/{token}"]
    if category:
        extra_parts.append(f"分类: {category}")
    if total_pages:
        extra_parts.append(f"页数: {total_pages}")
    if gmeta.get("rating"):
        extra_parts.append(f"评分: {gmeta['rating']}")
    if tags:
        extra_parts.append(f"标签: {', '.join(tags[:12])}")

    is_nsfw = category.lower() != "non-h"
    media = ParsedMedia(
        platform="exhentai" if "exhentai" in host else "ehentai",
        title=title,
        url=f"https://{host}/g/{gid}/{token}/",
        author=uploader,
        extra_info="\n".join(extra_parts),
        cover_bytes=cover_bytes,
        cover_url=thumb_url,
        image_paths=image_paths,
        page_count=total_pages or len(image_paths),
        is_nsfw=is_nsfw,
        skipped_download_reason=skipped_reason,
        work_dir=str(work_dir),
    )
    return EhDownloadResult(media=media, image_paths=image_paths, archive_zip_path=archive_zip_path)


async def fetch_eh_media(
    target_url: str,
    *,
    timeout: int = 120,
    concurrency: int = 5,
    ehentai_cookie: str = "",
    exhentai_cookie: str = "",
    prefer_archive: bool = True,
    proxy: str | None = None,
    download_images: bool = True,
    preview_limit: int = 0,
    max_pages: int = 0,
    work_dir: Path | None = None,
) -> EhDownloadResult:
    """Fetch E-Hentai/ExHentai metadata, optional cover/archive/pages with host fallback."""
    client_timeout = aiohttp.ClientTimeout(total=max(20, int(timeout)))
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        host, gid, token = await resolve_gallery_from_target(
            session,
            target_url,
            ehentai_cookie=ehentai_cookie,
            exhentai_cookie=exhentai_cookie,
            proxy=proxy,
        )

        if work_dir is None:
            work_dir = temp_root() / f"eh_{gid}_{uuid.uuid4().hex[:8]}"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            return await _fetch_eh_on_host(
                session,
                host,
                gid,
                token,
                work_dir,
                concurrency=concurrency,
                ehentai_cookie=ehentai_cookie,
                exhentai_cookie=exhentai_cookie,
                prefer_archive=prefer_archive,
                proxy=proxy,
                download_images=download_images,
                preview_limit=preview_limit,
                max_pages=max_pages,
            )
        except Exception as primary_err:
            fallback_host: str | None = None
            if host == "e-hentai.org" and has_ex_auth(exhentai_cookie):
                fallback_host = "exhentai.org"
            elif host == "exhentai.org":
                fallback_host = "e-hentai.org"

            if fallback_host:
                try:
                    return await _fetch_eh_on_host(
                        session,
                        fallback_host,
                        gid,
                        token,
                        work_dir,
                        concurrency=concurrency,
                        ehentai_cookie=ehentai_cookie,
                        exhentai_cookie=exhentai_cookie,
                        prefer_archive=prefer_archive,
                        proxy=proxy,
                        download_images=download_images,
                        preview_limit=preview_limit,
                        max_pages=max_pages,
                    )
                except Exception:
                    raise primary_err
            raise
