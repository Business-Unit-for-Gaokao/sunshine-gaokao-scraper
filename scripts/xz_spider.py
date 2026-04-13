import json
import re
import time
import mimetypes
from hashlib import md5
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl
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


def rel_path(path: Path) -> str:
    return path.as_posix()


def unique_keep_order(items, key_func=None):
    seen = set()
    out = []
    for item in items:
        key = key_func(item) if key_func else json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": INDEX_URL,
    })
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


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
    return ".bin"


def download_file(url: str, dest_dir: Path, referer: str = ""):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Referer": referer or INDEX_URL,
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


def extract_template_tokens(html: str):
    return sorted(set(re.findall(r"\{\{[^{}]+\}\}", html)))


def extract_vue_like_paths(html: str):
    candidates = re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,}", html)
    keep = []
    for item in candidates:
        if item.startswith(("http.", "https.")):
            continue
        if any(k in item for k in ["resultJson", "jsrw", "storyList", "taskTitle", "evaluateText", "item.", "item2.", "zy."]):
            keep.append(item)
    return sorted(set(keep))


def extract_action_urls(html: str, base_url: str):
    patterns = [
        r"""https?://[^\s"'<>]+""",
        r"""[A-Za-z0-9_/\-\.]+\.(?:action|do|json|jsp)(?:\?[^\s"'<>]+)?""",
        r"""/[A-Za-z0-9_/\-\.]+(?:action|do|json|jsp)(?:\?[^\s"'<>]+)?""",
        r"""/speciality/[A-Za-z0-9_/\-\.?=&%-]+""",
    ]
    found = []
    for pat in patterns:
        found.extend(re.findall(pat, html))

    cleaned = []
    for raw in found:
        raw = raw.strip().strip('\'"')
        if not raw:
            continue
        full = urljoin(base_url, raw)
        cleaned.append(full)

    return sorted(set(cleaned))


def extract_script_blocks(html: str):
    soup = BeautifulSoup(html, "html.parser")
    blocks = []
    for i, script in enumerate(soup.find_all("script"), start=1):
        content = script.string or script.get_text("\n", strip=False) or ""
        src = script.get("src", "")
        if not content and not src:
            continue
        blocks.append({
            "index": i,
            "src": urljoin(INDEX_URL, src) if src else "",
            "length": len(content),
            "preview": content[:3000],
        })
    return blocks


def find_keywords_in_scripts(script_blocks):
    keywords = [
        "axios", "fetch(", "$.ajax", "$http", "XMLHttpRequest", "api", "url:",
        "resultJson", "jsrw", "speciality", "search", "query", "list", "detail",
        "index.action", "detail.action", "unlock", "task"
    ]
    hits = []
    for block in script_blocks:
        text = block["preview"]
        matched = [kw for kw in keywords if kw in text]
        if matched:
            hits.append({
                "script_index": block["index"],
                "src": block["src"],
                "matched_keywords": matched,
                "preview": text,
            })
    return hits


def parse_visible_text_blocks(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    texts = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "span", "div", "a", "button", "label"]):
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

        parent_text = ""
        parent = img.parent
        if parent:
            parent_text = clean_text(parent.get_text(" ", strip=True))[:200]

        images.append({
            "index": idx,
            "src": full,
            "alt": clean_text(img.get("alt", "")),
            "title": clean_text(img.get("title", "")),
            "class": " ".join(img.get("class", [])) if img.get("class") else "",
            "width": img.get("width", ""),
            "height": img.get("height", ""),
            "parent_text": parent_text,
        })
    return images


def parse_forms(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    forms = []

    for idx, form in enumerate(soup.find_all("form"), start=1):
        action = form.get("action", "")
        method = (form.get("method") or "GET").upper()
        inputs = []

        for tag in form.find_all(["input", "select", "textarea", "button"]):
            inputs.append({
                "tag": tag.name,
                "type": tag.get("type", ""),
                "name": tag.get("name", ""),
                "id": tag.get("id", ""),
                "value": tag.get("value", ""),
                "placeholder": tag.get("placeholder", ""),
            })

        forms.append({
            "index": idx,
            "method": method,
            "action": urljoin(base_url, action) if action else "",
            "inputs": inputs,
        })
    return forms


def extract_inline_data_candidates(html: str):
    candidates = []

    object_patterns = [
        r"resultJson\s*[:=]\s*(\{.*?\})",
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})",
        r"window\.__NUXT__\s*=\s*(\{.*?\})",
        r"__NEXT_DATA__\s*=\s*(\{.*?\})",
    ]

    for pat in object_patterns:
        for m in re.finditer(pat, html, flags=re.S):
            blob = m.group(1)
            candidates.append({
                "pattern": pat,
                "preview": blob[:2000],
                "length": len(blob),
            })

    return candidates


