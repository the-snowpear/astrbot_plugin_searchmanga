import base64
import json
import re
import time

import aiohttp

from .constants import DEFAULT_UA


SOUTUBOT_BASE = "https://soutubot.moe"
SEARCH_ENDPOINT = f"{SOUTUBOT_BASE}/api/search"
GLOBAL_M_RE = re.compile(r"\bm:\s*(\d+),")


def build_x_api_key(user_agent: str, challenge_m: int) -> str:
    seed = str(int(time.time()) ** 2 + len(user_agent) ** 2 + challenge_m)
    return base64.b64encode(seed.encode()).decode()[::-1].replace("=", "")


async def fetch_challenge_m(sess: aiohttp.ClientSession, proxy: str | None = None) -> int:
    async with sess.get(SOUTUBOT_BASE, headers={"User-Agent": DEFAULT_UA}, proxy=proxy) as resp:
        resp.raise_for_status()
        html_text = await resp.text()
    match = GLOBAL_M_RE.search(html_text)
    if not match:
        raise RuntimeError("无法解析 soutubot 首页鉴权参数 m")
    return int(match.group(1))


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
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
        challenge_m = await fetch_challenge_m(sess, proxy=proxy)

        headers = {
            "User-Agent": DEFAULT_UA,
            "Origin": SOUTUBOT_BASE,
            "Referer": f"{SOUTUBOT_BASE}/",
            "X-Requested-With": "XMLHttpRequest",
            "X-API-KEY": build_x_api_key(DEFAULT_UA, challenge_m),
        }
        form = aiohttp.FormData()
        form.add_field("file", image_bytes, filename="image.png", content_type="image/png")
        form.add_field("factor", str(factor))

        async with sess.post(SEARCH_ENDPOINT, headers=headers, data=form, proxy=proxy) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"soutubot HTTP {resp.status}: {text[:200]}")
            return json.loads(text)
