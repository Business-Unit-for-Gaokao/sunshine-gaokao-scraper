import json
import mimetypes
import os
import re
import time
import urllib.request
from hashlib import md5
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


INDEX_URL = "https://xz.chsi.com.cn/speciality/index.action"
LIST_URL = "https://xz.chsi.com.cn/speciality/list.action"
SUBCATEGORY_URL = "https://xz.chsi.com.cn/speciality/subcategory.action"
OUTPUT_ROOT = Path(os.getenv("XZ_OUTPUT_DIR", "output/xz_detail"))


def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


HEADLESS = env_bool("HEADLESS", True)
SAVE_HTML = env_bool("XZ_SAVE_HTML", True)
DOWNLOAD_IMAGES = env_bool("XZ_DOWNLOAD_IMAGES", True)
SAVE_LIST_HTML = env_bool("XZ_SAVE_LIST_HTML", True)
MAX_PAGES = int(os.getenv("XZ_MAX_PAGES", "200"))
MAX_DETAILS = int(os.getenv("XZ_MAX_DETAILS", "0"))
LIST_STEP = int(os.getenv("XZ_LIST_STEP", "15"))
EMPTY_PAGE_STOP = int(os.getenv("XZ_EMPTY_PAGE_STOP", "3"))

DEFAULT_SECTION_HEADINGS = [
    "专业介绍",
    "统计信息",
    "专业满意度",
    "学习投入意愿",
    "学长学姐有话说",
    "开设院校",
    "课程统计",
    "开设课程",
    "升学路径",
    "升学情况",
    "升学指数",
    "从业情况",
    "薪酬指数",
    "已毕业学生主要就业省份",
    "专业能力",
    "资质证书",
]

TAB_TEXTS = [
    "基本信息",
    "开设院校",
    "课程统计",
    "开设课程",
    "薪酬指数",
    "升学指数",
    "学习投入意愿",
    "就业指数",
    "专业解读",
    "图解专业",
    "选科要求",
]

SCHOOL_NAME_RE = re.compile(
    r"(大学|学院|学校|职业大学|职业学院|高等专科学校|师范大学|师范学院|医学院|中医药大学)$"
)


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def now_ms():
    return str(int(time.time() * 1000))


def clean_text(text):
    if text is None:
        return ""
    text = str(text).replace("\xa0", " ")
    return " ".join(text.split()).strip()


def normalize_lines(text):
    return [clean_text(x) for x in (text or "").splitlines() if clean_text(x)]


def safe_name(text, max_len=120):
    text = clean_text(text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ")
    return text[:max_len] or "untitled"


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


def extract_spec_id(url: str):
    m = re.search(r"specId=([^&]+)", url)
    return m.group(1) if m else ""


def build_detail_url(spec_id: str, cc: str = ""):
    if cc == "本科（普通教育）":
        return f"https://xz.chsi.com.cn/speciality/detail/ptbk.action?specId={spec_id}"
    if cc == "本科（职业教育）":
        return f"https://xz.chsi.com.cn/speciality/detail/bkzyjy.action?specId={spec_id}"
    if cc in ("高职（专科）", "专科（高职）"):
        return f"https://xz.chsi.com.cn/speciality/detail/zyjy.action?specId={spec_id}"
    return f"https://xz.chsi.com.cn/speciality/detail.action?specId={spec_id}"


def guess_ext(url: str, content_type: str = ""):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}:
        return ext
    if content_type:
        ext2 = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        if ext2 == ".jpe":
            ext2 = ".jpg"
        if ext2:
            return ext2
    return ".jpg"


def download_file(url: str, dest_dir: Path, referer: str):
    dest_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        content = resp.read()
        content_type = resp.headers.get("Content-Type", "")
    ext = guess_ext(url, content_type)
    filename = f"{md5(url.encode('utf-8')).hexdigest()[:12]}{ext}"
    path = dest_dir / filename
    path.write_bytes(content)
    return path, len(content), content_type


def make_session():
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


def fetch_text(session: requests.Session, url: str, params=None):
    r = session.get(url, params=params, timeout=60)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding or "utf-8"
    return r.text, r


