#!/usr/bin/env python3
"""wf_parser.py — 万方数据提取纯函数（可离线单测）"""
import json
import math
import re

from config import get

TYPE_MAP = {"期刊论文": "Periodical", "学位论文": "Thesis",
            "会议论文": "Conference", "专利": "Patent"}
# 中文类型名 → detail URL 英文路径段（facet 用 TYPE_MAP 的英文值，URL 路径用此映射）
TYPE_PATH = {"期刊论文": "periodical", "学位论文": "thesis",
             "会议论文": "conference", "专利": "patent"}
MIN_YEAR = 1793
# 中文类型前缀（旧版 DOM，如 "期刊论文_12345"）；英文前缀（新版 DOM，如 "periodical_hebgydxxb202606001"）
ID_RE_CN = re.compile(r"^(期刊论文|学位论文|会议论文|专利)_(\d+)$")
ID_RE_EN = re.compile(r"^(periodical|thesis|conference|patent)_(.+)$")
TITLE_RE = re.compile(r"^\d+\.(?:目录\s*)?(.+?)(?=\s*(?:文摘阅读|在线阅读|$))")
SNIPPET_RE = re.compile(r"摘要：\s*(.*?)(?=\s*(?:在线阅读|整篇下载|分章下载|下载全文|$))", re.S)
TOTAL_RE = re.compile(r"找到([\d,]+)条")


def parse_year_range(year_arg):
    """'2020' → (2020,2020)；'2020-2025' → (2020,2025)；非法抛 ValueError"""
    if not year_arg:
        return None
    parts = [p.strip() for p in str(year_arg).split("-")]
    if len(parts) not in (1, 2) or not all(p.isdigit() for p in parts):
        raise ValueError(f"year 格式非法: {year_arg}")
    lo, hi = int(parts[0]), int(parts[-1])
    if lo < MIN_YEAR or hi < MIN_YEAR or lo > hi:
        raise ValueError(f"year 需 >= {MIN_YEAR} 且 lo<=hi: {year_arg}")
    return lo, hi


def build_search_url(kw, types, year, page):
    """构造 s.wanfangdata.com.cn 搜索 URL（facet JSON 编码）"""
    from urllib.parse import urlencode
    facet = [{"Type": {"label": types, "title": "资源类型",
                       "value": [TYPE_MAP[t] for t in types]}}]
    if year:
        lo, hi = parse_year_range(year)
        years = [str(y) for y in range(lo, hi + 1)]
        facet.append({"PublishYear": {"label": years, "title": "年份", "value": years}})
    query = [("q", kw), ("p", page), ("facet", json.dumps(facet, ensure_ascii=False))]
    return "https://s.wanfangdata.com.cn/paper?" + urlencode(query)


