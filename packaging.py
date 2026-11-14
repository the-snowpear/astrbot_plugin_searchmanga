from io import BytesIO
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable

from astrbot.api import logger

from .constants import IMAGE_EXTS, NSFW_POLICY_BLUR, NSFW_POLICY_IGNORE
from .models import GalleryDownload, ParsedMedia
from .parsing import sanitize_filename

_PIL_FORMAT_EXT = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "BMP": "bmp",
    "AVIF": "avif",
}

_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    # Linux / Docker
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
)

_FONT_EXTENSIONS = {".ttf", ".ttc", ".otf"}
_REQUIRED_CJK_CHARS = ("作", "者", "页", "链")
_BONUS_CJK_CHARS = ("の", "訳", "☆")
_PREFERRED_FONT_HINTS = (
    "noto",
    "cjk",
    "sc",
    "tc",
    "jp",
    "wqy",
    "droid",
    "msyh",
    "sim",
    "pingfang",
    "heiti",
    "song",
    "hy",
)
_FONT_UNSET = object()
_CACHED_CJK_FONT_PATH: str | None | object = _FONT_UNSET
_FONT_OBJECT_CACHE: dict[tuple[str, int], object] = {}
_FONT_MISSING_WARNED = False
_FONT_WARNED_PATHS: set[str] = set()


def temp_root() -> Path:
    root = Path(tempfile.gettempdir()) / "astrbot_searchmanga"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_output_path(title: str, suffix: str = ".zip") -> str:
    base = sanitize_filename(title, "doujin")
    root = temp_root()
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    path = root / f"{base}{ext}"
    if not path.exists():
        return str(path)
    return str(root / f"{base}_{int(time.time() * 1000)}{ext}")


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def natural_sort_key(value: Path | str) -> list[int | str]:
    text = value.as_posix() if isinstance(value, Path) else str(value)
    parts: list[int | str] = []
    for seg in text.split("/"):
        for item in re.split(r"(\d+)", seg):
            if item.isdigit():
                parts.append(int(item))
            elif item:
                parts.append(item.lower())
    return parts


def validate_zip(zip_path: str) -> None:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad_name = zf.testzip()
            if bad_name:
                raise RuntimeError(f"ZIP CRC 校验失败: {bad_name}")
            if not zf.infolist():
                raise RuntimeError("ZIP 文件为空")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ZIP 文件损坏: {zip_path}") from exc


def looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
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
    if data.startswith(b"BM"):
        return True
    return False


def verify_image_bytes(data: bytes) -> str:
    """校验图片字节是否可解码（防止把 HTML/错误占位图打进包），返回规范后缀。"""
    if not data:
        raise ValueError("图片数据为空")
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            img.verify()
        with Image.open(BytesIO(data)) as img:
            img.load()
        ext = _PIL_FORMAT_EXT.get(fmt)
        if ext:
            return ext
    except ImportError:
        pass
    except Exception as exc:
        raise ValueError(f"图片解码校验失败: {exc}") from exc

    if not looks_like_image(data):
        raise ValueError("下载内容不是有效图片")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return "jpg"


def try_blur_image_bytes(data: bytes, radius: int = 20) -> bytes | None:
    """对图片字节施加高斯模糊；Pillow 不可用或图片解码失败时返回 None。"""
    if not data:
        return None
    try:
        from PIL import Image, ImageFilter

        with Image.open(BytesIO(data)) as img:
            rgb = img.convert("RGB")
            blurred = rgb.filter(ImageFilter.GaussianBlur(radius=radius))
            out = BytesIO()
            blurred.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except Exception:
        return None


def blur_image_bytes(data: bytes, radius: int = 20) -> bytes:
    """对图片字节施加高斯模糊；Pillow 不可用时原样返回。"""
    blurred = try_blur_image_bytes(data, radius=radius)
    return blurred if blurred is not None else data


def blur_image_file(image_path: Path, radius: int = 20) -> Path:
    """对本地图片文件施加高斯模糊，返回模糊后的新路径。"""
    try:
        from PIL import Image, ImageFilter

        out_path = image_path.with_name(f"{image_path.stem}_blur.jpg")
        with Image.open(image_path) as img:
            rgb = img.convert("RGB")
            rgb.filter(ImageFilter.GaussianBlur(radius=radius)).save(
                out_path, format="JPEG", quality=85
            )
        return out_path
    except Exception:
        return image_path


def apply_nsfw_to_cover(
    cover_bytes: bytes | None,
    *,
    is_nsfw: bool,
    nsfw_policy: str,
) -> bytes | None:
    if not cover_bytes or not is_nsfw:
        return cover_bytes
    if nsfw_policy == NSFW_POLICY_IGNORE:
        return None
    if nsfw_policy == NSFW_POLICY_BLUR:
        return blur_image_bytes(cover_bytes)
    return cover_bytes


