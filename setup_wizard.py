# -*- coding: utf-8 -*-
"""首次运行设置向导 —— 让新用户把引擎配起来

要解决什么
------------------------------------------------------------
老板 2026-10-23 原话：
    「用户安装这个软件之后，会有一个初始设置，让用户配置电脑里的
      katago 权重或者连接智子围棋里面的云算力，当然我已经设置好了的
      引擎不变」

三种用户，一套界面
------------------------------------------------------------
① **老板自己**（已经全配好）
   → `settings.json` 里 `setup_done=true`，启动**不弹**，一切照旧。
② **新用户，装了智子客户端**
   → 自动检测到 zz-ikatago + 权重，勾上「智子云」就行，零配置。
③ **新用户，只有本机 katago.exe**
   → 自动检测到 exe + 权重，勾上「本机」。
   一个都没检测到的，用「浏览…」手动指。

设计原则
------------------------------------------------------------
· **检测结果分组显示**：智子云 / 本机权重 / 已登记（Lizzie）三块，
  每块带「全选」和逐项勾选框，默认全勾 —— 能用的都开，用户只需做减法。
· **只勾能用的**：检测到但不可用（没登录 / 路径失效）的条目灰掉，
  并在右边写清原因，别让用户勾了一个必然失败的东西。
· **不用滚轮折腾**：三块放在同一个滚动区里，一眼能看到全部。
· **「稍后再说」能退出**：点了也记住 `setup_done=True`，
  免得每次启动都弹 —— 用户可以在主界面点「引擎设置」再进来。
"""
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 复用主界面的配色（避免两处各写一套颜色，改一处漏一处）
BG = "#1a1d23"
BG2 = "#23272f"
BG3 = "#2c313a"
FG = "#e8eaed"
FG_DIM = "#8f97a3"
ACCENT = "#d4a86a"
ACCENT2 = "#f0d9a8"
GREEN = "#5fbf6a"
RED = "#e06868"
BORDER = "#333944"

FONT = "Microsoft YaHei UI"


