#!/usr/bin/env python3
"""
cdp_base.py — CDP 客户端 + Chrome 裸启动 + 人类行为模拟

职责:
  CdpClient      CDP WebSocket 消息循环（send/evaluate/navigate/wait_for）
  human_scroll   类人滚动（3-6 轮随机、15% 回滚、停顿 0.5-4s）
  human_mouse    40% 概率随机鼠标移动
  random_delay   随机等待
  RateLimiter    相邻请求最小间隔
  ensure_cdp     优先连接现有 CDP，否则 schtasks 裸启动 Chrome（持久 profile）
  pick_port      在 9222-9299 中挑空闲端口
"""
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request

import websocket

from config import get


def setup_stdout():
    """stdout/stderr 强制 UTF-8（CLI 入口必须调用一次）。

    控制台默认 GBK 时，结果里的 emoji 变体符（\\ufe0f、✅ 等）会让
    json.dumps 的输出抛 UnicodeEncodeError —— 抓取全部成功，用户却
    一个字都拿不到。errors="replace" 只作极端兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ── 常量（从 config 读，沿用 xhs skill 的 profile 约定）──────────
BASE = get("state.dir")
PROFILE = os.path.join(BASE, "chrome-cdp")
PORT_FILE = os.path.join(BASE, ".cdp_port")
TASK_NAME = "ChromeCDP-Shared"

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


class CdpError(Exception):
    pass


def port_busy(port):
    """探测端口是否已有 CDP 服务。

    先用 socket 做 TCP 预探测：urlopen 连未监听端口会等满 timeout，
    扫描 9222-9299 因此累计 ~200s（Chrome 离线时 ensure_cdp 卡几分钟）；
    TCP connect 对未监听端口是立刻返回的。
    """
    import socket
    s = socket.socket()
    try:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return False
    finally:
        s.close()
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return r.status == 200
    except Exception:
        return False


def port_bindable(port):
    """端口是否可被绑定（被非 CDP 进程占用时返回 False）"""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port():
    """在 9222-9299 中挑选第一个空闲且可绑定的端口"""
    for port in range(9222, 9300):
        if not port_busy(port) and port_bindable(port):
            return port
    raise CdpError("9222-9299 端口全部被占")


def find_chrome():
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    raise CdpError("Chrome 未找到")


# ── 启动诊断：Chrome 的 stderr 必须落盘，否则"启动失败"会伪装成"启动超时" ──

# Chrome 在受限沙箱里被杀掉时，stderr 里会出现这些特征词
_SANDBOX_MARKERS = ("crashpad", "OpenProcess", "platform_channel", "self-terminating", "拒绝访问")


def chrome_log_path():
    """Chrome 自身 stdout/stderr 的落盘路径。

    DSH 沙箱只允许写工作区：state.dir（profile 所在处）常被拒，所以先试
    logs.dir（默认 <运行目录>/logs，沙箱内可写），再退到系统临时目录，
    最后退回脚本目录；全失败返回 None（那时放弃落盘，但不能因此中断启动）。
    """
    import tempfile
    candidates = []
    for key in ("logs.dir", "state.dir"):
        try:
            d = get(key)
        except Exception:
            d = None
        if isinstance(d, str) and d:
            candidates.append(os.path.join(d, "logs") if key == "state.dir" else d)
    candidates.append(tempfile.gettempdir())
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "chrome-launch.log")
            with open(path, "ab"):
                pass          # 探写一次：没权限时立刻退回下一个候选目录
            return path
        except Exception:
            continue
    return None


def read_log_tail(path, limit=1500):
    """读启动日志末尾；任何失败都返回空串 —— 日志只用于诊断，不能盖住真实错误"""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            return f.read()[-limit:].decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def launch_failed(port, log_path, code):
    """构造 Chrome 启动失败的错误：附 Chrome 自己的 stderr + 可执行的处置建议"""
    tail = read_log_tail(log_path)
    when = "启动即退出" if code is not None else f"{get('timeout.chrome_start')} 秒内未就绪"
    msg = [f"Chrome {when}（port {port}）", f"Chrome 日志: {log_path or '(日志目录不可写，未能落盘)'}"]
    if tail:
        msg += ["--- Chrome stderr 末尾 ---", tail, "-------------------------"]
    if any(m in tail for m in _SANDBOX_MARKERS):
        msg += [
            "诊断: crashpad 的进程句柄与 mojo 的命名管道被当前受限沙箱拒绝，Chrome 启动即自杀，"
            "调试端口永不监听。与 skill 代码/依赖/启动参数均无关：--no-sandbox、"
            "--disable-crash-reporter、--headless=new 都试过，全部无效。",
            "处置: Chrome 必须在放行沙箱(danger-full-access)或 DSH 之外启动一次，之后本脚本在"
            "沙箱内直接复用该实例（ensure_cdp 会自动发现 9222-9299 上的在线 CDP）：\n"
            "  1) 沙箱外: python scripts/chrome_session.py --start   （并在窗口里登录）\n"
            "  2) 沙箱内: 正常跑抓取脚本，无需再启动 Chrome",
        ]
    elif tail:
        msg += ["诊断: Chrome 未监听调试端口就退出了，原因见上面的 stderr；"
                "若显示 profile 被占用，先关掉使用同一 profile 的 Chrome 再重试。"]
    else:
        msg += ["诊断: 拿不到 Chrome 的 stderr（日志目录都不可写）。常见原因仍是受限沙箱拒绝 "
                "crashpad/mojo；处置同上：在放行沙箱或 DSH 之外启动一次 Chrome。"]
    return CdpError("\n".join(msg))


def ensure_cdp():
    """返回可用端口。优先复用在线 CDP，否则直接 detached 启动 Chrome。"""
    # 1) 端口文件里记录的端口若在线则复用
    try:
        with open(PORT_FILE) as f:
            saved = int(f.read().strip())
        if port_busy(saved):
            return saved
    except Exception:
        pass
    # 2) 9222-9299 里已有的 CDP 直接复用（用户手动开的、在沙箱外起的都算）
    for port in range(9222, 9300):
        if port_busy(port):
            return port
    # 3) 启动新 Chrome。必须脱离调用方进程组，否则脚本退出时 Chrome 会被一起收走。
    #    原先用 schtasks 计划任务达到同样目的，但实测本机不再触发
    #    （任务 Last Run Time 不更新、端口不监听），改为直接 Popen(DETACHED_PROCESS)。
    #    Chrome 的 stdout/stderr 落盘而不是丢 DEVNULL —— 否则"启动失败"会伪装成"启动超时"。
    port = pick_port()
    exe = find_chrome()
    os.makedirs(PROFILE, exist_ok=True)
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    log_path = chrome_log_path()
    log_handle = None
    if log_path:
        try:
            log_handle = open(log_path, "ab")
            log_handle.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch port={port} ===\n".encode("utf-8"))
            log_handle.flush()
        except Exception:
            log_handle = None
    sink = log_handle if log_handle is not None else subprocess.DEVNULL
    proc = subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE}",
         "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check"],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
        close_fds=True)
    if log_handle is not None:
        try:
            log_handle.close()      # 子进程持有自己的句柄副本，父进程这份可以关
        except Exception:
            pass
    deadline = time.time() + get("timeout.chrome_start")
    while time.time() < deadline:
        if port_busy(port):
            try:
                with open(PORT_FILE, "w") as f:
                    f.write(str(port))
            except Exception:
                pass
            return port
        code = proc.poll()
        if code is not None:
            # Chrome 已经退出，不必再等满超时：直接把它的 stderr 抛出来
            raise launch_failed(port, log_path, code)
        time.sleep(1)
    raise launch_failed(port, log_path, proc.poll())


class CdpClient:
    """CDP WebSocket 客户端：同步消息循环（id 匹配 + 超时）"""

    def __init__(self, port, timeout=None, ws_url=None):
        self.port = port
        timeout = timeout if timeout is not None else get("timeout.cdp")
        if ws_url:
            target_ws_url = ws_url
        else:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5)
            tabs = json.loads(r.read())
            target = next((t for t in tabs if t.get("type") == "page"), None)
            if not target:
                raise CdpError("没有可用的 page target")
            target_ws_url = target["webSocketDebuggerUrl"]
        # 显式声明 Origin，配合 --remote-allow-origins=http://localhost：
        # Chrome 放行本客户端，但拒绝任意页面 JS 发起的跨源连接
        try:
            self.ws = websocket.create_connection(
                target_ws_url, timeout=timeout, origin="http://localhost")
        except (websocket.WebSocketException, OSError) as e:
            raise CdpError(f"WebSocket 连接失败: {e}") from e
        self.target_id = None          # create_page 填充，close_page 据此关闭 tab
        self._id = 0
        self._closed = False

    def send(self, method, params=None, timeout=None):
        timeout = timeout if timeout is not None else get("timeout.cdp")
        if self._closed:
            raise CdpError("WebSocket 已关闭")
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.close()
                raise CdpError(f"{method}: 等待响应超时（{timeout}s）")
            try:
                self.ws.settimeout(remaining)
                msg = json.loads(self.ws.recv())
            except (websocket.WebSocketTimeoutException, TimeoutError):
                self.close()
                raise CdpError(f"{method}: 等待响应超时（{timeout}s）") from None
            except (websocket.WebSocketException, OSError) as e:
                raise CdpError(f"WebSocket 通信失败: {e}") from e
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def evaluate(self, expr, await_promise=False, return_by_value=True, timeout=None):
        r = self.send("Runtime.evaluate", {
            "expression": expr, "awaitPromise": await_promise, "returnByValue": return_by_value},
            timeout=timeout)
        if "exceptionDetails" in r:
            raise CdpError(f"eval 异常: {expr[:80]}")
        return r.get("result", {}).get("value")

    def navigate(self, url):
        self.send("Page.navigate", {"url": url})

    def get_body_text(self):
        return self.evaluate("document.body ? document.body.textContent : ''") or ""

    def wait_for(self, expr, timeout=15, interval=0.5):
        """轮询直到表达式为真，超时返回 False"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expr):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False


