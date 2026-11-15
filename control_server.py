# -*- coding: utf-8 -*-
"""野狐自动对弈 · 本机 HTTP 控制接口（给 fox-agent 用）

契约见 `projects/fox-agent/DESIGN.md` 第 4 节。这里只做「接口 + 状态」，
**不 import gui / fox_auto / tkinter**，所以能被单元测试直接 import，不会把
野狐、引擎、Tk 拉起来。

接口（全部要求 `X-Control-Token`）
------------------------------------------------------------
    GET  /api/ping     -> {"ok":true,"app":"野狐自动对弈","version":"3.1.6"}
    GET  /api/status   -> {"ok":true, state/running/paused/... }（见 STATUS_FIELDS）
    GET  /api/engines  -> {"ok":true,"engines":[{"key","name","kind","note"}]}
    POST /api/config   -> {"ok":true,"applied":{...}}
    POST /api/start    -> {"ok":true,"state":"starting"}
    POST /api/stop     -> {"ok":true}
    POST /api/pause    -> {"ok":true}
    POST /api/resume   -> {"ok":true}

线程规则（DESIGN 4.2）
------------------------------------------------------------
    · HTTP 线程**绝不碰 Tk**：所有 GUI 动作都通过构造时传进来的 `ui_call`
      投递给 GUI 线程执行（gui.py 用的是它自己的 queue + _poll 消息泵，
      等价于 root.after，绝对不跨线程调 Tk）。
    · `/api/status` 只读 `ControlState`（带锁），**不查 Tk**。
    · GUI 侧每 100ms 把 Tk 变量/运行标志同步进 ControlState（见 gui.App._sync_control）。

鉴权（DESIGN 4.3）
------------------------------------------------------------
    · 只绑 127.0.0.1，并额外核对 client_address。
    · token 存 `%LOCALAPPDATA%\\野狐自动对弈\\control_token.txt`
      （文件已存在就沿用，不存在就生成 32 位十六进制），
      请求头 `X-Control-Token` 必须一致，否则 401。
    · 没有任何「执行任意命令」的入口。

curl 示例（PowerShell 用 curl.exe）
------------------------------------------------------------
    TOKEN = type "%LOCALAPPDATA%\\野狐自动对弈\\control_token.txt"
    curl.exe http://127.0.0.1:8767/api/ping -H "X-Control-Token: <TOKEN>"
    curl.exe -X POST http://127.0.0.1:8767/api/start -H "X-Control-Token: <TOKEN>"
    curl.exe -X POST http://127.0.0.1:8767/api/config -H "X-Control-Token: <TOKEN>" ^
             -H "Content-Type: application/json" -d "{\\"komi\\":7.5,\\"my_color\\":\\"B\\"}"
"""
import hmac
import json
import math
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# ================================================================ 常量

HOST = "127.0.0.1"                 # 只绑本机（DESIGN 4.3）
PORT = 8767                        # 8765 被死活分析服务占了
TOKEN_HEADER = "X-Control-Token"
TOKEN_DIR_NAME = "野狐自动对弈"
TOKEN_FILENAME = "control_token.txt"
TOKEN_ENV = "WEIQI_CONTROL_TOKEN_FILE"   # 测试/联调可指到别处；默认按 DESIGN 走

APP_NAME = "野狐自动对弈"
VERSION_FALLBACK = "3.1.6"

MAX_BODY = 64 * 1024               # 本地接口，不需要大 body
MAX_ENGINES = 200

# DESIGN 4.1 里 status 的字段（顺序即 JSON 输出顺序；多一个少一个都算违约）
STATUS_FIELDS = (
    "state", "running", "paused", "my_color", "komi", "rules",
    "engine_key", "engine_name", "think_mode", "think", "think_min",
    "think_max", "visits", "move_no", "last_move", "last_message",
    "game_over", "winrate", "score_lead",
)

STATES = ("idle", "starting", "running", "paused", "done", "error")
THINK_MODES = ("fixed", "random", "visits")
RULES = ("chinese", "japanese", "korean")
COLORS = ("B", "W")

