# -*- coding: utf-8 -*-
"""control_server.py 的自测（**不启动真野狐、不开真局**）

跑法：
    cd projects/fox-weiyi-auto
    python _test_control_server.py

覆盖：
    GET  /api/ping /api/status /api/engines
    POST /api/config /api/start /api/stop /api/pause /api/resume
    token 缺失/错误/正确，未知路径、错方法、坏 JSON、未知字段
    运行中 config 只放 think*/visits（其余 409）
    start 失败码（no_engine）→ 409
    token 文件内容/沿用

所有 GUI 动作都经过 FakeApp 的 `ui_call`（模拟 gui.App._control_call），
所以这里不会 import gui、不会建 Tk、不会碰野狐。
"""
import json
import os
import shutil
import sys
import tempfile
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import control_server as CS  # noqa: E402

OK = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        OK.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


# ================================================================ 假 App

class FakeApp:
    """模拟 gui.App 上 control_server 用到的那几个方法（纯内存，无 Tk）"""

    def __init__(self):
        self.config = {
            "my_color": "B", "komi": 7.5, "rules": "chinese",
            "engine_key": "cloud:vip-share:fdx", "engine_name": "智子VIP共享",
            "think_mode": "fixed", "think": 5.0, "think_min": 1.0,
            "think_max": 10.0, "visits": 10000,
        }
        self.running = False
        self.paused = False
        self.calls = []
        self.start_result = {"ok": True, "started": True, "state": "starting"}

    # -- 接口实际调用的 5 个方法 ----------------------------------------
    def apply_control_config(self, values):
        if self.running:
            bad = [k for k in values if k not in CS.LIVE_CONFIG_KEYS]
            if bad:
                # 和真实 GUI 一样：运行中改别的 → ValueError（服务端应先挡成 409）
                raise ValueError("运行中不能改：" + "、".join(sorted(bad)))
        if "engine_key" in values and values["engine_key"] not in (
                "cloud:vip-share:fdx", "local:b18c384"):
            raise ValueError(f"未知 engine_key：{values['engine_key']}")
        self.config.update(values)
        self.calls.append(("config", dict(values)))
        return dict(values)

    def control_start(self):
        self.calls.append(("start",))
        res = dict(self.start_result)
        if res.get("ok") and res.get("started", True):
            self.running = True
            self.paused = False
        return res

    def stop(self):
        self.calls.append(("stop",))
        self.running = False
        self.paused = False

    def pause(self):
        self.calls.append(("pause",))
        self.paused = True

    def resume(self):
        self.calls.append(("resume",))
        self.paused = False


# ================================================================ HTTP 小工具

class Client:
    def __init__(self, srv):
        self.srv = srv
        self.base = f"http://127.0.0.1:{srv.port}"

    def call(self, method, path, body=None, token=None, raw=None, timeout=6.0):
        data = raw
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method)
        if token is not None:
            req.add_header("X-Control-Token", token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw_body = e.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw_body)
            except Exception:
                payload = {"raw": raw_body}
            return e.code, payload
        except urllib.error.URLError as e:
            return 0, {"error": f"连接失败：{e}"}


def boot(seed=None, app=None, same_token_file=None):
    """起一个临时服务（随机端口 + 临时 token 文件）"""
    app = app or FakeApp()
    state = CS.ControlState(**(seed or {}))
    tmp = tempfile.mkdtemp(prefix="foxctl-")
    token_file = same_token_file or os.path.join(tmp, "control_token.txt")
    ui_calls = []

    def ui_call(fn, timeout=8.0):
        # 模拟「HTTP 线程 → GUI 线程」：记录一次投递，然后同步执行
        ui_calls.append(getattr(fn, "__name__", "fn"))
        return fn()

    logs = []
    srv = CS.ControlServer(app, state=state, version="3.1.6", port=0,
                           token_path=token_file, ui_call=ui_call,
                           log=logs.append)
    if not srv.start():
        raise RuntimeError("测试服务起不来（端口/临时目录问题）")
    return types.SimpleNamespace(
        app=app, state=state, srv=srv, client=Client(srv), tmp=tmp,
        token=srv.token(), token_file=token_file, ui_calls=ui_calls,
        logs=logs, reap=[tmp])