class SetupWizard:
    """首次设置对话框（模态）

    用法：
        w = SetupWizard(root, reg=engines.registry(), cfg=settings.load()[0])
        root.wait_window(w.win)
        if w.result:            # None=取消, dict=要保存的设置
            settings.save(w.result)
    """

    def __init__(self, root, reg=None, cfg=None, detect=None,
                 first_run=True):
        self.root = root
        self.result = None
        self.first_run = first_run
        self.reg = list(reg or [])
        self.cfg = dict(cfg or {})
        # detect 可以是已经算好的结果（省一次扫描），也可以传 None 让它自己扫
        self.det = detect

        # 勾选状态：key → BooleanVar
        self.vars = {}
        # 每项的「能不能用」：key → (usable, reason)
        self.usable = {}
        # 「手动添加…」是否已被扶成主按钮（_emphasize_manual 会来回切）
        self._manual_promoted = False

        self.win = tk.Toplevel(root)
        self.win.title("首次设置 · 野狐自动对弈")
        self.win.configure(bg=BG)
        self.win.transient(root)
        self.win.resizable(True, True)
        # ⚠️ 高度按「内容实际需要」定，别给死板的大数（2026-10-23 截图实测）
        # 880x760 时内容只有约 560 高，下方空出一大片深色，
        # 看着像「还没加载完」。700 刚好贴住三组列表 + 底部按钮。
        #
        # ⚠️ 但不能**超出屏幕**（2026-10-23 分发级修复，和主窗口同源）
        # ----------------------------------------------------
        # 写死 880x700 在 1366x768 上刚好放得下，但在
        # 1024x600 的老上网本 / 高分屏 150% 缩放（逻辑 1024x640）上
        # 就超出屏幕了 —— 底部「完成」按钮落到屏幕外，用户**没法完成设置**，
        # 于是永远卡在向导里（下次启动还会弹）。
        # 这里跟主窗口用同一套逻辑：先问可用区，再收敛。
        _DW, _DH = 880, 700
        _MW, _MH = 760, 560
        try:
            import gui as G
            _aw, _ah, _ax, _ay = G.work_area()
        except Exception:
            _aw, _ah, _ax, _ay = 1920, 1040, 0, 0
        _w = min(_DW, max(600, _aw - 16))
        _h = min(_DH, max(440, _ah - 16))
        self._center(_w, _h)
        # minsize 也收敛：不许比屏幕大，否则用户永远拖不下来
        self.win.minsize(min(_MW, _w), min(_MH, _h))

        self._build()

        # ⚠️⚠️ 建完界面后**再夹一次位置**（2026-10-23 实测 1366x768）
        # ----------------------------------------------------
        # 上面那句 _center 是在 _build() **之前**调用的，此时窗口里没有
        # 任何控件，`winfo_width()` 还是初始的 200x200，算出来的 x/y
        # 是按「假设窗口是 w×h」推的 —— 但 `transient(root)` 的窗口
        # 在 Windows 上位置会被**窗口管理器重算**：实测设了 +254+20，
        # 布局完成后实际跑到 +265+65（WM 按主窗口重新对齐了）。
        #
        # 后果：1366x768（工作区 728）上窗口 y=65 + 高 700 = 765，
        # 底部 37px 落在屏幕外 —— 而「完成」按钮就在最底下那条。
        #
        # 这必须在**真正 map 出来之后**做才有意义，所以用 after_idle
        # （比 after(0) 更靠后，等 WM 完成位置调整）。
        self.win.after_idle(self._clamp_on_screen)
        # 模态：挡住主窗口，但**不要** grab_set 之后再调 after
        # （after 的回调在 grab 生效后仍能跑，但用户点不到主窗口）
        try:
            self.win.grab_set()
        except Exception:
            pass

        if self.det is None:
            self.win.after(80, lambda: self._scan(auto=True))
        else:
            # ⚠️ 有现成的检测结果就直接渲染（2026-10-23 老板实测抓到的 bug）
            # ----------------------------------------------------
            # 原来这里只有 `if self.det is None: _scan()` 一个分支，
            # 意思是「调用方给了结果就不扫」。但**忘了渲染** ——
            # 于是界面停在 _build() 时的初始文字「准备扫描…」，
            # 列表一片空白，用户只看到一个永远不动的向导。
            #
            # 主界面每次都会传 detect=self.detect_res（它已经扫过了，
            # 何必再扫一遍），所以这条路是**主路径**，不是边角：
            # 别人拿到 exe 一打开、点「完成/设置」，看到的就是空列表。
            #
            # 也不能改成「有结果也重扫」：detect(check_token=True) 要起
            # subprocess 读 token（约 0.3~1s，慢机器更久），
            # 白白让用户多等一次；而且两次扫描的 key 若不一致会出乱子
            # （主界面和向导必须共用同一份，这是之前的教训）。
            self.win.after(80, self._render)

    # ------------------------------------------------------------ 版面

    def _center(self, w, h):
        """把向导摆在主窗口正中，并**保证整个窗口在屏幕可见区内**

        ⚠️ 原来只做了「相对主窗口居中」+ 一个 `max(0, ...)`（2026-10-23）
        ----------------------------------------------------
        `max(0, x)` 只挡住了**左上**越界，右下没管：
        如果主窗口贴右边缘，或主窗口比屏幕还大（老代码可能），
        算出来的 x 让向导右半截在屏幕外 —— 而「完成」按钮恰好在右下角。
        用户看到的是「向导出来了，但没有完成按钮」，无从下手。

        现在：先按主窗口居中，再往屏幕可用区里**夹一次**。
        """
        self.win.update_idletasks()
        try:
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            x = rx + (rw - w) // 2
            y = ry + (rh - h) // 2
        except Exception:
            x = y = 80

        # 夹进屏幕（留 8px 边，别贴着边）
        try:
            import gui as G
            aw, ah, ax, ay = G.work_area()
        except Exception:
            aw = ah = 0
        if aw > 200 and ah > 200:
            x = max(ax + 8, min(x, ax + aw - w - 8))
            y = max(ay + 8, min(y, ay + ah - h - 8))
        x = max(0, x)
        y = max(0, y)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _clamp_on_screen(self):
        """窗口**真正 map 出来之后**，把尺寸和位置夹进屏幕

        ⚠️ 为什么不能只在 _center 里做（2026-10-23 实测）
        ----------------------------------------------------
        见 __init__ 里 after_idle 那段说明：transient 窗口的位置
        会被 Windows 窗口管理器**重新计算**，_center 里设的坐标不作数。
        实测 1366x768 上：设 +254+20，实际落 +265+65，
        底部「完成」按钮被推出屏幕 37px。

        ⚠️⚠️ 只挪 y 是**没用的**（2026-10-23 二次实测踩到）
        ----------------------------------------------------
        第一次实现只改 y：算 `ny = min(65, 728-700-4) = 24`，然后
        `geometry("880x700+265+24")`。结果窗口**底边还是超出屏幕**。
        原因：窗口高度 700 已经超过「可用区 728 - 标题栏 45 - 边距」，
        y 无论设多少，底边都在屏幕外。

        ⚠️⚠️⚠️ 第三个坑：geometry 的 y 是**客户区**坐标，
        `winfo_rooty()` 是**外框**坐标，两者差一个标题栏高度（45px）
        ----------------------------------------------------
        实测：`geometry("880x700+276+24")` → `winfo_rooty()` 报 **69**。
        69 = 24 + 45(标题栏)。所以：
          · 判断「有没有超出屏幕」必须用 **winfo_rooty() + winfo_height()
            + 标题栏**，不能只用 geometry 里的 y
          · 而这个函数拿不到标题栏高度，所以**改用外框尺寸算**：
            把 height 压到 `ah - 标题栏 - 边距`，标题栏按 45 估（Windows 标准）
        """
        try:
            self.win.update_idletasks()
            W = self.win.winfo_width()
            H = self.win.winfo_height()
            X = self.win.winfo_rootx()
            Y = self.win.winfo_rooty()
            if W < 50 or H < 50:      # 还没真正 map
                self.win.after(80, self._clamp_on_screen)
                return

            try:
                import gui as G
                aw, ah, ax, ay = G.work_area()
            except Exception:
                return
            if aw < 200 or ah < 200:
                return

            # 标题栏高度：外框 y - 客户区 y（几何管理器给的真实值）
            cap = 45
            try:
                gy = 0
                g = self.win.geometry()          # "880x700+265+24"
                if "+" in g:
                    gy = int(g.split("+")[-1])
                d = Y - gy
                if 0 <= d <= 120:
                    cap = d
            except Exception:
                pass

            # ① 先缩**尺寸**：外框底边必须落在 ay+ah 之内
            nw = min(W, max(600, aw - 12))
            # 客户区最大高 = 可用区高 - 标题栏 - 上下边距
            nh = min(H, max(300, ah - cap - 12))
            if (nw, nh) != (W, H):
                try:
                    mw, mh = self.win.minsize()
                    self.win.minsize(min(mw, nw), min(mh, nh))
                except Exception:
                    pass
                self.win.geometry(f"{nw}x{nh}")
                self.win.update_idletasks()
                W, H = nw, nh
                X = self.win.winfo_rootx()
                Y = self.win.winfo_rooty()

            # ② 再把**外框**夹进可用区
            nx = max(ax + 4, min(X, ax + aw - W - 4))
            ny = max(ay + 4, min(Y, ay + ah - H - cap - 4))
            nx = max(0, nx)
            # geometry 要的是**客户区** y，所以减掉标题栏
            ny_client = max(0, ny - cap)
            if (nx, ny) != (X, Y):
                self.win.geometry(f"{W}x{H}+{nx}+{ny_client}")
        except Exception:
            pass

    def _build(self):
        # ---- 顶部说明 ----
        top = tk.Frame(self.win, bg=BG)
        top.pack(fill="x", padx=22, pady=(18, 6))

        title = ("欢迎使用 · 先把引擎配好" if self.first_run
                 else "引擎设置")
        tk.Label(top, text=title, bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(anchor="w")
        # ⚠️ 说明里的按钮名必须和实际按钮一致（2026-10-23）
        # 原来非首启写的是「改完点「保存」」，但按钮上写的是「完成」——
        # 用户照着找「保存」找不到。这类"指错按钮名"的文案比没有文案更糟。
        sub = ("已自动扫描本机。能用的一律默认勾选 —— 你只需要去掉不想用的。"
               if self.first_run else
               "引擎有变动就点「重新扫描」。改完点「完成」，"
               "勾选的引擎会出现在主界面的下拉框里。")
        tk.Label(top, text=sub, bg=BG, fg=FG_DIM,
                 font=(FONT, 9)).pack(anchor="w", pady=(4, 0))

        # ---- 扫描状态行 ----
        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", padx=22, pady=(10, 4))
        self.scan_lbl = tk.Label(bar, text="准备扫描…", bg=BG, fg=ACCENT,
                                 font=(FONT, 9), anchor="w")
        self.scan_lbl.pack(side="left", fill="x", expand=True)
        self.btn_rescan = ttk.Button(bar, text="重新扫描",
                                     command=self._scan)
        self.btn_rescan.pack(side="right")

        # ---- 底部 ----
        # ⚠️⚠️ 底部必须**先建、且 side="bottom"**（2026-10-23 1024x600 实测）
        # ----------------------------------------------------
        # 原来顺序是：top → 扫描行 → wrap(expand=True) → bot。
        # 而 `wrap` 是 `fill="both", expand=True` —— Tk 的 pack 里，
        # expand 的控件会**吃掉所有剩余空间**。窗口够高时没感觉；
        # 窗口只有 544 高时，wrap 把空间吃干净，bot 分到 0 高度 →
        # **三个按钮全部变成 1x1，界面上完全看不见**。
        # 而那正是「完成」按钮所在的那一行：用户看到向导出来了，
        # 但没有完成键，只能强关进程（下次启动还弹）。
        #
        # 现在：bot 用 side="bottom" 先 pack，Tk 会**优先**给它所需高度，
        # wrap 再吃剩下的。这样无论窗口多矮，按钮永远在。
        bot = tk.Frame(self.win, bg=BG)
        bot.pack(side="bottom", fill="x", padx=22, pady=(2, 14))

        # ---- 可滚动的三组 ----
        wrap = tk.Frame(self.win, bg=BG2, highlightthickness=1,
                        highlightbackground=BORDER)
        wrap.pack(fill="both", expand=True, padx=22, pady=(4, 8))

        self.canvas = tk.Canvas(wrap, bg=BG2, highlightthickness=0, bd=0)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=BG2)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_config)
        self.canvas.bind("<Configure>", self._on_canvas_config)
        # 鼠标滚轮（Windows 上 delta 是 120 的倍数）
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(seq, self._on_wheel, add="+")

        # ⚠️⚠️ 提示语与按钮**分成两行**，不再抢同一行的宽度（2026-10-23）
        # ----------------------------------------------------
        # 之前是「提示语 side=left + 三个按钮 side=right」挤在同一行。
        # 实测（裸机、窗口 880 宽）：
        #   bot 容器 836px
        #   三个 ttk.Button 各 **131px**（ttk 有最小宽度，不是文字宽！）
        #   → 占 131*3 + padx16 = 409px
        #   提示语只剩 **427px**
        # 而提示语整句 measure = 963px → 按 423 折行要 **4 行**，
        # 第一行仅剩「· 」两个字符（截图里就是个孤零零的点，
        # 看着像界面坏了）。bot 容器因此高达 90px。
        #
        # 根因不是 wraplength 算错（那部分是对的），
        # 而是**两个东西不该抢同一行的宽度**：
        # 提示语是可变长的中文句子，按钮是固定宽度的操作；
        # 挤在一起时，谁先 pack 谁赢，另一方必然被压。
        # 分开后：提示语独占一整行（836px，全句 963 折 2 行），
        # 按钮排独占一行，两个都不会再被对方挤。
        #
        # 顺序要求：bot 用 side="bottom" 先占位，提示语先 pack
        #（它在上面），按钮后 pack（它在下面）。
        self.prob_lbl = tk.Label(bot, text="", bg=BG, fg=FG_DIM,
                                 font=(FONT, 8), anchor="w",
                                 justify="left", wraplength=400)
        self.prob_lbl.pack(side="top", fill="x", pady=(0, 6))

        # 按钮排：单独一个 Frame，从右往左摆
        row = tk.Frame(bot, bg=BG)
        row.pack(side="top", fill="x")

        self.btn_ok = ttk.Button(row, text="完成", style="Go.TButton",
                                 command=self._on_ok)
        self.btn_ok.pack(side="right", padx=(8, 0))
        cancel_txt = "稍后再说" if self.first_run else "取消"
        self.btn_cancel = ttk.Button(row, text=cancel_txt,
                                     command=self._on_cancel)
        self.btn_cancel.pack(side="right")
        self.btn_manual = ttk.Button(row, text="手动添加…",
                                     command=self._on_manual)
        self.btn_manual.pack(side="right", padx=(0, 8))

        # ⚠️ wraplength 必须按**标签自己的实际宽度**算（2026-10-23 截图发现）
        # ----------------------------------------------------
        # 第一版是这样写的：
        #     bot.bind("<Configure>",
        #              lambda e: prob_lbl.configure(wraplength=e.width - 300))
        # 错在哪：e.width 是**父容器**（整行）的宽度 836，减 300 得 536，
        # 但标签实际只有 435px（三个按钮先占走了 401）。
        # wraplength(536) > 实际宽度(435) → **Tk 认为不用换行** →
        # 直接按 536 排一行，右边超出部分被**硬裁掉**：
        #     第二行显示成「…客户端再点「重」
        # 用户永远看不到「新扫描」这三个字。
        #
        # 正确做法：监听标签**自己**的 <Configure>，用它给的 e.width。
        # 这里 -4 是给光标的余量，避免最后一个字贴着边。
        self.prob_lbl.bind(
            "<Configure>",
            lambda e: self.prob_lbl.configure(
                wraplength=max(160, e.width - 4)))

        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _on_inner_config(self, _e=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_config(self, e):
        # 让内部 frame 宽度跟着画布走（否则文字换行/对齐全靠猜）
        try:
            self.canvas.itemconfigure(self._win_id, width=e.width)
        except Exception:
            pass

    def _on_wheel(self, e):
        # 只有指针真的在画布上才滚，否则会影响别的控件
        try:
            w = self.win.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        if w is None:
            return
        # 从命中的控件往上找，看是不是画布里的东西
        p = w
        inside = False
        while p is not None:
            if p is self.canvas or p is self.inner:
                inside = True
                break
            try:
                p = p.master
            except Exception:
                break
        if not inside:
            return
        try:
            if getattr(e, "num", None) == 4:
                d = -1
            elif getattr(e, "num", None) == 5:
                d = 1
            else:
                d = -1 if e.delta > 0 else 1
            self.canvas.yview_scroll(d, "units")
        except Exception:
            pass

    # ------------------------------------------------------------ 扫描

    def _scan(self, auto=False):
        """跑一次自动检测并重建列表"""
        self.scan_lbl.configure(text="正在扫描本机引擎 …", fg=ACCENT)
        self.btn_rescan.configure(state="disabled")
        try:
            self.win.update_idletasks()
        except Exception:
            pass

        def work():
            try:
                import engine_detect as ED
                return ED.detect(check_token=True)
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}

        # ⚠️ 扫描里有 subprocess（读 token）和 os.walk，放主线程会冻界面
        # 用线程 + after 回主线程重建 UI（tkinter 不是线程安全的）
        import threading
        box = {}

        def run():
            box["r"] = work()

        t = threading.Thread(target=run, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                self.win.after(120, poll)
                return
            self.det = box.get("r") or {}
            # ⚠️⚠️ 重扫之后必须把 self.reg 也换成新的（2026-10-23 老板要求）
            # ----------------------------------------------------
            # 老板原话：「如果用户电脑里的引擎有变动，也能够修改」
            #
            # 老代码这里只更新 self.det，**没动 self.reg**。
            # 而 `_render()` 里是这样取列表的：
            #     merged = list(self.reg) if self.reg else list(det['merged'])
            # 也就是 **self.reg 优先**，而它是打开向导那一刻的旧快照。
            #
            # 后果（用户装了智子客户端之后点「重新扫描」）：
            #   · 扫描线程确实跑完了，det 里也有新引擎
            #   · 但 _render 用的是旧 self.reg → **界面一点都没变**
            #   · 用户以为"没扫到"，反复点，始终没反应
            # 「重新扫描」按钮形同虚设，而这正是用户改配置后唯一的入口。
            #
            # ⚠️ 但**手动添加的条目不能被冲掉**：
            # 用户点「手动添加…」指定了 D:\tools\kata\katago.exe，
            # 这个位置不在任何探测清单里，重扫自然扫不到。
            # 如果直接 self.reg = merged，他刚加的那条就消失了 ——
            # 表现为「加了之后点重新扫描，我加的不见了」。
            #
            # ⚠️ 不能用 `merged(_m, None, extra=...)` 去并 ——
            #    merged() 的第一个参数语义是「Lizzie 已登记的」，会把
            #    传进去的每一项都标成 src=lizzie 并置顶，等于**改写了来源**，
            #    而 _render 是靠 src 分组的（lizzie / manual / detect），
            #    来源一错，分组全乱。手动项还是由 _render 从
            #    `_manual_items` 自己补回来（见 _render 的 manual 那段）。
            _m = (self.det or {}).get("merged")
            if _m:
                self.reg = list(_m)
            self._render()
            self.btn_rescan.configure(state="normal")

        self.win.after(120, poll)

    # ------------------------------------------------------------ 渲染

    def _render(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.vars.clear()
        self.usable.clear()

        det = self.det or {}
        if not det or det.get("error"):
            # 没检测结果（或扫描炸了）→ 退回主界面那份列表
            self.scan_lbl.configure(
                text=(f"扫描出错：{det.get('error')}" if det.get("error")
                      else "只列出了已有引擎"),
                fg=RED if det.get("error") else ACCENT)
            self._render_group("已有引擎", "来自程序自带的引擎表",
                               self.reg, icon="📋")
            self._on_inner_config()
            return

        # ⚠️ 列表来源必须和主界面**完全同一份**（2026-10-23 实测 bug）
        # ----------------------------------------------------
        # 最可靠的是主界面传进来的 `self.reg`（= ED.merged(..., extra=...)），
        # 它已经含了 Lizzie + 自动检测 + 手动添加，且 key 已经算好。
        # 只有在 reg 为空（比如直接单跑向导调试）时才退回 det["merged"]。
        merged = list(self.reg) if self.reg else list(det.get("merged") or [])
        if not merged:
            import engine_detect as ED
            merged = ED.merged(det.get("lizzie"), det)

        # ⚠️ 把「手动添加」的条目并回来（2026-10-23）
        # ----------------------------------------------------
        # 用户点「手动添加…」指定的 exe 不在任何探测清单里，
        # 任何一次 `self.reg = 重扫结果` 都会把它冲掉。
        # 这里按 key 去重后补进 merged，保证：
        #   · 点了「重新扫描」→ 手动加的那些还在
        #   · 加了两遍同一个 → 只出现一次
        _seen = {e.get("key") for e in merged}
        for m in (getattr(self, "_manual_items", []) or []):
            if m.get("key") and m["key"] not in _seen:
                merged.append(m)
                _seen.add(m["key"])

        clouds = [e for e in merged if e.get("kind") == "cloud"]
        locals_ = [e for e in merged if e.get("kind") in ("local", "lifego")]
        lizzie = [e for e in merged if (e.get("src") or "") == "lizzie"]
        manual = [e for e in merged if (e.get("src") or "") == "manual"]

        n_ok = 0
        # 智子云的「能不能用」靠 detect 的 usable 标记
        cloud_ok = {e.get("key"): e.get("usable") for e in
                    (det.get("clouds") or [])}
        self._cloud_usable = cloud_ok

        n_ok += self._render_group(
            "智子云算力", "联网用云端的机子，不占本机显卡",
            clouds, icon="☁")
        n_ok += self._render_group(
            "本机 KataGo 权重", "用自己电脑的显卡算，需要 katago.exe + 权重",
            locals_, icon="🖥")
        if manual:
            n_ok += self._render_group(
                "手动添加", "你自己指定的 exe + 权重", manual, icon="✚")
        # 已登记组：只在它还有 merged 里没被前两组覆盖的项时才显示
        rest = [e for e in lizzie
                if e.get("key") not in
                {x.get("key") for x in (clouds + locals_)}]
        if rest:
            n_ok += self._render_group(
                "其它已登记引擎（Lizzie 配置）", "之前配好的其它权重",
                rest, icon="📋")

        import engine_detect as ED
        self.scan_lbl.configure(
            text=ED.summary(det) + f"　·　共 {len(merged)} 项，可勾选 {n_ok} 项",
            fg=ACCENT)

        probs = det.get("problems") or []
        self.prob_lbl.configure(
            text=("　".join("· " + p for p in probs[:2]) if probs else ""),
            fg=FG_DIM)

        self._emphasize_manual(n_ok)
        self._on_inner_config()

    def _emphasize_manual(self, n_ok):
        """一个都不能用时，把「手动添加…」变成主按钮

        ⚠️ 为什么（2026-10-23 裸机实测）
        ----------------------------------------------------
        新电脑上（没装智子、没装 katago）的界面是这样：
            列表只有 1 项，灰色，不可点
            底部一个**大绿色**「完成」
            「手动添加…」缩在最右边
        用户的本能是点绿色的那个 → 弹「一个都没勾，仍然保存吗？」
        → 点「是」→ 进主界面 → 下拉框空着 → **什么都做不了**。

        这不是用户的错，是我们的引导错了。此刻唯一有意义的动作是
        「装客户端」或「指定本机 katago」，所以把它做成主按钮。
        同时把「完成」改成「先这样吧」，语义上不再诱导他去点。

        条件用 `n_ok == 0`（可用且已勾的数量），不是 `len(merged)==0`：
        有可能列表里有东西但**全都不可用**，那种情况一样需要引导。
        """
        try:
            if n_ok == 0:
                if not self._manual_promoted:
                    self._manual_promoted = True
                    self.btn_manual.configure(style="Go.TButton")
                    self.btn_ok.configure(style="TButton",
                                          text=("先这样吧" if self.first_run
                                                else "完成"))
                # 提示语换成"该怎么办"，别只说"没找到"
                #
                # ⚠️ 不写「点右边」——按钮的左右位置随 pack 顺序变，
                #    写死方位以后改布局就成了错话。
                #    只说「点下面的『手动添加…』」永远对。
                self.prob_lbl.configure(
                    text=("· 当前电脑没找到能用的引擎。"
                          "点下面的「手动添加…」指定你的 katago.exe 和权重；"
                          "或者先装「智子围棋」客户端再点「重新扫描」。"),
                    fg=ACCENT)
            else:
                if self._manual_promoted:
                    self._manual_promoted = False
                    self.btn_manual.configure(style="TButton")
                    self.btn_ok.configure(style="Go.TButton", text="完成")
        except Exception:
            pass

    def _render_registered(self):
        """扫描炸了时的兜底：至少把已登记的引擎列出来"""
        self._render_group("已登记的引擎", "来自 Lizzie 配置文件",
                           self.reg, icon="📋")

    def _render_group(self, title, hint, items, icon=""):
        """画一组。返回其中「可用且已勾选」的条目数"""
        if not items:
            return 0

        head = tk.Frame(self.inner, bg=BG2)
        head.pack(fill="x", padx=14, pady=(14, 2))

        tk.Label(head, text=f"{icon} {title}", bg=BG2, fg=ACCENT2,
                 font=(FONT, 10, "bold")).pack(side="left")
        tk.Label(head, text=f"　{hint}", bg=BG2, fg=FG_DIM,
                 font=(FONT, 8)).pack(side="left")

        # 全选/全不选
        def all_on():
            for e in items:
                k = e.get("key")
                if k in self.vars and self.usable.get(k, (False,))[0]:
                    self.vars[k].set(True)

        def all_off():
            for e in items:
                k = e.get("key")
                if k in self.vars:
                    self.vars[k].set(False)

        btns = tk.Frame(head, bg=BG2)
        btns.pack(side="right")
        tk.Label(btns, text="全选", bg=BG2, fg=FG_DIM, font=(FONT, 8),
                 cursor="hand2").pack(side="left", padx=(0, 4))
        btns.winfo_children()[-1].bind("<Button-1>", lambda _e: all_on())
        tk.Label(btns, text="清空", bg=BG2, fg=FG_DIM, font=(FONT, 8),
                 cursor="hand2").pack(side="left")
        btns.winfo_children()[-1].bind("<Button-1>", lambda _e: all_off())

        n = 0
        for e in items:
            key = e.get("key")
            if not key:
                continue
            ok, reason = self._judge(e)
            before = bool(self.cfg.get("enabled") is None)
            checked = self._initial_checked(e, ok)
            var = tk.BooleanVar(value=checked)
            self.vars[key] = var
            self.usable[key] = (ok, reason)

            row = tk.Frame(self.inner, bg=BG2)
            row.pack(fill="x", padx=20, pady=1)

            cb = tk.Checkbutton(
                row, text=e.get("name") or key, variable=var,
                bg=BG2, fg=FG if ok else FG_DIM,
                selectcolor=BG3, activebackground=BG2,
                activeforeground=FG, font=(FONT, 9),
                anchor="w", bd=0, highlightthickness=0,
                state="normal" if ok else "disabled")
            cb.pack(side="left")

            note = e.get("note") or ""
            if not ok and reason:
                note = reason
            if note:
                tk.Label(row, text=note, bg=BG2,
                         fg=FG_DIM if ok else RED,
                         font=(FONT, 8)).pack(side="left", padx=(10, 0))
            if ok and checked:
                n += 1

        # 组尾细线
        tk.Frame(self.inner, bg=BORDER, height=1).pack(
            fill="x", padx=14, pady=(8, 0))
        return n

    def _initial_checked(self, e, ok):
        """这一项默认勾不勾

        ⚠️ 三种情况分开处理（2026-10-23）
        ----------------------------------------------------
        ① 用户以前选过（cfg['enabled'] 是 list）→ **严格按他的选择**
           他上次没勾的，这次也不勾。不能因为「检测到了」就替他勾上。
        ② 用户没选过（enabled is None）→ 能用的全勾（首次体验）
        ③ 不可用（没登录 / 路径没了）→ 无论哪种都不勾
        """
        if not ok:
            return False
        en = self.cfg.get("enabled")
        if isinstance(en, list):
            return e.get("key") in en
        return True

    def _judge(self, e):
        """这一项现在能不能用。返回 (ok, 原因)"""
        kind = e.get("kind")
        if kind == "cloud":
            # 云端：靠 detect 给的 usable（等价于「登录 token 读到了」）
            #
            # ⚠️⚠️ 这里原有一个「默认放行」的坑（2026-10-23 裸机实测抓到）
            # ----------------------------------------------------
            # 原代码：
            #     u = e.get("usable")
            #     if u is None: u = self._cloud_usable.get(key)
            #     if u is False: return False, "未登录智子账号"
            #     return True, ""
            # `u is None` 时（merged 去重后保留了 Lizzie 那份，
            # 它本来就没有 usable 字段）→ 落到 `return True`。
            #
            # 后果（新电脑上就是 100% 复现）：
            #   · 界面上「智子云 未检测到」（连 zz-ikatago.exe 都没有）
            #   · 9 个云端项**全部打勾、全部可点**
            #   · 用户点「完成」→ 这 9 项被写进 settings.json
            #   · 主界面下拉框里 9 个全是连不上的
            #   · 点「开启引擎」→ 报「没有找到智子云客户端」
            # 也就是：**软件在明确知道自己不能用的前提下，把所有坏引擎
            # 都推荐给了用户**。对新手来说这是最糟的第一次体验。
            #
            # 现在改成「未知 = 不可用，但说明白为什么」。
            # 判定优先级：
            #   ① 条目自己带 usable（detect 直接产出的那份）
            #   ② 按 key 查 detect 的原始结果
            #   ③ 都没有 → 看这台机器到底有没有 zz-ikatago.exe
            #      （没有 = 明确不可用且能说清原因；有但没验证 = 放行，
            #        因为 check_token=False 只是"没验"，不代表"不行"）
            u = e.get("usable")
            if u is None:
                u = (getattr(self, "_cloud_usable", {}) or {}).get(e.get("key"))
            if u is False:
                return False, "未登录智子账号"
            if u is True:
                return True, ""
            # u 仍是 None → 谁也没给结论。自己判断一次
            try:
                import engine_detect as ED
                ik = None
                for p in ED._ikatago_candidates():
                    if p and os.path.isfile(p):
                        ik = p
                        break
            except Exception:
                ik = None
            if not ik:
                return False, "没装智子客户端"
            # 客户端在，只是这次启动没读 token（首次扫描不读，省 1 秒）
            # → 允许勾选，真连不上时点开启会有明确提示
            return True, ""
        # 本机 / 死活：三个路径都得在
        exe = e.get("exe") or ""
        model = e.get("model") or ""
        cfg = e.get("cfg") or ""
        if exe and not os.path.isfile(exe):
            return False, f"找不到 {os.path.basename(exe)}"
        if model and not os.path.isfile(model):
            return False, f"找不到权重 {os.path.basename(model)}"
        if cfg and not os.path.isfile(cfg):
            return False, "找不到 analysis 配置"
        if not exe:
            return False, "缺少 katago.exe"
        return True, ""

    # ------------------------------------------------------------ 动作

    def _on_manual(self):
        """手动添加：选 katago.exe + 权重 → 造一条引擎条目

        ⚠️ 为什么要独立于自动检测（2026-10-23）
        ----------------------------------------------------
        自动检测只扫「已知路径清单」，用户把 katago 装在 D:\\tools\\kata\\
        这种位置就扫不到。没有这个入口，那些用户就彻底用不了本机引擎。
        """
        exe = filedialog.askopenfilename(
            parent=self.win, title="选 katago.exe（Windows 版）",
            filetypes=[("可执行文件", "*.exe"), ("全部", "*.*")])
        if not exe:
            return
        model = filedialog.askopenfilename(
            parent=self.win, title="选权重文件（*.bin.gz）",
            initialdir=os.path.dirname(exe),
            filetypes=[("KataGo 权重", "*.bin.gz *.txt.gz"),
                       ("全部", "*.*")])
        if not model:
            return

        try:
            import engine_detect as ED
            cfg = ED._cfg_for(os.path.dirname(exe))
            if not cfg:
                messagebox.showwarning(
                    "缺少配置文件",
                    "没找到 analysis 配置（.cfg）。\n\n"
                    "请把随软件的 engine/analysis_fast.cfg 放到 "
                    "katago.exe 同目录，或重新安装完整版。",
                    parent=self.win)
                return
            e = ED._entry_local(
                exe, model, cfg,
                f"本机 · {os.path.basename(model).split('.')[0]}",
                "手动添加", kind=None)
            e["src"] = "manual"
        except Exception as ex:
            messagebox.showerror("添加失败",
                                 f"{type(ex).__name__}: {ex}",
                                 parent=self.win)
            return

        # 加进「手动添加」组，勾上
        self.cfg.setdefault("_manual", []).append(e)
        if not hasattr(self, "_manual_items"):
            self._manual_items = []
        # 去重（同一个 exe+model 不重复加）
        if any(m.get("key") == e["key"] for m in self._manual_items):
            messagebox.showinfo("已经加过", "这个组合已经在列表里了。",
                                parent=self.win)
            return
        self._manual_items.append(e)
        self.det = self.det or {}
        self.det["locals"] = (self.det.get("locals") or []) + [e]
        self._render()

    def _collect(self):
        """把界面状态收成设置 dict"""
        enabled = [k for k, v in self.vars.items() if v.get()]
        extra = list(getattr(self, "_manual_items", []) or [])
        # 手动添加但没勾的，不进 extra（免得列表里有一堆用不上的）
        extra = [e for e in extra if e.get("key") in set(enabled)]
        return {"enabled": enabled, "extra": extra}

    def _on_ok(self):
        got = self._collect()
        # ⚠️⚠️ 一个可用引擎都没有 → 拦住，明确让他去装（2026-10-23 老板要求）
        # ----------------------------------------------------
        # 老板原话：「就是如果没有智子围棋和本机katago的话，
        #             就提示用户去安装之后再使用」
        #
        # 老行为是弹一个 askyesno「仍然保存吗？」—— 这等于**允许**他
        # 带着空配置进主界面。进去之后：下拉框空的、开启键点不动、
        # 日志区一行「没有可用引擎」，而他完全不知道该干什么。
        # 对一个"熟悉围棋AI"的人也许能自己摸出来，但这属于
        # **把问题丢给用户**，不是软件该有的样子。
        #
        # 新行为：直接弹一段**可操作**的说明（装哪个、去哪装、装完做什么），
        # 然后留在向导里。不写 setup_done，下次启动还会弹，直到配好为止。
        #
        # 两种"没有"要分开说，因为他该做的事不一样：
        #   A. 列表里一项都没有（连 FALLBACK 都没渲染出来）→ 极少见
        #   B. 有项但全部不可用（裸机的典型情况）→ 说清楚装什么
        n_all = len(getattr(self, "usable", {}) or {})
        n_ok = sum(1 for k, (ok, _r) in (getattr(self, "usable", {}) or {}).items()
                   if ok)
        if not got["enabled"] and n_ok == 0:
            self._show_install_help(n_all)
            return
        if not got["enabled"]:
            # 有可用的但用户一个都没勾 —— 这才是"你自己选的"，
            # 他明确知道自己在干什么，允许保存。
            if not messagebox.askyesno(
                    "一个都没勾",
                    "你没有勾选任何引擎，这样无法开始对弈。\n\n"
                    "仍然保存吗？（之后可以在「引擎设置」里再勾）",
                    parent=self.win):
                return
        got["setup_done"] = True
        # 默认引擎：如果以前记的那个还在勾选里就留着
        prev = self.cfg.get("default_key")
        got["default_key"] = (prev if prev in set(got["enabled"])
                              else (got["enabled"][0] if got["enabled"]
                                    else None))
        self.result = got
        self._close()

    def _show_install_help(self, n_all):
        """「这台电脑没有可用引擎」的说明弹窗

        文案原则（照老板的要求）：
          ① 先说结论：现在还不能用
          ② 再说装什么：**二选一**，别让他以为是两个都要装
          ③ 每条都给具体动作（点哪、搜什么）
          ④ 最后说装完怎么办（回来点重新扫描）
        """
        body = (
            "这台电脑上没找到能用的围棋引擎，所以现在还不能开始对弈。\n"
            "\n"
            # ⚠️ 这里不能写 Markdown（`**任意一项**`）（2026-10-23 截图发现）
            # tkinter 的 messagebox 是纯文本，星号会原样显示成
            # 「请先完成下面**任意一项**」，看着像程序没写完。
            # 要强调就用【】或「」，别用 Markdown 语法。
            "请先完成下面【任意一项】，装好后回到这个窗口点「重新扫描」：\n"
            "\n"
            "【方式一 · 用云端算力】需要联网，吃对方的显卡\n"
            "  1. 安装「智子围棋」客户端\n"
            "     （zhizigo.com 下载；装的时候会自带 zz-ikatago.exe）\n"
            "  2. 打开客户端，注册并登录一次账号\n"
            "  3. 回到这里点「重新扫描」，云端那几档就会出现\n"
            "\n"
            "【方式二 · 用本机显卡】不需要联网，要求有独立显卡\n"
            "  1. 下载 KataGo（Windows 版，解压到任意目录）\n"
            "  2. 下载一个权重文件（*.bin.gz，和 katago.exe 放一起）\n"
            "  3. 回到这里点「手动添加…」，依次选 katago.exe 和权重\n"
            "\n"
            "两种都可以装，也可以都装。"
        )
        messagebox.showinfo("还不能开始 · 请先安装引擎", body, parent=self.win)
        # 顺手把「手动添加…」扶正，让他的下一步更好点
        try:
            self._manual_promoted = True
            self.btn_manual.configure(style="Go.TButton")
        except Exception:
            pass

    def _on_cancel(self):
        # 首次运行的「稍后再说」也要标记 setup_done，
        # 否则每次启动都弹（老板会烦）。用户随时能在主界面点「设置」。
        self.result = {"__skipped__": True} if self.first_run else None
        self._close()

    def _close(self):
        try:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")
        except Exception:
            pass
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


def ask(root, reg=None, cfg=None, first_run=True, detect=None):
    """便捷入口：弹向导，返回要保存的 dict（None = 用户取消）

    ⚠️ 调用方拿到 dict 后**必须**自己调 settings.save()。
       向导不写盘 —— 这样「预演/测试」时不会污染真实配置。

    ⚠️ detect 传进来就复用，不重复扫描（2026-10-23）
    ----------------------------------------------------
    主界面启动时已经扫过一次（还做了 merged() 合并）。向导再扫一遍，
    既慢（读 token 要起子进程）又可能因为两次结果不同导致 key 对不上。
    所以主界面把 `self.detect_res` 传进来，向导直接用。
    """
    w = SetupWizard(root, reg=reg, cfg=cfg, first_run=first_run,
                    detect=detect)
    try:
        root.wait_window(w.win)
    except Exception:
        pass
    return w.result


if __name__ == "__main__":
    # 单独跑：只弹向导，不启动主程序（方便调界面）
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import engines as REG
    import settings as ST

    r = tk.Tk()
    r.withdraw()
    try:
        reg = REG.registry()
    except Exception:
        reg = []
    cfg, _ = ST.load()
    res = ask(r, reg=reg, cfg=cfg)
    print("向导返回:", res)
    r.destroy()