# 运行中允许改的字段（DESIGN 4.2：只允许 think*/visits）
LIVE_CONFIG_KEYS = ("think_mode", "think", "think_min", "think_max", "visits")
CONFIG_KEYS = ("my_color", "komi", "rules", "engine_key") + LIVE_CONFIG_KEYS

# /api/start 失败码 -> HTTP 状态
START_ERROR_STATUS = {
    "no_engine": 409,
    "engine_unusable": 409,
    "no_fox": 409,
    "bad_config": 400,
    "already_running": 200,
    "start_failed": 500,
}

_STATE_DEFAULTS = {
    "state": "idle",
    "running": False,
    "paused": False,
    "my_color": "B",
    "komi": 7.5,
    "rules": "chinese",
    "engine_key": "",
    "engine_name": "",
    "think_mode": "fixed",
    "think": 5.0,
    "think_min": 1.0,
    "think_max": 10.0,
    "visits": 10000,
    "move_no": 0,
    "last_move": None,
    "last_message": "",
    "game_over": False,
    # AI 托管押分要用的「我方视角」胜率/目差（2026-11-08 加）
    "winrate": None,
    "score_lead": None,
}


# ================================================================ 异常

class ControlError(Exception):
    """统一的可预期错误：dispatch() 直接把它翻成 JSON + HTTP 状态码"""

    def __init__(self, message, code="error", status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


# ================================================================ 共享状态

class ControlState:
    """GUI ↔ HTTP 之间唯一的共享状态

    ⚠️ 必须带锁：GUI 线程每 100ms 写一次，HTTP 线程随时读。
    ⚠️ 读接口只返回 `snapshot()` 的拷贝，外部拿到的是死数据，改不回去。
    """

    FIELDS = STATUS_FIELDS

    def __init__(self, **initial):
        self._lock = threading.RLock()
        self._d = dict(_STATE_DEFAULTS)
        for k, v in initial.items():
            if k in self._d:
                self._d[k] = v

    def snapshot(self):
        """返回一份完整拷贝（含全部 STATUS_FIELDS）"""
        with self._lock:
            return {k: self._d.get(k, _STATE_DEFAULTS.get(k)) for k in self.FIELDS}

    def update(self, **kw):
        """只更新认识的字段；值按原样存（类型由调用方负责）"""
        with self._lock:
            for k, v in kw.items():
                if k in self.FIELDS:
                    self._d[k] = v
        return self.snapshot()

    def get(self, key, default=None):
        with self._lock:
            return self._d.get(key, default)


# ================================================================ token

def default_token_path():
    """DESIGN 规定的 token 位置

    `%LOCALAPPDATA%\\野狐自动对弈\\control_token.txt`
    （LOCALAPPDATA 取不到时退回用户主目录，保证「一定有个地方能放」）
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, TOKEN_DIR_NAME, TOKEN_FILENAME)


def load_or_create_token(path=None):
    """读 token；没有/被清空就生成并写盘。**不改动已有 token**（沿用）。"""
    p = path or os.environ.get(TOKEN_ENV) or default_token_path()
    try:
        with open(p, encoding="utf-8") as f:
            tok = f.read().strip()
        if len(tok) >= 16:
            return tok
    except FileNotFoundError:
        pass
    except Exception:
        pass                    # 读不了（权限/编码）→ 下面覆盖写一次

    tok = secrets.token_hex(16)          # 32 字符
    d = os.path.dirname(os.path.abspath(p))
    os.makedirs(d, exist_ok=True)
    # 先写临时文件再换名：避免写一半断电留下半截 token
    tmp = os.path.join(d, f".{TOKEN_FILENAME}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.replace(tmp, p)
    return tok


# ================================================================ 参数校验

def _is_num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _need_num(name, v):
    if not _is_num(v):
        raise ControlError(f"{name} 必须是数字", code="bad_value")
    return float(v)


def _validate_config_body(body):
    """校验 POST /api/config 的 body，返回规范化后的 dict

    只做「类型/取值域」校验；吸附与夹紧（0.5 贴目、1~180 秒、1~100000 计算量）
    在 GUI 侧做 —— 那边已有 komi_snap/time_snap/visits_snap，不能在这里复制一份。
    """
    if not isinstance(body, dict):
        raise ControlError("body 必须是 JSON 对象", code="bad_request")
    unknown = [k for k in body if k not in CONFIG_KEYS]
    if unknown:
        raise ControlError(
            "不认识的字段：" + "、".join(sorted(map(str, unknown)))
            + "（可用：" + "、".join(CONFIG_KEYS) + "）",
            code="bad_field")
    out = {}
    for k, v in body.items():
        if k == "my_color":
            if not isinstance(v, str) or v.strip().upper() not in COLORS:
                raise ControlError("my_color 只能是 \"B\" 或 \"W\"",
                                   code="bad_value")
            out[k] = v.strip().upper()
        elif k == "komi":
            f = _need_num(k, v)
            if not (-150.0 <= f <= 150.0):
                raise ControlError("komi 要在 -150 ~ 150 之间", code="bad_value")
            out[k] = f
        elif k == "rules":
            if not isinstance(v, str) or v.strip().lower() not in RULES:
                raise ControlError(
                    "rules 只能是 " + "/".join(RULES), code="bad_value")
            out[k] = v.strip().lower()
        elif k == "engine_key":
            if not isinstance(v, str) or not v.strip():
                raise ControlError("engine_key 不能为空", code="bad_value")
            out[k] = v.strip()
        elif k == "think_mode":
            if not isinstance(v, str) or v.strip().lower() not in THINK_MODES:
                raise ControlError(
                    "think_mode 只能是 " + "/".join(THINK_MODES),
                    code="bad_value")
            out[k] = v.strip().lower()
        elif k in ("think", "think_min", "think_max"):
            f = _need_num(k, v)
            if not (0.5 <= f <= 180.0):
                raise ControlError(f"{k} 要在 0.5 ~ 180 秒之间",
                                   code="bad_value")
            out[k] = f
        elif k == "visits":
            f = _need_num(k, v)
            n = int(round(f))
            if not (1 <= n <= 100000):
                raise ControlError("visits 要在 1 ~ 100000 之间",
                                   code="bad_value")
            out[k] = n
    if not out:
        raise ControlError("body 里没有可应用的字段", code="bad_request")
    return out


# ================================================================ 服务

class ControlServer:
    """127.0.0.1:8767 的 HTTP 服务（线程 = daemon，随 GUI 进程退出）

    参数
    ----------------------------------------------------
    app        : gui.App 实例。**只在 ui_call 里**用它（绝不跨线程直接调）
    state      : 共享的 ControlState；不给就自己建一个
    version    : /api/ping 报的版本号（gui 传 APP_VERSION）
    ui_call    : callable(fn, timeout) -> fn 的返回值；在 GUI 线程执行
                 （不传 = 直接执行，只给测试用）
    token_path : token 文件路径（不传 = DESIGN 默认位置）
    log        : callable(msg)；只写文件，**禁止碰 Tk**
    """

    def __init__(self, app=None, *, state=None, version=VERSION_FALLBACK,
                 host=HOST, port=PORT, token_path=None,
                 ui_call=None, log=None):
        self.app = app
        self.state = state if state is not None else ControlState()
        self.version = str(version or VERSION_FALLBACK)
        self.host = host
        self.port = int(port)
        self._want_port = int(port)
        self.token_path = token_path
        self._ui_call = ui_call
        self._log_fn = log
        self._token = ""
        self._engines = []
        self._engines_lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self):
        return bool(self._thread is not None and self._thread.is_alive())

    def start(self):
        """建 token + 起监听。成功返回 True；端口被占/写不了 token 返回 False。"""
        with self._lock:
            if self.running:
                return True
            if self.host != HOST:
                # DESIGN 4.3：只绑 127.0.0.1，不许配成 0.0.0.0
                self.log(f"控制接口只允许绑定 {HOST}，已忽略 {self.host}")
                self.host = HOST
            path = (self.token_path or os.environ.get(TOKEN_ENV)
                    or default_token_path())
            try:
                self.token_path = path
                self._token = load_or_create_token(path)
            except Exception as e:
                self.log(f"token 写不了（{e}）—— 控制接口不启动")
                return False
            try:
                httpd = ThreadingHTTPServer((self.host, self._want_port),
                                            _make_handler(self))
            except OSError as e:
                self.log(f"控制接口绑定 {self.host}:{self._want_port} 失败：{e}")
                return False
            httpd.daemon_threads = True
            self._httpd = httpd
            self.port = int(httpd.server_address[1])
            t = threading.Thread(target=httpd.serve_forever,
                                 kwargs={"poll_interval": 0.2},
                                 name="fox-control-http", daemon=True)
            self._thread = t
            t.start()
        self.log(f"控制接口已监听 http://{self.host}:{self.port}")
        return True

    def stop(self):
        """关监听（可重复调用）。GUI 退出前调一次。"""
        with self._lock:
            httpd, self._httpd = self._httpd, None
            self._thread = None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass

    # ------------------------------------------------------------ 小工具

    def log(self, msg):
        fn = self._log_fn
        if fn is None:
            return
        try:
            fn(str(msg))
        except Exception:
            pass

    def token(self):
        return self._token

    def set_engines(self, engines):
        """GUI 线程调用：把下拉框里实际的引擎列表推给接口

        ⚠️ 不能在这里查 Tk —— GUI 线程调，HTTP 线程读，锁保护。
        """
        out = []
        for e in (engines or [])[:MAX_ENGINES]:
            if not isinstance(e, dict):
                continue
            key = str(e.get("key") or "").strip()
            if not key:
                continue
            out.append({
                "key": key,
                "name": str(e.get("name") or ""),
                "kind": str(e.get("kind") or ""),
                "note": str(e.get("note") or ""),
            })
        with self._engines_lock:
            self._engines = out
        return len(out)

    def _call_ui(self, fn, timeout=8.0):
        """在 GUI 线程执行 fn；没有 ui_call（测试态）就直接跑"""
        if self._ui_call is None:
            return fn()
        try:
            return self._ui_call(fn, timeout)
        except ControlError:
            raise
        except TimeoutError:
            raise ControlError("GUI 忙，没有及时响应", code="gui_timeout",
                               status=503)
        except ValueError:
            raise
        except Exception as e:
            # 把 GUI 侧异常翻成 500，别把 traceback 漏给客户端
            self.log(f"GUI 动作失败：{type(e).__name__}: {e}")
            raise ControlError(f"{type(e).__name__}: {e}", code="gui_error",
                               status=500)

    def _require_app(self):
        if self.app is None:
            raise ControlError("控制接口没有绑定 GUI（服务单独启动？）",
                               code="no_app", status=503)
        return self.app

    # ------------------------------------------------------------ 路由

    def dispatch(self, method, path, body):
        """纯逻辑入口（测试可以直接调）：返回 (status, payload_dict)"""
        m = (method or "").upper()
        p = (path or "/")
        p = urlsplit(p).path
        p = p.rstrip("/") or "/"
        routes = {
            "/api/ping": ("GET", self._api_ping),
            "/api/status": ("GET", self._api_status),
            "/api/engines": ("GET", self._api_engines),
            "/api/config": ("POST", self._api_config),
            "/api/start": ("POST", self._api_start),
            "/api/stop": ("POST", self._api_stop),
            "/api/pause": ("POST", self._api_pause),
            "/api/resume": ("POST", self._api_resume),
        }
        route = routes.get(p)
        if route is None:
            raise ControlError(f"未知接口：{p}", code="not_found", status=404)
        want, fn = route
        if m != want:
            raise ControlError(f"{p} 只支持 {want}", code="method_not_allowed",
                               status=405)
        if want == "GET":
            return fn()
        if not isinstance(body, dict):
            body = {}
        return fn(body)

    # ------------------------------------------------------------ GET

    def _api_ping(self):
        return 200, {"ok": True, "app": APP_NAME, "version": self.version}

    def _api_status(self):
        payload = {"ok": True}
        payload.update(self.state.snapshot())
        return 200, payload

    def _api_engines(self):
        with self._engines_lock:
            engines = [dict(e) for e in self._engines]
        return 200, {"ok": True, "engines": engines}

    # ------------------------------------------------------------ POST

    def _api_config(self, body):
        values = _validate_config_body(body)
        snap = self.state.snapshot()
        if snap.get("running"):
            locked = [k for k in values if k not in LIVE_CONFIG_KEYS]
            if locked:
                raise ControlError(
                    "引擎运行中，只能改 " + "/".join(LIVE_CONFIG_KEYS)
                    + "（先 /api/stop 才能改 " + "、".join(sorted(locked)) + "）",
                    code="locked", status=409)
        app = self._require_app()
        try:
            applied = self._call_ui(
                lambda: app.apply_control_config(values), timeout=8.0)
        except ValueError as e:
            raise ControlError(str(e), code="bad_value", status=400)
        if not isinstance(applied, dict):
            applied = dict(values)
        # 把落地的规范值写回共享状态（/api/status 立刻可见，不等下一拍）
        self.state.update(**{k: v for k, v in applied.items()
                             if k in ControlState.FIELDS})
        return 200, {"ok": True, "applied": applied}

    def _api_start(self, body):
        snap = self.state.snapshot()
        if snap.get("running"):
            return 200, {"ok": True, "state": snap.get("state") or "running"}
        app = self._require_app()
        try:
            res = self._call_ui(lambda: app.control_start(), timeout=20.0)
        except ControlError:
            raise
        except ValueError as e:
            raise ControlError(str(e), code="bad_config", status=400)
        if not isinstance(res, dict):
            res = {"ok": True}
        if not res.get("ok"):
            code = str(res.get("code") or "start_failed")
            raise ControlError(res.get("error") or "启动失败", code=code,
                               status=START_ERROR_STATUS.get(code, 500))
        if not res.get("started", True):
            raise ControlError(res.get("error") or "启动没有发生",
                               code="start_failed", status=500)
        self.state.update(state="starting", running=True, paused=False,
                          game_over=False, move_no=0, last_move=None,
                          last_message="")
        return 200, {"ok": True, "state": "starting"}

    def _api_stop(self, body):
        snap = self.state.snapshot()
        if not snap.get("running"):
            return 200, {"ok": True}
        app = self._require_app()
        self._call_ui(app.stop, timeout=10.0)
        # 等 GUI 真正收工（App.stop 立刻返回，_finish 在 done 事件里才置 running=False）
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                still = bool(self._call_ui(
                    lambda: bool(getattr(app, "running", False)), timeout=2.0))
            except ControlError:
                break
            if not still:
                break
            time.sleep(0.05)
        self.state.update(state="done", running=False, paused=False)
        return 200, {"ok": True}

    def _api_pause(self, body):
        snap = self.state.snapshot()
        if not snap.get("running"):
            raise ControlError("引擎没在运行，不能暂停", code="not_running",
                               status=409)
        if snap.get("paused"):
            return 200, {"ok": True}
        app = self._require_app()
        self._call_ui(app.pause, timeout=10.0)
        self.state.update(state="paused", paused=True)
        return 200, {"ok": True}

    def _api_resume(self, body):
        snap = self.state.snapshot()
        if not snap.get("running"):
            raise ControlError("引擎没在运行，不能继续", code="not_running",
                               status=409)
        if not snap.get("paused"):
            return 200, {"ok": True}
        app = self._require_app()
        self._call_ui(app.resume, timeout=10.0)
        self.state.update(state="running", paused=False)
        return 200, {"ok": True}


# ================================================================ HTTP 层

def _make_handler(ctrl):
    """给 ThreadingHTTPServer 造 handler（闭包持有 ControlServer）"""

    class _Handler(BaseHTTPRequestHandler):
        # HTTP/1.0：一请求一连接，最省心（本地调用，不需要 keep-alive）
        protocol_version = "HTTP/1.0"
        server_version = "FoxControl/1.0"

        # -- 基础设施 ------------------------------------------------------
        def setup(self):
            super().setup()
            try:
                self.connection.settimeout(10.0)
            except Exception:
                pass

        def log_message(self, fmt, *args):
            # BaseHTTPRequestHandler 的默认日志写 stderr —— 打包后是黑洞，
            # 而且 GUI 日志文件更该是唯一出口。
            try:
                ctrl.log("[control] " + (fmt % args))
            except Exception:
                pass

        # -- 方法入口 ------------------------------------------------------
        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_PUT(self):
            self._serve("PUT")

        def do_DELETE(self):
            self._serve("DELETE")

        def do_OPTIONS(self):
            self._serve("OPTIONS")

        # -- 处理 ----------------------------------------------------------
        def _serve(self, method):
            status, payload = self._process(method)
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            except Exception as e:              # 正常到不了这里；兜底
                status = 500
                body = json.dumps({"ok": False,
                                   "error": f"响应序列化失败：{e}"},
                                  ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True

        def _process(self, method):
            try:
                # ① 只认本机（绑定已经是 127.0.0.1，这里再挡一道）
                try:
                    addr = self.client_address[0]
                except Exception:
                    addr = ""
                if addr not in ("127.0.0.1", "::1", "localhost"):
                    return 403, {"ok": False, "error": "只允许本机访问",
                                 "code": "forbidden"}

                # ② 鉴权（先鉴权再解析 body：不给未授权请求探接口的可能）
                token = self.headers.get(TOKEN_HEADER)
                if not self._token_ok(token):
                    return 401, {"ok": False,
                                 "error": f"token 不对（请求头 {TOKEN_HEADER}）",
                                 "code": "unauthorized"}

                # ③ body
                body = {}
                if method in ("POST", "PUT", "DELETE"):
                    body, err = self._read_json()
                    if err:
                        return 400, {"ok": False, "error": err,
                                     "code": "bad_request"}

                # ④ 路由
                return ctrl.dispatch(method, self.path, body)

            except ControlError as e:
                return e.status, {"ok": False, "error": str(e), "code": e.code}
            except Exception as e:
                ctrl.log(f"[control] {method} {self.path} 内部错误："
                         f"{type(e).__name__}: {e}")
                return 500, {"ok": False,
                             "error": f"{type(e).__name__}: {e}",
                             "code": "internal"}

        def _token_ok(self, provided):
            expected = ctrl.token()
            if not expected or provided is None:
                return False
            try:
                return hmac.compare_digest(str(provided).strip(), expected)
            except Exception:
                return False

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return None, "Content-Length 不合法"
            if n < 0 or n > MAX_BODY:
                return None, f"请求体太大（上限 {MAX_BODY} 字节）"
            if n == 0:
                return {}, None
            raw = self.rfile.read(n)
            if len(raw) != n:
                return None, "请求体没读全"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return None, "请求体不是合法 JSON"
            if not isinstance(data, dict):
                return None, "请求体必须是 JSON 对象"
            return data, None

    return _Handler


# ================================================================ 自检入口

def _main(argv=None):
    """`python control_server.py`：只起一个「无 GUI」的服务，方便手工 curl

    ⚠️ 没有 app，所以 /api/start|stop|pause|resume|config 会返回 503；
    /api/ping、/api/status、/api/engines 能用来看契约。
    """
    import argparse
    ap = argparse.ArgumentParser(description="野狐自动对弈 · 控制接口自检")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--token-file", default=None)
    args = ap.parse_args(argv)

    srv = ControlServer(port=args.port, token_path=args.token_file,
                        log=lambda m: print(m, flush=True))
    if not srv.start():
        print("启动失败", flush=True)
        return 1
    print(f"token 文件：{srv.token_path or os.environ.get(TOKEN_ENV) or default_token_path()}",
          flush=True)
    print(f"监听：http://{srv.host}:{srv.port}（Ctrl+C 退出）", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
