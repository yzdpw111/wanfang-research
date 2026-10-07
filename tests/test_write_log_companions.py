# tests/test_write_log_companions.py
"""`write_log` 的 meta 与可读性副本（.jsonl / .tsv）测试。

★ 为什么用**工作区内**临时目录而不是 `tmp_path`：
  DSH 受限沙箱会拒绝写系统 `%TEMP%`（`PermissionError: [WinError 5]`），
  而 pytest 的 `tmp_path` 正落在那里 —— 本 skill 有 10 个用例因此在本机跑不起来。
  这里改用工作区内目录，**在任何环境下都能跑**。

覆盖（每条都要有判别力）：
  · 主 `.json` 里出现 `meta`（skill/script/time/argv）——**且原字段不变**
  · `.jsonl` 首行是 meta、之后**一行一条记录**
  · **截断容忍**：`.jsonl` 被截断后，**前面的行仍可逐行解析**（主 `.json` 不行）
  · 检索结果的 `items` 会被**展平**并带上所属 `keyword`
  · **不再产出 `.tsv`**（设计决定，钉住它防止被顺手加回来）
"""
import json
import shutil
from pathlib import Path

import cdp_base

HERE = Path(__file__).resolve()
TMP_ROOT = HERE.parents[1] / ".tmp-test-writelog"


def _setup(monkeypatch, name):
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    d = TMP_ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cdp_base, "get", lambda key: str(d))
    return d


SEARCH_LIKE = {
    "count": 2,
    "results": [
        {"keyword": "kw1", "totalResults": 10, "items": [
            {"id": "D1", "title": "论文一", "type": "[硕士论文]", "url": "u1", "snippet": "s1"},
            {"id": "D2", "title": "论文二", "type": "[博士论文]", "url": "u2", "snippet": "s2"},
        ]},
    ],
}


def test_meta_is_added_and_original_fields_kept(monkeypatch):
    _setup(monkeypatch, "meta")
    p = cdp_base.write_log(SEARCH_LIKE, "wf_search", params=["--q", "x"])
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    assert data["count"] == 2 and len(data["results"]) == 1, "原有字段不许被改动"
    m = data["meta"]
    assert m["skill"] == "wanfang-research"
    assert m["script"] == "wf_search"
    assert m["argv"] == ["--q", "x"]
    assert "T" in m["time"], m["time"]


def test_jsonl_has_meta_first_then_one_record_per_line(monkeypatch):
    _setup(monkeypatch, "jsonl")
    p = cdp_base.write_log(SEARCH_LIKE, "wf_search")
    lines = Path(p[:-5] + ".jsonl").read_text(encoding="utf-8").strip().splitlines()
    head = json.loads(lines[0])
    assert "__meta__" in head and head["__count__"] == 2
    recs = [json.loads(x) for x in lines[1:]]
    assert len(recs) == 2, "一行一条记录"
    # items 被展平，且带上所属 keyword
    assert recs[0]["keyword"] == "kw1" and recs[0]["id"] == "D1"


def test_no_tsv_companion_is_written(monkeypatch):
    """★ 设计决定：**不再写 `.tsv`**。

    它是纯派生视图（列集合随字段漂移），应由下游分析层生成，不属于日志本身。
    本用例钉住这个决定，防止"顺手又加回来"。
    """
    _setup(monkeypatch, "notsv")
    p = cdp_base.write_log(SEARCH_LIKE, "wf_search")
    assert not Path(p[:-5] + ".tsv").exists(), "不应再产出 .tsv"
    assert Path(p[:-5] + ".jsonl").exists()


def test_jsonl_survives_truncation_while_json_does_not(monkeypatch):
    """★ 这是加 `.jsonl` 的**理由本身**：宿主截断时 JSON 全废，JSONL 还能救。

    ★ 夹具必须**足够多条**（这里 12 条）：真实场景是"大日志被截断"。
      初版只用 2 条，切 60% 时连第一条记录行都不完整 → 救不出任何完整行，
      于是用例失败。**那是用例设计问题，不是实现问题**（改夹具后即通过）。
    """
    _setup(monkeypatch, "trunc")
    many = {
        "count": 12,
        "results": [{"keyword": "kw", "items": [
            {"id": f"D{i}", "title": f"论文{i}" * 6, "type": "[硕士论文]", "url": f"u{i}"}
            for i in range(12)
        ]}],
    }
    p = cdp_base.write_log(many, "wf_search")
    jt = Path(p).read_text(encoding="utf-8")
    lt = Path(p[:-5] + ".jsonl").read_text(encoding="utf-8")

    # 主 JSON 截断后无法整体解析
    try:
        json.loads(jt[: int(len(jt) * 0.6)])
        raise AssertionError("主 JSON 被截断后不应仍可解析（用例前提不成立）")
    except json.JSONDecodeError:
        pass

    # JSONL 截断后：前面的**完整行**仍可逐行解析
    cut = lt[: int(len(lt) * 0.6)]
    ok = 0
    for line in cut.splitlines()[1:]:
        try:
            json.loads(line)
            ok += 1
        except json.JSONDecodeError:
            break
    assert ok >= 3, f"12 条记录切 60% 后应能抢救出多条，实际 {ok}"


def test_jsonl_records_keep_multiline_text_intact(monkeypatch):
    """记录里的换行由 JSON 转义为 `\\n`，**不会**把一条记录拆成多行。"""
    _setup(monkeypatch, "cells")
    data = {"count": 1, "results": [{"id": "X", "title": "第一行\n第二行"}]}
    p = cdp_base.write_log(data, "wf_detail")
    lines = Path(p[:-5] + ".jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "1 行 meta + 1 条记录"
    rec = json.loads(lines[1])
    assert rec["title"] == "第一行\n第二行", rec
