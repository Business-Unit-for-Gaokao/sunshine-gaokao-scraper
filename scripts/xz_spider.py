import json
import re
import time
import mimetypes
from hashlib import md5
from pathlib import Path
from urllib.parse import urljoin, urlparse
import urllib.request

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://xz.chsi.com.cn"
INDEX_URL = "https://xz.chsi.com.cn/speciality/index.action"
OUTPUT_DIR = Path("output/xz")
IMAGES_DIR = OUTPUT_DIR / "images"
TIMEOUT = 30


def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def clean_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def safe_name(text: str, max_len: int = 80) -> str:
    text = clean_text(text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ")
    return text[:max_len] or "untitled"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def extract_template_tokens(html: str):
    tokens = sorted(set(re.findall(r"\{\{[^{}]+\}\}", html)))
    return tokens


def extract_js_hints(html: str):
    hints = []

    patterns = [
        r"resultJson\.[A-Za-z0-9_\.]+",
        r"jsrw\.[A-Za-z0-9_\.]+",
        r"[A-Za-z0-9_]+\s*:\s*function\s*\(",
        r"url\s*:\s*['\"][^'\"]+['\"]",
        r"[A-Za-z0-9_/-]+\.action(?:\?[^\s'\"<>]+)?",
        r"/speciality/[A-Za-z0-9_./?-]+",
    ]

    for pat in patterns:
        matches = re.findall(pat, html)
        hints.extend(matches)

    hints = [clean_text(x) for x in hints if clean_text(x)]
    return sorted(set(hints))


def parse_visible_text_blocks(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    texts = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "span", "div", "a"]):
        text = clean_text(el.get_text(" ", strip=True))
        if not text:
            continue
        if len(text) < 2:
            continue
        texts.append(text)

    uniq = []
    seen = set()
    for t in texts:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq


def parse_links(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()

    for a in soup.find_all("a"):
        text = clean_text(a.get_text(" ", strip=True))
        href = a.get("href", "")
        if not href:
            continue
        full = urljoin(base_url, href)
        key = (text, full)
        if key in seen:
            continue
        seen.add(key)
        links.append({
            "text": text,
            "href": full,
        })
    return links


def parse_images(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    images = []
    seen = set()

    for idx, img in enumerate(soup.find_all("img"), start=1):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if not src:
            continue
        full = urljoin(base_url, src)
        if full in seen:
            continue
        seen.add(full)

        alt = clean_text(img.get("alt", ""))
        title = clean_text(img.get("title", ""))

        images.append({
            "index": idx,
            "src": full,
            "alt": alt,
            "title": title,
        })
    return images


def guess_ext(url: str, content_type: str = ""):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return ext
    if content_type:
        ext2 = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        if ext2 == ".jpe":
            ext2 = ".jpg"
        if ext2:
            return ext2
    return ".png"


def download_image(url: str, dest_dir: Path, referer: str = ""):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Referer": referer or url,
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        content = resp.read()
        content_type = resp.headers.get("Content-Type", "")

    ext = guess_ext(url, content_type)
    name = f"{md5(url.encode('utf-8')).hexdigest()[:12]}{ext}"
    path = dest_dir / name
    path.write_bytes(content)
    return path, len(content), content_type


def download_images(images, page_key: str, referer: str):
    page_dir = IMAGES_DIR / safe_name(page_key)
    page_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for item in images:
        try:
            path, size, content_type = download_image(item["src"], page_dir, referer=referer)
            downloaded.append({
                **item,
                "local_path": path.as_posix(),
                "file_size": size,
                "content_type": content_type,
                "error": "",
            })
        except Exception as e:
            downloaded.append({
                **item,
                "local_path": "",
                "file_size": 0,
                "content_type": "",
                "error": repr(e),
            })
    return downloaded


def build_summary(url: str, html: str):
    return {
        "url": url,
        "fetched_at": now_ts(),
        "template_tokens": extract_template_tokens(html),
        "js_hints": extract_js_hints(html),
        "visible_text_blocks": parse_visible_text_blocks(html),
        "links": parse_links(html, url),
        "images": parse_images(html, url),
    }


def main():
    ensure_dirs()
    session = make_session()

    html = fetch_text(session, INDEX_URL)
    save_text(OUTPUT_DIR / "index_raw.html", html)

    summary = build_summary(INDEX_URL, html)
    summary["images_downloaded"] = download_images(
        summary["images"],
        page_key="speciality_index",
        referer=INDEX_URL,
    )

    save_json(OUTPUT_DIR / "index_summary.json", summary)

    print("done")
    print(f"template_tokens: {len(summary['template_tokens'])}")
    print(f"js_hints: {len(summary['js_hints'])}")
    print(f"links: {len(summary['links'])}")
    print(f"images: {len(summary['images'])}")
    print(f"images_downloaded: {len(summary['images_downloaded'])}")


if __name__ == "__main__":
    main()
