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
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.2"))
OVERWRITE = os.getenv("OVERWRITE", "0") == "1"
SAVE_RAW_HTML = os.getenv("SAVE_RAW_HTML", "0") == "1"

PROVINCES = {
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
    "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆"
}

NAV_BLACKLIST = {
    "首页", "高考资讯", "阳光志愿", "在线咨询", "招生动态", "高职招生", "试题评析",
    "院校库", "专业库", "招生章程", "名单公示", "院校满意度", "专业满意度", "专业推荐",
    "专业解读", "招办访谈", "帮助中心", "登录", "注册", "学籍查询", "学历查询",
    "学位查询", "在线验证", "出国教育背景服务", "图像校对", "学信档案", "高考", "研招",
    "港澳台招生", "征兵", "就业", "学职平台", "学信网", "中心简介", "联系我们", "版权声明",
    "网站地图", "网站宣传", "查看", "取消", "搜索", "返回", "上一页", "下一页", "更多信息"
}

PATTERNS = {
    "schoolInfoMain": re.compile(r"/sch/schoolInfoMain--schId-(\d+)\.dhtml"),
    "schoolInfo": re.compile(r"/sch/schoolInfo--schId-(\d+),categoryId-(\d+),mindex-(\d+)\.dhtml"),
    "listzyjs": re.compile(r"/sch/listzyjs--schId-(\d+),categoryId-(\d+),mindex-(\d+)\.dhtml"),
    "listdksw": re.compile(r"/sch/listdksw--schId-(\d+),categoryId-(\d+),mindex-(\d+)\.dhtml"),
    "listBulletin": re.compile(r"/sch/listBulletin--schId-(\d+),categoryId-(\d+),mindex-(\d+)\.dhtml"),
    "listlqjggs": re.compile(r"/sch/listlqjggs--schId-(\d+),categoryId-(\d+),mindex-(\d+)\.dhtml"),
    "zszc": re.compile(r"/zsgs/zhangcheng/listZszc--schId-(\d+)\.dhtml"),
}

COMMON_PAGE_CANDIDATES = [
    {"name": "学校首页", "path": "/sch/schoolInfoMain--schId-{schId}.dhtml"},
    {"name": "学校简介", "path": "/sch/schoolInfo--schId-{schId},categoryId-26172,mindex-1.dhtml"},
    {"name": "院系设置", "path": "/sch/schoolInfo--schId-{schId},categoryId-26177,mindex-2.dhtml"},
    {"name": "专业介绍", "path": "/sch/listzyjs--schId-{schId},categoryId-417809,mindex-3.dhtml"},
    {"name": "录取规则", "path": "/sch/schoolInfo--schId-{schId},categoryId-26187,mindex-4.dhtml"},
    {"name": "体检要求", "path": "/sch/schoolInfo--schId-{schId},categoryId-26196,mindex-5.dhtml"},
    {"name": "收费项目", "path": "/sch/schoolInfo--schId-{schId},categoryId-26201,mindex-7.dhtml"},
    {"name": "奖学金设置", "path": "/sch/schoolInfo--schId-{schId},categoryId-26204,mindex-8.dhtml"},
    {"name": "食宿条件", "path": "/sch/schoolInfo--schId-{schId},categoryId-26208,mindex-8.dhtml"},
    {"name": "基础设施", "path": "/sch/schoolInfo--schId-{schId},categoryId-26213,mindex-9.dhtml"},
    {"name": "毕业生就业", "path": "/sch/schoolInfo--schId-{schId},categoryId-26216,mindex-10.dhtml"},
    {"name": "联系办法", "path": "/sch/schoolInfo--schId-{schId},categoryId-26221,mindex-11.dhtml"},
    {"name": "公示栏", "path": "/sch/listBulletin--schId-{schId},categoryId-26219,mindex-12.dhtml"},
    {"name": "答考生问", "path": "/sch/listdksw--schId-{schId},categoryId-420549,mindex-16.dhtml"},
    {"name": "录取结果公示", "path": "/sch/listlqjggs--schId-{schId},categoryId-423317,mindex-8.dhtml"},
    {"name": "其他", "path": "/sch/schoolInfo--schId-{schId},categoryId-26224,mindex-17.dhtml"},
    {"name": "招生章程", "path": "/zsgs/zhangcheng/listZszc--schId-{schId}.dhtml"},
]


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
        backoff_factor=1.0,
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
        return clean_text(soup.title.get_text())
    for tag in ["h1", "h2", "h3"]:
        node = soup.find(tag)
        if node and clean_text(node.get_text()):
            return clean_text(node.get_text())
    return ""


