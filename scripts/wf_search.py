#!/usr/bin/env python3
"""wf_search.py — 万方数据搜索 CLI（Python 重构版）

用法:
  python wf_search.py --q "机器学习" --type 期刊论文 --year 2020-2025 --rows 20
输出: stdout JSON { count, results: [...] }；进度日志 → stderr
"""
import argparse
import json
import sys
import time

from cdp_base import close_page, create_page, ensure_cdp, setup_stdout, write_log
from config import get
from wf_parser import (TYPE_MAP, build_search_url, extract_total, page_info,
                       parse_result_item)

MAX_ROWS = 20
RESULTS_SEL = "div.normal-list"

EXTRACT_RESULTS_JS = """() => Array.from(document.querySelectorAll('.normal-list'))
  .map(card => {
    const idEl = card.querySelector('.title-id-hidden');
    const titleEl = card.querySelector('.ajust .title, span.title');
    const typeEl = card.querySelector('.author-area .essay-type');
    const abstractEl = card.querySelector('.abstract-area');
    const ab = abstractEl ? (abstractEl.textContent || '').trim() : '';
    return {
      id_raw: idEl ? (idEl.textContent || '').trim() : '',
      title_raw: titleEl ? (titleEl.textContent || '').trim() : '',
      type_raw: typeEl ? '[' + (typeEl.textContent || '').trim() + ']' : '',
      snippet_raw: ab ? (ab.startsWith('摘要：') || ab.startsWith('摘要:') ? ab : '摘要：' + ab) : '',
    };
  })"""

NEXT_PAGE_JS = """() => {
  const pages = document.querySelector('.bottom-pagination, .pagination, [class*=pagination]');
  if (!pages) return false;
  const next = pages.querySelector('.next');
  if (next && !next.className.includes('disabled')) { next.click(); return true; }
  const byText = Array.from(pages.querySelectorAll('a, li, span, button'))
    .find(e => /^(>|下一页|next)$/i.test((e.textContent || '').trim()));
  if (byText && !byText.className.includes('disabled')) { byText.click(); return true; }
  const num = Array.from(pages.querySelectorAll('a, li'))
    .find(e => /^\\d+$/.test((e.textContent || '').trim()) && !e.className.includes('active'));
  if (num) { num.click(); return true; }
  return false;
}"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="万方数据搜索")
    ap.add_argument("--q", action="append", required=True, help="搜索关键词（可重复，1-8 个）")
    ap.add_argument("--page", default="1", help="页码（默认 1，SPA 分页靠点击）")
    ap.add_argument("--rows", type=int, default=20, help=f"每关键词最大结果数（默认 20，最大 {MAX_ROWS}）")
    ap.add_argument("--year", default=None, help="年份 YYYY 或 YYYY-YYYY（>=1793）")
    ap.add_argument("--type", action="append", choices=list(TYPE_MAP), help="资源类型（可重复）")
    ap.add_argument("--parallel", type=int, default=None, help="并行任务数（默认 2，最大 8）")
    args = ap.parse_args(argv)
    if len(args.q) > 8:
        ap.error("--q 最多 8 个")
    args.type = args.type or list(TYPE_MAP)
    args.rows = max(1, min(args.rows, MAX_ROWS))
    args.parallel = args.parallel if args.parallel is not None else get("parallel.limit")
    args.parallel = max(1, min(args.parallel, 8))
    return args


def detect_state(client):
    for _ in range(int(get("timeout.page_load") / 0.5)):
        try:
            head = client.get_body_text()[:400]
        except Exception:
            time.sleep(0.5)
            continue
        if "没有检索到数据" in head or "没有找到您要的资源" in head:
            return "noresult"
        if "已达查看上限" in head or "缩小检索结果范围" in head:
            return "limit"
        if client.evaluate(f"!!document.querySelector('{RESULTS_SEL}')"):
            return "normal"
        time.sleep(0.5)
    return "timeout"


def search_one(client, kw, args):
    url = build_search_url(kw, args.type, args.year, args.page)
    client.navigate(url)
    state = detect_state(client)
    if state == "noresult":
        return {"keyword": kw, "totalResults": 0, "items": [], "notice": "无搜索结果"}
    if state == "limit":
        return {"keyword": kw, "totalResults": 0, "items": [], "error": "已达查看上限，请稍后再试"}
    if state == "timeout":
        return {"keyword": kw, "totalResults": 0, "items": [], "error": "页面加载超时"}

    # SPA 分页：URL p= 无效，靠点击翻页到目标页
    target = int(args.page)
    for _ in range(target - 1):
        if not client.evaluate(f"({NEXT_PAGE_JS})()"):
            break
        time.sleep(get("timeout.render_wait"))
        if not client.wait_for(f"!!document.querySelector('{RESULTS_SEL}')", timeout=get("timeout.detail_load")):
            break

    body = client.get_body_text()
    raws = client.evaluate(f"({EXTRACT_RESULTS_JS})()") or []
    items, ids = [], set()
    for i, r in enumerate(raws, 1):
        it = parse_result_item(r)
        if not it or it["url"] in ids:
            continue
        ids.add(it["url"])
        it["id"] = len(items) + 1
        items.append(it)
        if len(items) >= args.rows:
            break
    total = extract_total(body)
    result = {"keyword": kw, "totalResults": total if total is not None else 0,
              "pageInfo": page_info(args.page, total if total is not None else 0, args.rows),
              "perPage": f"{len(items)}/{len(items)}", "items": items}
    if not items and not total:
        result["notice"] = ("结果为空：可能是无匹配，也可能是万方限流（限流时返回 0 条且不报错）。"
                            "建议冷却几分钟后重试")
    return result


def _search_in_tab(port, kw, args):
    client = None
    try:
        client = create_page(port)
        return kw, search_one(client, kw, args)
    except Exception as e:
        return kw, e
    finally:
        close_page(client)


def main():
    from concurrent.futures import ThreadPoolExecutor
    args = parse_args()
    port = ensure_cdp()
    limit = min(len(args.q), args.parallel)
    results = []
    with ThreadPoolExecutor(max_workers=limit) as ex:
        for kw, r in ex.map(_search_in_tab, [port] * len(args.q), args.q, [args] * len(args.q)):
            if isinstance(r, Exception):
                results.append({"keyword": kw, "totalResults": 0, "items": [], "error": str(r)[:200]})
                sys.stderr.write(f"[wf-search] {kw} 失败: {str(r)[:120]}\n")
            else:
                results.append(r)
                sys.stderr.write(f"[wf-search] {kw}: {r.get('totalResults', 0)} 条\n")
            sys.stderr.flush()
    out = {"count": len(results), "results": results}
    # 先落盘再写 stdout：宿主对 stdout 有大小上限，Agent 拿全文直接读 logPath
    try:
        out["logPath"] = write_log(out, "wf_search")
        sys.stderr.write(f"[wf-search] 完整结果已落盘: {out['logPath']}\n")
    except Exception as e:
        sys.stderr.write(f"[wf-search] 落盘失败: {str(e)[:80]}\n")
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    setup_stdout()
    main()
