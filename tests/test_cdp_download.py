import json

import pytest

from cdp_base import (CdpError, consume_download_events, fetch_binary,
                      parse_download_begin, parse_download_progress)


class TestParseDownloadMessages:
    def test_begin(self):
        p = parse_download_begin({"method": "Browser.downloadWillBegin",
                                  "params": {"suggestedFilename": "a.pdf", "guid": "g1"}})
        assert p == {"filename": "a.pdf", "guid": "g1"}

    def test_progress_completed(self):
        p = parse_download_progress({"method": "Browser.downloadProgress",
                                     "params": {"state": "completed",
                                                "filePath": r"C:\dl\a.pdf", "totalBytes": 123}})
        assert p == {"state": "completed", "path": r"C:\dl\a.pdf", "size": 123}

    def test_progress_canceled(self):
        p = parse_download_progress({"method": "Browser.downloadProgress",
                                     "params": {"state": "canceled"}})
        assert p["state"] == "canceled"


class TestConsumeDownloadEvents:
    def test_happy_path(self):
        events = [
            {"method": "Browser.downloadWillBegin",
             "params": {"suggestedFilename": "a.pdf", "guid": "g1"}},
            {"method": "Browser.downloadProgress",
             "params": {"state": "completed", "filePath": r"C:\dl\a.pdf", "totalBytes": 99}},
        ]
        it = iter(events)
        out = consume_download_events(lambda t: next(it), timeout=5)
        assert out == {"filename": "a.pdf", "path": r"C:\dl\a.pdf", "size": 99}

    def test_canceled_raises(self):
        events = [
            {"method": "Browser.downloadWillBegin", "params": {"suggestedFilename": "a.pdf", "guid": "g1"}},
            {"method": "Browser.downloadProgress", "params": {"state": "canceled"}},
        ]
        it = iter(events)
        with pytest.raises(CdpError, match="canceled"):
            consume_download_events(lambda t: next(it), timeout=5)

    def test_timeout_raises(self):
        def never(t):
            import time
            time.sleep(0.05)
            raise CdpError("recv 超时")  # 模拟 ws 超时
        with pytest.raises(CdpError, match="timeout"):
            consume_download_events(never, timeout=0.2)


class TestFetchBinaryB64:
    def test_roundtrip(self):
        import base64
        payload = b"PDF-BYTES-\x00\xff"
        b64 = base64.b64encode(payload).decode()
        captured = {}

        class FakeClient:
            def evaluate(self, expr, **kw):
                captured["expr"] = expr
                return b64

        out = fetch_binary(FakeClient(), "https://x/pdf", timeout=5)
        assert out == payload
        assert '"https://x/pdf"' in captured["expr"]
        assert captured.get("expr", "").count("btoa") == 1


class TestWriteLog:
    def test_writes_json_to_configured_dir(self, monkeypatch, tmp_path):
        """落盘目录由 config 的 logs.dir 决定（默认=运行目录），不再依赖运行时 chdir。"""
        import cdp_base
        from cdp_base import write_log
        real = cdp_base.get
        monkeypatch.setattr(cdp_base, "get",
                            lambda k: str(tmp_path) if k == "logs.dir" else real(k))

        path = write_log({"a": 1, "k": "中"}, "test_skill")

        assert path.startswith(str(tmp_path))
        with open(path, encoding="utf-8") as f:
            assert json.load(f) == {"a": 1, "k": "中"}
