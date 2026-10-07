import json
import urllib.parse

import pytest

from wf_parser import (TYPE_MAP, build_search_url, detect_type, doi_before_references,
                       extract_item_id, extract_refs, extract_total, gen_chapter_label,
                       page_info, parse_chapter_label, parse_citations, parse_ids,
                       parse_result_item, parse_year_range, split_claims, split_title)


class TestDoiBeforeReferences:
    """★ 修复实测 bug（2026-10）。

    万方「参考文献」列表里**每一条都带 `DOI:10.x`**，而旧实现从**整页**正则抓第一个
    → **必然抓成参考文献里某篇的 DOI**。实测：中国学位论文 `thesis/D03561632`
    被挂上 `10.1016/j.measurement.2021.109273`（Elsevier），而该页题录区没有 DOI。
    """

    def test_doi_after_references_heading_is_ignored(self):
        """核心用例：题录区无 DOI、参考文献里有 → **必须返回空**。"""
        body = (
            "基于深度迁移学习的剩余使用寿命预测和健康状态估计\n"
            "作者：张三\n摘要：本文研究……\n"
            "关键词：锂电池\n"
            "参考文献\n"
            "[1] Qing Xu,Min Wu. A Hybrid Ensemble Approach[J]. Journal. 2021. "
            "DOI:10.1016/j.measurement.2021.109273 .\n"
            "[2] Ding, Ning. A novel method[J]. 2019. DOI:10.1016/j.isatra.2019.08.012 .\n"
        )
        assert doi_before_references(body) == ""

    def test_doi_before_references_heading_is_returned(self):
        """题录区有 DOI → 正常返回（期刊论文常见）。"""
        body = (
            "某期刊论文\nDOI：10.11918/202509041\n摘要：……\n"
            "参考文献\n[1] x. DOI:10.1016/j.other.1 .\n"
        )
        assert doi_before_references(body) == "10.11918/202509041"

    def test_no_references_heading_returns_empty(self):
        """**找不到「参考文献」标题就返回空** —— 宁可缺，不许错。

        若这里退回"整页抓第一个"，就会重现那个把参考文献 DOI 当本文 DOI 的 bug。
        """
        body = "某论文\n作者：张三\n[1] Some ref. DOI:10.1016/j.x.1 .\n"
        assert doi_before_references(body) == ""

    def test_empty_body(self):
        assert doi_before_references("") == ""
        assert doi_before_references(None) == ""

    def test_more_than_one_doi_in_metadata_takes_first(self):
        body = "标题\nDOI：10.1000/first\nDOI：10.1000/second\n参考文献\n[1] a DOI:10.1/x .\n"
        assert doi_before_references(body) == "10.1000/first"


class TestParseYearRange:
    def test_range(self):
        assert parse_year_range("2020-2025") == (2020, 2025)

    def test_below_min(self):
        with pytest.raises(ValueError):
            parse_year_range("1700")


class TestBuildSearchUrl:
    def test_facet_type_only(self):
        u = build_search_url("人工智能", ["期刊论文"], None, "1")
        assert u.startswith("https://s.wanfangdata.com.cn/paper?")
        assert "q=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD" in u
        assert "facet=" in u
        facet = json.loads(urllib.parse.unquote_plus(u.split("facet=", 1)[1]))
        assert facet[0]["Type"]["value"] == ["Periodical"]

    def test_facet_year(self):
        u = build_search_url("ai", ["期刊论文"], "2020-2022", "2")
        facet = json.loads(urllib.parse.unquote_plus(u.split("facet=", 1)[1]))
        assert facet[1]["PublishYear"]["value"] == ["2020", "2021", "2022"]