def try_parse_json_text(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def fetch_subcategory(session: requests.Session):
    try:
        text, resp = fetch_text(session, SUBCATEGORY_URL, params={"df": "10", "_t": now_ms()})
        obj = try_parse_json_text(text)
        result = {
            "url": resp.url,
            "status_code": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "raw_text_preview": text[:2000],
            "json": obj,
        }
        save_json(OUTPUT_ROOT / "discovery" / "subcategory.json", result)
        save_text(OUTPUT_ROOT / "discovery" / "subcategory.txt", text)
        return result
    except Exception as e:
        result = {"url": SUBCATEGORY_URL, "error": repr(e)}
        save_json(OUTPUT_ROOT / "discovery" / "subcategory.json", result)
        return result


def parse_list_page_json(text: str):
    obj = try_parse_json_text(text)
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    if not isinstance(data, dict):
        return None
    page_array = data.get("pageArray")
    if not isinstance(page_array, list):
        return None
    return obj


def discover_detail_urls_by_list():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    session = make_session()
    subcategory_info = fetch_subcategory(session)

    discovered_rows = []
    pages = []
    seen_page_hash = set()
    seen_spec = set()
    consecutive_no_new = 0

    for page_no in range(MAX_PAGES):
        start = page_no * LIST_STEP
        params = {
            "start": str(start),
            "phbType": "1",
            "cc": "",
            "ml": "",
            "xk": "",
            "zymc": "",
            "_t": now_ms(),
        }

        try:
            text, resp = fetch_text(session, LIST_URL, params=params)
        except Exception as e:
            pages.append({"page_no": page_no + 1, "start": start, "error": repr(e)})
            consecutive_no_new += 1
            if consecutive_no_new >= EMPTY_PAGE_STOP:
                break
            continue

        page_hash = md5(text.encode("utf-8")).hexdigest()
        if page_hash in seen_page_hash:
            pages.append({
                "page_no": page_no + 1,
                "start": start,
                "url": resp.url,
                "status_code": resp.status_code,
                "content_type": resp.headers.get("Content-Type", ""),
                "duplicate_page": True,
                "candidate_count": 0,
            })
            break
        seen_page_hash.add(page_hash)

        if SAVE_LIST_HTML:
            save_text(OUTPUT_ROOT / "discovery" / "list_pages" / f"{start}.txt", text)

        obj = parse_list_page_json(text)
        page_candidates = []

        if obj:
            data = obj.get("data", {})
            page_array = data.get("pageArray", [])
            for row in page_array:
                if not isinstance(row, dict):
                    continue
                spec_id = clean_text(row.get("specId", ""))
                if not spec_id:
                    continue
                cc = clean_text(row.get("cc", ""))
                page_candidates.append({
                    "spec_id": spec_id,
                    "detail_url": build_detail_url(spec_id, cc),
                    "all_urls": [
                        build_detail_url(spec_id, cc),
                        f"https://xz.chsi.com.cn/speciality/detail.action?specId={spec_id}",
                    ],
                    "meta": {
                        "专业名称": clean_text(row.get("zymc", "")),
                        "专业代码": clean_text(row.get("zydm", "")),
                        "层次": cc,
                        "门类代码": clean_text(row.get("ml", "")),
                        "门类名称": clean_text(row.get("mlmc", "")),
                        "专业类": clean_text(row.get("xk", "")),
                        "专业原始ID": clean_text(row.get("zyId", "")),
                        "评价人数": row.get("evlNum"),
                        "综合满意度": row.get("evlValue"),
                    }
                })

        new_count = 0
        for item in page_candidates:
            if item["spec_id"] not in seen_spec:
                seen_spec.add(item["spec_id"])
                discovered_rows.append(item)
                new_count += 1

        total_page = None
        total_count = None
        if obj:
            data = obj.get("data", {})
            total_page = data.get("totalPage")
            total_count = data.get("totalCount")

        pages.append({
            "page_no": page_no + 1,
            "start": start,
            "url": resp.url,
            "status_code": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "candidate_count": len(page_candidates),
            "new_spec_count": new_count,
            "total_page": total_page,
            "total_count": total_count,
            "preview": clean_text(text)[:500],
        })

        if MAX_DETAILS > 0 and len(discovered_rows) >= MAX_DETAILS:
            discovered_rows = discovered_rows[:MAX_DETAILS]
            break

        if new_count == 0:
            consecutive_no_new += 1
        else:
            consecutive_no_new = 0

        if consecutive_no_new >= EMPTY_PAGE_STOP:
            break

        if obj:
            data = obj.get("data", {})
            tp = data.get("totalPage")
            ns = data.get("startOfNextPage")
            nxt = data.get("isNextPageAvailable")
            if tp and page_no + 1 >= int(tp):
                break
            if nxt is False:
                break
            if ns is not None and int(ns) <= start:
                break

    discovery = {
        "generated_at": iso_now(),
        "index_url": INDEX_URL,
        "list_url": LIST_URL,
        "list_step": LIST_STEP,
        "detail_count": len(discovered_rows),
        "details": discovered_rows,
        "pages": pages,
        "subcategory": subcategory_info,
    }
    save_json(OUTPUT_ROOT / "discovery" / "index_discovery.json", discovery)
    return discovery


def auto_scroll(page):
    for _ in range(8):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(700)
    page.mouse.wheel(0, -200000)
    page.wait_for_timeout(500)


def wait_ready(page):
    page.wait_for_selector("body", timeout=30000)
    page.wait_for_timeout(1800)
    auto_scroll(page)
    page.wait_for_timeout(1500)


def extract_basic_info(lines, body_text, url, page_title):
    level = ""
    name = ""

    for i, line in enumerate(lines):
        if line in ("本科（普通教育）", "本科（职业教育）", "高职（专科）", "专科（高职）"):
            level = line
            if i > 0:
                name = lines[i - 1]
            break

    code = ""
    m = re.search(r"专业代码[:：]?\s*([A-Za-z0-9]+)", body_text)
    if m:
        code = clean_text(m.group(1))

    discipline = ""
    m = re.search(r"门类[:：]?\s*([^\n]+)", body_text)
    if m:
        discipline = clean_text(m.group(1))

    major_class = ""
    m = re.search(r"专业类[:：]?\s*([^\n]+)", body_text)
    if m:
        major_class = clean_text(m.group(1))

    return {
        "name": name or page_title.replace("_专业洞察_学职平台", "").replace("_专业洞察", "").replace("专业洞察", "").strip(),
        "level": level,
        "code": code,
        "discipline": discipline,
        "major_class": major_class,
        "detail_url": url,
        "spec_id": extract_spec_id(url),
    }


def collect_tab_links(page):
    links = []
    anchors = page.locator("a")
    for i in range(anchors.count()):
        a = anchors.nth(i)
        text = clean_text(a.inner_text())
        href = a.get_attribute("href") or ""
        if not text or not href:
            continue
        full = urljoin(page.url, href)
        if text in TAB_TEXTS:
            links.append({"text": text, "href": full})
    return unique_keep_order(links, key_func=lambda x: (x["text"], x["href"]))


def collect_school_links(page):
    items = []
    try:
        anchors = page.locator("a").evaluate_all(
            """
            els => els.map(a => {
              const text = (a.innerText || '').replace(/\\s+/g, ' ').trim();
              const href = a.href || a.getAttribute('href') || '';
              return {text, href};
            }).filter(x => x.text && x.href && x.href.includes('schoolInfoMain--schId-'))
            """
        )
        for item in anchors:
            name = clean_text(item.get("text", ""))
            href = clean_text(item.get("href", ""))
            if name and href:
                items.append({"school_name": name, "school_url": href})
    except Exception:
        pass
    return unique_keep_order(items, key_func=lambda x: (x["school_name"], x["school_url"]))


def extract_tables(page):
    try:
        tables = page.locator("table").evaluate_all(
            """
            els => {
              const clean = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
              return els.map((table, idx) => {
                const rows = [...table.querySelectorAll('tr')].map(tr => {
                  return [...tr.querySelectorAll('th,td')]
                    .map(td => clean(td.innerText))
                    .filter(Boolean);
                }).filter(r => r.length > 0);
                return {index: idx + 1, rows};
              }).filter(t => t.rows.length > 0);
            }
            """
        )
        return tables
    except Exception:
        return []


def detect_headings(lines):
    found = []
    for line in lines:
        if line in DEFAULT_SECTION_HEADINGS:
            found.append(line)
    return unique_keep_order(found, key_func=lambda x: x)


def extract_section(lines, heading, stop_headings):
    try:
        start = lines.index(heading)
    except ValueError:
        start = -1
        for idx, line in enumerate(lines):
            if line.startswith(heading):
                start = idx
                break
        if start < 0:
            return {"present": False, "heading": heading, "lines": [], "raw_text": ""}

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] in stop_headings and lines[i] != heading:
            end = i
            break

    section_lines = lines[start + 1:end]
    return {
        "present": True,
        "heading": heading,
        "lines": section_lines,
        "raw_text": "\n".join(section_lines).strip(),
    }


