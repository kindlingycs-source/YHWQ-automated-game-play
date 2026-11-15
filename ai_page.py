# -*- coding: utf-8 -*-
"""AI 托管页 —— 自然语言指挥野狐的对话框 + 任务/策略面板

老板 2026-11-08 要求：
  · 「AI 操控页面要有一个对话框来发出指令」
  · 「AI 操控页面和自动对弈页面是两个不同的页面」
  · 「用户首次进去要有一个配置 API 的页面，推荐火山方舟」
  · 固定任务/定时任务、押分、聊天都用一句话说

页面结构
------------------------------------------------------------
    ┌──────────────────────────────────────────────────────────┐
    │ ● 托管状态   · 当前任务/剩余时间        [开始托管][停止] │
    ├───────────────────────────────────┬──────────────────────┤
    │ 对话记录（我 / AI / 聊天）         │ 托管任务             │
    │                                   │  · 连下 3 小时 …     │
    │ 快捷指令：下一盘 / 连下3小时 / 押分│ 当前策略             │
    │ [输入框.......................][发]│  我执黑 · 随机2~4s…  │
    └───────────────────────────────────┴──────────────────────┘

首次进入（没有 api_key）显示「连接大模型」配置页，预填火山方舟 Ark，
支持火山方舟 / DeepSeek / 自定义 OpenAI 兼容三种预设。

本文件只做 UI；所有动作都通过回调交给 gui.App（它再交给 AIAgent）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#14181e"
BG2 = "#1e242c"
BG3 = "#242b34"
FG = "#e9edf2"
DIM = "#8b94a1"
ACCENT = "#d9ad72"
GREEN = "#5fbf74"
RED = "#e07070"
BLUE = "#6fa8dc"
LINE = "#2a313b"
CHIP = "#232a33"
FONT = "Microsoft YaHei UI"

ARK_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_MODEL = "doubao-seed-1-6-vision-250815"
DEEPSEEK_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

QUICK = [
    ("下一盘·聊天", "下一盘棋，边下边聊天，注意聊天间隔"),
    ("连下 3 小时", "连续匹配友谊赛 3 小时，尽量接 9 段左右的对手"),
    ("每天 20 点", "每天 20:00 开始下两盘友谊赛"),
    ("按赔率押分", "根据当前胜率和赔率帮我选最优的押分，单注不超过 1 万"),
    ("现在状态", "现在什么状态？"),
    ("暂停/继续", "暂停"),
]

WELCOME_TEXT = """已连上大模型。我是野狐围棋专用操作员，能：
· 开局、接邀请、连续匹配友谊赛 / 升降级
· 下棋：选引擎、执黑执白、规则、贴目、思考时间；暂停/继续/停止
· 在大厅 / 对局室 / 观战室看多人聊天，回复别人、评论棋局
  （野狐里的聊天不限话题；你和我对话只限野狐和围棋）
· 按胜率+赔率押分（受单注和日限约束）
· 定时/固定任务：「连下 3 小时」「每天 20:00 下两盘」「下一盘并聊天半小时」
· 用一句话改策略和聊天风格（谦虚型/骄傲型/阴阳怪气型/趾高气扬型/吹牛型……）