class TestResultExtract:
    def test_item_id(self):
        assert extract_item_id("期刊论文_12345") == ("期刊论文", "12345")

    def test_split_title(self):
        assert split_title("1. 深度学习研究 文摘阅读 在线阅读") == "深度学习研究"

    def test_parse_result_item(self):
        item = {"id_raw": "学位论文_999", "title_raw": "3. 某硕士论文 文摘阅读",
                "type_raw": "[硕士论文]", "snippet_raw": "摘要：这是摘要 在线阅读"}
        out = parse_result_item(item)
        assert out["url"] == "https://d.wanfangdata.com.cn/thesis/999"
        assert out["title"] == "某硕士论文"
        assert out["type"] == "[硕士论文]"
        assert out["snippet"] == "这是摘要"

    def test_extract_item_id_patent_multi_segment(self):
        assert extract_item_id("patent_ZL_CN202480071965.1_CN122180973A_20260609") == \
            ("patent", "ZL_CN202480071965.1_CN122180973A_20260609")

    def test_parse_result_item_patent(self):
        item = {"id_raw": "patent_ZL_CN123.1_CN456A_20260101", "title_raw": "5. 一种方法 文摘阅读",
                "type_raw": "[专利]", "snippet_raw": "摘要：这是专利摘要 在线阅读"}
        out = parse_result_item(item)
        assert out is not None
        assert out["url"] == "https://d.wanfangdata.com.cn/patent/ZL_CN123.1_CN456A_20260101"
        assert out["type"] == "[专利]"


DETAIL_BODY = """摘要：这是一个摘要文本
关键词：机器学习; 深度学习
文献发表日期: 2023-05-01
学科专业: 计算机科学与技术
导师姓名: 张三
学位年度: 2023
授予学位: 硕士
会议时间: 2023-01-01
专利类型: 发明
申请/专利号: CN123456
公开/公告日: 2023-06-01
主权项: 1. 一种方法……(多行)
参考文献
[1] 张三. 某文献. 期刊, 2023.
[2] 李四. 另一文献. 期刊, 2022.
"""
REF_NOISE = ["仅看全文", "排序", "发表时间", "被引频次", "查看引文网络",
             "ISSN", "年,卷", "所属栏目", "CSTPCD", "北大核心", "CSSCI", "AMI"]


class TestDetailExtract:
    def test_detect_type(self):
        assert detect_type("https://d.wanfangdata.com.cn/thesis/D001") == "thesis"
        assert detect_type("https://d.wanfangdata.com.cn/periodical/xyz") == "periodical"
        assert detect_type("https://d.wanfangdata.com.cn/conference/c1") == "conference"
        assert detect_type("https://d.wanfangdata.com.cn/patent/p1") == "patent"
        assert detect_type("https://x.com/other") is None

    def test_refs(self):
        refs = extract_refs(DETAIL_BODY)
        assert refs and refs[0].startswith("[1]")
        assert len(refs) == 2

    def test_claims(self):
        c = split_claims(DETAIL_BODY)
        assert c.startswith("1. 一种方法")

    def test_claims_stops_at_patent_sections(self):
        body = DETAIL_BODY + "\n\n专利图片\n\n法律状态\n详情\n\n相关文献\n换一换\n"
        c = split_claims(body)
        assert "专利图片" not in c
        assert "法律状态" not in c
        assert "相关文献" not in c

    def test_citations_split(self):
        modal = "GB/T 7714 引用格式A 复制 MLA格式 引用格式B 复制 APA格式 引用格式C 复制"
        out = parse_citations(modal)
        assert "GB/T 7714" in out[0] and "MLA" not in out[0]
        assert "MLA" in out[1] and "APA" not in out[1]
        assert "APA" in out[2]


class TestChapterLabel:
    def test_gen(self):
        assert gen_chapter_label([1, 0, 0]) == "1"
        assert gen_chapter_label([1, 2, 0]) == "1.2"
        assert gen_chapter_label([2, 1, 3]) == "2.1.3"

    def test_parse(self):
        idx, title, pages = parse_chapter_label("1.2 绪论 5-9 页")
        assert (idx, title, pages) == ("1.2", "绪论", "5-9")


class TestParseIds:
    def test_simple(self):
        assert parse_ids("1,3") == ["1", "3"]

    def test_range_expand(self):
        assert parse_ids("5-7") == ["5", "6", "7"]

    def test_mixed(self):
        assert parse_ids("1,5-6,2.0") == ["1", "5", "6", "2.0"]

    def test_nested_kept(self):
        assert parse_ids("2.0-2.3") == ["2.0", "2.3"]
