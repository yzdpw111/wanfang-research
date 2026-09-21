import pytest

from wf_parser import TYPE_MAP
from wf_search import parse_args


def test_defaults():
    a = parse_args(["--q", "ai"])
    assert a.rows == 20 and a.page == "1" and a.type == list(TYPE_MAP)
    assert a.parallel == 2


def test_bad_type_exits():
    with pytest.raises(SystemExit):
        parse_args(["--q", "ai", "--type", "报纸"])


def test_rows_capped():
    a = parse_args(["--q", "ai", "--rows", "999"])
    assert a.rows == 20


def test_detail_url_required():
    with pytest.raises(SystemExit):
        from wf_detail import parse_args
        parse_args([])


def test_detail_url_multi():
    from wf_detail import parse_args
    a = parse_args(["--url", "https://x/1", "--url", "https://x/2"])
    assert len(a.url) == 2


def test_download_requires_save_dir():
    with pytest.raises(SystemExit):
        from wf_paper_download import parse_args
        parse_args(["--url", "https://d.wanfangdata.com.cn/thesis/D001"])


def test_download_url_with_ids():
    from wf_paper_download import parse_args
    a = parse_args(["--url", "https://d.wanfangdata.com.cn/thesis/D001|1,5-6", "--save-dir", "tmp"])
    assert a.url[0].endswith("|1,5-6")
