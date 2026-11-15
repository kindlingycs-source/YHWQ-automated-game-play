# -*- coding: utf-8 -*-
"""
智子云算力引擎驱动
============================================================
用法：把 `zz-ikatago.exe` 当成一根 SSH 隧道 —— 它吃 GTP 命令，
原样转发到智子云端那台机器上跑的真 KataGo。

实测（2026-10-22）：
    KataGo v1.18.2 / TF3 后端 / 11b768t 权重 / Chinese rules
    第一次握手 7.8s，之后常驻不重连

几个必须知道的点
------------------------------------------------------------
1) 凭据存在 `resources/data/.client`，由 `zz-ikatago save-credential` 写入。
   本机已登录，直接跑就能连上，不用带 /token。
2) `--gpu-type` 决定用哪档算力（见下方 CLOUD_KINDS）。
   vip-share 是 VIP 会员免费共享，不扣时长；1x/3x/6x 按小时扣钱。
3) 云端的 komi、rules 由 GTP 命令控制，跟 Lizzie 界面无关。
4) 死活引擎那套 komi=20 的规则必须用 `kata-set-rules` / `komi` 显式设定，
   云端默认是 Chinese + komi 7.5。
"""
import os, re, json, time, subprocess, threading, atexit

# ---------------------------------------------------------------- 路径
#
# ⚠️⚠️ 这里原来全是写死的 `C:\Users\Administrator\...`（2026-10-23 修）
# ----------------------------------------------------------------------
# 老板把 exe 发给别人用，别人的用户名不叫 Administrator，
# 于是 `IKATAGO` 指向一个不存在的路径 → 云端引擎永远起不来。
# 本机实测"能跑"纯属因为我自己就是 Administrator，属于
# **假阴性**：测了一整天都发现不了，只有拷到别人机器上才炸。
#
# 现在一律从环境变量推：`%LOCALAPPDATA%` 在每台机器上都指向
# 当前用户自己的目录，用户名是什么都不用管。
_la = os.environ.get("LOCALAPPDATA") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local")

# 智子客户端版本号会随升级变化（1.3.0 → 1.4.0 …），所以**不许写死**
# 版本目录，扫一遍 `ikatago_gui_installer/app-*` 取最高的那个。
# 扫不到再退回旧的写死位置（万一有人装的是精简版）。
def _find_lizzie_dir():
    base = os.path.join(_la, "ikatago_gui_installer")
    cands = []
    try:
        for name in os.listdir(base):
            if name.lower().startswith("app-"):
                for sub in ("lizzieyzy", ""):
                    d = os.path.join(base, name, "resources", "data", sub)
                    if os.path.isdir(d):
                        cands.append(d)
    except Exception:
        pass
    # 版本号大的排前面（app-1.10.0 > app-1.3.0，按元组比不能按字符串）
    def _ver(p):
        import re as _re
        m = _re.search(r"app-([\d.]+)", p)
        if not m:
            return (0,)
        return tuple(int(x) for x in m.group(1).split(".") if x.isdigit())
    cands.sort(key=_ver, reverse=True)
    # 优先挑真有 zz-ikatago.exe 的那个
    for d in cands:
        if os.path.isfile(os.path.join(d, "zz-ikatago.exe")):
            return d
    cands.append(os.path.join(base, "app-1.3.0", "resources", "data",
                              "lizzieyzy"))
    return cands[0]


LIZZIE_DIR = _find_lizzie_dir()
IKATAGO = os.path.join(LIZZIE_DIR, "zz-ikatago.exe")

# 本机 KataGo（离线，兜底用）
# ⚠️ 同样是当前用户目录，不是 Administrator
_dl = os.path.join(os.path.expanduser("~"), "Downloads")
LOCAL_KATAGO = os.path.join(_dl, "katago-v1.18.0", "katago.exe")
LOCAL_WEIGHT = os.path.join(
    _dl, "20210116-windows64", "20210116-windows64", "weights",
    "kata1-b28c512nbt-s13255194368-d5935380940.bin.gz")
LOCAL_CFG = os.path.join(_dl, "katago-v1.18.0", "analysis_example.cfg")


# ---------------------------------------------------------------- 算力档位

# 老板 2026-10-22 定的规矩：
#   「之后我叫你连 katago 默认都连这个，这个是正常下棋的，不做死活题」
#   → DEFAULT_KIND = vip，也就是 Lizzie 里那条
#     "智子VIP算力 11b 天梯14374 (fdx)"
#   死活题另有专用引擎，见 kill_all=True 分支（本地 lifego_b18）。
DEFAULT_KIND = "vip"
DEFAULT_WEIGHT = "fdx"