点上方「AI 能做什么」看完整指令范围和示例。直接说事就行，比如「连下 3 小时」。"""


class AIPage:
    def __init__(self, app, parent, *, on_send=None, on_start=None,
                 on_stop=None, on_save_api=None, on_test_api=None,
                 on_cancel_task=None):
        self.app = app
        self.parent = parent
        self.on_send = on_send
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_save_api = on_save_api
        self.on_test_api = on_test_api
        self.on_cancel_task = on_cancel_task
        self.messages = []
        self._setup_mode = False
        self._test_thread = None
        self._build_main()
        self._build_setup()
        self.show_main() if self._api_ready() else self.show_setup()

    # ------------------------------------------------------------ 工具
    def _api_ready(self):
        try:
            return bool(getattr(self.app, "ai", None)
                        and self.app.ai.api_ready)
        except Exception:
            return False

    def _ai(self):
        return getattr(self.app, "ai", None)

    def _section(self, parent, text):
        return ttk.Label(parent, text=text, style="Field.TLabel")

    def _rule(self, parent, pady=6):
        tk.Frame(parent, height=1, bg=LINE).pack(fill="x", pady=pady)

    # ============================================================ 主页面
    def _build_main(self):
        self.main_frame = ttk.Frame(self.parent, padding=(14, 8, 14, 10))
        root = self.main_frame

        # ---- 顶部状态条 ----
        card = tk.Frame(root, bg=BG2, highlightbackground=LINE,
                        highlightthickness=1)
        card.pack(fill="x", pady=(0, 8))
        left = tk.Frame(card, bg=BG2)
        left.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        row = tk.Frame(left, bg=BG2)
        row.pack(fill="x")
        self.dot = tk.Canvas(row, width=10, height=10, bg=BG2,
                             highlightthickness=0)
        self._dot_id = self.dot.create_oval(1, 1, 9, 9, fill=DIM, outline="")
        self.dot.pack(side="left", pady=(2, 0))
        self.state_lbl = tk.Label(row, text="待命", bg=BG2, fg=FG,
                                  font=(FONT, 11, "bold"), anchor="w")
        self.state_lbl.pack(side="left", padx=(6, 0))
        self.api_lbl = tk.Label(row, text="", bg=BG2, fg=DIM,
                                font=(FONT, 9), anchor="w")
        self.api_lbl.pack(side="left", padx=(12, 0))
        self.btn_help = tk.Label(row, text="AI 能做什么", bg=BG2, fg=ACCENT,
                                 font=(FONT, 9), cursor="hand2")
        self.btn_help.pack(side="left", padx=(12, 0))
        self.btn_help.bind("<Button-1>", lambda e: self._show_help())
        self.btn_help.bind("<Enter>", lambda e: self.btn_help.configure(
            fg="#f2ddb6"))
        self.btn_help.bind("<Leave>", lambda e: self.btn_help.configure(
            fg=ACCENT))
        self.task_lbl = tk.Label(left, text="当前没有托管任务", bg=BG2, fg=DIM,
                                 font=(FONT, 10), anchor="w")
        self.task_lbl.pack(fill="x", pady=(4, 0))

        right = tk.Frame(card, bg=BG2)
        right.pack(side="right", padx=10)
        self.btn_start = ttk.Button(right, text="▶  开始托管",
                                    style="Go.TButton", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(right, text="■  停止", style="Stop.TButton",
                                   command=self._stop)
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_stop.configure(state="disabled")

        # ---- 主体：左对话 / 右任务 ----
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)

        leftcol = ttk.Frame(body)
        leftcol.pack(side="left", fill="both", expand=True, padx=(0, 10))

        head = ttk.Frame(leftcol)
        head.pack(fill="x")
        self._section(head, "对话").pack(side="left")
        self.ai_note = tk.Label(head, text="只处理野狐与围棋相关的事",
                                bg=BG, fg=DIM, font=(FONT, 9))
        self.ai_note.pack(side="right")

        wrap = tk.Frame(leftcol, bg="#12161b", highlightbackground=LINE,
                        highlightthickness=1)
        wrap.pack(fill="x", pady=(4, 6))
        self.txt = tk.Text(wrap, width=1, height=10, bg="#12161b", fg="#c7d0da",
                           bd=0, highlightthickness=0, font=(FONT, 10),
                           padx=10, pady=8, wrap="word", state="disabled",
                           selectbackground="#3a4656")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview,
                           style="Dark.Vertical.TScrollbar")
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("user", foreground=ACCENT,
                               font=(FONT, 10, "bold"))
        self.txt.tag_configure("assistant", foreground=FG)
        self.txt.tag_configure("chat", foreground=GREEN)
        self.txt.tag_configure("system", foreground=DIM, font=(FONT, 9))
        self.txt.tag_configure("err", foreground=RED)

        # 对话区不再撑满整页（老板 2026-11-08：「对话框有点大」）。
        # 下面的留白放一段**看得清**的说明，字号 10，不再是一行小灰字。
        tipwrap = tk.Frame(leftcol, bg=BG)
        tipwrap.pack(fill="both", expand=True, pady=(0, 6))
        tip = tk.Message(
            tipwrap,
            text=("AI 在野狐大厅 / 对局室 / 观战室能看到多人聊天，"
                  "会回复别人、评论棋局；野狐里的聊天不限话题。\n"
                  "你与 AI 的指令只处理野狐和围棋。\n"
                  "常用：连下 3 小时 · 每天 20:00 下两盘 · 按赔率押分 · "
                  "暂停 / 继续 · 现在什么状态\n"
                  "改聊天风格：说「聊天风格改成谦虚型 / 骄傲型 / 阴阳怪气型 / "
                  "趾高气扬型 / 吹牛型」即可。"),
            bg=BG, fg="#aeb8c4", font=(FONT, 10), justify="left",
            anchor="nw", width=520, padx=2, pady=4)
        tip.pack(fill="both", expand=True)

        chips = tk.Frame(leftcol, bg=BG)
        chips.pack(fill="x", pady=(0, 6))
        for label, text in QUICK:
            b = tk.Label(chips, text=label, bg=CHIP, fg="#cfd7e2",
                         font=(FONT, 9), padx=8, pady=3, cursor="hand2")
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda e, t=text: self._fill_input(t))
            b.bind("<Enter>", lambda e, w=b: w.configure(bg="#2f3843"))
            b.bind("<Leave>", lambda e, w=b: w.configure(bg=CHIP))

        inp = tk.Frame(leftcol, bg=BG2, highlightbackground=LINE,
                       highlightthickness=1)
        inp.pack(fill="x")
        self.entry = tk.Text(inp, height=3, width=1, bg=BG2, fg=FG,
                             insertbackground=FG, bd=0, highlightthickness=0,
                             font=(FONT, 11), padx=8, pady=6, wrap="word")
        self.entry.pack(side="left", fill="both", expand=True)
        self.entry.bind("<Return>", self._on_return)
        self.btn_send = tk.Button(inp, text="发送", command=self._send,
                                  bg=ACCENT, fg="#1a1408",
                                  activebackground="#e8c188",
                                  activeforeground="#1a1408", relief="flat",
                                  bd=0, highlightthickness=0, cursor="hand2",
                                  font=(FONT, 11, "bold"), padx=16)
        self.btn_send.pack(side="right", padx=(4, 6), pady=8)
        hint = tk.Label(leftcol, text="Enter 发送 · Shift+Enter 换行 · "
                                     "F1 能力说明 · F2 切页 · "
                                     "自然语言即可，比如「再下三盘」「停」",
                        bg=BG, fg=DIM, font=(FONT, 9), anchor="w")
        hint.pack(fill="x", pady=(4, 0))

        # ---- 右侧 ----
        side = ttk.Frame(body, width=320)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        thead = ttk.Frame(side)
        thead.pack(fill="x")
        self._section(thead, "托管任务").pack(side="left")
        self.lbl_task_count = tk.Label(thead, text="0", bg=BG, fg=DIM,
                                       font=(FONT, 9))
        self.lbl_task_count.pack(side="right")
        twrap = tk.Frame(side, bg="#12161b", highlightbackground=LINE,
                         highlightthickness=1)
        twrap.pack(fill="x", pady=(4, 4))
        self.task_txt = tk.Text(twrap, height=8, width=1, bg="#12161b",
                                fg="#c7d0da", bd=0, highlightthickness=0,
                                font=(FONT, 9), padx=8, pady=6, wrap="word",
                                state="disabled")
        tsb = ttk.Scrollbar(twrap, orient="vertical",
                            command=self.task_txt.yview,
                            style="Dark.Vertical.TScrollbar")
        self.task_txt.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.task_txt.pack(side="left", fill="both", expand=True)
        self.task_txt.tag_configure("active", foreground=GREEN)
        self.task_txt.tag_configure("pending", foreground=DIM)
        self.task_txt.tag_configure("done", foreground="#6b7480")
        self.btn_cancel = ttk.Button(side, text="取消当前任务",
                                     style="Ghost.TButton",
                                     command=self._cancel_task)
        self.btn_cancel.pack(fill="x", pady=(0, 8))

        phead = ttk.Frame(side)
        phead.pack(fill="x")
        self._section(phead, "当前策略").pack(side="left")
        btn_api = tk.Label(phead, text="API 设置", bg=BG, fg=DIM,
                           font=(FONT, 9), cursor="hand2")
        btn_api.pack(side="right")
        btn_api.bind("<Button-1>", lambda e: self.show_setup())
        btn_api.bind("<Enter>", lambda e: btn_api.configure(fg=ACCENT))
        btn_api.bind("<Leave>", lambda e: btn_api.configure(fg=DIM))
        pwrap = tk.Frame(side, bg="#12161b", highlightbackground=LINE,
                         highlightthickness=1)
        pwrap.pack(fill="x", pady=(4, 6))
        self.policy_txt = tk.Text(pwrap, height=12, width=1, bg="#12161b",
                                  fg="#c7d0da", bd=0, highlightthickness=0,
                                  font=(FONT, 9), padx=8, pady=6, wrap="word",
                                  state="disabled")
        psb = ttk.Scrollbar(pwrap, orient="vertical",
                            command=self.policy_txt.yview,
                            style="Dark.Vertical.TScrollbar")
        self.policy_txt.configure(yscrollcommand=psb.set)
        psb.pack(side="right", fill="y")
        self.policy_txt.pack(side="left", fill="both", expand=True)

        tk.Label(side, text="与 AI 对话只限野狐/围棋；\n"
                           "野狐内聊天不限话题。\n"
                           "提示词/白名单内置，改不了。",
                 bg=BG, fg=DIM, font=(FONT, 9), justify="left",
                 anchor="w", wraplength=260).pack(fill="x")

    # ============================================================ 首次配置
    def _build_setup(self):
        self.setup_frame = ttk.Frame(self.parent, padding=(14, 10, 14, 10))
        box = tk.Frame(self.setup_frame, bg=BG2, highlightbackground=LINE,
                       highlightthickness=1)
        # 配置卡不再撑满整页（老板 2026-11-08：「页面也有点大」）：
        # 只占内容高度，下面留深色背景，视觉上轻很多。
        box.pack(fill="x")
        inner = tk.Frame(box, bg=BG2, padx=28, pady=24)
        inner.pack(fill="x")

        tk.Label(inner, text="连接大模型", bg=BG2, fg=FG,
                 font=(FONT, 18, "bold"), anchor="w").pack(fill="x")
        tk.Label(inner, text="首次使用先配置 API。推荐火山方舟（豆包视觉），"
                             "能看截图找按钮、读赔率。",
                 bg=BG2, fg=DIM, font=(FONT, 10), anchor="w").pack(
            fill="x", pady=(6, 14))

        prov = tk.Frame(inner, bg=BG2)
        prov.pack(fill="x", pady=(0, 10))
        tk.Label(prov, text="服务商", bg=BG2, fg=ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w")
        row = tk.Frame(prov, bg=BG2)
        row.pack(fill="x", pady=(6, 0))
        self.prov_var = tk.StringVar(value="ark")
        for key, label in (("ark", "火山方舟（推荐）"),
                           ("deepseek", "DeepSeek"),
                           ("custom", "OpenAI 兼容 / 自定义")):
            rb = tk.Radiobutton(row, text=label, value=key,
                                variable=self.prov_var, bg=BG2, fg=FG,
                                selectcolor=BG3, activebackground=BG2,
                                activeforeground=FG, font=(FONT, 10),
                                command=self._on_provider)
            rb.pack(side="left", padx=(0, 14))

        def field(label, var, show=None, width=54):
            f = tk.Frame(inner, bg=BG2)
            f.pack(fill="x", pady=(6, 0))
            tk.Label(f, text=label, bg=BG2, fg=DIM, font=(FONT, 9),
                     anchor="w").pack(fill="x")
            e = tk.Entry(f, textvariable=var, bg="#12161b", fg=FG,
                         insertbackground=FG, relief="flat",
                         highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACCENT, font=(FONT, 10), width=width,
                         show=show)
            e.pack(fill="x", ipady=5, pady=(2, 0))
            return e

        self.base_var = tk.StringVar(value=ARK_URL)
        self.model_var = tk.StringVar(value=ARK_MODEL)
        self.key_var = tk.StringVar(value="")
        self.url_entry = field("API Base URL", self.base_var)
        self.model_entry = field("模型名（对话 / 视觉理解，不是图片生成）",
                                 self.model_var)
        tk.Label(inner, text="填控制台的 Model ID 或接入点 ID（ep-...）；"
                             "必须是「对话/视觉理解」模型，Seedream 图片生成不行。",
                 bg=BG2, fg=DIM, font=(FONT, 9), anchor="w",
                 justify="left").pack(fill="x", pady=(4, 0))
        self.vision_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            inner, text="模型支持视觉（DeepSeek-V4.1-Flash 等纯文本模型请取消勾选）",
            variable=self.vision_var, bg=BG2, fg=FG, selectcolor=BG3,
            activebackground=BG2, activeforeground=FG,
            font=(FONT, 9), anchor="w").pack(fill="x", pady=(4, 0))
        key_row = tk.Frame(inner, bg=BG2)
        key_row.pack(fill="x", pady=(6, 0))
        tk.Label(key_row, text="API Key（用 Windows DPAPI 加密存本机，"
                              "不写日志）", bg=BG2, fg=DIM,
                 font=(FONT, 9), anchor="w").pack(fill="x")
        self.key_entry = tk.Entry(key_row, textvariable=self.key_var,
                                  bg="#12161b", fg=FG, insertbackground=FG,
                                  relief="flat", highlightthickness=1,
                                  highlightbackground=LINE,
                                  highlightcolor=ACCENT, font=(FONT, 10),
                                  show="•")
        self.key_entry.pack(fill="x", ipady=5, pady=(2, 0))

        self.setup_status = tk.Label(inner, text="", bg=BG2, fg=DIM,
                                     font=(FONT, 10), anchor="w",
                                     justify="left")
        self.setup_status.pack(fill="x", pady=(8, 0))

        btnrow = tk.Frame(inner, bg=BG2)
        btnrow.pack(fill="x", pady=(12, 0))
        self.btn_test = ttk.Button(btnrow, text="测试连接", style="Ghost.TButton",
                                   command=self._test_api)
        self.btn_test.pack(side="left")
        self.btn_save = ttk.Button(btnrow, text="保存并开始", style="Go.TButton",
                                   command=self._save_api)
        self.btn_save.pack(side="left", padx=(10, 0))
        btn_skip = tk.Label(btnrow, text="稍后配置，先手动下棋", bg=BG2,
                            fg=DIM, font=(FONT, 9), cursor="hand2")
        btn_skip.pack(side="right")
        btn_skip.bind("<Button-1>", lambda e: self.show_main())
        btn_skip.bind("<Enter>", lambda e: btn_skip.configure(fg=ACCENT))
        btn_skip.bind("<Leave>", lambda e: btn_skip.configure(fg=DIM))

        tk.Label(inner, text="说明：AI 只处理野狐/围棋相关指令（开局、接邀请、"
                             "下棋、聊天、观战、押分、定时任务、状态问答）。\n"
                             "系统提示词与范围限制内置在程序里，对话中改不了。\n"
                             "仅供学习交流，禁止代打、刷屏、嘲讽对手；使用风险自负。",
                 bg=BG2, fg=DIM, font=(FONT, 9), justify="left",
                 anchor="w").pack(fill="x", pady=(14, 0))

    def _on_provider(self):
        p = self.prov_var.get()
        if p == "ark":
            self.base_var.set(ARK_URL)
            self.model_var.set(ARK_MODEL)
        elif p == "deepseek":
            self.base_var.set(DEEPSEEK_URL)
            self.model_var.set(DEEPSEEK_MODEL)
        # custom 不动，保留用户输入

    # ============================================================ 显隐
    def _show_help(self):
        """能力/指令范围说明窗（用户随时能看）"""
        win = getattr(self, "_help_win", None)
        if win is not None:
            try:
                win.deiconify()
                win.lift()
                return
            except Exception:
                self._help_win = None
        try:
            import ai_skill
            text = ai_skill.HELP_TEXT
        except Exception:
            text = "AI 能做什么：开局、接邀请、下棋、聊天、观战、押分、定时任务、查状态。"
        win = tk.Toplevel(self.parent)
        win.title("AI 能做什么 · 指令范围")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.geometry("1000x760")
        win.minsize(760, 540)
        self._help_win = win

        head = tk.Frame(win, bg=BG2)
        head.pack(fill="x")
        tk.Label(head, text="AI 能做什么 · 指令范围", bg=BG2, fg=FG,
                 font=(FONT, 11, "bold"), anchor="w").pack(
            side="left", padx=14, pady=8)
        close_lbl = tk.Label(head, text="×", bg=BG2, fg=DIM,
                             font=(FONT, 12), cursor="hand2")
        close_lbl.pack(side="right", padx=(0, 14))
        close_lbl.bind("<Button-1>", lambda e: self._close_help())

        wrap = tk.Frame(win, bg="#12161b")
        wrap.pack(fill="both", expand=True, padx=12, pady=10)
        txt = tk.Text(wrap, bg="#12161b", fg="#c7d0da", bd=0,
                      highlightthickness=0, font=(FONT, 10), padx=12, pady=10,
                      wrap="char", spacing1=3, spacing3=3)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview,
                           style="Dark.Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        tk.Label(win, text="提示：聊天风格可以直接说「聊天风格改成吹牛型」；"
                          "规则与范围改不了。", bg=BG, fg=DIM,
                 font=(FONT, 9), anchor="w").pack(fill="x", padx=14, pady=(0, 8))
        win.protocol("WM_DELETE_WINDOW", self._close_help)

    def _close_help(self):
        try:
            if self._help_win is not None:
                self._help_win.destroy()
        except Exception:
            pass
        self._help_win = None

    def show_main(self):
        self._setup_mode = False
        try:
            self.setup_frame.pack_forget()
            self.main_frame.pack(fill="both", expand=True)
        except Exception:
            pass
        self.refresh_api()
        self.refresh_policy()
        self.refresh_tasks()
        if self._api_ready() and not self.messages:
            try:
                self.append_message("system", WELCOME_TEXT)
                # 欢迎语比较长，滚到顶部让用户先看到「能做什么」
                self.txt.see("1.0")
            except Exception:
                pass

    def show_setup(self):
        self._setup_mode = True
        try:
            self.main_frame.pack_forget()
            self.setup_frame.pack(fill="both", expand=True)
        except Exception:
            pass
        ai = self._ai()
        if ai is not None:
            try:
                llm = (ai.settings or {}).get("llm") or {}
                if llm.get("base_url"):
                    self.base_var.set(llm["base_url"])
                if llm.get("model"):
                    self.model_var.set(llm["model"])
                if llm.get("api_key"):
                    self.key_var.set(llm["api_key"])
                self.vision_var.set(bool(llm.get("vision", True)))
            except Exception:
                pass

    # ============================================================ 回调
    def _start(self):
        if callable(self.on_start):
            self.on_start()
        elif hasattr(self.app, "ai_start"):
            self.app.ai_start()

    def _stop(self):
        if callable(self.on_stop):
            self.on_stop()
        elif hasattr(self.app, "ai_stop"):
            self.app.ai_stop()

    def _cancel_task(self):
        if callable(self.on_cancel_task):
            self.on_cancel_task()
        elif hasattr(self.app, "ai_cancel_task"):
            self.app.ai_cancel_task()

    def _send(self):
        try:
            text = self.entry.get("1.0", "end-1c").strip()
        except Exception:
            return
        if not text:
            return
        try:
            self.entry.delete("1.0", "end")
        except Exception:
            pass
        if callable(self.on_send):
            self.on_send(text)
        elif hasattr(self.app, "ai_send"):
            self.app.ai_send(text)

    def _on_return(self, ev):
        if ev.state & 0x0001:       # Shift
            return None
        self._send()
        return "break"

    def _fill_input(self, text):
        try:
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", text)
            self.entry.focus_set()
        except Exception:
            pass

    def _save_api(self):
        data = {
            "base_url": self.base_var.get().strip(),
            "model": self.model_var.get().strip(),
            "api_key": self.key_var.get().strip(),
            "vision": bool(self.vision_var.get()),
        }
        if not data["api_key"]:
            self.setup_status.configure(text="请先填 API Key。", fg=RED)
            return
        if callable(self.on_save_api):
            ok, msg = self.on_save_api(data)
        else:
            ok, msg = False, "保存接口未接"
        self.setup_status.configure(text=msg, fg=(GREEN if ok else RED))
        if ok:
            self.show_main()
            self.append_message("system", "API 已保存。现在可以直接用自然语言"
                                          "指挥野狐了。")
            try:
                # 首次欢迎语从顶部开始读；别被「API 已保存」滚到底
                self.txt.see("1.0")
            except Exception:
                pass

    def _test_api(self):
        key = self.key_var.get().strip()
        if not key:
            self.setup_status.configure(text="请先填 API Key。", fg=RED)
            return
        self.setup_status.configure(text="正在测试连接…", fg=ACCENT)
        self.btn_test.configure(state="disabled")
        data = {"api_key": key,
                "base_url": self.base_var.get().strip(),
                "model": self.model_var.get().strip()}
        if callable(self.on_test_api):
            self.on_test_api(data)
        else:
            self.set_test_result(False, "测试接口未接")

    def set_test_result(self, ok, msg):
        try:
            self.btn_test.configure(state="normal")
            self.setup_status.configure(
                text=("连接成功：" + msg) if ok else ("连接失败：" + msg),
                fg=(GREEN if ok else RED))
        except Exception:
            pass

    # ============================================================ 展示
    def refresh_api(self):
        ready = self._api_ready()
        try:
            self.api_lbl.configure(
                text=("API 已连接" if ready else "未配置 API"),
                fg=(GREEN if ready else RED))
        except Exception:
            pass
        return ready

    def refresh_policy(self):
        ai = self._ai()
        try:
            from ai_skill import summarize_policy
            text = summarize_policy(ai.policy_snapshot() if ai else {})
        except Exception:
            text = "（暂无策略）"
        try:
            self.policy_txt.configure(state="normal")
            self.policy_txt.delete("1.0", "end")
            self.policy_txt.insert("1.0", text)
            self.policy_txt.configure(state="disabled")
        except Exception:
            pass

    def refresh_tasks(self):
        ai = self._ai()
        try:
            snap = ai.task_snapshot() if ai else {"tasks": [], "active": None}
        except Exception:
            snap = {"tasks": [], "active": None}
        tasks = snap.get("tasks") or []
        try:
            self.task_txt.configure(state="normal")
            self.task_txt.delete("1.0", "end")
            if not tasks:
                self.task_txt.insert("end", "还没有任务。\n在左边说一句就行，"
                                            "例如「连下 3 小时」。", "pending")
            for t in tasks[-10:]:
                tag = {"active": "active", "pending": "pending",
                       "done": "done"}.get(t.get("status"), "pending")
                self.task_txt.insert("end", _task_line(t) + "\n\n", tag)
            self.task_txt.configure(state="disabled")
            self.lbl_task_count.configure(text=str(
                sum(1 for t in tasks if t.get("status") == "active")))
        except Exception:
            pass
        self._refresh_task_line(snap)

    def _refresh_task_line(self, snap):
        active = None
        for t in snap.get("tasks") or []:
            if t.get("id") == snap.get("active"):
                active = t
                break
        try:
            self.task_lbl.configure(
                text=("当前任务：" + _task_line(active, short=True))
                if active else "当前没有托管任务")
        except Exception:
            pass

    def set_state(self, payload):
        state = (payload or {}).get("state") or "idle"
        text = (payload or {}).get("text") or "待命"
        dot = {"idle": DIM, "running": GREEN, "paused": ACCENT,
               "thinking": BLUE, "error": RED}.get(state, DIM)
        try:
            self.state_lbl.configure(text=text, fg=FG)
            self.dot.itemconfigure(self._dot_id, fill=dot)
        except Exception:
            pass
        try:
            running = state in ("running", "paused", "thinking")
            self.btn_start.configure(state=("disabled" if running else "normal"))
            self.btn_stop.configure(state=("normal" if running else "disabled"))
        except Exception:
            pass

    def append_message(self, role, text, ts=""):
        self.messages.append({"role": role, "text": str(text), "ts": ts})
        del self.messages[:-80]
        try:
            self.txt.configure(state="normal")
            who = {"user": "我", "assistant": "AI", "chat": "对局聊天",
                   "system": "系统", "err": "错误"}.get(role, "AI")
            self.txt.insert("end", f"{who}  {ts}\n", role)
            self.txt.insert("end", f"{text}\n\n", role)
            self.txt.see("end")
            self.txt.configure(state="disabled")
        except Exception:
            pass


# ---------------------------------------------------------------- 纯函数
def _task_line(t, short=False):
    if not t:
        return "无"
    kind = {"duration": "时长", "games": "盘数", "daily": "每天"}.get(
        t.get("type"), t.get("type") or "")
    goal = {"play": "下一盘", "match_friendly": "连续匹配",
            "chat": "聊天", "watch": "观战", "bet": "押分"}.get(
        t.get("goal"), t.get("goal") or "")
    status = {"active": "进行中", "pending": "待触发",
              "done": "已完成"}.get(t.get("status"), t.get("status") or "")
    if t.get("type") == "daily":
        when = f"每天 {t.get('at')}"
    elif t.get("type") == "games":
        when = f"{t.get('count')} 盘"
    else:
        when = f"{t.get('minutes')} 分钟"
    extra = ""
    if t.get("type") == "duration" and t.get("ends_at"):
        import time
        left = max(0, int(t["ends_at"] - time.time()))
        extra = f"（剩 {left // 60}:{left % 60:02d}）"
    if short:
        return f"{goal} · {when} · {status}"
    note = f" · {t['note']}" if t.get("note") else ""
    return f"[{status}] {goal} · {when}{extra}{note}"
