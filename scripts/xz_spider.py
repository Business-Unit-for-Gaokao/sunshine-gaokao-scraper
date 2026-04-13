import json
import time
from pathlib import Path

import requests


BASE = "https://xz.chsi.com.cn"
OUT = Path("output/xz_probe")
TIMEOUT = 30


def ensure_dirs():
    OUT.mkdir(parents=True, exist_ok=True)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def save_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"{BASE}/speciality/index.action",
    })
    return s


def probe(session, name, url, method="GET", params=None, data=None):
    try:
        if method.upper() == "POST":
            resp = session.post(url, params=params, data=data, timeout=TIMEOUT)
        else:
            resp = session.get(url, params=params, timeout=TIMEOUT)

        content_type = resp.headers.get("Content-Type", "")
        text = resp.text
        result = {
            "name": name,
            "url": resp.url,
            "status_code": resp.status_code,
            "content_type": content_type,
            "content_length": len(text),
            "fetched_at": now(),
            "preview": text[:2000],
        }

        parsed_json = None
        if "json" in content_type.lower():
            try:
                parsed_json = resp.json()
            except Exception:
                parsed_json = None
        else:
            try:
                parsed_json = resp.json()
            except Exception:
                parsed_json = None

        if parsed_json is not None:
            result["json"] = parsed_json

        return result, text
    except Exception as e:
        return {
            "name": name,
            "url": url,
            "status_code": None,
            "content_type": "",
            "content_length": 0,
            "fetched_at": now(),
            "error": repr(e),
            "preview": "",
        }, ""


def main():
    ensure_dirs()
    s = make_session()

    probes = [
        {
            "name": "index",
            "url": f"{BASE}/speciality/index.action",
            "method": "GET",
            "params": None,
        },
        {
            "name": "list_no_params",
            "url": f"{BASE}/speciality/list.action",
            "method": "GET",
            "params": None,
        },
        {
            "name": "subcategory_no_params",
            "url": f"{BASE}/speciality/subcategory.action",
            "method": "GET",
            "params": None,
        },

        # 下面这些是“猜参数”探测，先跑通再逐步精简
        {
            "name": "list_kw_empty",
            "url": f"{BASE}/speciality/list.action",
            "method": "GET",
            "params": {"keyword": ""},
        },
        {
            "name": "list_kw_zhexue",
            "url": f"{BASE}/speciality/list.action",
            "method": "GET",
            "params": {"keyword": "哲学"},
        },
        {
            "name": "list_kw_010101",
            "url": f"{BASE}/speciality/list.action",
            "method": "GET",
            "params": {"keyword": "010101"},
        },
        {
            "name": "subcategory_cc_empty",
            "url": f"{BASE}/speciality/subcategory.action",
            "method": "GET",
            "params": {"cc": ""},
        },
        {
            "name": "subcategory_cc_ptbk",
            "url": f"{BASE}/speciality/subcategory.action",
            "method": "GET",
            "params": {"cc": "ptbk"},
        },
        {
            "name": "subcategory_keyword_zhexue",
            "url": f"{BASE}/speciality/subcategory.action",
            "method": "GET",
            "params": {"keyword": "哲学"},
        },
    ]

    results = []
    for item in probes:
        result, raw = probe(
            s,
            name=item["name"],
            url=item["url"],
            method=item.get("method", "GET"),
            params=item.get("params"),
            data=item.get("data"),
        )
        results.append(result)

        base_name = item["name"]
        save_json(OUT / f"{base_name}.json", result)
        if raw:
            save_text(OUT / f"{base_name}.txt", raw)

    # 已知详情页样本，验证 detail.action 可直接访问
    known_details = [
        "https://xz.chsi.com.cn/speciality/detail.action?specId=sb4t562ct3mvkthy",
        "https://xz.chsi.com.cn/speciality/detail.action?specId=f9fyx7tf4qhxehir",
        "https://xz.chsi.com.cn/speciality/detail/zyjy.action?specId=4otxlj23da72492u",
    ]

    detail_results = []
    for i, url in enumerate(known_details, start=1):
        result, raw = probe(s, f"detail_sample_{i}", url)
        detail_results.append(result)
        save_json(OUT / f"detail_sample_{i}.json", result)
        if raw:
            save_text(OUT / f"detail_sample_{i}.html", raw)

    summary = {
        "generated_at": now(),
        "probe_count": len(results),
        "detail_probe_count": len(detail_results),
        "results": results,
        "detail_results": detail_results,
    }
    save_json(OUT / "summary.json", summary)

    print("done")
    print(f"probes: {len(results)}")
    print(f"detail_probes: {len(detail_results)}")


if __name__ == "__main__":
    main()
