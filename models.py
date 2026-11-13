from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SearchEntry:
    index: int
    title: str
    source: str
    url: str
    similarity: float = 0.0
    preview_url: str = ""
    alternatives: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DownloadIntent:
    index: int | None = None
    url: str | None = None
    jm_id: str | None = None


@dataclass
class GalleryDownload:
    title: str
    zip_path: str


@dataclass
class ParsedMedia:
    platform: str
    title: str
    url: str
    author: str = ""
    summary: str = ""
    extra_info: str = ""
    cover_bytes: bytes | None = None
    cover_url: str = ""
    image_paths: list[Path] = field(default_factory=list)
    file_path: str | None = None
    file_name: str | None = None
    video_path: str | None = None
    page_count: int = 0
    is_nsfw: bool = True
    ignored_reason: str = ""
    skipped_download_reason: str = ""
    work_dir: str | None = None