# ── tab 生命周期（create_page 必须配对 close_page）──────────

_BLANK_URL_PREFIXES = ("about:blank", "chrome://newtab", "chrome://new-tab-page")
_TAB_LOCK = threading.RLock()   # 可重入：create_page 持锁期间仍会调用 _warn_tab_limit
_ACTIVE_TARGETS = set()         # 本进程正在使用的 tab，告警统计时排除


def _register_target(target_id):
    with _TAB_LOCK:
        _ACTIVE_TARGETS.add(target_id)


def _unregister_target(target_id):
    with _TAB_LOCK:
        _ACTIVE_TARGETS.discard(target_id)


def is_blank_target(url):
    """空 tab 判定（about:blank / 新标签页）"""
    u = (url or "").strip().lower()
    return any(u.startswith(p) for p in _BLANK_URL_PREFIXES)


def list_page_targets(port):
    """当前 Chrome 的 page target 列表（含 id/url），查询失败返回 []"""
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read())
        return [t for t in tabs if t.get("type") == "page" and t.get("id")]
    except Exception:
        return []


def close_target(port, target_id):
    """按 targetId 关闭 tab（DevTools HTTP /json/close/<id>）。失败返回 False，不抛。"""
    if not port or not target_id:
        return False
    url = f"http://127.0.0.1:{port}/json/close/{target_id}"
    for method in ("GET", "PUT"):      # 各 Chrome 版本对该端点的方法要求不一致
        try:
            urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5).read()
            return True
        except Exception:
            continue
    return False


