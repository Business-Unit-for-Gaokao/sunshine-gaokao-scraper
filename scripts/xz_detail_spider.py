import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
from hashlib import md5
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_URL = "https://xz.chsi.com.cn/speciality/detail/ptbk.action?specId=hota9lc5d66i41zx"
OUTPUT_ROOT = Path(os.getenv("XZ_DETAIL_OUTPUT_DIR", "output/xz_detail"))
HEADLESS = os.getenv("HEADLESS", "1") == "1"

SECTION_CANDIDATES = [
    "课程统计",
    "开设院校",
    "薪酬指数",
    "升学指数",
    "学习投入意愿",
    "就业指数",
    "深造与就业",
    "专业介绍",
]

STOP_HEADINGS = [
    "课程统计",
    "开设院校",
    "薪酬指数",
    "升学指数",
    "学习投入意愿",
    "就业指数",
    "深造与就业",
    "专业介绍",
]

TAB_NAMES = [
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


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def clean_text(text):
    if text is None:
        return ""
    text = str(text).replace("\xa0", " ")
    return " ".join(text.split()).strip()


def normalize_lines(text):
    return [clean_text(x) for x in (text or "").splitlines() if clean_text(x)]


def safe_name(text, max_len=80):
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


def extract_field(text, label):
    m = re.search(rf"{re.escape(label)}[:：]\s*([^\n]+)", text)
    return clean_text(m.group(1)) if m else ""


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


def auto_scroll(page):
    for _ in range(8):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(700)
    page.mouse.wheel(0, -20000)
    page.wait_for_timeout(500)


def wait_ready(page):
    page.wait_for_selector("body", timeout=30000)
    page.wait_for_timeout(1500)
    auto_scroll(page)
    page.wait_for_timeout(1000)


def extract_basic_info(lines, body_text, url, page_title):
    level = ""
    name = ""

    for i, line in enumerate(lines):
        if line in ("本科（普通教育）", "本科（职业教育）", "高职（专科）", "专科（高职）"):
            level = line
            if i > 0:
                name = lines[i - 1]
            break

    basic = {
        "name": name or page_title.replace("_专业洞察", "").replace("专业洞察", "").strip(),
        "level": level,
        "code": extract_field(body_text, "专业代码"),
        "discipline": extract_field(body_text, "门类"),
        "major_class": extract_field(body_text, "专业类"),
        "detail_url": url,
        "spec_id": extract_spec_id(url),
    }
    return basic


def extract_tab_links(page):
    anchors = page.locator("a")
    out = []
    for i in range(anchors.count()):
        a = anchors.nth(i)
        text = clean_text(a.inner_text())
        href = a.get_attribute("href") or ""
        if not text or not href:
            continue
        if text in TAB_NAMES:
            out.append({
                "name": text,
                "url": href if href.startswith("http") else page.url.rsplit("/", 1)[0] + "/" + href if href.startswith("./") else href
            })
    out = unique_keep_order(out, key_func=lambda x: (x["name"], x["url"]))
    return out


def find_section_start(lines, heading):
    for i, line in enumerate(lines):
        if line == heading:
            return i
    for i, line in enumerate(lines):
        if line.startswith(heading):
            return i
    return -1


def extract_section(lines, heading, stop_headings):
    start = find_section_start(lines, heading)
    if start < 0:
        return {"present": False, "heading": heading, "lines": [], "raw_text": ""}

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] in stop_headings and lines[i] != heading:
            end = i
            break

    content_lines = lines[start + 1:end]
    return {
        "present": True,
        "heading": heading,
        "lines": content_lines,
        "raw_text": "\n".join(content_lines).strip(),
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
    lines = section["lines"]
    result = {
        "present": section["present"],
        "raw_text": section["raw_text"],
        "courses": [],
    }
    if not section["present"]:
        return result

    skip_keywords = ["我要补充课程", "高校综合课程", "课程说明", "课程名称", "喜欢就点个赞", "课程难易度", "课程实用性"]
    filtered = [x for x in lines if not any(k in x for k in skip_keywords)]

    i = 0
    while i < len(filtered):
        course_name = filtered[i]

        if i + 3 >= len(filtered):
            break

        likes_line = filtered[i + 1]
        difficulty_line = filtered[i + 2]
        usefulness_line = filtered[i + 3]

        if re.fullmatch(r"\d+", likes_line):
            diff = parse_score_votes(difficulty_line)
            usef = parse_score_votes(usefulness_line)
            if diff and usef:
                result["courses"].append({
                    "course_name": course_name,
                    "likes": int(likes_line),
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
        if "gaokao.chsi.com.cn/sch/schoolInfoMain--schId-" in href and SCHOOL_NAME_RE.search(text):
            out.append({
                "school_name": text,
                "school_url": href,
            })
    return unique_keep_order(out, key_func=lambda x: (x["school_name"], x["school_url"]))


def parse_offering_schools(section, page):
    lines = section["lines"]
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

    joined = " ".join(lines)
    m = re.search(r"全国\((\d+)\)", joined)
    if m:
        result["summary"]["national_total"] = int(m.group(1))

    province_counts = []
    for line in lines[:20]:
        for m in re.finditer(r"([\u4e00-\u9fa5]+)\((\d+)\)", line):
            province = m.group(1)
            count = int(m.group(2))
            if province != "全国":
                province_counts.append({"province": province, "count": count})
    result["summary"]["province_counts"] = unique_keep_order(
        province_counts,
        key_func=lambda x: x["province"]
    )

    tags = []
    for line in lines[:20]:
        if "本科高校" in line or "专科高校" in line or "双一流" in line:
            tags.append(line)
    result["summary"]["school_type_tags"] = unique_keep_order(tags, key_func=lambda x: x)

    school_anchor_map = collect_school_anchor_map(page)
    url_by_name = {x["school_name"]: x["school_url"] for x in school_anchor_map}

    i = 0
    while i < len(lines):
        name = lines[i]

        if not SCHOOL_NAME_RE.search(name):
            i += 1
            continue

        metrics = []
        j = i + 1
        while j < len(lines) and len(metrics) < 5:
            parsed = parse_score_votes(lines[j])
            if parsed:
                metrics.append(parsed)
                j += 1
                continue
            if SCHOOL_NAME_RE.search(lines[j]) or lines[j] in STOP_HEADINGS:
                break
            j += 1

        if len(metrics) >= 2:
            row = {
                "school_name": name,
                "school_url": url_by_name.get(name, ""),
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
                row["teaching_or_employment"] = metrics[4]

            result["school_rows"].append(row)
            i = j
            continue

        i += 1

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

            function findHeading(el) {
                let node = el;
                for (let d = 0; node && d < 8; d++, node = node.parentElement) {
                    const heading = node.querySelector('h1,h2,h3,h4,h5,h6,.title,.tit,.section-title,.module-title,.sub-title');
                    if (heading) {
                        const t = clean(heading.innerText);
                        if (t) return t;
                    }
                }

                let cur = el.parentElement;
                while (cur) {
                    let prev = cur.previousElementSibling;
                    while (prev) {
                        const t = clean(prev.innerText);
                        if (t && t.length <= 80) return t;
                        prev = prev.previousElementSibling;
                    }
                    cur = cur.parentElement;
                }
                return '';
            }

            function containerText(el) {
                const box = el.closest('section,article,div,li,figure') || el.parentElement;
                return clean(box ? box.innerText : '').slice(0, 500);
            }

            return {
                dom_index: idx,
                src,
                alt: clean(img.alt),
                title: clean(img.title),
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0,
                section_guess: findHeading(img),
                container_text: containerText(img)
            };
        }).filter(Boolean)
        """
    )
    return unique_keep_order(items, key_func=lambda x: x["src"])


def infer_chart_section(src_item, lines):
    if src_item.get("section_guess"):
        return src_item["section_guess"]

    text = src_item.get("container_text", "")
    if "薪酬指数" in text:
        return "薪酬指数"
    if "升学指数" in text:
        return "升学指数"
    if "学习投入意愿" in text:
        return "学习投入意愿"
    if "就业省份" in text:
        return "已毕业学生主要就业省份"
    for line in lines:
        if line in ("薪酬指数", "升学指数", "学习投入意愿"):
            return line
    return ""


def extract_chart_descriptions(lines):
    chart_sections = {}
    for heading in ["薪酬指数", "升学指数", "学习投入意愿"]:
        chart_sections[heading] = extract_section(lines, heading, STOP_HEADINGS)
    return chart_sections


def download_chart_images(chart_items, detail_dir, referer, lines):
    image_dir = detail_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    descriptions = extract_chart_descriptions(lines)
    saved = []

    for idx, item in enumerate(chart_items, start=1):
        src = item["src"]
        section_name = infer_chart_section(item, lines) or "chart"
        try:
            path, size, content_type = download_file(src, image_dir, referer=referer)
            saved.append({
                "index": idx,
                "section": section_name,
                "src": src,
                "local_path": path.as_posix(),
                "file_size": size,
                "content_type": content_type,
                "alt": item.get("alt", ""),
                "title": item.get("title", ""),
                "width": item.get("width", 0),
                "height": item.get("height", 0),
                "section_guess": item.get("section_guess", ""),
                "container_text": item.get("container_text", ""),
                "description_lines": descriptions.get(section_name, {}).get("lines", []),
                "description_text": descriptions.get(section_name, {}).get("raw_text", ""),
                "error": "",
            })
        except Exception as e:
            saved.append({
                "index": idx,
                "section": section_name,
                "src": src,
                "local_path": "",
                "file_size": 0,
                "content_type": "",
                "alt": item.get("alt", ""),
                "title": item.get("title", ""),
                "width": item.get("width", 0),
                "height": item.get("height", 0),
                "section_guess": item.get("section_guess", ""),
                "container_text": item.get("container_text", ""),
                "description_lines": descriptions.get(section_name, {}).get("lines", []),
                "description_text": descriptions.get(section_name, {}).get("raw_text", ""),
                "error": repr(e),
            })

    return {
        "count": len(saved),
        "items": saved,
    }


def collect_key_links(page):
    anchors = page.locator("a")
    links = []
    for i in range(anchors.count()):
        a = anchors.nth(i)
        text = clean_text(a.inner_text())
        href = a.get_attribute("href") or ""
        if not text or not href:
            continue
        if text in TAB_NAMES or "schoolInfoMain--schId-" in href:
            links.append({
                "text": text,
                "href": href,
            })
    return unique_keep_order(links, key_func=lambda x: (x["text"], x["href"]))


def run(detail_url: str):
    spec_id = extract_spec_id(detail_url) or "unknown_spec"
    detail_dir = OUTPUT_ROOT / safe_name(spec_id)

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
        page = context.new_page()

        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
            wait_ready(page)

            html = page.content()
            body_text = page.locator("body").inner_text(timeout=30000)
            lines = normalize_lines(body_text)
            page_title = clean_text(page.title())

            detail_dir.mkdir(parents=True, exist_ok=True)
            save_text(detail_dir / "page.html", html)
            save_text(detail_dir / "page.txt", body_text)

            basic = extract_basic_info(lines, body_text, detail_url, page_title)
            tab_links = extract_tab_links(page)

            sections = {}
            for name in SECTION_CANDIDATES:
                sections[name] = extract_section(lines, name, STOP_HEADINGS)

            course_stats = parse_course_stats(sections["课程统计"])
            offering_schools = parse_offering_schools(sections["开设院校"], page)

            showimg_items = extract_showimg_items(page)
            charts = download_chart_images(showimg_items, detail_dir, detail_url, lines)

            result = {
                "fetched_at": iso_now(),
                "url": detail_url,
                "spec_id": basic["spec_id"],
                "page_title": page_title,
                "basic": basic,
                "tab_links": tab_links,
                "sections": {
                    name: {
                        "present": sections[name]["present"],
                        "raw_text": sections[name]["raw_text"],
                        "line_count": len(sections[name]["lines"]),
                        "lines": sections[name]["lines"],
                    }
                    for name in SECTION_CANDIDATES
                },
                "course_stats": course_stats,
                "offering_schools": offering_schools,
                "charts": charts,
                "key_links": collect_key_links(page),
                "raw_files": {
                    "html": (detail_dir / "page.html").as_posix(),
                    "text": (detail_dir / "page.txt").as_posix(),
                },
            }

            save_json(detail_dir / "detail.json", result)
            save_json(
                OUTPUT_ROOT / "latest.json",
                {
                    "generated_at": iso_now(),
                    "detail_url": detail_url,
                    "spec_id": spec_id,
                    "detail_json": (detail_dir / "detail.json").as_posix(),
                },
            )

            print("done")
            print(f"spec_id: {spec_id}")
            print(f"courses: {len(course_stats['courses'])}")
            print(f"schools: {len(offering_schools['school_rows'])}")
            print(f"charts: {charts['count']}")

        except PlaywrightTimeoutError as e:
            detail_dir.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(detail_dir / "timeout.png"), full_page=True)
            except Exception:
                pass
            raise e
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    detail_url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("XZ_DETAIL_URL", DEFAULT_URL)
    run(detail_url)
