# -*- coding: utf-8 -*-
"""
智子云 KataGo 常驻服务
============================================================
一次握手（~9s），之后所有分析都复用同一根连接。

启动：
    python catago_service.py                 # 前台，默认 8765 端口
    python catago_service.py --port 8765 &

接口（全部 POST，JSON）：
    POST /status            引擎状态
    POST /start             确保已启动（幂等）
    POST /position          设置局面 {w,h,turn,stones:[["B","Q16"],...],komi}
    POST /analyze           分析当前局面 {visits, maxmoves}
    POST /live              读 Lizzie 当前局面 + 分析（一步到位）
    POST /genmove           让引擎下一手 {visits}
    POST /sgf               读指定 sgf 并分析
    POST /stop              关闭引擎

命令行直接跑（不起服务）：
    python catago_service.py --once --live --visits 2000
"""
import os, sys, json, time, threading, argparse, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zhizi_cloud as Z


# ---------------------------------------------------------------- 引擎单例

class EngineHolder:
    def __init__(self):
        self.eng = None
        self.lock = threading.Lock()
        self.started_at = None
        self.boot_secs = None
        self.err = None

    def ensure(self, kind=None, weight=None, force=False, extra=None, gpu=None):
        """确保引擎在跑。force=True 时先关掉现有引擎再按新参数重开。

        ⚠️ 为什么需要 force（2026-10-22 实测）
        ----------------------------------------------------
        fox_auto 切换引擎时原先走的是「/stop + 另起一个进程」，
        但 /stop 只关引擎、**HTTP 服务还占着 8765 端口**，
        新进程 bind 失败直接在后台崩掉；前台再连就是 ConnectionResetError。
        正确做法是让这个常驻服务自己换热引擎，进程不重启、端口不改。

        extra：透传给 zz-ikatago 的额外参数，如
               ["--kata-config", "2stones_handicap"]（让子局专用）
        gpu  ：--gpu-type 原值（vip-share / 1x / 3x …）。
               kind 是简写（vip/free/1x），两者需分清。
        """
        with self.lock:
            if force and self.eng:
                try:
                    self.eng.close()
                except Exception:
                    pass
                self.eng = None
            if self.eng and self.eng.alive():
                return self.eng
            try:
                self.eng = Z.CloudEngine(kind=kind, weight=weight,
                                         extra_args=extra or [], gpu=gpu)
                self.eng.start(quiet=True)
                self.started_at = time.time()
                self.boot_secs = self.eng.boot_secs
                self.err = None
            except Exception as e:
                self.err = f"{type(e).__name__}: {e}"
                raise
            return self.eng

    def get(self):
        with self.lock:
            return self.eng if (self.eng and self.eng.alive()) else None

    def close(self):
        with self.lock:
            if self.eng:
                self.eng.close()
                self.eng = None

    def status(self):
        e = self.get()
        st = {
            "ok": True,
            "alive": bool(e),
            "defaultKind": Z.DEFAULT_KIND,
            "defaultWeight": Z.DEFAULT_WEIGHT,
            "err": self.err,
        }
        if e:
            st.update(e.info())
            st["uptimeSecs"] = round(time.time() - (self.started_at or time.time()), 1)
        return st


HOLDER = EngineHolder()
ENG_LOCK = threading.Lock()      # 串行化分析（一个引擎同时只跑一次）


# ---------------------------------------------------------------- 操作实现

def gtp_xy(mv, h=19):
    """GTP 着法 → (x, y)，y=0 是最上面一行。x 用 0..18（跳过 I）"""
    if not mv:
        return None
    s = str(mv).upper().strip()
    if s in ("PASS", ""):
        return None
    col = Z.COLS.find(s[0])
    if col < 0:
        return None
    try:
        row = int(s[1:])
    except ValueError:
        return None
    if not (1 <= row <= h):
        return None
    return col, h - row


def op_start(body):
    """确保引擎在跑；给了 kind/weight/gpu 且与当前不一致时自动换引擎

    body:
      kind, weight   目标引擎参数（可选）
      gpu            --gpu-type 原值（vip-share / 1x / 3x …）
      extra          透传给 zz-ikatago 的额外参数列表
      force          true = 无条件重开（换了权重就必须带上）
    """
    kind = body.get("kind")
    weight = body.get("weight")
    gpu = body.get("gpu")
    extra = body.get("extra") or []
    force = bool(body.get("force"))
    if not force and (kind or weight or gpu):
        cur = HOLDER.get()
        if cur:
            cur_kind = getattr(cur, "kind", None)
            cur_weight = getattr(cur, "weight", None)
            cur_gpu = getattr(cur, "gpu_type", None)
            cur_extra = getattr(cur, "extra_args", []) or []
            want_gpu = gpu or None
            if (kind is None or kind == cur_kind) and \
               (weight is None or weight == cur_weight) and \
               (want_gpu is None or want_gpu == cur_gpu) and \
               (not extra or list(extra) == list(cur_extra)):
                return {"ok": True, "info": cur.info(), "reused": True}
            force = True
    e = HOLDER.ensure(kind=kind, weight=weight, force=force,
                      extra=extra, gpu=gpu)
    return {"ok": True, "info": e.info(), "reused": False}


