import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE = "https://gaokao.chsi.com.cn"
SEARCH_URL_TEMPLATE = BASE + "/sch/search--ss-on,option-qg,searchType-1,start-{start}.dhtml"
OUTPUT_DIR = Path("output")
SCHOOLS_DIR = OUTPUT_DIR / "schools"

START_MIN = int(os.getenv("START_MIN", "0"))
START_MAX = int(os.getenv("START_MAX", "2900"))
PAGE_STEP = int(os.getenv("PAGE_STEP", "20"))
TIMEOUT = int(os.getenv("TIMEOUT", "30"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.15"))
OVERWRITE = os.getenv("OVERWRITE", "0") == "1"
SAVE_RAW_HTML = os.getenv("SAVE_RAW_HTML", "0") == "1"

MAIN_RE = re.compile(r"/sch/schoolInfoMain--schId-(\d+)\.dhtml")
INFO_RE = re.compile(r"/sch/schoolInfo--schId-(\d+),categoryId-(\d+),mindex-(\d+)\.dhtml")

PROVINCES = {
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
    "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆"
}

NAV_BLACKLIST = {
    "首页", "高考资讯", "阳光志愿", "高招咨询", "在线咨询", "招生动态", "试题评析",
    "院校库", "专业库", "院校满意度", "专业满意度", "专业推荐", "更多", "招生政策",
    "选科参考", "云咨询周", "成绩查询", "招生章程", "名单公示", "志愿参考", "咨询室",
    "录取结果", "高职招生", "工作动态", "心理测评", "直播安排", "批次线", "专业解读",
    "各地网站", "职业前景", "特殊类型招生", "志愿填报时间", "招办访谈", "登录", "注册",
    "搜索", "查看", "取消", "返回", "上一页", "下一页", "跳至", "页"
}


def ensure_output():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SCHOOLS_DIR.mkdir(parents=True, exist_ok=True)


def iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat()


def clean_text(text):
    if text is None:
        return ""
    return " ".join(str(text).replace("\xa0", " ").split()).strip()


def normalize_lines(text):
    return [clean_text(x) for x in (text or "").splitlines() if clean_text(x)]


def unique_keep_order(items):
    seen = set()
    out = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_url(url: str):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def make_session():
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": BASE + "/sch/search.do",
    })
    return session


def get_html(session: requests.Session, url: str):
    time.sleep(REQUEST_DELAY)
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def soup_of(html: str):
    return BeautifulSoup(html, "html.parser")


def title_from_soup(soup: BeautifulSoup):
    if soup.title and clean_text(soup.title.get_text()):
        title = clean_text(soup.title.get_text())
        title = re.sub(r"_院校信息库_阳光高考.*$", "", title)
        return title
    for tag in ["h1", "h2", "h3"]:
        node = soup.find(tag)
        if node and clean_text(node.get_text()):
            return clean_text(node.get_text())
    return ""


def extract_url_meta(url: str):
    m1 = MAIN_RE.search(url)
    if m1:
        return {
            "page_type": "main",
            "schId": m1.group(1),
            "categoryId": "",
            "mindex": "",
        }
    m2 = INFO_RE.search(url)
    if m2:
        return {
            "page_type": "section",
            "schId": m2.group(1),
            "categoryId": m2.group(2),
            "mindex": m2.group(3),
        }
    return {
        "page_type": "unknown",
        "schId": "",
        "categoryId": "",
        "mindex": "",
    }


def is_same_school_info_url(url: str, sch_id: str):
    u = normalize_url(url)
    m1 = MAIN_RE.search(u)
    if m1 and m1.group(1) == str(sch_id):
        return True
    m2 = INFO_RE.search(u)
    if m2 and m2.group(1) == str(sch_id):
        return True
    return False