def close_page(client):
    """关闭 create_page 开的 tab 并断开 ws —— 与 create_page 配对使用。

    关 tab 失败也必须断开 ws，且任何清理异常都不外抛（不污染抓取结果）。
    若它是最后一个 tab：先建一个 about:blank 占位再关它（直接关会让 Chrome 整体
    退出；不关又会在浏览器里留下"上次的搜索结果/详情页"）；占位建不出来时则保留它。
    """
    if client is None:
        return False
    _unregister_target(getattr(client, "target_id", None))
    port = getattr(client, "port", None)
    target_id = getattr(client, "target_id", None)
    ok = False
    if target_id:
        with _TAB_LOCK:      # 检查与关闭同锁：否则两个线程会双双放行，把 tab 关空
            tabs = list_page_targets(port)
            if len(tabs) > 1:
                ok = close_target(port, target_id)
            elif tabs and tabs[0]["id"] == target_id and _create_blank_page(port):
                ok = close_target(port, target_id)
    try:
        client.close()
    except Exception:
        pass
    return ok


_MULTI_SUFFIX = ("com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
                 "co.jp", "co.kr", "com.hk")


def _create_blank_page(port):
    """新建一个 about:blank tab（占位用）。成功 True，失败 False，不抛。"""
    try:
        bws = _browser_ws(port)
    except Exception:
        return False
    try:
        bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                             "params": {"url": "about:blank"}}))
        deadline = time.time() + 5
        while time.time() < deadline:
            bws.settimeout(max(0.1, deadline - time.time()))
            msg = json.loads(bws.recv())
            if msg.get("id") == 1:
                return "error" not in msg
    except Exception:
        return False
    finally:
        try:
            bws.close()
        except Exception:
            pass
    return False