def op_position(body):
    """设置局面

    body:
      w, h         棋盘尺寸（默认 19）
      komi         贴目（默认 7.5）
      turn         "B" / "W"
      stones       [["B","Q16"], ["W","D4"], ...]   按顺序打
      board        二维数组（可选，和 stones 二选一；死活题用这个）
      rules        "chinese" / "japanese" / ...
    """
    e = HOLDER.ensure()
    w = int(body.get("w") or 19)
    h = int(body.get("h") or 19)
    komi = float(body.get("komi") if body.get("komi") is not None else 7.5)
    turn = (body.get("turn") or "B").upper()
    rules = body.get("rules")

    with ENG_LOCK:
        e.boardsize(w, h)
        if rules:
            e.rules(rules)
        e.komi(komi)
        e.clear()

        n = 0
        if body.get("board"):
            board = body["board"]
            if any(v for row in board for v in row):
                e.set_position(board, turn, w, h)
                n = sum(1 for row in board for v in row if v)
        elif body.get("stones"):
            # ⚠️ 关键：从屏幕识别出来的棋子**没有手顺**，只有当前位置。
            #    早先逐个 play 有两个坑：
            #      1) 顺序是棋盘扫描序，不是真实手顺 → 提子/打劫语义全错
            #      2) turn 靠"最后一手是谁"推断 → 一错就整个视角翻面
            #    所以一律转成二维 board 用 set_position 摆，轮走方由调用方显式给。
            stones = body["stones"]
            grid = [[0] * w for _ in range(h)]
            for item in stones:
                c, mv = item[0], item[1]
                xy = gtp_xy(mv, h)
                if xy is None:
                    continue
                x, y = xy
                if 0 <= x < w and 0 <= y < h:
                    grid[y][x] = 1 if c.upper() == "B" else 2
            if any(v for row in grid for v in row):
                e.set_position(grid, turn, w, h)
                n = sum(1 for row in grid for v in row if v)
        e.komi_val = komi
        e.turn = turn
    return {"ok": True, "w": w, "h": h, "komi": komi, "stones": n, "turn": turn}


def op_analyze(body):
    e = HOLDER.ensure()
    visits = int(body.get("visits") or 2000)
    maxmoves = int(body.get("maxmoves") or 16)
    timeout = float(body.get("timeout") or 240)
    max_secs = body.get("maxSecs")
    max_secs = float(max_secs) if max_secs else None
    # ⚠️ turn 必须显式传（2026-10-23 补）
    # kata-analyze 的 color 参数决定「替谁出主意」，不传默认按 BLACK 算。
    # 老代码只依赖 op_position 里设的 e.turn —— 单次调用碰巧没事，但
    # 万一 /position 和 /analyze 之间插了别的请求（比如另一个客户端
    # 改了局面），拿到的就是别人的轮次。显式传最稳。
    turn = body.get("turn")
    with ENG_LOCK:
        r = e.analyze(visits=visits, maxmoves=maxmoves, timeout=timeout,
                      max_secs=max_secs, turn=turn)
    return r


def op_live(body):
    """读 Lizzie 当前局面 → 灌进云引擎 → 分析"""
    import lizzie_live
    hit = lizzie_live.find_autosave()
    if not hit:
        return {"ok": False, "error": "没找到 Lizzie 自动存档"}
    mtime, path, d = hit
    g = lizzie_live.parse_sgf(path)

    e = HOLDER.ensure()
    komi = body.get("komi")
    if komi is None:
        komi = g.get("komi") or 7.5
    komi = float(komi)

    with ENG_LOCK:
        turn = Z.apply_lizzie(e, g, komi=komi, rules=body.get("rules"))
    out = op_analyze(body)
    out.update({
        "ok": True,
        "sgf": path,
        "sgfAge": round(time.time() - mtime, 1),
        "sgfTime": time.strftime("%H:%M:%S", time.localtime(mtime)),
        "live": {
            "w": g["w"], "h": g["h"], "komi": komi,
            "nMoves": len(g["moves"]),
            "moves": [m["gtp"] for m in g["moves"]],
            "turn": turn,
            "board": g["board"],
            "lizzieCands": g["cands"][:5],
            "comment": g["comment"][:300],
        },
    })
    return out


