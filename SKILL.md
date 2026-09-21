---
name: wanfang-research
description: 万方数据学术论文检索 — 搜索、详情、整篇/分章下载，CDP Chrome 自动化
---

# 万方数据 Research

万方数据学术检索：搜索（期刊/学位/会议/专利四类型）、详情（参考文献、引用格式、学位论文章节树）、论文整篇/分章下载。基于 CDP 裸 Chrome（`navigator.webdriver=false`）规避反爬，输出 stdout JSON。

## 安装

1. 确认 Chrome 已安装（`C:\Program Files\Google\Chrome\Application\chrome.exe`）
2. 安装依赖：`pip install -r requirements.txt`（仅 `websocket-client`）
3. 启动 Chrome 会话：`python chrome_session.py --start`，在 Chrome 中通过机构 IP / CARSI 登录；登录态持久保存，`--status` 查看、`--stop` 关闭

profile 位于 `%USERPROFILE%\.yzdpw_state\chrome-cdp`，CDP 端口记录于同目录 `.cdp_port`。

**权限说明**：搜索无需登录；引用弹窗、章节树、整篇/分章下载需机构访问权限（`hasInstitutionalAccess` 为页面级检测，与下载接口权限可能不完全一致，以下载实际结果为准）。下载按钮带 transaction token 逻辑，必须真实鼠标点击触发（脚本内已处理）。下载顺序执行防限流。

**限流提示**：万方对高频访问会静默限流（页面正常但结果为空且无提示）——脚本在搜索返回 0 条时会在 `notice` 中提示"疑似限流"，需冷却几分钟后重试。

**输出落盘**：所有脚本 stdout 输出完整 JSON 的同时，把**同一份完整结果**写入 `<WF_LOGS_DIR>/<脚本名>-<时间戳>.json`（默认 `logs/`，即脚本启动时的工作目录；可用 `WF_LOGS_DIR` 覆盖），并在 stderr 与 stdout 的 `logPath` 字段给出绝对路径。落盘发生在写 stdout **之前**，stdout 被截断或失败也不丢结果；需要全文时直接读该文件（多对话接力同理）。

## 命令

### wf_search.py

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--q` | ✅ | — | 搜索关键词（可重复，1-8 个） |
| `--type` | ❌ | 全部 | 资源类型（可重复）：`期刊论文` / `学位论文` / `会议论文` / `专利` |
| `--year` | ❌ | — | 年份 `YYYY` 或 `YYYY-YYYY`（≥1793） |
| `--rows` | ❌ | 20 | 每关键词最大结果数（≤20） |
| `--page` | ❌ | 1 | 页码（SPA 分页靠点击，非 URL 参数） |
| `--parallel` | ❌ | 2 | 并行关键词数（1-8） |

**输出：** `{ count, results, logPath }`，每条 result 含 `keyword, totalResults, pageInfo, perPage, items[{ id, title, type, url, snippet }]`。无结果 `notice`；限流 `error`；超时 `error`。

### wf_detail.py

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--url` | ✅ | — | 详情 URL（可重复，1-8 个） |
| `--parallel` | ❌ | 2 | 并行任务数（1-8） |

**输出：** `{ count, results, logPath }`，每条 result 含 `url, type`（`[期刊论文]/[学位论文]/[会议论文]/[专利]`，学位按授予学位细化为 `[博士论文]/[硕士论文]`）、`hasInstitutionalAccess, title, authors[], institution, doi, abstract, keywords[], readCount, downloadCount, citedCount`；类型专属字段（期刊 `pubDate`；学位 `discipline/advisor/degreeYear` + `chapters` 章节树；会议 `conferenceDate`；专利 `patentType/patentNumber/pubDate/claims`）；另有 `references[]`、`citations[]`（GB/T 7714 / MLA / APA 三段，需登录）。空 `doi`/`keywords` 字段删除；无效 URL → `"Invalid URL — 404 page not found"`。

