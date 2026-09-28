---
name: wanfang-research
description: 万方数据学术论文检索 — 搜索、详情、整篇/分章下载，CDP Chrome 自动化
---

# 万方数据 Research

万方数据学术检索：搜索（期刊论文/学位论文/会议论文/专利）、详情（参考文献、引用格式、学位论文章节树）、论文整篇与分章下载。基于 CDP 裸 Chrome（`navigator.webdriver=false`）规避反爬。

结果 **输出到 stdout（JSON）**，同时**落盘到文件**（stdout 有大小上限，需要全文时读落盘文件）。

## 快速开始（首次使用）

前置：**Windows** / **Google Chrome** / **Python 3.9+** / 能访问 `wanfangdata.com.cn` / （详情/下载还需要**机构订阅**）

```powershell
# 1) 装依赖（requirements.txt 在 scripts/ 下）
pip install -r scripts/requirements.txt

# 2) 搜索不需要登录，可以先跑起来验证环境
python scripts/wf_search.py --q "机器学习" --type 期刊论文 --rows 5 --parallel 1

# 3) 要抓引用格式 / 章节树 / 下载时，先确认机构访问已生效
python scripts/chrome_session.py --start
#    → 用机构 IP 或 CARSI 登录，打开任意万方页面确认已显示机构访问状态
python scripts/chrome_session.py --status     # 应看到: CDP 在线: ... (port 9222)

# 4) 下载（--save-dir 必填）
python scripts/wf_paper_download.py --url "https://d.wanfangdata.com.cn/periodical/hebgydxxb202606001" --save-dir ".\out\papers"

# 5) 用完可关闭 Chrome
python scripts/chrome_session.py --stop
```

三条约定：

- **所有命令都在仓库根目录执行**（脚本路径写成 `scripts/xxx.py`）。
- **日志落在"当前工作目录"的 `logs/`**：在仓库根跑 → `<repo>/logs/`；在 `scripts/` 里跑 → `scripts/logs/`。可用 `WF_LOGS_DIR` 固定。
- `--status` 只证明 **Chrome/CDP 在线**，**不显示机构访问状态**；确认办法是跑一次搜索/详情看是否拿到内容。

## 运行前提与约定

| 项 | 说明 |
|---|---|
| Chrome | 按 `Program Files` → `Program Files (x86)` → `%LOCALAPPDATA%` 顺序查找；都没找到会报 `Chrome 未找到` |
| profile / 端口 | profile 在 `%USERPROFILE%\.yzdpw_state\chrome-cdp`；CDP 端口 9222-9299，端口号写在同目录 `.cdp_port` |
| 四个 skill 共用 | xhs / zhihu / ieee / wanfang **共用这一个 Chrome 和 profile**，机构访问/登录一次四个都能用 |
| 自动启动 | 脚本会**自动启动或复用** Chrome（搜索不必先 `--start`）；详情/下载需要机构访问，必须先 `--start` 激活 |
| Chrome 启动方式 | 裸启动：`--remote-debugging-port` + `--remote-allow-origins=*`，**不带** `--enable-automation` → `navigator.webdriver=false` |
| 权限边界 | 搜索**无需登录**；引用弹窗、章节树、整篇/分章下载需机构访问（`hasInstitutionalAccess` 是页面级检测，与下载接口权限可能不一致，以下载实际结果为准） |
| 下载机制 | 下载按钮带 transaction token，**必须真实鼠标点击**才生效（脚本已处理）；下载**顺序执行**防限流 |
| 静默限流 | 万方对高频访问会**静默限流**：页面正常但结果为空且无提示 → 脚本在 0 条时于 `notice` 里提示「疑似限流」，需冷却几分钟重试 |

## 命令

### wf_search.py

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--q` | 必填 | — | 搜索关键词（可重复，1-8 个） |
| `--type` | 可选 | 全部 | 资源类型（可重复）：`期刊论文` / `学位论文` / `会议论文` / `专利` |
| `--year` | 可选 | — | 年份 `YYYY` 或 `YYYY-YYYY`（≥1793） |
| `--rows` | 可选 | 20 | 每关键词最大结果数（≤20） |
| `--page` | 可选 | 1 | 页码（SPA 分页靠点击，不是 URL 参数） |
| `--parallel` | 可选 | 2 | 并行关键词数（1-8） |

**输出：** `{ count, results, logPath }`，每条 result 含 `keyword, totalResults, pageInfo, perPage, items[{ id, title, type, url, snippet }]`。

- **无结果 / 疑似限流 → `notice`**（页面正常但 0 条时会提示"疑似限流"，冷却几分钟再试）
- 异常（页面超时等）→ `error`

### wf_detail.py

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--url` | 必填 | — | 详情 URL（可重复，1-8 个） |
| `--parallel` | 可选 | 2 | 并行任务数（1-8） |

**输出：** `{ count, results, logPath }`，每条 result 含：

