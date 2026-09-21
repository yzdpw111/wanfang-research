import os
import sys

import pytest


def test_prefix_env_override(monkeypatch):
    monkeypatch.setenv("WF_STATE_DIR", r"C:\tmp\state")
    monkeypatch.setenv("WF_TIMEOUT_DOWNLOAD", "90")
    import config
    assert config.get("state.dir") == r"C:\tmp\state"
    assert config.get("timeout.download") == 90


def test_tuple_coerce(monkeypatch):
    monkeypatch.setenv("WF_RATE_LIMIT", "5,10")
    import config
    assert config.get("rate.limit") == (5, 10)


def test_defaults_present():
    import config
    assert config.get("timeout.download") == 60
    assert config.get("timeout.download_event") == 60
    assert config.get("parallel.limit") == 2
