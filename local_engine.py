# -*- coding: utf-8 -*-
"""
本机 KataGo 引擎（JSON analysis 协议）
============================================================
为什么用它而不是云端 zz-ikatago：
    实测同一局面对比（2026-10-22，RTX 5070 Laptop）
        本机 b28c512nbt  800v → 0.84s      4000v → 4.8s
        云端 11b768t   1200v → 7~34s（共享算力波动）
    本机快一个数量级，权重还更大，而且不花时长。

关键设计
------------------------------------------------------------
1) 常驻进程。KataGo 每次启动要 7s 做 OpenCL 调优，绝不能每手重启。
2) 分析请求是「一次一个局面」的 JSON 行协议，等 `isDuringSearch == False`
   的那条才是最终结果 —— 中途会推多条中间更新，早收会拿到半成品。
3) 引擎用 `moves`（手顺）还是 `initialStones`（摆盘）：
   · 有完整手顺 → 传 moves，提子/打劫语义准确
   · 只有盘面截图（没手顺）→ 传 initialStones + 显式 initialPlayer
   两者都能指定 `initialPlayer`，所以轮次永远不会猜错。
"""
import os, re, json, time, subprocess, threading, atexit

# ---------------------------------------------------------------- 路径
#
# ⚠️⚠️ 这里原来是写死的 `C:\Users\Administrator\...`（2026-10-23 修）
# ----------------------------------------------------------------------
# 分发级 bug：别人的用户名不是 Administrator，`KATAGO` 指向不存在的
# 文件 → 本机引擎永远起不来。
# 而且它**不会崩**：`start()` 里我加的友好提示会弹「找不到 katago.exe」，
# 看起来像"用户没装"，实际是**程序自己找错了地方**。
# 这是最难查的一类 bug —— 报错信息把人指向错误的原因。
#
# 现在：`%USERPROFILE%` 推当前用户，KATAGO / 权重目录都跟着走。
_user_home = os.path.expanduser("~")
_dl = os.path.join(_user_home, "Downloads")

# 默认指向老板实际在用的 LizzieYzy Next（B11 在里面）
# 别人的机器上这个目录不存在 → 相关兜底路径自动跳过，不影响主流程
LIZZIE_NEXT = (r"D:\WEIXIN\xwechat_files\wxid_1fppd180iksb22_6818\msg\file"
               r"\2026-09\2026-09-03-windows64.without.engine.portable\LizzieYzy Next")

# 当前用户的常见 katago 安装位置（第一个存在的算默认）
KATAGO = os.path.join(_dl, "katago-v1.18.0", "katago.exe")
KATAGO_CWD = os.path.join(_dl, "katago-v1.18.0")

WEIGHT_DIR = os.path.join(_dl, "20210116-windows64", "20210116-windows64",
                          "weights")


def _find_katago():
    """按常见位置找 katago.exe，找到第一个就返回 (exe, 所在目录)

    ⚠️ 为什么要有这个（2026-10-23）
    ----------------------------------------------------
    光把常量改成 `%USERPROFILE%\\Downloads\\...` 还不够 ——
    别人可能装在 `C:\\Program Files\\KataGo` 或桌面。
    这里按命中概率再过一遍，并复用 engine_detect 的候选清单
    （它已经维护了那些位置，别在两处各写一份，早晚会漂移）。
    """
    try:
        import engine_detect as ED
        for p in ED._katago_candidates():
            if p and os.path.isfile(p):
                return p, os.path.dirname(p)
    except Exception:
        pass
    return KATAGO, KATAGO_CWD

import runtime_paths as RP

# ⚠️ engine/analysis_fast.cfg 是**随包资源**，打包后要从 _MEIPASS 找，
#    所以这里用 RP.HERE 而不是 __file__ 的目录。
FAST_CFG = os.path.join(RP.HERE, "engine", "analysis_fast.cfg")

COLS = "ABCDEFGHJKLMNOPQRST"

# 老路径下的四份权重（兜底用）
WEIGHTS = {
    "b28": ("kata1-b28c512nbt-s13255194368-d5935380940.bin.gz",
            "b28c512nbt 最新 28B", 271),
    "b18": ("b18c384nbt-humanv0.bin.gz", "b18c384nbt 人类棋风", 99),
    "b40": ("kata1-b40c256-s5600995072-d1348643660.bin.gz", "b40c256 老版 40B", 174),
    "b20": ("kata20bs530.bin.gz", "b20 老版", 87),
}


# ---------------------------------------------------------------- info 行解析