def shutdown(ctx):
    ctx.srv.stop()
    for d in ctx.reap:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


# ================================================================ 测试项

def test_ping():
    ctx = boot()
    try:
        st, js = ctx.client.call("GET", "/api/ping", token=ctx.token)
        check("ping: 200", st == 200, str(st))
        check("ping: 字段严格",
              js == {"ok": True, "app": "野狐自动对弈", "version": "3.1.6"},
              repr(js))
        st, js = ctx.client.call("GET", "/api/ping/", token=ctx.token)
        check("ping: 末尾斜杠也认", st == 200 and js.get("ok") is True, repr(js))
    finally:
        shutdown(ctx)


def test_status():
    seed = {
        "state": "running", "running": True, "paused": False,
        "my_color": "W", "komi": 6.5, "rules": "japanese",
        "engine_key": "local:b18c384", "engine_name": "KataGo b18",
        "think_mode": "visits", "think": 5.0, "think_min": 1.0,
        "think_max": 16.0, "visits": 5000, "move_no": 12,
        "last_move": "Q16", "last_message": "运行中", "game_over": False,
    }
    ctx = boot(seed=seed)
    try:
        st, js = ctx.client.call("GET", "/api/status", token=ctx.token)
        check("status: 200", st == 200, str(st))
        want = {"ok"} | set(CS.STATUS_FIELDS)
        check("status: 字段集合与 DESIGN 一致",
              set(js) == want,
              f"多={sorted(set(js) - want)} 少={sorted(want - set(js))}")
        bad = {k: (js.get(k), seed[k]) for k in seed if js.get(k) != seed[k]}
        check("status: 值来自 ControlState", not bad, repr(bad))
        # 不查 Tk：只改共享状态，接口立刻看到
        ctx.state.update(state="paused", paused=True, move_no=13,
                         last_move="R14")
        st, js = ctx.client.call("GET", "/api/status", token=ctx.token)
        check("status: 读的是共享状态（无需 Tk）",
              js["state"] == "paused" and js["paused"] is True
              and js["move_no"] == 13 and js["last_move"] == "R14",
              repr(js))
    finally:
        shutdown(ctx)


def test_engines():
    ctx = boot()
    try:
        st, js = ctx.client.call("GET", "/api/engines", token=ctx.token)
        check("engines: 初始为空列表", st == 200 and js == {"ok": True, "engines": []},
              repr(js))
        n = ctx.srv.set_engines([
            {"key": "cloud:vip-share:fdx", "name": "智子VIP共享",
             "kind": "cloud", "note": "VIP 不限时", "idx": 1, "exe": "x"},
            {"key": "local:b18c384", "name": "KataGo b18c384",
             "kind": "local", "note": None},
            "坏条目", {"name": "没有 key"},
        ])
        check("engines: 只收有 key 的字典", n == 2, f"n={n}")
        st, js = ctx.client.call("GET", "/api/engines", token=ctx.token)
        check("engines: 200", st == 200 and js.get("ok") is True, repr(js))
        lst = js.get("engines")
        check("engines: 条目字段严格为 key/name/kind/note",
              isinstance(lst, list) and len(lst) == 2
              and all(set(e) == {"key", "name", "kind", "note"} for e in lst),
              repr(lst))
        check("engines: 空 note 归一成空串",
              lst and lst[1]["note"] == "", repr(lst))
        check("engines: 内容正确",
              lst and lst[0]["key"] == "cloud:vip-share:fdx"
              and lst[0]["kind"] == "cloud", repr(lst))
    finally:
        shutdown(ctx)