def extract_showimg_items(page):
    try:
        items = page.locator("img").evaluate_all(
            """
            imgs => imgs.map((img, idx) => {
                const src = img.currentSrc || img.src || '';
                if (!src.includes('/xzpt/survey/cdn/showimg/')) return null;
                const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
                return {
                    dom_index: idx,
                    src,
                    alt: clean(img.alt),
                    title: clean(img.title),
                    width: img.naturalWidth || img.width || 0,
                    height: img.naturalHeight || img.height || 0
                };
            }).filter(Boolean)
            """
        )
        return unique_keep_order(items, key_func=lambda x: x["src"])
    except Exception:
        return []


def infer_chart_section(item):
    text = (item.get("title", "") + " " + item.get("alt", "")).strip()
    if "薪酬" in text:
        return "薪酬指数"
    if "升学" in text:
        return "升学指数"
    return "图表"


def download_chart_images(items, detail_dir, referer):
    image_dir = detail_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for idx, item in enumerate(items, start=1):
        rec = {
            "index": idx,
            "section": infer_chart_section(item),
            "src": item["src"],
            "alt": item.get("alt", ""),
            "title": item.get("title", ""),
            "width": item.get("width", 0),
            "height": item.get("height", 0),
        }

        if DOWNLOAD_IMAGES:
            try:
                path, size, content_type = download_file(item["src"], image_dir, referer=referer)
                rec["local_path"] = path.as_posix()
                rec["file_size"] = size
                rec["content_type"] = content_type
                rec["error"] = ""
            except Exception as e:
                rec["local_path"] = ""
                rec["file_size"] = 0
                rec["content_type"] = ""
                rec["error"] = repr(e)
        else:
            rec["local_path"] = ""
            rec["file_size"] = 0
            rec["content_type"] = ""
            rec["error"] = ""

        saved.append(rec)

    return {"count": len(saved), "items": saved}