_INFO_PATTERNS = (
    ("move",       r"move\s+([A-Z]\d+|pass)",         str),
    ("visits",     r"(?:^|\s)visits\s+(\d+)",         int),
    ("winrate",    r"winrate\s+(-?[\d.eE+-]+)",       float),
    ("scoreMean",  r"scoreMean\s+(-?[\d.eE+-]+)",     float),
    ("scoreLead",  r"scoreLead\s+(-?[\d.eE+-]+)",     float),
    ("prior",      r"prior\s+(-?[\d.eE+-]+)",         float),
    ("order",      r"order\s+(\d+)",                  int),
    ("lcb",        r"(?:^|\s)lcb\s+(-?[\d.eE+-]+)",   float),
)


def _parse_infos(line):
    """一行里可能拼了多个 info 段（引擎习惯），必须切开逐个解析"""
    s = line.rstrip("\r\n")
    out = []
    for seg in re.split(r"\s+(?=info\s)", s):
        seg = seg.strip()
        if not seg.startswith("info"):
            continue
        d = {}
        for key, pat, cast in _INFO_PATTERNS:
            m = re.search(pat, seg)
            if m:
                try:
                    d[key] = cast(m.group(1))
                except ValueError:
                    pass
        if d.get("move"):
            m = re.search(r"\bpv((?:\s+(?:[A-Za-z]\d+|pass))+)", seg)
            if m:
                d["pv"] = m.group(1).split()[:24]
            out.append(d)
    return out


def weight_path(key="b28"):
    fn, _, _ = WEIGHTS.get(key, WEIGHTS["b28"])
    return os.path.join(WEIGHT_DIR, fn)


# ---------------------------------------------------------------- 引擎