def extract_same_school_links(soup: BeautifulSoup, page_url: str, sch_id: str):
    links = []
    for a in soup.find_all("a", href=True):
        href = clean_text(a.get("href"))
        if not href or href.startswith("javascript:"):
            continue
        full = normalize_url(urljoin(page_url, href))
        if is_same_school_info_url(full, sch_id):
            links.append({
                "名称": clean_text(a.get_text()),
                "链接": full,
                "页面标识": extract_url_meta(full)
            })
    return unique_keep_order(links)


def extract_other_links(soup: BeautifulSoup, page_url: str, sch_id: str):
    links = []
    for a in soup.find_all("a", href=True):
        href = clean_text(a.get("href"))
        text = clean_text(a.get_text())
        if not href or href.startswith("javascript:"):
            continue
        full = normalize_url(urljoin(page_url, href))
        if is_same_school_info_url(full, sch_id):
            continue
        if text and text in NAV_BLACKLIST:
            continue
        if not text and not full:
            continue
        links.append({
            "名称": text,
            "链接": full
        })
    return unique_keep_order(links)


def extract_images(soup: BeautifulSoup, page_url: str):
    imgs = []
    for img in soup.find_all("img", src=True):
        src = clean_text(img.get("src"))
        if not src:
            continue
        imgs.append({
            "alt": clean_text(img.get("alt")),
            "src": normalize_url(urljoin(page_url, src))
        })
    return unique_keep_order(imgs)


def extract_tables(soup: BeautifulSoup):
    tables = []
    for idx, table in enumerate(soup.find_all("table"), start=1):
        rows = []
        for tr in table.find_all("tr"):
            cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append({
                "序号": idx,
                "行数": len(rows),
                "内容": rows
            })
    return tables


def extract_key_values_from_lines(lines):
    kv = {}
    for line in lines:
        m = re.match(r"^([^：:]{1,30})[：:]\s*(.+)$", line)
        if not m:
            continue
        key = clean_text(m.group(1))
        value = clean_text(m.group(2))
        if not key or not value:
            continue
        kv.setdefault(key, [])
        if value not in kv[key]:
            kv[key].append(value)
    return kv


def find_school_card(anchor, school_name):
    for parent in anchor.parents:
        name = getattr(parent, "name", "")
        if name not in {"div", "li", "td", "tr"}:
            continue
        text = clean_text(parent.get_text("\n", strip=True))
        if school_name in text and "主管部门" in text and len(text) < 500:
            return parent
    return anchor.parent


def guess_location(lines):
    for line in lines:
        if line in PROVINCES:
            return line
    return ""


def guess_authority(text):
    m = re.search(r"主管部门[:：]\s*([^\n]+)", text)
    return clean_text(m.group(1)) if m else ""


def guess_level(text):
    for item in ["本科", "高职(专科)", "高职（专科）", "专科"]:
        if item in text:
            return item
    return ""


def guess_satisfaction(text):
    m = re.search(r"满意度\s*([0-9.]+)", text)
    return clean_text(m.group(1)) if m else ""


def guess_features(lines, school_name, location, authority, level, satisfaction):
    features = []
    known = {
        school_name, location, authority, level, satisfaction,
        "主管部门：", "主管部门", "满意度", "院校信息库", "阳光高考"
    }
    fixed_patterns = [
        "“双一流”建设高校", "民办高校", "独立学院", "中外合作办学",
        "内地与港澳台地区合作办学"
    ]
    joined = "\n".join(lines)
    for p in fixed_patterns:
        if p in joined and p not in features:
            features.append(p)
    for line in lines:
        if line in known:
            continue
        if line in PROVINCES:
            continue
        if re.fullmatch(r"[0-9.]+", line):
            continue
        if "主管部门" in line or "满意度" in line:
            continue
        if len(line) <= 20 and line not in features:
            features.append(line)
    return unique_keep_order(features)