CLOUD_KINDS = {
    # kind: (gpu-type, 默认权重, 显示名, 计费)
    #
    # ⚠️ 显示名 = 智子客户端里的叫法，不带权重/天梯（2026-10-23 老板要求）
    # ----------------------------------------------------
    # 老板原话：
    #   「智子里面的引擎名称就只有智子VIP共享，没有什么权重和天梯分数，
    #     这是我自己配的…你就写简单的引擎名称就行，
    #     对应着用户智子里面的引擎名称就好」
    #
    # 所以这里一律用「智子 + 档位」，与 engine_detect.py 的 tiers 表
    # **逐字一致**（两处名字不一样的话，用户在两个界面上看到同一个引擎
    # 却叫两个名字，会以为是两种东西）。
    # 权重（fdx / 28bnbt…）仍然在元组第 2 位，是**程序参数**，不上界面。
    #
    # ⚠️ vip 不标「免费」：VIP 本身要充值，写免费会误导（2026-10-23 老板纠正）
    "vip":    ("vip-share", "fdx",     "智子VIP共享", "VIP 不限时"),
    "free":   ("share",     "20b",     "智子共享",    "每天1小时·5分钟断"),
    "1x":     ("1x",        "28bnbt",  "智子 1x",     "约 0.8~1.5 元/小时"),
    "3x":     ("3x",        "28bnbt",  "智子 3x",     "2.4 元/小时"),
    "6x":     ("6x",        "28bnbt",  "智子 6x",     "4.8 元/小时"),
    "12x":    ("12x",       "28bnbt",  "智子 12x",    "9.6 元/小时"),
    "24x":    ("24x",       "28bnbt",  "智子 24x",    "19.2 元/小时"),
}

WEIGHTS = ["18bnbt", "28bnbt", "20b", "40b", "fdx", "11b768t", "10b512t", "10b384t"]

COLS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------- GTP 引擎

