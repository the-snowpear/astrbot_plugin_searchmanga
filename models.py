from dataclasses import dataclass


@dataclass
class SearchEntry:
    index: int
    title: str
    source: str
    url: str
    similarity: float = 0.0
    preview_url: str = ""


@dataclass
class DownloadIntent:
    index: int | None = None
    url: str | None = None
    jm_id: str | None = None


@dataclass
class GalleryDownload:
    title: str
    zip_path: str
