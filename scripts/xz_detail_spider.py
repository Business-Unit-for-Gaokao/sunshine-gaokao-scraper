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
    "课程统计",
    "开设院校",
    "薪酬指数",
    "升学指数",
    "学习投入意愿",
    "就业指数",
    "就业去向",
    "深造与就业",
    "专业介绍",
    "培养目标",
    "核心课程",
    "课程说明",
]

TAB_TEXTS = [
    "基本信息",
    "开设院校",
    "课程统计",
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
        if SAVE_HTML:
            save_text(OUTPUT_ROOT / "discovery" / "subcategory.txt", text)
        return result
    except Exception as e:
        result = {
            "url": SUBCATEGORY_URL,
            "error": repr(e),
        }
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
            pages.append({
                "page_no": page_no + 1,
                "start": start,
                "error": repr(e),
            })
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
            total_page = data.get("totalPage")
            next_start = data.get("startOfNextPage")
            is_next = data.get("isNextPageAvailable")
            if total_page and page_no + 1 >= int(total_page):
                break
            if is_next is False:
                break
            if next_start is not None and int(next_start) <= start:
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
    for _ in range(6):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(600)
    page.mouse.wheel(0, -100000)
    page.wait_for_timeout(400)


def wait_ready(page):
    page.wait_for_selector("body", timeout=30000)
    page.wait_for_timeout(1800)
    auto_scroll(page)
    page.wait_for_timeout(1200)


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
        "name": name or page_title.replace("_专业洞察", "").replace("专业洞察", "").strip(),
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
        if text in TAB_TEXTS:
            links.append({"text": text, "href": urljoin(page.url, href)})
    return unique_keep_order(links, key_func=lambda x: (x["text"], x["href"]))


def detect_headings(lines, page):
    found = []
    for line in lines:
        if line in DEFAULT_SECTION_HEADINGS:
            found.append(line)

    try:
        candidates = page.locator("h1,h2,h3,h4,h5,h6,.title,.tit,.section-title,.module-title").evaluate_all(
            """
            els => els.map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean)
            """
        )
        for item in candidates:
            t = clean_text(item)
            if t and len(t) <= 30:
                found.append(t)
    except Exception:
        pass

    return unique_keep_order([x for x in found if x], key_func=lambda x: x)


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


def parse_score_votes(line):
    line = clean_text(line)
    m = re.search(r"([0-5](?:\.\d+)?)\s*([0-9]+)人", line)
    if not m:
        return None
    return {
        "score": float(m.group(1)),
        "votes": int(m.group(2)),
        "raw": line,
    }


def parse_course_stats(section):
    result = {
        "present": section["present"],
        "raw_text": section["raw_text"],
        "courses": [],
    }
    if not section["present"]:
        return result

    skip_words = [
        "我要补充课程",
        "高校综合课程",
        "课程说明",
        "课程名称",
        "喜欢就点个赞",
        "课程难易度",
        "课程实用性",
    ]
    lines = [x for x in section["lines"] if not any(k in x for k in skip_words)]

    i = 0
    while i < len(lines):
        name = lines[i]
        if i + 3 >= len(lines):
            break
        likes = lines[i + 1]
        difficulty = lines[i + 2]
        usefulness = lines[i + 3]

        if re.fullmatch(r"\d+", likes):
            diff = parse_score_votes(difficulty)
            usef = parse_score_votes(usefulness)
            if diff and usef:
                result["courses"].append({
                    "course_name": name,
                    "likes": int(likes),
                    "difficulty": diff,
                    "usefulness_for_growth": usef,
                })
                i += 4
                continue
        i += 1

    return result


def collect_school_anchor_map(page):
    anchors = page.locator("a")
    out = []
    for i in range(anchors.count()):
        a = anchors.nth(i)
        text = clean_text(a.inner_text())
        href = a.get_attribute("href") or ""
        if not text or not href:
            continue
        href = urljoin(page.url, href)
        if "gaokao.chsi.com.cn/sch/schoolInfoMain--schId-" in href and SCHOOL_NAME_RE.search(text):
            out.append({"school_name": text, "school_url": href})
    return unique_keep_order(out, key_func=lambda x: (x["school_name"], x["school_url"]))


def parse_offering_schools(section, page):
    result = {
        "present": section["present"],
        "raw_text": section["raw_text"],
        "summary": {
            "national_total": None,
            "province_counts": [],
            "school_type_tags": [],
        },
        "school_rows": [],
    }
    if not section["present"]:
        return result

    lines = section["lines"]
    joined = " ".join(lines)

    m = re.search(r"全国\((\d+)\)", joined)
    if m:
        result["summary"]["national_total"] = int(m.group(1))

    province_counts = []
    for line in lines[:40]:
        for m in re.finditer(r"([\u4e00-\u9fa5]+)\((\d+)\)", line):
            province = m.group(1)
            count = int(m.group(2))
            if province != "全国":
                province_counts.append({"province": province, "count": count})
    result["summary"]["province_counts"] = unique_keep_order(
        province_counts,
        key_func=lambda x: (x["province"], x["count"])
    )

    tags = []
    for line in lines[:40]:
        if "本科高校" in line or "专科高校" in line or "双一流" in line:
            tags.append(line)
    result["summary"]["school_type_tags"] = unique_keep_order(tags, key_func=lambda x: x)

    school_url_map = {x["school_name"]: x["school_url"] for x in collect_school_anchor_map(page)}

    i = 0
    while i < len(lines):
        name = lines[i]
        if not SCHOOL_NAME_RE.search(name):
            i += 1
            continue

        metrics = []
        j = i + 1
        while j < len(lines) and len(metrics) < 6:
            parsed = parse_score_votes(lines[j])
            if parsed:
                metrics.append(parsed)
                j += 1
                continue
            if SCHOOL_NAME_RE.search(lines[j]) or lines[j] in DEFAULT_SECTION_HEADINGS:
                break
            j += 1

        row = {
            "school_name": name,
            "school_url": school_url_map.get(name, ""),
            "metrics": metrics,
        }
        if len(metrics) >= 1:
            row["recommend_index"] = metrics[0]
        if len(metrics) >= 2:
            row["major_satisfaction"] = metrics[1]
        if len(metrics) >= 3:
            row["overall"] = metrics[2]
        if len(metrics) >= 4:
            row["conditions"] = metrics[3]
        if len(metrics) >= 5:
            row["teaching"] = metrics[4]
        if len(metrics) >= 6:
            row["employment"] = metrics[5]

        result["school_rows"].append(row)
        i = j if j > i else i + 1

    return result


def extract_showimg_items(page):
    items = page.locator("img").evaluate_all(
        """
        imgs => imgs.map((img, idx) => {
            const src = img.currentSrc || img.src || '';
            if (!src.includes('/xzpt/survey/cdn/showimg/')) return null;

            function clean(s) {
                return (s || '').replace(/\\s+/g, ' ').trim();
            }

            function nearestHeading(el) {
                let node = el;
                for (let d = 0; node && d < 8; d++, node = node.parentElement) {
                    const h = node.querySelector('h1,h2,h3,h4,h5,h6,.title,.tit,.section-title,.module-title,.sub-title');
                    if (h) {
                        const t = clean(h.innerText);
                        if (t) return t;
                    }
                }
                return '';
            }

            function containerText(el) {
                const box = el.closest('section,article,div,li,figure') || el.parentElement;
                return clean(box ? box.innerText : '').slice(0, 800);
            }

            return {
                dom_index: idx,
                src,
                alt: clean(img.alt),
                title: clean(img.title),
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0,
                section_guess: nearestHeading(img),
                container_text: containerText(img)
            };
        }).filter(Boolean)
        """
    )
    return unique_keep_order(items, key_func=lambda x: x["src"])


def infer_chart_section(item):
    text = (item.get("section_guess", "") + " " + item.get("container_text", "")).strip()
    if "薪酬指数" in text:
        return "薪酬指数"
    if "升学指数" in text:
        return "升学指数"
    if "学习投入意愿" in text:
        return "学习投入意愿"
    if "就业省份" in text:
        return "已毕业学生主要就业省份"
    if "就业指数" in text:
        return "就业指数"
    return item.get("section_guess", "") or "图表"


def extract_section_descriptions(lines, headings):
    result = {}
    for h in headings:
        result[h] = extract_section(lines, h, headings)
    return result


def download_chart_images(items, detail_dir, referer, lines, headings):
    image_dir = detail_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    descriptions = extract_section_descriptions(lines, headings)
    saved = []

    for idx, item in enumerate(items, start=1):
        section = infer_chart_section(item)
        desc = descriptions.get(section, {"lines": [], "raw_text": ""})
        rec = {
            "index": idx,
            "section": section,
            "src": item["src"],
            "alt": item.get("alt", ""),
            "title": item.get("title", ""),
            "width": item.get("width", 0),
            "height": item.get("height", 0),
            "section_guess": item.get("section_guess", ""),
            "container_text": item.get("container_text", ""),
            "description_lines": desc.get("lines", []),
            "description_text": desc.get("raw_text", ""),
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


def scrape_detail(context, item):
    detail_url = item["detail_url"]
    spec_id = item["spec_id"]
    meta = item.get("meta", {})
    detail_dir = OUTPUT_ROOT / "details" / safe_name(spec_id)

    page = context.new_page()
    page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)

    html = page.content()
    body_text = page.locator("body").inner_text(timeout=30000)
    lines = normalize_lines(body_text)
    page_title = clean_text(page.title())
    headings = detect_headings(lines, page)
    stop_headings = unique_keep_order(DEFAULT_SECTION_HEADINGS + headings, key_func=lambda x: x)

    if SAVE_HTML:
        save_text(detail_dir / "page.html", html)
    save_text(detail_dir / "page.txt", body_text)

    basic = extract_basic_info(lines, body_text, detail_url, page_title)
    if meta:
        basic["list_meta"] = meta

    sections = {}
    for heading in stop_headings:
        sec = extract_section(lines, heading, stop_headings)
        if sec["present"]:
            sections[heading] = {
                "raw_text": sec["raw_text"],
                "line_count": len(sec["lines"]),
                "lines": sec["lines"],
            }

    course_stats = parse_course_stats(extract_section(lines, "课程统计", stop_headings))
    offering_schools = parse_offering_schools(extract_section(lines, "开设院校", stop_headings), page)
    chart_items = extract_showimg_items(page)
    charts = download_chart_images(chart_items, detail_dir, detail_url, lines, stop_headings)

    requires_login_sections = []
    if "学习投入意愿" in body_text and ("登录" in body_text or "登录后" in body_text):
        requires_login_sections.append("学习投入意愿")

    result = {
        "fetched_at": iso_now(),
        "url": detail_url,
        "spec_id": basic["spec_id"],
        "page_title": page_title,
        "basic": basic,
        "detected_headings": stop_headings,
        "sections": sections,
        "course_stats": course_stats,
        "offering_schools": offering_schools,
        "charts": charts,
        "tab_links": collect_tab_links(page),
        "key_links": collect_key_links(page),
        "requires_login_sections": requires_login_sections,
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
            viewport={"width": 1440, "height": 2200},
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
                detail_url = item["detail_url"]
                spec_id = item["spec_id"]
                try:
                    result = scrape_detail(context, item)
                    all_results.append({
                        "index": idx,
                        "spec_id": spec_id,
                        "detail_url": detail_url,
                        "ok": True,
                        "page_title": result.get("page_title", ""),
                        "courses": len(result.get("course_stats", {}).get("courses", [])),
                        "schools": len(result.get("offering_schools", {}).get("school_rows", [])),
                        "charts": result.get("charts", {}).get("count", 0),
                    })
                except PlaywrightTimeoutError as e:
                    all_results.append({
                        "index": idx,
                        "spec_id": spec_id,
                        "detail_url": detail_url,
                        "ok": False,
                        "error": repr(e),
                    })
                except Exception as e:
                    all_results.append({
                        "index": idx,
                        "spec_id": spec_id,
                        "detail_url": detail_url,
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