def _registrable(host):
    """粗略取注册域：d.wanfangdata.com.cn → wanfangdata.com.cn

    注意 .com.cn 这类二级后缀本身占两段，不能简单取末两段。
    """
    parts = (host or "").lower().split(".")
    if len(parts) < 2:
        return host or ""
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_SUFFIX:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host_of(url):
    """URL → 主机名（小写），解析失败返回 ''"""
    try:
        from urllib.parse import urlparse
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def snapshot_page_ids(port):
    """当前 page target id 集合（用于记录"操作前已存在"的 tab）"""
    return {t["id"] for t in list_page_targets(port)}


def close_new_targets(port, known_ids, hosts=()):
    """关闭 known_ids 之后新出现、且域名命中 hosts 的 tab，返回关闭数量。

    用途：下载时站点自己 window.open 出打包/下载页（万方实测每次 +1，
    Chrome 重启还会恢复），这些 tab 不是 create_page 创建的，close_page 管不到。
    只动"新出现 + 域名命中白名单"的 tab：不碰其它站点、不碰操作前就有的、
    也不碰本进程正在使用的 tab。
    """
    allowed = {h.lower() for h in hosts if h}
    if not allowed:
        return 0
    closed = 0
    for t in list_page_targets(port):
        tid = t["id"]
        if tid in known_ids or tid in _ACTIVE_TARGETS:
            continue
        if _registrable(_host_of(t.get("url") or "")) in allowed:
            if close_target(port, tid):
                closed += 1
    return closed


def _warn_tab_limit(port):
    """tab 总数达到 tabs.max 时只告警，不自动关闭。

    四个 skill 共用一个 Chrome：本进程无从得知别的 skill / 别的进程正在用哪些
    tab，自动关"空 tab"会误伤它们刚建好、还没 navigate 的 about:blank（并行实测
    误关过 2/6）。真正的回收靠 create_page/close_page 配对。
    返回 (page target 总数, 其中未被本进程占用的空 tab 数)；未达上限返回 (0, 0)。
    """
    limit = get("tabs.max")
    if not limit or limit <= 0:
        return (0, 0)
    with _TAB_LOCK:
        tabs = list_page_targets(port)
        if len(tabs) < limit:
            return (0, 0)
        blanks = [t for t in tabs
                  if is_blank_target(t.get("url") or "") and t["id"] not in _ACTIVE_TARGETS]
        sys.stderr.write(
            f"[cdp] tab 数 {len(tabs)} 已达上限 {limit}（其中空 tab {len(blanks)} 个）；"
            f"只告警不自动关 —— 自动关会误伤并行任务正在用的 tab\n")
        return (len(tabs), len(blanks))


