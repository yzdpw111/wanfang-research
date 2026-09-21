#!/usr/bin/env python3
"""config.py — 集中配置（对标 Node config.js）

两层: DEFAULTS → {PREFIX}_* 环境变量覆盖
API: get(key) / set(key) / resolve_port()
"""
import os

_ENV_PREFIX = "WF_"   # wf 版改为 "WF_"

DEFAULTS = {
    "state.dir": os.path.expandvars(r"%USERPROFILE%\.yzdpw_state"),
    "timeout.cdp": 30,                # CDP WebSocket 超时(s)
    "timeout.chrome_start": 30,       # Chrome 启动等待(s)
    "timeout.download": 60,           # 单次 PDF/图片二进制请求超时(s)
    "timeout.download_event": 60,     # 下载事件等待超时(s)
    "timeout.page_load": 20,          # 搜索页加载判定总时长(s)（万方渲染 8-16s，比 ieee 的 8 大）
    "timeout.detail_load": 30,        # 详情页/章节页元素出现等待(s)
    "timeout.cite_modal": 5,          # 引用弹窗内容就绪等待(s)
    "timeout.render_wait": 2,         # 渲染等待默认(s)（翻页、摘要展开、part 页等）
    "caps.ref_pages": 50,             # 参考文献翻页上限
    "caps.chapter_rounds": 10,        # 章节树展开轮数上限
    "trunc.abstract": 2000,           # 摘要/主权项截断
    "trunc.snippet": 250,             # 搜索摘要截断
    "trunc.filename": 120,            # 下载文件名截断
    "rate.limit": (8.0, 18.0),        # 请求限速范围(s)
    "human.scroll_rounds": (3, 6),
    "human.scroll_delta": (150, 500),
    "human.pause": (0.5, 4.0),
    "human.mouse_prob": 0.4,
    "parallel.limit": 2,
    "tabs.max": 30,                   # tab 数上限，达到即只告警（不自动关 tab；0=不启用）
    "logs.dir": os.path.join(os.getcwd(), "logs"),  # 完整结果落盘目录（默认运行目录）
}

_OVERRIDES = {}


def _coerce(raw):
    s = raw.strip()
    if "," in s:
        return tuple(_coerce(x) for x in s.split(","))
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return s


def _env_key(key):
    return _ENV_PREFIX + key.upper().replace(".", "_")


def get(key):
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    env_key = _env_key(key)
    if env_key in os.environ:
        return _coerce(os.environ[env_key])
    return DEFAULTS.get(key)


def set(key, value):
    _OVERRIDES[key] = value


def resolve_port():
    try:
        pf = os.path.join(get("state.dir"), ".cdp_port")
        with open(pf) as f:
            return int(f.read().strip())
    except Exception:
        return 9222