def extract_item_id(raw):
    """返回 (类型, id)。中文前缀返回中文类型名（parse_result_item 经 TYPE_PATH 映射为 URL 路径），
    英文前缀原样返回英文路径，兼容新旧两种 DOM。"""
    m = ID_RE_CN.match(raw or "")
    if m:
        return m.group(1), m.group(2)
    m = ID_RE_EN.match(raw or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def split_title(raw):
    m = TITLE_RE.match(raw or "")
    return (m.group(1) or "").strip() if m else (raw or "").strip()


def extract_total(body):
    m = TOTAL_RE.search(body or "")
    return int(m.group(1).replace(",", "")) if m else None


def page_info(page, total, page_size=20):
    return f"{page}/{max(1, math.ceil((total or 0) / page_size))}"


def parse_result_item(raw):
    """单条结果清洗。type_raw 如 '[硕士论文]'；id_raw 如 '期刊论文_123'"""
    ctype, cid = extract_item_id(raw.get("id_raw") or "")
    if not cid:
        return None
    snippet = SNIPPET_RE.search(raw.get("snippet_raw") or "")
    return {
        "title": split_title(raw.get("title_raw") or ""),
        "type": (raw.get("type_raw") or "").strip(),
        "url": f"https://d.wanfangdata.com.cn/{TYPE_PATH.get(ctype, ctype)}/{cid}",
        "snippet": (snippet.group(1).strip() if snippet else "")[:get("trunc.snippet")],
    }


# ── detail 提取 ────────────────────────────────────────
REF_HEAD = "参考文献"
REF_LINE_RE = re.compile(r"^\[(\d+)\]\s*(.+)")
REF_NOISE = ("仅看全文", "排序", "发表时间", "被引频次", "查看引文网络", "ISSN",
             "年,卷", "所属栏目", "CSTPCD", "北大核心", "CSSCI", "AMI")
CLAIMS_RE = re.compile(
    r"主权项[：:]\s*(.+?)(?=[\r\n]{1,}(?:专利图片|法律状态|相关文献|相关学者|相关机构)|\Z)",
    re.S,
)
CLAIM_STOPS = ("专利图片", "法律状态", "相关文献", "相关学者", "相关机构")


def detect_type(url):
    """按路径段判类型：thesis/periodical/conference/patent；未知返回 None"""
    for t in ("thesis", "periodical", "conference", "patent"):
        if f"/{t}/" in (url or ""):
            return t
    return None


def extract_refs(body):
    """正文 → ['[N] 文本', ...]；无参考文献返回 []"""
    idx = (body or "").find(REF_HEAD)
    if idx < 0:
        return []
    seg = body[idx + len(REF_HEAD):]
    refs, cur = [], None
    for line in seg.splitlines():
        m = REF_LINE_RE.match(line.strip())
        if m:
            if cur:
                refs.append(cur)
            cur = f"[{m.group(1)}] {m.group(2).strip()}"
        elif cur and line.strip():
            cur += " " + line.strip()
    if cur:
        refs.append(cur)
    return [r for r in refs if not any(n in r for n in REF_NOISE)]


#: `DOI：10.x` / `DOI:10.x`
DOI_RE = re.compile(r"DOI\s*[：:]\s*(10\.\S+?)(?:\s|$)", re.I)


def doi_before_references(body):
    """只在「参考文献」标题**之前**的文本里找 DOI；找不到标题则返回 `''`。

    ★ 修复实测 bug（2026-10）：万方「参考文献」列表里**每一条都带 `DOI:10.x`**，
      旧实现从**整页**正则抓第一个 → **必然抓成参考文献里某篇的 DOI**。
      实测证据：一篇中国学位论文（`thesis/D03561632`）被挂上
      `10.1016/j.measurement.2021.109273`（Elsevier），而该页「参考文献」之前的
      题录区**一个 DOI 都没有**（实测 headDois=[]，tailDoiCount=6）。

    **找不到「参考文献」标题时返回 `''`** —— 宁可缺，不许错：
    错误归属比缺字段更坏，读者会永远找不到那篇文献。
    """
    idx = (body or "").find(REF_HEAD)
    if idx < 0:
        return ""
    m = DOI_RE.search(body[:idx])
    return m.group(1).rstrip(".") if m else ""


def split_claims(body):
    m = CLAIMS_RE.search(body or "")
    text = m.group(1).strip() if m else ""
    for stop in CLAIM_STOPS:
        idx = text.find("\n" + stop)
        if idx > 0:
            text = text[:idx]
        else:
            idx2 = text.find(stop)
            if idx2 > 0:
                text = text[:idx2]
    return text.strip()[:get("trunc.abstract")]


def parse_citations(modal_text):
    """引用弹窗文本 → [GB/T段, MLA段, APA段]（按 'MLA格式'/'APA格式' 段标题切分）"""
    text = modal_text or ""
    parts = re.split(r"(?=MLA格式|APA格式)", text)
    out = []
    for seg in parts:
        seg = re.sub(r"\s*复制\s*$", "", seg.strip())
        if seg:
            out.append(seg)
    return out[:3]


def gen_chapter_label(counters):
    """[1,0,0] → '1'；[1,2,0] → '1.2'；[2,1,3] → '2.1.3'"""
    parts = [str(c) for c in counters if c]
    return ".".join(parts) if parts else ""


def parse_chapter_label(label):
    """'1.2 绪论 5-9 页' → (idx, title, pages)"""
    m = re.search(r"\s*(\d+-\d+)\s*页\s*$", label or "")
    pages = m.group(1) if m else ""
    rest = (label[:m.start()] if m else label).strip()
    parts = rest.split(" ", 1)
    idx = parts[0]
    title = parts[1] if len(parts) > 1 else ""
    return idx, title, pages


def parse_ids(ids_arg):
    """'1,5-7,2.0-2.3' → ['1','5','6','7','2.0','2.3']
    纯整数范围展开；含 '.' 的嵌套索引范围拆为两端（2.0-2.3 → 2.0, 2.3），不展开中间"""
    out = []
    for part in str(ids_arg).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            lo, hi = lo.strip(), hi.strip()
            if lo.isdigit() and hi.isdigit():
                out.extend(str(i) for i in range(int(lo), int(hi) + 1))
                continue
            if lo and hi:
                out.append(lo)
                out.append(hi)
                continue
        out.append(part)
    return out