def create_page(port, timeout=None):
    """创建新 tab 并返回连到它的 CdpClient（并行抓取用）。

    通过 browser 级 CDP 的 Target.createTarget 新建 tab，
    轮询 /json 拿到新 target 的 ws url 后建立独立连接。
    用完必须 close_page(client)：断开 ws 不会关闭 tab，
    只调 client.close() 会让 tab 永久残留在共享 profile 的 Chrome 里。
    """
    _warn_tab_limit(port)
    r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5)
    version = json.loads(r.read())
    bws = websocket.create_connection(version["webSocketDebuggerUrl"], timeout=15, origin="http://localhost")
    target_id = None
    try:
        bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                             "params": {"url": "about:blank"}}))
        while True:
            msg = json.loads(bws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise CdpError(f"Target.createTarget 失败: {msg['error']}")
                target_id = msg["result"]["targetId"]
                _register_target(target_id)
                break
    finally:
        bws.close()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read())
            target = next((t for t in tabs if t.get("id") == target_id and t.get("webSocketDebuggerUrl")), None)
            if target:
                client = CdpClient(port, timeout=timeout, ws_url=target["webSocketDebuggerUrl"])
                client.target_id = target_id
                return client
        except Exception:
            pass
        time.sleep(0.3)
    _unregister_target(target_id)      # 建了 tab 却拿不到 ws：别让它永久占着活跃登记
    raise CdpError("新 tab 未就绪")


# ── 人类行为模拟 ────────────────────────────────────

_SCROLL_JS = """(maxRounds, dMin, dMax, pMin, pMax) => new Promise((resolve) => {
  const rounds = Math.min(maxRounds, 3 + Math.floor(Math.random() * 4));
  let i = 0;
  const step = () => {
    if (i >= rounds) { resolve(true); return; }
    i++;
    let delta = dMin + Math.floor(Math.random() * (dMax - dMin));
    if (Math.random() < 0.15) delta = -(Math.floor(dMin / 3) + Math.floor(Math.random() * Math.floor(dMin / 3)));
    window.scrollBy(0, delta);
    const pause = pMin + Math.random() * (pMax - pMin);
    setTimeout(step, pause);
  };
  step();
})"""


def human_scroll(client, max_rounds=None):
    """类人滚动：3-6 轮随机步长、15% 回滚、停顿 0.5-4s"""
    max_rounds = get("human.scroll_rounds")[1] if max_rounds is None else max_rounds
    d_min, d_max = get("human.scroll_delta")
    p_min, p_max = get("human.pause")
    client.evaluate(f"({_SCROLL_JS})({max_rounds}, {d_min}, {d_max}, {p_min}, {p_max})", await_promise=True)


def human_mouse(client, probability=None):
    """40% 概率随机移动鼠标（输入轨迹特征）"""
    probability = get("human.mouse_prob") if probability is None else probability
    if random.random() >= probability:
        return
    x, y = random.randint(100, 800), random.randint(100, 600)
    client.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    time.sleep(random.uniform(0.5, 1.5))