def collect_key_links(page):
    anchors = page.locator("a")
    links = []
    for i in range(anchors.count()):
        a = anchors.nth(i)
        text = clean_text(a.inner_text())
        href = a.get_attribute("href") or ""
        if not text or not href:
            continue
        href = urljoin(page.url, href)
        if text in TAB_TEXTS or "schoolInfoMain--schId-" in href or "/speciality/detail" in href:
            links.append({"text": text, "href": href})
    return unique_keep_order(links, key_func=lambda x: (x["text"], x["href"]))


def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_json(item)


def attach_response_collector(page, api_dir: Path):
    api_dir.mkdir(parents=True, exist_ok=True)
    payloads = []
    seen = set()

    def on_response(resp):
        try:
            url = resp.url
            if "xz.chsi.com.cn" not in url:
                return
            if not any(k in url for k in [".action", ".do"]):
                return
            if resp.status != 200:
                return
            text = resp.text()
            obj = try_parse_json_text(text)
            if obj is None:
                return
            key = (url, md5(text.encode("utf-8")).hexdigest())
            if key in seen:
                return
            seen.add(key)
            rec = {
                "index": len(payloads) + 1,
                "url": url,
                "status_code": resp.status,
                "resource_type": resp.request.resource_type,
                "json": obj,
            }
            payloads.append(rec)
            save_json(api_dir / f"{rec['index']:02d}.json", rec)
        except Exception:
            pass

    page.on("response", on_response)
    return payloads


