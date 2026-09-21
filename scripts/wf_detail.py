#!/usr/bin/env python3
"""wf_detail.py — 万方数据详情 CLI（Python 重构版）

用法:
  python wf_detail.py --url "https://d.wanfangdata.com.cn/thesis/D001"
输出: stdout JSON { count, results: [...] }；进度日志 → stderr
"""
import argparse
import json
import re
import sys
import time

from cdp_base import close_page, create_page, ensure_cdp, setup_stdout, write_log
from config import get
from wf_parser import (REF_HEAD, REF_LINE_RE, detect_type, extract_refs,
                       parse_citations, split_claims)

MAX_DETAIL = 8
TYPE_LABELS = {"periodical": "[期刊论文]", "thesis": "[学位论文]",
               "conference": "[会议论文]", "patent": "[专利]"}

EXPAND_ABSTRACT_JS = """() => {
  const b = document.querySelector('.abstractIcon.btn, span[title="查看全部"]');
  if (b) { b.click(); return true; }
  return false;
}"""
TITLE_JS = """() => {
  const sels = ['.detailTitleCN span span', '.detailTitleCN span', '.detailTitle span', 'h1'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && (el.textContent || '').trim()) return (el.textContent || '').trim();
  }
  return '';
}"""
STATS_JS = """() => {
  const miner = document.querySelector('.miner');
  const t = miner ? miner.textContent : (document.body ? document.body.textContent : '');
  const g = (re) => { const m = t.match(re); return m ? m[1] : null; };
  return {
    readCount: g(/文摘阅读 (\\d+)/),
    downloadCount: g(/下载 (\\d+)/),
    citedCount: g(/被引 (\\d+)/),
  };
}"""
CITE_OPEN_JS = """() => {
  const el = Array.from(document.querySelectorAll('*'))
    .find(e => (e.textContent || '').trim() === '引用' && e.children.length === 0);
  if (!el) return false;
  const box = el.closest('.collection') || document.querySelector('.collection');
  (box || el).click();
  return true;
}"""
CITE_TEXT_JS = """() => {
  const body = Array.from(document.querySelectorAll('.modal-body, .ivu-modal-body'))
    .find(b => b.offsetParent !== null && (b.textContent || '').trim().length > 10);
  if (body) return body.textContent || '';
  const wrap = Array.from(document.querySelectorAll('.ivu-modal-wrap'))
    .find(w => w.offsetParent !== null && (w.textContent || '').trim().length > 10);
  return wrap ? (wrap.textContent || '') : '';
}"""
CITE_CLOSE_JS = """() => {
  const b = document.querySelector('.ivu-modal-close');
  if (b) { b.click(); return true; }
  return false;
}"""
# 展开轮数 {rounds} 由调用处 replace 注入 caps.chapter_rounds（JS 内对象字面量花括号多，
# .format()/f-string 需转义，故用 replace 占位符替换）
CHAPTER_TREE_JS = """async () => {
  for (let round = 0; round < {rounds}; round++) {
    const arrows = Array.from(document.querySelectorAll('.ivu-tree-arrow'))
      .filter(a => !a.className.includes('open') && a.querySelector('i'));
    if (!arrows.length) break;
    arrows.forEach(a => a.click());
    await new Promise(r => setTimeout(r, 800));
  }
  const roots = Array.from(document.querySelectorAll('.ivu-tree > .ivu-tree-children > li, .ivu-tree > li'));
  const walk = (li, depth, counters) => {
    counters.length = depth + 1;
    counters[depth] = (counters[depth] || 0) + 1;
    const idx = counters.slice(0, depth + 1).join('.');
    const titleEl = li.querySelector('.ivu-tree-title');
    const label = titleEl ? (titleEl.textContent || '').trim().replace(/^\\d+(?:\\.\\d+)*\\s+/, '') : '';
    const node = { label: idx + ' ' + label, children: [] };
    li.querySelectorAll(':scope > .ivu-tree-children > li').forEach(k => {
      node.children.push(walk(k, depth + 1, counters));
    });
    return node;
  };
  const counters = [];
  const out = [];
  roots.forEach(r => out.push(walk(r, 0, counters)));
  return out;
}"""
# 引用翻页（每页 1000ms）
REFS_NEXT_JS = """() => {
  const next = document.querySelector('.ivu-page-next:not(.ivu-page-disabled)');
  if (next) { next.click(); return true; }
  return false;
}"""
# 登录态：机构账号条文本 ≠ '登录机构账号'
ACCESS_JS = """() => {
  const el = document.querySelector('[class*=anxs-8qwe-list-jg]');
  return el ? (el.textContent || '').trim() !== '登录机构账号' : false;
}"""
# 正文 innerText（对齐 Node 版 document.body.innerText，保留换行供正则行匹配）
BODY_TEXT_JS = "document.body ? document.body.innerText.replace(/\\t/g, ' ') : ''"
AUTHORS_JS = """() => Array.from(document.querySelectorAll('a.test-detail-author'))
  .map(e => (e.textContent || '').trim()).filter(Boolean)"""
