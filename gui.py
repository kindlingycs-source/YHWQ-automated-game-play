#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""野狐自动对弈 · 桌面控制台

给老板双击就用的那个东西。把 fox_auto.py 的命令行参数全搬到了界面上。

设计取舍（为什么用 tkinter 而不是 QT/web）
------------------------------------------------------------
1) 系统里 py -3.14 自带 tkinter（Tk 9.0），不用装任何 GUI 框架；
2) 这是**本机工具**，不是产品。不需要打包、不需要好看，
   需要的是「双击就开、能看到日志、能停」；
3) 关键：numpy/mss/pillow 必须和 GUI 在同一个解释器里
   （读盘靠它们截屏）。tkinter 是标准库，装第三方库只需 pip，
   而 QT/web 方案要额外引依赖，反而多一层坑。

为什么重活全扔线程
------------------------------------------------------------
tkinter 是单线程的，主线程一旦被 `engine.analyze()` 阻塞（云端 6s、
40B 握手 20s），窗口会变成「无响应」白板，Windows 还会弹「未响应」。
所以：GUI 主线程只管画，Auto.run() 跑在 daemon 线程里，
两边只通过 queue.Queue 传消息（tkinter 唯一线程安全的方式）。
"""
import os
import sys

# ⚠️⚠️ DPI 感知必须在这里设 —— 早于 tkinter 和其它模块的 import
# ----------------------------------------------------------------------
# 这是 2026-10-23 查出来的真问题：原来这句写在 main() 里、
# `tk.Tk()` **之后**，而且没设成功也没人知道。
#
# 实际情况是：
#   ① fox_board.py 在模块顶层就调了 SetProcessDpiAwareness(2)；
#   ② 只要 gui.py 里 `import fox_board`（第 40 行左右）先执行，
#      进程就被定成 PER_MONITOR_AWARE；
#   ③ 之后 main() 里那句 SetProcessDpiAwareness(1) 直接返回
#      **0x80070005 (E_ACCESSDENIED)** —— awareness 只能设一次，
#      设过就不许改。所以那句注释「高分屏别糊」从来没生效过。
#
# 一直没出事，是因为 (2) 恰好也是「清晰」的那一档。
# 但这是**靠别人的副作用兜底**：谁把 fox_board 的 DPI 那三行删掉、
# 或者换个只写 (0) 的模块先 import，界面就整体变模糊，
# 而且现象是「文字糊但程序正常」，极难联想到 DPI。
#
# 所以把决定权收到这里：**本文件第一个动作**就定下来，
# 后面任何模块再调都会失败（本来也只能设一次），无害。
#   (2) = per-monitor，多显示器/改缩放时最不容易糊
#   失败就退 (1) system，再失败就算了（老系统没有 shcore）
def _init_dpi():
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()    # 老系统兜底
            except Exception:
                pass
    except Exception:
        pass


_init_dpi()

import math
import queue
import re
import threading
import time
import traceback
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont

import runtime_paths as RP

# ⚠️ HERE 只读资源（打包后 = _MEIPASS）；写日志用 RP.USER_DIR
HERE = RP.HERE
sys.path.insert(0, HERE)

import engines as REG
import fox_board as FB
import fox_auto as FA
import settings as ST

# ⚠️ 本机 HTTP 控制接口（2026-11-05 给 fox-agent 加的）
# 契约：projects/fox-agent/DESIGN.md 第 4 节。
# 单独一个文件、只依赖标准库；这里 import 失败也必须能照常开界面
# （打包漏文件 / 老版本目录）—— 控制接口没有，人工功能不能跟着废。
try:
    import control_server as CS
except Exception:                     # pragma: no cover
    CS = None


# ================================================================ 配色
# 深色 + 围棋的木色点缀。老板天天看棋盘，别用花哨色。
APP_VERSION = "3.2.0"   # 跟 version_info.txt 的两处版本号保持一致


def _integrity_level():
    """当前进程的完整性级别（High=管理员完整令牌 / Medium=普通用户）

    ⚠️ 2026-11-02 现场根因：野狐以管理员(High)启动时，Medium 的 exe
    向它发模拟鼠标会被 Windows UIPI **静默丢弃** —— 连光标都推不过去，
    表现就是「点了没反应、鼠标都没动」。所以每次启动把这一行写进日志，
    以后一眼就能分清是「程序没点」还是「权限不够点不进去」。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        a = ctypes.windll.advapi32
        a.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        a.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        a.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        a.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        h = k.OpenProcess(0x1000, False, k.GetCurrentProcessId())
        if not h:
            return "未知"
        try:
            tok = wintypes.HANDLE()
            if not a.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
                return "未知"
            try:
                size = wintypes.DWORD()
                a.GetTokenInformation(tok, 25, None, 0, ctypes.byref(size))
                buf = ctypes.create_string_buffer(size.value)
                if not a.GetTokenInformation(tok, 25, buf, size, ctypes.byref(size)):
                    return "未知"
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                n = a.GetSidSubAuthorityCount(sid)[0]
                rid = a.GetSidSubAuthority(sid, n - 1)[0]
                return {12288: "High(管理员)", 16384: "System",
                        8192: "Medium(普通)", 2048: "Low"}.get(rid, "RID%d" % rid)
            finally:
                k.CloseHandle(tok)
        finally:
            k.CloseHandle(h)
    except Exception:
        return "未知"

BG = "#101317"          # 主背景（2026-11-01 美化：更深，卡片才浮得起来）
BG2 = "#1a1f26"         # 卡片 / 输入框底色
BG3 = "#242b34"         # 悬停 / 次级卡片
FG = "#e9edf2"          # 主文字
FG_DIM = "#8b94a1"      # 次要文字
ACCENT = "#d9ad72"      # 棋盘木色（主强调色）
ACCENT2 = "#f2ddb6"     # 浅木色（高亮文字）
GREEN = "#5fbf74"       # 正常 / 运行中
RED = "#e07070"         # 错误 / 危险
HDR = "#171d25"         # 顶栏底色
CHIP = "#171c23"        # 状态胶囊底色
LINE = "#3b3223"        # 顶栏下的金色细线
WOOD = "#dfb866"        # 棋盘木色
GRID = "#6f5522"        # 棋盘网格线