def parse_courses_from_tables(tables):
    courses = []

    for table in tables:
        rows = table.get("rows", [])
        if not rows:
            continue
        header = rows[0]
        joined_header = " | ".join(header)

        if "课程名称" not in joined_header:
            continue

        if len(header) >= 4 and ("课程难易度" in joined_header or "课程实用性" in joined_header):
            for row in rows[1:]:
                if not row:
                    continue
                name = clean_text(row[0]) if len(row) > 0 else ""
                if not name or "暂无数据" in name or name == "课程名称":
                    continue
                courses.append({
                    "course_name": name,
                    "likes": clean_text(row[1]) if len(row) > 1 else "",
                    "difficulty": clean_text(row[2]) if len(row) > 2 else "",
                    "practicality": clean_text(row[3]) if len(row) > 3 else "",
                    "source": "table_course_eval",
                })
        elif len(header) >= 2 and "课程类型" in joined_header:
            for row in rows[1:]:
                if not row:
                    continue
                name = clean_text(row[0]) if len(row) > 0 else ""
                if not name or "暂无数据" in name or name == "课程名称":
                    continue
                courses.append({
                    "course_name": name,
                    "course_type": clean_text(row[1]) if len(row) > 1 else "",
                    "source": "table_course_type",
                })

    return unique_keep_order(courses, key_func=lambda x: x.get("course_name", ""))


def parse_courses_from_api(api_payloads):
    courses = []

    for payload in api_payloads:
        obj = payload.get("json")
        if obj is None:
            continue
        for node in walk_json(obj):
            name = clean_text(
                node.get("kcmc")
                or node.get("courseName")
                or node.get("kc")
                or ""
            )
            if not name:
                continue
            courses.append({
                "course_name": name,
                "course_type": clean_text(node.get("type") or node.get("lb") or ""),
                "likes": node.get("praise"),
                "difficulty": clean_text(node.get("difficulty") or ""),
                "practicality": clean_text(node.get("practicality") or ""),
                "credit": clean_text(node.get("xf") or ""),
                "source": "api",
                "raw": node,
            })

    return unique_keep_order(courses, key_func=lambda x: x.get("course_name", ""))


def parse_schools_from_tables(tables, school_links):
    link_map = {x["school_name"]: x["school_url"] for x in school_links}
    schools = []

    for table in tables:
        rows = table.get("rows", [])
        if not rows:
            continue
        header = rows[0]
        joined_header = " | ".join(header)
        if "院校名称" not in joined_header:
            continue

        for row in rows[1:]:
            if not row:
                continue
            name = clean_text(row[0]) if len(row) > 0 else ""
            if not name or name in {"院校名称", "搜索无结果", "暂无数据"}:
                continue
            if "搜索无结果" in name:
                continue

            schools.append({
                "school_name": name,
                "school_url": link_map.get(name, ""),
                "columns": row[1:],
                "source": "table",
            })

    if not schools:
        for item in school_links:
            schools.append({
                "school_name": item["school_name"],
                "school_url": item["school_url"],
                "columns": [],
                "source": "link_only",
            })

    return unique_keep_order(schools, key_func=lambda x: x.get("school_name", ""))


def parse_schools_from_api(api_payloads, school_links):
    link_map = {x["school_name"]: x["school_url"] for x in school_links}
    schools = []

    for payload in api_payloads:
        obj = payload.get("json")
        if obj is None:
            continue
        for node in walk_json(obj):
            name = clean_text(
                node.get("yxmc")
                or node.get("schoolName")
                or node.get("school_name")
                or ""
            )
            if not name or not SCHOOL_NAME_RE.search(name):
                continue

            schools.append({
                "school_name": name,
                "school_url": link_map.get(name, ""),
                "recommend_index": node.get("zytjRank"),
                "recommend_votes": node.get("zytjCount"),
                "school_type": clean_text(node.get("bxxz") or ""),
                "source": "api",
                "raw": node,
            })

    return unique_keep_order(schools, key_func=lambda x: x.get("school_name", ""))