INST_JS = """() => {
  const el = document.querySelector('.organization') || document.querySelector('.test-detail-org');
  return el ? (el.textContent || '').trim() : '';
}"""
DOI_JS = """() => {
  const d = document.querySelector('.doiStyle');
  return d ? (d.textContent || '').trim() : '';
}"""
KEYWORDS_JS = """() => Array.from(document.querySelectorAll('.itemKeyword a span'))
  .map(e => (e.textContent || '').trim()).filter(Boolean)"""
PATENT_META_JS = """() => {
  const links = Array.from(document.querySelectorAll('.detailIntro a.multi-sep'));
  const authors = links
    .filter(a => /^[\\u4e00-\\u9fa5]{2,4}$/.test((a.textContent || '').trim()))
    .map(a => (a.textContent || '').trim());
  const inst = links.find(a => /(?:公司|大学|学院|研究所|研究院|科学院|中心)/.test(a.textContent || ''));
  return { authors, institution: inst ? (inst.textContent || '').trim() : '' };
}"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="万方数据详情")
    ap.add_argument("--url", action="append", required=True,
                    help="详情 URL（可重复，1-8 个）")
    ap.add_argument("--parallel", type=int, default=None, help="并行任务数（默认 2，最大 8）")
    args = ap.parse_args(argv)
    if len(args.url) > MAX_DETAIL:
        ap.error(f"--url 最多 {MAX_DETAIL} 个")
    args.parallel = args.parallel if args.parallel is not None else get("parallel.limit")
    args.parallel = max(1, min(args.parallel, 8))
    return args


def _body_text(client):
    return client.evaluate(BODY_TEXT_JS) or ""


def _collect_references(client):
    """参考文献：正文含 '参考文献' 才提取；点 .ivu-page-next 翻页，最多 50 页每页 1000ms"""
    if REF_HEAD not in _body_text(client):
        return []
    seen, refs = set(), []
    for _ in range(get("caps.ref_pages")):
        for r in extract_refs(_body_text(client)):
            m = REF_LINE_RE.match(r)
            num = m.group(1) if m else r
            if num not in seen:
                seen.add(num)
                refs.append(r)
        if not client.evaluate(f"({REFS_NEXT_JS})()"):
            break
        time.sleep(1)
    refs.sort(key=lambda r: int(REF_LINE_RE.match(r).group(1)) if REF_LINE_RE.match(r) else 0)
    return refs


def _extract_citations(client, has_access):
    """引用弹窗（需机构登录）；未出弹窗 → '需要登录获取'"""
    if not has_access:
        return "需要登录获取"
    if not client.evaluate(f"({CITE_OPEN_JS})()"):
        return "需要登录获取"
    out = "需要登录获取"
    if client.wait_for(
            "(() => { const b = Array.from(document.querySelectorAll('.modal-body, .ivu-modal-body'))"
            ".find(x => x.offsetParent !== null && (x.textContent || '').trim().length > 10); return !!b; })()",
            timeout=get("timeout.cite_modal"), interval=0.3):
        time.sleep(get("timeout.render_wait"))
        out = parse_citations(client.evaluate(f"({CITE_TEXT_JS})()") or "")
    try:
        client.evaluate(f"({CITE_CLOSE_JS})()")
    except Exception:
        pass
    return out


def _collect_chapters(port, url):
    """学位论文章节树：另开 tab 访问 /part/thesis/，展开后返回 {label, children} 树"""
    ch = None
    try:
        ch = create_page(port)
        ch.navigate(url.replace("/thesis/", "/part/thesis/"))
        # part 页渲染等待（render_wait 默认 2s，原硬编码 3s）
        time.sleep(get("timeout.render_wait"))
        chapter_js = CHAPTER_TREE_JS.replace("{rounds}", str(get("caps.chapter_rounds")))
        tree = ch.evaluate(f"({chapter_js})()", await_promise=True)
        return tree if isinstance(tree, list) and tree else None
    except Exception:
        return None
    finally:
        close_page(ch)


def scrape_meta(client, url, dtype):
    """详情页元数据提取。dtype: detect_type(url) 的结果"""
    client.navigate(url)
    # 详情元素加载：万方页面渲染可达 8-16s，等 30s；超时后再确认是否 404
    if not client.wait_for("!!document.querySelector('.detailIntro, .detailTitleCN, h1')",
                           timeout=get("timeout.detail_load"), interval=0.5):
        if client.evaluate("!!document.querySelector('.content-error')"):
            return {"url": url, "error": "Invalid URL — 404 page not found"}
        return {"url": url, "error": "页面加载超时"}

    # 展开摘要
    try:
        client.evaluate(f"({EXPAND_ABSTRACT_JS})()")
        # 摘要展开渲染等待（render_wait 默认 2s，原硬编码 0.5s）
        time.sleep(get("timeout.render_wait"))
    except Exception:
        pass

    # 登录态（机构账号）
    has_access = bool(client.evaluate(f"({ACCESS_JS})()"))

    # 标题（去尾部噪声：文摘阅读|下载|导出题录|被引 <数字>）
    title = re.sub(r"\s*(文摘阅读|下载|导出题录|被引\s*\d+)\s*$", "",
                   (client.evaluate(f"({TITLE_JS})()") or "").strip())

    # 统计
    stats = client.evaluate(f"({STATS_JS})()") or {}

    def _num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    read_count = _num(stats.get("readCount"))
    download_count = _num(stats.get("downloadCount"))
    cited_count = _num(stats.get("citedCount"))

    # 作者 / 机构
    authors = client.evaluate(f"({AUTHORS_JS})()") or []
    institution = client.evaluate(f"({INST_JS})()") or ""

    # DOI：.doiStyle 去前缀，fallback 正文
    doi = re.sub(r"^DOI\s*[：:]\s*", "", client.evaluate(f"({DOI_JS})()") or "", flags=re.I).strip()
    text = _body_text(client)
    if not doi:
        m = re.search(r"DOI[：:]\s*(10\.\S+)", text)
        doi = m.group(1) if m else ""

    # 摘要（截 2000，空白折叠对齐 Node 版）
    abstract = ""
    m = re.search(r"摘要[：:]\s*([\s\S]+?)(?=\n\s*(?:关键词|英文信息|基金|分类号))", text)
    if m:
        abstract = re.sub(r"\s+", " ", m.group(1)).strip()[:get("trunc.abstract")]

    # 关键词：DOM 优先，fallback 正文按 ; 或 ；分
    keywords = client.evaluate(f"({KEYWORDS_JS})()") or []
    if not keywords:
        m = re.search(r"关键词[：:]\s*([^\n]+)", text)
        if m:
            keywords = [k.strip() for k in re.split(r"[;；]", m.group(1)) if k.strip()]

    meta = {
        "title": title,
        "authors": authors,
        "institution": institution,
        "doi": doi,
        "abstract": abstract,
        "keywords": keywords,
        "readCount": read_count,
        "downloadCount": download_count,
        "citedCount": cited_count,
    }

    def _m(regex):
        mm = re.search(regex, text)
        return mm.group(1).strip() if mm else ""

    # 类型专属字段（正文正则，Node :196-222）
    if dtype == "periodical":
        meta["pubDate"] = _m(r"文献发表日期[：:]\s*(.+?)(?:\n)")
    elif dtype == "thesis":
        meta["discipline"] = _m(r"学科专业[：:]\s*(.+?)(?:\n)")
        meta["advisor"] = _m(r"导师姓名[：:]\s*(.+?)(?:\n)")
        meta["degreeYear"] = _m(r"学位年度[：:]\s*(.+?)(?:\n)")
        meta["_degree"] = _m(r"授予学位[：:]\s*(.+?)(?:\n)")
    elif dtype == "conference":
        meta["conferenceDate"] = _m(r"会议时间[：:]\s*(.+?)(?:\n)")
    elif dtype == "patent":
        # 专利字段区块异步加载，等待出现后再提取（最多 10s）
        client.wait_for(
            "document.body.innerText.includes('主权项') || document.body.innerText.includes('专利类型')",
            timeout=get("timeout.detail_load"))
        text = _body_text(client)
        pa = client.evaluate(f"({PATENT_META_JS})()") or {}
        if pa.get("authors"):
            meta["authors"] = pa["authors"]
        if pa.get("institution"):
            meta["institution"] = pa["institution"]
        m = re.search(r"摘要[：:]\s*([\s\S]+?)(?=\n\s*(?:专利类型|申请\/专利号|关键词))", text)
        if m:
            meta["abstract"] = re.sub(r"\s+", " ", m.group(1)).strip()[:get("trunc.abstract")]
        meta["patentType"] = _m(r"专利类型[：:]\s*(.+?)(?:\n)")
        meta["patentNumber"] = _m(r"申请\/专利号[：:]\s*(.+?)(?:\n)")
        meta["pubDate"] = _m(r"公开\/公告日[：:]\s*(.+?)(?:\n)")
        meta["claims"] = split_claims(text)

    # 类型标签（学位论文按授予学位细化）
    type_label = TYPE_LABELS.get(dtype) or (dtype or "unknown")
    if dtype == "thesis" and meta.get("_degree"):
        type_label = "[博士论文]" if "博士" in meta["_degree"] else "[硕士论文]"
    meta.pop("_degree", None)

    references = _collect_references(client)

    chapters = None
    if dtype == "thesis" and "/thesis/" in url:
        chapters = _collect_chapters(client.port, url)

    citations = _extract_citations(client, has_access)

    # 条件字段：空值删除，有内容才加
    result = {"url": url, "type": type_label, "hasInstitutionalAccess": has_access, **meta}
    if not result.get("doi"):
        result.pop("doi", None)
    if not result.get("keywords"):
        result.pop("keywords", None)
    if references:
        result["references"] = references
    if citations:
        result["citations"] = citations
    if chapters:
        result["chapters"] = chapters
    return result


def _detail_in_tab(port, url, args):
    client = None
    try:
        client = create_page(port)
        dtype = detect_type(url)
        return url, scrape_meta(client, url, dtype)
    except Exception as e:
        return url, e
    finally:
        close_page(client)


def main():
    from concurrent.futures import ThreadPoolExecutor
    args = parse_args()
    port = ensure_cdp()
    limit = min(len(args.url), args.parallel)
    results = []
    with ThreadPoolExecutor(max_workers=limit) as ex:
        for url, r in ex.map(_detail_in_tab, [port] * len(args.url), args.url,
                             [args] * len(args.url)):
            if isinstance(r, Exception):
                results.append({"url": url, "error": str(r)[:200]})
                sys.stderr.write(f"[wf-detail] {url} 失败: {str(r)[:120]}\n")
            else:
                results.append(r)
                sys.stderr.write(f"[wf-detail] {url} 完成\n")
            sys.stderr.flush()
    out = {"count": len(results), "results": results}
    # 先落盘再写 stdout：宿主对 stdout 有大小上限，Agent 拿全文直接读 logPath
    try:
        out["logPath"] = write_log(out, "wf_detail")
        sys.stderr.write(f"[wf-detail] 完整结果已落盘: {out['logPath']}\n")
    except Exception as e:
        sys.stderr.write(f"[wf-detail] 落盘失败: {str(e)[:80]}\n")
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    setup_stdout()
    main()