def parse_school_list_page(html: str, page_url: str, start: int):
    soup = soup_of(html)
    schools = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = normalize_url(urljoin(page_url, a.get("href")))
        m = MAIN_RE.search(href)
        if not m:
            continue

        sch_id = m.group(1)
        school_name = clean_text(a.get_text())
        if not school_name or school_name in NAV_BLACKLIST:
            continue
        if sch_id in seen:
            continue
        seen.add(sch_id)

        card = find_school_card(a, school_name)
        card_text = card.get_text("\n", strip=True) if card else a.get_text("\n", strip=True)
        lines = normalize_lines(card_text)
        text = "\n".join(lines)

        location = guess_location(lines)
        authority = guess_authority(text)
        level = guess_level(text)
        satisfaction = guess_satisfaction(text)
        features = guess_features(lines, school_name, location, authority, level, satisfaction)

        schools.append({
            "schId": sch_id,
            "学校名称": school_name,
            "主页链接": href,
            "搜索页": normalize_url(page_url),
            "start": start,
            "页码": start // PAGE_STEP + 1,
            "所在地": location,
            "主管部门": authority,
            "办学层次": level,
            "院校特性": features,
            "满意度": satisfaction,
            "列表页原始文本": text,
        })

    return schools


def parse_page_payload(url: str, html: str, sch_id: str):
    soup = soup_of(html)
    body = soup.body or soup
    page_meta = extract_url_meta(url)
    title = title_from_soup(soup)
    raw_text = body.get_text("\n", strip=True)
    lines = normalize_lines(raw_text)
    headings = unique_keep_order(
        [clean_text(x.get_text(" ", strip=True)) for x in body.find_all(["h1", "h2", "h3", "h4"]) if clean_text(x.get_text(" ", strip=True))]
    )

    payload = {
        "链接": normalize_url(url),
        "页面标识": page_meta,
        "标题": title,
        "标题层级": headings,
        "原始文本": "\n".join(lines),
        "文本行": lines,
        "结构化字段": extract_key_values_from_lines(lines),
        "表格": extract_tables(body),
        "图片": extract_images(body, url),
        "同校信息链接": extract_same_school_links(body, url, sch_id),
        "其他链接": extract_other_links(body, url, sch_id),
        "抓取时间": iso_now(),
    }

    if SAVE_RAW_HTML:
        payload["原始HTML"] = str(soup)

    return payload


def sort_pages(pages):
    def key(x):
        meta = x.get("页面标识", {})
        page_type = meta.get("page_type", "")
        if page_type == "main":
            return (0, 0, 0, x.get("链接", ""))
        mindex = meta.get("mindex") or "9999"
        category = meta.get("categoryId") or "999999999"
        return (1, int(mindex), int(category), x.get("链接", ""))

    return sorted(pages, key=key)


def extract_school_name_from_pages(stub, pages):
    if stub.get("学校名称"):
        return stub["学校名称"]
    for page in pages:
        if page.get("标题"):
            return page["标题"]
    return ""


def crawl_school(session: requests.Session, school_stub: dict):
    sch_id = school_stub["schId"]
    queue = deque([school_stub["主页链接"]])
    visited = set()
    pages = []
    errors = []

    while queue:
        current = normalize_url(queue.popleft())
        if current in visited:
            continue
        visited.add(current)

        try:
            html = get_html(session, current)
            payload = parse_page_payload(current, html, sch_id)
            pages.append(payload)

            for link in payload.get("同校信息链接", []):
                full = normalize_url(link["链接"])
                if full not in visited:
                    queue.append(full)

        except Exception as e:
            errors.append({
                "链接": current,
                "错误": repr(e),
                "时间": iso_now(),
            })

    pages = sort_pages(pages)
    main_page = next((p for p in pages if p.get("页面标识", {}).get("page_type") == "main"), None)
    section_pages = [p for p in pages if p.get("页面标识", {}).get("page_type") == "section"]

    return {
        "schId": sch_id,
        "学校名称": extract_school_name_from_pages(school_stub, pages),
        "列表信息": school_stub,
        "详情主页": main_page,
        "栏目页数量": len(section_pages),
        "栏目页": section_pages,
        "详情页总数": len(pages),
        "错误": errors,
        "抓取时间": iso_now(),
    }