- 公共：`url, type, hasInstitutionalAccess, title, authors[], institution, doi, abstract, keywords[], readCount, downloadCount, citedCount`
- 类型专属：期刊 `pubDate`；学位 `discipline/advisor/degreeYear` + **`chapters` 章节树**；会议 `conferenceDate`；专利 `patentType/patentNumber/pubDate/claims`
- 另有 `references[]`、`citations[]`（GB/T 7714 / MLA / APA 三段，**需机构登录**，否则为 `"需要登录获取"`）
- **`references` 不是每篇都有**：万方详情页对部分期刊不提供参考文献区块（实测：`innerText` 与 `textContent`
  里都搜不到"参考文献"），此时该字段直接不出现——**这是数据源如此，不是抓取失败**，别据此去动提取逻辑；
  真要该文献的参考文献，得从 PDF 原文里找。

`type` 形如 `[期刊论文]/[学位论文]/[会议论文]/[专利]`，学位按授予学位细化为 `[博士论文]/[硕士论文]`。空的 `doi`/`keywords` 字段会被删除；无效 URL → `"Invalid URL — 404 page not found"`。

### wf_paper_download.py

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--url` | 必填 | — | 详情 URL，可带 `\|ids` 章节索引（可重复，1-5 个，顺序执行） |
| `--save-dir` | 必填 | — | 保存目录（不存在会自动创建） |

**`|ids` 语法**：逗号分隔章节索引，支持范围展开。

- 顶层章节：`|1,5-9` → 1、5、6、7、8、9
- 子章节：`|9.1,9.2`；**含 `.` 的范围只取两端**（`|9.1-9.4` → 9.1、9.4，不展开中间）
- 索引从哪来：`wf_detail` 输出里 `chapters[].label` 的**前缀**（label `9.1 研究背景与意义11-13 页` → 索引 `9.1`）
- 注意：**不是每篇论文都有子章节**：只用顶层索引即可；索引不在树里会报 `"章节索引未匹配 — ids=..."`

两种模式：

- **整篇**（不带 `|ids`）→ `<save-dir>/<标题>.pdf`
- **分章**（带 `|ids`）→ `<save-dir>/<标题>/(idx) 章节名.pdf`（标题取不到时用文档 ID，如 `D04070817`）

**输出：** `{ count, results, logPath }`，每条含 `download: { name, path, size }`（整篇 `size` 是字节数，分章是 `"N files"`）。

- 404 → `"Invalid URL — 404 page not found"`；未登录 → `"Not logged in"`

**用法：**

```powershell
python scripts/wf_search.py --q "机器学习" --type 学位论文 --rows 10
python scripts/wf_detail.py --url "https://d.wanfangdata.com.cn/periodical/hebgydxxb202606001"
python scripts/wf_detail.py --url "https://d.wanfangdata.com.cn/thesis/D04337667"        # 含章节树 chapters
python scripts/wf_paper_download.py --url "https://d.wanfangdata.com.cn/periodical/hebgydxxb202606001" --save-dir ".\out\papers"
python scripts/wf_paper_download.py --url "https://d.wanfangdata.com.cn/thesis/D04337667|9.1,9.2" --save-dir ".\out\papers"   # 分章
```

## 输出落盘

所有脚本在写 stdout 的**同时**，把**同一份完整结果**写入：

```
<WF_LOGS_DIR>/<脚本名>-<时间戳>.json      # 默认 <运行目录>/logs/
```

绝对路径会同时出现在 **stderr** 和 **stdout 的 `logPath` 字段**。

- 文件里**不含** `logPath`，是纯结果。
- **落盘发生在写 stdout 之前** → stdout 被截断或失败也不丢结果。
- 读取方式：

```powershell
$f = (Get-Content .\logs\wf_search-*.json | Select-Object -Last 1)   # 或直接用 logPath
Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object count
```

## 配置（环境变量，前缀 `WF_`）

`scripts/config.py` 集中配置，`WF_*` 覆盖（键名 = `WF_` + 点号转下划线大写，如 `timeout.page_load` → `WF_TIMEOUT_PAGE_LOAD`）。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WF_TIMEOUT_PAGE_LOAD` | `20` | 搜索页加载判定总时长(s)（万方渲染 8-16s） |
| `WF_TIMEOUT_DETAIL_LOAD` | `30` | 详情页/章节页元素出现等待(s) |
| `WF_TIMEOUT_CITE_MODAL` | `5` | 引用弹窗内容就绪等待(s) |
| `WF_TIMEOUT_RENDER_WAIT` | `2` | 渲染等待默认(s) |
| `WF_TIMEOUT_DOWNLOAD_EVENT` | `60` | 下载事件等待超时(s) |
| `WF_TIMEOUT_CDP` | `30` | CDP WebSocket 超时(s) |
| `WF_RATE_LIMIT` | `8,18` | 下载任务间隔范围(s) |
| `WF_PARALLEL_LIMIT` | `2` | `--parallel` 的默认值 |
| `WF_CAPS_REF_PAGES` | `50` | 参考文献翻页上限 |
| `WF_CAPS_CHAPTER_ROUNDS` | `10` | 章节树展开轮数上限 |
| `WF_TRUNC_ABSTRACT` | `2000` | 摘要/主权项截断 |
| `WF_TRUNC_SNIPPET` | `250` | 搜索摘要截断 |
| `WF_TRUNC_FILENAME` | `120` | 下载文件名截断 |
| `WF_HUMAN_SCROLL_DELTA` / `WF_HUMAN_PAUSE` / `WF_HUMAN_MOUSE_PROB` | — | 人类行为模拟 |
| `WF_TABS_MAX` | `30` | tab 数上限，达到即**只告警**（不自动关 tab；`0`=不启用） |
| `WF_LOGS_DIR` | `<运行目录>/logs` | 结果落盘目录（可设成固定路径） |