def classify_links(links):
    action_like = []
    static_like = []
    other = []

    for item in links:
        href = item["href"]
        if re.search(r"\.(action|do|json|jsp)(\?|$)", href):
            action_like.append(item)
        elif re.search(r"\.(png|jpg|jpeg|gif|svg|webp)(\?|$)", href, re.I):
            static_like.append(item)
        else:
            other.append(item)

    return {
        "action_like": action_like,
        "static_like": static_like,
        "other": other,
    }


def extract_query_params_from_urls(urls):
    params = []
    for url in urls:
        parsed = urlparse(url)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            params.append({
                "url": url,
                "key": k,
                "value": v,
            })
    return unique_keep_order(params)


def download_images(images, page_key: str, referer: str):
    page_dir = IMAGES_DIR / safe_name(page_key)
    page_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for item in images:
        try:
            path, size, content_type = download_file(item["src"], page_dir, referer=referer)
            downloaded.append({
                **item,
                "local_path": rel_path(path),
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
    template_tokens = extract_template_tokens(html)
    script_blocks = extract_script_blocks(html)
    links = parse_links(html, url)
    images = parse_images(html, url)
    action_urls = extract_action_urls(html, url)
    link_groups = classify_links(links)

    all_action_candidates = unique_keep_order(
        action_urls + [x["href"] for x in link_groups["action_like"]],
        key_func=lambda x: x
    )

    return {
        "url": url,
        "fetched_at": now_ts(),
        "page_title_guess": clean_text(BeautifulSoup(html, "html.parser").title.get_text()) if BeautifulSoup(html, "html.parser").title else "",
        "template_tokens": template_tokens,
        "template_token_count": len(template_tokens),
        "vue_like_paths": extract_vue_like_paths(html),
        "action_candidates": all_action_candidates,
        "action_candidate_count": len(all_action_candidates),
        "action_query_params": extract_query_params_from_urls(all_action_candidates),
        "inline_data_candidates": extract_inline_data_candidates(html),
        "script_blocks_meta": [{"index": x["index"], "src": x["src"], "length": x["length"]} for x in script_blocks],
        "script_keyword_hits": find_keywords_in_scripts(script_blocks),
        "visible_text_blocks": parse_visible_text_blocks(html),
        "links": links,
        "link_groups": link_groups,
        "forms": parse_forms(html, url),
        "images": images,
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

    action_probe = {
        "fetched_at": now_ts(),
        "base_url": INDEX_URL,
        "top_action_candidates": summary["action_candidates"][:100],
        "query_params_seen": summary["action_query_params"],
        "script_keyword_hit_count": len(summary["script_keyword_hits"]),
        "notes": [
            "优先检查 action_candidates 中包含 speciality / search / detail / list / query 的地址",
            "优先检查 script_keyword_hits 里命中的脚本预览",
            "如果页面仍是模板壳，下一步应使用浏览器 DevTools Network 找真实 XHR 接口"
        ]
    }
    save_json(OUTPUT_DIR / "action_probe.json", action_probe)

    print("done")
    print(f"template_tokens: {summary['template_token_count']}")
    print(f"vue_like_paths: {len(summary['vue_like_paths'])}")
    print(f"action_candidates: {summary['action_candidate_count']}")
    print(f"script_keyword_hits: {len(summary['script_keyword_hits'])}")
    print(f"links: {len(summary['links'])}")
    print(f"images: {len(summary['images'])}")
    print(f"images_downloaded: {len(summary['images_downloaded'])}")
    print(f"forms: {len(summary['forms'])}")


if __name__ == "__main__":
    main()