# ================================================================ 屏幕适配
def work_area():
    """当前显示器**可用区**（已去掉任务栏），返回 (w, h, x, y)

    ⚠️⚠️ 为什么必须有这个函数（2026-10-23，分发级 bug）
    ----------------------------------------------------
    主窗口原来写死 `geometry("1080x1000") + minsize(1000, 940)`。
    老板的屏幕是 2560x1600 @150% → 逻辑 2560x1600（DPI aware 后不缩放），
    1000 高绰绰有余，所以**在本机永远看不出问题**。

    但别人的机器：
      · 1366x768 笔记本 → 工作区约 1366x728。窗口高 940（minsize）→
        超出屏幕 **212px**，底部「开启引擎」「引擎设置」那一排
        **整个在屏幕外**，鼠标够不到（Windows 也不给拖到屏幕外）。
        用户看到的是「按键不全 / 点不了开始」，而他不知道能改什么。
      · 1920x1080 @125% → 逻辑 1536x864，工作区约 824。
        minsize 940 仍然超出 116px，同样点不到。

    也就是说：**这个软件在相当一部分笔记本上根本没法用**，
    而作者（我）在自己的大屏上永远复现不出来。

    现在一律先问系统要可用区，再按它收敛窗口尺寸。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        r = RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(r), 0):
            w = r.right - r.left
            h = r.bottom - r.top
            if w > 200 and h > 200:
                return w, h, r.left, r.top
    except Exception:
        pass
    # 拿不到就退回 Tk 的屏幕尺寸（含任务栏，宁可保守地留边）
    try:
        import tkinter as _tk
        r = _tk._default_root
        if r is not None:
            return (int(r.winfo_screenwidth()),
                    int(r.winfo_screenheight()) - 80, 0, 0)
    except Exception:
        pass
    return 1920, 1040, 0, 0
BLUE = "#7db2e8"        # 目差
BORDER = "#2a313b"      # 卡片描边


# ================================================================ 贴目
# ⚠️ 贴目为什么需要自己的一套解析/吸附/校验（2026-10-23 实测 Tk 9.0.4）
# ----------------------------------------------------
# 老板要求：「贴目要能自己写数字，最小单位 0.5，而且必须是合法的贴目数字」。
#
# 原来只给了 `ttk.Spinbox(increment=0.5, format="%.1f", from_=-150, to=150)`，
# 以为这就能约束输入 —— **实测一个都约束不住**（_tests/_t51_probe.py）：
#
#     键入 "7.3"  → 框里就是 "7.3"（不吸附到 7.5）
#     键入 "999"  → 框里就是 "999"（不夹到 to=150）
#     键入 "abc"  → 框里就是 "abc"（连"是数字"都不要求）
#
# 根因：`-format` 只作用于「控件自己写回值」那条路（点箭头、内部 set），
# 对**人手键入**一个字符都不管；`-from/-to` 更是只在箭头里当边界用。
# 所以字符过滤（validate="key"）+ 吸附夹紧（失焦/回车/箭头）都得自己做。
KOMI_MIN, KOMI_MAX = -150.0, 150.0
# 引擎实测 ±150 能设、±500 会被拒（"unacceptable komi"），提前在界面挡住
KOMI_STEP = 0.5                     # 围棋贴目本来就是半目单位
KOMI_DEFAULT = 7.5

# 全角 → 半角（老板用中文输入法时经常打出全角数字和句号）
_FW_MAP = str.maketrans("０１２３４５６７８９．－，。",
                        "0123456789.-..")


def komi_norm(s):
    """把可能带全角字符的输入统一成半角数字串"""
    return str("" if s is None else s).translate(_FW_MAP)


def komi_parse(s):
    """宽松解析贴目 → float；解析不出来返回 None

    接受 '7.5' / '7,5'（中文逗号）/ '７．５'（全角）/ '-0.5' / ' 7 '
    拒绝 '' / 'abc' / '7.5.5' / 'inf' / 'nan'
    """
    t = komi_norm(s).replace(",", ".").strip()
    if not t:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def komi_snap(v):
    """按 0.5 吸附并夹进 ±150

    ⚠️ 不用 round()：Python 的 round 是「银行家舍入」，
    round(14.5)==14、round(15.5)==16 —— 同样以 .25 结尾的数，
    一半往下、一半往上，看着像随机。这里统一「离 0 远的半边进位」。
    """
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float(KOMI_DEFAULT)
    if not math.isfinite(v):
        return float(KOMI_DEFAULT)
    v = max(KOMI_MIN, min(KOMI_MAX, v))          # 先夹，避免超大数白算
    n = math.floor(abs(v) / KOMI_STEP + 0.5)
    v = math.copysign(n * KOMI_STEP, v)
    v = round(v, 1)
    return 0.0 if v == 0 else v                  # 消掉 -0.0


def komi_text(v):
    """显示文本：7.5 / -0.5 / 150（不带多余的 .0）"""
    return f"{komi_snap(v):g}"


def komi_key_ok(proposed):
    """键入过程中的字符级校验（ttk.Spinbox 的 validate="key" 回调）

    ⚠️ 只挡「一眼就不是数字」的字符，**不做吸附、也不夹范围**
    ----------------------------------------------------
    ① 老板打 "7" 想改成 "7.5"，中间态就是 "7" —— 这一瞬间要是给他
       吸附成整数或夹成别的值，第二个字符就永远打不进去。
    ② 同理 "-" 是负数打了一半、"150." 是小数打了一半，都必须放行。
    ③ 真正变成「合法贴目」的时机是失焦 / 回车 / 点箭头，
       见 App._commit_komi()。

    返回 False 时 Tk 拒绝这次编辑，控件里保持原样。
    ⚠️ 绝不能在回调里改控件或 var（Tk 会判定「validate 改了自己的值」
       从而**永久关掉** validate，之后什么都能输进去）。
    """
    try:
        s = komi_norm(proposed).replace(",", ".")
        if s == "":
            return True                     # 全删重打
        if len(s) > 6:                      # "-150.0" 正好 6 个字符
            return False
        body = s[1:] if s.startswith("-") else s
        if "-" in body:
            return False
        if body.count(".") > 1:
            return False
        return all(ch.isdigit() or ch == "." for ch in body)
    except Exception:
        return True                         # 校验自己炸了就别挡用户


# ================================================================ 思考时间
# 老板 2026-10-27 要求：
#   「时间也设置也做成可上下滚动也可键入（最小单位0.5，0.5到180之间，
#     固定随机都一样）」
#
# 和贴目同一套毛病、同一套解法（Tk 的 Spinbox 对键入零约束），
# 所以直接复用上面 komi_* 的数学，只是换一套边界。
#
# ⚠️ 为什么单独一套函数而不是给 komi_snap 加参数
# ----------------------------------------------------
# 贴目允许负数（-150~+150），时间不允许（0.5~180）。
# 把「是否允许负」做成参数，两个调用点都得记得传，迟早有一处忘；
# 分开写各自的边界一目了然。共用的只有「半目吸附」那 3 行数学。
THINK_MIN, THINK_MAX = 1.0, 180.0
THINK_STEP = 0.5                 # 随机区间用 0.5 步进（可以抽到小数）
THINK_FIXED_STEP = 1.0           # 固定时间只收整数秒（老板 2026-11-03）
THINK_DEFAULT = 5.0
THINK_MIN_DEFAULT = 1.0          # 思考下限（固定/随机都是 1 秒）
THINK_MAX_DEFAULT = 10.0         # 随机区间上限的默认

# 按计算量落子（老板 2026-11-03）：1~100000 的整数，默认 10000，非法按默认
VISITS_MIN, VISITS_MAX = 1, 100000
VISITS_DEFAULT = 10000
VISITS_STEP = 1000


def time_snap(v, dflt=THINK_DEFAULT, step=THINK_STEP):
    """按 step 吸附并夹进 [1, 180]（固定用 1 秒，随机用 0.5 秒）

    「先夹再吸附」的顺序有讲究：输入 1000 时先夹成 180，
    再吸附还是 180；反过来先吸附成 1000 再夹也是 180 —— 结果一样，
    但先夹可以避免对超大数做无意义的乘除。
    """
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float(dflt)
    if not math.isfinite(v):
        return float(dflt)
    v = max(THINK_MIN, min(THINK_MAX, v))
    # 同 komi_snap：不用 round()（银行家舍入会让 .25 结尾的值一半上一半下）
    n = math.floor(abs(v) / step + 0.5)
    v = math.copysign(n * step, v)
    v = round(v, 1)
    return THINK_MIN if v < THINK_MIN else v     # 吸附后可能掉到 1 以下


def time_text(v, dflt=THINK_DEFAULT, step=THINK_STEP):
    """显示文本：5 / 1.5 / 180（不带多余的 .0）"""
    return f"{time_snap(v, dflt, step):g}"


def visits_snap(v, dflt=VISITS_DEFAULT):
    """计算量：1~100000 的整数；非法/非数字一律按默认 10000（老板 2026-11-03）"""
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError):
        n = VISITS_DEFAULT
    return max(VISITS_MIN, min(VISITS_MAX, n))


def visits_text(v):
    return str(visits_snap(v))


def time_parse(s):
    """解析时间输入 → float；解析不出来返回 None

    比 komi_parse 严格：**不接受负数**（时间没有负的）。
    """
    t = komi_norm(s).replace(",", ".").strip()
    if not t or t.startswith("-"):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def time_key_ok(proposed):
    """时间框的字符级校验（validate="key"）

    和 komi_key_ok 的唯一区别：**不许出现负号**。
    其余规矩完全一样（只能数字和一个小数点、长度上限、中间态放行）。

    ⚠️ 长度上限 6：允许 "180.0" 这种（5 个字符）+ 打一半的 "180.5"。
    """
    try:
        s = komi_norm(proposed).replace(",", ".")
        if s == "":
            return True
        if len(s) > 6:
            return False
        if "-" in s:
            return False                    # 时间不能是负的
        if s.count(".") > 1:
            return False
        return all(ch.isdigit() or ch == "." for ch in s)
    except Exception:
        return True


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self.auto = None
        self.engine = None
        self.running = False
        self.paused = False
        # ---- 悬浮球（2026-11-02 起替掉 ESC）----
        # 对局中主窗口最小化，操作全在这颗球上：
        #   · 开/关引擎   · 暂停/继续   · 最近两行日志
        # 球的创建是懒的（第一次开引擎才建），这里只放状态位。
        self.ball = None
        self._ball_lines = []
        self._ball_visible = False
        # ---- AI 托管（2026-11-08）----
        # 与自动对弈页互斥显示；AI 悬浮球只在「托管中」出现。
        self.ai = None
        self.ai_page = None
        self.ai_ball = None
        self.ai_messages = []
        self._ai_mode_active = False
        self._ai_hover_paused = False
        self._ai_visible = False
        # ---- 会话日志文件 ----
        # 老板报问题时界面早已关掉、日志区的内容也就没了。
        # 从 3.1.0 起同步写一份到 %LOCALAPPDATA%\野狐自动对弈\logs\，
        # 接手的人不用再猜「他到底看到什么错」。
        self._log_fp = None
        self._log_path = None
        try:
            _d = RP.ensure_dirs()
            self._log_path = os.path.join(
                _d, f"gui-{datetime.datetime.now():%Y%m%d}.log")
            self._log_fp = open(self._log_path, "a", encoding="utf-8")
            self._log_fp.write(
                f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                f"启动 v{APP_VERSION} · 权限={_integrity_level()} ===\n")
            self._log_fp.flush()
        except Exception:
            self._log_fp = None
        # ---- 自动更新（2026-11-02）：启动 6 秒后后台查一次 Gitee ----
        # 放 after 里而不是 __init__ 里直接发请求：窗口先出来，检查慢也不挡启动。
        try:
            self.root.after(6000, self._auto_check_update)
        except Exception:
            pass
        # 收工把窗口放回来（下棋时是最小化的，别让老板找不着）
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
            self.root.lift()
        except Exception:
            pass        # 暂停 ≠ 停止：引擎留着，只是不下子
        self._last_move = None
        # 说明标签的**完整**文案（_on_eng 写、_reflow_note 读）
        # 留着全文是为了窗口缩放时能重新截断，见 _reflow_note 的说明
        self._note_full = ""
        self._resize_job = None
        # 信息栏排版模式，由 _fit_board 按「宽度够不够 + 高度够不够」选定：
        #   full    4 行 · 计算量带耗时    —— 最完整（大屏）
        #   short   4 行 · 计算量不带耗时  —— 窄屏
        #   compact 3 行 · 胜率与目差同行  —— 最小屏兜底
        self._info_mode = "full"
        # 兼容旧代码：有些地方还在读 _info_lines（4 / 3）
        self._info_lines = 4

        root.title("野狐自动对弈")
        root.configure(bg=BG)
        self._set_icon(root)
        root.after(80, self._dark_titlebar)
        # ⚠️ 窗口尺寸**按屏幕收敛**，不许写死（2026-10-23 分发级修复）
        # ----------------------------------------------------
        # 原来的两行：
        #     root.geometry("1080x1000")
        #     root.minsize(1000, 940)
        # 在老板的 2560x1600 上完美，但在 1366x768 笔记本上
        # （工作区约 728 高）会超出屏幕 212px —— 底部按钮排整排看不见。
        # 详见 work_area() 的说明。
        #
        # 现在的算法：
        #   ① 问系统要可用区 (aw, ah)
        #   ② 目标宽高各留一点边距（窗口别贴死屏幕边）
        #   ③ minsize 也跟着降，否则用户永远拖不下来
        #   ④ 算出这次能用多大盘面（BOARD_PX），_draw_board 会照它画
        #
        # ⚠️ 高 940 / 宽 1000 是「内容真正需要的最小值」，
        #    在够大的屏幕上就是原样，不改变老板已经调好的观感。
        _MIN_W, _MIN_H = 1000, 940        # 内容最小需求（写死的常量看着刺眼，
        _DEF_W, _DEF_H = 1080, 1000       # 但这两个数字是量出来的，不是拍的）
        aw, ah, ax, ay = work_area()
        # ⚠️ 边距只留 8 / 4（2026-10-23 实测 1366x768 后收紧）
        # ----------------------------------------------------
        # 第一版留「宽 40、高 80」。高 80 是**双重扣减** ——
        # work_area() 已经排除任务栏了，再扣 80 等于白扔 80px，
        # 在 728 高的笔记本上是致命的（盘面被迫压到 220 下限，
        # 信息栏还是只剩 37px）。
        #
        # ⚠️ 高度还要减「外框装饰」（2026-10-23 三次修正）
        # ----------------------------------------------------
        # geometry 的 h 是**客户区**高，而屏幕上看的是**外框**。
        # Windows 10/11 上外框比客户区多 **56px**：
        #   标题栏 32 + 底部隐形边框/阴影 24
        # （实测：客户区 676 → GetWindowRect 732，差 56）
        #
        # 所以可用高度要按 `ah - 56` 算，否则底边必然超出屏幕。
        # 56 是实测值，不同 Windows 版本可能差几像素 ——
        # 下面还有个 _clamp_main_window() 兜底，多出来会被推回去。
        _FRAME = 56
        _maw, _mah = max(760, aw - 8), max(600, ah - _FRAME - 8)

        w = min(_DEF_W, _maw)
        h = min(_DEF_H, _mah)
        # minsize 不许超过屏幕，否则「最小尺寸」比屏幕还大 = 永远超出
        mw, mh = min(_MIN_W, _maw), min(_MIN_H, _mah)
        self._fit = {"area": (aw, ah), "win": (w, h), "min": (mw, mh)}

        # ⚠️ y 用「垂直居中」而不是除以 60（2026-10-23）
        # 原来写的是 `ay + (ah - h) // 60` —— 60 这个数字没有道理
        # （本意大概是"稍微偏上一点"，但除太狠了，等于贴顶）。
        # 窗口矮时贴顶还行，窗口高时会显得很别扭。
        # 改成 /4：居中偏上一点，视觉上更稳（这也是常见做法）。
        #
        # ⚠️ 标题栏：Windows 上 geometry 的 y 指**客户区**，
        # 外框会再往上占掉约 32~45px。所以给 y 留出上限，
        # 保证 h + 标题栏 <= ah。
        _cap = 46                     # 标题栏估算（严格说随主题变）
        _y = ay + max(0, (ah - h - _cap) // 4)
        _y = min(_y, ay + max(0, ah - h - _cap))
        root.geometry(f"{w}x{h}+{max(0, ax + (aw - w) // 2)}+{max(0, _y)}")
        root.minsize(mw, mh)

        # 盘面大小跟着窗口走：窗口矮就得缩盘面，否则 info 会被压扁
        # （实测窗口 900 时 info 只剩 119px，四行文字被裁）
        self.BOARD_PX = self._pick_board_px(h)
        # 小屏要收紧的地方记下来，_build 里用
        self._compact = (w < 1060) or (h < 980)

        # ⚠️ 先读设置，再建界面（2026-10-23）
        # 界面上「勾了哪些引擎 / 默认哪一项 / 思考时间」全依赖它，
        # 建完再读的话要额外走一遍刷新，容易漏控件。
        self.cfg, cfg_note = ST.load()
        # ⚠️ 「跳过」不等于「配好了」（2026-10-23 裸机端到端抓到的 bug）
        # ----------------------------------------------------
        # 原来点「稍后再说」直接写 `setup_done=True`，理由是
        # 「否则每次启动都弹，老板会烦」。
        # 但裸机上这一跳的后果是：下次启动**不再弹向导**，用户落到
        # 一个空下拉框的主界面，点「开启引擎」只弹一段安装说明 ——
        # 而他既不知道从哪进设置，也不记得自己跳过什么。
        # 真正的死锁是：**没引擎却记成"配好了"**。
        #
        # 现在分两个状态：
        #   setup_done=1       真的配好了（勾了至少一个可用引擎）
        #   setup_skipped=1    用户主动跳过，但**没配好**
        # 判据：跳过了但下拉框没有可用引擎 → 下次仍然弹。
        # 配好了（有可用引擎）就真的不弹了，老板不会被烦。
        self._setup_skipped = bool(self.cfg.get("setup_skipped"))
        self._need_setup = not bool(self.cfg.get("setup_done"))

        # 引擎表先读出来，界面要显示
        # ⚠️ 用 engine_detect.merged() 而不是直接用 engines.registry()
        #    （2026-10-23 实测 bug）
        # registry() 只给 Lizzie 配置里那 16 项；而设置向导展示的是
        # detect() 自动扫出来的（认得新用户机器上的 katago + 权重）。
        # 两边的 key 算法不完全一致（比如 local:b18c384 vs
        # local:b18c384nbt），于是向导勾了 8 项、主界面只剩 4 项。
        # merged() 把两边合成**一份**列表，主界面和向导共用，key 天然一致。
        try:
            import engine_detect as ED
            reg_lizzie = REG.registry()
            self.detect_res = ED.detect(check_token=False)
            self.reg = ED.merged(reg_lizzie, self.detect_res,
                                 extra=self.cfg.get("extra"))
            ok, bad = REG.check(self.reg)
            self.reg_ok, self.reg_bad = ok, bad
        except Exception as e:
            # 检测/合并不该阻断启动：退回 Lizzie 那批（老板已调好的）
            self.detect_res = None
            try:
                self.reg = REG.registry()
                ok, bad = REG.check(self.reg)
                self.reg_ok, self.reg_bad = ok, bad
            except Exception as e2:
                self.reg, self.reg_ok, self.reg_bad = [], False, [str(e2)]

        self._check_deps()
        self._build()
        self._bind_keys()
        self._poll()          # 起消息泵
        # ⚠️ 控制接口的共享状态（2026-11-05）
        # 只建对象、不监听端口 —— 监听放在 main() 里（窗口真建好之后），
        # 顺手也保证测试里 `App(root)` 不会去抢 8767。
        self._init_control()
        # ⚠️ AI 托管（2026-11-08）：控制接口建好之后再建 Agent，
        # 这样它拿得到 control_state；页面本身懒建（切到 AI 页才建）。
        self._init_ai()
        # ⚠️ 建完之后立刻用**实测**校准一次盘面（2026-10-23）
        # ----------------------------------------------------
        # _pick_board_px 是在建界面**之前**用常量估的，误差在
        # 不同 DPI/字体下能到几十像素。小屏上这几十像素直接决定
        # 信息栏是"4 行"还是"看不见"。
        # 这里 after(60) 等 Tk 把控件真正布局完（winfo_height 有效），
        # 再用实测值重算一次。60ms 用户完全感知不到，但量到的数字是准的。
        self.root.after(60, self._fit_board)
        # ⚠️ 再用**真实外框矩形**兜一次底（2026-10-23）
        # 客户区尺寸不等于屏幕上看到的尺寸，差 56px 外框装饰。
        # 这道兜底会把窗口真正推进屏幕里，改完还会再校一次盘面。
        self.root.after(200, self._clamp_main_window)
        self._refresh_info(None, "", "B", None)   # 先占位，别留空框
        # 开局先把当前盘面读一次填进信息栏，别让左下角空着
        # （空着的框会让人以为坏了；这里只读不清点，安全）
        self.root.after(120, self._initial_read)
        self.log("准备就绪。① 选引擎　② 确认野狐已开　③ 点「开启引擎」")
        if cfg_note:
            self.log(f"（{cfg_note}）", "dim")
        if not self.reg_ok:
            self.log(f"⚠ 引擎路径自检有 {len(self.reg_bad)} 个问题，"
                     f"选那几项会启动失败", tag="err")
        if not self.deps_ok:
            self.log(f"⚠ 缺少读盘组件：{', '.join(self.deps_missing)}"
                     f"　—— 读盘会失败，请重新安装完整版", tag="err")

        # ⚠️ 首次运行 → 弹设置向导（老板 2026-10-23 要求）
        # ----------------------------------------------------
        # 用 after 而不是直接在 __init__ 里弹：此时主窗口还没 map 出来，
        # 直接开 Toplevel 会定位在屏幕左上角 + 主窗口被它挡住一半 —— 难看。
        # 450ms 足够主窗口画完，向导会自己按主窗口居中。
        #
        # ⚠️ 弹之前先做一次「真有可用引擎吗」的校正（2026-10-23）
        # ----------------------------------------------------
        # 两个方向都要管：
        #   · setup_done 缺失但引擎其实是好的（老用户升级上来的配置）
        #     → 不该无端弹向导打扰他
        #   · setup_done=True 但引擎全都没了（换了电脑、删了 katago）
        #     → 该弹，否则他永远找不到配置入口
        # 判据统一用 `_engine_ready`（会实测 zz-ikatago / 权重是否还在）。
        try:
            n_ok = 0
            for _e in (self.reg or []):
                if self._engine_ready(_e)[0]:
                    n_ok += 1
                    break
            if n_ok > 0:
                self._need_setup = False       # 有能用的 → 别烦用户
            else:
                self._need_setup = True        # 一个都不能用 → 必须提醒
                self.log("⚠ 这台电脑上还没有可用的引擎 —— 请点「引擎设置」配置",
                         "err")
        except Exception:
            pass

        if self._need_setup:
            self.root.after(450, lambda: self.open_setup(first_run=True))

    def _initial_read(self):
        """启动时静默读一次盘面（野狐没开/最小化就安静跳过）"""
        try:
            if not FB.find_hwnd():
                self.log("野狐未启动 —— 打开客户端后点「检查野狐」", "dim")
                return
            try:
                im, title, org = FB.grab()
            except FB.WindowGone:
                # 最小化了 → 安静跳过，**绝不还原窗口**
                self.log("野狐处于最小化 —— 先不读盘（请自己恢复窗口）", "dim")
                return
            if FB.GEOM is None or not FB.GEOM.get("ok"):
                g = FB.calibrate(im)
                # ⚠️ 标定失败要立刻停手（2026-10-22 实测）
                # 大厅/复盘列表里没有棋盘线，calibrate 返回的 dict 里
                # 根本没有 step，硬往下走就是 KeyError: 'step'。
                if not g.get("ok"):
                    self.log(f"初次读盘跳过：{g.get('reason')}", "dim")
                    return
                FB.GEOM = g
            b, conf, last = FB.read_board_full(im, geom=FB.GEOM)
            nb = sum(1 for r in b for v in r if v == 1)
            nw = sum(1 for r in b for v in r if v == 2)
            turn = FA.side_to_move(b, title, last)
            # 走统一刷新路径（_draw_board + _refresh_info 一起做）
            self._last_board = b
            self._last_title = title
            self._last_turn = turn
            self._last_cnt = (nb, nw)
            self._last_mark = last
            self._last_mark_xy = (FB.from_gtp(last[0], len(b))
                                  if last else None)
            self._refresh_board_and_info()
            st = FB.GEOM or {}
            src = f"依标记 {last[0]}" if last else "无标记"
            self.log(f"已读到盘面：黑 {nb} 白 {nw}　该{'黑' if turn=='B' else '白'}走"
                     f"　{src}　step={st.get('step', 0):.2f}", "ok")
        except Exception as e:
            self.log(f"初次读盘跳过：{type(e).__name__}: {e}", "dim")

    # ------------------------------------------------ 构图

    def _check_deps(self):
        """启动自检：读盘三大件能不能 import

        ⚠️ 为什么要单独检（2026-10-23 打包）
        ------------------------------------
        numpy / mss / PIL 在 fox_board.py 里是**函数内 import**（延迟加载），
        PyInstaller 的静态分析扫不到。打包时要是漏了 hiddenimports，
        程序能正常启动、界面全对，一直到点「开始」读盘那一刻才崩 ——
        老板会以为「这软件一下棋就坏」。

        所以在窗口出来前先试一遍，缺了就在日志区红字报警。
        """
        missing = []
        for mod in ("numpy", "mss", "PIL.Image"):
            try:
                __import__(mod)
            except Exception as e:
                missing.append(f"{mod}（{type(e).__name__}）")
        self.deps_ok = not missing
        self.deps_missing = missing

    def _set_icon(self, root):
        """挂软件图标（窗口左上角 + 任务栏）

        ⚠️ 打包后 resources 在 _MEIPASS 里，源码态在项目目录 —— 都走 RP.HERE。
        ⚠️ 为什么同时试 ico 和 png：
            Windows 用 .ico 效果最好（多尺寸，任务栏 16px 档清晰）；
            但 ico 在某些环境（如远程桌面、非 Windows）会抛 TclError，
            所以退到 iconphoto(png)。两个都失败就算了，不能因此启动失败。
        """
        ico = os.path.join(RP.HERE, "app.ico")
        try:
            if os.path.isfile(ico):
                root.iconbitmap(default=ico)
                return
        except Exception:
            pass
        try:
            png = os.path.join(RP.HERE, "icon-256.png")
            if os.path.isfile(png):
                self._icon_img = tk.PhotoImage(file=png)
                root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _dark_titlebar(self, tries=0, confirm=0):
        """把 Windows 原生标题栏也刷成深色（Win10 1809+/Win11）

        Tk 画的客户区早就是深色，只有最上面那条标题栏还是系统白 ——
        一整块白卡在深色窗口顶上很难看（2026-11-01 美化）。

        ⚠️ 三个坑，都是实测出来的
        ----------------------------------------------------
        1) 句柄必须是**顶层窗口**：`winfo_id()` 是客户区子窗口，
           属性设在它身上标题栏纹丝不动（读回来还是 0）。
           `GetAncestor(hwnd, GA_ROOT)` 才稳。
        2) 应用会在窗口**第一次真正显示**之后被 DWM 重置 ——
           早期只在 80ms 时设一次，读回来是 1，最后却是 0。
           所以设完要**隔一段时间回来核对**（confirm），掉了就再设。
        3) 老系统上没有 20 这个属性号（Win10 20H1 之前是 19），
           两个都试；都失败就安静放弃，不影响任何功能。
        """
        try:
            import ctypes
            u = ctypes.windll.user32
            dw = ctypes.windll.dwmapi
            hwnd = u.GetAncestor(self.root.winfo_id(), 2) or                 self.root.winfo_id()
            on = False
            g = ctypes.c_int(0)
            if dw.DwmGetWindowAttribute(hwnd, 20,
                                        ctypes.byref(g), 4) == 0 and g.value == 1:
                on = True
            else:
                v = ctypes.c_int(1)
                for attr in (20, 19):
                    dw.DwmSetWindowAttribute(hwnd, attr,
                                             ctypes.byref(v), 4)
                    if dw.DwmGetWindowAttribute(hwnd, attr, ctypes.byref(g),
                                                4) == 0 and g.value == 1:
                        on = True
                        break
            delay = None
            if on:
                if confirm < 3:          # 前 3 秒里再确认几次，被重置就重设
                    delay, confirm = 700, confirm + 1
            elif tries < 40:             # 最多试 10 秒（40 × 250ms）
                delay, tries = 250, tries + 1
            if delay:
                self.root.after(
                    delay, lambda: self._dark_titlebar(tries, confirm))
        except Exception:
            pass

    def _section(self, parent, text):
        """区块小标题：一根金色竖条 + 标题

        比一行纯文字多一层层次感，也顺手解决了「盘面/日志」两个区块
        跟参数区看起来一样重的问题（2026-11-01 美化）。
        竖条用 tk.Frame：宽 3px、高 13px，跟 11pt 标题的光学中线对齐。
        """
        wrap = tk.Frame(parent, bg=BG)
        tk.Frame(wrap, width=3, height=13, bg=ACCENT).pack(
            side="left", pady=(3, 0))
        ttk.Label(wrap, text=text, style="Section.TLabel").pack(
            side="left", padx=(7, 0))
        return wrap

    def _rule(self, top_pad=2, bot_pad=2):
        """一条 1px 的细分割线（纯视觉，用来分组）

        用 tk.Frame 而不是 ttk.Separator：clam 主题下 Separator 会
        带一圈浅色边框，在深色背景上反而像根凸起来的条。这里要的是
        「几乎看不见的一条暗线」，Frame 高度 1 + 边框色最干净。
        """
        parent = getattr(self, "page_auto", None) or self.root
        line = tk.Frame(parent, height=1, bg=BORDER)
        line.pack(fill="x", padx=16, pady=(top_pad, bot_pad))
        return line

    # ------------------------------------------------ 屏幕适配

    # 主窗口除「盘面区」之外的固定高度（标题 54 + 引擎行 47 + 分线 1
    # + 参数 78 + 思考时间 40 + 分线 1 + 按钮 63 + body 上下（删掉激进度那行，-40）
    # 内边距 12 = 296）。实测值见 _tests/_t17.log、_t19.log。
    #
    # ⚠️ 这只是**第一次建界面前的初估值**（2026-10-23）
    # ----------------------------------------------------
    # 真正用哪个值由 `_measure_chrome()` 实测决定 —— 因为
    # 不同 DPI / 字体 / 主题下每段高度都不一样，写死必然会算错。
    # 实测发现：小屏上按 336 估出来的盘面，会让信息栏只剩 1px。
    _CHROME_H = 296
    # 盘面区里除 canvas 之外的部分：区块标题 22 + 外框边距 14 + 间距 8
    _BOARD_CHROME = 44
    # 信息栏最小可读高度（4 行 Consolas 11 + padding）
    #
    # ⚠️ 从 170 改成 126（2026-10-23 实测纠正）
    # ----------------------------------------------------
    # 170 是早期用 `bbox("4.0")` 量错了得到的值 —— 那时量的是被挤小的
    # 信息栏（91px），第 4 行本来就不可见，bbox 返回 None 后退回估算，
    # 越估越大。实测 4 行的 ypixels 只有 101，加 pady 20 和余量 4 = **125**。
    # 这个错值曾把 4 行布局在所有小屏上误判成「永远装不下」。
    _INFO_MIN = 126
    # 盘面上下限：300 是「19 路还能看清」的下限，430 是老板定的舒适值
    #
    # ⚠️ 下限分两档（2026-10-23 实测 1366x768 后改）
    # ----------------------------------------------------
    # 1366x768 的工作区只有 728 高，减掉固定栏 ~360 只剩 ~360 给
    # 「盘面 + 信息栏」。两个都要 >=300/>=126 是不可能的。
    # 这时候只能让步：盘面降到 240 以下（19 路每格 11px，照样能看清棋形），
    # 信息栏改 3 行紧凑排版（四项数据一个不少，只是并成两行一行）。
    # 换句话说：**小屏上宁可盘面小，也不能让信息栏消失**。
    _BOARD_MIN = 300
    # ⚠️ 「够用 4 行排版」的盘面门限（2026-10-23 四次迭代后定稿）
    # ----------------------------------------------------
    # 只要盘面还能给到 240（19 路每格 11px，棋形/断点一眼可辨），
    # 就优先用 4 行标准排版 —— 信息栏四行各占一行，最好读。
    # 低于 240 才退到 3 行紧凑排版，把省下的 30px 还给盘面。
    #
    # 实测：1536x864 上 px4=288，改成 300 门限会被误踢到 3 行，
    # 白白牺牲可读性；240 则稳稳走 4 行。
    _BOARD_OK4 = 240
    # ⚠️ 地板从 220 降到 190（2026-10-23 1366x768 实测）
    # ----------------------------------------------------
    # 190px 的 19 路棋盘：每格 (190-40)/18 ≈ 8.3px，棋子直径约 7px。
    # 小是小，但**棋形、断点、气都看得清**；而且 1366x768 是
    # 老式宽屏笔记本，本来就不是给围棋软件设计的分辨率。
    # 换来的是「信息栏数据一个不少」，比看不见目差值得。
    _BOARD_FLOOR = 190
    _BOARD_MAX = 430            # 老板定的舒适值，再大没意义（盘面不用铺满）
    _INFO_MIN_SMALL = 96        # 小屏 3 行紧凑排版的最小高度（实测量出 95）

    def _pick_board_px(self, h):
        """给定窗口高度 h，算这次用多大的盘面（**保守估计**）

        这个函数在**建界面之前**就要给 canvas 定尺寸，所以只能靠
        常量估算。建完之后 `_fit_board()` 会用实测值再校一遍
        （见 _measure_chrome 的说明）。
        """
        avail = h - self._CHROME_H - self._INFO_MIN - self._BOARD_CHROME
        if avail >= self._BOARD_MIN:
            px = min(self._BOARD_MAX, int(avail))
        else:
            # 已经挤不下了：优先保住信息栏的 3 行，盘面往下让
            px = max(self._BOARD_FLOOR,
                     int(h - self._CHROME_H - self._INFO_MIN_SMALL
                         - self._BOARD_CHROME))
        # 取偶数：19 路步长 = (px-40)/18，奇数 px 会让线条落在半像素上，
        # 在 100% 缩放下会出现深浅不一的行（截图里能看到摩尔纹）
        return px - (px % 2)

    def _measure_chrome(self):
        """实测「除盘面区之外」真正吃掉多少高度

        ⚠️ 为什么要实测而不是用常量（2026-10-23 小屏实测）
        ----------------------------------------------------
        按 _CHROME_H=336 估算，在 1366x768 上算出的盘面是 300，
        但实际固定栏加起来是 ~360（字体行高、控件 padding 在不同
        DPI 下都会变），差 24px 全从信息栏扣 —— 结果信息栏只剩 **1px**，
        四行文字一个字都看不见。

        这种「估算差一点、后果全落在某个控件上」的问题，只有实测能根治。
        算法：把 root 的直接子控件里**除了 body 之外**的高度全加起来，
        就是这一版界面的真实 chrome。
        """
        total = 0
        body = getattr(self, "_body_frame", None)
        pages = getattr(self, "_pages", None)
        page_auto = getattr(self, "page_auto", None)
        try:
            for w in self.root.winfo_children():
                if w is pages or w is body:
                    continue
                hh = w.winfo_height()
                if hh > 1:
                    total += hh
            # 2026-11-08：自动对弈页的控件现在装在 page_auto 里，
            # 盘面之外的固定高度要从它里面量（root 只看得到 pages 容器）。
            if page_auto is not None:
                for w in page_auto.winfo_children():
                    if w is body:
                        continue
                    hh = w.winfo_height()
                    if hh > 1:
                        total += hh
        except Exception:
            return self._CHROME_H
        # 量不到（还没 map）→ 退回常量
        return total if total > 150 else self._CHROME_H

    def _need_live(self, rows):
        """量装下 `rows` 行信息需要多高（**以控件当前高度是否够用为可信前提**）

        ⚠️⚠️ 九版迭代。核心教训两条（2026-10-23）：
        ====================================================
        **教训一：不要模拟真控件，直接量它。**
        前七版都在"造一个和真控件一样的测量环境"，结果每造一处
        就引入一个新差异：
          ① 常量估算（不知道 16px 粗体首行占 41px）
          ② 隐藏 probe + bbox（probe 没 map，坐标不可信）
          ③ 真控件 bbox（只对可见行有效，被挤小就量不到）
          ④ configure(height=8)（pack 下无效，只改 reqheight）
          ⑤ ypixels（少算行间距：3 行报 71，真实 111）
          ⑥ 透明 Toplevel 探针（wrap=word + tag 显式设 font，
             跟真控件不是同一个排版环境，多算 36px）
          ⑦ place 临时接管高度（dlineinfo 的 y 跟着控件高度变）

        **教训二：量出来的值只有在"控件够高"时才可信。**
        这是第八版踩到的坑：
        info 只有 127 高时量 4 行，`dlineinfo` 一路给到底边 96 就
        **返回 None**（第 4 行在可视区外），于是 `_measure` 中途
        break，把「前 3 行的底边」当成了「4 行的底边」——
        量出 126（真实要 143），**而且这个错值被写进了缓存**。
        更糟的是它比「3 行」还小（126 < 147），逻辑都倒挂了，
        导致拉高窗口时 _fit_board 认为 4 行比 3 行更省，
        盘面一下涨到 426，info 被压回 127，第 4 行又被切掉。
        **表现就是：窗口拉高，信息栏反而变残缺。**

        ✅ 本版三条纪律：
        ----------------------------------------------------
          ① 只量真控件，不做任何模拟
          ② **量不到最后一行（dlineinfo 给 None）就认定"控件太矮"**，
             先用「盘面缩到地板」把高度让出来再量；
             还是量不到 → 返回一个**保守上限**，且 **不写缓存**
          ③ 缓存只存「确认可信」的值（最后一行确实量到了）
        """
        t = getattr(self, "info", None)
        if t is None:
            return self._INFO_MIN if rows >= 4 else self._INFO_MIN_SMALL

        cache = getattr(self, "_need_live_cache", None)
        if cache is None:
            cache = self._need_live_cache = {}
        if rows in cache:
            return cache[rows]

        old_txt = ""
        was_state = "disabled"
        saved_px = getattr(self, "BOARD_PX", 0)
        bottom = None
        try:
            old_txt = t.get("1.0", "end-1c")
            was_state = str(t.cget("state"))

            def _fill():
                # ⚠️ 填充文本必须和 `_refresh_info` **逐字符一致**
                # （2026-10-23 修正：原来漏了「计算量 ...v / ...s」的
                #  两个空格，实测少算 24px，导致折行判错；末尾也刻意
                #  不带 \n，因为 _refresh_info 现在末行不插 \n）
                t.configure(state="normal")
                t.delete("1.0", "end")
                t.insert("end", "首选  ", "k")
                t.insert("end", "T19\n", "big")
                if rows >= 4:
                    t.insert("end", "计算量 999999v / 99.9s\n", "k")
                    t.insert("end", "胜率  100.0%\n", "k")
                    t.insert("end", "目差  +123.5", "k")
                else:
                    t.insert("end", "计算量 999999v\n", "k")
                    t.insert("end", "胜率 100.0%　目差 +123.5", "k")
                self.root.update_idletasks()

            def _measure():
                """返回最后一行**墨迹底边**；任何一行不可见 → None

                ⚠️ 关键：必须**走完全部显示行**才算成功。
                中途遇到 None 就说明控件装不下 → 返回 None，
                不能拿"已经量到的部分"充数（第八版就是这么错的）。
                """
                last = None
                n = 0
                idx = "1.0"
                while n < 40:
                    di = t.dlineinfo(idx)
                    if di is None:
                        return None
                    n += 1
                    last = di
                    nxt = t.index(f"{idx} + 1 display lines")
                    if t.compare(nxt, ">=", "end") or nxt == idx:
                        break
                    idx = nxt
                if n == 0 or last is None:
                    return None
                # 确认没有漏行：最后一行之后应该就到底了
                return last[1] + last[3]

            _fill()
            bottom = _measure()

            # 量不到 → 临时把盘面缩到地板，把高度让给信息栏，再量
            if bottom is None and saved_px > self._BOARD_FLOOR:
                try:
                    self.cv.configure(width=self._BOARD_FLOOR,
                                      height=self._BOARD_FLOOR)
                    self.root.update_idletasks()
                    bottom = _measure()
                except Exception:
                    bottom = None
        except Exception:
            bottom = None
        finally:
            # 恢复盘面
            try:
                if saved_px and saved_px > self._BOARD_FLOOR:
                    self.cv.configure(width=saved_px, height=saved_px)
            except Exception:
                pass
            try:
                t.delete("1.0", "end")
                if old_txt:
                    t.insert("1.0", old_txt)
                t.configure(state=was_state)
                self.root.update_idletasks()
            except Exception:
                pass

        if bottom is None:
            # 连"盘面缩到地板"都装不下 —— 只可能在极小窗口上发生。
            # 返回保守上限，**不写缓存**（下次窗口变大要重量）。
            return 200 if rows >= 4 else 160

        # + 上下 pady(20) + descent 空白余量(10)
        #
        # ⚠️ 必须给 10px 而不是 4~6（2026-10-23 实测）
        # `dlineinfo` 的底边是**字形墨迹**，Tk 给行分配的高度还要加
        # descent。实测 Consolas 11 一行占 30px、墨迹只有 21px，
        # 最坏差 8px；给 6 时第 4 行被切成 5px 高的一条边。
        val = int(bottom) + 30
        val = max(72, min(400, val))
        cache[rows] = val            # 只缓存可信值
        return val

    def _info_wmax(self):
        """三种排版的「最宽一行」像素宽（超过它就会折行）

        返回 (w4_full, w4_short, w3)：
          · w4_full  = 4 行标准排版，计算量带耗时
          · w4_short = 4 行精简排版，计算量不带耗时（窄屏备选）
          · w3       = 3 行紧凑排版（计算量独占一行，胜率目差同行）

        用 tkfont 直接量，不建控件 —— 这个调用很频繁（每次 resize），
        必须便宜。

        ⚠️ 这里量的字符串必须和 `_refresh_info` **逐字符一致**（2026-10-23）
        ----------------------------------------------------
        曾经写成 `"计算量 999999v/99.9s"`（无空格），而实际渲染的是
        `f"{visits}v / {elapsed:g}s"` → **"999999v / 99.9s"（带空格）**。
        少算 2 个半角 = 24px（150% DPI 下半角 12px）。
        _t43 实测真控件该行 w=258，而这里量出 234，差 24 —— 正好两格。
        后果：可用宽 240 时判「234 装得下」，实际 258 折行，
        第三行被顶出框外。
        同理「胜率」在 full/short 排版里是**两个空格**（"胜率  "），
        在 compact 里是**一个空格**，不能混。
        """
        try:
            f11 = tkfont.Font(root=self.root, font=("Consolas", 11))
            f16 = tkfont.Font(root=self.root, font=("Consolas", 16, "bold"))
            first = f11.measure("首选  ") + f16.measure("T19")
            # full / short：胜率、目差各占一行，标签后都是两个空格
            core4 = max(f11.measure("胜率  100.0%"),
                        f11.measure("目差  +123.5"),
                        first)
            w4f = max(core4, f11.measure("计算量 999999v / 99.9s"))
            w4s = max(core4, f11.measure("计算量 999999v"))
            # compact：胜率与目差同行，标签后各一个空格，中间全角空格
            w3 = max(first,
                     f11.measure("计算量 999999v"),
                     f11.measure("胜率 100.0%　目差 +123.5"))
            return w4f, w4s, w3
        except Exception:
            # 量不了就退回粗估：150% DPI 下 1 个汉字 ≈ 22px
            return 250, 200, 280

    def _info_need(self):
        """兼容接口：返回 (4 行需要的高度, 3 行需要的高度)"""
        return self._need_live(4), self._need_live(3)

    def _clamp_main_window(self):
        """窗口 map 出来后，用**真实外框矩形**把它夹进屏幕

        ⚠️ 为什么需要（2026-10-23 实测）
        ----------------------------------------------------
        `geometry("1080x676+143+1")` 设的是**客户区**尺寸/位置，
        但屏幕上看的是**外框**（客户区 + 标题栏 32 + 底部边框 24 = +56）。
        实测 1366x728 可用区上：客户区 676 看着没问题，
        外框却是 732 —— 外框底边 733 > 728，最后 5px 在屏幕外。

        而且 56 这个差值随 Windows 版本/主题/DPI 会变，
        写死常量必然有一批机器差几像素。所以这里**实测差值**再夹一次。

        用 after_idle 而不是立即执行：窗口必须真正 map 出来，
        GetWindowRect 才给真实值。
        """
        try:
            import ctypes
            from ctypes import wintypes

            self.root.update_idletasks()
            cw = self.root.winfo_width()
            ch = self.root.winfo_height()
            if cw < 100 or ch < 100:      # 还没 map
                self.root.after(120, self._clamp_main_window)
                return

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long),
                            ("bottom", ctypes.c_long)]

            u = ctypes.windll.user32
            child = self.root.winfo_id()
            parent = u.GetParent(wintypes.HWND(child))
            rc = RECT()
            u.GetWindowRect(wintypes.HWND(parent or child), ctypes.byref(rc))
            fw, fh = rc.right - rc.left, rc.bottom - rc.top
            if fw < 100 or fh < 100:
                return

            aw, ah, ax, ay = work_area()
            if aw < 200 or ah < 200:
                return

            # 外框装饰差（标题栏 + 底边）= 外框 - 客户区
            dw, dh = fw - cw, fh - ch
            # 目标客户区尺寸：外框必须装进可用区
            nw = min(cw, max(700, aw - dw - 8))
            nh = min(ch, max(400, ah - dh - 8))

            # 外框位置往可用区里夹
            nx = max(ax, min(rc.left, ax + aw - (nw + dw)))
            ny = max(ay, min(rc.top, ay + ah - (nh + dh)))
            nx = max(0, nx)
            ny = max(0, ny)

            if (nw, nh) != (cw, ch):
                try:
                    mw, mh = self.root.minsize()
                    self.root.minsize(min(mw, nw), min(mh, nh))
                except Exception:
                    pass
                # geometry 要的 y 是客户区左上，减掉标题栏高
                cap = 32
                g = self.root.geometry()
                try:
                    gy = int(g.split("+")[-1])
                    d = self.root.winfo_rooty() - gy
                    if 0 <= d <= 120:
                        cap = d
                except Exception:
                    pass
                self.root.geometry(f"{nw}x{nh}+{nx}"
                                   f"+{max(0, ny - cap)}")
                # 尺寸变了 → 盘面也要重算
                self.root.after(60, self._fit_board)
        except Exception:
            pass

    def _fit_board(self):
        """窗口尺寸变了 → 重算「盘面大小 + 信息栏排版」，并重画

        ⚠️⚠️ 这里处理的是一个**真实的循环依赖**（2026-10-23，第六版）
        ====================================================
            盘面大小 → 左栏宽度 → 信息栏宽度 → 文字是否折行
                     → 信息栏需要多高 → 盘面能有多大 …
        前五版全死在"试图用一次计算解开它"：
          · 用常量估 → 不知道会折行
          · bbox 量真控件 → 只能量到可见行，被挤小就量不到
          · ypixels 量真控件 → 量的是"上一轮宽度"的值，越量越大
          · 固定参考宽探针 → 探针宽 250 不折行，真实列只有 224，
                             **照样折行**，第三行只露出 3px（截图铁证）
          · 靠缩小盘面换宽度 → 盘面小 → 列更窄 → 更会折行，无解

        ✅ 破法：**枚举有限个候选方案，逐个验证，取第一个可行的**
        ----------------------------------------------------
        候选只有 3 个（信息栏排版）+ 每个方案一个盘面尺寸，非常小：
            A. 4 行·标准（计算量带耗时）  需宽 w4f
            B. 4 行·精简（计算量不带耗时）需宽 w4s
            C. 3 行·紧凑（计算量独占一行）需宽 w3
        对每个方案：
            需要宽度  = max(信息栏最宽行, 盘面地板) + 26
            可用宽度  = body 宽 - 12（右间距）
            若 可用宽度 < 需要宽度 → 这个方案在小屏上会折行，淘汰
            否则 盘面 = min(可用宽-26, 高度允许值)，取最大的通过者
        这样"宽度够不够"和"高度够不够"是**分别验证**的，
        不再互相污染。

        为什么按 A→B→C 的顺序试：
          信息栏排版越"完整"越好读。A 是四行各归其位；
          B 只省掉耗时的显示（仍四行）；C 压成三行。
          先试最好的，不行才降级 —— 降级只在真的放不下时发生。

        ⚠️ 高度从 left 容器实测（不量 body）
        ----------------------------------------------------
        `body` 带 padding（compact 模式底部 6px），`winfo_height()`
        返回含 padding 的外框高，用它算会多算 6px。而 `left` 无 padding，
        它的高度就是**可分配高度本身**，零误差。
        """
        try:
            if self.root.winfo_height() <= 100:   # 还没 map
                return
            lf = getattr(self, "_left_frame", None)
            body = getattr(self, "_body_frame", None)
            if lf is None or body is None:
                return
            lh = lf.winfo_height()
            bw = body.winfo_width()
            if lh <= 50 or bw <= 100:
                return

            # 左栏最多能占 body 的多宽（留 12px 给右栏间距，
            # 右栏日志至少留 260px，否则日志没法读）
            max_left = max(self._BOARD_FLOOR + 26, bw - 12 - 260)

            w4f, w4s, w3 = self._info_wmax()
            # 各方案需要的最小左栏宽（+26 = 盘面外框/间距，+24 = Text 内边距）
            cand = [
                (w4f + 24 + 26, 4, "full"),
                (w4s + 24 + 26, 4, "short"),
                (w3 + 24 + 26, 3, "compact"),
            ]

            # 高度侧：left 高 - 47（「盘面」标题 + 外框边距）= 可分配高度
            avail_h = lh - 47

            best = None
            for need_w, rows, mode in cand:
                if need_w > max_left:
                    continue                       # 宽度放不下，淘汰
                # 这个方案在「左栏宽度 = need_w」时不折行需要多高
                need_h = self._need_live(rows)
                px_h = int(avail_h - need_h)       # 高度允许的盘面
                px = min(max_left - 26, px_h)      # 宽度也封顶
                px = max(self._BOARD_FLOOR, min(self._BOARD_MAX, px))
                if px >= self._BOARD_OK4 or mode == "compact":
                    # 4 行方案要求盘面 >= _BOARD_OK4（看得清棋形）；
                    # 3 行是最终兜底，只要 >= 地板就接受
                    best = (px, rows, mode)
                    break

            if best is None:
                # 全部淘汰（极窄窗口）→ 兜底：3 行 + 地板盘面
                best = (self._BOARD_FLOOR, 3, "compact")

            px, rows, mode = best
            px -= (px % 2)

            # 左栏宽度：跟着**信息栏需求**走，而不是跟着盘面走
            # —— 这是破环的关键。盘面在栏内居中，栏略宽一点无妨。
            lw = max(px + 26, min(max_left, max(w for w, _, _ in cand
                                               if w <= max_left) if any(
                w <= max_left for w, _, _ in cand) else px + 26))

            # ⚠️ 无论走不走重绘，**决策状态必须先落盘**（2026-10-23）
            # ----------------------------------------------------
            # 原来上面这个 `if` 里直接 `return`，跳过了后面所有赋值。
            # 大屏（1920 以上）首轮就算出 430 / 4 行，跟初始值
            # `BOARD_PX=430`、`_info_lines=4` 恰好相同 → 走 return →
            # `_info_lines_shown` / `_info_mode_shown` **永远是空**。
            # 界面看着没问题（初始值正好对），但任何想读"当前排版"
            # 的代码（测试、日志、以后的功能）都拿不到真值，
            # 只能靠 `getattr(..., 默认值)` 猜 —— 一猜就猜错。
            # 现在把赋值提到判定之前：状态永远反映真实决策。
            prev_mode = getattr(self, "_info_mode_shown_applied", None)
            self._info_lines_shown = rows
            self._info_mode_shown = mode
            self._info_lines = rows
            self._info_mode = mode

            if (abs(px - getattr(self, "BOARD_PX", 0)) < 2
                    and abs(lw - lf.winfo_width()) < 2
                    and mode == prev_mode):
                # 盘面、左栏宽、排版方案都没变 → 不必重绘
                return
            # 记录「上次真正画上去的方案」，用于判断是否需要重绘。
            # 不能直接用 _info_mode_shown 比 —— 它在判定前就被赋值了。
            self._info_mode_shown_applied = mode
            self.BOARD_PX = px
            self.cv.configure(width=px, height=px)
            try:
                lf.configure(width=lw)
            except Exception:
                pass
            self._refresh_board_and_info()
            # 布局变了 → 尺寸可能再变，再校一次（最多两轮，防临界横跳）
            if getattr(self, "_fit_round", 0) < 2:
                self._fit_round = getattr(self, "_fit_round", 0) + 1
                self.root.after(50, self._fit_board)
        except Exception:
            pass

    def _build(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                     bordercolor=BORDER, lightcolor=BG2, darkcolor=BG2)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Dim.TLabel", background=BG, foreground=FG_DIM,
                     font=("Microsoft YaHei UI", 9))
        # ⚠️ 被锁的字段标签（2026-10-27）
        # 深色主题下 ttk 的 disabled 只把文字从 #ddd 降到 #888，
        # 整屏扫过去看不出「这几项调不了」。标签再压到 #5a5f66 + 🔒，
        # 视觉上成块地暗下去，才是一眼可辨。
        st.configure("Locked.TLabel", background=BG, foreground="#5a5f66",
                     font=("Microsoft YaHei UI", 9))
        st.configure("H.TLabel", background=BG, foreground=ACCENT,
                     font=("Microsoft YaHei UI", 10, "bold"))
        # 大标题（窗口顶部的产品名）
        st.configure("Title.TLabel", background=BG, foreground=FG,
                     font=("Microsoft YaHei UI", 13, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=FG_DIM,
                     font=("Microsoft YaHei UI", 9))
        st.configure("TButton", background=BG2, foreground=FG, borderwidth=1,
                     bordercolor=BORDER, focusthickness=0, padding=(14, 8),
                     font=("Microsoft YaHei UI", 10))
        st.map("TButton",
               background=[("active", BG3), ("pressed", BG3)],
               bordercolor=[("active", ACCENT)],
               foreground=[("disabled", "#5a5f66")])
        # 次级按钮：透明底 + 细边，别跟主键抢眼睛（2026-11-01 美化）
        st.configure("Ghost.TButton", background=BG, foreground=FG,
                     borderwidth=1, bordercolor=BORDER, focusthickness=0,
                     padding=(14, 8), font=("Microsoft YaHei UI", 10))
        st.map("Ghost.TButton",
               background=[("active", BG2), ("pressed", BG2)],
               bordercolor=[("active", ACCENT)],
               foreground=[("disabled", "#5a5f66")])
        # 行内小标题 / 区块标题 / 顶栏 / 状态胶囊
        st.configure("Field.TLabel", background=BG, foreground=ACCENT,
                     font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("Section.TLabel", background=BG, foreground=FG,
                     font=("Microsoft YaHei UI", 11, "bold"))
        st.configure("Chip.TLabel", background=CHIP, foreground=FG_DIM,
                     font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 3))
        st.configure("Hdr.TFrame", background=HDR)
        st.configure("HdrTitle.TLabel", background=HDR, foreground=FG,
                     font=("Microsoft YaHei UI", 16, "bold"))
        st.configure("HdrSub.TLabel", background=HDR, foreground=FG_DIM,
                     font=("Microsoft YaHei UI", 9))
        st.configure("HdrChip.TLabel", background="#1e242c", foreground=FG_DIM,
                     font=("Microsoft YaHei UI", 9), padding=(9, 3))
        st.configure("Go.TButton", background="#2f6b45", foreground="#eaf7ee",
                     font=("Microsoft YaHei UI", 10, "bold"),
                     padding=(24, 10), borderwidth=0)
        st.map("Go.TButton",
               background=[("active", "#3a8154"), ("pressed", "#255234")],
               foreground=[("disabled", "#87a08c")])
        st.configure("Stop.TButton", background="#7a3535", foreground="#ffe1e1",
                     padding=(24, 10), borderwidth=0)
        st.map("Stop.TButton",
               background=[("active", "#874040"), ("pressed", "#582626")])
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TCombobox", fieldbackground=BG2, background=BG2,
                     foreground=FG, arrowcolor=FG, selectbackground=BG2,
                     selectforeground=FG)
        # ⚠️ Combobox 在 readonly 状态下不吃上面的 foreground（2026-10-22 实测）
        # 必须单独 map readonly 的两项，否则下拉框里的字是「深灰压深灰」，
        # 肉眼基本看不见 —— 截图里引擎名和「我执」的值都糊掉了。
        st.map("TCombobox",
               fieldbackground=[("readonly", BG2), ("disabled", BG2),
                                ("active", BG2)],
               foreground=[("readonly", FG), ("disabled", FG_DIM),
                           ("active", FG)],
               selectbackground=[("readonly", BG2), ("active", BG2)],
               selectforeground=[("readonly", FG)],
               arrowcolor=[("readonly", ACCENT), ("active", ACCENT)])
        # 下拉展开的那一层是原生 tk Listbox，样式得靠 option 数据库
        self.root.option_add("*TCombobox*Listbox.background", BG2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", BG)
        self.root.option_add("*TCombobox*Listbox.font",
                             "{Microsoft YaHei UI} 10")
        st.configure("TSpinbox", fieldbackground=BG2, foreground=FG,
                     arrowcolor=FG)
        # ⚠️ Spinbox 的 disabled 也要单独压暗（2026-10-27）
        # 不 map 的话 clam 主题会用默认的浅灰前景，深色背景上反而变
        # **更亮**，看着像「还能改」。这里显式给一个明显暗的色，
        # 箭头也一起暗下去。
        st.map("TSpinbox",
               fieldbackground=[("disabled", BG2)],
               foreground=[("disabled", "#5a5f66")],
               arrowcolor=[("disabled", "#4a4e55")])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=BG2, foreground=FG_DIM,
                     padding=(14, 6))
        st.map("TNotebook.Tab",
               background=[("selected", BG)], foreground=[("selected", ACCENT)])

        # 深色滚动条：clam 主题的 TScrollbar 用 trough/background 两色画，
        # 不配的话是一条白条（截图里很跳）。这里配成「深槽 + 灰滑块」，
        # 悬停时滑块变亮一点，宽度 10px（默认 17px 太占地方）。
        st.configure("Dark.Vertical.TScrollbar",
                     background="#39414e", troughcolor="#12161b",
                     bordercolor="#12161b", arrowcolor=FG_DIM,
                     darkcolor="#39414e", lightcolor="#39414e",
                     width=8, arrowsize=0, borderwidth=0)
        st.map("Dark.Vertical.TScrollbar",
               background=[("active", "#4d5666"), ("pressed", ACCENT)],
               arrowcolor=[("active", FG)])

        # ---- 顶部标题栏（美化：产品名 + 一句话说明）----
        # 老板 2026-10-23：「优化界面并配上软件封面」
        # 标题栏是一眼看到的第一块内容，给个名字比直接堆控件清爽。
        #
        # ⚠️ 小屏时上下 padding 收紧（2026-10-23 屏幕适配）
        # 默认 (16,14,16,2) 这一块占 54px。1366x768 上总共只有
        # 约 660 客户区高，这 54px 是三行信息的代价 ——
        # 压到 (16,6,16,0) 省 16px，观感几乎不变。
        _hp = (18, 6, 18, 5) if self._compact else (18, 10, 18, 9)
        hdr = tk.Frame(self.root, bg=HDR)
        hdr.pack(fill="x")
        inner = tk.Frame(hdr, bg=HDR, padx=_hp[0], pady=0)
        inner.pack(fill="x", pady=(_hp[1], _hp[3]))
        # 徽标：木色底 + 一黑一白两颗子（纯 Canvas，不依赖打包资源）
        logo = tk.Canvas(inner, width=34, height=34, bg=HDR,
                         highlightthickness=0, bd=0)
        logo.pack(side="left", padx=(0, 12))
        logo.create_rectangle(1, 1, 33, 33, fill=WOOD, outline="#b8903f")
        logo.create_oval(6, 6, 17, 17, fill="#14161a", outline="")
        logo.create_oval(15, 15, 28, 28, fill="#f5f2ea", outline="#b9b1a4")
        self._logo = logo
        tbox = tk.Frame(inner, bg=HDR)
        tbox.pack(side="left")
        ttk.Label(tbox, text="野狐自动对弈",
                  style="HdrTitle.TLabel").pack(anchor="w")
        ttk.Label(tbox, text="KataGo 智子云 · 自动对弈 / AI 托管",
                  style="HdrSub.TLabel").pack(anchor="w", pady=(2, 0))
        ver_lbl = ttk.Label(inner, text=f"v{APP_VERSION}", style="HdrChip.TLabel")
        ver_lbl.pack(side="right", pady=(6, 0))
        # 顶栏下面一条极淡的金线，把"标题"和"控制区"分开
        tk.Frame(self.root, height=1, bg=LINE).pack(fill="x")

        # ---- 页面切换（2026-11-08）：自动对弈 / AI 托管 ----
        # 两个页面互斥显示；自动对弈页的控件全部放进 self.page_auto，
        # 这样切到 AI 页时不会把原来的布局打乱（_measure_chrome 会跟着改）。
        # ⚠️ 标签放在顶栏里而不是另起一行：另起一行会多吃 40px，
        # 老板的自动对弈页盘面会被迫缩小（「老样子不用变」）。
        pagebar = tk.Frame(inner, bg=HDR)
        pagebar.pack(side="left", padx=(26, 0), pady=(2, 0))
        self._page_bar = pagebar
        self._page_btns = {}
        self._page_underlines = {}
        for _key, _label in (("auto", "自动对弈"), ("ai", "AI 托管")):
            _box = tk.Frame(pagebar, bg=HDR)
            _box.pack(side="left", padx=(0, 6))
            _lab = tk.Label(_box, text=_label, bg=HDR, fg=FG_DIM,
                            font=("Microsoft YaHei UI", 10, "bold"),
                            padx=8, pady=2, cursor="hand2")
            _lab.pack()
            _line = tk.Frame(_box, height=2, bg=HDR)
            _line.pack(fill="x")
            _lab.bind("<Button-1>", lambda e, k=_key: self.show_page(k))
            self._page_btns[_key] = _lab
            self._page_underlines[_key] = _line
        self._page = "auto"
        self._page_btns["auto"].configure(fg=ACCENT)
        self._page_underlines["auto"].configure(bg=ACCENT)

        self._pages = tk.Frame(self.root, bg=BG)
        self._pages.pack(fill="both", expand=True)
        self.page_auto = tk.Frame(self._pages, bg=BG)
        self.page_auto.pack(fill="both", expand=True)
        self.page_ai = tk.Frame(self._pages, bg=BG)
        self.ai_page = None                    # 懒建，见 _build_ai_page()

        # ---- 顶部：引擎选择 ----
        top = ttk.Frame(self.page_auto, padding=(14, 8, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="引擎", style="Field.TLabel").pack(side="left")

        self.eng_var = tk.StringVar()
        # ⚠️ width 只是**初值**，_fill_engines() 末尾会按内容自适应
        # （原来固定 46，实测浪费 200px，见 _fit_eng_box 的说明）
        self.eng_box = ttk.Combobox(top, textvariable=self.eng_var,
                                    state="readonly", width=28,
                                    font=("Microsoft YaHei UI", 10))
        self.eng_box.pack(side="left", padx=(10, 8))
        self._fill_engines()
        self.eng_box.bind("<<ComboboxSelected>>", self._on_eng)

        self.eng_note = ttk.Label(top, text="", style="Dim.TLabel")
        # ⚠️ 必须锚在左边（2026-10-23 实测）
        # tkinter 的 side="left" 只是「按顺序往右排」，标签本身默认居中。
        # 字数一变，文字就跟着左右晃 —— 换引擎时那句话会跳动，很难看。
        # anchor="w" 让它固定从左边开始写。
        self.eng_note.pack(side="left", padx=(6, 0), anchor="w")

        # ⚠️ 窗口变宽变窄都要重新截断说明（2026-10-23）
        # ----------------------------------------------------
        # _note_max_px() 是按「窗口宽 - 说明起点」实时算的，所以窗口一拖，
        # 上一句的截断结果就过期了：
        #   · 拖宽 → 明明能显示全，还挂着上个尺寸的省略号
        #   · 拖窄（minsize 1000）→ 截断长度没跟上，文字被硬裁，连省略号都没有
        # <Configure> 在每次尺寸变化时触发。用一个 after 去抖，
        # 免得拖动过程中每一像素都重算一遍字体（会卡）。
        self._resize_job = None
        def _on_resize(ev):
            if ev.widget is not self.root:
                return
            if self._resize_job:
                try:
                    self.root.after_cancel(self._resize_job)
                except Exception:
                    pass
            self._resize_job = self.root.after(120, self._reflow_note)
            # ⚠️ 顺带把盘面尺寸也重算（2026-10-23 屏幕适配）
            # 用户把窗口拖高 → 盘面该变大；拖矮 → 盘面变小、信息栏保住。
            # 不重算的话，小屏启动时盘面是 300，用户拖到最大也只有 300 ——
            # 大屏用户会觉得「盘面怎么这么小」（因为启动那一刻屏幕小，
            # 比如接了投影仪）。绑在同一个去抖里，不额外加定时器。
            self.root.after(160, self._fit_board)
        self.root.bind("<Configure>", _on_resize)

        # 细分隔线：把「引擎选择」和「参数」两块在视觉上分开
        self._rule()

        # ---- 参数区 ----
        # ⚠️ 小屏时收紧行内边距（2026-10-23 屏幕适配）
        # chrome 每减 8px，盘面就能多 8px。1366x768 上这几处加起来
        # 能省 30 多像素，正好够把盘面从 220 抬回 260 左右。
        _vpad = 3 if self._compact else 8
        box = ttk.Frame(self.page_auto, padding=(14, _vpad, 14, _vpad))
        box.pack(fill="x")

        def field(parent, label, var, kind="entry", width=7, values=None):
            """建一个「上标签 + 下控件」的小格子，返回控件本身

            ⚠️ 标签也挂在控件上（w._wb_label），锁定时要一起变暗
            （2026-10-27 加）。深色主题里 disabled 只是把文字从 #ddd
            降到 #888，整屏扫过去不容易注意到；标签变暗 + 加锁图标
            才是「一眼看出这几项调不了」。
            """
            f = ttk.Frame(parent)
            f.pack(side="left", padx=(0, 16))
            lbl = ttk.Label(f, text=label, style="Field.TLabel")
            lbl.pack(anchor="w")
            if kind == "combo":
                # var 允许为 None：调用方想自己装 textvariable 时用
                w = ttk.Combobox(f, values=values, state="readonly",
                                 width=width,
                                 font=("Microsoft YaHei UI", 10))
                if var is not None:
                    w.configure(textvariable=var)
            elif kind == "spin":
                w = ttk.Spinbox(f, textvariable=var, width=width, from_=1,
                                to=99999, font=("Microsoft YaHei UI", 10))
            else:
                w = ttk.Entry(f, textvariable=var, width=width,
                              font=("Microsoft YaHei UI", 10))
            w.pack()
            try:
                w._wb_label = lbl          # 供 _lock_game_info 变暗用
                w._wb_label_text = label   # 解锁时还原原文
            except Exception:
                pass
            return w

        # ⚠️ 以下全部从设置里取初值（2026-10-23）
        # 老板要的是「配一次，下次打开还是这样」。写死默认值等于每次重开
        # 都被打回原形，用户改了十几次会觉得软件没记住东西。
        _ui = self.cfg.get("ui") or {}

        def _uv(k, dflt):
            v = _ui.get(k)
            return dflt if v is None else v

        self.color_var = tk.StringVar(value=_uv("color", "白"))
        # 宽度给 6：Combobox 的 width 单位是「字符」，中文 1 字算 1 但也带内边距，
        # 给 5 的时候「白」后面那个下拉箭头会压到字上（截图里被挤没了）
        # 存引用：引擎跑起来后要把它灰掉（老板 2026-10-23 要求，见 _lock_game_info）
        self.color_box = field(box, "我执", self.color_var, "combo", 6,
                               ["白", "黑"])

        # ⚠️ 规则（老板 2026-10-23 要求）
        # 中国数子 / 日本数目 / 韩国数目。这三家的差别不只是数法：
        #   中国数子：子空皆地，贴着走棋不用收单官，对杀算得深
        #   日韩数目：只数空 + 提子，不收单官、不计双活，但**有禁全同**外的差异
        # 引擎拿到 kata-set-rules 后会按对应规则评估目数，
        # 所以选错规则时胜率/目差会系统性偏移 —— 必须跟野狐房间的规则一致。
        rules_box = field(box, "规则", None, "combo", 14,
                          [FA.RULES[k] for k in FA.RULES_ORDER])
        self.rules_box = rules_box
        self.rules_var = tk.StringVar(value=_uv("rules", FA.RULES["chinese"]))
        rules_box.configure(textvariable=self.rules_var)

        # ⚠️ 贴目：可键入 + 0.5 步长 + 只收合法值（老板 2026-10-23 要求）
        # ----------------------------------------------------
        # 老板原话：「这个调整贴目要做成不仅上下浮动，而且可以自己写数字的
        #            （当然必须是最小单位是0.5且合法的贴目数字）」
        #
        # 光给 Spinbox 的 increment/format/from_/to **一个都约束不住**
        # 键入的值（实测见文件顶部 KOMI_MIN 那段的说明）。所以这里三件事：
        #   ① validate="key" + komi_key_ok  → 挡掉「一眼不是数字」的字符
        #   ② <FocusOut>/<Return>/箭头      → 走 _commit_komi 吸附 0.5 + 夹 ±150
        #   ③ 清空重打时不立刻报错，提交时才纠正
        #
        # 为什么去掉 format="%.1f"：
        # 它只影响「控件自己写回」时的显示（点箭头），对键入毫无作用；
        # 而它有个副作用 —— 值仍是 "7" 时点一下箭头会变成 "7.5"（对），
        # 但键入 "7" 失焦后不会被改成 "7.0"（我们也不想改，
        # 老板看 "7" 比看 "7.0" 自然）。所以交给 komi_text() 统一输出。
        self.komi_var = tk.StringVar(value=komi_text(_uv("komi", KOMI_DEFAULT)))
        f_komi = ttk.Frame(box)
        f_komi.pack(side="left", padx=(0, 16))
        self._komi_lbl = ttk.Label(f_komi, text="贴目", style="Field.TLabel")
        self._komi_lbl.pack(anchor="w")
        self.komi_spin = ttk.Spinbox(
            f_komi, textvariable=self.komi_var, width=8,
            from_=KOMI_MIN, to=KOMI_MAX, increment=KOMI_STEP,
            justify="right",
            validate="key",
            validatecommand=(self.root.register(komi_key_ok), "%P"),
            font=("Microsoft YaHei UI", 10))
        self.komi_spin.pack()
        # 和 field() 里那三件对齐，锁定时一起变暗
        try:
            self.komi_spin._wb_label = self._komi_lbl
            self.komi_spin._wb_label_text = "贴目"
        except Exception:
            pass
        # 记住当前有效值：手输成 "7." / "-" / "abc" 时要回退到它（否则
        # 一处打错就把贴目清空，start() 会报「填的不是数字」）
        self._komi_val = komi_parse(self.komi_var.get())
        if self._komi_val is None:
            self._komi_val = float(KOMI_DEFAULT)
            self.komi_var.set(komi_text(self._komi_val))
        # 三条提交路径都进同一个入口：失焦、回车、点箭头
        # （箭头那边 ttk 自己会先按 increment 算一遍，我们再吸附一次兜住边界）
        self.komi_spin.bind("<FocusOut>", self._commit_komi)
        self.komi_spin.bind("<Return>", self._commit_komi)
        self.komi_spin.configure(command=self._commit_komi)

        # ⚠️ 「最多手数」和「思考上限」已删（老板 2026-10-23 原话：
        #    「把思考上限和最多手数选项去掉，不需要限制这两个」）
        # 现在：
        #   · 手数不限 —— 一直下到对局结束或手动停（Auto.max_moves=0）
        #   · 每手想多久完全由下面「思考时间」一栏决定
        # 原来那个「思考上限」是给引擎的兜底预算，跟「思考时间」重复
        # 且互相矛盾（填 8 秒上限 + 17 秒思考 = 到底听谁的？），删掉更清楚。

        # ---- 思考时间：固定 / 随机 ----
        gap = ttk.Frame(self.page_auto, padding=(14, 0, 14,
                                          (2 if self._compact else 6)))
        gap.pack(fill="x")
        ttk.Label(gap, text="思考时间", style="Field.TLabel").pack(
            side="left", padx=(0, 10))

        self.gap_mode = tk.StringVar(value=_uv("think_mode", "fixed"))

        def mode_lbl(text, val):
            rb = ttk.Radiobutton(gap, text=text, value=val,
                                 variable=self.gap_mode,
                                 command=self._on_gap_mode)
            rb.pack(side="left", padx=(0, 4))
            return rb

        mode_lbl("固定", "fixed")
        # ⚠️ 默认 5 秒（老板 2026-10-23 定）：
        # 5 秒是「11b 云端权重能给出稳定好手」的下限，再短（1~2s）
        # 访问量只有几十，候选质量明显掉；再长（15s+）对手会等得烦。
        # 配合 Auto._pad_think()，「固定」现在真的是每手 5 秒，
        # 不会因为引擎提前收敛变成 0.2 秒。
        # ⚠️ 三个时间框统一改造（老板 2026-10-27 要求）
        # ----------------------------------------------------
        # 原话：「时间也设置也做成可上下滚动也可键入
        #        （最小单位 0.5，0.5 到 180 之间，固定随机都一样）」
        #
        # 和贴目完全一样的毛病：Tk 的 Spinbox 对**人手键入**零约束，
        # from_/to/increment 只在点箭头时当边界。所以这里三件事：
        #   ① validate="key" + time_key_ok   → 逐字符挡住非法（含负号）
        #   ② <FocusOut>/<Return>/箭头        → 走 _commit_time 吸附 0.5 + 夹 [0.5,180]
        #   ③ 打一半（"1." / ""）先放行，提交时才纠正
        #
        # 三个框共用一套规则（老板明确说了「固定随机都一样」），
        # 所以写成一个内嵌工厂，别复制三遍。
        def _time_spin(parent, var, width, step=THINK_STEP):
            w = ttk.Spinbox(
                parent, textvariable=var, width=width,
                from_=THINK_MIN, to=THINK_MAX, increment=step,
                justify="right",
                validate="key",
                validatecommand=(self.root.register(time_key_ok), "%P"),
                font=("Microsoft YaHei UI", 10))
            w._step = step          # 吸附粒度：固定 1 秒、随机 0.5 秒
            # 记住当前有效值：手输成 "1." / "" 时回退到它
            # （不能留空，start() 读到空串会报「填的不是数字」）
            #
            # ⚠️ 必须取**控件此刻显示的值**，不能写死 THINK_DEFAULT
            # （2026-10-27 修）。老板上次存的是 30 秒，这里写死 5 的话，
            # 他手滑打了个 "1." 再点别处，时间就从 30 悄悄变成 5 秒 ——
            # 那正是注释里说「不能默认成 5」要防的事，之前只是写在了
            # 注释里、代码没照做。
            try:
                _init_v = time_parse(var.get())
            except Exception:
                _init_v = None
            w._wb_last = (time_snap(_init_v) if _init_v is not None
                          else float(THINK_DEFAULT))
            w.bind("<FocusOut>", self._commit_time)
            w.bind("<Return>", self._commit_time)
            w.configure(command=self._commit_time)
            return w

        self.gap_var = tk.StringVar(value=time_text(_uv("think", THINK_DEFAULT)))
        self.gap_spin = _time_spin(gap, self.gap_var, 6,
                                   step=THINK_FIXED_STEP)
        self.gap_spin.pack(side="left", padx=(0, 4))
        ttk.Label(gap, text="秒　", style="Dim.TLabel").pack(side="left",
                                                             padx=(0, 10))

        mode_lbl("随机", "random")
        # ⚠️ 默认 0.5~10 秒
        # 下限从 0 抬到 0.5 是老板 2026-10-27 定的（最小单位 0.5）——
        # 0 秒等于「不思考直接落子」，现在最少的 0.5 秒也算想了。
        self.gap_min_var = tk.StringVar(
            value=time_text(_uv("think_min", THINK_MIN_DEFAULT),
                            THINK_MIN_DEFAULT))
        self.gap_max_var = tk.StringVar(
            value=time_text(_uv("think_max", THINK_MAX_DEFAULT),
                            THINK_MAX_DEFAULT))
        self.gap_min_spin = _time_spin(gap, self.gap_min_var, 5)
        self.gap_min_spin.pack(side="left")
        ttk.Label(gap, text="~", style="Dim.TLabel").pack(side="left",
                                                          padx=(4, 4))
        self.gap_max_spin = _time_spin(gap, self.gap_max_var, 5)
        self.gap_max_spin.pack(side="left")
        ttk.Label(gap, text="秒之间随机",
                  style="Dim.TLabel").pack(side="left", padx=(4, 12))

        mode_lbl("按计算量", "visits")
        self.visits_var = tk.StringVar(
            value=visits_text(_uv("visits", VISITS_DEFAULT)))
        self.visits_spin = ttk.Spinbox(
            gap, textvariable=self.visits_var, width=7,
            from_=VISITS_MIN, to=VISITS_MAX, increment=VISITS_STEP,
            justify="right",
            font=("Microsoft YaHei UI", 10))
        self.visits_spin._step = 1
        self.visits_spin.pack(side="left", padx=(0, 4))
        ttk.Label(gap, text="v", style="Dim.TLabel").pack(side="left",
                                                          padx=(0, 10))

        # ⚠️ 下棋中实时生效（老板 2026-10-23 原话：
        #    「一个是要在下棋的时候也能随意调整思考或随机思考的时间」）
        # ----------------------------------------------------
        # 光有 Spinbox 不够：它只是把数字存进变量，Auto 在 start() 那一刻
        # 就把值拷走了，下棋途中改了完全没用。
        # 这里把三个输入框 + 模式切换都接到 _on_think_change()，
        # 它再去调 Auto.set_think()（线程安全，下一手就生效）。
        #
        # 为什么绑 <FocusOut> + <Return> + 箭头（command）+ 变量 trace：
        #   · 手动输入数字：焦点离开输入框或按回车时才提交
        #     （不能绑 <KeyRelease>，否则打「1」的时候就把 1 提交了，
        #       想改成 15 会先经历 1）
        #   · 点上下箭头：ttk.Spinbox 的 command 回调
        #   · 三种路径都走同一个入口，反复提交同一值会被去重函数挡掉
        for w in (self.gap_spin, self.gap_min_spin, self.gap_max_spin,
                  self.visits_spin):
            w.bind("<FocusOut>", self._on_think_change)
            w.bind("<Return>", self._on_think_change)
            w.configure(command=self._on_think_change)

        # ⚠️ 「ESC 全局生效」勾选框早已删除（2026-10-23）；
        # 2026-11-02 起 **ESC 整个取消**：对局中的开关/暂停都改成
        # 屏幕右边那颗悬浮球上的按钮，不再抢系统的 ESC 键，
        # 也不再有「焦点在野狐上按 ESC」这件事。

        # ⚠️ 「演练（只算不下）」勾选框已删（老板 2026-10-23 要求）
        # ----------------------------------------------------
        # 这个模式不只是「没用」，它是「启动即结束」的元凶之一：
        # 不落子 → 盘面永远不变 → 主循环命中「同一盘面」分支 → 直接 return。
        # 老板看到的就是「引擎启动之后直接就结束了，一手棋都不下」。
        # 另外它还带来一个隐性风险：勾着演练却以为是实下，白等一整盘。
        # 现在落子行为只有一种：想完就点。

        self._on_gap_mode()

        # 分隔线：参数区 / 操作区
        self._rule(top_pad=4, bot_pad=0)

        # ---- 按钮 ----
        # ⚠️ 2026-10-23 老板要求重排（原话）：
        #   「把开始键改成开启/关闭引擎（按暂停/继续键不影响开启/关闭引擎，
        #     但在未开启引擎时，暂停/继续键无效）」
        #
        # 于是两个键的语义彻底分开了，互不干扰：
        #   主键「开启引擎 ⇄ 关闭引擎」= 造/销毁引擎进程（重，几秒）
        #   次键「暂停 ⇄ 继续」      = 只是不下子，引擎一直在（轻，瞬时）
        # 未开启引擎时暂停键 disabled，点了也什么都不做 ——
        # 免得老板以为「按了暂停就该停下来」，其实压根没开。
        bar = ttk.Frame(self.page_auto, padding=(14, (4 if self._compact else 10),
                                         14, (4 if self._compact else 8)))
        bar.pack(fill="x")

        # 主键：开启引擎 ⇄ 关闭引擎
        self.btn_go = ttk.Button(bar, text="▶  开启引擎",
                                 style="Go.TButton",
                                 command=self.toggle_engine)
        self.btn_go.pack(side="left")

        # 次键：暂停 ⇄ 继续（未开启引擎时禁用；对局中同一个动作也在悬浮球上）
        self.btn_pause = ttk.Button(bar, text="⏸  暂停",
                                    command=self.toggle_pause,
                                    state="disabled")
        self.btn_pause.pack(side="left", padx=(10, 0))

        self.btn_check = ttk.Button(bar, text="检查野狐", style="Ghost.TButton",
                                    command=self.check_fox)
        self.btn_check.pack(side="left", padx=(18, 0))
        self.btn_calib = ttk.Button(bar, text="重新标定棋盘",
                                    style="Ghost.TButton", command=self.calib)
        self.btn_calib.pack(side="left", padx=(10, 0))
        # ⚠️ 「设置」常驻（老板 2026-10-23）
        # 首次设置向导只在第一次启动弹；之后改引擎/改默认项全走这个按钮。
        # 没有它的话，用户首次点了「稍后再说」就再也找不到向导了。
        # ⚠️ 按钮名带「引擎」两字（2026-10-23 老板要求）
        # ----------------------------------------------------
        # 老板原话：「做一个设置的选项，就对应着首次设置，
        #             这样如果用户电脑里的引擎有变动，也能够修改」
        # 光写「设置」太笼统，用户不知道里面能干什么；
        # 「引擎设置」一眼就知道：引擎变了点这里。
        # 点开的向导标题在非首启时显示「引擎设置」，跟按钮对齐。
        self.btn_setup = ttk.Button(bar, text="引擎设置",
                                    style="Ghost.TButton",
                                    command=self.open_setup)
        self.btn_setup.pack(side="left", padx=(10, 0))

        # 右侧状态灯
        chip = tk.Frame(bar, bg=CHIP)
        chip.pack(side="right")
        self.dot = tk.Canvas(chip, width=12, height=12, bg=CHIP,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(10, 0), pady=7)
        self._dot_id = self.dot.create_oval(2, 2, 10, 10, fill=FG_DIM,
                                            outline="")
        self.state_lbl = ttk.Label(chip, text="待机", style="Chip.TLabel")
        self.state_lbl.pack(side="left", padx=(6, 0))

        # ---- 主体：左盘面 / 右日志 ----
        # ⚠️ 比例（老板 2026-10-23：「左边盘面做的稍微大一点，右边的日志稍微小一点」）
        # 盘面 292 → 430px（面积 +117%），左侧容器 300 → 452px。
        # 日志仍是 fill="both" + expand，窗口拉宽时它会吃掉多余空间 ——
        # 但**默认**宽度下它比原来窄了 150px 左右，正是老板要的比例。
        body = ttk.Frame(self.page_auto, padding=(14, 0, 14,
                                           (6 if self._compact else 12)))
        body.pack(fill="both", expand=True)
        # 存起来：_measure_chrome() 要靠它把 body 排除在固定高度之外
        self._body_frame = body

        # ⚠️ 左栏宽度跟着盘面走（2026-10-23 屏幕适配）
        # ----------------------------------------------------
        # 原来写死 456（盘面 430 + 外框 + 右边距）。小屏上盘面缩到 300 后，
        # 456 会空出 130px 木色/深色空白，看着像"盘面没画完"。
        # +26 = 盘面外框 2 + 描边 + 与日志区的间距 12。
        left_w = self.BOARD_PX + 26
        left = ttk.Frame(body, width=left_w)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        self._left_frame = left

        self._section(left, "盘面").pack(anchor="w", pady=(0, 2))
        # 盘面外框：1px 描边 + 一点内边距，让棋盘像「嵌在面板里」
        # 直接贴边会显得很硬；加一像素深色边框成本为零，观感差别明显。
        #
        # ⚠️ 用 pack(anchor="n") 而不是默认居中（2026-10-23）
        # 左栏宽度可能**大于**盘面（因为信息栏需要更宽的列才能不折行，
        # 见 _fit_board 的说明）。这时盘面必须靠上、水平居中，
        # 否则 pack 的默认行为会把它摆在垂直中间，上面留一大块空白。
        board_wrap = tk.Frame(left, bg="#4a3a22", bd=0)
        # ⚠️ 左栏可能比盘面宽（因为信息栏需要更宽的列才能不折行，
        # 见 _fit_board 的说明）→ 盘面必须**水平居中**，
        # 否则会贴在左边，右边留一大块深色空白（截图实测很难看）。
        # pack 的默认行为就是居中，只要不指定 side/anchor 即可。
        board_wrap.pack(pady=(6, 8))
        # ⚠️ 尺寸用 self.BOARD_PX，不许写死 430（2026-10-23 屏幕适配）
        # 小屏上这个值会是 300~400，_draw_board 也照它算格子间距。
        # 两处必须**同一个来源**：canvas 给 430 而 BOARD_PX 是 360，
        # 画出来只有左边一部分有棋盘，右边一大片木色空白。
        self.cv = tk.Canvas(board_wrap, width=self.BOARD_PX,
                            height=self.BOARD_PX,
                            bg=WOOD, highlightthickness=0, bd=0)
        self.cv.pack(padx=1, pady=1)

        # 信息栏：只显示「首选 / 计算量 / 胜率 / 目差」（老板 2026-10-23）
        # 原来的「该谁走」「末手」「黑N/白N」全部删掉 —— 那三项在实战里
        # 老板自己能看盘面看出来，而引擎的算力/胜率/目差是看不见的。
        #
        # ⚠️ height 必须给 5（不是 4）
        # ----------------------------------------------------
        # 「首选」那行用的是 16px 粗体，实际占约 1.5 行高度；
        # 给 4 行时最后一行「目差」会被裁掉一半（截图实测）。
        # 5 行刚好四行内容 + 一点余量。
        #
        # ⚠️ 高度策略（2026-10-23 美化时定稿）
        # ----------------------------------------------------
        # 第一版让它 expand=True 吃掉剩余高度，结果窗口一高就在
        # 盘面和信息栏之间拉出一大块空白（截图里很空）。
        # 现在：盘面固定、信息栏吃掉剩余，内容顶端对齐 ——
        # 四行永远紧贴盘面，多出来的空间留在卡片内部，不形成空洞。
        # ⚠️ width=1 是**故意的**（2026-10-23 分发级修复）
        # ----------------------------------------------------
        # tk.Text 的 width 单位是「字符数」，不给就默认 80 字符 ——
        # 在 Consolas 11 下 reqwidth 会算成 984px。而它外面是
        # `left` 容器（pack_propagate(False)、固定宽），实际宽度由容器定，
        # **这个 80 完全不参与显示**，只做一件事：把窗口的 reqwidth
        # 撑到 1322px。
        #
        # 后果：窗口 geometry 是 1080，比 reqwidth 小 242px，
        # Tk 会把里面所有东西挤一遍；在 1366x768 这类小屏上，
        # 尽管窗口已经贴到屏幕边，内容"想要"的宽度仍然更大 →
        # 日志区被压到很窄，右侧字被硬裁（没有省略号，直接从中间切断）。
        #
        # 改成 1：reqwidth 只剩 padding，实际尺寸照样由 fill="both" +
        # expand 决定，显示效果**完全不变**，但窗口终于能自由缩放了。
        self.info = tk.Text(left, height=5, width=1, bg="#1c212a", fg=FG, bd=0,
                            font=("Consolas", 11), padx=12, pady=10,
                            highlightthickness=0, spacing1=2, spacing3=4)
        self.info.pack(fill="both", expand=True)
        self.info.tag_configure("k", foreground=FG_DIM)
        self.info.tag_configure("v", foreground=ACCENT,
                                font=("Consolas", 11, "bold"))
        self.info.tag_configure("big", foreground=ACCENT2,
                                font=("Consolas", 16, "bold"))
        self.info.tag_configure("wr", foreground=GREEN,
                                font=("Consolas", 11, "bold"))
        self.info.tag_configure("lr", foreground=BLUE,
                                font=("Consolas", 11, "bold"))
        self.info.configure(state="disabled")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        # 日志标题行：左边标题，右边「清空」
        # 「清空」用 tk.Label 做按钮而不是 ttk.Button —— ttk.Button 有
        # 一圈边框和 padding，摆在「日志」这个小标题旁边显得很重。
        # Label 做链接式按钮，视觉上跟标题平级，鼠标悬停才变亮。
        head = ttk.Frame(right)
        head.pack(fill="x")
        self._section(head, "日志").pack(side="left")
        self.btn_clear = tk.Label(head, text="清空", bg=BG, fg=FG_DIM,
                                  font=("Microsoft YaHei UI", 9),
                                  cursor="hand2", padx=6)
        self.btn_clear.pack(side="right")
        self.btn_clear.bind("<Button-1>", lambda e: self._clear_log())
        self.btn_clear.bind("<Enter>", lambda e: self.btn_clear.configure(
            fg=ACCENT))
        self.btn_clear.bind("<Leave>", lambda e: self.btn_clear.configure(
            fg=FG_DIM))

        # ⚠️ 不用 scrolledtext（2026-10-23 美化时改）
        # scrolledtext 配的是**原生 tk.Scrollbar**，在深色界面上是一条
        # 惨白的竖条（截图里非常突兀，占了 17px 还带上下箭头方块）。
        # 改成 Text + ttk.Scrollbar，并给滚动条配一套深色样式。
        txt_wrap = tk.Frame(right, bg="#12161b", bd=0)
        txt_wrap.pack(fill="both", expand=True, pady=(6, 0))
        # ⚠️ width=1：同上，日志 Text 的默认 80 字符会把 reqwidth
        # 撑到 820px（2026-10-23）。它靠 fill="both" + expand 铺满，
        # 80 只影响 reqwidth、不影响显示。
        self.txt = tk.Text(
            txt_wrap, width=1, bg="#12161b", fg="#c7d0da", bd=0,
            insertbackground=FG,
            font=("Consolas", 9), padx=12, pady=10, highlightthickness=0,
            wrap="word", selectbackground="#3a4656", selectforeground=FG)
        self.sb = ttk.Scrollbar(txt_wrap, orient="vertical",
                                command=self.txt.yview, style="Dark.Vertical.TScrollbar")
        self.txt.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("dim", foreground=FG_DIM)
        self.txt.tag_configure("err", foreground=RED)
        self.txt.tag_configure("ok", foreground=GREEN)
        self.txt.tag_configure("hl", foreground=ACCENT2,
                               font=("Consolas", 9, "bold"))
        self.txt.tag_configure("move", foreground=BLUE)
        self.txt.configure(state="disabled")

        self._on_eng()

    # ------------------------------------------------ 引擎下拉

    def _fill_engines(self):
        """把（按设置筛过的）引擎灌进下拉框

        ⚠️ 序号重排（2026-10-23 老板要求）
        ----------------------------------------------------
        老板原话：「把里面的权重的序号整理一下，从上往下是 123456789 这样，
        现在是乱的」。engines.registry() 已经把 idx 重排成连续 1..N，
        这里直接用，不用再管。

        ⚠️ 但筛过之后序号会断（比如只勾了 1、5、9）
        ----------------------------------------------------
        那就**按当前可见列表重新编 1..N**：
        既然老板要的是「从上往下 123456789」，他看到的列表就该是连续的。
        原始编号（src_idx）不影响运行 —— 引擎靠 key 认，不靠 idx。
        """
        self.eng_map = {}
        items = []
        # 按设置筛（self.reg 就是 ED.merged() 的结果，跟给导向的那份同一个）
        try:
            reg, _why = ST.filter_engines(self.reg, self.cfg)
        except Exception:
            reg = list(self.reg)
        self.reg_shown = reg

        try:
            dk = ST.default_key(reg, self.cfg)
            if not dk:
                dk = REG.default_key(reg)
        except Exception:
            dk = None

        for n, e in enumerate(reg, 1):
            label = f"{n:>2}.  {e['name']}"
            items.append(label)
            self.eng_map[label] = e
            if e["key"] == dk:
                self.eng_var.set(label)
        self.eng_box["values"] = items
        if not self.eng_var.get() and items:
            self.eng_var.set(items[0])
        # 之前选的（比如刚筛掉的那一项）已经不在列表里 → 落回第一个
        if self.eng_var.get() not in self.eng_map and items:
            self.eng_var.set(items[0])
        self._fit_eng_box()
        # 引擎列表变了 → 推给控制接口（/api/engines 读的是这份快照）
        self._control_set_engines()

    def _fit_eng_box(self):
        """让下拉框按**实际内容**定宽，把省下的像素让给右边的说明

        ⚠️ 为什么不能写死 width=46（2026-10-23 实测）
        ----------------------------------------------------
        原来 width=46 ≈ 579px，而最长的一条选项
        「16.  18b死活对局 Kill-all (b18, komi=20)」只要 377px ——
        白白占了 200px。代价由右边那句说明承担：留给它的空间被压到
        400px 出头，最长的「死活专用 komi=20（无视目差，只看杀光/杀不光）」
        正好被截成「（无视目…」，**最关键的规则说明被切掉了**。

        也就是说：左边一条用不满的空白，换来右边一句看不全的话。
        修这个根子（按内容定宽），而不是把说明截得更狠。

        留 6px 余量：ttk 的下拉箭头 + 内边距会吃掉一点，
        正好卡在最长文本上时 Windows 主题下会显示成「…末尾」。

        ⚠️ 上限 46 字符：选项名是自动生成的（权重文件名可能很长），
        真遇到超长项就把框封顶，让它自己去省略 —— 不然一行又撑爆。
        """
        try:
            f = tkfont.Font(font=self.eng_box.cget("font"))
        except Exception:
            # 量不了字体（极早期 / 字体缺失）→ 退回最长的字符数估宽，
            # 不能就这么 return 把初值 width=28 留着 —— 那个数字对
            # 当前列表毫无意义，长名字会被截、短列表又浪费空间。
            vals = list(self.eng_box.cget("values") or [])
            if vals:
                self.eng_box.configure(
                    width=max(14, min(46, max(len(str(s)) for s in vals) + 2)))
            return
        vals = list(self.eng_box.cget("values") or [])
        if not vals:
            return
        # 按实际字体量像素，再折算成 ttk 的「字符宽」单位
        widest = max(vals, key=lambda s: f.measure(str(s)))
        px = f.measure(str(widest))
        # 一个「字符宽」在 ttk 里约等于 0 的宽度，用有限次测量换准确值
        try:
            unit = max(1, f.measure("0"))
        except Exception:
            unit = 11
        cols = max(12, min(46, int(px / unit) + 4))     # +4：留箭头和内边距
        self.eng_box.configure(width=cols)

    def _cur_engine(self):
        return self.eng_map.get(self.eng_var.get())

    def open_setup(self, first_run=False):
        """打开设置向导（首次自动弹 / 平时点「引擎设置」按钮）

        ⚠️ 对局进行中不许改（2026-10-23）
        ----------------------------------------------------
        引擎是 start() 那一刻按选中的那项造出来的。中途改列表不会换掉
        正在跑的引擎，只会让界面显示和实际不符 —— 老板会以为改没生效。
        所以跑着的时候直接提示先关引擎，比让他改完发现没用要诚实。
        """
        if self.running:
            messagebox.showinfo(
                "先关闭引擎",
                "引擎正在运行中。\n\n"
                "要改引擎列表，请先点「■ 关闭引擎」，改完再开启。",
                parent=self.root)
            return

        import setup_wizard as SW
        # ⚠️ 把主界面已知的检测结果传进去（2026-10-23）
        # 不传的话向导会自己再扫一遍，两次结果里的 key 生成方式必须
        # 完全一致才不出错 —— 与其保证一致，不如共用同一份数据。
        res = SW.ask(self.root, reg=self.reg, cfg=self.cfg,
                     first_run=first_run, detect=self.detect_res)
        if res is None:
            return                      # 用户取消（非首次），什么都不改

        if res.get("__skipped__"):
            # 首次点了「稍后再说」→ 记住「跳过」，但**不写 setup_done**
            # （2026-10-23 修正）
            # ----------------------------------------------------
            # 老行为：直接 setup_done=True。理由是"否则每次启动都弹会烦"。
            # 但在**没有可用引擎**的机器上（新电脑、没装智子/katago），
            # 这一跳把用户永久钉在"空下拉框 + 点开启只弹说明"的死角里。
            #
            # 新行为：写 setup_skipped，**同时看有没有可用引擎**：
            #   · 有可用引擎（老板的机器）→ 视为配好了，不再弹
            #     （老板原话「每次启动都弹会烦」，这层必须保住）
            #   · 一个可用引擎都没有 → 下次仍然弹，直到配好为止
            #     否则用户永远回不到配置入口
            n_ok = 0
            try:
                for e in (self.reg or []):
                    if self._engine_ready(e)[0]:
                        n_ok += 1
                        break
            except Exception:
                n_ok = 1        # 判不了就别烦用户
            self.cfg["setup_skipped"] = True
            if n_ok > 0:
                self.cfg["setup_done"] = True
                self._need_setup = False
            else:
                self.cfg.pop("setup_done", None)
                self._need_setup = True
                self.log("这台电脑还没有可用引擎 —— 下次启动会再提醒你配置。",
                         "err")
            ok, _ = ST.save(self.cfg)
            self.log("已跳过首次设置。随时点「引擎设置」重新配置。", "dim")
            return

        # 合并进 cfg 并写盘
        self.cfg.update(res)
        self.cfg["setup_done"] = True
        ok, msg = ST.save(self.cfg)
        if ok:
            self.log(f"设置已保存：{len(res.get('enabled') or [])} 个引擎", "ok")
        else:
            self.log(msg, "err")

        # ⚠️⚠️ 必须**重新扫描**，不能只按旧列表重建（2026-10-23 老板要求）
        # ----------------------------------------------------
        # 老板原话：「做一个设置的选项，就对应着首次设置，
        #             这样如果用户电脑里的引擎有变动，也能够修改」
        #
        # 老代码只做了 `self.reg_shown = []; self._fill_engines()`，
        # 用的是**启动那一刻**的 self.reg。于是：
        #   用户装了智子客户端 → 点「设置」→ 向导（它自己会重扫）
        #   确实看到新引擎、也勾上了 → 保存 → 回到主界面
        #   **下拉框里还是没有**，因为 self.reg 是旧的。
        # 用户会认为"设置没生效"，再点一次还是一样 —— 典型
        # 「改了没反应」，而且难查（向导里明明显示出来了）。
        #
        # 现在：保存后重扫一遍。重扫失败也不能把界面搞崩
        # （沿用旧列表 + 记一行日志，比清空好得多）。
        try:
            import engine_detect as ED
            self.detect_res = ED.detect(check_token=False)
            self.reg = ED.merged(REG.registry(), self.detect_res,
                                 extra=self.cfg.get("extra"))
            ok2, bad2 = REG.check(self.reg)
            self.reg_ok, self.reg_bad = ok2, bad2
            self.log(f"已重新扫描：现在共 {len(self.reg)} 个引擎", "dim")
        except Exception as ex:
            self.log(f"重新扫描失败（沿用旧列表）：{type(ex).__name__}: {ex}",
                     "err")

        # 界面立刻按新设置重建下拉框
        self.reg_shown = []
        self._fill_engines()
        self._on_eng()
        self._need_setup = False

    def _on_eng(self, *_):
        """切换引擎时更新右侧那句说明

        ⚠️ 文案分两类（老板 2026-10-23 原话）
        ----------------------------------------------------
        「云端，算力强」「本机，免费」—— 这是老板要的一句话卖点：
        用户扫一眼就知道该选哪边（要强 → 云端；不想花钱 → 本机）。

        但原来 note 里的东西（VIP 不限时 / 2.4 元/小时 / ≈14500 Elo）
        是老板真正做决策要看的，**不能丢**，所以接在后面当补充。
        顺序上「卖点在前、参数在后」—— 先让人看懂是什么，再给细节。
        """
        e = self._cur_engine()
        if not e:
            return
        note = e.get("note") or ""
        # cloud / local / lifego 三类都归到这两句里：
        # lifego 是本机权重，也免费，note 里的「死活专用」会说明它用来干什么
        head = "云端 · 算力强" if e["kind"] == "cloud" else "本机 · 免费"
        text = f"{head}　{note}" if note else head
        # ⚠️ 必须限宽（2026-10-23 实测出来的溢出）
        # ----------------------------------------------------
        # 引擎行是「引擎」+ 下拉框 + 这句说明。原来下拉框写死 width=46
        # （≈579px）而最长选项只要 377px，白白多占 200px —— 留给说明的
        # 空间被压到 400px 出头，于是最长的那条
        #   「本机 · 免费　死活专用 komi=20（无视目差，只看杀光/杀不光）」
        # 被截成「（无视目…」，**最关键的规则说明正好被切掉**。
        #
        # 两处都改成按实际像素算，而不是各拍一个数字：
        #   ① 下拉框：_fit_eng_box() 按内容定宽（省下 ~130px）
        #   ② 说明：这里按「窗口宽 - 说明的起始 x - 右边距」实时量
        # 这样窗口一缩（minsize 1000）它自己会收紧，不会撑破。
        #
        # 按**像素**限宽而不是字数：中英文混排下字数完全不可靠
        # （「18B」3 个字符 vs「死活专用」4 个字，宽度差一倍）。
        text = self._ellipsize(text, self._note_max_px())
        # 存全文：窗口缩放后 _reflow_note() 要拿它重算（不能拿截断后的）
        self._note_full = f"{head}　{note}" if note else head
        self.eng_note.configure(text=text, foreground=FG_DIM)
        self.btn_check.configure(state="normal")
        if not self.running:
            self._set_state("待机", FG_DIM)

    # 说明标签右侧要留的边距（跟 top 那个 Frame 的 padding 对齐）
    NOTE_RIGHT_PAD = 16

    def _note_max_px(self):
        """算出说明标签这一行实际能占多少像素

        ⚠️ 为什么要实时算而不是写死 400（2026-10-23）
        ----------------------------------------------------
        写死 400 是我第一版的修法，能防撑破，但太保守：窗口 1080 时
        「死活专用 komi=20（无视目差，只看杀光/杀不光）」需要 523px，
        明明放得下，却被切成「（无视目…」。老板一打开就看不到规则说明。

        正确口径是「这一行还剩多少」，它随两个东西变：
          · 下拉框宽度（已按内容自适应，_fit_eng_box）
          · 窗口宽度（老板可能拖窄到 minsize 1000）
        所以每次 _on_eng 都重新量一次。

        ⚠️ 用 winfo_x + winfo_width 而不是自己加常数：
        标签的起点由前面「引擎」二字 + 下拉框 + 各自的 padx 决定，
        任何一个改动（换字体、改文案、调 padx）都会让它移位，
        自己算必然对不上。让 Tk 算，我只负责减右边距。

        ⚠️ 窗口还没映射时 winfo_width() 返回 1
        （App.__init__ 里 _on_eng 早于 mainloop 就跑过一次），
        这时退回 reqwidth / 兜底值，否则第一句会被截成「云…」。
        """
        try:
            x = self.eng_note.winfo_x()
            # ⚠️ 还没映射时 winfo_x() 返回 0，直接减会算出「整窗都归我」，
            #    第一句就完全不截断，又被撑破。这时用下拉框右缘推算起点：
            #    说明标签就紧跟在「下拉框 + padx 6」后面。
            if x <= 1:
                x = (self.eng_box.winfo_x() + self.eng_box.winfo_width() + 6
                     if self.eng_box.winfo_width() > 1 else 660)
            rw = self.root.winfo_width()
            if rw <= 1:                 # 还没映射，用请求宽度
                rw = self.root.winfo_reqwidth()
            if rw <= 1:                 # 连请求宽度都没有（极早期）
                return 420
            avail = rw - x - self.NOTE_RIGHT_PAD
            # 夹在合理区间：太小等于什么都不显示，太大说明量错了（防呆）
            return max(160, min(avail, 900))
        except Exception:
            return 420

    def _reflow_note(self):
        """窗口尺寸变了，按新宽度重新截断（不重新取引擎信息）

        ⚠️ 必须用 self._note_full 里的**原文**重算（2026-10-23）
        ----------------------------------------------------
        最直觉的写法是拿标签当前显示的文本再截一次：
            self.eng_note.configure(text=self._ellipsize(cur, px))
        这样每调一次窗口就多砍一刀，最后只剩开头几个字 ——
        因为上一次的省略号也被当成了正文的一部分。

        所以 _on_eng() 把**完整文案**存进 self._note_full，
        这里永远从全文重算，截断是幂等的。
        """
        self._resize_job = None
        full = getattr(self, "_note_full", "")
        if not full:
            return
        try:
            self.eng_note.configure(
                text=self._ellipsize(full, self._note_max_px()))
        except Exception:
            pass

    def _ellipsize(self, text, max_px):
        """按像素把文字截断并加省略号

        为什么按像素（2026-10-23）
        ----------------------------------------------------
        中英混排时「字数」和「宽度」几乎没关系：
            "18B"        → 3 字符，约 26px
            "死活专用"    → 4 字符，约 56px
        按字数截会把宽的放过去、窄的切掉。所以用 tkinter 自己的
        font.measure() 量真实像素，逐级去掉尾巴直到放得下。

        找不到字体对象时退回保守的按字数截断（约 22px/字），
        保证任何情况下都不会撑破布局。
        """
        if not text:
            return text
        try:
            f = tkfont.nametofont("TkDefaultFont")
            # Dim.TLabel 用的是界面字体，量它
            try:
                f = tkfont.Font(font=self.eng_note.cget("font"))
            except Exception:
                pass
            if f.measure(text) <= max_px:
                return text
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if f.measure(text[:mid] + "…") <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            return text[:lo] + "…"
        except Exception:
            n = max(4, max_px // 22)
            return text if len(text) <= n else text[:n] + "…"

    # ------------------------------------------------ 贴目 / 对局信息

    def _commit_komi(self, _ev=None):
        """把贴目框里手输的内容**吸附到 0.5 并夹进 ±150**，写回控件

        ⚠️ 为什么必须有这一步（2026-10-23 实测 Tk 9.0.4）
        ----------------------------------------------------
        Spinbox 的 `increment` / `format` / `from_` / `to` 对**手输**
        一个都不管（实测：输 7.3 就留 7.3，输 999 就留 999，输 abc 也留）。
        老板要求「最小单位 0.5 且必须是合法贴目」，只能自己兜。

        三种输入的处理：
          · 合法数字（7.3）   → 吸附到 7.5，写回
          · 越界（999）       → 夹到 150
          · 空的 / 打一半的（"7." / "-" / "abc"）→ **回退到上次的有效值**
            不能留空着（start() 读到空串会报「贴目填的不是数字」），
            也不能默认成 7.5（会悄悄改掉老板原本设的值）。

        返回 True = 提交了一个新值（值真的变了）。
        """
        prev = getattr(self, "_komi_val", KOMI_DEFAULT)
        raw = None
        try:
            raw = self.komi_var.get()
        except Exception:
            raw = None
        v = komi_parse(raw)
        changed = False
        if v is None:
            # 打一半 / 非法 → 回退上次有效值，不弹窗打断
            v = prev
        else:
            snapped = komi_snap(v)
            if abs(snapped - v) > 1e-9:
                changed = True          # 值被吸附/夹过，界面要跟着改
            v = snapped
        txt = komi_text(v)
        if self.komi_var.get() != txt:
            self.komi_var.set(txt)
        self._komi_val = v
        # 吸附了（7.3 → 7.5）就留一行痕迹，让老板知道程序动过手
        # 静默改值最恼人：他看着自己打的 7.3 变成 7.5，不知道是谁干的
        if changed:
            self.log(f"贴目按 0.5 吸附：{str(raw).strip()} → {txt}", "dim")
        return changed

    def _lock_game_info(self, locked):
        """引擎启动后，把**改不了的对局信息**灰掉（老板 2026-10-23 要求）

        老板原话：
            「引擎启动之后，修改黑白似乎不成立，就是引擎启动之后，
              调不了的对局信息选项都禁掉，留下来的选项都是能调的」

        ⚠️ 哪些是真锁、哪些是留着的 —— 按「改了到底生不生效」判
        ----------------------------------------------------
        锁（start() 只在启动那一刻读一次，中途改只改界面、引擎不知道）：
          · 我执（黑白）—— 整个线程的 my 是构造 Auto 时定死的
          · 规则         —— Auto.rules 靠 _push_opts 推，但只在开局推一次
          · 贴目         —— 传进 Auto.komi，analyze 每手都用它
                            （这一项技术上能热改，但没有入口，锁上一并说明）

        不锁（有热更新链路，改了下一手就生效）：
          · 思考时间     -> Auto.set_think()（含固定/随机与上下限）
          · 引擎         -> 换引擎必须重开，但下拉框本来就该锁
                            （已有逻辑：start() 里 disabled，_finish 里 readonly）

        ⚠️ 用 "disabled" 而不是 "readonly"
        ----------------------------------------------------
        readonly 的 Combobox 视觉上还是亮的（能选中、能按方向键翻），
        用户会以为还能改；disabled 才是真正的灰掉 + 点不动。
        贴目那个 Spinbox 用 readonly 的话箭头仍可点（ttk 里 readonly 只
        拦键入），一样会误以为能调 —— 所以两个都用 disabled。

        ⚠️ 解锁时必须恢复 "readonly" 而不是 "normal"
        ----------------------------------------------------
        Combobox 恢复成 normal 就能在框里随便打字了（我执会变成「随便
        一个字符串」），而它本来只该从 ["白","黑"] 里选。所以解锁走
        readonly。贴目 Spinbox 恢复 normal 是**故意的** —— 它就是要能键入。

        ⚠️ 光改 state 在深色主题下**看不出来**（2026-10-27 实测）
        ----------------------------------------------------
        截图逐像素比对：锁定后全窗只有 1216 个像素变化，全挤在控件
        那一行。ttk 的 disabled 只是把前景色从 #ddd 降到 #888 —— 深色
        背景上这俩都算「亮」，整屏扫过去根本注意不到。
        老板要的是「一眼看出哪些调不了」，所以再加两件事：
          · 标签变暗 —— 连「我执」「规则」「贴目」这几个字一起灰掉，
            视觉块状更明显（只灰输入框，标签还是亮的，看着像没锁）
          · 标签加 🔒 —— 哪怕颜色看不出来，符号也说得明白
        解锁时两样都还原（原文存在 _wb_label_text 里）。
        """
        state_box = "disabled" if locked else "readonly"
        sp = getattr(self, "komi_spin", None)
        # (控件, 解锁后该回到的 state) —— 贴目是 normal（要能键入），
        # 另两个是 readonly（只能从下拉里选，不许乱打字）
        targets = [(getattr(self, "color_box", None), "readonly"),
                   (getattr(self, "rules_box", None), "readonly"),
                   (sp, "normal")]
        for w, unlock_state in targets:
            if w is None:
                continue
            try:
                w.configure(state="disabled" if locked else unlock_state)
            except Exception:
                pass

        # 标签：变暗 + 加锁标记
        for w, _unlock in targets:
            if w is None:
                continue
            lbl = getattr(w, "_wb_label", None)
            if lbl is None:
                continue
            base = getattr(w, "_wb_label_text", "")
            try:
                if locked:
                    lbl.configure(style="Locked.TLabel", text=f"🔒 {base}")
                else:
                    lbl.configure(style="Dim.TLabel", text=base)
            except Exception:
                pass

        # 锁的时候如果焦点正在贴目框里，Tk 会把焦点留在 disabled 控件上
        # —— 之后按 ESC/Tab 都怪怪的。主动挪走。
        if locked:
            try:
                if self.root.focus_get() is getattr(self, "komi_spin", None):
                    self.btn_go.focus_set()
            except Exception:
                pass
        self._game_info_locked = bool(locked)

    def _on_gap_mode(self):
        """固定/随机 切换时，把用不上的那几个框灰掉

        只灰不隐藏：布局不会跳，老板也不会因为控件忽然消失而找不到。
        （灰掉 vs 藏起来：藏起来会让整行宽度变化、下面的按钮跟着移位）
        """
        mode = self.gap_mode.get()
        fixed, rnd, vis = mode == "fixed", mode == "random", mode == "visits"
        self.gap_spin.configure(state="normal" if fixed else "disabled")
        for w in (self.gap_min_spin, self.gap_max_spin):
            w.configure(state="normal" if rnd else "disabled")
        self.visits_spin.configure(state="normal" if vis else "disabled")
        # 模式本身就是最重要的那个参数，切了要立刻下发
        self._on_think_change()

    def _set_visits(self, n):
        """把计算量框的显示值与回退基准成对写入（同 _set_time 的道理）"""
        try:
            txt = visits_text(n)
            if self.visits_var.get() != txt:
                self.visits_var.set(txt)
            self.visits_spin._wb_last = n
        except Exception:
            pass

    def _set_time(self, w, txt):
        """把时间框的显示值和「回退基准」**成对**写进去

        ⚠️ 为什么要包一层（2026-10-27）
        ----------------------------------------------------
        `_wb_last` 是「输入真空/非法时回退到哪儿」的基准。它必须和
        界面显示的**永远一致**，否则会出现这种鬼事：

            界面显示 180（`_on_think_change` 夹紧后写的）
            但 `_wb_last` 还停在 0.5（上次提交时的值）
            → 老板清空重输，回退成 0.5，时间凭空少了两百多秒

        这类 bug 单点看不出来：`_commit_time` 里两个都更新了，
        `_on_think_change` 里只更新了显示值。所以显示值的**所有**
        写入点统一走这里，别再各自 set。

        捕获异常是必须的：控件可能已经被销毁（关窗瞬间的 FocusOut）。
        """
        try:
            cur = self.root.getvar(w.cget("textvariable"))
        except Exception:
            cur = None
        if cur != txt:
            try:
                w.set(txt)
            except Exception:
                try:
                    self.root.setvar(w.cget("textvariable"), txt)
                except Exception:
                    pass
        try:
            v = time_parse(txt)
            if v is not None:
                w._wb_last = time_snap(v, step=getattr(w, "_step", THINK_STEP))
        except Exception:
            pass

    def _commit_time(self, ev=None):
        """把时间框里手输的内容**吸附到 0.5 并夹进 [0.5, 180]**，写回控件

        ⚠️ 为什么必须有这一步（老板 2026-10-27 实测 Tk 9.0.4）
        ----------------------------------------------------
        和贴目一模一样的坑：Spinbox 的 increment / from_ / to 对手输
        一个都不管（输 7.3 就留 7.3、输 999 就留 999、输 abc 也留）。
        老板要求「最小单位 0.5，0.5 到 180 之间」，只能自己兜。

        三种输入：
          · 合法数字（7.3）  → 吸附到 7.5，写回
          · 越界（999 / 0）  → 夹到 180 / 0.5
          · 打一半（"1." / "" / 非数字）→ **回退到上次有效值**
            不能留空（start() 会报「填的不是数字」），也不能默认成 5
            （会悄悄改掉老板原本设的值）

        返回 True = 提交了一个新值（值真的变了）。
        """
        w = ev.widget if (ev is not None and hasattr(ev, "widget")) else None
        if w is None:
            return False
        try:
            var = w.cget("textvariable")
            var = self.root.getvar(var) if var else ""
        except Exception:
            return False
        prev = float(getattr(w, "_wb_last", THINK_DEFAULT))
        raw = var
        v = time_parse(raw)
        changed = False
        if v is None:
            v = prev                # 真空 / 非数字 → 回退上次有效值
        else:
            snapped = time_snap(v, step=getattr(w, "_step", THINK_STEP))
            if abs(snapped - v) > 1e-9:
                changed = True      # 被吸附/夹过，界面要跟着改
            v = snapped
        txt = time_text(v)
        self._set_time(w, txt)
        # 吸附过就留一行痕迹，让老板知道程序动过手（静默改值最恼人）
        if changed:
            self.log(f"时间按 {getattr(w, '_step', THINK_STEP):g} 秒吸附："
                     f"{str(raw).strip()} → {txt} 秒", "dim")
        # 提交之后照常走热更新链路
        self._on_think_change()
        return changed

    def _on_think_change(self, _ev=None):
        """思考时间改了 —— 下棋中立刻生效（老板 2026-10-23 要求）

        ⚠️ 三条设计约束
        ----------------------------------------------------
        ① **不在下棋时也要能用**：没开引擎时只更新 UI 状态，
           值会留在这里，等 start() 时被读进去。
        ② **不能因为填错就弹窗**：老板在思考 5 秒的空档里改个数字，
           手快点出一个 `5..` 就弹一个错误框打断对局，太烦。
           所以这里宽松解析，解析不出来就保持原值、不提交。
        ③ **同一值反复提交要去重**：Spinbox 的 command 在点箭头时
           会连发多次，不去重会刷一屏日志。Auto.set_think 自己会
           返回「没变化」的标志，这里只在真变了时写一行。

        返回 True = 这次提交真的改变了运行中的设置。
        """
        try:
            mode = self.gap_mode.get()
            # ⚠️ 数值边界用 time_snap 兜（2026-10-27）
            # ----------------------------------------------------
            # 老代码这里只判 `v < 0`，是「下限 0 秒」时代的写法。
            # 现在下限抬到 0.5，而且允许键入 —— 用户完全可能打
            # "0.2" 或 "999" 然后直接去点开始。这里统一吸附+夹紧，
            # 保证交给 Auto 的**永远是合法值**，同时把界面也改过来
            # （否则界面上写着 999、实际按 180 跑，看着像 bug）。
            if mode == "random":
                lo = FA._to_float(self.gap_min_var.get(), None)
                hi = FA._to_float(self.gap_max_var.get(), None)
                if lo is None or hi is None:
                    return False
                lo2, hi2 = time_snap(lo), time_snap(hi)
                if lo2 > hi2:
                    lo2, hi2 = hi2, lo2      # 填反了就换回来，别报错打断
                # ⚠️ 走 _set_time 而不是 var.set：显示值和 _wb_last
                #    必须成对更新，否则夹紧之后回退基准是旧值（见 _set_time）
                self._set_time(self.gap_min_spin, time_text(lo2))
                self._set_time(self.gap_max_spin, time_text(hi2))
                lo, hi = lo2, hi2
            elif mode == "visits":
                n = visits_snap(self.visits_var.get())
                self._set_visits(n)
                v = None
            else:
                v = FA._to_float(self.gap_var.get(), None)
                if v is None:
                    return False
                v2 = time_snap(v, step=THINK_FIXED_STEP)
                self._set_time(self.gap_spin, time_text(v2, step=THINK_FIXED_STEP))
                v = v2

            bot = getattr(self, "auto", None)
            if not (self.running and bot):
                return False        # 没在下棋：值留在界面，start() 会读

            if mode == "random":
                ok, txt = bot.set_think(mode="random", lo=lo, hi=hi)
            elif mode == "visits":
                ok, txt = bot.set_think(mode="visits", visits=n)
            else:
                ok, txt = bot.set_think(think=v, mode="fixed")
            if not ok:
                return False

            # 去重：跟上次落地成功的那句相比，一样就不刷日志
            if getattr(self, "_think_txt", None) != txt:
                self._think_txt = txt
                self.log(f"思考时间改为 {txt}（下一手生效）", "dim")
            return True
        except Exception as e:
            # 这里的任何异常都不该影响对局，静默吞掉但留个痕迹
            try:
                self.log(f"（思考时间改动没生效：{type(e).__name__}: {e}）",
                         "dim")
            except Exception:
                pass
            return False

    # ------------------------------------------------ 快捷键

    # ------------------------------------------------ 快捷键 / 悬浮球

    def _bind_keys(self):
        """只留窗口关闭处理 —— **ESC 已取消**（2026-11-02 老板要求）

        原话：「把 ESC 这个快捷键取消掉，改为开始对局之后，窗口最小化
              并在屏幕上出现一个悬浮球……有开启/关闭引擎，继续/暂停
              这两个按钮，还出现两行左右的日志」。

        所以这里不再 bind <Escape>，也不再注册全局热键
        （hotkey.py 里的 GlobalHotkey 从此不被 gui 使用）。
        对局中所有操作都走悬浮球；暂停键只保留界面上的按钮。
        """
        # 关闭窗口时也把引擎关掉，别留下 zz-ikatago / katago 常驻
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 2026-11-08：F1 能力/指令说明；F2 在自动对弈 / AI 托管之间切页。
        # 悬浮球被收起、或窗口特别小时，键盘也能找到入口。
        self.root.bind("<F1>", self._show_ai_help)
        self.root.bind("<F2>", self._toggle_page)

    def _show_ai_help(self, _ev=None):
        try:
            self.show_page("ai")
            if self.ai_page is not None:
                self.ai_page._show_help()
        except Exception:
            pass
        return "break"

    def _toggle_page(self, _ev=None):
        try:
            self.show_page("auto" if self._page == "ai" else "ai")
        except Exception:
            pass
        return "break"

    # ------------------------------------------------ 自动更新

    def _auto_check_update(self):
        """启动几秒后后台查一次 Gitee（静默失败，不打扰用户）

        2026-11-02 老板要求「做成内置自动更新」：
          · 仓库根目录放 version.json（version / url / md5 / notes）
          · 有新版本 → 问一句，确认后下载、校验 MD5、写 update.bat 重启
          · 源码运行时不自动替换（只提示），避免把 python.exe 换成 exe
        """
        try:
            import threading
            import updater

            def _bg():
                info = updater.check(APP_VERSION)
                if info:
                    self.root.after(0, lambda: self._ask_update(info))

            threading.Thread(target=_bg, daemon=True).start()
        except Exception:
            pass                    # 没网 / 被墙 / 仓库还没建好，都当无事发生

    def _ask_update(self, info):
        try:
            import threading
            import updater
            ver = info.get("version")
            notes = (info.get("notes") or "").strip()
            msg = f"发现新版本 v{ver}（当前 v{APP_VERSION}）"
            if notes:
                msg += "\n\n" + notes[:400]
            msg += "\n\n现在下载并更新吗？更新完成后程序会自动重启。"
            if not messagebox.askyesno("野狐自动对弈 · 发现新版本", msg):
                return
            self.log(f"开始下载更新 v{ver} …")
            self._set_state("更新中", ACCENT)

            def _dl():
                try:
                    import runtime_paths as RP
                    upd = os.path.join(RP.ensure_dirs(), "update")
                    new = updater.download(info, upd)
                    target = updater.current_exe()
                    if not target:
                        self.root.after(0, lambda: messagebox.showinfo(
                            "更新", "源码模式不自动替换文件。\n"
                                    "新版已下载到：\n" + new))
                        return
                    updater.install_now(new, target, upd)
                    self.root.after(0, lambda: (
                        self.log("更新已就绪，正在重启…"),
                        self.root.after(300, lambda: os._exit(0))))
                except Exception as e:
                    self.root.after(0, lambda: messagebox.showerror(
                        "更新失败", f"{type(e).__name__}: {e}"))

            threading.Thread(target=_dl, daemon=True).start()
        except Exception:
            pass

    # ------------------------------------------------ 悬浮球

    def _ensure_ball(self):
        """第一次开引擎时才建悬浮球（懒创建，不用的时候零开销）"""
        if self.ball is not None:
            return self.ball
        try:
            import hud
            self.ball = hud.FloatBall(
                self.root,
                on_engine=self._ball_engine,
                on_pause=self._ball_pause,
                on_dismiss=self._ball_dismiss)
            self.ball.set_logs(getattr(self, "_ball_lines", []))
        except Exception as e:
            self.ball = None
            try:
                self.log(f"（悬浮球没建起来：{type(e).__name__}: {e}）", "dim")
            except Exception:
                pass
        return self.ball

    def _ball_place(self):
        """摆到棋盘之外 —— 用当前标定的几何 + 野狐窗口算"""
        ball = self._ensure_ball()
        if ball is None:
            return
        hwnd = None
        try:
            hits = FB.find_hwnd()
            if hits:
                # 最小化时 GetWindowRect 是 158x26 的假矩形，拿它算位置
                # 会把球甩到奇怪的地方 —— 这种情况按整块屏幕算。
                if not hits[0][6]:
                    hwnd = hits[0][0]
        except Exception:
            pass
        geom = FB.GEOM if hwnd else None
        if hwnd and (not geom or not geom.get("ok")):
            # 还没标定过 —— 趁开局读一次盘把几何补上（纯读，不动野狐窗口）。
            # 补不上就退回 hud 里的正方形估计，至少不会压到棋盘中间。
            try:
                im, _t, _o = FB.grab()
                g2 = FB.calibrate(im)
                if g2.get("ok"):
                    geom = g2
                    FB.GEOM = g2
                    self.log(f"标定棋盘：step={g2['step']:.2f}", "dim")
            except Exception:
                geom = None
        ball.place_beside(hwnd, geom)

    def _show_ball(self):
        """开跑：球出现（主窗口同时收起来）

        ⚠️ AI 托管模式下不显示自动对弈的球：同一时刻只留 AI 指令球，
        否则屏幕上两颗球、两套状态，用户分不清哪个在管。
        """
        if getattr(self, "_ai_mode_active", False):
            if self.ball is not None:
                try:
                    self.ball.hide()
                except Exception:
                    pass
            self._ball_visible = False
            return
        ball = self._ensure_ball()
        if ball is None:
            return
        self._ball_place()
        ball.set_running(bool(self.running), bool(self.paused))
        ball.show()
        self._ball_visible = True

    def _hide_ball(self):
        self._ball_visible = False
        if self.ball is not None:
            self.ball.hide()

    def _ball_engine(self):
        """球上的「开启引擎 ⇄ 关闭引擎」"""
        if self.running:
            self.stop()          # 收工会把主窗口放回来
        else:
            # 参数沿用上一次确认过的（球上没有输入框），所以跳过确认框
            self.start(quick=True)

    def _ball_pause(self):
        """球上的「暂停 ⇄ 继续」"""
        self.toggle_pause()

    def _ball_dismiss(self):
        """停手之后点 × —— 只收球，程序和主窗口都还在"""
        self._ball_visible = False

    def _sync_ball(self):
        """把运行/暂停状态同步给球（按钮文字、状态点）"""
        ball = getattr(self, "ball", None)
        if ball is None:
            return
        try:
            ball.set_running(bool(self.running), bool(self.paused))
            if not self.running:
                ball.set_state("已停止", FG_DIM)
            elif self.paused:
                ball.set_state("已暂停", ACCENT)
            else:
                ball.set_state(f"运行中 · 我执{self._my_color_text()}", GREEN)
        except Exception:
            pass

    # ================================================ AI 托管（2026-11-08）
    # 与自动对弈页是两个独立页面；AI 页只通过 Agent 说话，不直接操纵控件。
    # 线程规则：AI Agent 在后台线程，所有界面更新都从 q 进 _handle（Tk 线程）。

    def show_page(self, key):
        """切页：auto / ai"""
        key = "ai" if str(key).lower() in ("ai", "托管") else "auto"
        if key == "ai":
            self._build_ai_page()
        if key == self._page:
            if key == "ai":
                self.ai_page.refresh_api()
                self.ai_page.refresh_policy()
                self.ai_page.refresh_tasks()
            return
        self._page = key
        for k, lab in (self._page_btns or {}).items():
            on = (k == key)
            try:
                lab.configure(fg=(ACCENT if on else FG_DIM))
                self._page_underlines[k].configure(bg=(ACCENT if on else HDR))
            except Exception:
                pass
        try:
            if key == "ai":
                self.page_auto.pack_forget()
                self.page_ai.pack(fill="both", expand=True)
                if self.ai_page is not None:
                    self.ai_page.refresh_api()
                    self.ai_page.refresh_policy()
                    self.ai_page.refresh_tasks()
            else:
                self.page_ai.pack_forget()
                self.page_auto.pack(fill="both", expand=True)
                self.root.after(100, self._fit_board)
        except Exception:
            pass

    def _build_ai_page(self):
        if self.ai_page is not None:
            return
        try:
            import ai_page as AP
            self.ai_page = AP.AIPage(
                self, self.page_ai,
                on_send=self.ai_send,
                on_start=self.ai_start,
                on_stop=self.ai_stop,
                on_save_api=self.ai_save_api,
                on_test_api=self.ai_test_api,
                on_cancel_task=self.ai_cancel_task)
            for m in (self.ai_messages or [])[-24:]:
                self.ai_page.append_message(m.get("role"), m.get("text"),
                                            m.get("ts", ""))
            if not (self.ai and self.ai.api_ready):
                self.ai_page.show_setup()
        except Exception as e:
            self.ai_page = None
            self.log(f"AI 页没建起来：{type(e).__name__}: {e}", "err")

    def _init_ai(self):
        try:
            import ai_agent
            self.ai = ai_agent.AIAgent(self)
            self.log("AI 托管已就绪（" + (
                "API 已连接" if self.ai.api_ready else "未配置 API") + "）", "dim")
        except Exception as e:
            self.ai = None
            self.log(f"AI 托管初始化失败：{type(e).__name__}: {e}", "err")

    # ---------------------------------------------------------- 回调
    def ai_send(self, text):
        if self.ai is None:
            self.log("AI 托管没初始化，发不了指令", "err")
            return
        self._ai_mode_active = True
        self.ai.send(text)

    def ai_start(self):
        if self.ai is None:
            self.log("AI 托管没初始化", "err")
            return
        if not self.ai.api_ready:
            self.show_page("ai")
            if self.ai_page is not None:
                self.ai_page.show_setup()
                self.ai_page.append_message(
                    "system", "先填 API Key 才能托管；推荐火山方舟。")
            return
        self._ai_mode_active = True
        self.ai.start()
        self._ai_enter_running()

    def ai_stop(self):
        try:
            if self.ai is not None:
                self.ai.stop("用户停止托管")
        except Exception:
            pass
        self._ai_mode_active = False
        self._ai_hover_paused = False
        self._ai_hide_ball()
        self._restore_self()

    def ai_cancel_task(self):
        try:
            if self.ai is not None:
                self.ai.cancel_task()
        except Exception as e:
            self.log(f"取消失败：{e}", "err")

    def ai_save_api(self, data):
        if self.ai is None:
            return False, "AI 托管没初始化"
        ok, msg = self.ai.configure_llm(
            data.get("base_url"), data.get("model"), data.get("api_key"),
            vision=data.get("vision"))
        try:
            if self.ai_page is not None:
                self.ai_page.refresh_api()
        except Exception:
            pass
        return ok, msg

    def ai_test_api(self, data):
        """后台测连通性（不落盘、不改现有配置）"""
        if isinstance(data, str):
            data = {"api_key": data}
        try:
            import ai_llm
            key = str(data.get("api_key") or "").strip()
            base = str(data.get("base_url") or "").strip()
            model = str(data.get("model") or "").strip()
            if not base or not model:
                cfg = (self.ai.settings or {}).get("llm") if self.ai else {}
                cfg = cfg or {}
                base = base or cfg.get("base_url") or \
                    "https://ark.cn-beijing.volces.com/api/v3"
                model = model or cfg.get("model") or \
                    "doubao-seed-1-6-vision-250815"
            cli = ai_llm.LLMClient(base_url=base, api_key=key, model=model,
                                   timeout=45)
        except Exception as e:
            if self.ai_page is not None:
                self.ai_page.set_test_result(False, str(e))
            return

        def work():
            try:
                reply = cli.test()
                ok, msg = True, str(reply)[:120]
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"[:240]
            try:
                self.root.after(0, lambda: self.ai_page
                                and self.ai_page.set_test_result(ok, msg))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------- 悬浮球
    def _ai_enter_running(self):
        self._ai_show_ball()
        try:
            self._minimize_self(force=True)
        except Exception:
            pass

    def _ensure_ai_ball(self):
        if self.ai_ball is not None:
            return self.ai_ball
        try:
            import ai_hud
            self.ai_ball = ai_hud.AIBall(
                self.root,
                on_hover=self._ai_hover,
                on_send=self.ai_send,
                on_close=self.ai_stop,
                on_open=self._ai_open_main)
            self.ai_ball.set_messages(self.ai_messages)
        except Exception as e:
            self.ai_ball = None
            self.log(f"AI 悬浮球没建起来：{type(e).__name__}: {e}", "err")
        return self.ai_ball

    def _ai_ball_place(self):
        ball = self._ensure_ai_ball()
        if ball is None:
            return
        hwnd = None
        try:
            hits = FB.find_hwnd()
            if hits and not hits[0][6]:
                hwnd = hits[0][0]
        except Exception:
            pass
        geom = FB.GEOM if hwnd else None
        if hwnd and (not geom or not geom.get("ok")):
            try:
                im, _t, _o = FB.grab()
                g2 = FB.calibrate(im)
                if g2.get("ok"):
                    geom = g2
                    FB.GEOM = g2
            except Exception:
                geom = None
        ball.place_beside(hwnd, geom)

    def _ai_show_ball(self):
        ball = self._ensure_ai_ball()
        if ball is None:
            return
        self._ai_ball_place()
        try:
            snap = self.ai.status_snapshot() if self.ai else {}
            ball.set_state(snap.get("text") or "托管中",
                           GREEN if snap.get("state") == "running" else ACCENT,
                           snap.get("state"))
        except Exception:
            pass
        ball.set_messages(self.ai_messages)
        ball.show()
        self._ai_visible = True

    def _ai_hide_ball(self):
        self._ai_visible = False
        if self.ai_ball is not None:
            try:
                self.ai_ball.hide()
            except Exception:
                pass

    def _ai_hover(self, inside):
        """鼠标进入/离开 AI 球：暂停/恢复自动对弈 + Agent"""
        try:
            if self.ai is not None:
                self.ai.set_hover(bool(inside))
        except Exception:
            pass
        if inside:
            if self.running and not self.paused:
                self._ai_hover_paused = True
                try:
                    self.pause()
                except Exception:
                    pass
        else:
            if self._ai_hover_paused and self.running:
                try:
                    self.resume()
                except Exception:
                    pass
            self._ai_hover_paused = False

    def _ai_open_main(self):
        """AI 球面板上的「主界面」：回来但不停止托管"""
        try:
            self.show_page("ai")
        except Exception:
            pass
        self._restore_self()
        try:
            self.root.lift()
        except Exception:
            pass

    # ---------------------------------------------------------- 事件
    def _handle_ai_event(self, kind, payload):
        if kind == "ai_msg":
            m = payload if isinstance(payload, dict) else {"text": str(payload)}
            self.ai_messages.append(m)
            del self.ai_messages[:-80]
            if self.ai_page is not None:
                self.ai_page.append_message(
                    m.get("role"), m.get("text"), m.get("ts", ""))
            if self.ai_ball is not None:
                try:
                    self.ai_ball.add_message(m.get("role"), m.get("text"),
                                             m.get("ts", ""))
                except Exception:
                    pass
        elif kind == "ai_state":
            p = payload if isinstance(payload, dict) else {}
            st = p.get("state") or "idle"
            color = {"idle": FG_DIM, "running": GREEN, "paused": ACCENT,
                     "thinking": BLUE, "error": RED}.get(st, FG_DIM)
            if self.ai_page is not None:
                self.ai_page.set_state(p)
            if self.ai_ball is not None:
                try:
                    self.ai_ball.set_state(p.get("text") or st, color, st)
                except Exception:
                    pass
            if st == "running" and self._ai_mode_active:
                self._ai_enter_running()
        elif kind == "ai_task":
            if self.ai_page is not None:
                try:
                    self.ai_page.refresh_tasks()
                except Exception:
                    pass
        elif kind == "ai_log":
            self.log(f"[AI] {payload}", "dim")


    def _minimize_self(self, force=False):
        """下棋时把本窗口最小化

        老板 2026-11-02：「开始对局之后，窗口最小化并在屏幕上出现一个悬浮球」。
        为什么必须这样：本窗口只要有任何一个像素压在棋盘上，
        鼠标点击就会落在它身上，野狐收不到 —— 表现就是「一手都不下」。

        2026-11-08：AI 托管开始时要立即收起（此时引擎可能还没 running），
        所以加了 force；自动对弈原来的调用方式不变。
        """
        try:
            if force or (self.running and not self.paused):
                self.root.iconify()
        except Exception:
            pass

    def _restore_self(self):
        """把主窗口放回来（收工 / 暂停需要改设置 / 出错时）"""
        if getattr(self, "_closing", False):
            return          # 正在关窗：别把已经藏起来的窗口又弹回来
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
            self.root.lift()
        except Exception:
            pass

    def _save_ui_state(self):
        """把界面上的参数与当前选中的引擎记进 settings.json

        只记「用户会想保留」的东西：我执 / 规则 / 贴目 /
        思考时间 / 默认引擎。
        盘面、日志、运行状态一概不记（那是会话状态，不是偏好）。
        """
        ui = dict(self.cfg.get("ui") or {})
        try:
            ui["color"] = self.color_var.get()
            ui["rules"] = self.rules_var.get()
            ui["komi"] = self.komi_var.get()
            ui["think_mode"] = self.gap_mode.get()
            ui["think"] = self.gap_var.get()
            ui["think_min"] = self.gap_min_var.get()
            ui["think_max"] = self.gap_max_var.get()
            ui["visits"] = self.visits_var.get()
        except Exception:
            pass
        self.cfg["ui"] = ui

        # ⚠️ window.global_esc 是老配置里的历史字段：2026-11-02 起
        # ESC 彻底取消（改成悬浮球），这个字段不再读也不再写。
        # 不删它 —— 删要动 load/save 两条路径，收益为零；留着没人读。
        self.cfg["window"] = dict(self.cfg.get("window") or {})

        # 默认引擎 = 下拉框当前选的那项（下次打开还是它）
        try:
            e = self._cur_engine()
            if e and e.get("key"):
                self.cfg["default_key"] = e["key"]
        except Exception:
            pass

        self.cfg["setup_done"] = True
        ok, msg = ST.save(self.cfg)
        if not ok:
            try:
                self.log(msg, "err")
            except Exception:
                pass
        return ok

    # ================================================ 本机控制接口（2026-11-05）
    # 给 fox-agent 用的 127.0.0.1:8767；契约见 projects/fox-agent/DESIGN.md 第 4 节。
    #
    # 三条硬规矩（DESIGN 4.2，也是这里所有写法的原因）：
    #   ① HTTP 线程**绝不碰 Tk**。GUI 动作统一走 self.q 投递，由 _poll（每 100ms
    #      的 after 循环）在 Tk 线程里执行 —— 这正是本程序"后台线程只往队列塞
    #      消息"的既有设计，比跨线程 root.after 更安全。
    #   ② /api/status 只读带锁的 ControlState，绝不现场查 Tk。
    #   ③ /api/start 复用 start(quick=True)，不复制启动逻辑；
    #      为了让 HTTP 调用**不弹任何对话框**，调用前先做一遍只读预检
    #      （引擎/野狐/参数），预检不过就直接返回错误码，绝不进 start() 的弹窗分支。

    def _init_control(self):
        """建共享状态 + 服务对象（**不监听端口**，监听在 main() 里）"""
        self.control_state = None
        self.control_server = None
        if CS is None:
            return
        try:
            self.control_state = CS.ControlState()
            self.control_server = CS.ControlServer(
                app=self,
                state=self.control_state,
                version=APP_VERSION,
                ui_call=self._control_call,
                log=self._control_log)
            self._sync_control()            # 先把当前界面参数灌进去
            self._control_set_engines()
        except Exception as e:
            self.control_state = None
            self.control_server = None
            try:
                self.log(f"控制接口初始化失败：{type(e).__name__}: {e}", "err")
            except Exception:
                pass

    def start_control_server(self):
        """真正开始监听（main() 在 mainloop 之前调）

        失败（端口被占 / token 写不了）只记一行日志 —— 人工功能不受影响。
        """
        srv = getattr(self, "control_server", None)
        if srv is None:
            return None
        try:
            ok = srv.start()
        except Exception as e:
            self.log(f"控制接口启动失败：{type(e).__name__}: {e}", "err")
            return None
        if not ok:
            self.log("控制接口没起来（8767 被占用？其它功能不受影响）", "dim")
            return None
        if srv.token_path:
            self.log(f"控制接口已就绪：127.0.0.1:{srv.port}"
                     f"（token：{srv.token_path}）", "dim")
        else:
            self.log(f"控制接口已就绪：127.0.0.1:{srv.port}", "dim")
        return srv

    def _control_log(self, msg):
        """HTTP 线程的日志出口 —— 只写会话日志文件，**绝不碰 Tk 控件**"""
        try:
            self._log_to_file(msg)
        except Exception:
            pass

    def _control_call(self, fn, timeout=8.0):
        """HTTP 线程 → GUI 线程的唯一通道：投队列、等在 Tk 线程执行完

        返回 fn 的返回值；fn 抛什么这里原样抛回给 HTTP 线程
        （ControlServer 会把 ValueError 翻成 400、其余翻成 500）。
        """
        done = threading.Event()
        box = {}
        try:
            self.q.put(("ctl", (fn, done, box)))
        except Exception as e:
            raise RuntimeError(f"GUI 消息队列不可用：{e}")
        wait = max(0.1, float(timeout or 8.0))
        if not done.wait(wait):
            raise TimeoutError(f"GUI 在 {wait:g} 秒内没有响应")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def _handle_control_call(self, p):
        """_poll 里执行 ("ctl", (fn, done, box))：这一步一定在 Tk 线程上"""
        try:
            fn, done, box = p
        except Exception:
            return
        try:
            v = fn()
            if box is not None:
                box["v"] = v
        except Exception as e:
            if box is not None:
                box["e"] = e
            else:
                try:
                    self.log(f"控制接口动作失败："
                             f"{type(e).__name__}: {e}", "err")
                except Exception:
                    pass
        finally:
            if done is not None:
                try:
                    done.set()
                except Exception:
                    pass

    def _control_set_engines(self):
        """把下拉框里实际的引擎列表推给接口（Tk 线程调用）

        不从 HTTP 线程查 Tk —— 列表变了由这边主动推（_fill_engines 末尾）。
        """
        srv = getattr(self, "control_server", None)
        if srv is None:
            return
        try:
            reg = (getattr(self, "reg_shown", None)
                   or getattr(self, "reg", None) or [])
            srv.set_engines(reg)
        except Exception:
            pass

    def _control_think_values(self, d):
        """把三个时间框/一个计算量读进 d（解析不出来就跳过，保留旧值）"""
        try:
            mode = self.gap_mode.get()
        except Exception:
            mode = None
        if mode in ("fixed", "random", "visits"):
            d["think_mode"] = mode
        try:
            v = time_parse(self.gap_var.get())
            if v is not None:
                d["think"] = time_snap(v, step=THINK_FIXED_STEP)
        except Exception:
            pass
        try:
            v = time_parse(self.gap_min_var.get())
            if v is not None:
                d["think_min"] = time_snap(v)
        except Exception:
            pass
        try:
            v = time_parse(self.gap_max_var.get())
            if v is not None:
                d["think_max"] = time_snap(v)
        except Exception:
            pass
        try:
            d["visits"] = visits_snap(self.visits_var.get())
        except Exception:
            pass

    def _sync_control(self):
        """每 100ms（_poll 里）把 GUI 的真实状态推进 ControlState

        ⚠️ 这是「/api/status 不查 Tk」的落点：Tk 变量只在**这个线程**读一次，
        接口那边永远读共享状态。
        """
        st = getattr(self, "control_state", None)
        if st is None or getattr(self, "_closing", False):
            return
        try:
            running = bool(self.running)
            paused = bool(self.paused)
            d = {"running": running, "paused": paused}
            cur = st.get("state") or "idle"
            stop_req = bool(getattr(self, "_stop_requested", False))
            if not running:
                if cur in ("starting", "running", "paused"):
                    cur = "done"
            elif paused:
                cur = "paused"
            elif cur == "paused":
                cur = "running"
            elif cur == "idle" or (cur == "done" and not stop_req):
                # 上一盘结束后又开跑（GUI 按钮或接口都算）：新一局从 starting 起
                # ⚠️ 不把 error 也拉回 starting：错误信息要留到下一局开工，
                #    否则 Auto 报错后界面（和接口）就只剩一句「启动中」。
                cur = "starting"
                d["game_over"] = False
                d["move_no"] = 0
                d["last_move"] = None
            d["state"] = cur

            try:
                d["my_color"] = "B" if self.color_var.get() == "黑" else "W"
            except Exception:
                pass
            try:
                k = komi_parse(self.komi_var.get())
                if k is not None:
                    d["komi"] = komi_snap(k)
            except Exception:
                pass
            try:
                raw = self.rules_var.get()
                key = {v: kk for kk, v in FA.RULES.items()}.get(raw)
                if not key:
                    key = FA.norm_rules(raw)
                if key in FA.RULES:
                    d["rules"] = key
            except Exception:
                pass
            try:
                self._control_think_values(d)
            except Exception:
                pass
            try:
                e = self._cur_engine()
                if e:
                    d["engine_key"] = str(e.get("key") or "")
                    d["engine_name"] = str(e.get("name") or "")
            except Exception:
                pass
            # 2026-11-08：AI 押分要读「我方视角」胜率/目差
            try:
                d["winrate"] = getattr(self, "_best_wr", None)
                d["score_lead"] = getattr(self, "_best_sl", None)
            except Exception:
                pass
            st.update(**d)
        except Exception:
            pass

    def _control_event(self, kind, p):
        """把 Auto 的 on_state/on_move/on_info/on_board 事件写进共享状态

        只在 Tk 线程被 _handle 调；纯字典更新，不碰控件。
        """
        st = getattr(self, "control_state", None)
        if st is None:
            return
        if kind == "state":
            name = p[0] if len(p) > 0 else ""
            msg = p[1] if len(p) > 1 else ""
            msg = "" if msg is None else str(msg)
            if name == "running":
                prev = st.get("state") or "idle"
                upd = {"state": "running", "running": True, "paused": False,
                       "last_message": msg}
                if prev in ("idle", "done", "error"):
                    # 这是新一局的第一拍（恢复暂停不会走到这里）→ 清上一盘痕迹
                    upd.update(game_over=False, move_no=0, last_move=None)
                st.update(**upd)
            elif name == "warmup":
                st.update(state="starting", running=True, last_message=msg)
            elif name == "paused":
                st.update(state="paused", paused=True, last_message=msg)
            elif name == "hidden":
                # 野狐最小化/关掉：引擎还在、程序没停（不是错误）
                st.update(last_message=msg)
            elif name == "done":
                prev = st.get("state") or "idle"
                stop_req = bool(getattr(self, "_stop_requested", False))
                upd = {"running": False, "paused": False,
                       "state": "error" if prev == "error" else "done",
                       "last_message": msg}
                if not stop_req and prev in ("running", "starting", "paused"):
                    upd["game_over"] = True
                st.update(**upd)
            elif name == "error":
                st.update(state="error", last_message=msg)
        elif kind == "board":
            if len(p) < 2:
                return
            upd = {}
            # 手数优先读标题里的「第N手」（不受提子影响；
            # 标题会滞后 0.3~1s，所以读不到时才退回数子）
            try:
                m = re.search(r"第\s*(\d+)\s*手", str(p[1] or ""))
                if m:
                    upd["move_no"] = int(m.group(1))
            except Exception:
                pass
            if "move_no" not in upd:
                try:
                    cnt = p[3] if len(p) > 3 else None
                    if cnt and len(cnt) == 2:
                        upd["move_no"] = int(cnt[0]) + int(cnt[1])
                except Exception:
                    pass
            try:
                last = p[4] if len(p) > 4 else None
                if last:
                    upd["last_move"] = str(last[0])
            except Exception:
                pass
            try:
                # 标题里带「对局结束/超时负/数子结果」→ 这盘真的完了
                if len(p) > 1 and FA._game_over(str(p[1] or "")):
                    upd["game_over"] = True
            except Exception:
                pass
            if upd:
                st.update(**upd)
        elif kind == "move":
            try:
                gtp = p[1] if len(p) > 1 else None
                if gtp:
                    st.update(last_move=str(gtp))
            except Exception:
                pass

    # ---------------------------------------------------------------- config

    def apply_control_config(self, values):
        """POST /api/config 的 Tk 侧实现（**一定在 Tk 线程执行**）

        返回真正落地的规范值（贴目 0.5 吸附、时间吸附、计算量夹紧都由这里做，
        和界面手工输入走同一套 komi_snap/time_snap/visits_snap）。
        运行中只允许 think*/visits —— 这里再兜一层，不依赖调用方。
        """
        if not isinstance(values, dict) or not values:
            raise ValueError("config 需要至少一个字段")
        if self.running:
            bad = [k for k in values if k not in CS.LIVE_CONFIG_KEYS]
            if bad:
                raise ValueError("引擎运行中不能改：" + "、".join(sorted(bad)))
        applied = {}

        if "my_color" in values:
            v = str(values["my_color"]).strip().upper()
            if v not in ("B", "W"):
                raise ValueError("my_color 只能是 B 或 W")
            self.color_var.set("黑" if v == "B" else "白")
            applied["my_color"] = v

        if "komi" in values:
            k = komi_snap(float(values["komi"]))
            self.komi_var.set(komi_text(k))
            self._komi_val = k
            applied["komi"] = k

        if "rules" in values:
            key = str(values["rules"]).strip().lower()
            label = FA.RULES.get(key)
            if not label:
                raise ValueError(f"不认识的规则：{key}")
            self.rules_var.set(label)
            applied["rules"] = key

        if "engine_key" in values:
            key = str(values["engine_key"]).strip()
            label = None
            for lab, e in (getattr(self, "eng_map", None) or {}).items():
                if isinstance(e, dict) and e.get("key") == key:
                    label = lab
                    break
            if label is None:
                raise ValueError(f"未知 engine_key：{key}")
            self.eng_var.set(label)
            try:
                self._on_eng()
            except Exception:
                pass
            applied["engine_key"] = key
            try:
                e = self._cur_engine() or {}
                applied["engine_name"] = str(e.get("name") or "")
            except Exception:
                pass

        if "think_mode" in values:
            mode = str(values["think_mode"]).strip().lower()
            if mode not in CS.THINK_MODES:
                raise ValueError(f"不认识的 think_mode：{mode}")
            self.gap_mode.set(mode)
        if "think" in values:
            v = time_snap(float(values["think"]), step=THINK_FIXED_STEP)
            self._set_time(self.gap_spin,
                           time_text(v, step=THINK_FIXED_STEP))
            applied["think"] = v
        if "think_min" in values:
            v = time_snap(float(values["think_min"]))
            self._set_time(self.gap_min_spin, time_text(v))
            applied["think_min"] = v
        if "think_max" in values:
            v = time_snap(float(values["think_max"]))
            self._set_time(self.gap_max_spin, time_text(v))
            applied["think_max"] = v
        if "visits" in values:
            n = visits_snap(values["visits"])
            self._set_visits(n)
            applied["visits"] = n

        # 模式切换 / 时间改动都要走一遍现有热更新链路：
        #   · 没在跑：只留值，start() 时读
        #   · 在跑：Auto.set_think() 下一手生效
        if any(k in values for k in CS.LIVE_CONFIG_KEYS):
            try:
                self._on_gap_mode()
            except Exception:
                pass
            # _on_think_change 可能因为上下限填反做过交换 —— 以界面上的值为准
            try:
                if "think" in applied:
                    applied["think"] = time_snap(
                        self.gap_var.get(), step=THINK_FIXED_STEP)
                if "think_min" in applied:
                    applied["think_min"] = time_snap(self.gap_min_var.get())
                if "think_max" in applied:
                    applied["think_max"] = time_snap(self.gap_max_var.get())
                if "visits" in applied:
                    applied["visits"] = visits_snap(self.visits_var.get())
            except Exception:
                pass

        try:
            self._sync_control()
        except Exception:
            pass
        return applied

    # ---------------------------------------------------------------- start

    def _control_check_params(self):
        """只读预检 start() 会读的那几个参数，非法就抛 ValueError

        目的：HTTP 调 /api/start 时**不要弹「参数不对」对话框**。
        解析函数和 start() 用的是同一套（_num），所以预检过了 start() 就不会再炸。
        """
        komi = self._num(self.komi_var, "贴目", float)
        if not (-150.0 <= komi <= 150.0):
            raise ValueError("贴目要在 -150 ~ +150 之间（引擎上限）")
        mode = self.gap_mode.get()
        if mode == "random":
            self._num(self.gap_min_var, "随机下限", float)
            self._num(self.gap_max_var, "随机上限", float)
        elif mode != "visits":
            self._num(self.gap_var, "思考时间", float)

    def control_start(self):
        """POST /api/start 的 Tk 侧实现 —— 复用 start(quick=True)，不复制逻辑

        返回 dict：{"ok":True,"started":True,"state":"starting"}
                   {"ok":False,"code":"no_engine|engine_unusable|no_fox|bad_config","error":"..."}

        ⚠️ 预检的顺序和 start() 完全一致（引擎 → 引擎可用性 → 野狐 → 参数），
        这样失败时能返回具体错误码，而不是让 HTTP 线程干等一个弹窗。
        """
        if self.running:
            return {"ok": True, "started": True, "state": "running"}
        try:
            e = self._cur_engine()
        except Exception:
            e = None
        if not e:
            return {"ok": False, "code": "no_engine",
                    "error": "没有选中引擎（先在界面/接口里选一个，或 /api/engines 看列表）"}
        try:
            ok, why = self._engine_ready(e)
        except Exception as ex:
            ok, why = False, f"引擎自检失败：{ex}"
        if not ok:
            return {"ok": False, "code": "engine_unusable",
                    "error": why or "这个引擎在本机不可用"}
        try:
            hits = FB.find_hwnd()
        except Exception:
            hits = []
        if not hits:
            return {"ok": False, "code": "no_fox",
                    "error": "没检测到野狐窗口（foxwq.exe）—— 先打开野狐并进入对局"}
        try:
            self._control_check_params()
        except ValueError as ex:
            return {"ok": False, "code": "bad_config", "error": str(ex)}
        self.start(quick=True)
        if not self.running:
            return {"ok": False, "code": "start_failed",
                    "error": "启动没有发生（引擎或参数检查未通过）"}
        return {"ok": True, "started": True, "state": "starting"}

    def _on_close(self):
        """点窗口右上角 × —— 先收工再退出

        ⚠️ 退出流程的顺序是踩出来的（2026-10-23）
        ----------------------------------------------------
        1) 存界面参数         ← 要 Tk 还活着
        2) 收起悬浮球 / 关日志 ← 要趁 Tk 还在
        3) 让 Auto 停、关引擎 ← 可能要几秒（关云端会话 / kill katago）
        4) destroy()          ← 必须最后

        老代码 3) 只是「发个停止信号」就立刻 4)，工作线程还在跑
        engine.close()（等子进程退出，最多十几秒）。窗口先没了，
        线程接着往队列里塞消息、往已销毁的控件上写日志 →
        TclError 刷屏写进崩溃日志；更糟的时候进程迟迟不退，
        老板以为卡死，再点一次图标就开了第二份（野狐被两份程序同时点）。

        现在：先隐藏窗口（视觉上立刻消失，感觉是「秒关」），
        给工作线程最多 6 秒收尾，再真正 destroy。
        6 秒没结束也强制退 —— 宁可留个僵尸进程让系统回收，
        也不能让用户对着一个关不掉的窗口干等。
        """
        if getattr(self, "_closing", False):
            return                  # 用户连点 ×，别重复走流程
        self._closing = True

        # ① 存设置
        try:
            self._save_ui_state()
        except Exception:
            pass

        # ② 收起悬浮球 + 关掉控制接口 + 关掉会话日志
        try:
            self._hide_ball()
        except Exception:
            pass
        # ⚠️ 2026-11-08：AI 线程也要先收（它可能在等模型/点按钮）
        try:
            if self.ai is not None:
                self.ai.shutdown()
        except Exception:
            pass
        try:
            self._ai_hide_ball()
        except Exception:
            pass
        try:
            srv = getattr(self, "control_server", None)
            if srv is not None:
                srv.stop()          # GUI 退出 → 控制接口立即失效（DESIGN 4.0）
        except Exception:
            pass
        try:
            if self._log_fp is not None:
                self._log_fp.write(
                    f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                    f"退出 ===\n")
                self._log_fp.close()
                self._log_fp = None
        except Exception:
            pass

        # ③ 立刻藏窗口 —— 给用户「已经关了」的即时反馈
        try:
            self.root.withdraw()
        except Exception:
            pass

        # ④ 通知收工（关窗也立即掐引擎，别让进程挂在后台）
        try:
            self._stop_requested = True
            if self.auto:
                self.auto.resume()
                self.auto.stop()
            self._hard_close_engine(delay=0.05)
        except Exception:
            pass

        # ⑤ 在后台等线程收尾，主线程靠 after 轮询；最多 6 秒
        self._close_deadline = time.time() + 6.0
        self.root.after(60, self._wait_close)

    def _wait_close(self):
        """等后台线程把引擎关干净，再 destroy（见 _on_close 的顺序说明）"""
        try:
            alive = bool(self.worker and self.worker.is_alive())
        except Exception:
            alive = False
        if alive and time.time() < getattr(self, "_close_deadline", 0):
            self.root.after(60, self._wait_close)
            return
        # 走到这里：线程已结束，或等超时了
        try:
            if self.engine is not None:
                self.engine.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------ 日志

    def log(self, msg, tag=None):
        """往日志框追加一行

        ⚠️ 必须容错（2026-10-23 查 bug 补）
        ----------------------------------------------------
        log() 是全程序最热的调用点（Auto 每读一次盘就写几行）。
        它一抛，调用方往往在 `except` 里——比如 _poll 的兜底、
        _handle 的单条捕获——于是「记日志失败」会反过来把
        真正的错误信息吃掉，甚至形成「出错→想记日志→再出错」的循环。

        最常见的两种失败都不是 bug：
          · 只读态（state="disabled"）下 insert → 窗口正在销毁
          · 窗口已 destroy → TclError: invalid command name
        两种都该安静吞掉，不能让日志系统拖垮对局。
        """
        try:
            self.txt.configure(state="normal")
            self.txt.insert("end", msg + "\n", tag or ())
            self.txt.see("end")
            self.txt.configure(state="disabled")
        except Exception:
            pass        # 窗口在销毁 / 控件已失效 —— 不值得让调用方崩
        # 2026-11-02：同一行再喂给「日志文件 + 悬浮球」。
        # 两件事都必须各自兜住异常 —— 记日志失败绝不能反过来打断对局。
        self._log_to_file(msg)
        self._log_to_ball(msg)

    def _log_to_file(self, msg):
        """追加到本次会话的日志文件（%LOCALAPPDATA%\\野狐自动对弈\\logs）"""
        fp = getattr(self, "_log_fp", None)
        if fp is None:
            return
        try:
            fp.write(f"[{datetime.datetime.now():%H:%M:%S}] {msg}\n")
            fp.flush()
        except Exception:
            pass

    def _log_to_ball(self, msg):
        """只留最近两行给悬浮球（老板要「两行左右」）"""
        try:
            s = str(msg).rstrip()
            if not s or set(s) <= {"=", " ", "-"}:
                return
            buf = getattr(self, "_ball_lines", None)
            if buf is None:
                return
            buf.append(s)
            del buf[:-2]
            ball = getattr(self, "ball", None)
            if ball is not None:
                ball.set_logs(buf)
        except Exception:
            pass

    def _clear_log(self):
        """清空日志（老板 2026-10-23 加的，换局时手边清爽）

        只清界面上的文字，不动 Auto 任何状态 —— 对局照跑。
        """
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _set_state(self, text, color):
        self.state_lbl.configure(text=text, foreground=color)
        self.dot.itemconfigure(self._dot_id, fill=color)

    # ------------------------------------------------ 盘面绘制

    # 盘面画布边长的**兜底值**（实例属性 self.BOARD_PX 会覆盖它）
    # ⚠️ 之所以还留一个类属性（2026-10-23）：万一某条代码路径在
    # __init__ 之前就用到（比如单测直接调 _draw_board），
    # 有默认值就不会 AttributeError。正常运行时永远走实例属性。
    BOARD_PX = 430

    def _draw_board(self, board, last=None, best=None):
        """画盘面。路数按 board 实际尺寸走（2~19 路）

        参数
        ----------------------------------------------------
        board : 二维 list（1=黑 2=白 0=空），None 表示还没读到
        last  : (x, y) 野狐标记的最后一手 → 画反色三角
        best  : (x, y) KataGo 当前计算的首选点 → 画金色十字圈

        视觉（2026-11-01 美化）
        ----------------------------------------------------
        · 木底 + 一圈深色外框线，像一块真的棋盘，而不是一块黄方块
        · 不画坐标（老板 2026-11-01 要求），边距全部让给棋盘
        · 棋子带一点右下阴影，不是贴纸；白子描边压低，不刺眼
        · 星位缩到 3px：远看是「盘」，近看才见格
        """
        cv = self.cv
        cv.delete("all")
        n = len(board) if board else 19
        w = len(board[0]) if board else n
        S = self.BOARD_PX
        pad = 18 if S >= 300 else 15      # 只留一圈呼吸边（坐标删了）
        size = S - pad * 2
        step = size / (n - 1) if n > 1 else size

        # 木底 + 外框（真棋盘都有一圈边线）
        cv.create_rectangle(0, 0, S, S, fill=WOOD, outline="")
        edge = pad - 8
        cv.create_rectangle(edge, edge, S - edge, S - edge,
                            outline="#8a6a2e", width=2)
        # 网格
        for i in range(n):
            p = pad + i * step
            cv.create_line(pad, p, pad + size, p, fill=GRID)
        for i in range(w):
            p = pad + i * step
            cv.create_line(p, pad, p, pad + size, fill=GRID)
        # ⚠️ 棋盘上的 A..T / 1..19 坐标已经删掉（老板 2026-11-01）：
        # 「坐标字母别显示在小棋盘面板上了，看着密密麻麻」。
        # 现在把省下的边距还给棋盘本身，格子更大、更干净。
        # 星位：按路数比例摆（19 路 3/9/15，9 路 2/6，13 路 3/9）
        star = {19: (3, 9, 15), 13: (3, 9), 9: (2, 6)}.get(n)
        if star:
            for cy in star:
                for cx in star:
                    if cx >= w:
                        continue
                    r = 3
                    px, py = pad + cx * step, pad + cy * step
                    cv.create_oval(px - r, py - r, px + r, py + r,
                                   fill="#5f4718", outline="")
        if not board:
            return
        r = step * 0.44
        for y in range(n):
            for x in range(w):
                v = board[y][x]
                if not v:
                    continue
                px, py = pad + x * step, pad + y * step
                # 阴影：统一往右下偏 1.2px，木色上才有"立体"感
                if v == 1:
                    cv.create_oval(px - r + 1.2, py - r + 2.0,
                                   px + r + 1.2, py + r + 2.0,
                                   fill="#a8813d", outline="")
                    cv.create_oval(px - r, py - r, px + r, py + r,
                                   fill="#14161a", outline="#000000")
                else:
                    cv.create_oval(px - r + 1.2, py - r + 2.0,
                                   px + r + 1.2, py + r + 2.0,
                                   fill="#ab8541", outline="")
                    cv.create_oval(px - r, py - r, px + r, py + r,
                                   fill="#f5f2ea", outline="#a49c90")
        if last:
            # 照野狐的样式在最后一手上画反色三角（白子上黑三角、黑子上白三角）
            lx, ly = last
            v = board[ly][lx] if 0 <= ly < n and 0 <= lx < w else 0
            if v:
                px, py = pad + lx * step, pad + ly * step
                q = r * 0.78
                cv.create_polygon(px, py, px + q, py, px, py + q,
                                  fill="#14161a" if v == 2 else "#f5f2ea",
                                  outline="")
            else:
                px, py = pad + lx * step, pad + ly * step
                cv.create_rectangle(px - r - 1, py - r - 1,
                                    px + r + 1, py + r + 1,
                                    outline=RED, width=2)
        if best:
            # ⚠️ KataGo 首选点（老板 2026-10-23：「把当前 katago 计算的
            # 首选点显示在左边的盘面上」）
            # 画法讲究：
            #   · 金色双圈 + 中间实心点 —— 不管底下是黑子白子还是空点，
            #     都一眼能看见（单圈在空木色上不明显，纯实心点会盖住棋子）
            #   · 半径比棋子略小，不遮住它要指的交叉点
            #   · 加外发光一样的粗淡圈，缩略图上一眼抓住
            bx, by = best
            if 0 <= by < n and 0 <= bx < w:
                px, py = pad + bx * step, pad + by * step
                rr = r * 1.02
                cv.create_oval(px - rr - 3, py - rr - 3,
                               px + rr + 3, py + rr + 3,
                               outline="#ff9d3c", width=2)
                cv.create_oval(px - rr, py - rr, px + rr, py + rr,
                               outline="#ffd479", width=3)
                cv.create_oval(px - 4, py - 4, px + 4, py + 4,
                               fill="#ffb74d", outline="#7a4a00")

    def _refresh_info(self, board=None, title="", turn="B", cnt=None,
                      extra=None, last=None, best=None, visits=None,
                      elapsed=None, wr=None, sl=None):
        """信息栏：只显示「计算量 / 胜率 / 目差」（老板 2026-10-23）

        ⚠️ 删掉的三项（老板点名）
        ----------------------------------------------------
        该谁走、末手、黑N/白N —— 这三样老板自己看盘面就知道：
        黑白子数得出来、野狐本来就在最后一手上画了三角、该谁走看谁子多。
        而「引擎算了多少、胜率多少、目差多少」是他**看不见**的，
        这才是需要摊在屏幕上的东西。

        现在四行：
          首选  D4            ← KataGo 首选点（跟盘面上的金圈对应）
          计算量 1234v / 8.0s  ← 访问量 + 耗时
          胜率  62.3%
          目差  +3.5
        """
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")

        has_data = bool(board) or bool(title)
        if not has_data:
            # ⚠️ 空状态必须压到 **2 行**（2026-10-23 小屏实测）
            # ----------------------------------------------------
            # 原来是三行：
            #     还没有盘面数据
            #     先打开野狐并进入对局，
            #     再点上面的「检查野狐」。
            # 小屏（信息栏 3 行高）时第三行正好被裁掉一半，
            # 截图里看到「再点上面的「检查野狐」」只露出上半截字 ——
            # 比不写还难看，而且像界面坏了。
            #
            # 现在合成两句：第一行说状态，第二行说怎么做。
            # 两行在 3 行档里能完整显示，在 4 行档里更宽松。
            self.info.insert("end", "还没有盘面数据\n", "v")
            self.info.insert("end", "打开野狐进对局，再点「检查野狐」", "k")
            self.info.configure(state="disabled")
            return

        # 第一行：首选点（大字，跟盘面金圈对应）
        self.info.insert("end", "首选  ", "k")
        # ⚠️ 传进来的是 (x, y) 元组时要换成 GTP 坐标（2026-11-01 美化时改）
        # 原来直接 f"{best}"，信息栏显示成「(8, 8)」——
        # 老板看棋报的是 H8，不是第 8 行第 8 列，两种坐标混着看很别扭。
        if isinstance(best, (tuple, list)) and len(best) == 2:
            try:
                best = FB.to_gtp(int(best[0]), int(best[1]),
                                 len(board) if board else 19)
            except Exception:
                pass
        self.info.insert("end", f"{best or '—'}\n", "big")

        # ⚠️ 三种排版，由 _fit_board 按「宽度够不够 + 高度够不够」选定
        # （2026-10-23 第六版。前面五版都在这里翻车，详见 _fit_board 的说明）
        # ----------------------------------------------------
        #   full    4 行 · 计算量带耗时          —— 最完整，大屏用
        #   short   4 行 · 计算量不带耗时        —— 窄一点的屏
        #   compact 3 行 · 计算量独占一行，
        #                 胜率与目差并到一行      —— 最省，小屏兜底
        #
        # ⚠️ 为什么 short 只砍耗时、不砍别的
        # ----------------------------------------------------
        # 「计算量 999999v/99.9s」实测 234px，「计算量 999999v」只要 174px
        # —— 省了 60px，正好是一整行字宽的差别，足够把 1366x768 的
        # 信息栏从「折行裁掉第三行」救回「四行完整」。
        # 代价很小：耗时本来就每手都变，漏看一秒不影响决策；
        # 而访问量（v）是判断引擎有没有真的在想的关键指标，必须留。
        #
        # ⚠️ 紧凑排版为什么是「胜率+目差」同行而不是「计算量+胜率」
        # ----------------------------------------------------
        # 前者最宽 242px，后者 218px —— 看着后者更窄，但：
        # 「计算量 999999v」本身就 174px，和胜率拼一行会到 218px+；
        # 而「胜率 100.0%　目差 +123.5」242px 是含最坏情况的，
        # 拆开后并没有更窄的选择。实测两句都放不进 224px 的列，
        # 真正的解法是 _fit_board 把左栏加宽（见那里的说明），
        # 这里的排版只负责"在给定宽度下别再自己撑爆"。
        _mode = getattr(self, "_info_mode", "full")

        if _mode == "compact":
            # 第二行：计算量（独占一行，不带耗时 —— 最宽也才 174px）
            self.info.insert("end", "计算量 ", "k")
            if visits:
                self.info.insert("end", f"{visits}v", "v")
            else:
                self.info.insert("end", "—", "k")
            self.info.insert("end", "\n")

            # 第三行：胜率 + 目差（都是局面评估，合并读得通）
            self.info.insert("end", "胜率 ", "k")
            if wr is None:
                self.info.insert("end", "—", "k")
            else:
                self.info.insert("end", f"{wr*100:.1f}%", "wr")
            self.info.insert("end", "　目差 ", "k")
            if sl is None:
                self.info.insert("end", "—", "k")
            else:
                self.info.insert("end", f"{sl:+.1f}", "lr")
            # ⚠️ 最后一行**不要**再插 \n（2026-10-23 实测）
            # ----------------------------------------------------
            # tk.Text 遇到以 "\n" 结尾的内容，会在末尾**多渲染一行空行**。
            # 实测第 5 行 y=141 h=28 全空，白占 28px ——
            # 在小屏上这 28px 正是「盘面能多大」的差距。
            # 而且 `_need_live` 填的测量文本不带末尾 \n，
            # 于是「量的」比「渲染的」少一行，两头对不上。
            self.info.configure(state="disabled")
            return

        # ---- 4 行排版（full / short 共用）----
        # 第二行：计算量 —— 访问量 + 耗时（short 模式省略耗时）
        self.info.insert("end", "计算量 ", "k")
        if visits:
            vtxt = f"{visits}v"
            if elapsed and _mode == "full":
                vtxt += f" / {elapsed:g}s"
            self.info.insert("end", vtxt, "v")
        else:
            self.info.insert("end", "—", "k")
        self.info.insert("end", "\n")

        # 第三行：胜率（我方视角，已由 Auto 翻好）
        self.info.insert("end", "胜率  ", "k")
        if wr is None:
            self.info.insert("end", "—", "k")
        else:
            self.info.insert("end", f"{wr*100:.1f}%", "wr")
        self.info.insert("end", "\n")

        # 第四行：目差（我方视角，正数=我领先）
        self.info.insert("end", "目差  ", "k")
        if sl is None:
            self.info.insert("end", "—", "k")
        else:
            self.info.insert("end", f"{sl:+.1f}", "lr")
        # ⚠️ 末行不插 \n —— 否则 Tk 多渲染一行空行，白占 28px
        # （实测见 compact 分支的说明）

        self.info.configure(state="disabled")

    # ------------------------------------------------ 消息泵

    def _poll(self):
        """每 100ms 从队列取消息刷界面 —— tkinter 只能主线程动 UI

        ⚠️ 这是整个界面的心跳，**绝不能因为一条坏消息就停摆**（2026-10-23）
        ----------------------------------------------------
        老写法：
            try:
                while True:
                    kind, p = self.q.get_nowait()
                    self._handle(kind, p)     # ← 这里一抛，整条链就断了
            except queue.Empty:
                pass
            self.root.after(100, self._poll)  # ← 永远执行不到

        `_handle` 里在解元组（`color, gtp, wr, sl = p`），只要后台哪天
        多发/少发一个字段，这里就 ValueError。后果不是「这一条没画」，
        而是**消息泵彻底死掉**：之后盘面、状态灯、日志全都不再更新，
        ESC（走队列进来）也失灵 —— 界面看着还在，其实是张死图。
        而窗口化启动时 stderr 是空的，用户连个报错都看不到。

        所以：逐条 try 住，坏消息记一行日志丢掉，心跳照跑。
        """
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            except Exception:
                continue
            try:
                self._handle(kind, payload)
            except Exception as e:
                # 单条消息出错不影响后续（比如某个字段对不上）
                try:
                    self.log(f"（界面处理 {kind} 消息出错："
                             f"{type(e).__name__}: {e}）", "dim")
                except Exception:
                    pass
        # 控制接口的共享状态每拍同步一次（运行标志 + 对局参数，给 /api/status 读）
        try:
            self._sync_control()
        except Exception:
            pass
        # ⚠️ 用 try/finally 兜底：即使上面意外抛出，也要重新排下一次
        try:
            self.root.after(100, self._poll)
        except Exception:
            pass        # 窗口已经销毁（正在退出）→ 不用再排

    def _handle(self, kind, p):
        # AI 托管的事件（2026-11-08）：消息/状态/任务/日志
        if isinstance(kind, str) and kind.startswith("ai_"):
            self._handle_ai_event(kind, p)
            return
        # 控制接口投来的动作（由 HTTP 线程入队、必须在这里的 Tk 线程执行）
        if kind == "ctl":
            self._handle_control_call(p)
            return
        # 把 Auto 的回调/盘面事件同步进共享状态（纯字典更新，不碰控件）
        try:
            self._control_event(kind, p)
        except Exception:
            pass
        if kind == "log":
            self.log(p)
        elif kind == "board":
            # on_board 现在带第 5 个参数 last（最后一手标记），
            # 老代码只解 4 个会炸 —— 按长度兼容两种。
            board, title, turn, cnt = p[0], p[1], p[2], p[3]
            last = p[4] if len(p) > 4 else None
            self._last_board = board
            self._last_title = title
            self._last_turn = turn
            self._last_cnt = cnt
            self._last_mark = last
            mark = FB.from_gtp(last[0], len(board)) if last else None
            self._last_mark_xy = mark
            self._refresh_board_and_info()
        elif kind == "move":
            color, gtp, wr, sl = p
            # ⚠️ 落子只记日志，**不再重画盘面**（2026-10-23 改）
            # 原因是现在盘面上要画「KataGo 首选点」金圈。如果这里用
            # 旧的 self._last_board 重画一次，会把金圈抹掉 —— 而下一个
            # board 事件要等 0.3~2s，中间这段时间金圈就闪没了。
            # 盘面刷新统一交给 board 事件（它带着最新盘面 + 末手 + 首选点）。
            mine = (color is None or str(color).upper() == self._my_color())
            if mine:
                # wr/sl 已经是**我方视角**（Auto._emit_info 翻好了），
                # 这里直接显示，不能再翻一次。
                w_txt = f"胜率 {wr*100:.1f}%" if wr is not None else "胜率 —"
                l_txt = f"目差 {sl:+.1f}" if sl is not None else "目差 —"
            else:
                w_txt, l_txt = "", ""
            self.log(f"  → {gtp}　{w_txt}　{l_txt}", "move")
        elif kind == "info":
            # on_info: (首选点, 计算量, 耗时, 胜率, 目差) —— 全是**我方视角**
            gtp, vis, el, wr, sl = p
            self._best_gtp = gtp
            self._best_vis = vis
            self._best_el = el
            self._best_wr = wr
            self._best_sl = sl
            self._refresh_board_and_info()
        elif kind == "state":
            st, msg = p
            if st == "running":
                if not self.paused:
                    self._set_state(msg or "运行中", GREEN)
                    # ⚠️ 引擎就绪、马上要落子了 → 本窗口收起来，只留悬浮球
                    # （老板 2026-11-02：开始对局后窗口最小化 + 悬浮球）
                    self._minimize_self()
                    self._sync_ball()
                    ball = getattr(self, "ball", None)
                    if ball is not None:
                        ball.set_state(
                            f"{msg or '运行中'} · 我执{self._my_color_text()}",
                            GREEN)
                        ball.set_running(True, False)
            elif st == "warmup":
                self._set_state(msg or "准备中", ACCENT)
                ball = getattr(self, "ball", None)
                if ball is not None:
                    ball.set_state(
                        f"{msg or '准备中'} · 我执{self._my_color_text()}",
                        ACCENT)
            elif st == "paused":
                self._set_state(msg or "已暂停", ACCENT)
                ball = getattr(self, "ball", None)
                if ball is not None:
                    ball.set_state("已暂停", ACCENT)
                    ball.set_running(True, True)
            elif st == "hidden":
                # ⚠️ 野狐被最小化/关掉 —— 不是错误（老板 2026-10-23 要求
                #    「不许程序霸着野狐」），所以用琥珀色「待命」而不是红色。
                # 程序没停，引擎还在，野狐一回来就自动接着下。
                self._set_state(msg or "野狐不可见·待命", ACCENT)
                ball = getattr(self, "ball", None)
                if ball is not None:
                    ball.set_state("野狐不可见", ACCENT)
            elif st == "done":
                self._set_state("待机", FG_DIM)
                self._finish()
            elif st == "error":
                self._set_state("出错", RED)
                self.log(f"出错：{msg}", "err")
                ball = getattr(self, "ball", None)
                if ball is not None:
                    ball.set_state("出错", RED)
        elif kind == "engine":
            pass
        elif kind == "fatal":
            # ⚠️ 启动失败且原因对用户有指导意义 → 必须弹窗（2026-10-23）
            # ----------------------------------------------------
            # 只写日志是不够的：日志区在窗口下半部，刚拿到软件的人
            # 根本不知道要看那里。他看到的只是「点了开启，红了一下，没了」。
            # 用 showwarning 而不是 showerror：这不是程序 bug，
            # 是「还缺个东西，装上就行」，语气上别吓人。
            try:
                messagebox.showwarning("引擎没能启动", p, parent=self.root)
            except Exception:
                pass

    def _my_color(self):
        return "B" if self.color_var.get() == "黑" else "W"

    def _my_color_text(self):
        """给悬浮球看的中文颜色（老板一眼能对出「这盘我执黑/白」）"""
        return "黑" if self._my_color() == "B" else "白"

    def _refresh_board_and_info(self):
        """盘面 + 信息栏的唯一刷入口：把三份数据合起来重画一次

        为什么要合并（2026-10-23）
        ----------------------------------------------------
        盘面内容现在来自三个独立事件：
          · board 事件 → 盘面棋子、末手标记
          · info  事件 → KataGo 首选点（金圈）、计算量、胜率、目差
          · move  事件 → 只写日志，不碰盘面
        如果每个事件各自重画，后到的会把先画的覆盖掉（首选点被抹、
        或棋子变旧）。统一成「先攒状态、再一次性画」，谁先谁后都对。
        """
        board = getattr(self, "_last_board", None)
        if not board:
            # 还没读到盘：画个空棋盘占位
            self._draw_board(None)
            self._refresh_info(None, "", "B", None)
            return

        n = len(board)
        best_xy = None
        gtp = getattr(self, "_best_gtp", None)
        if gtp:
            best_xy = FB.from_gtp(gtp, n)

        self._draw_board(board, getattr(self, "_last_mark_xy", None), best_xy)
        self._refresh_info(
            board, getattr(self, "_last_title", ""),
            getattr(self, "_last_turn", "B"),
            getattr(self, "_last_cnt", None),
            last=getattr(self, "_last_mark", None),
            best=gtp,
            visits=getattr(self, "_best_vis", None),
            elapsed=getattr(self, "_best_el", None),
            wr=getattr(self, "_best_wr", None),
            sl=getattr(self, "_best_sl", None))

    def _finish(self):
        # 幂等：error 和 done 可能连着来，重复关引擎会抛异常
        if not self.running:
            return
        self.running = False
        self.paused = False
        # 主键回到「开始」= 引擎已关（老板 2026-10-23：开始/结束即开关引擎）
        self.btn_go.configure(state="normal", text="▶  开启引擎")
        self.btn_pause.configure(state="disabled", text="⏸  暂停")
        self.eng_box.configure(state="readonly")
        # ⚠️ 引擎关了 → 对局信息重新可编辑（老板 2026-10-23 要求）
        # 和 start() 里的 _lock_game_info(True) 严格成对。
        # 原来这里只恢复 rules_box，我执和贴目会一直灰着 ——
        # 用户关掉引擎想改个黑白再开，发现改不了，只能重启程序。
        self._lock_game_info(False)
        self.btn_check.configure(state="normal")
        self.btn_calib.configure(state="normal")
        if self.engine is not None:
            try:
                self.engine.close()
            except Exception:
                pass
            self.engine = None
        self.auto = None
        self.log("— 已结束 —", "dim")
        # ⚠️ 收工：主窗口放回来，球停在「已停止」但**不消失**
        # （2026-11-02）—— 老板还想从球上点「开启引擎」再来一局，
        # 所以球留在原地；不想看了点球右上角的 ×，或者直接关主窗口。
        self._restore_self()
        self._sync_ball()
        ball = getattr(self, "ball", None)
        if ball is not None:
            ball.set_state("已停止", FG_DIM)

    # ------------------------------------------------ 动作

    def check_fox(self):
        try:
            hits = FB.find_hwnd()
        except Exception as e:
            self.log(f"检测失败：{e}", "err")
            return
        if not hits:
            self.log("没找到野狐 —— 先把客户端开起来（进程 foxwq.exe）", "err")
            self._set_state("野狐未开", RED)
            return
        # ⚠️ find_hwnd() 返回 7 元组 (hwnd, title, left, top, w, h, mini)
        #    （2026-10-23 修）老代码按 6 个解包 —— 多出来的 mini 直接
        #    抛 ValueError，点「检查野狐」必崩。这是真 bug，不是笔误。
        hwnd, title, _l, _t, w, h, mini = hits[0]
        pid = self._pid_of(hwnd)
        self.log(f"野狐已找到：{title[:36]}　{w}x{h}　pid={pid}", "ok")
        if mini:
            # 最小化了：告诉老板「我看见了，但我不去动它」
            self.log("　（当前处于最小化状态 —— 程序不会替你还原，"
                     "对局期间落子会等一下）", "dim")
            self._set_state("野狐已最小化", ACCENT)
        else:
            self._set_state("野狐已找到", GREEN)

    def _pid_of(self, hwnd):
        """拿窗口所属进程 pid（只读，纯诊断用）"""
        try:
            from ctypes import wintypes as wt
            import ctypes
            pid = wt.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                hwnd, ctypes.byref(pid))
            return int(pid.value)
        except Exception:
            return "?"

    def calib(self):
        try:
            im, title, org = FB.grab()
        except FB.WindowGone as e:
            # 最小化/关闭 → 不是故障，提示一句就完事，不还原窗口
            self.log(f"读不到野狐：{e}　"
                     f"（程序不会替你还原窗口，请自己恢复后重试）", "dim")
            return
        try:
            g = FB.calibrate(im)
            if not g.get("ok"):
                self.log(f"标定失败：{g.get('reason')}", "err")
                return
            FB.GEOM = g
            self.log(f"标定成功：x0={g['x0']:.1f} y0={g['y0']:.1f} "
                     f"step={g['step']:.3f}（{g['method']}，"
                     f"线={g['nlines']}）", "ok")
            b, conf, last = FB.read_board_full(im, geom=FB.GEOM)
            nb = sum(1 for r in b for v in r if v == 1)
            nw = sum(1 for r in b for v in r if v == 2)
            self._last_board = b
            self._last_title = title
            self._last_turn = FA.side_to_move(b, title, last)
            self._last_cnt = (nb, nw)
            self._last_mark = last
            self._last_mark_xy = (FB.from_gtp(last[0], len(b))
                                  if last else None)
            self._refresh_board_and_info()
            self.log(f"读盘：黑 {nb} 白 {nw}"
                     f"　{'依标记 ' + last[0] if last else '无标记'}", "ok")
        except Exception as e:
            self.log(f"标定异常：{type(e).__name__}: {e}", "err")

    def _num(self, var, name, conv=float):
        s = (var.get() or "").strip()
        try:
            return conv(s)
        except Exception:
            raise ValueError(f"{name}填的不是数字：{s!r}")

    # ------------------------------------------------ 开始 / 暂停 / 关闭

    def toggle_engine(self):
        """「开启引擎 ⇄ 关闭引擎」—— 一个键管开/关（老板 2026-10-23 要求）

        语义刻意做得和「关灯开关」一样：
          没开 → 造引擎、开跑（按钮变成「■ 关闭引擎」）
          开着 → 关引擎、收工（按钮变回「▶ 开启引擎」）

        ⚠️ 和「暂停」的区别（这是老板要分清的两件事）
        ----------------------------------------------------
          暂停：引擎还活着、权重还在显存/云端会话里，恢复零等待
          关闭引擎：引擎真的关掉，下次开启要重新握手 2~9s
        两个动作在主窗口和悬浮球上都各有一个按钮（2026-11-02 起不再用 ESC）。
        """
        if self.running:
            self.stop()
        else:
            self.start()

    def toggle_pause(self):
        """「暂停 ⇄ 继续」—— 主界面按钮和悬浮球上的按钮都走这里

        ⚠️ 「在未开启引擎时，暂停/继续键无效」（老板原话）
        ----------------------------------------------------
        所以 running=False 时直接返回，**什么也不做、也不弹窗**。
        按钮本身是 disabled 态，用户点不到。

        ⚠️ 2026-11-02：ESC 取消后，暂停**不再把主窗口弹出来**。
        老板要的是窗口一直最小化、只看悬浮球；暂停时球上的状态点
        变琥珀色、按钮变「继续」，这就够了。
        """
        if not self.running:
            return
        if self.paused:
            self.resume()
        else:
            self.pause()

    def pause(self):
        if not self.running:
            return
        if self.paused:
            return
        # ⚠️ 引擎还在握手（self.auto 还没造出来）时点暂停也要有反应
        # （引擎启动那 2~9 秒里按下去，旧代码直接 return，界面一动不动）
        if not self.auto:
            self.paused = True
            self._pause_pending = True
            try:
                self.btn_pause.configure(text="▶  继续")
            except Exception:
                pass
            self._set_state("已暂停", ACCENT)
            self._sync_ball()
            self.log("已暂停（引擎还在启动，起来后不会落子）", "dim")
            return
        self.paused = True
        self.auto.pause()
        self.btn_pause.configure(text="▶  继续")
        self._set_state("已暂停", ACCENT)
        self._sync_ball()
        self.log("已暂停（引擎仍在，点悬浮球上的「继续」立刻接着下）", "dim")

    def _raise_self(self):
        """把窗口提到最前并抢焦点（仅诊断/兜底用，2026-11-02 起暂停不再调用）"""
        try:
            r = self.root
            if r.state() == "iconic":
                r.deiconify()
            r.lift()
            r.attributes("-topmost", True)
            # 两段撤销：300ms 是正常节奏，1200ms 是保险
            r.after(300, lambda: self._unset_topmost())
            r.after(1200, lambda: self._unset_topmost())
            r.focus_force()
        except Exception:
            pass

    def _unset_topmost(self):
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass

    def resume(self):
        if not self.running:
            return
        if not self.auto:
            # 引擎还没起来也要能「继续」
            self.paused = False
            self._pause_pending = False
            try:
                self.btn_pause.configure(text="⏸  暂停")
            except Exception:
                pass
            self._set_state("启动中", ACCENT)
            self._sync_ball()
            self.log("继续（等引擎就绪）", "dim")
            return
        self.paused = False
        self.auto.resume()
        self.btn_pause.configure(text="⏸  暂停")
        self._set_state("运行中", GREEN)
        self._sync_ball()
        self.log("继续下棋", "ok")
        # 继续下棋 → 窗口再收起来，别挡着棋盘
        self.root.after(150, self._minimize_self)

    def _no_engine_help(self):
        """「这台电脑没装能用的引擎」的安装引导 —— 唯一出口

        ⚠️ 抽出来的原因（2026-10-23 裸机端到端测试抓到的真 bug）
        ----------------------------------------------------
        原来只在 `if not e:`（下拉框取不到条目）时才弹这个引导。
        但裸机上 `filter_engines` 有「保底至少留 1 项」的逻辑，
        而 Lizzie 兜底那条 `智子VIP共享` 的 `usable` 是 **None**
        （Lizzie 配置里写的，不代表本机真能用）—— 于是 `e` 拿得到，
        程序**以为有引擎**，一路走到「确认实下」对话框，
        用户点确认后引擎启动失败（因为压根没装智子客户端）。
        用户看到的是「我点了开始，它问我确认，我确认了，然后报错」，
        而不是老板要的「先提示去安装」。

        ✅ 判据改成三层，任何一层不满足都走引导：
          ① 拿得到条目
          ② `usable` 不是明确的 False
          ③ 引擎需要的可执行文件/凭据在本机真的存在
        """
        messagebox.showinfo(
            "还不能开始 · 请先安装引擎",
            "这台电脑上没找到能用的围棋引擎，所以现在还不能开始"
            "对弈。\n\n"
            "请先完成下面【任意一项】，然后点「引擎设置」重新扫描：\n\n"
            "【方式一 · 用云端算力】需要联网，吃对方的显卡\n"
            "  1. 安装「智子围棋」客户端\n"
            "     （zhizigo.com 下载，装的时候会自带 zz-ikatago.exe）\n"
            "  2. 打开客户端，注册并登录一次账号\n\n"
            "【方式二 · 用本机显卡】不需要联网，要求有独立显卡\n"
            "  1. 下载 KataGo（Windows 版）解压到任意目录\n"
            "  2. 下载一个权重文件（*.bin.gz）和它放一起\n"
            "  3. 点「引擎设置 → 手动添加…」，依次选 exe 和权重\n\n"
            "两种都可以装，也可以都装。",
            parent=self.root)

    def _engine_ready(self, e):
        """这个引擎在本机**现在**能起来吗？返回 (能, 说明)

        ⚠️ `usable` 为 None 的含义（2026-10-23）
        ----------------------------------------------------
        None = **不知道**（来自 Lizzie 配置的历史条目，没经过本机检测）。
        不能当成 True —— 这正是裸机上「智子VIP共享」漏过去的原因。
        也不能一律当成 False —— 老板机器上那条就是好的（他装了智子）。

        所以 None 时做一次**实际校验**：
          · 云端：zz-ikatago.exe 存在 且 有登录凭据
          · 本机：exe 存在 且 权重文件存在
        """
        if not isinstance(e, dict):
            return False, "没有选中引擎"
        u = e.get("usable")
        if u is False:
            return False, e.get("note") or "这个引擎在本机不可用"
        if u is True:
            return True, ""

        # u is None → 实测
        kind = e.get("kind") or e.get("src") or ""
        exe = e.get("exe") or ""
        model = e.get("model") or ""
        if str(kind) == "cloud" or e.get("key", "").startswith("cloud:"):
            try:
                import engine_detect as _ED
                det = getattr(self, "detect_res", None) or {}
                info = det.get("info") or {}
                # — 「有没有装智子」看 info.ikatago（detect 已经找过）
                # — 没有 detect 结果时才自己找一遍
                ik = info.get("ikatago")
                if not ik:
                    for p in _ED._ikatago_candidates():
                        if os.path.isfile(p):
                            ik = p
                            break
                if not ik:
                    return False, "没找到智子围棋客户端（zz-ikatago.exe）"
                # — 「有没有登录」看 info.ikatago_cred
                #   只有明确 False 才算没登录（None/缺字段不当成失败，
                #   否则 detect 没跑过时会误判老板机器不可用）
                if info.get("ikatago_cred") is False and det:
                    return False, "智子客户端还没登录"
                return True, ""
            except Exception as ex:
                return False, f"云端引擎校验失败：{ex}"
        # 本机引擎：exe 和权重都得在
        try:
            if exe and not os.path.isfile(exe):
                return False, f"引擎文件不在了：{exe}"
            if model and not os.path.isfile(model):
                return False, f"权重文件不在了：{model}"
            if not exe and not model:
                # 路径型条目两个都空 → 认不出来，按不可用处理
                return False, "这条引擎没有可执行文件"
            return True, ""
        except Exception as ex:
            return False, f"本机引擎校验失败：{ex}"

    def start(self, quick=False):
        """开引擎 / 开跑

        quick=True 只给悬浮球用：参数沿用上一次确认过的，不再弹
        「确认实下」；失败时把主窗口放回来，让老板能改设置。
        """
        if self.running:
            return
        e = self._cur_engine()
        if not e:
            # ⚠️ 下拉框**本身就是空的**（新电脑还没装引擎）不能只说
            #    「先在上面选一个引擎」—— 没有可选的，这话等于没说。
            #    老板 2026-10-23：「如果没有智子围棋和本机katago的话，
            #    就提示用户去安装之后再使用」。
            if quick:
                self._restore_self()
            try:
                empty = not list(self.eng_box.cget("values") or [])
            except Exception:
                empty = False
            if empty:
                self._no_engine_help()
            else:
                messagebox.showwarning("没选引擎",
                                       "先在上面选一个引擎。",
                                       parent=self.root)
            return

        # ⚠️ 选中了 ≠ 能用（2026-10-23 裸机测试抓到的 bug）
        # ----------------------------------------------------
        # `filter_engines` 保底会把 Lizzie 的历史条目放进来，
        # 那条的 `usable` 是 None（未经本机检测）。以前只看 `if not e`，
        # 于是裸机上照样往下走，弹出「确认实下」让人确认，
        # 确认后才因引擎起不来报错 —— 用户已经白等了一轮。
        # 现在在弹任何对话框之前就把这种情况挡掉，直接给安装引导。
        ok, why = self._engine_ready(e)
        if not ok:
            self.log(f"引擎不可用：{e.get('name')} —— {why}", "err")
            if quick:
                self._restore_self()
            self._no_engine_help()
            return

        try:
            komi = self._num(self.komi_var, "贴目", float)
            # 贴目范围：老板要 ±150。引擎实测 ±150 能设、±500 会被拒
            # （"unacceptable komi"），所以在界面上先挡住，别等引擎报错。
            if not (-150.0 <= komi <= 150.0):
                raise ValueError("贴目要在 -150 ~ +150 之间（引擎上限）。")
            # 贴目按 0.5 吸附（老板要求最小单位 0.5）——
            # 手输 7.3 会被拉到 7.5，跟 Spinbox 箭头的粒度一致。
            #
            # ⚠️ 吸附逻辑已抽到 komi_snap()，和 _commit_komi() 共用一份
            # （2026-10-23：两处各写一份 round(v*2)/2 迟早会分叉，
            #   而且 round() 是银行家舍入，13.5/2=6.75 这种边界两边不一致）
            komi = komi_snap(komi)
            # 顺手把界面上的值也规范掉：老板打 7.3 直接点开始，
            # 引擎用 7.5、界面还写着 7.3 的话，确认框和日志会互相矛盾
            if self.komi_var.get() != komi_text(komi):
                self.komi_var.set(komi_text(komi))
            self._komi_val = komi
            # ⚠️ 时间范围统一 0.5 ~ 180（老板 2026-10-27 要求）
            # ----------------------------------------------------
            # 原话：「最小单位 0.5，0.5 到 180 之间，固定随机都一样」。
            # 三个框都走 time_snap：吸附 0.5 + 夹进 [0.5, 180]。
            # 这里不再抛「不能是负数」—— 键入阶段 time_key_ok 已经
            # 挡住负号了，走到这一步还出现负数只可能是程序 bug，
            # 静默夹正比弹窗更合适（不打断已经点下去的开始动作）。
            visits = None
            if self.gap_mode.get() == "visits":
                # 按计算量：1~100000，非法/空一律按默认 10000（老板 2026-11-03）
                visits = visits_snap(self.visits_var.get())
                self._set_visits(visits)
                gap = gap_min = gap_max = None
            elif self.gap_mode.get() == "random":
                gap_min = self._num(self.gap_min_var, "随机下限", float)
                gap_max = self._num(self.gap_max_var, "随机上限", float)
                gap = None
                gap_min, gap_max = time_snap(gap_min), time_snap(gap_max)
                if gap_min > gap_max:
                    gap_min, gap_max = gap_max, gap_min   # 填反了自动换
                # 顺手把界面规范掉（界面上写着 999、实际按 180 跑会像 bug）
                self._set_time(self.gap_min_spin, time_text(gap_min))
                self._set_time(self.gap_max_spin, time_text(gap_max))
            else:
                gap = self._num(self.gap_var, "思考时间", float)
                gap_min = gap_max = None
                gap = time_snap(gap, step=THINK_FIXED_STEP)
                self._set_time(self.gap_spin,
                               time_text(gap, step=THINK_FIXED_STEP))
        except ValueError as ex:
            if quick:
                self._restore_self()
            messagebox.showerror("参数不对", str(ex))
            return

        hits = []
        try:
            hits = FB.find_hwnd()
        except Exception as ex:
            self.log(f"窗口检测异常：{ex}", "err")
        if not hits:
            if quick:
                self._restore_self()      # 主窗口收着弹框会看不见
            if not messagebox.askyesno(
                    "野狐没开",
                    "没检测到野狐窗口（foxwq.exe）。\n\n"
                    "实下必须先开野狐对局。\n要继续吗？"):
                return

        my = "B" if self.color_var.get() == "黑" else "W"
        # 规则（老板 2026-10-23 要求可调）
        rules = FA.norm_rules(
            {v: k for k, v in FA.RULES.items()}.get(
                self.rules_var.get(), self.rules_var.get())) or "chinese"
        mode = self.gap_mode.get()
        if mode == "visits":
            gap_txt = f"按计算量 {visits}v"
            engine_budget = 300.0     # 访问量到了就停；300s 只是防卡死保险
        elif mode == "random":
            t_lo, t_hi = min(gap_min, gap_max), max(gap_min, gap_max)
            gap_txt = f"随机 {t_lo:g}~{t_hi:g}s"
            # 引擎按**最大**思考时间预算 —— 每手在区间里挑一个，
            # 挑到大值那次必须真的算到那么久，不能按平均值截断。
            # ⚠️ 给个 0.5 的下限：区间是 0~10 时 t_hi 是 10 没问题，
            # 但若老板填 0~0（已被上面的逻辑改成 0~10）之外的极端值，
            # 传 0 下去会让后端 `budget or self.budget` 退回默认 8 秒。
            engine_budget = max(1.0, t_hi)
        else:
            gap_txt = f"固定 {gap:g}s"
            engine_budget = max(1.0, gap)
        rules_txt = FA.RULES.get(rules, rules)
        # 悬浮球上的「开启引擎」= 沿用上一盘确认过的参数，不再弹确认框
        if not quick and not messagebox.askyesno(
                "确认实下",
                f"引擎：{e['name']}\n"
                f"我执：{self.color_var.get()}\n"
                f"规则：{rules_txt}　贴目 {komi:g}\n"
                f"思考时间 {gap_txt}（想完立刻落子）\n"
                f"手数：不限（下到对局结束或手动停）\n\n"
                "开局后会先等 3 秒再下（给你关弹窗的时间）。\n"
                "实下会真的点鼠标落子。确认开始？"):
            return

        self.running = True
        self.paused = False
        self._pause_pending = False
        self._stop_requested = False
        # ⚠️ 2026-11-02：开跑前确保本窗口**不是置顶**
        # 置顶窗口会盖在野狐上面，点击全部落到它身上 —— 野狐收不到，
        # 表现就是「一手都不下」。老的暂停弹窗逻辑（_raise_self）已经
        # 随 ESC 一起撤掉，但这里仍兜一道底：万一有别的路径把它置顶了。
        self._unset_topmost()
        # ⚠️ ESC 热键已整体取消（2026-11-02 老板要求），这里不再注册。
        self._last_move = None
        self._best_gtp = None       # 换局时清掉上一盘的首选点
        self._best_vis = self._best_el = None
        self._best_wr = self._best_sl = None
        # 主键变成「结束」—— 它现在就是引擎开关（老板 2026-10-23）
        self.btn_go.configure(state="normal", text="■  关闭引擎")
        self.btn_pause.configure(state="normal", text="⏸  暂停")
        self.eng_box.configure(state="disabled")
        # ⚠️ 对局信息：引擎跑起来后改不了了，全灰掉（老板 2026-10-23 要求）
        # 详见 _lock_game_info 的说明。留在可编辑状态的只有思考时间
        # —— 那两项有热更新链路，改了下一手就生效。
        self._lock_game_info(True)
        self._set_state("启动中", ACCENT)
        self.log("=" * 58, "hl")
        self.log(f"引擎：{e['name']}", "hl")
        self.log(f"我执{'黑' if my == 'B' else '白'}　komi={komi:g}　"
                 f"规则={rules_txt}　"
                 f"思考时间={gap_txt}　手数不限", "hl")

        self.worker = threading.Thread(
            target=self._run,
            args=(e, my, komi, engine_budget, mode,
                  gap, gap_min, gap_max, rules, visits),
            daemon=True)
        self.worker.start()

        # ⚠️ 对局一开始就先亮球、收窗口（2026-11-02 老板要求）
        # 不等引擎握手完：那 2~9 秒正是最需要看到「启动中」的时候，
        # 悬浮球的两行日志会把引擎就绪/倒计时/思考过程实时滚出来。
        self._show_ball()
        self.root.after(150, self._minimize_self)

    def _run(self, spec, my, komi, budget, gap_mode, gap,
             gap_min, gap_max, rules=None, visits=None):
        """工作线程：造引擎 → 跑 Auto。所有 UI 更新都走队列。"""
        # ⚠️ 必须把 Auto 的日志定向到界面上（2026-10-23 老板踩到）
        # ----------------------------------------------------
        # fox_auto.log() 原本只 print()。桌面版用 pythonw.exe 启动，
        # 那是无控制台解释器，stdout 是空的 —— Auto 内部所有解释性日志
        # （等对手 / 读盘失败 / 没得到可下点 / 落子失败原因）全部丢失。
        # 结果就是引擎起来了、状态灯写「运行中」、然后永远不下子，
        # 界面上一个字都不说为什么。
        FA.LOG_SINK = lambda msg: self.q.put(("log", msg))
        try:
            self.q.put(("state", ("running", "启动引擎…")))
            # 访问量固定给个够大的值当上限：GTP 模式真正生效的是 max_secs，
            # 访问量只是防止无限算的保险。
            eng = FA.make_engine_from_spec(spec, visits=100000, budget=budget,
                                           komi=komi, rules=rules)
            self.engine = eng
            if getattr(self, "_stop_requested", False):
                # 握手这几秒里老板已经点了「关闭引擎」→ 不再开跑
                self.engine = None
                try:
                    eng.close()
                except Exception:
                    pass
                self.q.put(("state", ("done", "")))
                return
            self.q.put(("log", f"引擎就绪：{eng.info_text}　"
                               f"规则={FA.RULES.get(rules, rules)}　"))

            # gap 就是「思考时间」（秒）。max_moves=0 = 不限手数。
            auto = FA.Auto(eng, my_color=my, komi=komi,
                           think=gap or 0.0, max_moves=0,
                           think_mode=gap_mode,
                           think_min=gap_min or 0.0, think_max=gap_max or 0.0,
                           visits=visits or VISITS_DEFAULT,
                           warmup=3.0, rules=rules)
            self.auto = auto
            # 开局那几秒里点过暂停（悬浮球上的按钮）在这里补上，
            # 否则引擎起来会直接开下、把「暂停」吃掉。
            if getattr(self, "_pause_pending", False):
                auto.pause()

            # 把 Auto 的钩子接到队列上
            # on_board 带 5 个参数（第 5 个是最后一手标记），
            # 用 *a 转发，以后 Auto 再加参数这里不用跟着改。
            auto.on_board = lambda *a: self.q.put(("board", a))
            auto.on_move = lambda c, g, w, s: self.q.put(
                ("move", (c, g, w, s)))
            # ⚠️ 首选点 / 计算量 / 胜率 / 目差（老板 2026-10-23 要的）
            # 漏接这个的话：盘面上没有金圈，信息栏永远显示「—」。
            auto.on_info = lambda *a: self.q.put(("info", a))
            # ⚠️ 这个不能漏（2026-10-22 踩过）
            # Auto.run() 结束时发 on_state('done')，GUI 靠它恢复按钮。
            # 漏接的话：对局跑完界面还显示「运行中」，开始键永不恢复，
            # 只能关窗口重开 —— 端到端测出来的就是这个。
            auto.on_state = lambda st, msg: self.q.put(("state", (st, msg)))

            auto.run()
        except Exception as ex:
            msg = str(ex)
            if getattr(self, "_stop_requested", False):
                # 关闭引擎时把工作线程的管道掐了，这里报「异常」吓人 ——
                # 其实是老板主动停的，安静收工即可。
                self.q.put(("log", "已停止（引擎立即关闭）"))
                self.q.put(("state", ("done", "")))
                return
            # ⚠️ 启动失败要弹窗，不能只写日志（2026-10-23 为「发给别人用」）
            # ----------------------------------------------------
            # 新电脑上最常见的失败是「没装智子客户端 / 没装 katago」。
            # 老行为只往日志区打一行：
            #     启动失败：RuntimeError: 云引擎启动失败（进程已退出）
            # 日志区在窗口下半部，新用户根本不会想到去看；他看到的是
            # 「点了开启，状态灯红了一下，然后就没了」。
            #
            # 我们给这两种缺文件的情况写的提示是**故意写长的**
            # （含「去哪儿装」「有什么替代方案」），正好适合弹窗。
            # 其它预料外的异常仍然只进日志，但把类名留着便于排查。
            friendly = ("找不到" in msg or "没有找到" in msg
                        or "不存在" in msg or "需要先" in msg)
            if friendly:
                self.q.put(("log", msg))
                self.q.put(("state", ("error", msg.splitlines()[0])))
                self.q.put(("fatal", msg))
            else:
                self.q.put(("log", f"启动失败：{type(ex).__name__}: {msg}"))
                self.q.put(("log",
                            traceback.format_exc().strip().split("\n")[-3]))
                self.q.put(("state", ("error", f"{type(ex).__name__}: {msg}")))
            self.q.put(("state", ("done", "")))

    def stop(self):
        """结束 = 真的收工（暂停只是不再下子，不关引擎）

        老板 2026-10-23：「开始/结束就是代表着开/关引擎」
        → 这个动作就是「关引擎」。

        ⚠️ 2026-11-02 改：**立即关**，不再「等当前这一手走完」。
        老板原话：「点关闭引擎就立即关闭就行，不需要等下完这一手棋之后再关」。
        执行顺序：
          ① 置停止标志（落子复查/换点循环每 0.12 秒看一次，立刻收手）
          ② 后台线程 0.2 秒后掐掉引擎进程（不等分析跑完、不冻界面）
          ③ 按钮立刻回到「开启引擎」，界面状态写「关闭中」
        """
        self._stop_requested = True
        if self.auto:
            self.auto.resume()          # 暂停中也要能一句话关掉
            self.auto.stop()
        self.log("已停止（引擎立即关闭）", "dim")
        self._set_state("关闭中", ACCENT)
        ball = getattr(self, "ball", None)
        if ball is not None:
            try:
                ball.set_state("关闭中", FG_DIM)
            except Exception:
                pass
        self.btn_pause.configure(state="disabled", text="⏸  暂停")
        # 主键立刻恢复成「开始」：老板要的是「开关」，不该卡在「结束」上
        self.btn_go.configure(state="normal", text="▶  开启引擎")
        # 真正掐引擎 —— 放在后台线程，close() 最长等 3 秒也不卡界面
        self._hard_close_engine()

    def _hard_close_engine(self, delay=0.2):
        """立即关掉引擎进程（不等这一手算完）

        ⚠️ 为什么不在 UI 线程直接 close（2026-11-02）
        `zhizi_cloud.close()` 会写 quit 再等最多 3 秒才 kill，
        在界面线程跑就是「点了关闭，窗口先僵 3 秒」。放后台线程。
        """
        eng = self.engine
        if eng is None:
            return
        self.engine = None          # 先摘引用，_finish 不会重复 close

        def work():
            time.sleep(delay)       # 给工作线程一个退出落子循环的机会
            try:
                eng.close()
            except Exception:
                pass
            try:
                self.q.put(("state", ("done", "")))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()


def _close_splash():
    """关掉启动封面

    ⚠️ 这是什么、为什么必须显式调用（2026-10-23）
    ------------------------------------------------------------
    PyInstaller 的 --splash 会显示一张启动图，但它**不会自动消失** ——
    必须在代码里调 pyi_splash.close()，否则那张图会一直浮在桌面上，
    用户以为程序卡死了。这是 PyInstaller 文档里一句话带过、
    实际最容易漏的坑。

    只在打包态存在该模块；源码态 import 会失败，直接忽略。
    分两处调用：
      ① App 构建完（窗口已出来）→ 关掉
      ② 万一 ① 之前就崩了 → excepthook 里也关一次

    ⚠️ 为什么先查 `_PYI_SPLASH_IPC`（2026-10-23 实测）
    ----------------------------------------------------
    `import pyi_splash` 在没有 splash 的进程里**不是**干净地 ImportError，
    而是在模块顶层直接 KeyError 打到 stderr：

        The environment does not allow connecting to the splash screen.
        Did bootloader fail to initialize it?
        KeyError: '_PYI_SPLASH_IPC'

    （bootloader 没注入环境变量时的表现，`--selftest` / 输出重定向时都会遇到）
    虽然被下面那句 `except Exception` 兜住了，但 traceback 已经由
    模块自己的顶层代码打到了 stderr —— 打包版日志里就这么凭空多一段
    红字，看着像程序出错，其实是「本来就没有封面」。
    所以先看环境变量，没有就根本不去 import。
    """
    if not os.environ.get("_PYI_SPLASH_IPC"):
        return
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass


def _boot_log(msg):
    """启动阶段打点（只在设了 WB_BOOTLOG 环境变量时写）

    ⚠️ 为什么需要（2026-10-23 打包排查）
    ------------------------------------
    打包成 --windowed 后 stdout 全丢，进程活着但窗口不出来时
    完全无从下手（py-spy 也不支持 Python 3.14）。
    把 main() 拆成几个阶段各打一行，就能一眼看出卡在哪。
    平时不设环境变量，零开销。
    """
    if not os.environ.get("WB_BOOTLOG"):
        return
    try:
        import datetime
        p = os.path.join(RP.USER_DIR, "启动阶段.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _selftest():
    """打包后自检：把「一键就崩」的几条链路在进程内实跑一遍

    ⚠️ 为什么需要这个（2026-10-23 打包复查）
    ----------------------------------------------------
    漏 hiddenimports 这类问题**源码态完全看不出来** —— 源码目录里那些
    .py 都在，import 永远不会失败；只有打包成 exe 之后，模块没进 PYZ
    才会 ModuleNotFoundError。而 --windowed 又让 stderr 全丢，现场表现
    就是「点了按钮，程序没了」。

    从外部点按钮做不到（tkinter 按钮不是原生控件，发不了 WM_COMMAND；
    用鼠标坐标硬点又会被窗口大小/DPR 影响）。所以在**进程内**调同一段
    代码，跟老板点按钮走的是完全一样的路径。

    跑法：`野狐自动对弈.exe --selftest`
    结果写 USER_DIR/selftest.log，同时打 stdout（源码态能直接看）。

    ⚠️ 只 import、不真的启动引擎 —— 握手要 2~9 秒且依赖网络，
       那不属于「打包完整性」的范畴。
    """
    import datetime

    lines = []

    def out(s):
        """打印一行到控制台，**绝不因为编码问题抛异常**

        ⚠️ 这是被真实现场打回来的（2026-10-23）
        ----------------------------------------------------
        打包版自检跑完 30 项全 OK，最后一行 `print("✓ 自检全部通过")`
        在 GBK 控制台下直接 UnicodeEncodeError —— 自检本身崩了，
        exit=1，看起来像「打包有严重问题」。
        更糟的是崩在**写 selftest.log 之前**，于是我连日志都拿不到，
        只能盯着乱码 stdout 猜。

        一个「用来诊断的东西」自己因为编码崩掉，是最坏的设计。
        所以这里统一兜住：编码失败就降级成 ASCII，再不行就吞掉。
        （重定向到文件时 Python 用 locale 编码 = GBK；
          窗口态根本没有 stdout，写哪儿都无所谓。）
        """
        try:
            print(s)
        except UnicodeEncodeError:
            try:
                enc = (getattr(sys.stdout, "encoding", None) or "ascii")
                print(s.encode(enc, "replace").decode(enc, "replace"))
            except Exception:
                pass
        except Exception:
            pass

    def rec(ok, name, detail=""):
        lines.append(f"{'OK  ' if ok else 'FAIL'} {name}"
                     + (f"　{detail}" if detail else ""))
        out(lines[-1])
        return ok

    out("=== 野狐自动对弈 · 打包自检 ===")

    rec(True, "frozen", str(RP.is_frozen()))
    rec(True, "USER_DIR", RP.USER_DIR)
    rec(True, "HERE", RP.HERE)

    # ① 模块完整性：这些全是 gui 里的**函数内 import**，
    #    静态分析扫不到，是 hiddenimports 最容易漏的一批。
    mods = [
        ("setup_wizard", "点「设置」的入口"),
        ("engine_detect", "向导里的自动检测"),
        ("hotkey", "全局 ESC"),
        ("settings", "读写 settings.json"),
        ("engines", "引擎注册表"),
        ("runtime_paths", "路径"),
        ("hud", "悬浮球（2026-11-02 新增，函数内 import）"),
        ("fox_board", "读盘"),
        ("fox_auto", "主循环"),
        ("zhizi_cloud", "智子云"),
        ("katago_cloud", "云端服务"),
        ("local_engine", "本机 KataGo"),
        ("numpy", "图像数组"),
        ("mss", "截屏"),
        ("PIL", "图像"),
        ("PIL.ImageGrab", "抓图"),
        ("tkinter.ttk", "界面控件"),
        ("tkinter.messagebox", "弹窗"),
        ("tkinter.filedialog", "选文件"),
        ("http.server", "katago_cloud 服务"),
        ("socketserver", "同上"),
        ("ctypes.wintypes", "Win32"),
    ]
    bad = []
    for m, why in mods:
        try:
            __import__(m)
            rec(True, f"import {m}")
        except Exception as e:
            rec(False, f"import {m}（{why}）", f"{type(e).__name__}: {e}")
            bad.append(m)

    # ①b 悬浮球：类要在、关键方法要全（打包漏了 hud 时这里立刻红）
    try:
        import hud as _hud
        if not hasattr(_hud, "FloatBall"):
            raise AttributeError("hud 里没有 FloatBall")
        need = ("place_beside", "set_running", "set_logs",
                "set_state", "show", "hide")
        miss = [n for n in need if not hasattr(_hud.FloatBall, n)]
        rec(not miss, "hud.FloatBall",
            ("缺失：" + "、".join(miss)) if miss else "方法齐全")
        if miss:
            bad.append("hud")
    except Exception as e:
        rec(False, "import hud（悬浮球）", f"{type(e).__name__}: {e}")
        bad.append("hud")

    # ② 引擎注册表能出结果（走 settings → engines → runtime_paths 整条链）
    try:
        reg = REG.registry()
        ok = bool(reg)
        rec(ok, "engines.registry()", f"{len(reg)} 项")
        if not ok:
            bad.append("registry()")
    except Exception as e:
        rec(False, "engines.registry()", f"{type(e).__name__}: {e}")
        bad.append("registry()")

    # ③ 读数路径：本机能找到几个权重（engine_detect 内部起子进程读 token）
    #    ⚠️ 用 check_token=False：自检的目的是「模块在不在」，
    #       不是「智子登录有没有过期」。读 token 要起子进程、最慢 12 秒，
    #       而且断网时必然失败 —— 那会把打包自检搞成网络自检。
    try:
        import engine_detect as ED
        d = ED.detect(check_token=False)
        rec(True, "engine_detect.detect()",
            f"智子云 {len(d.get('clouds') or [])} / "
            f"本机 {len(d.get('locals') or [])} / "
            f"Lizzie {len(d.get('lizzie') or [])}")
    except Exception as e:
        rec(False, "engine_detect.detect()", f"{type(e).__name__}: {e}")
        bad.append("engine_detect")

    # ④ 向导类能造出来（最容易漏 setup_wizard 的地方）
    try:
        import setup_wizard as SW
        rec(hasattr(SW, "ask"), "setup_wizard.ask 存在")
    except Exception as e:
        rec(False, "setup_wizard.ask", f"{type(e).__name__}: {e}")
        bad.append("setup_wizard")

    # ⑤ 全局 ESC 能建对象（不启动，只 import + 构造成员）
    try:
        import hotkey as HK
        hk = HK.GlobalHotkey(vk=HK.VK_ESCAPE, on_fire=lambda: None)
        rec(hasattr(hk, "start") and hasattr(hk, "stop"),
            "GlobalHotkey 可构造")
        del hk
    except Exception as e:
        rec(False, "GlobalHotkey", f"{type(e).__name__}: {e}")
        bad.append("hotkey")

    # ⑥ 数据文件（spec 的 datas）：图标 + 本地引擎配置
    for fn, sub in (("app.ico", "."),
                    ("engine/analysis_fast.cfg", "engine")):
        p = os.path.join(RP.HERE, sub, os.path.basename(fn)) \
            if sub != "." else os.path.join(RP.HERE, fn)
        rec(os.path.isfile(p), f"数据文件 {fn}", p)

    # ⑦ 先把结论写盘，再打印
    #    ⚠️ 顺序很重要（2026-10-23 现场）：上一版是「先 print 结论，再写日志」，
    #    结果最后那行 print 编码崩了 → 进程带着 traceback 退出 → 日志没写成。
    #    诊断文件必须先落盘，因为「打印」是最不可靠的一环。
    tail = (f"✗ 自检失败，{len(bad)} 项：{', '.join(bad)}" if bad
            else "✓ 自检全部通过（打包完整性没问题）")
    logp = os.path.join(RP.USER_DIR, "selftest.log")
    wrote = None
    try:
        with open(logp, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] "
                    f"frozen={RP.is_frozen()}　HERE={RP.HERE}\n")
            f.write("\n".join(lines) + "\n\n" + tail + "\n")
        wrote = logp
    except Exception as e:
        wrote = f"(写入失败：{e})"

    out("")
    out(tail)
    out(f"（结果已写入 {wrote}）")

    return 0 if not bad else 1


def main():
    _boot_log(f"main() 进入 argv={sys.argv} frozen={RP.is_frozen()}")
    _boot_log(f"HERE={RP.HERE} USER_DIR={RP.USER_DIR}")    # ⚠️ `--serve` 是**子服务模式**（2026-10-23 为打包加的）
    # ------------------------------------------------------------------
    # 打包成 exe 后没有 python.exe 可用来拉起 katago_cloud.py，
    # 所以让 exe 自己认一个参数：`野狐自动对弈.exe --serve --port 8765 …`
    # 就走到 katago_cloud 的服务入口，不弹窗、只监听 8765。
    # 源码态也能用（等价于 python gui.py --serve），方便对照测试。
    if "--serve" in sys.argv:
        i = sys.argv.index("--serve")
        # 把 --serve 自己摘掉，剩下的参数原样交给 katago_cloud.main()
        sys.argv = sys.argv[:i] + sys.argv[i + 1:]
        # ⚠️ 必须关掉启动封面（2026-10-23 查 bug）
        # ----------------------------------------------------
        # splash 是**进程级**的：只要 exe 带 splash 启动，PyInstaller 的
        # bootloader 就会先画一张图，等代码里调 pyi_splash.close()。
        # --serve 是**后台常驻**的子进程（fox_auto 起引擎时拉起来的），
        # 它从来不建 Tk、也走不到 mainloop 那句 after(150, _close_splash)。
        # 结果：每开一次引擎，桌面上就多一张关不掉的启动图 ——
        # 而且是"新开的那张"，用户根本不会联想到是这个进程。
        # 这里第一件事就把它撤掉。
        _close_splash()
        import katago_cloud
        katago_cloud.main()
        return 0

    # ⚠️ `--selftest`：打包完整性自检（2026-10-23 加）
    # 见 _selftest() 的 docstring —— 漏 hiddenimports 只有打包态才暴露，
    # 而 --windowed 又看不到报错，所以留一个可复现的自检入口。
    if "--selftest" in sys.argv:
        _close_splash()          # 同理：自检不建 Tk，得自己撤封面
        return _selftest()

    _boot_log("准备创建 Tk()")
    root = tk.Tk()
    _boot_log("Tk() 完成")
    # ⚠️ DPI 感知已提到文件最顶端（_init_dpi），这里不再设
    # ----------------------------------------------------
    # 原来这里是 `windll.shcore.SetProcessDpiAwareness(1)`，写在 Tk() 之后。
    # 实测它是失败的（返回 0x80070005）—— awareness 只能设一次，
    # fox_board 的顶层代码早就设成 (2) 了。见文件开头 _init_dpi 的说明。
    _boot_log("开始 App(root)")
    app = App(root)
    # ⚠️ 控制接口（2026-11-05）：窗口建好之后才真正监听 127.0.0.1:8767。
    # 失败（端口被占 / token 写不了）只影响接口，人工功能照常。
    try:
        app.start_control_server()
    except Exception as e:
        _boot_log(f"控制接口启动失败：{type(e).__name__}: {e}")
    _boot_log("App 构建完成，进 mainloop")
    # ⚠️ 窗口已经搭好 → 立刻撤掉启动封面（否则它会一直浮在最上层）
    # 用 after(150) 而不是立即关：让 Tk 先把窗口真正 map 出来，
    # 避免出现「封面关了、主窗还没画出来」的空白瞬间。
    root.after(150, _close_splash)
    root.mainloop()
    _boot_log("mainloop 退出")
    return 0


def _excepthook(exc_type, exc, tb):
    """崩溃时把 traceback 写进文件 + 弹个窗

    ⚠️ 为什么必须要（2026-10-22 老板踩到）
    ------------------------------------
    桌面快捷方式用 pythonw.exe 启动，它是**无控制台**的 GUI 解释器：
    任何未捕获异常都不会显示，进程直接消失 —— 老板看到的就是
    「双击闪退，没反应」，连报什么错都不知道。

    有了这个钩子：崩了会在同目录写 GUI-崩溃日志.txt，并弹窗告知。
    """
    import traceback, datetime, os
    txt = "".join(traceback.format_exception(exc_type, exc, tb))
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = f"[{stamp}] 程序异常退出\n\n{txt}\n"
    # ⚠️ 必须写 RP.USER_DIR，不能写 __file__ 旁边 —— 打包后那是 _MEIPASS
    #    临时目录，程序一退日志就跟着没了，等于白写。
    logpath = os.path.join(RP.USER_DIR, "GUI-崩溃日志.txt")
    try:
        with open(logpath, "a", encoding="utf-8") as f:
            f.write(body)
    except Exception:
        logpath = "(日志写入失败)"
    # 崩了也要把启动封面撤掉，否则封面上会叠一个错误弹窗
    _close_splash()
    try:
        from tkinter import messagebox
        messagebox.showerror(
            "野狐自动对弈 · 出错",
            f"{exc_type.__name__}: {exc}\n\n"
            f"详细信息已写入：\n{logpath}")
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc, tb)


if __name__ == "__main__":
    _boot_log("=== 入口 __main__ ===")
    sys.excepthook = _excepthook
    try:
        sys.exit(main())
    except Exception:
        _boot_log("!! 顶层异常，走 excepthook")
        _excepthook(*sys.exc_info())
        sys.exit(1)

