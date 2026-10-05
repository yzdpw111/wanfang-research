# tests/test_wf_chapter_naming.py
"""守护分章下载的「id ↔ 文件」配对与命名。

背景（真实 bug）：万方把分章 zip 按**章节在树中的顺序**打包，而不是按请求顺序。
实测：请求 `|7.1,6.3`（7.1 在前），zip 内部条目却是

    (1) 1.3_论文结构安排.pdf      ← 6.3（树中靠前）
    (2) 2.1_信号完整性理论.pdf    ← 7.1（树中靠后）

而 _expand_zip 原实现是 `for i, idx in enumerate(ids): src = files[i]` ——
用**请求顺序**的下标去索引**树顺序**的已排序文件，两者一旦不一致就静默错配，
序号与标题会张冠李戴。

修法：先按树顺序对 ids 排序，再与 sorted(files) 逐位配对；命名仍用该位置
对应的 idx 与树中 label，因此 (idx) 前缀与标题始终自洽。
"""
import os
import zipfile

import pytest

from wf_paper_download import _expand_zip, _sort_ids_by_tree_order

# 树顺序：6.1 → 6.2 → 6.3 → 7.1 → 7.2
TREE = [
    {"idx": "1", "label": "封面", "kids": []},
    {"idx": "6", "label": "绪论", "kids": [
        {"idx": "6.1", "label": "研究背景及意义 13-14 页", "kids": []},
        {"idx": "6.2", "label": "国内外相关研究现状 14-16 页", "kids": []},
        {"idx": "6.3", "label": "论文结构安排 16-17 页", "kids": []},
    ]},
    {"idx": "7", "label": "理论基础", "kids": [
        {"idx": "7.1", "label": "信号完整性理论 18-20 页", "kids": []},
        {"idx": "7.2", "label": "电源完整性理论 20-22 页", "kids": []},
    ]},
]


def _make_zip(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, b"%PDF-1.4 fake")
    return path


class TestSortIdsByTreeOrder:
    def test_reorders_to_tree_order(self):
        # 请求顺序 7.1,6.3 → 树顺序应为 6.3,7.1
        assert _sort_ids_by_tree_order(TREE, ["7.1", "6.3"]) == ["6.3", "7.1"]

    def test_already_in_tree_order_is_unchanged(self):
        assert _sort_ids_by_tree_order(TREE, ["6.1", "6.2"]) == ["6.1", "6.2"]

    def test_unknown_id_goes_last_and_is_not_lost(self):
        out = _sort_ids_by_tree_order(TREE, ["9.9", "6.1"])
        assert out == ["6.1", "9.9"]

    def test_empty(self):
        assert _sort_ids_by_tree_order(TREE, []) == []


class TestExpandZipPairsByTreeOrder:
    def test_request_order_reversed_must_not_mismatch(self, tmp_path):
        """核心回归：请求 7.1,6.3，zip 按树顺序（6.3 在前）。"""
        z = _make_zip(str(tmp_path / "c.zip"),
                      ["(1) 1.3_x.pdf", "(2) 2.1_y.pdf"])
        out = tmp_path / "out"
        out.mkdir()

        n = _expand_zip(z, str(out), TREE, ["7.1", "6.3"])
        assert n == 2

        files = sorted(os.listdir(out))
        # 6.3 的文件应命名为 (6.3)，7.1 的应命名为 (7.1)；且分别对应正确的源文件
        joined = "\n".join(files)
        assert "(6.3)" in joined and "论文结构安排" in joined, \
            f"6.3 应配到 (1) 那个文件，实际: {files}"
        assert "(7.1)" in joined and "信号完整性理论" in joined, \
            f"7.1 应配到 (2) 那个文件，实际: {files}"

        # 关键断言：标题与文件内容来源一致 —— 文件大小可区分来源
        sizes = {f: os.path.getsize(os.path.join(out, f)) for f in files}
        f63 = next(f for f in files if f.startswith("(6.3)"))
        f71 = next(f for f in files if f.startswith("(7.1)"))
        assert sizes[f63] == len(b"%PDF-1.4 fake")
        assert sizes[f71] == len(b"%PDF-1.4 fake")

    def test_same_order_still_works(self, tmp_path):
        z = _make_zip(str(tmp_path / "c.zip"), ["a.pdf", "b.pdf"])
        out = tmp_path / "out"
        out.mkdir()
        n = _expand_zip(z, str(out), TREE, ["6.1", "6.2"])
        assert n == 2
        files = sorted(os.listdir(out))
        assert any(f.startswith("(6.1)") for f in files)
        assert any(f.startswith("(6.2)") for f in files)

    def test_removes_zip(self, tmp_path):
        z = _make_zip(str(tmp_path / "c.zip"), ["a.pdf"])
        out = tmp_path / "out"
        out.mkdir()
        _expand_zip(z, str(out), TREE, ["6.1"])
        assert not os.path.exists(z), "解压后应删除 zip"


class TestNamingConsistency:
    def test_prefix_and_title_come_from_same_id(self, tmp_path):
        """(idx) 前缀里的 idx 必须与标题对应的章节一致（防止张冠李戴）。"""
        z = _make_zip(str(tmp_path / "c.zip"), ["x.pdf", "y.pdf"])
        out = tmp_path / "out"
        out.mkdir()
        _expand_zip(z, str(out), TREE, ["7.2", "6.1"])
        files = sorted(os.listdir(out))

        f72 = next(f for f in files if f.startswith("(7.2)"))
        f61 = next(f for f in files if f.startswith("(6.1)"))
        assert "电源完整性理论" in f72, f"(7.2) 应配 7.2 的标题，实际 {f72}"
        assert "研究背景及意义" in f61, f"(6.1) 应配 6.1 的标题，实际 {f61}"