设置方式（两种 shell 都给，别混用）：

```powershell
# PowerShell
$env:WF_RATE_LIMIT = "10,20"; python scripts/wf_search.py --q "机器学习" --rows 5
```
```cmd
:: CMD
set WF_RATE_LIMIT=10,20
python scripts\wf_search.py --q "机器学习" --rows 5
```

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 搜索返回 `count=0` 且 `notice` 提到疑似限流 | 万方静默限流 → **冷却几分钟**再试；别连续高频跑 |
| `"Invalid URL — 404 page not found"` | URL 不对或已失效 → 用 `wf_search` 结果里的 `url` 原样传入 |
| `"Not logged in"` | 机构访问没生效 → `python scripts/chrome_session.py --start` 后重新激活 |
| 下载报错含 `HTTP 403/302` 或非 PDF | 机构未订阅该文献，或访问失效 |
| `"章节索引未匹配 — ids=..."` | 索引不在章节树里 → 先 `wf_detail` 看 `chapters[].label` 前缀；**不是每篇都有子章节** |
| 分章下载目录名是 `D04070817` 这类 ID | part 页取不到标题时的兜底命名 → 正常 |
| `citations` 显示 `"需要登录获取"` | 引用格式需机构登录 → `--start` 激活机构访问 |
| 专利的 `patentType`/`claims` 为空 | 这些字段异步加载，脚本最多等 10s → 稍后重跑 |
| `Chrome 未找到` | Chrome 没装或装在别处 → 安装 Chrome，或在 `scripts/cdp_base.py` 的 `CHROME_PATHS` 里加路径 |
| `Chrome 30 秒内未就绪` | 启动超时 → 检查 Chrome 弹窗/杀软拦截，重试 |
| `9222-9299 端口全部被占` | 端口耗尽 → 关掉多余的调试用 Chrome |
| 下载后浏览器多了几个万方页面 | 站点自己 `window.open` 的打包/下载页 → 脚本**已自动清理**（限本次新出现 + 同注册域）；异常残留手动关即可 |
| 结果被截断（stdout 只看到一部分） | 宿主对 stdout 有大小上限 → 读 `logPath` 指向的文件 |

## 已知限制

- **静默限流**：万方高频访问会返回"页面正常但 0 条"，需冷却数分钟；脚本用 `notice` 提示。
- **专利字段异步**：专利类型/主权项异步加载，脚本最多等 10s，仍可能为空。
- **分章依赖 DOM**：`|ids` 依赖章节树 DOM，万方改版后索引可能失效。
- **引用格式需登录**：未登录时 `citations` 为 `"需要登录获取"`。
- **tab 管理**：每个关键词/URL 用一个 tab，用完即关（`close_page`，学位论文的章节树 tab 也一样）。tab 总数达 `WF_TABS_MAX` 时**只告警不自动关**——四个 skill 共用一个 Chrome，自动关"空 tab"会误伤其他任务刚建好、还没 navigate 的 tab。
- **下载自开的 tab**：万方会自己 `window.open` 出打包/下载页（每次 +1，Chrome 重启还会恢复）；脚本在下载结束后自动关掉**本次新出现且同注册域（`wanfangdata.com.cn`）**的 tab——其它站点、你手动开的页、下载前就存在的 tab **一律不动**。
- **最后一个 tab**：`close_page` 会先建一个 `about:blank` 占位页再关它（直接关会让整个共享 Chrome 退出，不关又会留下上次的页面）；占位建不出来时保留该 tab。
- **依赖站点结构**：选择器依赖万方当前页面结构，站点改版可能让某条路径静默失效。

## 脚本与测试

```
scripts/
  wf_search.py             搜索（facet URL + SPA 点击翻页，四类型）
  wf_detail.py             详情（四类型专属字段 + 参考文献翻页 + 引用弹窗 + 章节树）
  wf_paper_download.py     整篇/分章下载（真实鼠标点击 + 下载事件捕获 + zip 解压重命名）
  wf_parser.py             提取纯函数（URL/facet/结果/章节 ids/引用切分）
  cdp_base.py              CDP 客户端 + Chrome 启动 + 人类行为模拟 + tab 生命周期 + 下载能力
  chrome_session.py        登录会话管理（--start/--status/--stop）
  config.py                集中配置（可用 WF_* 覆盖）
  requirements.txt         依赖清单

tests/                     离线单测（不需要 Chrome / 网络 / 机构权限）
```

跑测试（不需要登录、不需要机构权限）：

```powershell
pip install pytest
python -m pytest tests -q
```