def merge_courses(dom_courses, api_courses):
    merged = {}
    for item in api_courses + dom_courses:
        name = clean_text(item.get("course_name", ""))
        if not name:
            continue
        if name not in merged:
            merged[name] = dict(item)
        else:
            base = merged[name]
            for k, v in item.items():
                if k not in base or base[k] in ("", None, [], {}):
                    base[k] = v
    return list(merged.values())


def merge_schools(dom_schools, api_schools):
    merged = {}
    for item in api_schools + dom_schools:
        name = clean_text(item.get("school_name", ""))
        if not name:
            continue
        if name not in merged:
            merged[name] = dict(item)
        else:
            base = merged[name]
            for k, v in item.items():
                if k not in base or base[k] in ("", None, [], {}):
                    base[k] = v
    return list(merged.values())


def summarize_school_section(body_text):
    result = {
        "national_total": None,
        "province_counts": [],
        "school_type_tags": [],
    }
    m = re.search(r"全国\((\d+)\)", body_text)
    if m:
        result["national_total"] = int(m.group(1))

    province_counts = []
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,10})\((\d+)\)", body_text):
        province = m.group(1)
        count = int(m.group(2))
        if province != "全国":
            province_counts.append({"province": province, "count": count})
    result["province_counts"] = unique_keep_order(
        province_counts,
        key_func=lambda x: (x["province"], x["count"])
    )

    for tag in ["本科高校", "专科高校", "双一流", "民办", "公办"]:
        if tag in body_text:
            result["school_type_tags"].append(tag)
    result["school_type_tags"] = unique_keep_order(result["school_type_tags"], key_func=lambda x: x)
    return result


def build_sections(lines):
    headings = detect_headings(lines)
    stop_headings = unique_keep_order(DEFAULT_SECTION_HEADINGS + headings, key_func=lambda x: x)
    sections = {}
    for heading in stop_headings:
        sec = extract_section(lines, heading, stop_headings)
        if sec["present"]:
            sections[heading] = {
                "raw_text": sec["raw_text"],
                "line_count": len(sec["lines"]),
                "lines": sec["lines"],
            }
    return headings, stop_headings, sections


def choose_working_url(context, item):
    candidates = [item["detail_url"]] + item.get("all_urls", [])
    candidates = unique_keep_order([x for x in candidates if x], key_func=lambda x: x)

    test_page = context.new_page()
    try:
        for url in candidates:
            try:
                test_page.goto(url, wait_until="domcontentloaded", timeout=45000)
                test_page.wait_for_selector("body", timeout=15000)
                title = clean_text(test_page.title())
                body = clean_text(test_page.locator("body").inner_text(timeout=15000))[:300]
                if title and "{{" not in title and ("专业洞察" in title or body):
                    return url
            except Exception:
                continue
        return item["detail_url"]
    finally:
        test_page.close()