def real_click(client, x, y):
    """真实鼠标点击（mousedown/mouseup），触发页面 mousedown/mouseup 监听逻辑

    万方下载按钮在 mousedown/mouseup 阶段把 href 替换为带 transaction token 的
    下载 URL，JS .click()（只触发 click 事件）不触发该逻辑。
    """
    client.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    time.sleep(random.uniform(0.2, 0.5))
    client.send("Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    time.sleep(random.uniform(0.1, 0.3))
    client.send("Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def random_delay(lo=None, hi=None):
    """请求间随机等待（防频率特征）"""
    lo, hi = (get("rate.limit")[0] if lo is None else lo), (get("rate.limit")[1] if hi is None else hi)
    time.sleep(random.uniform(lo, hi))


class RateLimiter:
    """相邻操作最小间隔（首次调用不等待）"""

    def __init__(self, lo=None, hi=None):
        lo, hi = (get("rate.limit")[0] if lo is None else lo), (get("rate.limit")[1] if hi is None else hi)
        self.lo, self.hi = lo, hi
        self._last = 0.0

    def wait(self):
        elapsed = time.time() - self._last
        need = random.uniform(self.lo, self.hi)
        if elapsed < need:
            time.sleep(need - elapsed)
        self._last = time.time()


# ── 下载能力（ieee/wf 共用）────────────────────────────

def _browser_ws(port):
    r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5)
    version = json.loads(r.read())
    return websocket.create_connection(version["webSocketDebuggerUrl"], timeout=15,
                                       origin="http://localhost")


def parse_download_begin(msg):
    p = msg.get("params", {})
    return {"filename": p.get("suggestedFilename"), "guid": p.get("guid")}


def parse_download_progress(msg):
    p = msg.get("params", {})
    return {"state": p.get("state"),
            "path": p.get("filePath"),
            "size": p.get("totalBytes")}


def consume_download_events(recv, timeout):
    """从下载事件流中收 downloadWillBegin + downloadProgress(completed)。

    recv: 可调用，带一个 float 剩余超时参数（remaining），返回一条事件消息
         dict（无消息时抛 CdpError/超时异常）。
    返回 {filename, path, size}；canceled 或超时抛 CdpError。
    """
    state = {"filename": None, "path": None, "size": 0}
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            msg = recv(remaining)
        except CdpError as e:
            raise CdpError("Download timeout") from e
        method = msg.get("method", "")
        if method == "Browser.downloadWillBegin":
            state.update(parse_download_begin(msg))
        elif method == "Browser.downloadProgress":
            p = parse_download_progress(msg)
            if p["state"] == "completed":
                state["path"], state["size"] = p["path"], p["size"]
                return {k: state[k] for k in ("filename", "path", "size")}
            if p["state"] == "canceled":
                raise CdpError("Download canceled")
    raise CdpError("Download timeout")


def fetch_binary(client, url, timeout=None):
    """页面上下文 fetch 二进制 → bytes（同源自动带 cookie，ieee 用）。"""
    timeout = timeout if timeout is not None else get("timeout.download")
    expr = (f"fetch({json.dumps(url)}).then(r => r.arrayBuffer()).then(b => {{"
            f"const u = new Uint8Array(b); let s = ''; const C = 0x8000;"
            f"for (let i = 0; i < u.length; i += C) "
            f"s += String.fromCharCode.apply(null, u.subarray(i, i + C));"
            f"return btoa(s);}})")
    import base64
    b64 = client.evaluate(expr, await_promise=True, timeout=timeout)
    return base64.b64decode(b64) if b64 else b""


def download_with_events(client, trigger_fn, save_dir, timeout=None):
    """万方下载：browser 级 ws 设下载行为，trigger_fn() 触发，等下载完成。

    trigger_fn: 无参可调用（在 page 级 client 上执行点击）。
    返回 {filename, path, size}。
    """
    timeout = timeout if timeout is not None else get("timeout.download_event")
    os.makedirs(save_dir, exist_ok=True)
    port = client.port
    # 下载时站点自己会 window.open 出打包/下载页（万方实测每次 +1，Chrome 重启还会
    # 恢复），它们不是 create_page 创建的、close_page 管不到 —— 记下操作前的 tab 与
    # 当前站点注册域，下载结束后只清"新出现 + 同名注册域"的那些。
    known = snapshot_page_ids(port)
    try:
        site = _registrable(_host_of(client.evaluate("location.href") or ""))
    except Exception:
        site = ""
    bws = _browser_ws(port)
    try:
        bws.send(json.dumps({"id": 1, "method": "Browser.setDownloadBehavior",
                             "params": {"behavior": "allowAndName",
                                        "downloadPath": save_dir,
                                        "eventsEnabled": True}}))
        deadline = time.time() + 15
        while time.time() < deadline:
            m = json.loads(bws.recv())
            if m.get("id") == 1:
                if "error" in m:
                    raise CdpError(f"setDownloadBehavior: {m['error']}")
                break
        trigger_fn()
        result = consume_download_events(lambda t: _recv_ws(bws, t), timeout)
    finally:
        try:
            bws.close()
        except Exception:
            pass
    try:
        close_new_targets(port, known, (site,) if site else ())
    except Exception:
        pass
    return result


def _recv_ws(ws, timeout):
    ws.settimeout(timeout)
    try:
        return json.loads(ws.recv())
    except (websocket.WebSocketTimeoutException, TimeoutError):
        raise CdpError("Download timeout")
    except (websocket.WebSocketException, OSError) as e:
        raise CdpError(f"WebSocket 通信失败: {e}")


# ── 结果落盘（供多对话接力复用）────────────────────────

def _find_record_list(data):
    """从结果对象里找出「记录列表」——兼容 `results` / `items` / `refs` / `files`。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "items", "refs", "files", "chapters"):
        v = data.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def flatten_records(data):
    """把落盘结果压成**一层记录**，供 `.jsonl` / `.tsv` 摘要使用。

    检索类结果形状是 `{results:[{keyword, items:[...]}]}`——这里会把 `items`
    展平到一层并带上所属 `keyword`，这样摘要里一行就是**一篇文献**。
    """
    out = []
    for r in _find_record_list(data):
        items = r.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            for it in items:
                out.append({"keyword": r.get("keyword"), **it})
        else:
            out.append(r)
    return out


def _tsv_cell(v):
    """TSV 单元格：去制表符/换行（否则会破坏列），超长截断。"""
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else ("" if v is None else str(v))
    s = s.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return s if len(s) <= 80 else s[:77] + "..."


def _find_record_list(data):
    """从结果对象里找出「记录列表」——兼容 `results` / `items` / `refs` / `files` / `notes`。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "items", "refs", "files", "chapters", "notes", "questions", "columns"):
        v = data.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def flatten_records(data):
    """把落盘结果压成**一层记录**，供 `.jsonl` 镜像使用。

    检索类结果形状是 `{results:[{keyword, items:[...]}]}`——这里会把 `items`
    展平到一层并带上所属 `keyword`，这样镜像里一行就是**一条结果**。
    """
    out = []
    for r in _find_record_list(data):
        items = r.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            for it in items:
                out.append({"keyword": r.get("keyword"), **it})
        else:
            out.append(r)
    return out