def test_config_idle():
    ctx = boot()
    try:
        body = {"my_color": "W", "komi": 7.3, "rules": "japanese",
                "engine_key": "local:b18c384", "think_mode": "random",
                "think": 6.0, "think_min": 2.0, "think_max": 12.0,
                "visits": 5000}
        st, js = ctx.client.call("POST", "/api/config", body=body,
                                 token=ctx.token)
        check("config: 200", st == 200, f"{st} {js}")
        check("config: 响应只有 ok/applied", set(js) == {"ok", "applied"},
              repr(js))
        check("config: applied 原样回传（假 App 不做吸附）",
              js.get("applied") == body, repr(js.get("applied")))
        check("config: 真的调到了 App.apply_control_config",
              any(c[0] == "config" for c in ctx.app.calls), repr(ctx.app.calls))
        check("config: 共享状态同步",
              ctx.state.get("my_color") == "W"
              and ctx.state.get("engine_key") == "local:b18c384"
              and ctx.state.get("think_mode") == "random"
              and ctx.state.get("visits") == 5000,
              repr(ctx.state.snapshot()))
        check("config: 走的是 ui_call（不是别线程直接摸 GUI）",
              len(ctx.ui_calls) >= 1, repr(ctx.ui_calls))

        st, js = ctx.client.call("POST", "/api/config", body={"visits": 20000},
                                 token=ctx.token)
        check("config: 允许只传一个字段",
              st == 200 and js.get("applied") == {"visits": 20000}, repr(js))
    finally:
        shutdown(ctx)


def test_config_invalid():
    ctx = boot()
    try:
        cases = [
            ({"color": "W"}, 400, "bad_field"),
            ({"my_color": "X"}, 400, "bad_value"),
            ({"komi": 999}, 400, "bad_value"),
            ({"komi": "7.5"}, 400, "bad_value"),
            ({"rules": "tibetan"}, 400, "bad_value"),
            ({"think_mode": "fast"}, 400, "bad_value"),
            ({"think": 0}, 400, "bad_value"),
            ({"think": 999}, 400, "bad_value"),
            ({"visits": 0}, 400, "bad_value"),
            ({"visits": "many"}, 400, "bad_value"),
            ({}, 400, "bad_request"),
            ({"engine_key": "nope"}, 400, "bad_value"),
        ]
        for body, want_st, want_code in cases:
            st, js = ctx.client.call("POST", "/api/config", body=body,
                                     token=ctx.token)
            check(f"config 非法 {body}", st == want_st
                  and js.get("code") == want_code, f"{st} {js}")
    finally:
        shutdown(ctx)


def test_config_running_409():
    ctx = boot()
    try:
        # 先正常开跑
        st, _ = ctx.client.call("POST", "/api/start", token=ctx.token)
        ctx.state.update(state="running", running=True)
        check("运行中: start 后状态确实 running",
              ctx.state.get("running") is True, repr(ctx.state.snapshot()))
        for body in ({"my_color": "W"}, {"komi": 6.5}, {"rules": "japanese"},
                     {"engine_key": "local:b18c384"},
                     {"my_color": "W", "think": 8}):
            st, js = ctx.client.call("POST", "/api/config", body=body,
                                     token=ctx.token)
            check(f"运行中 config 改 {list(body)} → 409",
                  st == 409 and js.get("code") == "locked", f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/config", body={"think": 8},
                                 token=ctx.token)
        check("运行中 config 改 think → 200",
              st == 200 and js.get("applied") == {"think": 8}, f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/config", body={"visits": 30000},
                                 token=ctx.token)
        check("运行中 config 改 visits → 200",
              st == 200 and js.get("applied") == {"visits": 30000},
              f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/config",
                                 body={"think_mode": "visits"}, token=ctx.token)
        check("运行中 config 改 think_mode → 200",
              st == 200 and js.get("applied") == {"think_mode": "visits"},
              f"{st} {js}")
    finally:
        shutdown(ctx)


