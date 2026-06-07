# 阳光高考专业信息爬虫

爬取 [阳光高考](https://gaokao.chsi.com.cn) 平台的高校专业信息，包括专业目录、开设院校、就业前景等数据。

## ⚠️ 重要说明

**阿里云盾 WAF 防护**：阳光高考网站使用了强力的阿里云盾 WAF（Web Application Firewall），会拦截自动化爬虫请求（返回 HTTP 412）。即使使用 Playwright + 反检测技术，仍然可能被拦截。

**推荐方案**：
1. **手动导入模式**：使用浏览器手动访问网站，复制数据后导入（见下方使用说明）
2. **代理服务器**：配置住宅代理或数据中心代理（需要付费服务）
3. **官方 API**：联系学信网获取官方数据接口

## 功能

- 爬取本科/专科专业目录
- 爬取每个专业的详细介绍（培养目标、课程、就业方向等）
- 爬取开设该专业的院校列表
- 支持手动导入数据
- 数据导出为 JSON / CSV

## 爬取的数据字段

### 专业信息
| 字段 | 说明 |
|------|------|
| specialty_id | 专业代码 |
| name | 专业名称 |
| level | 层次（本科/专科） |
| category | 学科门类 |
| description | 专业介绍 |
| training_objective | 培养目标 |
| main_courses | 主干课程 |
| employment_direction | 就业方向 |
| more_info_url | 详情页链接 |

### 开设院校
| 字段 | 说明 |
|------|------|
| specialty_id | 专业代码 |
| school_name | 院校名称 |
| school_id | 院校ID |
| province | 省份 |
| level | 办学层次 |
| feature | 办学特色 |

## 技术栈

- Python 3.10+
- requests + BeautifulSoup4（静态页面）
- Playwright（动态渲染页面，增强反检测）
- GitHub Actions 定时运行

## 使用方法

### 安装依赖

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器（如果使用自动爬取模式）
python -m playwright install chromium
```

### 方式 1：自动爬取（可能被 WAF 拦截）

```bash
# 尝试自动爬取（会自动检测并切换到 Playwright 模式）
python main.py --all --format json

# 强制使用 Playwright 模式
python main.py --mode playwright --all --format json
```

**注意**：由于阿里云盾 WAF 的强力防护，自动爬取很可能失败（HTTP 412）。

### 方式 2：手动导入（推荐）

这是目前最可靠的方法：

```bash
# 1. 生成数据模板
python main.py --template

# 2. 手动访问阳光高考网站，复制数据到 data/template.json

# 3. 导入数据
python main.py --import data/template.json --format json
```

**手动收集数据步骤**：
1. 访问 https://gaokao.chsi.com.cn/zyk/zybk/
2. 浏览专业列表，复制专业信息
3. 按照 `data/template.json` 的格式填写数据
4. 运行导入命令

### 方式 3：使用代理（需要付费代理服务）

编辑 `config.py`，配置代理：

```python
PROXY_HTTP = "http://your-proxy-server:port"
PROXY_HTTPS = "http://your-proxy-server:port"
```

然后运行：

```bash
python main.py --mode playwright --all --format json
```

### 输出格式

```bash
# 输出为 JSON（默认）
python main.py --all --format json

# 输出为 CSV
python main.py --all --format csv
```

## GitHub Actions 自动运行

项目已配置 GitHub Actions workflow，每周日自动运行一次。

**配置要求**：
- 需要配置 GitHub App 凭证（`APP_ID` 和 `APP_PRIVATE_KEY`）
- 由于 WAF 限制，GitHub Actions 运行可能失败
- 建议配合代理服务使用，或改为手动导入模式

## 项目结构

```
sunshine-gaokao-scraper/
├── main.py              # 入口文件
├── scraper.py           # 爬虫核心逻辑
├── config.py            # 配置文件
├── requirements.txt     # Python 依赖
├── data/                # 数据存储目录
│   └── template.json    # 数据模板（运行 --template 生成）
├── .github/
│   └── workflows/
│       └── scrape.yml   # GitHub Actions 配置
└── README.md
```

## 常见问题

### Q: 为什么爬取失败，返回 HTTP 412？

A: 阳光高考网站使用了阿里云盾 WAF，会检测并拦截自动化请求。这是一个非常强的反爬虫系统，即使使用 Playwright + 反检测技术也可能被拦截。

**解决方案**：
- 使用手动导入模式（推荐）
- 配置高质量的住宅代理
- 联系学信网获取官方 API

### Q: 如何绕过阿里云盾？

A: 阿里云盾使用多层检测机制（TLS 指纹、浏览器指纹、行为分析等），完全绕过非常困难。建议：
- 使用真实的住宅 IP 代理
- 降低请求频率
- 使用手动导入模式

### Q: GitHub Actions 运行失败怎么办？

A: GitHub Actions 的 IP 可能被 WAF 拦截。建议：
- 配置代理服务器
- 改为手动导入模式
- 或者在本地运行

## ⚠️ 免责声明

本项目仅供学习研究使用。请遵守阳光高考网站的使用条款和 robots.txt 规则，合理控制爬取频率，不要对目标网站造成过大压力。

**法律提示**：
- 未经授权的爬虫行为可能违反《网络安全法》
- 请尊重网站的服务条款和 robots.txt
- 建议优先使用官方 API 或手动导入方式

## License

MIT

<!-- target-crawl-links:start -->
## Target Crawl Links

This crawler targets 阳光高考 / CHSI, with additional 学职平台 / XZ speciality detail scripts found in source.

### 阳光高考 / CHSI

| Data | Link |
| --- | --- |
| 阳光高考根域名 | `https://gaokao.chsi.com.cn` |
| 专业库首页 | `https://gaokao.chsi.com.cn/zyk/zybk/` |
| 专业目录页 | `https://gaokao.chsi.com.cn/zyk/zybk/catalog.shtml` |
| 专业详情页模板 | `https://gaokao.chsi.com.cn/zyk/zybk/specialtyDetail.action?specialtyId={id}` |
| 专业开设院校页模板 | `https://gaokao.chsi.com.cn/zyk/zybk/schoolSpecialty.action?specialtyId={id}` |
| 院校列表分页 | `https://gaokao.chsi.com.cn/sch/search--ss-on,option-qg,searchType-1,start-{start}.dhtml` |
| 专业开设院校页 | `https://gaokao.chsi.com.cn/zyk/zybk/ksyxPage?specId={spec_id}` |
| 院校主页模板 | `https://gaokao.chsi.com.cn/sch/schoolInfoMain--schId-{schId}.dhtml` |
| 专业解读页模板 | `https://gaokao.chsi.com.cn/zyk/zybk/zyjd/viewPage/{id}` |
| 研招专业详情页线索 | `https://yz.chsi.com.cn/zyk/specialityDetail.do` |

### 学职平台 / XZ

| Data | Link |
| --- | --- |
| 专业入口 | `https://xz.chsi.com.cn/speciality/index.action` |
| 专业列表接口 | `https://xz.chsi.com.cn/speciality/list.action` |
| 专业子类接口 | `https://xz.chsi.com.cn/speciality/subcategory.action` |
| 普通本科详情页 | `https://xz.chsi.com.cn/speciality/detail/ptbk.action?specId={spec_id}` |
| 本科职业教育详情页 | `https://xz.chsi.com.cn/speciality/detail/bkzyjy.action?specId={spec_id}` |
| 职业教育详情页 | `https://xz.chsi.com.cn/speciality/detail/zyjy.action?specId={spec_id}` |
| 通用专业详情页 | `https://xz.chsi.com.cn/speciality/detail.action?specId={spec_id}` |
<!-- target-crawl-links:end -->