class CloudEngine:
    """常驻的智子云 KataGo（GTP 协议）

    默认就是老板指定的那台：
        "智子VIP算力 11b 天梯14374 (fdx)"
        = zz-ikatago --gpu-type vip-share --kata-weight fdx
        = KataGo v1.18.2 / TF3 / kata1-tf3-b11c768-s12002M-d6304M

    用法：
        e = CloudEngine()                # 默认 VIP fdx，正常下棋
        e.start()
        e.boardsize(19, 19)
        e.komi(7.5)
        e.play("B", "Q16")
        e.analyze(visits=2000)           # → 候选点列表
        e.close()
    """

    def __init__(self, kind=None, weight=None, name="katago-CUDA", extra_args=None,
                 gpu=None):
        """kind  档位名（vip / 1x / 3x …），查 CLOUD_KINDS 得默认权重与计费说明
        gpu   直接给 --gpu-type 的值，覆盖 kind 查出来的（engines.py 的
              key 里用的就是 gpu-type 原值，如 vip-share，而 CLOUD_KINDS 的
              key 是简写 vip —— 两者对不上，所以留这个口子）
        """
        kind = kind or DEFAULT_KIND
        gpu_def, w_def, desc, bill = CLOUD_KINDS.get(kind, CLOUD_KINDS[DEFAULT_KIND])
        self.gpu_type = gpu or gpu_def
        self.weight = weight or (DEFAULT_WEIGHT if kind == DEFAULT_KIND else w_def)
        self.kind = kind
        # desc 来自档位表，只对该档默认权重成立。显式换了权重（如 VIP 档跑
        # 18bnbt）时必须改写，否则 info 里会显示成 fdx 的档位描述——实测
        # 切到 18bnbt 后 /status 仍报 "11b 天梯14374 (fdx)"，容易看错。
        if weight and weight != (DEFAULT_WEIGHT if kind == DEFAULT_KIND else w_def):
            desc = f"{gpu_def} / {weight}"
        self.desc = desc
        self.billing = bill
        self.kata_name = name
        self.extra_args = extra_args or []

        self.proc = None
        self._lines = []          # [(ts, text)]
        self._lock = threading.Lock()
        self._buf = b""
        self._ready = False
        self._name = None
        self._boot_log = []
        self.w = self.h = 19
        self.komi_val = 7.5
        self.rules_name = "chinese"
        self.turn = "B"          # 当前轮走方，play/clear 里维护

    # ------------------------------------------------ 生命周期

    def _cmdline(self):
        a = [IKATAGO, "--gpu-type", self.gpu_type, "--kata-weight", self.weight]
        if self.kata_name:
            a += ["--kata-name", self.kata_name]
        a += self.extra_args
        return a

    def start(self, timeout=90, quiet=False):
        if self.proc and self.proc.poll() is None:
            return self._ready
        cmd = self._cmdline()
        # ⚠️ 先确认 zz-ikatago.exe 在不在（2026-10-23 为「发给别人用」加的）
        # ----------------------------------------------------
        # 新电脑上没装智子客户端 = 没有这个 exe。
        # 老代码直接 Popen，抛的是
        #     FileNotFoundError: [WinError 2] 系统找不到指定的文件
        # 用户完全看不懂这是「你没装智子客户端」，会以为是软件坏了。
        # 这里给出能照着做的说法，并说清「这只是云端不可用，
        # 本机 kataGo 那条路不受影响」—— 免得他以为整个软件废了。
        if not os.path.isfile(IKATAGO):
            raise RuntimeError(
                "没有找到智子云客户端（zz-ikatago.exe）。\n\n"
                "云端引擎需要先安装「智子围棋」客户端并登录，"
                "装好后这个文件会出现在：\n"
                f"{os.path.dirname(IKATAGO)}\n\n"
                "如果你只想用自己电脑的显卡算，可以：\n"
                "关闭引擎 → 点「引擎设置」→ 勾选「本机 KataGo 权重」里的项目"
                "（需要 katago.exe + 权重文件）。"
            )
        if not quiet:
            print(f"[云引擎] 启动 {' '.join(cmd[1:])}")
            print(f"[云引擎] {self.desc}　计费：{self.billing}")

        # ⚠️ CREATE_NO_WINDOW 必须加（2026-10-27 修）
        # ----------------------------------------------------
        # zz-ikatago.exe 是**控制台程序**。从 GUI 程序（console=False 的
        # PyInstaller exe）里 Popen 它，Windows 会自动为它**新建一个控制台
        # 窗口** —— 那就是老板截图里冒出来的黑窗口。
        # CREATE_NO_WINDOW 给它一个「没有窗口的控制台」，管道读写照常，
        # 只是不显示。
        self.proc = subprocess.Popen(
            cmd, cwd=LIZZIE_DIR,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=0x08000000,       # CREATE_NO_WINDOW
        )
        self._lines = []
        self._buf = b""
        threading.Thread(target=self._reader, daemon=True).start()

        t0 = time.time()
        while time.time() - t0 < timeout:
            if not quiet and time.time() - t0 > 1:
                pass
            if any("GTP ready" in s for _, s in self._lines):
                self._ready = True
                break
            if self.proc.poll() is not None:
                tail = "\n".join(s for _, s in self._lines[-8:])
                raise RuntimeError(f"云引擎启动失败（进程已退出）\n{tail}")
            time.sleep(0.08)
        if not self._ready:
            raise TimeoutError(
                "云引擎握手超时（%.0fs）\n%s"
                % (timeout, "\n".join(s for _, s in self._lines[-10:]))
            )

        # 记录版本
        for _, s in self._lines:
            if "Model name:" in s:
                self._name = s.split("Model name:", 1)[1].strip()
            if s.startswith("KataGo v"):
                self.version = s.strip()
        self.boot_secs = round(time.time() - t0, 1)
        if not quiet:
            print(f"[云引擎] 就绪 {self.boot_secs}s　{getattr(self, 'version', '')}　{self._name or ''}")
        atexit.register(self.close)
        return True

    def _reader(self):
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                break
            self._buf += ch
            if ch == b"\n":
                s = self._buf.decode("utf-8", "replace").rstrip("\r\n")
                self._buf = b""
                with self._lock:
                    self._lines.append((time.time(), s))
                    if len(self._lines) > 4000:
                        del self._lines[:1500]
                if not s.startswith("info"):
                    self._boot_log.append(s)

    def close(self):
        p = self.proc
        if not p:
            return
        try:
            p.stdin.write(b"quit\n")
            p.stdin.flush()
        except Exception:
            pass
        for _ in range(30):
            if p.poll() is not None:
                return
            time.sleep(0.1)
        try:
            p.kill()
        except Exception:
            pass

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    # ------------------------------------------------ 收发

    def send(self, cmd, timeout=60):
        """发一条命令，返回它之后的第一条 `=` 应答（多行应答拼成一段）

        ⚠️ 快照必须在 write 之前取（2026-10-22 修，同 local_engine 那个坑）
        ----------------------------------------------------
        先 write 再取 idx 的话，引擎的瞬时应答（komi/name/clear_board）
        可能已经进了 _lines，被快照圈掉 → 死等到超时。
        云端经 SSH 隧道，往返 30~100ms，看起来「慢」掩盖了竞态，
        但一旦网络抖动把应答提前塞进来就会炸。
        """
        if not self.alive():
            raise RuntimeError("云引擎未运行")
        idx = len(self._get_lines())        # ← 必须在 write 之前
        self.proc.stdin.write((cmd + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        return self._read_reply(timeout, start=idx)

    def _read_reply(self, timeout=60, start=None):
        t0 = time.time()
        idx = len(self._get_lines()) if start is None else start
        got = []
        started = False
        while time.time() - t0 < timeout:
            ls = self._get_lines()
            while idx < len(ls):
                _, s = ls[idx]
                idx += 1
                if not started:
                    if s.startswith("info"):
                        continue
                    if s.startswith("=") or s.startswith("?"):
                        started = True
                        got.append(s[1:].strip())
                        if got[-1] == "":
                            return ""
                        continue
                    # 非应答行（引擎日志），跳过
                    continue
                else:
                    if s == "":
                        return "\n".join(x for x in got if x).strip()
                    got.append(s.strip())
            time.sleep(0.01)
        raise TimeoutError(f"GTP 应答超时：{got[:3]}")

    def _get_lines(self):
        with self._lock:
            return list(self._lines)

    # ------------------------------------------------ 局面设置

    def boardsize(self, w, h=None):
        h = h or w
        self.send(f"boardsize {w} {h}" if w != h else f"boardsize {w}")
        self.w, self.h = w, h

    def komi(self, k):
        self.send(f"komi {k}")
        self.komi_val = k

    def rules(self, r="chinese"):
        self.send(f"kata-set-rules {r}")
        self.rules_name = r

    def clear(self):
        self.send("clear_board")
        self.turn = "B"

    def play(self, color, gtp):
        """走一手。同时维护 self.turn —— 这是唯一可靠的轮次依据。

        别指望引擎的 kata-analyze 自己报轮次：它不报，只按内部状态给值。
        """
        if gtp and gtp.lower() != "pass":
            self.send(f"play {color} {gtp}")
            self.turn = "W" if color.upper() == "B" else "B"
        else:
            # pass 也算换手
            self.turn = "W" if color.upper() == "B" else "B"

    def undo(self):
        self.send("undo")
        self.turn = "W" if self.turn == "B" else "B"

    def set_position(self, board, color, w, h):
        """用 `set_position` 直接摆出任意局面（含死活题的预置棋子）

        board: 二维数组 [[0,1,2...]]，1=黑 2=白，下标 [y][x]，y=0 是最上面一行
        color: "B" / "W"，轮到谁走

        ⚠️ 语法坑（2026-10-22 实测，血亏）
        ----------------------------------------------------
        正确：`set_position B D4 W D16`     ← 纯 <COLOR> <VERTEX> 成对序列
        错误：`set_position W black D4 white D16`
              → "Expected a space-separated sequence of <COLOR> <VERTEX>
                 pairs but got 'W black D4 white D16'"

        早先版本按「首参数=轮走方，然后 black/white 分段」写的，
        语法错误被引擎用一行报错顶回来（返回空串，不 raise），
        结果**盘面从来没被真正设置过** —— 引擎一直在分析上一次的局面，
        于是空盘和黑105:白15 两种盘面都返回同一个「白 33.5% / R16」。
        这个 bug 直接导致本机 B28 与云端 B11 结论打架，查了很久。

        `set_position` 没有单独的轮走方参数，轮次按「最后一手的对手方」推：
        白子多走一手就轮黑。要精确控制轮次，用 `set_position` 后
        再 `play` 一个 pass 是不行的 —— 所以这里按最后一手推断并写进 self.turn。

        ⚠️ 路数变了必须先 boardsize（2026-10-23 支持 2~19 路）
        ----------------------------------------------------
        引擎内部的盘面尺寸不会自己跟着 set_position 变：19 路引擎里摆 9 路
        的坐标（比如 E5 = 第 5 列），它会按 19 路理解成别的格子，甚至直接
        判非法。所以 w/h 跟当前引擎尺寸不一致时，先发 boardsize。
        """
        if (getattr(self, "w", None), getattr(self, "h", None)) != (w, h):
            self.boardsize(w, h)
            self.send("clear_board", timeout=30)

        pairs = []
        for y in range(h):
            for x in range(w):
                v = board[y][x]
                if not v:
                    continue
                c = "B" if v == 1 else "W"
                pairs.append(f"{c} {COLS[x]}{h - y}")

        if not pairs:
            self.send("clear_board", timeout=30)
        else:
            resp = self.send("set_position " + " ".join(pairs), timeout=60)
            # ⚠️ 任何非空应答都是失败，不能只认 "Expected"（2026-10-22 血亏）
            # ----------------------------------------------------
            # 摆了一个「黑天元 1 子 + 白铺满左上」的盘面，云端回的是
            #   "Illegal stone placements - overlapping stones or no liberties?"
            # 老代码只查 "Expected"，于是**静默通过**，
            # showboard 一看还是空盘 —— 后续所有分析都在空盘上做的。
            # 这就是「云引擎落子水平很低、不像围棋 AI」的直接原因之一。
            if resp:
                raise RuntimeError(f"set_position 被拒：{resp}")

        # ⚠️ 轮次：set_position / clear_board 之后引擎**恒认 Black 先**
        # ----------------------------------------------------
        # 实测（2026-10-22）：把黑子排最后、白子排最后，showboard 都是
        # `Next player: Black` —— set_position 没有轮走方参数。
        # 要白先只能补一手 pass。实测 `play B pass` 后 MoveNum 变 1，
        # 但胜率不受污染（空盘黑先 0.3372 → pass 后 0.3325），可以放心用。
        turn = (color or "B").upper()
        if turn == "W":
            self.send("play B pass", timeout=30)
            got = self._next_player()
            if got and got != "W":
                raise RuntimeError(f"摆盘后轮次不符：要 W，引擎是 {got}")
        self.w, self.h = w, h
        self.turn = turn

    def _next_player(self):
        """showboard 里那行 `Next player: Black/White` —— 唯一的轮次真相来源"""
        sb = self.send("showboard", timeout=20)
        if not sb:
            return None
        low = sb.lower()
        if "next player: black" in low:
            return "B"
        if "next player: white" in low:
            return "W"
        return None

    def set_param(self, key, val):
        self.send(f"kata-set-param {key} {val}")
        # 标记「管道里可能有余留应答」，让下一次 analyze 决定要不要冲
        self._need_barrier2 = True

    def list_params(self):
        return self.send("kata-list-params", timeout=20)

    # ------------------------------------------------ 分析

    def _barrier(self, timeout=10):
        """同步屏障：保证管道里没有上一轮分析的残留

        为什么必须有这个
        ----------------------------------------------------
        `analyze()` 开头取 `start_idx = len(lines)` 当作「从这行开始算我的」。
        但读线程和主线程是并发的：上一次 kata-analyze 被终止后，引擎可能
        还在往 stdout 吐 `info ...`。如果这些行在取 start_idx **之后**才被
        读线程收进 self._lines，就会被当成这一次的结果。

        实测后果（2026-10-22 抓到）：同一进程内连续分析两个不同轮次的
        同一盘面，第二次拿到的 best 是第一轮残留的着法，胜率也串了。
        表现就是「白先 33.5%、黑先 68%」这种自相矛盾的数字。
        上次野狐输棋的根子也在这里 —— 视角翻转本身没错，是数据源串了。

        做法：发一个无副作用的轻命令并等应答，逼引擎把先前输出全部冲出来。

        ⚠️ 应答后面的 sleep 是纯浪费（2026-10-23 为降低落子延迟去掉）
        ----------------------------------------------------
        `send("name")` 的语义是「读到 `=` 应答为止」，那一刻管道里该出的
        东西**已经**被读线程收进 self._lines 了。原先后面再 sleep(0.05)
        只是心理安慰，实测每次白扔 50ms —— 按老板「延迟尽量小」的要求删掉。
        真正防止串数据的仍是 send 本身的有序屏障。
        """
        self.send("name", timeout=timeout)

    def analyze(self, visits=1000, maxmoves=16, include_own=False,
                ownership=False, timeout=180, interval=None, max_secs=None,
                turn=None, stop_at_visits=None):
        """跑一次分析，返回结构化候选点

        max_secs：硬时间上限。到了就用手上已有的结果收工 ——
            实战对弈必须设，否则共享算力一卡就是 30s，对手读秒会急。
        turn    ：轮走方（"B"/"W"）。**必须传**，见下面两条教训。
        interval：info 刷新间隔，**单位是厘秒**，见下。

        返回：
            {
              "kind": "vip", "weight": "fdx", "model": "...",
              "visits": [请求值], "bestVisits": 实际最高访问量,
              "elapsed": 秒, "moves": [ {...}, ... ],
              "best": {...}, "ownership": [...]（可选）
            }

        ⚠️ 教训一：`kata-analyze` 的 color 必须显式传（2026-10-22 实测）
        ----------------------------------------------------
        `kata-analyze <color>` 的 color = **给谁出主意**。
        不传时 KataGo 默认按 **B（黑方）** 分析。实测（空盘，同一进程）：
            不传 color → 首选 R16  winrate 0.3372
            传 B       → 首选 R16  winrate 0.3369   ← 完全一样
            传 W       → 首选 C4   winrate 0.9965   ← 完全不同
        老代码不传 color，于是**轮到白走时引擎在替黑方出主意**，
        推荐的全是黑方最佳应手 —— 落子看着「不像围棋 AI」就是这个。

        ⚠️ 教训二：interval 单位是**厘秒**（2026-10-22 实测）
        ----------------------------------------------------
        老代码写死 interval=200，那是 **2 秒**才吐一轮 info。
        预算 1~2 秒时一帧都收不到 → 上层判定「没有可下点」→ 不下棋。
        （老板报的「调 1 秒或 2 秒就不动，必须 3 秒以上」就是这个）
        实测首帧到达时间：interval 200→2.14s，100→1.08s，20→0.29s，5→0.15s。
        所以默认改成按预算动态算，且上限不超过 50 厘秒（0.5s）。
        """
        self._barrier()                 # 先把上一轮的残留冲干净
        # ⚠️ 访问量参数只在变化时才发（2026-10-23 为「落子延迟尽量少」优化）
        # 每手都发 maxVisits + maxPlayouts 是两次 GTP 往返 ≈ 0.14s，
        # 而实战中这两个值几乎从不变化（GUI 固定 100000）。
        # 记一下上次发过的值，一样就跳过。
        if getattr(self, "_visits_sent", None) != visits:
            self.set_param("maxVisits", visits)
            if maxmoves:
                self.set_param("maxPlayouts", visits)
            self._visits_sent = visits
        # 只有真发过 set_param 才需要第二次 barrier 冲应答；
        # 缓存命中时管道里除了上一步的 `=` 应答什么都没有，冲也是白冲。
        if getattr(self, "_need_barrier2", False):
            self._barrier()
            self._need_barrier2 = False
        t0 = time.time()

        side = (turn or self.turn or "B").upper()

        # 动态 interval：预算紧就勤报，预算宽也别超过 0.5s 一轮
        if interval is None:
            budget = max_secs if max_secs else timeout
            interval = int(max(5, min(50, budget * 100 / 6)))

        extra = []
        if include_own:
            extra.append("ownership")
        cmd = "kata-analyze %s interval %d%s" % (
            side, interval, (" " + " ".join(extra)) if extra else "")
        self.proc.stdin.write((cmd + "\n").encode())
        self.proc.stdin.flush()

        start_idx = len(self._get_lines())
        frames = {}
        maxv = 0
        last_growth = time.time()

        hard_deadline = (t0 + max_secs) if max_secs else None
        # ⚠️ 「一个候选都没有」时的兜底（2026-10-23 加）
        # ----------------------------------------------------
        # 上面把收工条件放宽成 `frames` 非空，但万一引擎真的一帧都没吐
        # （共享算力排队、连接假活、权重加载慢），循环会一路转到
        # timeout=180s —— 老板那边就是「卡了三分钟一手不动」。
        # 不能只靠 timeout：那是给非对弈场景（人工分析）留的宽松值。
        #
        # 对弈场景给「预算 + 6 秒」的宽限：正常情况下首帧 0.3~2 秒就到，
        # 6 秒还没动静基本可以判定这一轮废了，早点返回让上层重试/提示，
        # 比闷头等 180 秒有用得多。
        # （max_secs=None 时不设此上限，保持原来的行为 —— 那是分析用途）
        nomove_deadline = (t0 + max_secs + 6.0) if max_secs else None
        while time.time() - t0 < timeout:
            ls = self._get_lines()
            changed = False
            while start_idx < len(ls):
                _, s = ls[start_idx]
                start_idx += 1
                if not s.startswith("info"):
                    continue
                changed = True
                for d in parse_infos(s):
                    mv = d.get("move")
                    if not mv:
                        continue
                    old = frames.get(mv)
                    if old is None or (d.get("visits") or 0) >= (old.get("visits") or 0):
                        frames[mv] = d
            if changed:
                nv = max((f.get("visits") or 0) for f in frames.values()) if frames else 0
                if nv > maxv:
                    maxv = nv
                    last_growth = time.time()
                # 按计算量模式：访问量到了就收，**不看候选数**
                # （kata-analyze 没有按访问量停的参数，只能在这里截；
                #   候选不足 6 个的局面以前会一直等到 max_secs）
                if stop_at_visits and maxv >= stop_at_visits:
                    break
                # 够用就收：拿到至少 6 个候选且已达目标访问量
                if len(frames) >= 6 and maxv >= visits * 0.98:
                    break
            elif frames and time.time() - last_growth > 3.0:
                break                       # 访问量不再涨 = 算完了
            # 硬上限：到点就收工
            #
            # ⚠️ 这里原来还要求 `len(frames) >= 2`（2026-10-23 老板纠正）
            # ----------------------------------------------------
            # 老代码：到时间了，但「候选不到 2 个」就继续算，一路算到
            # timeout=180s。老板设固定 5 秒，实际每手 7.3~11.6 秒 ——
            # 日志能对上（logs/fox_play.log），5 秒形同虚设。
            #
            # 老板原话：
            #   「候选点数量没有太大意义，有时候几十个点都可以，
            #     有时候比如被打吃时，只能这1个点」
            # 他说得对，而且这句话正好点出这个条件的**逻辑错误**：
            # 候选数少不代表引擎没算好，恰恰代表局面是**被迫的**
            # （打吃、单关、只有一路可走）—— 这时候唯一那个点就是
            # 唯一合法的点，再等 175 秒也不会多出第二个候选。
            # 用「候选数」当收工门槛，等于在**最简单**的局面里罚最久。
            #
            # 所以改成：只要拿到**任何一个**候选就到点收工。
            # 一个都没有（frames 空）才继续等 —— 那是真的还没吐出结果，
            # 不是「局面被迫」，等下去有意义。
            if hard_deadline and time.time() >= hard_deadline and frames:
                break
            # 一帧都没吐出来 → 不能死等 timeout（见 nomove_deadline 的说明）
            if nomove_deadline and not frames and time.time() >= nomove_deadline:
                break
            time.sleep(0.05)

        # 终止流
        try:
            self.proc.stdin.write(b"\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        # 空行实测 +0.065s 就到（2026-10-23 实测），给 0.5s 足够。
        # 老值 1.2s 是「万一」的保险，代价是每手白扔最多 1.2 秒。
        self._drain_blank(timeout=0.5)

        lst = sorted(frames.values(),
                     key=lambda x: (x.get("order", 99), -(x.get("visits") or 0)))
        # 换算成白方视角（跟随 color 参数，见 _recolor 的说明）
        out = self._recolor(lst[:maxmoves], turn=side)

        return {
            "kind": self.kind, "weight": self.weight,
            "gpu": self.gpu_type, "model": self._name,
            "requestedVisits": visits,
            "bestVisits": maxv,
            "elapsed": round(time.time() - t0, 2),
            "moves": out,
            "best": out[0] if out else None,
            "komi": self.komi_val, "rules": self.rules_name,
            "w": self.w, "h": self.h,
            "turn": side,                      # 本次分析用的 color
            "winratePerspective": "white",     # 已统一换算
        }

    def _recolor(self, moves, turn=None):
        """把 kata-analyze 的原始 winrate 统一换算成「白方视角」

        ⚠️ 视角**跟随 kata-analyze 的 color 参数**，不是固定 BLACK
        ----------------------------------------------------
        2026-10-22 用空盘在同一进程内做了三组对照（数字见下），
        推翻了之前「云端写死 BLACK」的判断：

            kata-analyze 不传 color → 首选 R16  winrate 0.3372
            kata-analyze B          → 首选 R16  winrate 0.3369
            kata-analyze W          → 首选 C4   winrate 0.9965

        「不传」与「传 B」逐位一致 → **默认就是 B**。
        所以真正的语义是：info 里的 winrate = **color 那一方的胜率**
        （与本机 GTP 版完全一致，本机早已用 228:0 的盘面钉死这条）。

        早先误判成「固定 BLACK」，是因为当时 set_position 静默失败
        （把无气的黑子摆进去，云端回 "Illegal stone placements"，
        而代码只查 "Expected"），引擎一直在分析**空盘**，
        于是不同盘面都返回同一个 0.337 / R16 —— 看起来像"固定视角"。

        现在：turn 给了就按 turn 判定，没给就退回 previously 的行为（当黑）。
        """
        if not moves:
            return []
        side = (turn or self.turn or "B").upper()
        to_white = (side == "B")
        out = []
        for d in moves:
            d = dict(d)
            wr = d.get("winrate")
            sl = d.get("scoreLead")
            d["winrateRaw"] = wr          # 原始值备查
            d["sideToMove"] = side
            if wr is not None and to_white:
                d["winrate"] = 1.0 - wr   # 黑方胜率 → 白方
            if sl is not None:
                d["scoreLeadRaw"] = sl
                if to_white:
                    d["scoreLead"] = -sl
                    if d.get("scoreMean") is not None:
                        d["scoreMean"] = -d["scoreMean"]
            d["perspective"] = "W"
            out.append(d)
        # ⚠️ 绝不按 winrate 重排（2026-10-22 实测）
        # 一条 info 里的候选 winrate 全是**根节点值、完全相同**，
        # 真正的推荐顺序只在 `order` 字段。排序不但无用，还会
        # 把 order=0 的首选挤下去（翻面后 1-x 让大小关系反转）。
        return out

    def _drain_blank(self, timeout=1.2):
        """吃掉分析终止后的残余应答（一串空行 / = 行）

        ⚠️ 这里的时间上限必须短（2026-10-22 实测）
        ----------------------------------------------------
        原先 timeout=5，且循环里 `time.time() - t0 > 1.2 and blanks == 0`
        才肯退出。实战测出来：分析本身 2.6s 就收工了，却被这个函数
        又拖了 5s，单手总耗时变成 8s —— 老板读秒时这一段纯属浪费。

        实际上我们**不需要**把残余应答读干净：`_barrier()` 下一轮开头
        会发 `name` 命令把管道冲一遍，比在这里干等可靠得多。
        所以这里只等 1.2s，主要作用是别让残余行污染紧接着的读取。
        """
        t0 = time.time()
        idx = len(self._get_lines())
        while time.time() - t0 < timeout:
            ls = self._get_lines()
            while idx < len(ls):
                _, s = ls[idx]
                idx += 1
                if s == "":
                    return
            time.sleep(0.02)

    # ------------------------------------------------ 便捷

    def genmove(self, color, visits=1000):
        self.set_param("maxVisits", visits)
        return self.send(f"genmove {color}", timeout=180)

    def info(self):
        return {
            "kind": self.kind, "desc": self.desc, "billing": self.billing,
            "gpu": self.gpu_type, "weight": self.weight,
            "version": getattr(self, "version", None),
            "model": self._name, "alive": self.alive(),
            "bootSecs": getattr(self, "boot_secs", None),
            "w": self.w, "h": self.h, "komi": self.komi_val,
        }


# ---------------------------------------------------------------- 解析

def parse_infos(line):
    """一行里可能拼了多个 info 段（引擎习惯），必须切开逐个解析"""
    s = line.rstrip("\r\n")
    out = []
    for seg in re.split(r"\s+(?=info\s)", s):
        seg = seg.strip()
        if not seg.startswith("info"):
            continue
        d = parse_info(seg)
        if d.get("move"):
            out.append(d)
    return out


def parse_info(line):
    d = {}
    for key, pat, cast in (
        ("move",       r"move\s+([A-Z]\d+|pass)",        str),
        ("visits",     r"(?:^|\s)visits\s+(\d+)",        int),
        ("edgeVisits", r"edgeVisits\s+(\d+)",            int),
        ("utility",    r"utility\s+(-?[\d.eE+-]+)",      float),
        ("winrate",    r"winrate\s+(-?[\d.eE+-]+)",      float),
        ("scoreMean",  r"scoreMean\s+(-?[\d.eE+-]+)",    float),
        ("scoreLead",  r"scoreLead\s+(-?[\d.eE+-]+)",    float),
        ("scoreStdev", r"scoreStdev\s+(-?[\d.eE+-]+)",   float),
        ("prior",      r"prior\s+(-?[\d.eE+-]+)",        float),
        ("lcb",        r"(?:^|\s)lcb\s+(-?[\d.eE+-]+)",  float),
        ("order",      r"order\s+(\d+)",                 int),
        ("weight",     r"(?:^|\s)weight\s+(-?[\d.eE+-]+)", float),
    ):
        m = re.search(pat, line)
        if m:
            try:
                d[key] = cast(m.group(1))
            except ValueError:
                pass
    m = re.search(r"\bpv((?:\s+(?:[A-Za-z]\d+|pass))+)", line)
    if m:
        d["pv"] = m.group(1).split()[:24]
    return d


# ---------------------------------------------------------------- 从 Lizzie 局面 → 引擎局面

def apply_lizzie(cg, g, komi=None, rules=None):
    """把 lizzie_live.parse_sgf() 的结果灌进云引擎

    死活题必须显式给 rules/komi，否则云端默认 chinese + 7.5。
    """
    w, h = g["w"], g["h"]
    k = komi if komi is not None else (g.get("komi") or 7.5)
    cg.boardsize(w, h)
    cg.rules(rules or "chinese")
    cg.komi(k)
    cg.clear()

    board = g["board"]
    n = len(g.get("moves") or [])
    # 有手顺就按手顺走（保留劫 / 提子语义），没手顺就 set_position
    if n:
        for m in g["moves"]:
            cg.play(m["color"], m["gtp"])
        turn = "W" if g["moves"][-1]["color"] == "B" else "B"
    else:
        # 空盘或纯预置棋子 → set_position
        has_stone = any(v for row in board for v in row)
        if has_stone:
            cg.set_position(board, "B", w, h)
        turn = "B"
    return turn


# ---------------------------------------------------------------- CLI

def _fmt_pct(x):
    return "—" if x is None else f"{x*100:.2f}%"


def main():
    import argparse
    ap = argparse.ArgumentParser(description="智子云算力 KataGo")
    ap.add_argument("--kind", default="vip", choices=list(CLOUD_KINDS),
                    help="算力档位（默认 vip）")
    ap.add_argument("--weight", default=None, help="权重，如 fdx / 28bnbt / 18bnbt")
    ap.add_argument("--visits", type=int, default=1000)
    ap.add_argument("--live", action="store_true", help="读 Lizzie 当前局面")
    ap.add_argument("--sgf", default=None, help="读指定 sgf")
    ap.add_argument("--komi", type=float, default=None)
    ap.add_argument("--test", action="store_true", help="跑一个标准自检局面")
    args = ap.parse_args()

    e = CloudEngine(kind=args.kind, weight=args.weight)
    e.start()
    print(json.dumps(e.info(), ensure_ascii=False, indent=1))
    print()

    try:
        if args.test:
            e.boardsize(19)
            e.komi(7.5)
            for c, m in [("B", "Q16"), ("W", "D4"), ("B", "Q4"), ("W", "D16")]:
                e.play(c, m)
        elif args.live or args.sgf:
            import lizzie_live
            if args.sgf:
                g = lizzie_live.parse_sgf(args.sgf)
            else:
                hit = lizzie_live.find_autosave()
                if not hit:
                    print("没找到 Lizzie 自动存档")
                    return
                g = lizzie_live.parse_sgf(hit[1])
            lizzie_live.show(g)
            print()
            turn = apply_lizzie(e, g, komi=args.komi)
            print(f"[已灌入云引擎] {g['w']}x{g['h']} komi={e.komi_val} 轮到 {turn}")
        else:
            e.boardsize(19); e.komi(7.5)

        t0 = time.time()
        r = e.analyze(visits=args.visits)
        print(f"\n=== 云分析 {r['elapsed']}s　最高访问 {r['bestVisits']}　{r['model']} ===")
        for d in r["moves"][:8]:
            pv = " ".join(d.get("pv", [])[:8])
            print(f"  {d['move']:>4}  win={_fmt_pct(d.get('winrate')):>8}"
                  f"  lead={d.get('scoreLead', 0):+6.2f}"
                  f"  v={d.get('visits', 0):>6}  {pv}")
    finally:
        e.close()


if __name__ == "__main__":
    main()