def test_start_stop():
    ctx = boot()
    try:
        st, js = ctx.client.call("POST", "/api/start", token=ctx.token)
        check("start: 200 + state=starting",
              st == 200 and js == {"ok": True, "state": "starting"},
              f"{st} {js}")
        check("start: 调到了 FakeApp.control_start",
              ("start",) in ctx.app.calls, repr(ctx.app.calls))
        check("start: 共享状态 running=True",
              ctx.state.get("running") is True
              and ctx.state.get("state") == "starting",
              repr(ctx.state.snapshot()))
        st, js = ctx.client.call("GET", "/api/status", token=ctx.token)
        check("start: status 立刻可见",
              js.get("running") is True and js.get("state") == "starting",
              repr(js))

        st, js = ctx.client.call("POST", "/api/start", token=ctx.token)
        check("start 重复调用幂等",
              st == 200 and js.get("ok") is True
              and js.get("state") in ("starting", "running"), f"{st} {js}")
        check("start 重复调用不会二次启动",
              ctx.app.calls.count(("start",)) == 1, repr(ctx.app.calls))

        st, js = ctx.client.call("POST", "/api/stop", token=ctx.token)
        check("stop: 200", st == 200 and js.get("ok") is True, f"{st} {js}")
        check("stop: 调到了 FakeApp.stop",
              ("stop",) in ctx.app.calls, repr(ctx.app.calls))
        check("stop: 共享状态收工",
              ctx.state.get("running") is False
              and ctx.state.get("state") == "done",
              repr(ctx.state.snapshot()))
        st, js = ctx.client.call("GET", "/api/status", token=ctx.token)
        check("stop: status 立刻变 done",
              js.get("running") is False and js.get("state") == "done",
              repr(js))
        st, js = ctx.client.call("POST", "/api/stop", token=ctx.token)
        check("stop 空闲时幂等", st == 200 and js.get("ok") is True,
              f"{st} {js}")
    finally:
        shutdown(ctx)


def test_start_failure():
    ctx = boot()
    try:
        ctx.app.start_result = {"ok": False, "code": "no_engine",
                                "error": "没有选中引擎"}
        st, js = ctx.client.call("POST", "/api/start", token=ctx.token)
        check("start 失败 no_engine → 409",
              st == 409 and js.get("code") == "no_engine", f"{st} {js}")
        check("start 失败后状态仍是空闲",
              ctx.state.get("running") is False, repr(ctx.state.snapshot()))

        ctx.app.start_result = {"ok": False, "code": "no_fox",
                                "error": "没检测到野狐窗口"}
        st, js = ctx.client.call("POST", "/api/start", token=ctx.token)
        check("start 失败 no_fox → 409",
              st == 409 and js.get("code") == "no_fox", f"{st} {js}")
    finally:
        shutdown(ctx)