### wf_paper_download.py

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--url` | ✅ | — | 详情 URL，可带 `|ids` 章节索引（可重复，1-5 个，顺序执行） |
| `--save-dir` | ✅ | — | 保存目录 |

`|ids` 语法：逗号分隔章节索引，支持范围展开，如 `|1,5-9`（顶层章节）、`|9.1,9.2`（子章节）。索引就是 `wf_detail` 输出里 `chapters[].label` 的前缀（如 label `9.1 研究背景与意义11-13 页` → 索引 `9.1`），注意**不是每篇论文都有子章节**；含 `.` 的范围只取两端（`|9.1-9.4` → 9.1、9.4，不展开中间）。

- **整篇**（无 `|ids`）：下载全文 PDF → `<save-dir>/<标题>.pdf`
- **分章**（有 `|ids`）：进 part 页勾选章节 → zip 下载解压 → `<save-dir>/<标题>/(idx) 章节名.pdf`

**输出：** `{ count, results, logPath }`，每条含 `download: { name, path, size }`（整篇 size 为字节数，分章为 `"N files"`）；404 → `"Invalid URL — 404 page not found"`；未登录 → `"Not logged in"`；索引不匹配 → `"章节索引未匹配 — ids=..."`。

**用法：**
```powershell
python wf_search.py --q "机器学习" --type 学位论文 --rows 10
python wf_detail.py --url "https://d.wanfangdata.com.cn/periodical/hebgydxxb202606001"
python wf_detail.py --url "https://d.wanfangdata.com.cn/thesis/D04337667"   # 含章节树 chapters
python wf_paper_download.py --url "https://d.wanfangdata.com.cn/periodical/hebgydxxb202606001" --save-dir "D:\papers"
python wf_paper_download.py --url "https://d.wanfangdata.com.cn/thesis/D04337667|9,10" --save-dir "D:\papers"
```

## 配置（环境变量，前缀 `WF_`）

调优参数集中在 `config.py`，用 `WF_*` 环境变量覆盖（键名 `WF_` + 点号转下划线大写，如 `timeout.page_load` → `WF_TIMEOUT_PAGE_LOAD`）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WF_TIMEOUT_PAGE_LOAD` | `20` | 搜索页加载判定总时长(s)（万方渲染 8-16s） |
| `WF_TIMEOUT_DETAIL_LOAD` | `30` | 详情页/章节页元素出现等待(s) |
| `WF_TIMEOUT_CITE_MODAL` | `5` | 引用弹窗内容就绪等待(s) |
| `WF_TIMEOUT_RENDER_WAIT` | `2` | 渲染等待默认(s) |
| `WF_TIMEOUT_DOWNLOAD_EVENT` | `60` | 下载事件等待超时(s) |
| `WF_TIMEOUT_CDP` | `30` | CDP WebSocket 超时(s) |
| `WF_RATE_LIMIT` | `8,18` | 下载任务间隔范围(s) |
| `WF_PARALLEL_LIMIT` | `2` | 并行上限 |
| `WF_CAPS_REF_PAGES` | `50` | 参考文献翻页上限 |
| `WF_CAPS_CHAPTER_ROUNDS` | `10` | 章节树展开轮数上限 |
| `WF_TRUNC_ABSTRACT` | `2000` | 摘要/主权项截断 |
| `WF_TRUNC_SNIPPET` | `250` | 搜索摘要截断 |
| `WF_TRUNC_FILENAME` | `120` | 下载文件名截断 |
| `WF_HUMAN_SCROLL_DELTA` / `WF_HUMAN_PAUSE` / `WF_HUMAN_MOUSE_PROB` | — | 人类行为模拟 |
| `WF_TABS_MAX` | `30` | tab 数上限，达到即**只告警**（不自动关 tab；0=不启用） |
| `WF_LOGS_DIR` | `<运行目录>/logs` | 完整结果落盘目录（默认 = 脚本启动时的工作目录） |

## 已知限制

- 每个关键词/URL 用一个 tab，用完即关（`close_page`，学位论文的章节树 tab 也一样）；tab 总数达 `WF_TABS_MAX` 时**只告警不自动关** —— 四个 skill 共用一个 Chrome，自动关"空 tab"会误伤其他任务刚建好、还没 navigate 的 tab
- 下载时万方会自己 `window.open` 出打包/下载页（实测每次 +1，Chrome 重启还会恢复）；脚本在下载结束后会自动关掉**本次新出现且同注册域（`wanfangdata.com.cn`）**的 tab —— 其它站点、你手动开的页、下载前就存在的 tab 一律不动
- 若只剩最后一个 tab：`close_page` 会先建一个空白页（about:blank）占位再关它 —— 直接关会让整个共享 Chrome 退出，不关又会留下上次的搜索结果/详情页
- 万方对高频访问静默限流：页面正常但结果为空，需冷却数分钟重试
- 专利详情字段（专利类型/主权项）异步加载，脚本等待字段出现（最多 10s）
- 分章下载依赖章节树 DOM，万方改版后 `|ids` 索引可能失效
- 引用弹窗需机构登录；未登录时 `citations` 为 `"需要登录获取"`

## 脚本清单

```
scripts/
  wf_search.py             搜索（facet URL + SPA 点击翻页，四类型）
  wf_detail.py             详情（四类型专属字段 + 参考文献翻页 + 引用弹窗 + 章节树）
  wf_paper_download.py     整篇/分章下载（真实鼠标点击 + 下载事件捕获 + zip 解压重命名）
  wf_parser.py             提取纯函数（URL/facet/结果/章节 ids/引用切分）
  cdp_base.py              CDP 客户端 + Chrome 裸启动 + 人类行为模拟 + 下载能力
  chrome_session.py        登录会话管理（--start/--status/--stop）
```
