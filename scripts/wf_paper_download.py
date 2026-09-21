#!/usr/bin/env python3
"""wf_paper_download.py — 万方数据论文下载 CLI（整篇 + 分章）

用法:
  python wf_paper_download.py --url "https://d.wanfangdata.com.cn/thesis/D001" --save-dir ./papers
  python wf_paper_download.py --url "https://d.wanfangdata.com.cn/thesis/D001|1,5-6" --save-dir ./papers
输出: stdout JSON { count, results: [...] }；进度日志 → stderr

对齐 Node 版 wf-download.js：
- ids 语法（:41-61）、章节勾选（:142-167）、触发（:180-189）、
  解压重命名（:207-229）、顺序执行（:257-260）。
- 关键修正（相对 plan）：download_with_events 先设 Browser.setDownloadBehavior
  再调 trigger_fn，因此点击必须放在 trigger_fn 内、发生在设置之后，
  否则下载事件丢失。
- 下载按钮改用真实鼠标点击（cdp_base.real_click + 本地 _click_selector）：
  万方下载按钮在 mousedown/mouseup 阶段把 href 替换为带 transaction token 的
  下载 URL，JS .click()（只触发 click 事件）不触发该逻辑，点击后导航到无
  token 的 aspx URL 会被 302 回详情页。
"""
import argparse
import json
import os
import re
import sys
import time
import zipfile

from cdp_base import (CdpError, close_page, create_page, download_with_events,  # noqa: E402
                      ensure_cdp, real_click, setup_stdout, write_log)
from config import get
from wf_parser import parse_chapter_label, parse_ids

MAX_DOWNLOAD = 5

# 不再用于触发下载（万方按钮的 token 替换逻辑挂在 mousedown/mouseup 上，
# JS .click() 不生效）；保留作为按钮判定/调试参考。触发见 _click_selector。
FULL_DL_JS = """() => {
  const a = document.querySelector('a.download.buttonItem');
  if (a) { a.click(); return true; }
  const span = Array.from(document.querySelectorAll('span'))
    .find(e => (e.textContent || '').trim() === '整篇下载');
  if (span) { span.click(); return true; }
  return false;
}"""
# 无副作用的存在性检测：与 FULL_DL_JS 同一判定，但不点击（用于进入 download_with_events 前的报错）
HAS_FULL_DL_JS = """() => {
  if (document.querySelector('a.download.buttonItem')) return true;
  return Array.from(document.querySelectorAll('span'))
    .some(e => (e.textContent || '').trim() === '整篇下载');
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
  const out = [];
  const walk = (li, depth, counters) => {
    counters.length = depth + 1;
    counters[depth] = (counters[depth] || 0) + 1;
    const idx = counters.slice(0, depth + 1).join('.');
    const titleEl = li.querySelector('.ivu-tree-title');
    const label = titleEl ? (titleEl.textContent || '').trim().replace(/^\\d+(?:\\.\\d+)*\\s+/, '') : '';
    const node = { idx, label, kids: [] };
    li.querySelectorAll(':scope > .ivu-tree-children > li').forEach(k =>
      node.kids.push(walk(k, depth + 1, counters)));
    return node;
  };
  const counters = [];
  roots.forEach(r => out.push(walk(r, 0, counters)));
  return out;
}"""
# 注意: 返回纯数据 {idx, label, kids}（不含 DOM 引用，可直接 returnByValue 序列化）
CHECK_CHAPTER_JS = """(idx) => {
  const counters = [];
  let found = false;
  document.querySelectorAll('.ivu-tree li').forEach(li => {
    let depth = -1, p = li.parentElement;
    while (p && p !== document.body) {
      if (p.classList.contains('ivu-tree-children')) depth++;
      p = p.parentElement;
    }
    counters.length = depth + 1;
    counters[depth] = (counters[depth] || 0) + 1;
    if (counters.slice(0, depth + 1).join('.') === idx) {
      const cb = li.querySelector('label.ivu-checkbox-wrapper');
      if (cb) { cb.click(); found = true; }
    }
  });
  return found;
}"""
# 不再用于触发下载（原因同上）；保留作为按钮判定/调试参考。触发见 _click_selector。
CONFIRM_JS = """() => {
  const b = Array.from(document.querySelectorAll('button'))
    .find(e => (e.textContent || '').includes('确认下载'));
  if (b) { b.click(); return true; }
  return false;
}"""
# 无副作用的存在性检测：CONFIRM_JS 的判定，但不点击
HAS_CONFIRM_JS = """() => Array.from(document.querySelectorAll('button'))
  .some(e => (e.textContent || '').includes('确认下载'))"""