def images_to_zip(image_paths: Iterable[Path], title: str) -> GalleryDownload:
    valid = [Path(p) for p in image_paths if Path(p).is_file()]
    if not valid:
        raise RuntimeError("没有可打包的有效图片文件")
    zip_path = safe_output_path(title, ".zip")
    pad = max(3, len(str(len(valid))))
    logger.info(f"[搜本子打包] {title}: 开始打包 ZIP ({len(valid)} 页) -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, img_path in enumerate(valid, 1):
            ext = img_path.suffix.lower().lstrip(".") or "jpg"
            ext = "jpg" if ext == "jpeg" else ext
            zf.write(img_path, f"{idx:0{pad}d}.{ext}")
    validate_zip(zip_path)
    return GalleryDownload(title=title, zip_path=zip_path)


def images_to_pdf(image_paths: Iterable[Path], title: str) -> GalleryDownload:
    valid = [Path(p) for p in image_paths if Path(p).is_file()]
    if not valid:
        raise RuntimeError("没有可合成 PDF 的有效图片文件")
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("生成 PDF 需要安装 Pillow 依赖") from exc

    pdf_path = safe_output_path(title, ".pdf")
    logger.info(f"[搜本子打包] {title}: 开始合成 PDF ({len(valid)} 页) -> {pdf_path}")

    opened: list[Image.Image] = []
    try:
        for img_path in valid:
            with Image.open(img_path) as img:
                opened.append(img.convert("RGB"))
        if not opened:
            raise RuntimeError("没有可合成 PDF 的有效图片")
        opened[0].save(
            pdf_path,
            "PDF",
            save_all=True,
            append_images=opened[1:],
            resolution=100.0,
        )
    finally:
        for img in opened:
            try:
                img.close()
            except Exception:
                pass

    if not Path(pdf_path).exists() or Path(pdf_path).stat().st_size == 0:
        raise RuntimeError("生成的 PDF 文件为空")
    return GalleryDownload(title=title, zip_path=pdf_path)


def repack_zip_flat(source_zip: str, title: str) -> GalleryDownload:
    zip_path = safe_output_path(title, ".zip")
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
        infos.sort(key=lambda info: natural_sort_key(info.filename))
        logger.info(f"[搜本子打包] {title}: 重打包归档 {len(infos)} 张图片 -> {zip_path}")
        pad = max(3, len(str(len(infos))))
        for idx, info in enumerate(infos, 1):
            ext = Path(info.filename).suffix.lower().lstrip(".")
            ext = "jpg" if ext == "jpeg" else ext
            dst.writestr(f"{idx:0{pad}d}.{ext}", src.read(info))
    validate_zip(zip_path)
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

    image_paths.sort(key=lambda path: natural_sort_key(path.relative_to(root)))
    return images_to_zip(image_paths, title)


def reset_font_cache() -> None:
    global _CACHED_CJK_FONT_PATH, _FONT_MISSING_WARNED
    _CACHED_CJK_FONT_PATH = _FONT_UNSET
    _FONT_MISSING_WARNED = False
    _FONT_WARNED_PATHS.clear()
    _FONT_OBJECT_CACHE.clear()


def _font_cjk_score(font) -> int:
    """检查字体对中文/日文字符的覆盖程度，缺失核心中文字符时返回 0。

    DejaVuSans / Liberation / NotoMono 这类纯西文字体在缺少汉字时会退化成
    .notdef 方块，Pillow 的 getmask 能画出非空 bbox，因此不能只看 bbox，
    必须与 .notdef(U+FFFF)的位图做对比。
    """
    try:
        missing_mask = bytes(font.getmask("\uffff"))
    except Exception:
        return 0

    def _has_glyph(ch: str) -> bool:
        try:
            mask = font.getmask(ch)
            return mask.getbbox() is not None and bytes(mask) != missing_mask
        except Exception:
            return False

    for ch in _REQUIRED_CJK_CHARS:
        if not _has_glyph(ch):
            return 0
    return len(_REQUIRED_CJK_CHARS) + sum(1 for ch in _BONUS_CJK_CHARS if _has_glyph(ch))


def _score_font_path(path: Path, size: int = 20) -> int:
    from PIL import ImageFont

    try:
        font = ImageFont.truetype(str(path), size=size)
    except Exception:
        return 0
    return _font_cjk_score(font)


def _warn_font_path_once(key: str, message: str) -> None:
    """同一路径只告警一次，避免每张卡片刷屏。"""
    if key in _FONT_WARNED_PATHS:
        return
    _FONT_WARNED_PATHS.add(key)
    logger.warning(message)


def _font_name_priority(path: Path) -> tuple[int, str]:
    lower = path.name.lower()
    for idx, hint in enumerate(_PREFERRED_FONT_HINTS):
        if hint in lower:
            return (idx, lower)
    return (len(_PREFERRED_FONT_HINTS), lower)


def _env_font_paths() -> list[Path]:
    """SEARCHMANGA_FONT_PATH 可指定字体文件或目录（Windows 用 ; 分隔）。"""
    raw = os.environ.get("SEARCHMANGA_FONT_PATH", "").strip()
    if not raw:
        return []
    paths: list[Path] = []
    for part in re.split(r"[;\n]", raw):
        part = part.strip().strip('"')
        if not part:
            continue
        probe = Path(part).expanduser()
        if probe.is_dir():
            try:
                paths.extend(
                    p for p in sorted(probe.iterdir(), key=_font_name_priority)
                    if p.is_file() and p.suffix.lower() in _FONT_EXTENSIONS
                )
            except OSError:
                continue
        elif probe.is_file():
            paths.append(probe)
    return paths


def _iter_cjk_font_paths(preferred: str | None = None) -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []

    def _add(p: Path) -> None:
        if p.suffix.lower() not in _FONT_EXTENSIONS:
            return
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        if resolved not in seen and p.is_file():
            seen.add(resolved)
            candidates.append(p)

    if preferred:
        _add(Path(preferred).expanduser())
    for p in _env_font_paths():
        _add(p)
    for raw in _FONT_CANDIDATES:
        _add(Path(raw))

    plugin_root = Path(__file__).resolve().parent
    for local_dir in (plugin_root / "data" / "fonts", temp_root() / "fonts"):
        if local_dir.is_dir():
            for p in sorted(local_dir.iterdir(), key=_font_name_priority):
                _add(p)

    for sys_dir in (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local" / "share" / "fonts",
        Path("C:/Windows/Fonts"),
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
    ):
        if sys_dir.is_dir():
            try:
                found = [
                    p for p in sys_dir.rglob("*")
                    if p.is_file() and p.suffix.lower() in _FONT_EXTENSIONS
                ]
            except OSError:
                continue
            for p in sorted(found, key=_font_name_priority):
                _add(p)

    plugins_dir = plugin_root.parent
    if plugins_dir.name == "plugins" and plugins_dir.is_dir():
        plugin_fonts: list[Path] = []
        sub_dirs = ("", "fonts", "font", "resource", "resources", "core/resources", "ziti")
        try:
            subdirs = [d for d in plugins_dir.iterdir() if d.is_dir() and d != plugin_root]
        except OSError:
            subdirs = []
        for plugin_dir in subdirs:
            for rel in sub_dirs:
                target_dir = plugin_dir / rel if rel else plugin_dir
                if not target_dir.is_dir():
                    continue
                try:
                    for item in target_dir.iterdir():
                        if item.is_file() and item.suffix.lower() in _FONT_EXTENSIONS:
                            plugin_fonts.append(item)
                except OSError:
                    continue
        for p in sorted(plugin_fonts, key=_font_name_priority):
            _add(p)

    return candidates


def _resolve_cjk_font_path(preferred: str | None = None, use_cache: bool = True) -> str | None:
    """挑一个真正含中文/日文字形的字体，避免把中文渲染成方块。"""
    global _CACHED_CJK_FONT_PATH, _FONT_MISSING_WARNED

    if preferred:
        probe = Path(preferred).expanduser()
        if probe.is_file() and probe.suffix.lower() in _FONT_EXTENSIONS:
            if _score_font_path(probe) > 0:
                return str(probe)
            _warn_font_path_once(f"{probe}", f"[搜本子] 配置的卡片字体缺少中文/日文字形，已忽略: {probe}")
        else:
            _warn_font_path_once(
                f"missing:{probe}",
                f"[搜本子] 配置的卡片字体不存在或格式不支持: {probe}",
            )

    if use_cache and _CACHED_CJK_FONT_PATH is not _FONT_UNSET:
        return _CACHED_CJK_FONT_PATH  # type: ignore[return-value]

    required = len(_REQUIRED_CJK_CHARS)
    best_path: str | None = None
    best_score = 0

    for cand in _iter_cjk_font_paths(preferred):
        score = _score_font_path(cand)
        if score > best_score:
            best_score = score
            best_path = str(cand)
            if score >= required:
                break

    if best_path is None and not _FONT_MISSING_WARNED:
        _FONT_MISSING_WARNED = True
        logger.warning(
            "[搜本子] 未找到含中文/日文字形的字体，信息卡片将回退为纯文本。"
            "Docker 部署可在镜像里安装 fonts-noto-cjk，"
            "或把字体挂载到 /usr/share/fonts，"
            "也可用 card_font_path / 环境变量 SEARCHMANGA_FONT_PATH 指定字体文件。"
        )

    if use_cache:
        _CACHED_CJK_FONT_PATH = best_path
    return best_path


def _find_font(size: int, font_path: str | None = None):
    from PIL import ImageFont

    resolved = _resolve_cjk_font_path(font_path, use_cache=True)
    if not resolved:
        return None
    cache_key = (resolved, size)
    cached = _FONT_OBJECT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(resolved, size=size)
    except Exception:
        reset_font_cache()
        return None
    _FONT_OBJECT_CACHE[cache_key] = font
    return font


def _wrap_lines(text: str, font: object, max_width: int, max_lines: int = 10) -> list[str]:
    lines: list[str] = []
    for raw_line in (text or "").splitlines():
        if not raw_line.strip():
            lines.append("")
            continue
        current = ""
        for ch in raw_line:
            candidate = current + ch
            try:
                w = font.getlength(candidate)
            except Exception:
                w = len(candidate) * 14
            if w > max_width and current:
                lines.append(current)
                current = ch
                if len(lines) >= max_lines:
                    lines[-1] = lines[-1][:-1] + "…"
                    return lines
            else:
                current = candidate
        if current:
            lines.append(current)
            if len(lines) >= max_lines:
                return lines
    return lines


def render_info_card(
    media: ParsedMedia,
    *,
    nsfw_policy: str = "send",
    font_path: str | None = None,
) -> Path | None:
    """使用 Pillow 渲染简洁的信息卡片，缺字体或渲染失败时返回 None 以回退文本。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    title_font = _find_font(28, font_path)
    body_font = _find_font(20, font_path)
    badge_font = _find_font(18, font_path)
    if title_font is None or body_font is None or badge_font is None:
        return None

    width = 880
    padding = 32
    content_w = width - padding * 2

    cover_img: Image.Image | None = None
    cover_bytes = apply_nsfw_to_cover(
        media.cover_bytes, is_nsfw=media.is_nsfw, nsfw_policy=nsfw_policy
    )
    if cover_bytes:
        try:
            with Image.open(BytesIO(cover_bytes)) as src_cov:
                cov = src_cov.convert("RGB")
                max_cov_w = 240
                max_cov_h = 340
                cov.thumbnail((max_cov_w, max_cov_h), Image.Resampling.LANCZOS)
                cover_img = cov.copy()
        except Exception:
            cover_img = None

    text_max_w = content_w - (cover_img.width + 28 if cover_img else 0)
    title_lines = _wrap_lines(media.title, title_font, text_max_w, max_lines=3)

    meta_parts: list[str] = []
    if media.author:
        meta_parts.append(f"作者: {media.author}")
    if media.page_count and "页数:" not in (media.extra_info or "") and "页数：" not in (media.extra_info or ""):
        meta_parts.append(f"页数: {media.page_count}")
    if media.summary:
        meta_parts.append(media.summary)
    if media.extra_info:
        meta_parts.append(media.extra_info)
    meta_parts.append(f"链接: {media.url}")
    body_lines = _wrap_lines("\n".join(meta_parts), body_font, text_max_w, max_lines=14)

    text_block_h = 46 + len(title_lines) * 38 + 14 + len(body_lines) * 28
    cover_h = cover_img.height if cover_img else 0
    height = max(text_block_h, cover_h) + padding * 2

    card = Image.new("RGB", (width, height), (28, 30, 38))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (12, 12, width - 12, height - 12),
        radius=18,
        fill=(36, 39, 49),
        outline=(66, 72, 92),
        width=2,
    )

    text_x = padding
    if cover_img:
        card.paste(cover_img, (padding, padding))
        text_x = padding + cover_img.width + 28

    badge_text = media.platform.upper()
    badge_w = int(badge_font.getlength(badge_text)) + 24
    draw.rounded_rectangle(
        (text_x, padding, text_x + badge_w, padding + 32),
        radius=8,
        fill=(88, 101, 242),
    )
    draw.text((text_x + 12, padding + 5), badge_text, font=badge_font, fill=(255, 255, 255))

    y = padding + 46
    for line in title_lines:
        draw.text((text_x, y), line, font=title_font, fill=(242, 244, 248))
        y += 38

    y += 12
    for line in body_lines:
        if line:
            draw.text((text_x, y), line, font=body_font, fill=(195, 201, 214))
        y += 28

    out_path = Path(safe_output_path(f"card_{media.platform}", ".png"))
    card.save(out_path, format="PNG")
    return out_path