def scrape_detail(context, item):
    working_url = choose_working_url(context, item)
    spec_id = item["spec_id"]
    meta = item.get("meta", {})
    detail_dir = OUTPUT_ROOT / "details" / safe_name(spec_id)
    api_dir = detail_dir / "api"

    page = context.new_page()
    api_payloads = attach_response_collector(page, api_dir)

    page.goto(working_url, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)

    html = page.content()
    body_text = page.locator("body").inner_text(timeout=30000)
    lines = normalize_lines(body_text)
    page_title = clean_text(page.title())

    if SAVE_HTML:
        save_text(detail_dir / "page.html", html)
    save_text(detail_dir / "page.txt", body_text)

    basic = extract_basic_info(lines, body_text, working_url, page_title)
    if meta:
        basic["list_meta"] = meta

    headings, stop_headings, sections = build_sections(lines)

    tables = extract_tables(page)
    school_links = collect_school_links(page)
    tab_links = collect_tab_links(page)
    key_links = collect_key_links(page)

    dom_courses = parse_courses_from_tables(tables)
    api_courses = parse_courses_from_api(api_payloads)
    merged_courses = merge_courses(dom_courses, api_courses)

    dom_schools = parse_schools_from_tables(tables, school_links)
    api_schools = parse_schools_from_api(api_payloads, school_links)
    merged_schools = merge_schools(dom_schools, api_schools)

    chart_items = extract_showimg_items(page)
    charts = download_chart_images(chart_items, detail_dir, working_url)

    school_summary = summarize_school_section(body_text)

    requires_login_sections = []
    if "学习投入意愿" in body_text and ("登录" in body_text or "登录后" in body_text):
        requires_login_sections.append("学习投入意愿")

    network_json_summaries = []
    for rec in api_payloads:
        obj = rec.get("json")
        top_keys = []
        if isinstance(obj, dict):
            top_keys = list(obj.keys())[:20]
        network_json_summaries.append({
            "index": rec["index"],
            "url": rec["url"],
            "status_code": rec["status_code"],
            "resource_type": rec["resource_type"],
            "top_keys": top_keys,
        })

    result = {
        "fetched_at": iso_now(),
        "url": working_url,
        "spec_id": basic["spec_id"],
        "page_title": page_title,
        "basic": basic,
        "detected_headings": stop_headings,
        "sections": sections,
        "tables": tables,
        "courses": {
            "count": len(merged_courses),
            "items": merged_courses,
            "sources": {
                "dom_count": len(dom_courses),
                "api_count": len(api_courses),
            },
        },
        "offering_schools": {
            "count": len(merged_schools),
            "summary": school_summary,
            "items": merged_schools,
            "sources": {
                "dom_count": len(dom_schools),
                "api_count": len(api_schools),
                "school_link_count": len(school_links),
            },
        },
        "charts": charts,
        "tab_links": tab_links,
        "key_links": key_links,
        "requires_login_sections": requires_login_sections,
        "network_json_summaries": network_json_summaries,
        "raw_files": {
            "html": (detail_dir / "page.html").as_posix() if SAVE_HTML else "",
            "text": (detail_dir / "page.txt").as_posix(),
        },
    }

    save_json(detail_dir / "detail.json", result)
    page.close()
    return result


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    discovery = discover_detail_urls_by_list()
    details = discovery["details"]

    if not details:
        save_json(OUTPUT_ROOT / "batch_summary.json", {
            "generated_at": iso_now(),
            "index_url": INDEX_URL,
            "discovered_count": 0,
            "processed_count": 0,
            "items": [],
        })
        print("done")
        print("discovered: 0")
        print("processed: 0")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1440, "height": 2400},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )

        try:
            all_results = []
            for idx, item in enumerate(details, start=1):
                spec_id = item["spec_id"]
                try:
                    result = scrape_detail(context, item)
                    all_results.append({
                        "index": idx,
                        "spec_id": spec_id,
                        "detail_url": result.get("url", item["detail_url"]),
                        "ok": True,
                        "page_title": result.get("page_title", ""),
                        "courses": result.get("courses", {}).get("count", 0),
                        "schools": result.get("offering_schools", {}).get("count", 0),
                        "charts": result.get("charts", {}).get("count", 0),
                        "network_json": len(result.get("network_json_summaries", [])),
                    })
                except PlaywrightTimeoutError as e:
                    all_results.append({
                        "index": idx,
                        "spec_id": spec_id,
                        "detail_url": item["detail_url"],
                        "ok": False,
                        "error": repr(e),
                    })
                except Exception as e:
                    all_results.append({
                        "index": idx,
                        "spec_id": spec_id,
                        "detail_url": item["detail_url"],
                        "ok": False,
                        "error": repr(e),
                    })

            batch = {
                "generated_at": iso_now(),
                "index_url": INDEX_URL,
                "list_url": LIST_URL,
                "discovered_count": discovery["detail_count"],
                "processed_count": len(all_results),
                "items": all_results,
            }
            save_json(OUTPUT_ROOT / "batch_summary.json", batch)

            print("done")
            print(f"discovered: {discovery['detail_count']}")
            print(f"processed: {len(all_results)}")

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