def detect_page_type(url: str):
    for page_type, pattern in PATTERNS.items():
        m = pattern.search(url)
        if m:
            groups = list(m.groups())
            sch_id = groups[0] if groups else ""
            category_id = groups[1] if len(groups) >= 2 else ""
            mindex = groups[2] if len(groups) >= 3 else ""
            return {
                "page_type": page_type,
                "schId": sch_id,
                "categoryId": category_id,
                "mindex": mindex,
            }
    return {
        "page_type": "unknown",
        "schId": "",
        "categoryId": "",
        "mindex": "",
    }


def is_target_school_url(url: str, sch_id: str):
    meta = detect_page_type(url)
    return meta["schId"] == str(sch_id) and meta["page_type"] != "unknown"


def extract_target_links(soup: BeautifulSoup, page_url: str, sch_id: str):
    links = []
    for a in soup.find_all("a", href=True):
        href = clean_text(a.get("href"))
        if not href or href.startswith("javascript:"):
            continue
        full = normalize_url(urljoin(page_url, href))
        if is_target_school_url(full, sch_id):
            links.append({
                "名称": clean_text(a.get_text()),
                "链接": full,
                "页面标识": detect_page_type(full)
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
        if is_target_school_url(full, sch_id):
            continue
        if text in NAV_BLACKLIST:
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


def extract_list_items(soup: BeautifulSoup, page_url: str):
    items = []
    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text())
        href = normalize_url(urljoin(page_url, a.get("href")))
        if not text or text in NAV_BLACKLIST:
            continue
        if len(text) > 100:
            continue
        items.append({
            "标题": text,
            "链接": href
        })
    return unique_keep_order(items)


def extract_key_values_from_lines(lines):
    kv = {}
    for line in lines:
        m = re.match(r"^([^：:]{1,40})[：:]\s*(.+)$", line)
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
        if school_name in text and len(text) < 800:
            return parent
    return anchor.parent


def guess_location(lines):
    for line in lines:
        if line in PROVINCES:
            return line
    return ""


def guess_field(text, label):
    m = re.search(rf"{re.escape(label)}[:：]\s*([^\n]+)", text)
    return clean_text(m.group(1)) if m else ""


def guess_level(text):
    for item in ["本科", "高职（专科）", "高职(专科)", "专科"]:
        if item in text:
            return item
    return ""


def guess_features(lines):
    wanted = [
        "“双一流”建设高校", "211工程", "985工程",
        "民办高校", "独立学院", "中外合作办学", "研究生院"
    ]
    out = []
    joined = "\n".join(lines)
    for x in wanted:
        if x in joined:
            out.append(x)
    return out


def parse_school_list_page(html: str, page_url: str, start: int):
    soup = soup_of(html)
    schools = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = normalize_url(urljoin(page_url, a.get("href")))
        meta = detect_page_type(href)
        if meta["page_type"] != "schoolInfoMain":
            continue

        sch_id = meta["schId"]
        school_name = clean_text(a.get_text())
        if not school_name or sch_id in seen:
            continue
        seen.add(sch_id)

        card = find_school_card(a, school_name)
        raw_text = card.get_text("\n", strip=True) if card else a.get_text("\n", strip=True)
        lines = normalize_lines(raw_text)
        text = "\n".join(lines)

        schools.append({
            "schId": sch_id,
            "学校名称": school_name,
            "主页链接": href,
            "搜索页": normalize_url(page_url),
            "start": start,
            "页码": start // PAGE_STEP + 1,
            "所在地": guess_location(lines),
            "教育行政主管部门": guess_field(text, "教育行政主管部门") or guess_field(text, "主管部门"),
            "详细地址": guess_field(text, "详细地址"),
            "官方网址": guess_field(text, "官方网址"),
            "招生网址": guess_field(text, "招生网址"),
            "官方电话": guess_field(text, "官方电话"),
            "办学层次": guess_level(text),
            "院校特性": guess_features(lines),
            "列表页原始文本": text,
        })

    return schools


def build_seed_urls(sch_id: str):
    urls = []
    for item in COMMON_PAGE_CANDIDATES:
        urls.append({
            "名称": item["name"],
            "链接": normalize_url(BASE + item["path"].format(schId=sch_id))
        })
    return urls


def parse_page_payload(url: str, html: str, sch_id: str):
    soup = soup_of(html)
    body = soup.body or soup
    meta = detect_page_type(url)
    title = title_from_soup(soup)
    lines = normalize_lines(body.get_text("\n", strip=True))

    payload = {
        "链接": normalize_url(url),
        "页面标识": meta,
        "标题": title,
        "原始文本": "\n".join(lines),
        "文本行": lines,
        "结构化字段": extract_key_values_from_lines(lines),
        "表格": extract_tables(body),
        "图片": extract_images(body, url),
        "同校栏目链接": extract_target_links(body, url, sch_id),
        "其他链接": extract_other_links(body, url, sch_id),
        "列表项": extract_list_items(body, url) if meta["page_type"] in {"listzyjs", "listdksw", "listBulletin", "listlqjggs"} else [],
        "抓取时间": iso_now(),
    }

    if SAVE_RAW_HTML:
        payload["原始HTML"] = str(soup)

    return payload


def sort_pages(pages):
    order = {
        "schoolInfoMain": 0,
        "schoolInfo": 1,
        "listzyjs": 2,
        "listdksw": 3,
        "listBulletin": 4,
        "listlqjggs": 5,
        "zszc": 6,
        "unknown": 99,
    }

    def key(x):
        meta = x.get("页面标识", {})
        page_type = meta.get("page_type", "unknown")
        mindex = meta.get("mindex") or "9999"
        category = meta.get("categoryId") or "999999999"
        return (order.get(page_type, 99), int(mindex) if str(mindex).isdigit() else 9999, int(category) if str(category).isdigit() else 999999999, x.get("链接", ""))

    return sorted(pages, key=key)


def crawl_school(session: requests.Session, school_stub: dict):
    sch_id = school_stub["schId"]
    queue = deque()
    visited = set()
    pages = []
    errors = []

    queue.append(school_stub["主页链接"])
    for seed in build_seed_urls(sch_id):
        queue.append(seed["链接"])

    while queue:
        current = normalize_url(queue.popleft())
        if current in visited:
            continue
        visited.add(current)

        try:
            html = get_html(session, current)
            payload = parse_page_payload(current, html, sch_id)
            pages.append(payload)

            for link in payload.get("同校栏目链接", []):
                url = normalize_url(link["链接"])
                if url not in visited:
                    queue.append(url)

        except Exception as e:
            errors.append({
                "链接": current,
                "错误": repr(e),
                "时间": iso_now(),
            })

    pages = sort_pages(pages)

    return {
        "schId": sch_id,
        "学校名称": school_stub.get("学校名称", ""),
        "列表信息": school_stub,
        "详情页总数": len(pages),
        "页面列表": pages,
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

    result = []
    for province, items in province_map.items():
        result.append({
            "所在地": province,
            "学校数量": len(items),
            "学校列表": items,
        })
    result.sort(key=lambda x: x["所在地"])
    return result


def collect_school_index(session: requests.Session):
    all_rows = []
    seen = set()

    for start in range(START_MIN, START_MAX + 1, PAGE_STEP):
        url = SEARCH_URL_TEMPLATE.format(start=start)
        print(f"[INFO] 列表页 start={start}")
        html = get_html(session, url)
        rows = parse_school_list_page(html, url, start)
        print(f"[INFO] start={start} 学校数: {len(rows)}")

        for row in rows:
            if row["schId"] in seen:
                continue
            seen.add(row["schId"])
            all_rows.append(row)

        write_partial(all_rows, [])

    return all_rows


def run():
    ensure_output()
    session = make_session()

    index_rows = collect_school_index(session)
    write_partial(index_rows, [])

    school_rows = []
    total = len(index_rows)

    for i, school_stub in enumerate(index_rows, start=1):
        sch_id = school_stub["schId"]
        path = SCHOOLS_DIR / f"{sch_id}.json"

        if path.exists() and not OVERWRITE:
            data = load_json(path, {})
            if data:
                school_rows.append(data)
                print(f"[INFO] 跳过已有 {i}/{total} schId={sch_id}")
                continue

        print(f"[INFO] 抓取学校 {i}/{total} schId={sch_id} {school_stub.get('学校名称', '')}")
        data = crawl_school(session, school_stub)
        save_json(path, data)
        school_rows.append(data)

        if i % 10 == 0 or i == total:
            write_partial(index_rows, school_rows)

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
            "按所在地聚合": build_hierarchy_by_province(school_rows),
        },
    )

    save_json(
        OUTPUT_DIR / "meta.json",
        {
            "抓取时间": iso_now(),
            "来源": {
                "搜索页模板": SEARCH_URL_TEMPLATE,
                "页面类型": list(PATTERNS.keys()),
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

    print(f"school_index: {len(index_rows)}")
    print(f"school_saved: {len(school_rows)}")
    print(f"output_dir: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    run()