TITLE_JS = """() => {
  const sels = ['.detailTitleCN span span', '.detailTitleCN span', '.detailTitle span', 'h1'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && (el.textContent || '').trim()) return (el.textContent || '').trim();
  }
  // 回退：分章下载走的是 part 页，没有 detailTitle —— 用浏览器标题
  //（万方形如 "<论文标题>-万方数据知识服务平台"）
  return (document.title || '').replace(/\\s*[-—|]\\s*万方数据.*$/, '').trim();
}"""
ACCESS_TEXT_JS = """() => {
  const el = document.querySelector('[class*=anxs-8qwe-list-jg]');
  return el ? (el.textContent || '').trim() : '';
}"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="万方数据论文下载")
    ap.add_argument("--url", action="append", required=True,
                    help="详情 URL，可带 |ids 章节索引（可重复，1-5 个）")
    ap.add_argument("--save-dir", required=True, help="保存目录（必填）")
    args = ap.parse_args(argv)
    if len(args.url) > MAX_DOWNLOAD:
        ap.error(f"--url 最多 {MAX_DOWNLOAD} 个")
    args.save_dir = os.path.abspath(args.save_dir)
    os.makedirs(args.save_dir, exist_ok=True)
    return args


def _click_selector(client, selector):
    """滚动到元素并真实鼠标点击；元素不存在返回 False。

    支持 Playwright 风格后缀 :has-text('文本')（原生 querySelector 不支持该
    伪类，会抛 SyntaxError，故桥接为 CSS 部分 + 文本包含匹配），
    如 button:has-text('确认下载')。
    """
    m = re.match(r"^(.*):has-text\('([^']*)'\)$", selector)
    if m:
        css, text = m.group(1), m.group(2)
        expr = (f"(function(){{ const els = Array.from(document.querySelectorAll({json.dumps(css)}));"
                f" const el = els.find(e => (e.textContent || '').includes({json.dumps(text)}));"
                " if (!el) return null; el.scrollIntoView({block:'center'});"
                " const r = el.getBoundingClientRect();"
                " return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)}; })()")
    else:
        expr = (f"(function(){{ const el = document.querySelector({json.dumps(selector)});"
                " if (!el) return null; el.scrollIntoView({block:'center'});"
                " const r = el.getBoundingClientRect();"
                " return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)}; })()")
    rect = client.evaluate(expr)
    if not rect:
        return False
    real_click(client, rect["x"], rect["y"])
    return True


def _title(client):
    t = client.evaluate(f"({TITLE_JS})()") or ""
    t = re.sub(r"\s*(文摘阅读|下载|导出题录|被引\s*\d+)\s*$", "", t.strip())
    # part 页（分章下载）没有 detailTitle，document.title 只剩站点名
    # → 视为"无标题"，交由 doc_id_from_url 兜底命名
    return "" if t in ("万方数据知识服务平台", "万方数据", "万方") else t


def doc_id_from_url(url):
    """从详情 URL 取文档 ID（thesis/D04070817 → D04070817）。

    part 页（分章下载）取不到标题时用它兜底命名，避免退化成固定的 "paper"。
    """
    m = re.search(r"/(?:periodical|thesis|conference|patent)/([A-Za-z0-9_.-]+)", url or "")
    return m.group(1) if m else "paper"


def _logged_in(client):
    """机构登录检测：顶部栏渲染晚，轮询等待（最多 5s）"""
    for _ in range(10):
        t = client.evaluate(f"({ACCESS_TEXT_JS})()") or ""
        if t and t != "登录机构账号":
            return True
        time.sleep(0.5)
    return False


def _match_tree(tree, idx):
    """在章节树（含 idx 字段）中按层级 idx 找节点"""
    for node in tree:
        if node["idx"] == idx:
            return node
        hit = _match_tree(node.get("kids", []), idx)
        if hit:
            return hit
    return None


def _expand_zip(zip_path, out_dir, tree, ids):
    """解压章节 zip，按 (idx) 标题.pdf 重命名；返回文件数"""
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    os.remove(zip_path)
    files = sorted(f for f in os.listdir(out_dir) if f.lower().endswith(".pdf"))
    n = 0
    for i, idx in enumerate(ids):
        node = _match_tree(tree, idx)
        label = (node or {}).get("label", "")
        _, clean_title, _ = parse_chapter_label(f"{idx} {label}")
        safe = re.sub(r'[\\/:*?"<>|]', "_", clean_title or "") or f"ch_{idx}"
        if i < len(files):
            src = os.path.join(out_dir, files[i])
            dst = os.path.join(out_dir, f"({idx}) {safe}.pdf")
            if src != dst:
                os.rename(src, dst)
            n += 1
    return n


def download_full(client, url, save_dir):
    client.navigate(url)
    if not client.wait_for("!!document.querySelector('.detailIntro, .detailTitleCN, h1')", timeout=get("timeout.detail_load")):
        if client.evaluate("!!document.querySelector('.content-error')"):
            return {"url": url, "error": "Invalid URL — 404 page not found"}
        return {"url": url, "error": "页面加载超时"}
    if not _logged_in(client):
        return {"url": url, "error": "Not logged in"}
    title = _title(client)
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:get("trunc.filename")] or doc_id_from_url(url)
    # 先无副作用检测按钮（不点击）：不存在则报错，不进入 download_with_events
    if not client.evaluate(f"({HAS_FULL_DL_JS})()"):
        return {"url": url, "error": "未找到整篇下载按钮"}
    # 点击必须发生在 setDownloadBehavior 之后：放进 trigger_fn，由 download_with_events 先设后点
    # 真实鼠标点击（mousedown/mouseup）触发按钮的 token 替换逻辑；JS .click() 不生效
    dl = download_with_events(client, lambda: _click_selector(client, "a.download.buttonItem"), save_dir)
    src, name = dl["path"], dl["filename"]
    final = os.path.join(save_dir, f"{safe}.pdf")
    if os.path.exists(final):
        os.remove(final)
    os.replace(src, final)
    return {"url": url, "title": title,
            "download": {"name": os.path.basename(final), "path": final,
                         "size": dl["size"]}}


def download_chapters(client, url, ids, save_dir):
    part_url = url.replace("/thesis/", "/part/thesis/")
    client.navigate(part_url)
    if not client.wait_for("!!document.querySelector('.ivu-tree, .detailTitleCN, h1')", timeout=get("timeout.detail_load")):
        return {"url": url, "error": "页面加载超时"}
    if not _logged_in(client):
        return {"url": url, "error": "Not logged in"}
    # async JS 必须 awaitPromise 取回树结构
    chapter_js = CHAPTER_TREE_JS.replace("{rounds}", str(get("caps.chapter_rounds")))
    tree = client.evaluate(f"({chapter_js})()", await_promise=True) or []
    for idx in ids:
        if not _match_tree(tree, idx):
            return {"url": url, "error": f"章节索引未匹配 — ids={ids} 均不在树中"}
    for idx in ids:
        if not client.evaluate(f"({CHECK_CHAPTER_JS})({json.dumps(idx)})"):
            return {"url": url, "error": f"章节索引未匹配 — ids={ids} 均不在树中"}
    # 先无副作用检测确认按钮：不存在则报错，不进入 download_with_events
    if not client.evaluate(f"({HAS_CONFIRM_JS})()"):
        return {"url": url, "error": "未找到确认下载按钮"}
    # 真实鼠标点击确认下载按钮（:has-text 文本匹配由 _click_selector 桥接）
    dl = download_with_events(client, lambda: _click_selector(client, "button:has-text('确认下载')"), save_dir)
    title = _title(client)
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:get("trunc.filename")] or doc_id_from_url(url)
    zip_path = dl["path"]
    out_dir = os.path.join(save_dir, safe)
    os.makedirs(out_dir, exist_ok=True)
    n = _expand_zip(zip_path, out_dir, tree, ids)
    return {"url": url, "title": title,
            "download": {"name": safe, "path": out_dir, "size": f"{n} files"}}


def main():
    args = parse_args()
    port = ensure_cdp()
    results = []
    for i, entry in enumerate(args.url):
        client = None
        try:
            client = create_page(port)
            if i > 0:
                time.sleep(get("rate.limit")[0])
            url, _, ids_arg = entry.partition("|")
            ids = parse_ids(ids_arg) if ids_arg else []
            if ids:
                r = download_chapters(client, url, ids, args.save_dir)
            else:
                r = download_full(client, url, args.save_dir)
            results.append(r)
        except Exception as e:
            results.append({"url": entry, "error": str(e)[:200]})
        finally:
            close_page(client)
    out = {"count": len(results), "results": results}
    # 先落盘再写 stdout：宿主对 stdout 有大小上限，Agent 拿全文直接读 logPath
    try:
        out["logPath"] = write_log(out, "wf_paper_download")
        sys.stderr.write(f"[wf-paper-download] 完整结果已落盘: {out['logPath']}\n")
    except Exception as e:
        sys.stderr.write(f"[wf-paper-download] 落盘失败: {str(e)[:80]}\n")
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    setup_stdout()
    main()
