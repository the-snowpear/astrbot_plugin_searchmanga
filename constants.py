DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SOURCE_HOST = {
    "nhentai": "nhentai.net",
    "ehentai": "e-hentai.org",
    "exhentai": "exhentai.org",
    "panda": "panda.chaika.moe",
    "jmcomic": "18comic.vip",
    "iwara": "www.iwara.tv",
}

JMCOMIC_HOSTS = {
    "18comic.vip",
    "18comic.org",
    "18comic.net",
    "18comic.ink",
    "jmcomic.me",
    "jmcomic1.me",
    "jmcomic2.me",
    "jmcomic3.me",
    "jmcomic4.me",
    "jmcomic5.me",
    "jmcomic6.me",
    "jmcomic7.me",
    "jmcomic8.me",
    "jmcomic9.me",
    "jm-comic1.club",
    "jm-comic.cc",
}

EHENTAI_HOSTS = {
    "e-hentai.org",
    "exhentai.org",
}

NHENTAI_HOSTS = {
    "nhentai.net",
    "nhentai.xxx",
}

IWARA_HOSTS = {
    "iwara.tv",
    "ecchi.iwara.tv",
}

SUPPORTED_HOSTS = {
    "nhentai.net",
    "nhentai.xxx",
    "e-hentai.org",
    "exhentai.org",
    "panda.chaika.moe",
    "iwara.tv",
    "ecchi.iwara.tv",
} | JMCOMIC_HOSTS

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
NHENTAI_EXT = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}

PROXY_POLICY_DISABLED = "disabled"
PROXY_POLICY_ALL = "all"
PROXY_POLICY_SEARCH_ONLY = "search_only"
PROXY_POLICY_DOWNLOAD_ONLY = "download_only"

OUTPUT_FORMAT_ZIP = "zip"
OUTPUT_FORMAT_PDF = "pdf"
OUTPUT_FORMAT_MERGE = "merge"
VALID_OUTPUT_FORMATS = {OUTPUT_FORMAT_ZIP, OUTPUT_FORMAT_PDF, OUTPUT_FORMAT_MERGE}

NSFW_POLICY_SEND = "send"
NSFW_POLICY_BLUR = "blur"
NSFW_POLICY_IGNORE = "ignore"
VALID_NSFW_POLICIES = {NSFW_POLICY_SEND, NSFW_POLICY_BLUR, NSFW_POLICY_IGNORE}