class LocalEngine:
    """常驻本机 KataGo（analysis JSON 协议）

    用法：
        e = LocalEngine(weight="b28")
        e.start()
        r = e.analyze(board=grid, turn="W", komi=7.5, visits=2000)
        e.close()
    """

    def __init__(self, weight="b28", cfg=None, threads=None, batch=None,
                 model=None, exe=None, cwd=None, komi=None, label=None,
                 gtp=False, extra_args=None):
        """两种构造方式

        1) 老接口：LocalEngine(weight="b28") —— 查老目录的四份权重，analysis 模式
        2) 注册表接口：LocalEngine(model=..., cfg=..., exe=..., gtp=True)
           —— 直接用 engines.py 从 Lizzie config 里抠出来的路径。

        ⚠️ 为什么必须支持 gtp 模式（2026-10-22 实测，卡了半小时）
        ----------------------------------------------------
        Lizzie 自带的那几个 cfg（default_gtp.cfg / gtp_aggressive_dynamic.cfg）
        是**给 GTP 模式**写的，里面没有 `numAnalysisThreads` 这个键。
        拿它们去起 `katago analysis` 会直接崩：

            Could not find key 'numAnalysisThreads' in config file ...

        而自己写的 analysis_fast.cfg 反过来又缺 GTP 需要的东西。
        所以：注册表里的引擎一律按 **GTP** 起（跟 Lizzie 里怎么起就怎么起），
        只有老的 weight= 接口才用 analysis 模式。
        """
        self.weight_key = label or weight
        self.komi_default = komi
        self.gtp_mode = bool(gtp)
        self.extra_args = list(extra_args or [])
        # ⚠️ exe 没指定时要**现场找**，不能直接用常量（2026-10-23）
        # 常量只是 %USERPROFILE%\Downloads\katago-v1.18.0\ 这个猜测，
        # 别人可能装在 Program Files / 桌面 / 任意目录。
        _exe_found, _cwd_found = (exe, cwd) if (exe and cwd) else (None, None)
        if not _exe_found:
            _auto_exe, _auto_cwd = _find_katago()
            _exe_found = exe or _auto_exe
            _cwd_found = cwd or (os.path.dirname(_exe_found) or _auto_cwd)
        self.exe = _exe_found
        self.cwd = _cwd_found
        if model:
            self.model = model
            self.cfg = cfg or FAST_CFG
        else:
            self.model = weight_path(weight)
            self.cfg = cfg or FAST_CFG
        self.proc = None
        self._lock = threading.Lock()
        self._seq = 0
        self.version = None
        self.model_name = os.path.basename(self.model)
        self.threads = threads
        self.batch = batch
        # GTP 模式专用
        self._lines = []
        self._buf = b""
        self._linelock = threading.Lock()
        self._gtp_lock = threading.Lock()
        self._name = None
        self.boot_secs = None
        self.w = self.h = 19
        self.komi_val = komi if komi is not None else 7.5
        self._warmed = False       # 首次 kata-analyze 有 ~2s 预热，见 analyze_gtp
        self.turn = "B"

    # ------------------------------------------------ 启动 / 关闭

    def start(self, timeout=120, quiet=False):
        if self.proc and self.proc.poll() is None:
            return True
        # ⚠️ 三个文件都要先查，而且话说得让人看得懂（2026-10-23）
        # ----------------------------------------------------
        # 原来只查了 model 和 cfg，**漏了 exe 本身** —— 新电脑上没装
        # katago 时直接 Popen，抛的是
        #     FileNotFoundError: [WinError 2] 系统找不到指定的文件
        # 用户不知道是哪一步缺东西。而且原来的「权重不存在：C:\...」
        # 只有路径没有出路，等于把问题丢回给他。
        #
        # 这三条错误最终会显示在界面上，所以每条都要包含「怎么办」。
        if not os.path.isfile(self.exe):
            raise FileNotFoundError(
                f"找不到 katago.exe：{self.exe}\n\n"
                "本机引擎需要 KataGo 程序 + 权重文件（.bin.gz）。\n"
                "装好之后在「引擎设置 → 手动添加…」里选一遍 exe 和权重即可。\n"
                "也可以用云端引擎（需要安装智子围棋客户端）。"
            )
        if not os.path.isfile(self.model):
            raise FileNotFoundError(
                f"找不到权重文件：{self.model}\n\n"
                "权重（*.bin.gz）要单独下载，通常在 KataGo 的 weights 目录。\n"
                "在「引擎设置 → 手动添加…」里重新选一次权重路径。"
            )
        if not os.path.isfile(self.cfg):
            raise FileNotFoundError(
                f"找不到分析配置：{self.cfg}\n\n"
                "这是程序自带的配置文件，正常不该缺。"
                "如果是从别处拷来的 exe，请重新下载完整的程序。"
            )
        if self.gtp_mode:
            return self._start_gtp(timeout, quiet)

        cmd = [self.exe, "analysis", "-model", self.model, "-config", self.cfg]
        if self.threads:
            cmd += ["-override-config", f"numSearchThreadsPerAnalysisThread={self.threads}"]
        if self.batch:
            cmd += ["-override-config", f"nnMaxBatchSize={self.batch}"]

        if not quiet:
            print(f"[本机引擎] {self.weight_key} · {self.model_name}")
        # CREATE_NO_WINDOW：katago.exe 是控制台程序，GUI 里 Popen 它
        # 会弹黑窗口（2026-10-27 同 zhizi_cloud 的修法）
        self.proc = subprocess.Popen(
            cmd, cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, bufsize=1,
            creationflags=0x08000000,       # CREATE_NO_WINDOW
        )
        # stderr 单独收，避免管道塞满把进程卡死
        self._err = []
        threading.Thread(target=self._drain_err, daemon=True).start()

        t0 = time.time()
        # 用一次极浅的分析确认就绪（KataGo 没有 ready 握手）
        #
        # ⚠️ 超时必须给够。首次加载某个新架构的权重时，OpenCL 后端会先跑
        #    OpenCL tuning（实测 b40 要试 144 组 config，b28 只要缓存直接过）。
        #    之前用 120s 超时，b40 直接被判定「引擎进程结束」。
        #    调优结果缓存在 KataGoData/opencltuning/，同一权重第二次就快。
        r = self._request({"moves": [], "rules": "chinese", "komi": 7.5,
                           "boardXSize": 19, "boardYSize": 19,
                           "maxVisits": 4}, timeout=max(300, timeout),
                          quiet=True)
        if r is None or r.get("error"):
            raise RuntimeError(f"引擎启动失败：{(r or {}).get('error')}\n"
                               f"{''.join(self._err[-12:])}")
        self.boot_secs = round(time.time() - t0, 1)
        if not quiet:
            print(f"[本机引擎] 就绪 {self.boot_secs}s")
        atexit.register(self.close)
        return True

    def _drain_err(self):
        try:
            for line in self.proc.stderr:
                self._err.append(line)
                if len(self._err) > 400:
                    del self._err[:200]
        except Exception:
            pass

    # ------------------------------------------------ GTP 模式

    def _start_gtp(self, timeout=300, quiet=False):
        """按 Lizzie 的方式起：exe gtp -model ... -config ...

        ⚠️ 必须加 -override-config 放开 maxVisits（2026-10-22 实测）
        ----------------------------------------------------
        Lizzie 这几个 cfg 把 `maxVisits = 500` 写死了，而
        `kata-set-param maxVisits 1200` **改不动它**（返回空串、不报错、
        但引擎照旧按 500 封顶）。后果：
            kata-analyze 只给每个候选分配几十次访问就收工，
            info 里 winrate 还是神经网络的原始直觉值
            （空盘能报出 99.6% / +14 目 这种荒谬结果）。
        必须在进程启动时用 -override-config 覆盖，才真正放开。
        这里给一个极大值，实际每次分析的深度由 kata-set-param 控制。
        """
        cmd = [self.exe, "gtp", "-model", self.model, "-config", self.cfg,
               "-override-config", "maxVisits=10000000,maxTime=100000.0"]
        cmd += self.extra_args
        if not quiet:
            print(f"[本机引擎/GTP] {self.weight_key} · {self.model_name}")
        self.proc = subprocess.Popen(
            cmd, cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=0x08000000,       # CREATE_NO_WINDOW（GTP 模式同样）
        )
        self._lines, self._buf = [], b""
        threading.Thread(target=self._read_loop, daemon=True).start()

        t0 = time.time()
        while time.time() - t0 < timeout:
            if any("GTP ready" in s for _, s in self._lines) or \
               any(s.startswith("=") for _, s in self._lines):
                break
            if self.proc.poll() is not None:
                tail = "\n".join(s for _, s in self._lines[-12:])
                raise RuntimeError(f"引擎启动失败（进程已退出）\n{tail}")
            time.sleep(0.1)
        else:
            raise TimeoutError(
                "引擎握手超时 %.0fs\n%s"
                % (timeout, "\n".join(s for _, s in self._lines[-12:])))

        for _, s in self._lines:
            if "Model name:" in s:
                self._name = s.split("Model name:", 1)[1].strip()
            if s.startswith("KataGo v") or "KataGo" in s and "gpu" in s.lower():
                if not self.version and s.startswith("KataGo"):
                    self.version = s.strip()
        self.boot_secs = round(time.time() - t0, 1)
        # 把基础设置定下来（跟 Lizzie 一致）
        self.send("boardsize 19", timeout=30)
        self.send(f"komi {self.komi_val}", timeout=30)
        if not quiet:
            print(f"[本机引擎/GTP] 就绪 {self.boot_secs}s　{self._name or ''}")
        atexit.register(self.close)
        return True

    def _read_loop(self):
        while True:
            try:
                ch = self.proc.stdout.read(1)
            except Exception:
                break
            if not ch:
                break
            self._buf += ch
            if ch == b"\n":
                s = self._buf.decode("utf-8", "replace").rstrip("\r\n")
                self._buf = b""
                with self._linelock:
                    self._lines.append((time.time(), s))
                    if len(self._lines) > 4000:
                        del self._lines[:1500]

    def _get_lines(self):
        with self._linelock:
            return list(self._lines)

    def send(self, cmd, timeout=60):
        """GTP 发命令，等 `=` 应答（多行拼成一段）

        ⚠️ 快照必须在写之前取（2026-10-22 找到「经常连接失败超时」的根因）
        ----------------------------------------------------
        老写法是「先 write，再在 _read_reply 里 idx = len(lines)」。
        这中间有个竞态窗口：`komi` / `boardsize` / `name` 这类命令引擎
        几乎瞬间就回，应答可能在**取快照之前**已经被读线程放进 _lines。
        于是快照把这条应答也圈进去了 → 从它后面开始找 → 永远等不到
        → 30 秒后抛 TimeoutError。

        表现就是老板说的「经常显示连接失败超时」：
        时快时慢、纯看 GIL 调度，不是每次都炸，所以特别难缠。

        正确顺序：先取快照 → 再写命令 → 从快照往后读。
        """
        if not self.alive():
            raise RuntimeError("引擎未运行")
        with self._gtp_lock:
            idx = len(self._get_lines())        # ← 一定要在 write 之前
            self.proc.stdin.write((cmd + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            return self._read_reply(timeout, start=idx)

    def _read_reply(self, timeout=60, start=None):
        t0 = time.time()
        idx = len(self._get_lines()) if start is None else start
        got, started = [], False
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
                    continue
                if s == "":
                    return "\n".join(x for x in got if x).strip()
                got.append(s.strip())
            time.sleep(0.01)
        raise TimeoutError(f"GTP 应答超时（{timeout}s）：{got[:3]}")

    def set_position(self, board, color, w=19, h=19):
        """摆局面 + 精确指定轮走方（用 loadsgf，唯一干净的办法）

        ⚠️ 三次试错记录（2026-10-22，别再走回头路）
        ----------------------------------------------------
        1) `set_position B D4 W D16` —— 只摆子，**不管轮次**。
           showboard 永远 `Next player: Black`，白先的局被当黑先算。
        2) `clear_board` + `play` 逐手 —— 轮次能对，但有副作用：
           想造「白先」就得让黑方 `play B pass`，
           而 pass 会被引擎当成**停一手**，MoveNum+1，
           于是 kata-analyze 认为对局已进入终局阶段，
           空盘直接报「白方 99.6% / 领先 14 目」—— 荒谬值的真凶。
        3) `loadsgf`（本实现）—— SGF 头部写 `PL[W]`/`PL[B]` + `AB`/`AW`
           摆子，轮次和盘面一次到位，零副作用，**MoveNum 仍是 0**。
           唯一代价是每手写一个临时 sgf，几十字节，可忽略。

        为什么不担心「假手顺」：analyze 只看 盘面+轮走方+贴目，
        不重演历史；劫争状态本来也无法从单张截图还原。
        """
        import tempfile
        turn = (color or "B").upper()
        parts = [f"(;GM[1]FF[4]SZ[{max(w, h)}]KM[{self.komi_val:g}]PL[{turn}]"]
        ab, aw = [], []
        for y in range(h):
            for x in range(w):
                v = board[y][x]
                if not v:
                    continue
                # SGF 坐标：字母 a..s，第一个是列，第二个是**从上往下**的行
                sgf_xy = f"{chr(ord('a') + x)}{chr(ord('a') + y)}"
                (ab if v == 1 else aw).append(sgf_xy)
        if ab:
            parts.append("AB[" + "][".join(ab) + "]")
        if aw:
            parts.append("AW[" + "][".join(aw) + "]")
        parts.append(")")
        sgf = "".join(parts)

        fd, path = tempfile.mkstemp(suffix=".sgf", prefix="kata_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(sgf)
            resp = self.send(f"loadsgf {path}", timeout=60)
            if resp and resp.startswith("?"):
                raise RuntimeError(f"loadsgf 失败：{resp}")
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

        # 校验轮次真的对了（这个断言必须留：轮次错 = 全盘胜率错）
        got = self._next_player()
        if got and got != turn:
            raise RuntimeError(
                f"摆盘后轮次不符：要 {turn}，引擎是 {got}（SGF={sgf[:120]}）")
        self.w, self.h = w, h
        self.turn = turn

    def _next_player(self):
        """问引擎现在轮到谁 —— showboard 里那行 `Next player: Black/White`

        比自己在 Python 里记账可靠：pass/undo/提子都会改轮次，
        自己算迟早算错（这次就是）。
        """
        sb = self.send("showboard", timeout=20)
        if not sb:
            return None
        low = sb.lower()
        if "next player: black" in low:
            return "B"
        if "next player: white" in low:
            return "W"
        return None

    def _analyze_gtp(self, board=None, turn="B", komi=7.5, visits=1200,
                     maxmoves=12, w=19, h=19, max_secs=None,
                     stop_at_visits=None):
        """GTP 模式的分析入口：摆局面 → kata-analyze → 统一结构

        视角（2026-10-22 实测定论，别再猜）
        ----------------------------------------------------
        这几个 Lizzie cfg 里 reportAnalysisWinratesAs 全被注释掉，
        实测默认行为 = **相对 kata-analyze 的 color 参数（轮走方）**：
          判据① 空盘白先 → 0.34（白贴 7.5 目略亏，合理）
          判据② 黑D16 后白先 → 0.66（轮走方变黑，黑优，正确）
          判据③ 黑228子压满 vs 白0子、白先 → 0.0002
                若真按 BLACK 该报 ≈1.0；报 0.0002 只能解释为轮走方。
        → 本函数把 winrate 统一**换算成白方视角**：
          turn=W 时原值就是白胜率，直接用；
          turn=B 时是黑胜率，1-x 才是白胜率。
        """
        # ⚠️ komi 必须先设 —— set_position 走 SGF，SGF 头里要写 KM[]
        if komi != self.komi_val:
            self.send(f"komi {komi}", timeout=20)
            self.komi_val = komi
        if board is not None:
            self.set_position(board, turn, w, h)
        r = self.analyze_gtp(visits=visits, maxmoves=maxmoves,
                             max_secs=max_secs, turn=turn,
                             stop_at_visits=stop_at_visits)
        # 轮走方 → 白方视角
        # 注意这里**只翻数值、不重排**：候选的胜负值都是根节点值，
        # 排序会毁掉引擎的推荐顺序（order 0 才是首选）。
        to_white = (turn.upper() == "B")
        for d in r["moves"]:
            if d.get("winrate") is not None and to_white:
                d["winrate"] = 1.0 - d["winrate"]
            if d.get("scoreLead") is not None and to_white:
                d["scoreLead"] = -d["scoreLead"]
            d["perspective"] = "W"
        root = r["moves"][0] if r["moves"] else {}
        return {
            "ok": bool(r["moves"]),
            "engine": "local", "weight": self.weight_key,
            "model": self.model_name, "turn": turn.upper(),
            "winratePerspective": "white",
            "rootWinrate": root.get("winrate"),
            "rootScoreLead": root.get("scoreLead"),
            "visits": r["bestVisits"], "elapsed": r["elapsed"],
            "requestedVisits": visits,
            "moves": r["moves"],
            "best": r["moves"][0] if r["moves"] else None,
            "komi": komi, "w": w, "h": h,
        }

    def analyze_gtp(self, visits=1200, maxmoves=12, max_secs=None, interval=None,
                    turn="B", stop_at_visits=None):
        """跑一次 kata-analyze，返回**轮走方视角**的候选

        ⚠️ `kata-analyze <color>` 的语义（2026-10-22 用已知盘面钉死）
        ----------------------------------------------------
        color = **给谁出主意** = 轮走方。而 info 里的 `winrate`
        就是**这一方**的胜率，不是黑视角、也不是白视角。

        判据（黑压全场 228:0，黑必胜）：
            kata-analyze W → wr=0.0002  （白方胜率 0.02%，对）
            kata-analyze B → wr=0.9998  （黑方胜率 99.98%，对）
        所以：**必须传轮走方**；不传或传错，胜率就是别的意思。
        （云端 kata-analyze 也一样：不传默认按 B）

        ⚠️ interval 单位是**厘秒**，不是毫秒（2026-10-22 血亏）
        ----------------------------------------------------
        老代码写死 interval=200，那是 **2 秒**才吐一轮 info。
        实测首帧到达时间：
            interval 200 → 2.14s      interval  20 → 0.29s
            interval 100 → 1.08s      interval   5 → 0.15s
        而实战预算常是 1~2 秒 → **一帧都收不到** → 上层判「没有可下点」
        → 直接不下棋。老板报的「调 1 秒或 2 秒就不动，3 秒以上才行」
        就是这个；3 秒之所以"能用"，只是因为 3s > 2s 刚好过线。

        现在按预算动态算：interval = 预算秒 × 100 / 6，夹在 5~50 厘秒之间。
        1 秒预算 → 16 厘秒（每 0.16s 一轮），2 秒 → 33，3 秒以上 → 50。
        """
        self.send("name", timeout=10)          # 屏障：冲掉上一轮残留
        self.set_param("maxVisits", visits)
        self.send("name", timeout=10)
        t0 = time.time()

        # 动态 interval（厘秒）。没给预算时按 8 秒算 → 50 厘秒。
        if interval is None:
            budget = max_secs if max_secs else 8.0
            interval = int(max(5, min(50, budget * 100 / 6)))

        self.proc.stdin.write(
            f"kata-analyze {(turn or 'B').upper()} interval {interval}\n".encode())
        self.proc.stdin.flush()
        start_idx = len(self._get_lines())
        frames, maxv, last_growth = {}, 0, time.time()
        # ⚠️ 首次分析有约 2s 预热（权重首轮上卡 / cache）。
        # interval 已经改小，正常 0.2s 内就有第一帧；
        # 只有「一帧都没有」时才需要等这 2.5s 兜底。
        min_wait = 2.5 if not self._warmed else 0.0
        deadline = max(max_secs or 60, min_wait)
        while time.time() - t0 < deadline:
            ls = self._get_lines()
            changed = False
            while start_idx < len(ls):
                _, s = ls[start_idx]
                start_idx += 1
                if not s.startswith("info"):
                    continue
                changed = True
                for d in _parse_infos(s):
                    mv = d.get("move")
                    if not mv:
                        continue
                    old = frames.get(mv)
                    if old is None or (d.get("visits") or 0) >= (old.get("visits") or 0):
                        frames[mv] = d
            if changed:
                nv = max(((f.get("visits") or 0) for f in frames.values()), default=0)
                if nv > maxv:
                    maxv, last_growth = nv, time.time()
                if stop_at_visits and maxv >= stop_at_visits:
                    break
                if len(frames) >= 6 and maxv >= visits * 0.98:
                    break
            elif frames and time.time() - last_growth > 3.0:
                break
            # 硬上限：到点就收工
            #
            # ⚠️ 原来还要求 `len(frames) >= 2`（2026-10-23 老板纠正）
            # ----------------------------------------------------
            # 与 zhizi_cloud.py 同一处逻辑错误，两边必须一起改，
            # 否则换成本机引擎又是「设 5 秒实际跑 11 秒」。
            #
            # 老板原话：「候选点数量没有太大意义，有时候几十个点都可以，
            #   有时候比如被打吃时，只能这1个点」
            # 被迫局面（打吃/单关）本来就只有一个合法点，候选数少
            # 不代表没算好 —— 恰恰是最好算的局面，却被罚等到 timeout。
            #
            # 改成：只要拿到任何候选就到点收工；一个都没有才继续等。
            if max_secs and time.time() - t0 >= max_secs and frames:
                break
            time.sleep(0.03)
        if frames:
            self._warmed = True          # 预热过了，后面按正常预算走
        try:
            self.proc.stdin.write(b"\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        time.sleep(0.15)

        lst = sorted(frames.values(),
                     key=lambda x: (x.get("order", 99), -(x.get("visits") or 0)))
        out = []
        for d in lst[:maxmoves]:
            d = dict(d)
            # ⚠️ 不做任何翻转（2026-10-22 实测定论）
            # ----------------------------------------------------
            # 老板这 7 个 cfg 里 reportAnalysisWinratesAs 全是**注释掉的**，
            # 但实测默认行为 = **相对 kata-analyze 的 color 参数（轮走方）**：
            #   判据① 空盘黑先 → 白胜率 0.340（黑 34%，贴 7.5 目吃紧）
            #   判据② 空盘白先 → 白胜率 0.996（白白多走一手，黑几无胜率）
            #   判据③ 黑228子压满 vs 白0子、白先 → 0.0002
            #   判据④ 黑D16 后黑先（黑连走两手）→ 白 0.5%
            # 四条全部自洽，winrate 就是「kata-analyze 那个 color 一方」的胜率。
            # 所以这里原样保留，由 _analyze_gtp 按 turn 统一换成白方视角。
            wr = d.get("winrate")
            sl = d.get("scoreLead")
            d["winrateRaw"] = wr
            d["scoreLeadRaw"] = sl
            d["perspective"] = "SIDETOMOVE"
            out.append(d)
        # ⚠️ 不要按 winrate 排序（2026-10-22 实测）
        # ----------------------------------------------------
        # kata-analyze 吐的是一条 info 里塞几百个候选，而它们的
        # winrate/scoreLead **全都是根节点的值、一模一样**
        # （实测 189~273 个候选写的是同一个 0.9963 / +14.24）。
        # 按 winrate 排等于乱序 —— 引擎给的推荐顺序在 `order` 字段里：
        # order 0 = 首选，1 = 次选……上面 lst 已经按 order 排好了，别再动。
        return {"elapsed": round(time.time() - t0, 2), "bestVisits": maxv,
                "moves": out}

    def set_param(self, k, v):
        return self.send(f"kata-set-param {k} {v}", timeout=20)

    def close(self):
        p = self.proc
        if not p:
            return
        try:
            if self.gtp_mode:
                p.stdin.write(b"quit\n")
            else:
                p.stdin.write('{"id":"__quit__"}\n')
            p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        self.proc = None

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    # ------------------------------------------------ 底层请求

    def _request(self, body, timeout=180, quiet=False):
        """发一个分析请求，等到 isDuringSearch=False 的最终结果"""
        if not self.alive():
            raise RuntimeError("引擎未运行")
        with self._lock:
            self._seq += 1
            rid = f"q{self._seq}"
            body = dict(body)
            body["id"] = rid
            self.proc.stdin.write(json.dumps(body) + "\n")
            self.proc.stdin.flush()

            t0 = time.time()
            last = None
            while time.time() - t0 < timeout:
                line = self.proc.stdout.readline()
                if not line:
                    return {"error": "引擎进程结束"}
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue                 # 非 JSON 的日志行
                if d.get("id") != rid:
                    continue
                if d.get("error"):
                    return d
                last = d
                if d.get("isDuringSearch") is False:
                    return d
            if last:
                return last                  # 超时就用最后一条中间结果
            return {"error": "分析超时"}

    # ------------------------------------------------ 对外：分析

    def analyze(self, board=None, turn="B", komi=7.5, visits=2000,
                moves=None, maxmoves=16, w=19, h=19, rules="chinese",
                timeout=None, include_ownership=False, max_secs=None,
                stop_at_visits=None):
        """分析一个局面

        board  : 二维数组 [[0,1,2...]]，1=黑 2=白，[y][x]，y=0 最上面一行
        turn   : "B"/"W"，轮到谁（会作为 initialPlayer 显式传给引擎，不会猜错）
        moves  : 完整手顺 [["B","Q16"],...]，给了就优先用它（提子语义准确）

        GTP 模式下走 kata-analyze，返回结构统一成白方视角。
        """
        if self.gtp_mode:
            return self._analyze_gtp(board=board, turn=turn, komi=komi,
                                     visits=visits, maxmoves=maxmoves,
                                     w=w, h=h, max_secs=max_secs,
                                     stop_at_visits=stop_at_visits)
        body = {
            "rules": rules, "komi": komi,
            "boardXSize": w, "boardYSize": h,
            "maxVisits": visits,
            "includeOwnership": bool(include_ownership),
            "includePolicy": False,
        }
        if moves:
            body["moves"] = [[c.upper(), m] for c, m in moves]
            # 手顺给了，轮走方由引擎推导；仍显式覆盖以防万一
            body["initialPlayer"] = "B"
        else:
            stones_b, stones_w = [], []
            for y in range(h):
                for x in range(w):
                    v = board[y][x]
                    if not v:
                        continue
                    g = f"{COLS[x]}{h - y}"
                    (stones_b if v == 1 else stones_w).append(g)
            # `moves` 必须给（哪怕空数组）。
            # 缺了它引擎直接回 "Must specify an array of [player,location] pairs"
            # 并把 field 指到 moves —— 报错看着像 initialStones 有问题，其实是这个。
            body["moves"] = []
            body["initialStones"] = ([["B", s] for s in stones_b] +
                                     [["W", s] for s in stones_w])
            body["initialPlayer"] = turn.upper()

        t0 = time.time()
        r = self._request(body, timeout=timeout or max(60, visits / 200))
        elapsed = round(time.time() - t0, 2)
        if r.get("error"):
            return {"ok": False, "error": r["error"], "elapsed": elapsed}

        root = r.get("rootInfo") or {}
        infos = r.get("moveInfos") or []
        out = []
        for d in infos[:maxmoves]:
            out.append({
                "move": d.get("move"),
                "winrate": d.get("winrate"),
                "scoreLead": d.get("scoreLead"),
                "scoreMean": d.get("scoreMean"),
                "visits": d.get("visits"),
                "prior": d.get("prior"),
                "order": d.get("order"),
                "pv": d.get("pv") or [],
            })
        return {
            "ok": True,
            "engine": "local",
            "weight": self.weight_key,
            "model": self.model_name,
            "turn": turn.upper(),
            "winratePerspective": "side_to_move",
            "rootWinrate": root.get("winrate"),
            "rootScoreLead": root.get("scoreLead"),
            "visits": root.get("visits"),
            "elapsed": elapsed,
            "requestedVisits": visits,
            "moves": out,
            "best": out[0] if out else None,
            "komi": komi, "rules": rules, "w": w, "h": h,
        }

    def genmove(self, board=None, turn="B", komi=7.5, visits=2000,
                moves=None, w=19, h=19, rules="chinese"):
        """只要一个最佳着法（分析结果的第一个）"""
        r = self.analyze(board=board, turn=turn, komi=komi, visits=visits,
                         moves=moves, maxmoves=8, w=w, h=h, rules=rules)
        if not r.get("ok"):
            return None, r
        b = r.get("best")
        return (b.get("move") if b else None), r

    def info(self):
        return {
            "engine": "local", "alive": self.alive(),
            "weight": self.weight_key, "model": self.model_name,
            "cfg": self.cfg, "bootSecs": getattr(self, "boot_secs", None),
        }


# ---------------------------------------------------------------- CLI

def _board_from_sgf_or_live():
    import lizzie_live
    hit = lizzie_live.find_autosave()
    if not hit:
        return None
    return lizzie_live.parse_sgf(hit[1])


def main():
    import argparse
    ap = argparse.ArgumentParser(description="本机 KataGo 引擎")
    ap.add_argument("--weight", default="b28", choices=list(WEIGHTS))
    ap.add_argument("--visits", type=int, default=2000)
    ap.add_argument("--komi", type=float, default=7.5)
    ap.add_argument("--turn", default="B", choices=["B", "W"])
    ap.add_argument("--live", action="store_true", help="读 Lizzie 当前局面")
    ap.add_argument("--bench", action="store_true", help="跑标准测速")
    args = ap.parse_args()

    e = LocalEngine(weight=args.weight)
    e.start()
    print(json.dumps(e.info(), ensure_ascii=False))

    try:
        if args.bench:
            moves = [["B", "Q16"], ["W", "D4"], ["B", "Q4"], ["W", "D16"]]
            for v in (500, 1000, 2000, 4000):
                r = e.analyze(moves=moves, turn="W", visits=v, komi=args.komi)
                b = r.get("best") or {}
                print(f"  {v:>5}v  {r['elapsed']:5.2f}s  {b.get('move')}  "
                      f"win={b.get('winrate', 0)*100:5.1f}% "
                      f"lead={b.get('scoreLead', 0):+5.2f}  "
                      f"实访={r.get('visits')}")
        elif args.live:
            g = _board_from_sgf_or_live()
            if not g:
                print("没找到 Lizzie 存档")
                return
            turn = "W" if (g["moves"] and g["moves"][-1]["color"] == "B") else "B"
            r = e.analyze(board=g["board"], turn=turn,
                          komi=g.get("komi") or args.komi, visits=args.visits,
                          w=g["w"], h=g["h"])
            print(f"\n{r['w']}x{r['h']} komi={r['komi']} 轮到{r['turn']}  "
                  f"{r['elapsed']}s / {r['visits']}v")
            for d in r["moves"][:8]:
                print(f"  {d['move']:>4} win={d['winrate']*100:5.2f}% "
                      f"lead={d['scoreLead']:+5.2f} v={d['visits']:>5}  "
                      f"{' '.join(d['pv'][:6])}")
        else:
            r = e.analyze(board=[[0]*19 for _ in range(19)], turn="B",
                          visits=args.visits, komi=args.komi)
            print(f"空盘 {r['elapsed']}s / {r['visits']}v")
            for d in r["moves"][:5]:
                print(f"  {d['move']:>4} win={d['winrate']*100:5.2f}%")
    finally:
        e.close()


if __name__ == "__main__":
    main()