def write_log(data, script_name, params=None):
    """完整结果落盘到 <logs.dir>/<脚本名>-<时间戳>.json，返回绝对路径。

    宿主对脚本 stdout 有大小上限，结果一多就会被截断；Agent 需要全文时
    直接读这个文件。**必须在 stdout 输出之前调用**：这样 stdout 崩了也不丢结果。

    ★ 2026-10 增强 ①：落盘时**追加一个 `meta` 字段**（纯增量，`count`/`results` 不动）：
        {"meta": {"skill", "script", "time", "argv"}}
    `argv` 默认取本次命令行参数 —— **原始 JSON 因此能自证"是哪个命令、什么时候产生的"**。

    ★ 2026-10 增强 ②：**额外**落一份 `<名>.jsonl` 等价镜像（主 `.json` 契约完全不变）。
      它**不是**因为"文件会被截断"（文件是我们自己完整写的，不会截断），而是因为
      **一行一条记录**：agent 可以直接 `Select-String` / `grep` / 按行分页读取，
      不必每次都写一个解析器。首行是 `{"__meta__": …, "__count__": …}`。

    ⚠️ **不再写 `.tsv`**：它是纯派生视图，列集合随记录字段漂移（检索日志与详情日志的列
    完全不同），且不属于"证据层"。需要一行一篇的表时，**由下游分析层生成**
    （例如 `lit-research` 的聚合表）。
    """
    import datetime
    import sys

    logs_dir = get("logs.dir")
    os.makedirs(logs_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(logs_dir, f"{script_name}-{ts}.json")

    payload = dict(data) if isinstance(data, dict) else {"results": data}
    # `meta` 放最后，保持原有字段在前（对按位置阅读的人影响最小）
    payload["meta"] = {
        "skill": "wanfang-research",
        "script": script_name,
        "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "argv": list(params) if params is not None else list(sys.argv[1:]),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # `.jsonl` 等价镜像（失败不影响主结果）
    try:
        recs = flatten_records(data)
        with open(path[:-5] + ".jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps(
                {"__meta__": payload["meta"],
                 "__count__": payload.get("count", len(recs))},
                ensure_ascii=False) + "\n")
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 镜像是附加物；它失败不该让主结果算失败
    return os.path.abspath(path)
