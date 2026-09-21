import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: 实机 smoke test，需 CDP 在线 + 登录")
