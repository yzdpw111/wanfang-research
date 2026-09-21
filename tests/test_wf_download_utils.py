# tests/test_wf_download_utils.py
from wf_paper_download import _title, doc_id_from_url


class _FakeClient:
    def __init__(self, value):
        self._value = value

    def evaluate(self, expr):
        return self._value


class TestTitleFiltering:
    """part 页（分章下载）取到的标题只剩站点名，必须当"无标题"处理，
    否则目录名会变成"万方数据知识服务平台"。"""

    def test_site_name_treated_as_empty(self):
        assert _title(_FakeClient("万方数据知识服务平台")) == ""
        assert _title(_FakeClient("万方数据")) == ""

    def test_real_title_kept_and_suffix_stripped(self):
        assert _title(_FakeClient("基于机器学习的加载絮凝效能优化与机理解析")) == \
            "基于机器学习的加载絮凝效能优化与机理解析"
        assert _title(_FakeClient("某论文标题 下载")) == "某论文标题"

    def test_empty(self):
        assert _title(_FakeClient("")) == ""
        assert _title(_FakeClient(None)) == ""


class TestDocIdFromUrl:
    """回归：分章下载时 part 页取不到标题，兜底名曾是固定 'paper'
    （实测目录名退化成 paper）。改为用文档 ID 兜底。"""

    def test_thesis(self):
        assert doc_id_from_url("https://d.wanfangdata.com.cn/thesis/D04070817") == "D04070817"

    def test_periodical(self):
        assert doc_id_from_url("https://d.wanfangdata.com.cn/periodical/hebgydxxb202606001") == "hebgydxxb202606001"

    def test_ignores_ids_suffix(self):
        assert doc_id_from_url("https://d.wanfangdata.com.cn/thesis/D001|1-2") == "D001"

    def test_unknown_host_falls_back(self):
        assert doc_id_from_url("https://example.com/x") == "paper"

    def test_empty(self):
        assert doc_id_from_url("") == "paper"
        assert doc_id_from_url(None) == "paper"