def test_pause_resume():
    ctx = boot()
    try:
        st, js = ctx.client.call("POST", "/api/pause", token=ctx.token)
        check("pause 未运行 → 409 not_running",
              st == 409 and js.get("code") == "not_running", f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/resume", token=ctx.token)
        check("resume 未运行 → 409 not_running",
              st == 409 and js.get("code") == "not_running", f"{st} {js}")

        ctx.client.call("POST", "/api/start", token=ctx.token)
        ctx.state.update(state="running", running=True)
        st, js = ctx.client.call("POST", "/api/pause", token=ctx.token)
        check("pause: 200", st == 200 and js.get("ok") is True, f"{st} {js}")
        check("pause: 共享状态 paused",
              ctx.state.get("paused") is True
              and ctx.state.get("state") == "paused",
              repr(ctx.state.snapshot()))
        st, js = ctx.client.call("POST", "/api/pause", token=ctx.token)
        check("pause 重复调用幂等", st == 200 and js.get("ok") is True,
              f"{st} {js}")

        st, js = ctx.client.call("POST", "/api/resume", token=ctx.token)
        check("resume: 200", st == 200 and js.get("ok") is True, f"{st} {js}")
        check("resume: 共享状态恢复",
              ctx.state.get("paused") is False
              and ctx.state.get("state") == "running",
              repr(ctx.state.snapshot()))
        st, js = ctx.client.call("POST", "/api/resume", token=ctx.token)
        check("resume 未暂停时幂等", st == 200 and js.get("ok") is True,
              f"{st} {js}")
    finally:
        shutdown(ctx)


def test_auth():
    ctx = boot()
    try:
        st, js = ctx.client.call("GET", "/api/ping")
        check("鉴权: 没 token → 401",
              st == 401 and js.get("code") == "unauthorized", f"{st} {js}")
        st, js = ctx.client.call("GET", "/api/ping", token="deadbeef" * 4)
        check("鉴权: 错 token → 401",
              st == 401 and js.get("code") == "unauthorized", f"{st} {js}")
        st, js = ctx.client.call("GET", "/api/ping", token=ctx.token)
        check("鉴权: 正确 token → 200", st == 200, f"{st} {js}")
        # 鉴权先于 body 解析：错 token + 坏 body 还是 401
        st, js = ctx.client.call("POST", "/api/config", raw=b"{not json",
                                 token="wrong")
        check("鉴权: 错 token 先于 body 校验", st == 401, f"{st} {js}")
        st, js = ctx.client.call("GET", "/api/status", token="")
        check("鉴权: 空 token → 401", st == 401, f"{st} {js}")
    finally:
        shutdown(ctx)


def test_http_edges():
    ctx = boot()
    try:
        st, js = ctx.client.call("GET", "/api/nope", token=ctx.token)
        check("未知路径 → 404", st == 404 and js.get("code") == "not_found",
              f"{st} {js}")
        st, js = ctx.client.call("GET", "/api/start", token=ctx.token)
        check("GET /api/start → 405",
              st == 405 and js.get("code") == "method_not_allowed",
              f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/ping", token=ctx.token)
        check("POST /api/ping → 405",
              st == 405 and js.get("code") == "method_not_allowed",
              f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/config", raw=b"{not json",
                                 token=ctx.token)
        check("坏 JSON → 400",
              st == 400 and js.get("code") == "bad_request", f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/config", raw=b"[1,2]",
                                 token=ctx.token)
        check("body 不是对象 → 400",
              st == 400 and js.get("code") == "bad_request", f"{st} {js}")
        st, js = ctx.client.call("POST", "/api/config", body={},
                                 token=ctx.token)
        check("空 body → 400",
              st == 400 and js.get("code") == "bad_request", f"{st} {js}")
    finally:
        shutdown(ctx)


def test_token_file():
    # token 目录**故意独立**：后半个用例要复用同一个文件，不能被第一个服务收走
    tokdir = tempfile.mkdtemp(prefix="foxctl-tok-")
    token_file = os.path.join(tokdir, "control_token.txt")
    ctx = boot(same_token_file=token_file)
    try:
        check("token 文件已生成", os.path.isfile(ctx.token_file),
              ctx.token_file)
        with open(ctx.token_file, encoding="utf-8") as f:
            tok = f.read().strip()
        check("token 文件内容 = 服务 token",
              tok == ctx.token and len(tok) >= 16, f"{tok!r}")
        check("token 像随机串（32 位十六进制）",
              len(tok) == 32 and all(c in "0123456789abcdef" for c in tok), tok)
    finally:
        shutdown(ctx)
    # 沿用：同一份 token 文件再起一个服务，token 必须不变
    ctx2 = boot(same_token_file=token_file)
    try:
        with open(token_file, encoding="utf-8") as f:
            tok2 = f.read().strip()
        check("重启沿用已有 token", tok2 == tok, f"{tok2!r} != {tok!r}")
        st, js = ctx2.client.call("GET", "/api/ping", token=tok2)
        check("沿用 token 能通过鉴权", st == 200, f"{st} {js}")
    finally:
        shutdown(ctx2)
        try:
            shutil.rmtree(tokdir, ignore_errors=True)
        except Exception:
            pass


def test_dispatch_direct():
    """不经过 HTTP 直接调 dispatch（方便其它测试/工具复用）"""
    ctx = boot()
    try:
        st, js = ctx.srv.dispatch("GET", "/api/ping", None)
        check("dispatch: 直接调用可用", st == 200 and js.get("version"),
              f"{st} {js}")
        try:
            ctx.srv.dispatch("GET", "/api/xxx", None)
            check("dispatch: 未知路径抛 ControlError", False, "没抛")
        except CS.ControlError as e:
            check("dispatch: 未知路径抛 ControlError",
                  e.status == 404 and e.code == "not_found", repr(e))
    finally:
        shutdown(ctx)


def main():
    print("=" * 60)
    print("control_server 自测（不启动野狐、不开真局）")
    print("=" * 60)
    for fn in (test_ping, test_status, test_engines, test_config_idle,
               test_config_invalid, test_config_running_409, test_start_stop,
               test_start_failure, test_pause_resume, test_auth,
               test_http_edges, test_token_file, test_dispatch_direct):
        print(f"\n-- {fn.__name__}")
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append((fn.__name__, f"异常：{type(e).__name__}: {e}"))
            print(f"  [FAIL] {fn.__name__} 抛异常：{type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name, detail in FAIL:
            print(f"  [x] {name}  {detail}")
        print("结果：FAIL")
        return 1
    print("结果：ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