def write_partial(index_rows, school_rows):
    save_json(
        OUTPUT_DIR / "school-index.partial.json",
        {
            "抓取时间": iso_now(),
            "数量": len(index_rows),
            "学校列表": index_rows,
        },
    )
    save_json(
        OUTPUT_DIR / "schools-flat.partial.json",
        {
            "抓取时间": iso_now(),
            "数量": len(school_rows),
            "学校列表": school_rows,
        },
    )


def build_hierarchy_by_province(schools):
    province_map = {}
    for school in schools:
        province = school.get("列表信息", {}).get("所在地") or "未知"
        province_map.setdefault(province, [])
        province_map[province].append(school)

    out = []
    for province, items in province_map.items():
        out.append({
            "所在地": province,
            "学校数量": len(items),
            "学校列表": items,
        })
    out.sort(key=lambda x: x["所在地"])
    return out


def collect_school_index(session: requests.Session):
    all_rows = []
    seen = set()

    for start in range(START_MIN, START_MAX + 1, PAGE_STEP):
        url = SEARCH_URL_TEMPLATE.format(start=start)
        print(f"[INFO] 列表页 start={start}")
        html = get_html(session, url)
        rows = parse_school_list_page(html, url, start)
        print(f"[INFO] start={start} 提取到学校数: {len(rows)}")

        for row in rows:
            sch_id = row["schId"]
            if sch_id in seen:
                continue
            seen.add(sch_id)
            all_rows.append(row)

        write_partial(all_rows, [])

    return all_rows


def run():
    ensure_output()
    session = make_session()

    index_rows = collect_school_index(session)
    write_partial(index_rows, [])

    school_rows = []
    for idx, school_stub in enumerate(index_rows, start=1):
        sch_id = school_stub["schId"]
        school_path = SCHOOLS_DIR / f"{sch_id}.json"

        if school_path.exists() and not OVERWRITE:
            data = load_json(school_path, {})
            if data:
                school_rows.append(data)
                print(f"[INFO] 跳过已存在学校 {idx}/{len(index_rows)} schId={sch_id}")
                continue

        print(f"[INFO] 抓取学校 {idx}/{len(index_rows)} schId={sch_id} {school_stub.get('学校名称', '')}")
        data = crawl_school(session, school_stub)
        save_json(school_path, data)
        school_rows.append(data)

        if idx % 10 == 0 or idx == len(index_rows):
            write_partial(index_rows, school_rows)

    hierarchy = build_hierarchy_by_province(school_rows)

    save_json(
        OUTPUT_DIR / "school-index.json",
        {
            "抓取时间": iso_now(),
            "数量": len(index_rows),
            "学校列表": index_rows,
        },
    )

    save_json(
        OUTPUT_DIR / "schools-flat.json",
        {
            "抓取时间": iso_now(),
            "数量": len(school_rows),
            "学校列表": school_rows,
        },
    )

    save_json(
        OUTPUT_DIR / "all.json",
        {
            "抓取时间": iso_now(),
            "数量": len(school_rows),
            "按所在地聚合": hierarchy,
        },
    )

    save_json(
        OUTPUT_DIR / "meta.json",
        {
            "抓取时间": iso_now(),
            "来源": {
                "搜索列表模板": SEARCH_URL_TEMPLATE,
                "详情主页格式": BASE + "/sch/schoolInfoMain--schId-<schId>.dhtml",
                "栏目页格式": BASE + "/sch/schoolInfo--schId-<schId>,categoryId-<categoryId>,mindex-<mindex>.dhtml",
            },
            "范围": {
                "start_min": START_MIN,
                "start_max": START_MAX,
                "page_step": PAGE_STEP,
            },
            "学校总数": len(school_rows),
            "是否覆盖已存在文件": OVERWRITE,
            "是否保存原始HTML": SAVE_RAW_HTML,
        },
    )

    print(f"schools_index: {len(index_rows)}")
    print(f"schools_saved: {len(school_rows)}")
    print(f"output_dir: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    run()