def op_genmove(body):
    e = HOLDER.ensure()
    color = (body.get("color") or "B").upper()
    visits = int(body.get("visits") or 2000)
    with ENG_LOCK:
        mv = e.genmove(color, visits=visits)
    return {"ok": True, "move": mv, "color": color, "visits": visits}


def op_sgf(body):
    import lizzie_live
    path = body.get("path")
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"sgf 不存在：{path}"}
    g = lizzie_live.parse_sgf(path)
    e = HOLDER.ensure()
    komi = float(body.get("komi") if body.get("komi") is not None else (g.get("komi") or 7.5))
    with ENG_LOCK:
        turn = Z.apply_lizzie(e, g, komi=komi, rules=body.get("rules"))
    out = op_analyze(body)
    out.update({"ok": True, "sgf": path,
                "live": {"w": g["w"], "h": g["h"], "komi": komi,
                         "nMoves": len(g["moves"]), "turn": turn,
                         "moves": [m["gtp"] for m in g["moves"]],
                         "board": g["board"]}})
    return out


def op_stop(body):
    HOLDER.close()
    return {"ok": True, "alive": False}


OPS = {
    "/status": lambda b: HOLDER.status(),
    "/start": op_start,
    "/position": op_position,
    "/analyze": op_analyze,
    "/live": op_live,
    "/genmove": op_genmove,
    "/sgf": op_sgf,
    "/stop": op_stop,
}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._handle({})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        self._handle(body)

    def _handle(self, body):
        path = self.path.split("?")[0].rstrip("/") or "/"
        fn = OPS.get(path)
        if not fn:
            self._json({"ok": False, "error": f"未知接口 {path}", "ops": list(OPS)}, 404)
            return
        t0 = time.time()
        try:
            out = fn(body)
            out.setdefault("ok", True)
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}",
                   "trace": traceback.format_exc()[-1500:]}
        out["tookSecs"] = round(time.time() - t0, 2)
        self._json(out)

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self._cors()
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def run_once(args):
    body = {"visits": args.visits, "maxmoves": args.maxmoves}
    if args.komi is not None:
        body["komi"] = args.komi
    if args.live:
        r = op_live(body)
    elif args.sgf:
        r = op_sgf({"path": args.sgf, **body})
    elif args.stones:
        op_position({"stones": [x.split(":") for x in args.stones],
                     "komi": args.komi if args.komi is not None else 7.5})
        r = op_analyze(body)
    else:
        op_position({"komi": args.komi if args.komi is not None else 7.5})
        r = op_analyze(body)
    HOLDER.close()
    print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:6000])


def main():
    ap = argparse.ArgumentParser(description="智子云 KataGo 常驻服务")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--kind", default=None,
                    help="算力档位简写：vip / free / 1x / 3x …")
    ap.add_argument("--weight", default=None)
    ap.add_argument("--gpu", default=None,
                    help="--gpu-type 原值（vip-share / share / 1x …），优先于 kind")
    ap.add_argument("--extra", nargs="*", default=None,
                    help="透传 zz-ikatago 的额外参数，如 --kata-config 2stones_handicap")
    ap.add_argument("--once", action="store_true", help="跑一次就退出（不起服务）")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sgf", default=None)
    ap.add_argument("--stones", nargs="*", default=None, help="B:Q16 W:D4 ...")
    ap.add_argument("--visits", type=int, default=2000)
    ap.add_argument("--maxmoves", type=int, default=16)
    ap.add_argument("--komi", type=float, default=None)
    ap.add_argument("--preload", action="store_true", help="起服务前先握手")
    args = ap.parse_args()

    def _ensure():
        return HOLDER.ensure(kind=args.kind, weight=args.weight,
                             extra=args.extra or [], gpu=args.gpu)

    if args.once:
        _ensure()
        run_once(args)
        return

    if args.preload:
        print("[预热] 连接智子云算力 …")
        try:
            _ensure()
            print(f"[预热] 就绪 {HOLDER.boot_secs}s　{HOLDER.eng.info().get('model')}")
        except Exception as e:
            print(f"[预热] 失败：{e}")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[服务] http://127.0.0.1:{args.port}")
    print(f"[服务] 默认引擎：{Z.CLOUD_KINDS[Z.DEFAULT_KIND][2]}")
    print(f"[服务] 接口：{', '.join(OPS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        HOLDER.close()


if __name__ == "__main__":
    main()
