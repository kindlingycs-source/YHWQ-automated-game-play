# -*- coding: utf-8 -*-
"""
野狐自动对弈 · 一体化入口
============================================================
一条命令打通全链路：识盘 → 分析 → 落子 → 复查。

    python fox_auto.py                    # 默认：智子VIP云FDX(B11) + 自动读盘落子
    python fox_auto.py --engine local     # 换本机引擎
    python fox_auto.py --color B          # 我执黑
    python fox_auto.py --moves 20         # 只下 20 手（不给 = 不限，下到终局）

为什么默认走云端
------------------------------------------------------------
老板 2026-10-22 的判定，也是这行的常识：
    围棋 AI 不是「算得越快越强」，是「权重越强越强」。
    同访问量下 B11(tf3) 比 B28 高约 490 Elo（kata1 官方 rating 14594 vs 14103）。
    速度只值 150~250 Elo / 每次翻倍 —— 拿 10 倍速度换 490 Elo 是亏的。
所以：默认 `--engine cloud`，跑的就是智子 VIP FDX 那台 B11。
本机只作为「云断了」的兜底，不因为快就当主力。

引擎抽象
------------------------------------------------------------
两个后端接口一致：analyze(board, turn, komi, visits, budget) → 统一结构
    cloud : 已有 katago_cloud.py 常驻服务（8765），HTTP 调用
    local : local_engine.LocalEngine，进程内直连
两者返回的 winrate 视角必须都是**白方视角**：cloud 侧已在 _recolor 里翻好，
local 侧原本是 side_to_move，这里现翻。混着用不会翻错面。
"""
import os, re, sys, json, time, argparse, random, traceback, math
import threading
import urllib.request, urllib.error
from datetime import datetime

import numpy as np

import runtime_paths as RP

# ⚠️ HERE 是**只读资源目录**（打包后 = _MEIPASS 临时目录）
#    写文件（logs/、崩溃日志）必须用 RP.USER_DIR，否则 exe 一关就没了。
HERE = RP.HERE
USER_DIR = RP.USER_DIR
sys.path.insert(0, HERE)
import fox_board as FB

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CLOUD = "http://127.0.0.1:8765"

# ------------------------------------------------------------------ 日志
# ⚠️ 为什么要有 LOG_SINK（2026-10-23 老板踩到「云引擎加载了之后就不下棋」）
# ------------------------------------------------------------------
# 这里原本只有 `print()`。CLI 下没问题，但桌面版是用 pythonw.exe 启动的
# —— 那是**无控制台**的解释器，sys.stdout 是 None 或空设备。
# 于是 Auto 内部所有解释性日志（等对手 / 读盘失败 / 没得到可下点 /
# 连续落子失败 / 预热倒数）在 GUI 里**一个字都不显示**。
# 老板看到的就是：引擎后面板写「运行中」，然后界面一片安静，永不下子，
# 也永远不知道卡在哪儿。
#
# GUI 现在把 LOG_SINK 指到自己的队列上，Auto 说什么都能进日志区。
# CLI 下 LOG_SINK 是 None，照旧打屏。
LOG_SINK = None


def log(*a):
    msg = " ".join(str(x) for x in a)
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    if LOG_SINK is not None:
        try:
            LOG_SINK(msg)
            return
        except Exception:
            pass          # 界面挂了不能连带把对局搞崩，退回打屏
    try:
        print(line, flush=True)
    except Exception:
        pass              # pythonw 下 stdout 为 None，print 会抛，静默即可


# ============================================================ 规则 / 激进度
#
# 老板 2026-10-23 的要求：
#   「做成可调整规则的（中国数子规则，日韩数目规则）以及自定义 katago
#     激进度（±3）自定义贴目（±150 目）」
#
# 三件事分别对应：
#   规则 → `kata-set-rules <name>`，引擎原生支持 9 种名字（已实测）
#   贴目 → `komi <x>`，实测 ±150 都能设，±500 被引擎拒（unacceptable komi）
#   激进度 → **KataGo 没有单一参数**，只能映射到一组搜索参数（见下表）

# 规则：界面文案 ←→ GTP 名字
# 只列老板要的三种。aga/tromp-taylor/stone-scoring 等引擎也认，
# CLI 里直接给英文名就能用，界面上不铺开（多余的选项只会让人选错）。
RULES = {
    "chinese": "中国规则·数子",
    "japanese": "日本规则·数目",
    "korean": "韩国规则·数目",
}
RULES_ORDER = ("chinese", "japanese", "korean")
_RULES_ALIAS = {
    "中国": "chinese", "数子": "chinese", "中国规则": "chinese",
    "cn": "chinese", "cn规则": "chinese",
    "日本": "japanese", "数目": "japanese", "日本规则": "japanese",
    "jp": "japanese", "japanese规则": "japanese",
    "韩国": "korean", "韩": "korean", "韩国规则": "korean", "kr": "korean",
}


def _to_float(v, default):
    """宽松转 float —— 界面输入框里可能是 ''、'5'、5、' ８ ' 之类

    转不出来就返回 default（而不是抛异常）：思考时间这种参数
    填错一个字符就弹窗拒绝对局，体验太差。宁可退回原值。
    """
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace("，", ".").replace("。", ".")
        if not s:
            return default
        # 全角数字顺手转掉（老板用中文输入法时容易打出来）
        s = s.translate(str.maketrans("０１２３４５６７８９．",
                                      "0123456789."))
        return float(s)
    except Exception:
        return default


def norm_rules(x):
    """把界面/CLI 给的规则名规整成 `kata-set-rules` 认识的名字

    认不出来就原样返回 —— 让引擎去拒，比在这里瞎猜一个规则强。
    （猜错了不会报错，只会悄悄按另一套规则数子，界面上看不出来）
    """
    if not x:
        return None
    s = str(x).strip()
    low = s.lower()
    if low in RULES:
        return low
    if low in _RULES_ALIAS:
        return _RULES_ALIAS[low]
    if s in _RULES_ALIAS:
        return _RULES_ALIAS[s]
    return low


# 激进度 -3（保守）~ +3（激进）→ 一组 KataGo 搜索参数
#
# ⚠️ 为什么不是一个参数
# ------------------------------------------------------------
# KataGo 没有 `aggression` 这种东西。业界（Lizzie / KaTrain 的「风格」滑条）
# 的做法都是调同一组旋钮，各自的作用：
#
#   rootPolicyTemperature     根节点 policy 温度。>1 让 policy 分布变平
#                             → 冷门点也有机会被搜到（敢想）
#                             <1 变尖 → 只在主流着法里挑（稳）
#   rootPolicyTemperatureEarly 开局的温度。开局本来就没有「稳」可言，
#                             这里给得比中盘更接近 1，免得开局就乱下
#   wideRootNoise             根节点宽搜索噪声。0 = 完全不信 policy 之外的点；
#                             越大越愿意去「看看到底会怎样」
#   rootDirichletNoiseWeight  根节点 Dirichlet 噪声权重。
#                             这是自对弈用来防过拟合的，分析时用很小的值，
#                             激进档才抬起来一点，作用是**打散首选点**
#   chosenMoveTemperature     最终选点温度。0 = 永远选访问最多的那个；
#                             >0 = 在访问量接近的候选里随机挑
#   cpuctExploration          UCT 探索系数。大 → 更愿意去访问次数少的点
#   subtreeValueBiasFactor    子树价值偏差补偿。高 → 对「暂时看着亏」的变化
#                             更宽容（愿意下先亏后赚的战斗）
#
# 档位取 0 时**必须逐项等于引擎默认值**（见 _STYLE_DEFAULT），
# 否则「激进度 = 0」这个「不改」的语义就废了 —— 老板会以为默认档被改坏了。
_AGGRO = {
    # level: (rootPolT, rootPolT Early, wideNoise, dirichletW,
    #         chosenMoveT, cpuctExpl, subtreeBias)
    -3: (0.55, 0.70, 0.00, 0.00, 0.00, 0.85, 0.30),
    -2: (0.70, 0.80, 0.01, 0.05, 0.00, 0.95, 0.35),
    -1: (0.85, 0.90, 0.02, 0.14, 0.00, 1.03, 0.40),
     0: (1.00, 1.00, 0.04, 0.25, 0.00, 1.10, 0.45),   # ← 引擎默认
    +1: (1.25, 1.15, 0.08, 0.32, 0.05, 1.22, 0.55),
    +2: (1.60, 1.35, 0.15, 0.42, 0.10, 1.40, 0.70),
    +3: (2.10, 1.60, 0.25, 0.55, 0.18, 1.65, 0.90),
}

_AGGRO_KEYS = ("rootPolicyTemperature", "rootPolicyTemperatureEarly",
               "wideRootNoise", "rootDirichletNoiseWeight",
               "chosenMoveTemperature", "cpuctExploration",
               "subtreeValueBiasFactor")

# 引擎默认值（kata-list-params 实测）—— 只用于「激进度 0 时是否要下发」的判断
_STYLE_DEFAULT = dict(zip(_AGGRO_KEYS, _AGGRO[0]))

# 界面上的档位文案（滑块旁边显示，让老板一眼知道当前是什么风格）
AGGRO_LABEL = {
    -3: "极保守（只走最稳的）",
    -2: "保守",
    -1: "略保守",
     0: "标准（引擎默认）",
    +1: "略激进",
    +2: "激进（偏爱战斗）",
    +3: "极激进（越乱越好）",
}

# 档位步长（老板 2026-10-23 要求：「带小数的最小单位 0.1」）
# 6 个区间，每档 10 步 → 一共 61 个可选值（-3.0, -2.9, … 0.0 … +3.0）
AGGRO_STEP = 0.1
AGGRO_MIN = -3.0
AGGRO_MAX = 3.0


def _lerp_table(table, lv):
    """在「整数锚点」的表上按小数 lv 线性插值

    为什么不在表里直接写满 61 个值
    ----------------------------------------------------
    61 个手写值没法维护，也没法解释「为什么 +2.3 是这个数」。
    保留 7 个有棋理含义的锚点（-3/-2/…/+3），中间线性过渡，
    这样任何一个小数值都能追溯到「它在哪两档之间、靠谁更多」。
    """
    ks = sorted(table)
    v = max(ks[0], min(ks[-1], lv))
    lo = max(k for k in ks if k <= v)
    hi = min(k for k in ks if k >= v)
    if lo == hi:
        return table[lo]
    t = (v - lo) / float(hi - lo)
    a, b = table[lo], table[hi]
    if isinstance(a, tuple):
        return tuple(x + (y - x) * t for x, y in zip(a, b))
    return a + (b - a) * t


def clamp_aggro(level):
    """夹到 -3.0 ~ +3.0 并按 0.1 吸附

    ⚠️ 返回的是 float，不再是 int（2026-10-23 老板要求 0.1 单位）
    所有调用方都不能再假设它是整数：
      · 格式化用 {x:+.1f}
      · 判「是否标准档」用 abs(x) < 1e-9
      · 取文案用 aggro_label(x)
    """
    try:
        v = float(level)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = max(AGGRO_MIN, min(AGGRO_MAX, v))
    v = round(v / AGGRO_STEP) * AGGRO_STEP
    v = round(v, 1)
    return 0.0 if v == 0 else v        # 消掉 -0.0


def aggro_label(level):
    """档位文案：按**最近整数锚点**取名字

    小数没有独立文案（61 条文案没人看得完），就近归到某一档。
    +2.3 显示「激进（偏爱战斗）」，+2.6 也显示它 —— 差 0.3 档说不出区别，
    但数值本身在界面上照实显示，老板看得出来自己在哪。
    """
    k = int(round(clamp_aggro(level)))
    return AGGRO_LABEL[max(-3, min(3, k))]


def style_params(level):
    """激进度 → {参数名: 值}

    返回的 dict 只含**与默认不同**的项：默认档返回空 dict，
    调用方据此跳过全部 GTP 往返（0 档不发命令，也就不会打乱引擎状态）。
    """
    lv = clamp_aggro(level)
    vals = dict(zip(_AGGRO_KEYS, _lerp_table(_AGGRO, lv)))
    return {k: v for k, v in vals.items() if abs(v - _STYLE_DEFAULT[k]) > 1e-9}


# --------------------------------------------------------------- 选点层
#
# ⚠️ 为什么光调引擎参数不够（2026-10-23 实测两轮）
# ------------------------------------------------------------
# 实测证明 kata-set-param **确实生效**：
#   · rootPolicyTemperature 0.25 vs 5.0 → 根节点 prior 分布天差地别
#     （实测 prior 从 0.64/0.33/0.03… 变成 0.0255/0.0263/0.0224…）
#   · 候选访问量熵随档位**单调**上升：-3 → 0.355，+3 → 0.673
#
# 但 **top-1 首选点纹丝不动**，反复都是同一个必然要点。
# 原因：`kata-analyze` 只报告候选，取舍发生在「谁 visits 最多」；
# 而 visits 最多的点在任何温度下都是同一个（盘面上的天王山）。
# 参数只能让候选池更丰富，翻不动首选。
#
# `chosenMoveTemperature` 在 analysis 路径上更是不起作用 ——
# 实测设成 0.0 / 0.3 / 1.0，6 次首选全是同一个点。
# 那个参数是给 `genmove` 用的，我们走的是分析接口。
#
# 结论：要做「棋风」必须在**选点层**动手。
#
# 怎么挑才不损棋力
# ------------------------------------------------------------
# ⚠️ 绝不能「为了敢下而挑胜率差的点」—— 那不是棋风，是送分。
# 正确做法是「**等价着法之间按风格取舍**」：
#   ① 先算出 轮走方视角的最优胜率
#   ② 把胜率与最优差距 ≤ tol 的候选全收进「等价集」
#      （这个差距用百分点算，+3 也只放宽到 1.2 点）
#   ③ 等价集里按风格挑：
#        保守 (-) → visits 最多（引擎验证最充分 = 变化最简明）
#        标准 (0) → 引擎 order 第一（完全不掺私货）
#        激进 (+) → visits 最少 / 目差波动大（变化最复杂、对手最难算）
#
# 关键性质：等价集外的点**永不入选**。所以激进档最多也就丢掉 tol
# 那么多胜率，绝不会把一盘好棋下崩。这是可接受的代价上限。

# level: (允许的胜率损耗百分点, 候选池深度)
#
# ⚠️ 这张表**不是**单调的，而且不能改成单调（2026-10-23 想清楚才定）
# ------------------------------------------------------------
#     -3  0.00 / 1    死守引擎第一名
#     -2  0.05 / 2
#     -1  0.15 / 4    ← 允许比 0 档多一点损耗
#      0  0.00 / 1    ← 归零：必须跟改动前**一模一样**
#     +1  0.35 / 6
#     +2  0.70 / 10
#     +3  1.20 / 16
#
# 为什么 -1 的损耗上限比 0 档高：
#   0 档是「标准」= 老板不调激进度时的默认，这条路径**一个字节都不能变**，
#   所以它必须是最小值（0.00 / 1），等价于「原样返回引擎第一名」。
#   而 -1 是「略保守」—— 它要做的是「在几个差不多好的点里，
#   挑变化最简明的那个」，为此**愿意**付一点点胜率（0.15 点）当代价。
#   所以 -1 的上限高于 0 是设计意图，不是笔误。
#   -2/-3 反而收回来：越保守越不愿意付代价，到 -3 就直接死守第一名。
#
# 同理池深也不是单调的：-3 池深 1（只看第一名），0 档池深 1（不看别的），
# 但从 -1 开始就已经要看多个候选了。
# level: (允许的胜率损耗百分点, 候选池深度, **目差容差**)
#
# ⚠️ 第三列「目差容差」是 2026-10-27 补的，修老板报的那个 bug：
#    「在大劣势的情况下，激进度一提升，katago 的棋就下得乱七八糟，
#      正常是不会这样的，激进度是负的时候也是正常下棋不会乱七八糟」
#
# 为什么光有胜率容差不够（实测数据说话）
# ----------------------------------------------------
# 智子 VIP 云端，黑棋被打花的一个局面：
#     16 个候选，winrate 全是 0.99994 ~ 0.99997（极差 **0.00004**）
#     但 scoreLead 跨了 +310.9 ~ +320.6（极差 **9.68 目**）
# 也就是说：**胜率已经饱和到 0.0001 的精度，彻底失去区分度**。
# 而 +3 档的胜率容差是 1.2 个点（0.012），比 0.00004 大 300 倍 ——
# 「等价集」直接退化成整个候选池。后面再按「复杂度」排，
# 就变成「在全部候选里挑 visits 最少 / 目数波动最大」的那个，
# 而那恰恰就是引擎最没算透、最可能崩的点。
# 实测就选中 order=12 的第 13 号候选（胜率损耗表面上 0.00，
# 因为胜率压根分不出来）。
#
# 同类局面（均势）为什么看不出问题：
#     胜率极差 0.0168（1.68 个点），等价集只有前 8 名，还在合理范围。
# 所以这个 bug **只在大优大劣时暴露** —— 正是老板观察到的现象。
#
# 修法：等价集必须**同时**满足两个条件
#     · 胜率差 <= 容差（原来的判断，管中盘细微差别）
#     · 目差   <= 容差（新增，管胜率饱和时的大差别）
# 目差是不饱和的量：±310 的局面上它照样能分出 9 目差距。
_PICK = {
    -3: (0.00,  1, 0.0),   # 死守第一名，一点风险都不担
    -2: (0.05,  2, 0.3),
    -1: (0.15,  4, 0.6),
     0: (0.00,  1, 0.0),   # 标准 = 完全按引擎推荐，不掺私货（回归要求）
    +1: (0.35,  6, 0.6),
    +2: (0.70, 10, 1.2),
    +3: (1.20, 16, 2.0),
}

# 胜负已定的阈值（轮走方胜率低于 5% 或高于 95%）
#
# 到了这个区间，**任何「花活」都是纯亏**：
#   · 大优时乱搞 = 把赢棋送回去
#   · 大劣时乱搞 = 多输几目，翻不了盘（胜率 0.005% 翻什么）
# 所以这时激进档主动收手，跟着引擎走。见 pick_move 里的说明。
_DECIDED_WR = 0.05
# 胜负已定时，候选池最多看前几名（别去看 order=12 的第 13 号）
_DECIDED_DEPTH = 3

# 向引擎要几个候选。必须 ≥ _PICK 里最大深度（+3 要 16），
# 否则激进档拿不到足够深的池子，选点层就退化成「只在前几名里挑」。
_MAXMOVES = 16


def _pick_params(level):
    """激进度 → (允许的胜率损耗百分点, 候选池深度, 目差容差)

    ⚠️ 小数档的处理（2026-10-23 老板要 0.1 单位后补）
    ----------------------------------------------------
    损耗可以插值（它是连续的），但**池深必须取整**，而且要向上取整：
    池深是「看几个候选」，看 3.4 个没有意义；向下取整会让 +3.4 反而
    看得比 +3 少，出现「越激进越保守」的怪现象。

    ⚠️ 第三项「目差容差」（2026-10-27 加，修激进档大劣局面乱下）
    ----------------------------------------------------
    胜率在大优大劣时会**饱和**（0.99994 / 0.99997 这种），精度用完，
    等价集退化成全池。目差（scoreLead）不饱和，同一局面能差 9 目。
    两项都要过，见 _PICK 表上方那段实测数据。
    """
    lv = clamp_aggro(level)
    tol, depth, lead = _lerp_table(_PICK, lv)
    return (float(tol), max(1, int(math.ceil(depth - 1e-9))), float(lead))


def _side_wr(d, turn):
    """候选在**轮走方视角**下的胜率

    ⚠️ 引擎层统一给的是白方视角（见各 Backend 的 persp 字段），
    翻回轮走方才能比「谁更好」：白方视角下胜率高 = 白好，
    轮走方是黑的时候，胜率**越低**才是黑越好。搞反就是专挑坏棋。
    """
    w = d.get("winrate")
    if w is None:
        return None
    return w if str(turn).upper() == "W" else 1.0 - w


def _side_lead(d, turn):
    """候选在**轮走方视角**下的目差（正数 = 轮走方领先）

    和 _side_wr 同源：引擎给的是白方视角的 scoreLead，
    轮走方是黑时要取负。返回 None 表示这个候选没这个字段。
    """
    sl = d.get("scoreLead")
    if sl is None:
        return None
    return sl if str(turn).upper() == "W" else -sl


def pick_move(moves, level, turn="W"):
    """从候选池里按激进度挑一手

    moves : 引擎候选，已按 order 排好（order 0 = 引擎首选）
    level : -3.0~+3.0（0.1 步长）
    turn  : 轮走方（"B"/"W"），用来判断候选优劣

    返回被选中的那一项（dict），池空则 None。

    ⚠️⚠️ 2026-10-27 大修：修「大劣势下激进档乱下」
    ============================================================
    老板原话：
        「在大劣势的情况下，激进度一提升，katago 的棋就下得乱七八糟，
          正常是不会这样的，激进度是负的时候也是正常下棋不会乱七八糟」

    实测复现（智子 VIP，黑棋被打花的局面，16 个候选）：
        winrate   0.99994 ~ 0.99997   ← 极差 0.00004，饱和了
        scoreLead +310.9  ~ +320.6    ← 极差 9.68 目，还有区分度
    老代码只看 winrate：
        +3 档容差 1.2 个点 >> 极差 0.0004 个点
        → 等价集 = 全部 16 个候选
        → 再按「visits 少 + scoreStdev 大」挑 → 选中 order=12 的第 13 号
        → 那正是引擎最没算透的点，表现出来就是「棋下得乱七八糟」
    均势局面看不出问题，因为胜率没饱和（极差 1.68 个点），
    等价集只有前 8 名 —— 所以这 bug 专挑大优大劣时发作。

    三条修法，缺一不可：
      ① **目差也进等价判断**（tol 之外再加 lead_tol）
         胜率饱和时靠目差把关：9.68 目的差距远大于 +3 档的 2.0 目
      ② **胜负已定时收手**（|轮走方胜率 - 0.5| >= 0.45）
         大优大劣时任何花活都是纯亏：赢棋别送回去、输棋别多输几目。
         这时主动把等价集收到引擎前几名 —— 这不是「降智」，
         是「认账」：胜率 0.005% 的局面去搞复杂变化翻不了盘。
      ③ **visits 少不能当优点**
         老代码把「visits 少」当成「对手更难准备」的正面信号。
         但 visits 少同时也是「引擎没算透」的同义词，在劣势局里
         那往往就是崩盘点。现在只在**等价集内部**用它做次级排序，
         而且权重降到 0.25，主信号交给 scoreStdev（目数波动）。
    """
    if not moves:
        return None
    lv = clamp_aggro(level)
    tol_pct, depth, lead_tol = _pick_params(lv)
    # 标准档：一模一样照引擎的来（回归要求，一个字节都不能变）
    if abs(lv) < 1e-9:
        return moves[0]

    pool = list(moves[:max(1, depth)])
    if not pool:
        return None

    scored = []
    for d in pool:
        w = _side_wr(d, turn)
        if w is None:
            continue
        scored.append((d, w, _side_lead(d, turn)))
    if not scored:
        return pool[0]

    # 轮走方视角：胜率越高越好（_side_wr 已经把视角翻过来了）
    best = max(w for _, w, _ in scored)

    # ---- ② 胜负已定就收手 ----------------------------------------
    # 老板原话里「大劣势」那种局面，胜率已经贴着 0 或贴着 1，
    # 再怎么「激进」也只是换个输法。这时只在前 _DECIDED_DEPTH 名里选，
    # 且等价集只保留胜率并列第一的那几个。
    decided = abs(best - 0.5) >= (0.5 - _DECIDED_WR)
    if decided:
        pool = pool[:_DECIDED_DEPTH]
        pool_set = {id(d) for d in pool}
        scored = [(d, w, l) for d, w, l in scored if id(d) in pool_set]
        if not scored:
            return moves[0]
        best = max(w for _, w, _ in scored)
        # 胜负已定时容差直接归零：只要不是并列最好的，就不碰
        tol = 0.0
        lead_tol = 0.0
    else:
        tol = tol_pct / 100.0

    # ---- ① 胜率 + 目差 双条件筛等价集 ----------------------------
    # 目差只对「有 scoreLead 的候选」生效；全都缺就退回纯胜率
    # （本地某些权重/死活模式没目差概念，不能因此一个都选不出来）
    have_lead = any(l is not None for _, _, l in scored)
    if have_lead:
        # scoreLead 是「轮走方领先多少目」，所以也是**越大越好**，
        # 和 _side_wr 的单调方向一致（都是轮走方视角）。
        best_lead = max((l for _, _, l in scored if l is not None),
                        default=None)
        eq = []
        for d, w, l in scored:
            if best - w > tol:
                continue                       # 胜率差太多
            if (l is not None and best_lead is not None
                    and best_lead - l > lead_tol + 1e-9):
                continue                       # 目差差太多（胜率饱和时的把关）
            eq.append((d, w, l))
    else:
        eq = [(d, w, l) for d, w, l in scored if best - w <= tol]
    if not eq:
        # 一个都不合格 → 直接跟引擎第一名，不要硬凑
        return pool[0]

    if lv < 0:
        # 保守：等价集里挑 visits 最多的（引擎验证最充分、变化最简明）
        eq.sort(key=lambda p: (-(p[0].get("visits") or 0),
                               p[0].get("order") or 0))
        return eq[0][0]

    # ---- ③ 激进：等价集里挑「最复杂」的 --------------------------
    # 复杂度信号：
    #   scoreStdev 大 → 目数波动大（战斗型变化）   ← 主信号，权重 0.75
    #   visits 少     → 引擎没算透（对手更难准备） ← 次信号，权重 0.25
    # ⚠️ 权重从 0.5/0.5 改成 0.75/0.25（2026-10-27）：
    #    visits 少本身也是「这手可能有问题」的信号，给它一半权重
    #    等于把「引擎没算透」当成优点，实测在大劣局面里专挑崩盘点。
    #    现在它只在**已经过了双条件等价集**的候选里做微调，风险可控。
    st = [d.get("scoreStdev") or 0.0 for d, _, _ in eq]
    vs = [(d.get("visits") or 0) for d, _, _ in eq]
    st_lo, st_hi = min(st), max(st)
    vs_lo, vs_hi = min(vs), max(vs)

    def complexity(d):
        s_ = d.get("scoreStdev") or 0.0
        v_ = d.get("visits") or 0
        a = (s_ - st_lo) / (st_hi - st_lo) if st_hi > st_lo else 0.0
        b = (vs_hi - v_) / (vs_hi - vs_lo) if vs_hi > vs_lo else 0.0
        return 0.75 * a + 0.25 * b

    eq.sort(key=lambda p: (-complexity(p[0]), p[0].get("order") or 0))
    return eq[0][0]


# ================================================================ 引擎层

class CloudBackend:
    """智子云算力

    引擎参数全部来自 engines.py 注册表，跟 Lizzie 那个引擎列表一一对应：
    只要是智子客户端里能选的档位/权重，这里都能传进来。
    默认仍是老板定的「智子VIP算力 11b 天梯14374 (fdx)」。

    两种接法（自动选，也能用 --service / --direct 强制）
    ----------------------------------------------------
    service：走 8765 常驻服务。服务活着时分析 0 握手开销，
             适合反复跑、或在别处（网页、Lizzie）已开好服务的情况。
    direct ：进程内直连 `zz-ikatago`。不依赖任何外部进程和端口，
             一次握手 ~8s，一次对局全程复用。**没服务时的默认。**

    为什么要有 direct
    ----------------------------------------------------
    常驻服务需要 spawn 一个后台进程，这个动作很脆：
    父进程组退出、端口被占、防火墙拦本机回环……任一环节出问题，
    前台看到的就是 ConnectionResetError / 连不上。
    对「下一盘棋」这种单次长任务，多一个进程毫无收益，
    直连反而少一个故障点。
    """

    name = "cloud"

    def __init__(self, kind="vip", weight="fdx", visits=1200, budget=8.0,
                 komi=7.5, ensure_service=True, spec=None, mode=None,
                 rules=None, aggro=0):
        self.spec = spec or {}
        self.gpu = self.spec.get("gpu") or kind
        self.weight = self.spec.get("weight") or weight
        self.args = self.spec.get("args") or []      # 如让子局的 --kata-config
        self.kind_name = self._kind_of(self.gpu)
        self.visits, self.budget, self.komi = visits, budget, komi
        # 棋盘路数（2~19）。每手 analyze 会按传进来的 board 实际尺寸更新，
        # 所以同一局里换路数也不用重建引擎。
        self.n = self.spec.get("n") or 19
        # 规则（chinese/japanese/korean）—— 老板 2026-10-23 要求可调
        self.rules = norm_rules(rules or self.spec.get("rules"))
        # 激进度 -3~+3。真正下发在 _apply_style()，analyze 前调一次。
        self.aggro = clamp_aggro(aggro)
        self._style_sent = {}        # 已经下发过的参数（避免每手重复发）
        label = self.spec.get("name") or f"{self.gpu}/{self.weight}"
        self.info_text = f"智子云 · {label}"

        # direct 模式下持有的进程内引擎
        self._eng = None
        self.mode = mode
        if ensure_service:
            self._connect()

    @staticmethod
    def _kind_of(gpu_type):
        """--gpu-type 原值 → CLOUD_KINDS 的简写 key
        CLOUD_KINDS 用 vip/free/1x… 作 key，而引擎表里给的是
        vip-share/share/1x… 原值，必须映射，否则 CloudEngine 查不到档位，
        计费/描述全错（虽然能连上，但 info 会显示成默认档）。
        """
        return {"vip-share": "vip", "share": "free"}.get(gpu_type, gpu_type)

    def _connect(self):
        """定接法：显式指定优先；否则端口开着就用服务，否则进程内直连"""
        if self.mode in (None, "auto"):
            self.mode = "service" if self._port_open() else "direct"
        if self.mode == "service":
            try:
                self._ensure_service()
                return
            except Exception as e:
                log(f"  常驻服务不可用（{type(e).__name__}: {e}），改进程内直连")
                self.mode = "direct"
        self._start_direct()

    def _start_direct(self):
        """进程内直连云引擎（无外部进程依赖）"""
        import zhizi_cloud as Z
        log(f"云引擎进程内直连（{self.gpu} / {self.weight}），握手约 8s…")
        t0 = time.time()
        self._eng = Z.CloudEngine(kind=self.kind_name, weight=self.weight,
                                  extra_args=self.args, gpu=self.gpu)
        self._eng.start(quiet=True)
        self.info_text = (f"智子云直连 {getattr(self._eng,'version','')} / "
                          f"{self.weight} / {self._eng._name or ''}")
        log(f"云引擎就绪 {time.time()-t0:.1f}s　{self._eng._name}")

    def _api(self, path, body=None, timeout=120, retries=3):
        """调常驻服务。带重试 —— 换引擎的那一瞬间会 ConnectionResetError。

        为什么会有 ConnectionResetError（2026-10-22 实测）
        ----------------------------------------------------
        老版本换引擎走「/stop + 另起进程」，/stop 只关引擎、服务还占着
        8765，新进程 bind 失败 → 前台再连就是连接被重置。
        现在改成让常驻服务自己换热引擎（不再重启进程），但**关旧引擎 +
        连新引擎仍要 8~13s**，这期间偶发 RST 属于正常，重试即可。
        """
        last = None
        for i in range(max(1, retries)):
            try:
                req = urllib.request.Request(
                    CLOUD + path,
                    data=json.dumps(body or {}).encode(), method="POST")
                req.add_header("Content-Type", "application/json")
                with OP.open(req, timeout=timeout) as r:
                    return json.loads(r.read().decode())
            except (ConnectionResetError, ConnectionRefusedError,
                    urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if i < retries - 1:
                    time.sleep(1.2 * (i + 1))
        raise last

    def _ensure_service(self):
        """确保 8765 常驻服务在跑、且跑的是我要的引擎

        ⚠️ 三个状态必须分清（2026-10-22 踩过）
        ----------------------------------------------------
          a) 拿不到 /status          → 服务不在，spawn 一个
          b) 拿到 /status 但 alive=false → **服务在、引擎没起**（被 /stop 过）
                                        → 发 /start 让服务内启引擎，绝不 spawn
          c) 拿到 /status 且 alive=true 但引擎不是我要的
                                        → 发 /start {force:true} 服务内换热

        早先把 b 当成 a 处理，去 spawn 新进程抢 8765，bind 失败崩在后台，
        前台再连就是 ConnectionResetError —— 那个报错就是这么来的。
        """
        want = {"gpu": self.gpu, "weight": self.weight}

        # 先 socket 探活 —— 端口没监听就直接 spawn，别浪费 40s 去试 HTTP。
        # 实测：urllib 连一个没人监听的 127.0.0.1 端口，在 Windows 上
        # 有时要走完 IPv6 回退，单次就 3s+，40 次循环能拖到两分钟。
        if not self._port_open():
            self._spawn_service()              # (a) 服务真不在 → 起一个
            return

        st = None
        for _ in range(20):                    # 端口在但 HTTP 还没应答，最多等 20s
            try:
                st = self._api("/status", timeout=4, retries=1)
                break
            except Exception:
                time.sleep(1.0)

        if st is None:
            self._spawn_service()              # (a) 服务真不在 → 起一个
            return

        if not st.get("alive"):
            # (b) 服务在、引擎没起 → 让服务自己启，不重启进程
            log(f"云服务在但引擎没起，让服务内启引擎（{self.gpu}/{self.weight}）…")
            self._switch_in_service(force=False)
            return

        cur = {"gpu": st.get("gpu"), "weight": st.get("weight")}
        if cur.get("gpu") == want["gpu"] and cur.get("weight") == want["weight"]:
            self.info_text = (f"智子云 {st.get('version','')} / "
                              f"{st.get('weight','')} / {st.get('desc','')}")
            return

        # (c) 引擎不是我要的 → 服务内换热
        log(f"切换云引擎：{cur['gpu']}/{cur['weight']} → "
            f"{want['gpu']}/{want['weight']}（服务内切换，需 8~13s）")
        self._switch_in_service(force=True)

    @staticmethod
    def _port_open(host="127.0.0.1", port=8765, timeout=0.6):
        """端口有没有人在听 —— 比发 HTTP 快得多，也不会有 IPv6 回退延迟"""
        import socket
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _switch_in_service(self, force=True):
        t0 = time.time()
        try:
            self._api("/start", {"kind": self.kind_name, "weight": self.weight,
                                 "gpu": self.gpu, "extra": self.args,
                                 "force": force}, timeout=200, retries=2)
        except Exception as e:
            log(f"  服务内启动失败：{type(e).__name__}: {e}")
            self._spawn_service()
            return
        st = self._api("/status", timeout=10, retries=5)
        if not st.get("alive"):
            raise RuntimeError(f"引擎没起来：{st.get('err')}")
        self.info_text = (f"智子云 {st.get('version','')} / "
                          f"{st.get('weight','')} / {st.get('desc','')}")
        log(f"引擎就绪 {time.time()-t0:.1f}s → {st.get('weight')} "
            f"({st.get('model')})")

    def _spawn_service(self):
        """8765 上没有服务 → 后台拉一个（首次握手 ~9s）

        ⚠️ 打包后的启动方式完全不同（2026-10-23）
        ------------------------------------------------------------
        源码态： [python.exe, katago_cloud.py, --port, ...]
        打包态： sys.executable 是**我们自己的 exe**，不是 python.exe。
                 照抄老写法等于「再把 GUI 启动一遍」，参数全被忽略，
                 然后主程序一直等 8765 → 150 秒后报「云服务没起来」。

                 正确做法：exe 认一个 `--serve` 参数，走到 katago_cloud
                 的 main()（见 gui.py 入口）。这样同一个 exe 既是 GUI
                 又是子服务，不需要随包带解释器。

        RP.serve_argv() 已经把两种情况统一好了，这里不用再判断。
        """
        log(f"拉起云服务（{self.gpu} / {self.weight}），首次握手约 9s…")
        import subprocess
        RP.ensure_dirs()
        logf = open(os.path.join(RP.LOG_DIR, "auto_cloud.log"), "a",
                    encoding="utf-8")
        cmd = RP.serve_argv() + [
               "--port", "8765", "--preload",
               "--kind", self.kind_name, "--weight", self.weight,
               "--gpu", self.gpu]
        if self.args:
            cmd += ["--extra"] + list(self.args)
        # cwd 给 USER_DIR：子服务若写相对路径的东西，也落在可写目录
        #
        # ⚠️ 绝不能再用 DETACHED_PROCESS（0x00000008）—— 2026-10-27 修
        # ----------------------------------------------------
        # 老板截图里那个黑窗口就是这么来的：
        #   「然后启动引擎之后，会弹出一个这样的弹窗，能不能想办法去了不显示」
        #
        # DETACHED_PROCESS 的语义是「子进程**没有**控制台」。但我们这个
        # 子进程是 PyInstaller 打出来的 exe（GUI 子系统也是 console=False），
        # 它一旦往后写 stdout / 起 Tcl 之类需要控制台的东西，Windows 会
        # **现场给它新建一个控制台窗口** —— 标题就是 _MEIPASS 临时路径，
        # 内容全黑。宿主完全管不住它，只能让子进程压根没有控制台。
        #
        # 正确做法：CREATE_NO_WINDOW（0x08000000）
        #   → 给它一个「没有窗口的隐藏控制台」，写入照常成功，只是看不见。
        #   Python 文档明确写：这个 flag 与 DETACHED_PROCESS 互斥，
        #   要静默跑后台就选它。
        #
        # 为什么不能只靠 STARTUPINFO(SW_HIDE)
        #   SW_HIDE 只管「第一次显示」，子进程自己再申请控制台照样会露出来。
        subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                         cwd=RP.USER_DIR,
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
        t0 = time.time()
        while time.time() - t0 < 150:
            try:
                st = self._api("/status", timeout=3, retries=1)
                if st.get("alive"):
                    self.info_text = (f"智子云 {st.get('version','')} / "
                                      f"{st.get('weight','')} / {st.get('desc','')}")
                    log(f"云服务就绪 {st.get('bootSecs')}s")
                    return
            except Exception:
                pass
            time.sleep(1.0)
        raise RuntimeError("云服务 150s 内没起来，看 logs/auto_cloud.log")

    def analyze(self, board, turn, komi=None, visits=None, budget=None,
                stop_at_visits=None):
        # 路数跟着盘面走（老板要求支持 2~19 路）
        h = len(board)
        w = len(board[0]) if h else 0
        if self.mode == "direct":
            return self._analyze_direct(board, turn, komi, visits, budget,
                                        stop_at_visits)
        stones = []
        for y in range(h):
            for x in range(w):
                v = board[y][x]
                if v:
                    stones.append(["B" if v == 1 else "W", FB.to_gtp(x, y, h)])
        self._api("/position", {"w": w, "h": h,
                                "komi": komi if komi is not None else self.komi,
                                "stones": stones, "turn": turn,
                                "rules": self.rules}, timeout=30)
        r = self._api("/analyze", {"visits": visits or self.visits,
                                   "maxSecs": budget or self.budget,
                                   "maxmoves": _MAXMOVES, "turn": turn}, timeout=180)
        return self._normalize(r)

    # ------------------------------------------------ 规则 / 激进度

    def set_rules(self, rules):
        """换规则（chinese / japanese / korean）

        云端**不用**在这里发命令：规则是随 /position 一起传的，
        op_position 里会 e.rules(rules) + e.clear() 重新来过。
        直连模式则要显式发 kata-set-rules（见 _analyze_direct），
        但那个可以等下一手 —— 规则只在摆设完成后才有意义。
        """
        self.rules = norm_rules(rules)
        return self.rules

    def set_aggro(self, level):
        """换激进度。清空已下发记录 → 下一次 analyze 会全量重发

        ⚠️ 这里**立刻**下发一次（2026-10-23 端到端测出来的必要之举）
        ----------------------------------------------------
        原设计是「只改字段，等下一手 analyze 时再发」。问题：
        ① 老板在界面上拖完滑条，看日志会发现"改了但没动静"，
           得等到下一手才生效 —— 中间那手的日志还写着旧档位，自相矛盾。
        ② 对局结束后才改的话，参数永远发不出去。
        现在改成立刻发（引擎没起来就静默跳过），下一手 analyze 时
        因为 _style_sent 已记录，不会重复发，延迟没有增加。
        """
        self.aggro = clamp_aggro(level)
        self._style_sent.clear()
        try:
            self._apply_style_direct()
        except Exception as e:
            log(f"  激进度下发失败（不影响下棋）：{type(e).__name__}: {e}")
        return self.aggro

    def _apply_style_direct(self):
        """直连模式：把激进度对应的参数真正发给引擎

        ⚠️ 为什么要缓存（2026-10-23，为了落子延迟）
        ----------------------------------------------------
        每个 kata-set-param 都是一次完整 GTP 往返（约 30~60ms）。
        0 档下 7 个参数 = 白扔 0.3s/手，而实战值从来不变化。
        只在「档位变了」之后发一次，之后每手零开销。

        ⚠️ 缓存的是「引擎当前值」而不是「上次发过什么」
        ----------------------------------------------------
        详见 LocalBackend._push_style 的注释（同一个 bug）：
        用 _style_sent 判断时，set_aggro 一 clear 就再也不会恢复默认，
        切回 0 档的引擎仍带着激进配置。改用 _style_now 镜像。
        """
        e = self._eng
        if e is None:
            return
        if not hasattr(self, "_style_now"):
            self._style_now = dict(_STYLE_DEFAULT)
        want = style_params(self.aggro)
        for k in _AGGRO_KEYS:
            v = want.get(k, _STYLE_DEFAULT[k])
            if self._style_now.get(k) != v:
                e.set_param(k, v)
                self._style_now[k] = v
                self._style_sent[k] = v

    def _analyze_direct(self, board, turn, komi=None, visits=None, budget=None,
                        stop_at_visits=None):
        """进程内直连分析。

        视角：zhizi_cloud 已按 color(=turn) 翻成白方视角，这里不再翻。
        摆设：走 set_position（盘面没有手顺），不能逐手 play。
        路数：按 board 实际尺寸，自动支持 2~19 路。
        """
        e = self._eng
        h = len(board)
        w = len(board[0]) if h else 0
        k = komi if komi is not None else self.komi
        if getattr(e, "_komi_now", None) != k:
            e.komi(k)
            e._komi_now = k
        if getattr(e, "_n_now", None) != (w, h):
            e.boardsize(w, h)
            e._n_now = (w, h)
        if self.rules and getattr(e, "rules_name", None) != self.rules:
            e.rules(self.rules)
        e.set_position(board, turn, w, h)
        # ⚠️ 激进度放在 set_position **之后**发（2026-10-23）
        # set_position 在路数变化时会顺带 boardsize + clear_board，
        # 参数虽然不会因此丢失，但「先摆盘再调参」这个顺序能让
        # 参数生效的时机毫无歧义。0 档完全不发，保持引擎原样。
        self._apply_style_direct()
        r = e.analyze(visits=visits or self.visits, maxmoves=_MAXMOVES,
                      max_secs=budget or self.budget, turn=turn,
                      stop_at_visits=stop_at_visits)
        return self._normalize(r)

    def _normalize(self, r):
        return {
            "ok": bool(r.get("moves")),
            "elapsed": r.get("elapsed"),
            "visits": r.get("bestVisits"),
            "turn": r.get("turn"),
            "persp": "W",                       # 已翻成白视角
            "rootWinrate": self._w(r.get("best")),
            # ⚠️ 必须保留 order / scoreStdev（2026-10-23 补）
            # ----------------------------------------------------
            # 老代码这个列表只搬 5 个字段，把 order 和 scoreStdev 丢了，
            # 而选点层要靠它们：
            #   order      → pick_move 里「引擎第几名」的兜底排序键
            #   scoreStdev → 激进档算复杂度（目数波动 = 战斗型变化）
            # 丢了以后两处都退化成「用 dict 顺序碰运气」，
            # 表现为激进档有时挑不出真正复杂的点。
            "moves": [{"move": d.get("move"),
                       "winrate": d.get("winrate"),
                       "scoreLead": d.get("scoreLead"),
                       "scoreStdev": d.get("scoreStdev"),
                       "order": d.get("order"),
                       "visits": d.get("visits"),
                       "pv": d.get("pv") or []}
                      for d in (r.get("moves") or [])],
            "raw": r,
        }

    @staticmethod
    def _w(d):
        return d.get("winrate") if d else None

    def close(self):
        """直连模式要主动关掉云引擎进程，否则 zz-ikatago 会挂在那儿"""
        if self._eng is not None:
            try:
                self._eng.close()
            except Exception:
                pass
            self._eng = None


class LocalBackend:
    """本机 KataGo（进程内直连，GTP 模式）

    两种构造：
      1) spec 给了（注册表条目）→ 用它从 Lizzie config 里抠出来的
         exe / model / cfg / komi，按 GTP 起。这是**主路径**，
         18~23、30 号引擎都走这里。
      2) 只给 weight（老接口）→ 走 analysis JSON 协议。

    视角：GTP 下的 cfg 都没写 reportAnalysisWinratesAs（默认 BLACK），
    所以 local_engine 里统一 1-x 翻成白视角，这里不再翻。
    """

    name = "local"

    def __init__(self, weight="b28", visits=1200, budget=8.0, komi=7.5,
                 spec=None, rules=None, aggro=0, **kw):
        import local_engine
        self.spec = spec or {}
        self.visits = int(visits)
        self.budget = budget
        self.komi = self.spec.get("komi") if self.spec.get("komi") else komi
        self.is_lifego = self.spec.get("kind") == "lifego"
        # 规则 / 激进度（老板 2026-10-23 要求可调）
        self.rules = norm_rules(rules or self.spec.get("rules"))
        self.aggro = clamp_aggro(aggro)
        self._style_sent = {}
        self._rules_sent = None

        label = self.spec.get("key") or weight
        if self.spec.get("model"):
            log(f"启动本机引擎：{self.spec.get('name') or label} …")
            self.e = local_engine.LocalEngine(
                model=self.spec["model"], cfg=self.spec.get("cfg"),
                exe=self.spec.get("exe"), komi=self.komi,
                label=label, gtp=True)
        else:
            log(f"启动本机引擎 {weight} …")
            self.e = local_engine.LocalEngine(weight=weight)
        self.e.start(timeout=300, quiet=True)
        nm = self.spec.get("name") or weight
        self.info_text = (f"本机 · {nm} · {self.e.model_name}"
                          + (f"　komi={self.komi:g}" if self.is_lifego else ""))
        log(f"本机引擎就绪 {self.e.boot_secs}s")

    def analyze(self, board, turn, komi=None, visits=None, budget=None,
                moves=None, stop_at_visits=None):
        """统一返回白方视角结构。

        budget：GTP 模式能真的按秒掐（max_secs），比访问量折算准。
        路数：按 board 实际尺寸传，支持 2~19 路。
        """
        k = komi if komi is not None else self.komi
        v = visits or self.visits
        h = len(board)
        w = len(board[0]) if h else 0
        if self.e.gtp_mode:
            # 规则 / 激进度只在**变化时**下发，稳态每手零额外 GTP 往返
            self._apply_gtp_opts()
            r = self.e.analyze(board=board, turn=turn, komi=k, visits=v,
                               maxmoves=_MAXMOVES, max_secs=budget or self.budget,
                               w=w, h=h, stop_at_visits=stop_at_visits)
        else:
            if budget:
                v = max(200, min(v, int(budget * 900)))
            r = self.e.analyze(board=board, turn=turn, komi=k,
                               visits=v, maxmoves=_MAXMOVES, w=w, h=h,
                               rules=self.rules or "chinese")
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error"), "moves": [],
                    "elapsed": r.get("elapsed")}
        out = [{"move": d.get("move"),
                "winrate": d.get("winrate"),
                "scoreLead": d.get("scoreLead"),
                # order / scoreStdev 同上：选点层要用，不能丢
                "scoreStdev": d.get("scoreStdev"),
                "order": d.get("order"),
                "visits": d.get("visits"),
                "pv": d.get("pv") or []}
               for d in (r.get("moves") or [])]
        return {"ok": bool(out), "elapsed": r.get("elapsed"),
                "visits": r.get("visits") or r.get("bestVisits"),
                "turn": turn.upper(), "persp": "W",
                "rootWinrate": r.get("rootWinrate"),
                "moves": out, "raw": r,
                "lifego": self.is_lifego}

    # ------------------------------------------------ 规则 / 激进度

    def set_rules(self, rules):
        """换规则。已下发记录清掉 → 下一次 analyze 会真发 kata-set-rules"""
        self.rules = norm_rules(rules)
        self._rules_sent = None
        return self.rules

    def set_aggro(self, level):
        """换激进度。清空记录后**立刻**下发（引擎已起的前提下）

        理由同云端 set_aggro：老板拖完滑条要马上看到效果，
        不能等下一手。_apply_gtp_opts 内部按 _style_sent 去重，
        所以下一手 analyze 不会重复发，延迟不增加。
        """
        self.aggro = clamp_aggro(level)
        self._style_sent.clear()
        try:
            if self.e.gtp_mode:
                self._apply_gtp_opts()
        except Exception as e:
            log(f"  激进度下发失败（不影响下棋）：{type(e).__name__}: {e}")
        return self.aggro

    def _apply_gtp_opts(self):
        """GTP 模式下把规则 / 激进度真正下发给本机 KataGo

        只在**值变了**的时候发。理由同云端 _apply_style_direct：
        每条 kata-set-rules / kata-set-param 都是一次 GTP 往返，
        稳态每手都发等于白扔 0.3s 延迟 —— 老板要「尽快落子」。
        """
        e = self.e
        if self.rules and self._rules_sent != self.rules:
            resp = e.send(f"kata-set-rules {self.rules}", timeout=20)
            if resp and resp.startswith("?"):
                log(f"  规则设置被拒（{self.rules}）：{resp[:60]}，仍用引擎默认")
                self.rules = None
            else:
                self._rules_sent = self.rules
        self._push_style(e)

    def _push_style(self, e):
        """把激进度对应的参数下发给引擎（恢复默认 + 设置新值）

        ⚠️ 恢复默认的判据必须是「引擎当前值」而不是「上次发过什么」
        ----------------------------------------------------
        原实现用 `if self._style_sent.get(k) not in (None, v)` 判断要不要
        恢复默认。但 set_aggro() 会先 clear() 掉 _style_sent，于是这个
        条件在清空后永远为 False → **从 +3 切回 0 档时一个参数都不恢复**，
        引擎继续带着 +3 的激进配置跑。这是端到端测试抓到的真 bug。
        正确做法：维护一份「我以为引擎当前是什么值」的镜像
        _style_now（初始一律等于默认值），按它比对，clear 不影响它。
        """
        if not hasattr(self, "_style_now"):
            self._style_now = dict(_STYLE_DEFAULT)
        want = style_params(self.aggro)
        for k in _AGGRO_KEYS:
            v = want.get(k, _STYLE_DEFAULT[k])
            if self._style_now.get(k) != v:
                e.set_param(k, v)
                self._style_now[k] = v
                self._style_sent[k] = v

    def close(self):
        try:
            self.e.close()
        except Exception:
            pass


def pick_engine_interactive(reg=None):
    """交互式选引擎 —— 对应老板截图里那个「选择要加载的引擎」对话框

    在终端里列出全部 20 项，输入序号回车即可。回车（不输）＝用默认。
    返回选中的 spec（dict），取消则返回 None。
    """
    import engines as REG
    reg = reg or REG.registry()
    txt, _ = REG.table(reg)
    dk = REG.default_key(reg)
    print()
    print("  选择要加载的引擎")
    print("  " + "-" * 72)
    print(txt)
    print("  " + "-" * 72)
    print(f"  输入序号 1~{len(reg)}，直接回车用默认（{dk}），q 退出")
    while True:
        try:
            s = input("  引擎 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if s.lower() in ("q", "quit", "exit"):
            return None
        if not s:
            return REG.find(dk, reg)
        if s.isdigit():
            i = int(s) - 1
            if 0 <= i < len(reg):
                return reg[i]
            print(f"  序号超范围（1~{len(reg)}）")
            continue
        e = REG.find(s, reg)
        if e:
            return e
        print(f"  没这个引擎：{s}")


def make_engine_from_spec(spec, visits=1200, budget=8.0, komi=7.5,
                          mode=None, rules=None, aggro=0):
    """按注册表条目直接造引擎 —— GUI 用这个（它手上只有 spec）

    跟 make_engine(args) 的区别：那个还要解析命令行优先级，
    这里 spec 已经是老板在下拉框里点定的那一项，不该再被兜底逻辑改写。

    rules / aggro 是老板 2026-10-23 要的可调项，两者都在造引擎时定下来
    （之后再改走 set_rules / set_aggro，不用重建引擎）。
    """
    if not spec:
        raise ValueError("没给引擎")
    if spec.get("kind") in ("local", "lifego"):
        return LocalBackend(visits=visits, budget=budget, komi=komi, spec=spec,
                            rules=rules, aggro=aggro)
    return CloudBackend(visits=visits, budget=budget, komi=komi, spec=spec,
                        mode=mode, rules=rules, aggro=aggro)


def make_engine(args):
    """按 --engine / --list 选引擎

    优先级：
      1) --engine <key 或序号>  精确指定（key 见 python engines.py）
      2) --engine cloud|local   按大类，用默认权重
      3) 都不给                  → 注册表里的默认项（VIP fdx）
    """
    import engines as REG

    reg = REG.registry()
    pick = args.engine

    # 序号（Lizzie 原编号：1~8/10 云、18~23 本机、30 死活）
    spec = REG.find(pick, reg) if pick else None
    rules = norm_rules(getattr(args, "rules", None))
    aggro = clamp_aggro(getattr(args, "aggro", 0))

    if spec is not None:
        if spec["kind"] in ("local", "lifego"):
            return LocalBackend(visits=args.visits, budget=args.budget,
                                komi=args.komi, spec=spec,
                                rules=rules, aggro=aggro)
        return CloudBackend(visits=args.visits, budget=args.budget,
                            komi=args.komi, spec=spec,
                            mode=getattr(args, "cloud_mode", None),
                            rules=rules, aggro=aggro)

    if pick == "local":
        # 没给具体序号却说要本机 → 用最强的本机那项（18 号 11B）
        for e in reg:
            if e["kind"] == "local" and "11B" in e["name"]:
                return LocalBackend(visits=args.visits, budget=args.budget,
                                    komi=args.komi, spec=e,
                                    rules=rules, aggro=aggro)
        for e in reg:
            if e["kind"] == "local":
                return LocalBackend(visits=args.visits, budget=args.budget,
                                    komi=args.komi, spec=e,
                                    rules=rules, aggro=aggro)

    # cloud（默认）或没给
    if pick in (None, "", "cloud"):
        dk = REG.default_key(reg)
        spec = REG.find(dk, reg) if dk else None
        if spec:
            return CloudBackend(visits=args.visits, budget=args.budget,
                                komi=args.komi, spec=spec,
                                mode=getattr(args, "cloud_mode", None),
                                rules=rules, aggro=aggro)

    gpu = args.kind or "vip-share"
    if gpu == "vip":
        gpu = "vip-share"
    wt = args.weight or "fdx"
    return CloudBackend(visits=args.visits, budget=args.budget,
                        komi=args.komi,
                        spec={"gpu": gpu, "weight": wt,
                              "name": f"{gpu}/{wt}", "args": []},
                        mode=getattr(args, "cloud_mode", None),
                        rules=rules, aggro=aggro)


# ================================================================ 对手方

def _place_ret(r):
    """把 place() 的返回值统一成 (ok, why)

    ⚠️ 为什么需要这个（2026-10-23 改接口后的兼容层）
    ----------------------------------------------------
    `place()` 原来返回 bool，我为了区分「落子失败」和「野狐被收起来」
    改成了 (bool, "ok"/"fail"/"paused")。但**测试里有 mock 直接返回 bool**，
    也难保没有别的调用点。这里做一次归一化，两种都能吃：
        True / False        → (bool, "ok"/"fail")
        (ok, why)           → 原样
    这样接口演进不会把老调用方炸掉（实测 _t5b 就是被这个炸的）。
    """
    if isinstance(r, tuple):
        if len(r) >= 2:
            return bool(r[0]), r[1]
        return bool(r), "ok"
    return bool(r), ("ok" if r else "fail")


def _new_stones(before, after):
    """列出 after 相对 before **新出现**的棋子 → [("E15", "黑"), ...]

    2026-11-01 加：落子「未见生效」时必须能回答一句话 ——
    **那一手到底点哪儿去了**。只报「没生效」等于什么都没说。
    """
    out = []
    if not after:
        return out
    n = len(after)
    for y in range(len(after)):
        row = after[y]
        for x in range(len(row)):
            v = row[x]
            if not v:
                continue
            b0 = 0
            if before and y < len(before) and x < len(before[y]):
                b0 = before[y][x]
            if v != b0:
                try:
                    out.append((FB.to_gtp(x, y, n), "黑" if v == 1 else "白"))
                except Exception:
                    out.append((f"({x},{y})", "黑" if v == 1 else "白"))
    return out


def _count(board):
    nb = sum(1 for r in board for v in r if v == 1)
    nw = sum(1 for r in board for v in r if v == 2)
    return nb, nw


def turn_by_title(title):
    """只看标题的「第 N 手」→ 下一手该谁走（读不到返回 None）

    ⚠️ 标题会滞后（2026-11-01 现场）：野狐落子后 0.3~1s 才改数字。
    所以它只能当**参考**，不能单独拿它决定要不要落子。
    """
    m = re.search(r"第\s*(\d+)\s*手", title or "")
    if not m:
        return None
    done = int(m.group(1))
    return "B" if (done + 1) % 2 == 1 else "W"


def turn_by_mark(last_move):
    """只看最后一手的反色三角 → 下一手该谁走（没有/不可信返回 None）"""
    if not last_move:
        return None
    try:
        ratio, color = last_move[1], last_move[2]
    except (IndexError, TypeError):
        return None
    if ratio is not None and FB.LAST_RATIO * 2 <= ratio < 0.60:
        return "W" if str(color).upper() == "B" else "B"
    return None


def side_to_move(board, title="", last_move=None):
    """推断下一手该谁走

    优先级（2026-10-22 老板定的口径：看最后一手的反色标记）
    ------------------------------------------------------
        1) **最后一手标记** —— 野狐在最后一手的棋子上画一个反色三角
           （白子上黑三角、黑子上白三角，直角在圆心、朝右下角）。
           这是最硬的证据：直接看到「谁刚走过」，对面就是轮走方。
           不依赖提子、不受标题文本格式影响、也比标题刷新快。
        2) 标题「第N手」—— 文本来源，会滞后一拍
           （野狐落子后标题有时要 0.3~1s 才更新）。
        3) 数子 —— 只在无提子时成立，纯兜底。

    ⚠️ 什么时候不信标记
    --------------------
    find_last_move 已经用「反色占比 + 单象限集中度」两道门把界面元素
    排除掉了（实测：真三角 0/0/0/100，居中黑方块 34/24/24/17）。
    这里的 `ratio < 0.60` 是第三道保险：万一某天界面改版，
    整个棋子被换色，也不至于凭一次怪异像素翻错轮次。

    标记和标题**结论相反**时：
      - 两个都是完整证据 → 信标题（纯文本，不受渲染影响）
      - 标记独有（标题没写第N手）→ 信标记

    last_move 由 FB.find_last_move() 给出，形如 ("R12", 0.204, "W")。
    """
    t_title = turn_by_title(title)
    t_mark = turn_by_mark(last_move)
    if t_mark is not None:
        if t_title is None:
            return t_mark
        if t_title == t_mark:
            return t_mark
        # 打架：信标题（稳定性优先）
        return t_title

    if t_title is not None:
        return t_title

    nb, nw = _count(board)
    if nb == nw:
        return "B"
    if nb == nw + 1:
        return "W"
    # 差距更大 = 有提子，按奇偶粗判
    return "B" if nb <= nw else "W"


def board_key(board):
    return tuple(v for r in board for v in r)


# 标题里出现这些词 = 这盘已经结束了，不必再等轮次
# （2026-10-23 现场抓到的原文：
#   "> [高级房1] > 22|124号房间 对局结束 (白 掉线负) - 友谊赛"）
#
# ⚠️ 词表必须「窄而硬」（2026-10-23 收紧）
# ----------------------------------------------------
# 原词表里有 "结算"、"复盘"、"查看棋谱" 三个词，全是错的：
# 它们是**界面按钮/菜单文案**，野狐窗口标题里常带着整条导航路径，
# 一旦扫到就误判成「对局结束」→ 老板看到的就是「启动后立刻结束」。
# "已结束" 也太宽 —— 「未结束」是它的超集，正好反着。
# 现在只留**棋局自身状态**的明确措辞。
_OVER_WORDS = (
    "对局结束", "对局已结束", "棋局结束", "终局",
    "掉线负", "掉线胜", "认输", "中盘胜", "中盘负",
    "超时负", "超时胜", "数子结果", "数目结果",
)


def _game_over(title):
    """标题里是否已表明这盘结束了

    为什么要专门判这个
    ----------------------------------------------------
    棋局结束后，野狐**不会**把「该谁走」从界面上抹掉 ——
    最后一手的标记和轮次都还留在原处。于是按轮次判断的程序会一直
    以为「还在等对手下」，永远等不到，表现就是「加载完不下棋」。

    只认标题里的明确结束语，不猜。宁可漏判（继续等对手，有 210 秒兜底）
    也不要误判（把进行中的对局当成结束）。

    ⚠️ 为什么不再匹配 "已结束"
    ----------------------------------------------------
    「未结束」包含「已结束」这三个字。野狐标题/提示里只要出现过
    "未结束"，`any(k in t)` 就会命中 → 开局第一秒判定收工 →
    正是 2026-10-23 老板报的「引擎启动之后直接就结束了，一手棋都不下」。
    现在先排否义词，再只用不会被否义词包住的措辞。
    """
    t = title or ""
    if not t:
        return False
    # 先排否义：出现「未结束」就绝不算结束（哪怕里面也含别的词）
    if "未结束" in t:
        return False
    return any(k in t for k in _OVER_WORDS)


# ================================================================ 主控

class Auto:
    def __init__(self, engine, my_color="W", komi=7.5,
                 think=0.0, verbose=True, max_moves=0,
                 think_mode="fixed", think_min=1.0, think_max=10.0,
                 visits=10000,
                 warmup=3.0, rules=None, aggro=None):
        """think = **每手思考时间**（秒），想完立刻落子

        ⚠️ 2026-10-23 老板定的两条硬规则
        ----------------------------------------------------
        ① 「自己做成想完立刻落子的，不需要勾选立刻落子的选项」
           → 落子后**不再有任何额外干等**（老版本的「落子间隔」已删）
        ② 「把思考上限和最多手数选项去掉，不需要限制这两个」
           → max_moves 默认 0 = **不限手数**，一直下到对局结束/手动停
             思考时间由界面「思考时间」一栏决定，Auto 不再兜底
        ③ 「你把演练模式给取消掉」
           → dry 参数已删。这个模式实际是 bug 温床：它不落子 →
             盘面永远不变 → 主循环命中「同一盘面」分支直接 return，
             表现出来就是「启动后一手都不下、立刻结束」。
        """
        self.eng = engine
        self.my = my_color.upper()
        self.komi = komi
        self.think = think
        self.think_mode = think_mode
        self.think_min = think_min
        self.think_max = think_max
        # 按计算量落子的访问量（1~100000，默认 10000）
        self.visits = max(1, min(100000, int(visits or 10000)))
        # 思考时间要能在**对局进行中**被 GUI 改（老板 2026-10-23 要求），
        # 三个字段必须整体更新，所以配一把锁。见 set_think()。
        self._think_lock = threading.Lock()
        self.warmup = warmup
        # 规则 / 激进度：老板 2026-10-23 要求可调。
        # ⚠️ rules 不传时**继承引擎后端的值**（2026-10-23 端到端发现的坑）
        # ----------------------------------------------------
        # 引擎造出来时可能已经带 rules（make_engine_from_spec 传进去的）。
        # 如果这里硬写成 None，_push_opts 会看到 "后端=chinese / 我=None"
        # 以为不一致，每手都白调一次 setter。
        # 继承后两边一致，稳态零开销。
        if rules is None:
            self.rules = getattr(engine, "rules", None)
        else:
            self.rules = norm_rules(rules)
        # ⚠️ aggro 的默认值必须是 None 才走「继承后端」这一支（2026-10-23）
        # ----------------------------------------------------
        # 老签名是 `aggro=0`，而下面写的是
        #     self.aggro = getattr(engine, "aggro", 0.0) if aggro is None else ...
        # —— 参数默认 0 永远不是 None，所以「继承」那一支**是死代码**，
        # 实测确认：后端 aggro=1.7、Auto() 不传，Auto.aggro 拿到的是 0.0。
        # 后果：每局开局都会白推一次 set_aggro（引擎明明已经是 1.7），
        # 而 _push_opts 的去重又是按「后端字段 vs 我的字段」比的，
        # 这一推等于把 1.7 改回 0 —— 真正下发的方向也错了。
        # rules 那边默认值是 None，一直是对的；这里对齐。
        self.aggro = (clamp_aggro(getattr(engine, "aggro", 0.0))
                      if aggro is None else clamp_aggro(aggro))
        # ⚠️ 「GUI 刚改过规则/激进度」的标记（2026-10-23 加）
        # set_rules / set_aggro 只写字段不发命令（线程安全），
        # 由 _push_opts 在工作线程里于下一手开头补发。
        # 这个标记本身不参与判断（_push_opts 比对的是后端字段），
        # 留着是为了将来排查「改了没生效」时能看出有没有人改过。
        self._opts_dirty = False
        self.verbose = verbose
        # ⚠️ 0 = 不限手数（老板要求删掉这个限制）
        self.max_moves = int(max_moves or 0)
        self.history = []
        self.stats = {"reads": 0, "clicks": 0, "fail": 0, "t_read": 0.0,
                      "t_think": 0.0, "marks": 0}
        self._last_mark = None   # 最近一次读到的最后一手标记
        # 野狐窗口状态："min"=最小化 / "closed"=没了 / None=正常
        # 只用来「状态变化时说一次」，避免每 0.8 秒刷一行日志
        self._win_gone = None
        # 最近一次分析结果（GUI 用来显示「计算量/胜率/目差/首选点」）
        self.last_ana = None
        # ---- GUI 用的钩子（CLI 下全是 None，不影响原行为）----
        self._stop = False       # stop() 置真，主循环下一圈退出
        self._fail_dumps = 0     # 落子失败现场最多存 3 张（诊断用）
        # 同一类问题只报一次（老板 2026-11-02：别一轮一轮重复报）
        self._dup_log = {}
        self._pause = False      # pause() 置真，主循环原地待命（引擎不关）
        self.on_board = None     # observe 后回调 (board, title, turn, (nb,nw), last)
        self.on_move = None      # 落子后回调 (color, gtp, winrate, lead)
        self.on_state = None     # 状态回调 (kind, text)，如 "engine"/"done"
        self.on_info = None      # 分析后回调 (首选点, 计算量, 胜率, 目差)

    def _log_once(self, key, msg, cooldown=60.0):
        """同一类问题在 cooldown 秒内只写一次日志

        老板 2026-11-02：「相同的问题报错一次就可以了，不用重复弹」。
        失败重试是每秒都在跑的，不拦一下日志区就会被同一句话刷满，
        真正有用的信息反而被顶掉。
        """
        now = time.time()
        last = self._dup_log.get(key)
        if last is not None and now - last < cooldown:
            return False
        self._dup_log[key] = now
        log(msg)
        return True

    def stop(self):
        """让主循环下一圈就退出（GUI 的「关闭引擎」按钮调它）

        故意只置标志、不强杀线程：正在落子的那一步必须让它走完，
        半路掐断可能点到一半，反而更危险。
        """
        self._stop = True

    def pause(self):
        """暂停下棋但**不关引擎**（老板要的「暂停」）

        和 stop 的区别：引擎进程、权重、缓存全留着。
        恢复时立刻就能接着算，不用再等 2~9s 的握手/预热。
        """
        self._pause = True

    def resume(self):
        self._pause = False

    @property
    def paused(self):
        return self._pause

    def set_think(self, think=None, mode=None, lo=None, hi=None,
                  visits=None):
        """**对局进行中**改思考时间 —— 下一手就生效（老板 2026-10-23 要求）

        老板原话：
            「一个是要在下棋的时候也能随意调整思考或随机思考的时间」

        为什么需要专门的方法而不是直接改属性
        ----------------------------------------------------
        主循环跑在后台线程里，GUI 在主线程改 `bot.think` 是**跨线程写**。
        在 CPython 里单个 float 赋值本身是原子的，但这里要一次改
        「模式 + 上界 + 下界」三个字段，中间可能被主循环读到 ——
        于是会出现「模式已经切成 random、但 lo/hi 还是上次的 fixed 值」
        这种半新半旧的状态，算出来的等待时间莫名其妙。
        加一把锁把三个字段作为整体更新，读侧 `_think_secs()` 也持锁拷一份。

        参数都可以省（None = 不改），这样 GUI 只在真正变了的字段上调用。
        返回 (ok, 说明)；ok=False 表示参数非法，界面该拒绝这次修改。

        ⚠️ 负值一律拒绝，**不许静默接受**（2026-10-23 查 bug 收紧）
        ----------------------------------------------------
        第一版只检查了 `new_hi < 0`，于是 `set_think(lo=-5)` 返回 ok=True：
        老板在界面下限输入框里打出一个负号，程序回「已改成 …」，
        实际拿着一个 [-5, 10] 的区间在跑 —— 随机里抽到负数，
        `_pad_think(want<0)` 直接返回 0 不补齐，那几手快得莫名其妙。
        现在上下限、固定值**全部**要求 >= 0，参数不合法就明确拒绝
        （界面据此不提交，值留在输入框里等老板自己改）。
        """
        with self._think_lock:
            new_mode = self.think_mode if mode is None else str(mode).lower()
            if new_mode not in ("fixed", "random", "visits"):
                return False, f"未知模式 {mode!r}"

            # 2026-11-03：按计算量模式（1~100000，非法按 10000）
            if new_mode == "visits":
                v = self.visits if visits is None else _to_float(visits, None)
                if v is None:
                    v = 10000
                self.visits = max(1, min(100000, int(round(v))))
                self.think = 0.0
                self.think_mode = new_mode
                snap = (self.think, self.think_mode, self.think_min,
                        self.think_max)
                return True, self._think_text(snap)

            # ⚠️ 解析失败要能分辨（2026-10-23 查 bug）
            # 老写法 `_to_float(lo, self.think_min)` 把「解析不出来」和
            # 「就是旧值」混成一个结果 —— 老板打错一个字符，程序既不报错
            # 也不改，界面看着像没反应。现在先转成 None 表示失败，再回退。
            new_lo = self.think_min if lo is None else _to_float(lo, None)
            new_hi = self.think_max if hi is None else _to_float(hi, None)
            if new_lo is None or new_hi is None:
                return False, "思考时间必须是数字"
            # 2026-11-03：下限 1 秒；固定值取整，随机区间保留小数
            new_lo = max(1.0, float(new_lo))
            new_hi = max(1.0, float(new_hi))

            if new_mode == "random":
                new_lo, new_hi = min(new_lo, new_hi), max(new_lo, new_hi)
                if new_lo < 1 or new_hi < 1:
                    return False, "思考时间至少 1 秒"
                # 随机模式下，think 也要跟着给成上限 —— 有些后端按 think
                # 设 max_secs，不跟着改会出现「说好 30 秒，引擎 5 秒就停」。
                self.think = new_hi
            else:
                if think is None:
                    v = self.think
                else:
                    v = _to_float(think, None)
                if v is None:
                    return False, "思考时间必须是数字"
                v = max(1, int(round(v)))
                if v < 1:
                    return False, "思考时间至少 1 秒"
                self.think = float(v)

            self.think_mode = new_mode
            self.think_min = new_lo
            self.think_max = new_hi
            snap = (self.think, self.think_mode, self.think_min, self.think_max)

        return True, self._think_text(snap)

    def _think_text(self, snap=None):
        """把当前思考设置说成一句人话（快照可选，避免界面读时被打断）"""
        if snap is None:
            with self._think_lock:
                snap = (self.think, self.think_mode,
                        self.think_min, self.think_max)
        think, mode, lo, hi = snap
        if mode == "visits":
            return f"按计算量 {getattr(self, 'visits', 10000)}v"
        if mode == "random":
            return f"随机 {lo:g}~{hi:g}s"
        return f"固定 {think:g}s"

    def _think_secs(self):
        """这一手「想多久」—— 固定秒数或随机区间

        ⚠️ 2026-10-23 老板报的「选了固定时间，仍然随机落子」
        ----------------------------------------------------
        根因不是这里算错了，而是下面 `_pad_think()` 那段：
        这个值只是传给引擎的**预算上限**，引擎一提前返回就立刻落子，
        于是「固定 5 秒」在简单局面下变成 0.2 秒、复杂局面 5 秒 ——
        老板看到的就是「随机」。现在由 `_pad_think()` 负责补足。

        返回值语义（改后）：
          固定模式 → **精确**的思考时长（含补齐）
          随机模式 → 区间内随机一个，同样补齐到该值

        ⚠️ 只是 _think_snapshot() 的薄封装（保留是因为老调用点/测试
           还在用它）。要同时拿 mode/lo/hi 请直接用 _think_snapshot()，
           别分开读 —— 那正是「随机目标 1.0s ∈ 5~5」的成因。
        """
        return self._think_snapshot()[4]

    def _think_snapshot(self):
        """一次性取「思考设置 + 这一手该想多久」的原子快照

        返回 (think, mode, lo, hi, secs)：
            think  固定模式的目标秒数（随机模式下等于区间上限）
            mode   "fixed" / "random"
            lo,hi  随机区间（固定模式下也返回，便于日志统一定宽）
            secs   这一手实际该想的秒数（随机模式已抽好）

        ⚠️ 为什么要专门做这个（2026-10-23 查 bug）
        ----------------------------------------------------
        主循环里既要**算**（secs），又要**写日志**（mode/lo/hi）。
        老代码分两次读，中间只要 GUI 调了 set_think()，日志就会写出
        「随机目标 1.0s ∈ 5~5」这种自相矛盾的行 —— 模式是新的、
        区间还是旧的。一次拿全，判断和显示就永远自洽。
        """
        with self._think_lock:
            mode = self.think_mode
            think = self.think
            lo, hi = self.think_min, self.think_max
        if mode == "visits":
            return think, mode, lo, hi, None
        if mode == "random":
            lo, hi = min(lo, hi), max(lo, hi)
            # 随机时间可以抽到小数（老板 2026-11-03 明确要求）
            secs = random.uniform(max(1.0, lo), max(1.0, hi))
        else:
            # 固定时间只收整数秒
            secs = float(max(1, int(round(think or 1))))
        return think, mode, lo, hi, secs

    def _pad_think(self, t_start, want):
        """把「已经想了多久」补齐到 want 秒

        ⚠️ 为什么必须补（2026-10-23 老板报的真 bug）
        ----------------------------------------------------
        `think` 传下去是引擎的 max_secs，是**上限**不是下限：
          · 简单局面引擎 0.2 秒就收敛并返回 → 立刻落子
          · 复杂局面跑满 5 秒
        同一局里手手不同，老板设的「固定 5 秒」观感上就是随机落子。

        现在统一：算完 → 差多少补多少 → 到点才落。
        这样「固定」真的是固定，「随机 0~10」也真的是在 0~10 里随机，
        两者都只是把**不同的目标时长**写清楚而已。

        返回实际补了多少秒（给日志用）。
        """
        if not want or want <= 0:
            return 0.0
        used = time.time() - t_start
        left = want - used
        if left <= 0.02:            # 已经超了（或刚好到），不补也不倒退
            return 0.0
        t0 = time.time()
        # 分片睡：这期间按停止键要能立刻退出，别傻睡 5 秒
        while True:
            if self._stop:
                break
            rest = want - (time.time() - t_start)
            if rest <= 0.01:
                break
            time.sleep(min(0.05, rest))
        return time.time() - t0

    def _sleep_unless_stopped(self, secs):
        """分片睡，期间响应停止/暂停 —— 别一睡 10 秒对停止键没反应"""
        end = time.time() + max(0.0, secs)
        while time.time() < end:
            if self._stop:
                return False
            time.sleep(min(0.1, end - time.time()))
        return True

    def _wait_if_paused(self):
        """暂停中就原地待命，直到 resume 或 stop"""
        if not self._pause:
            return True
        self._emit("on_state", "paused", "已暂停")
        while self._pause:
            if self._stop:
                return False
            time.sleep(0.15)
        if not self._stop:
            self._emit("on_state", "running", "运行中")
        return not self._stop

    def _emit(self, name, *a):
        cb = getattr(self, name, None)
        if cb:
            try:
                cb(*a)
            except Exception:
                pass     # GUI 回调炸了不能连带把对局搞崩

    # ------------------------------------------------ 野狐窗口不见了
    #
    # ⚠️ 老板 2026-10-23 的硬要求：
    #   「让野狐自动对弈启动的时候，不会强制控制野狐围棋的打开或者关闭」
    #
    # 所以野狐最小化 / 被关掉时，程序**不能**：
    #   ① 把它还原出来（那是劫持）
    #   ② 报错退出（老板只是去查个东西）
    # 只能安静待命，引擎保持就绪（重新开野狐不用再握手 9 秒）。

    def _note_window_gone(self, e):
        """野狐不见了 → 只在**状态变化时**说一次，不刷屏"""
        kind = "min" if getattr(e, "minimized", False) else "closed"
        if self._win_gone == kind:
            return                      # 一直是同一状态，不重复说
        self._win_gone = kind
        if kind == "min":
            log("野狐已最小化 —— 暂停落子，等你把它恢复（引擎保持就绪）")
            self._emit("on_state", "hidden", "野狐已最小化·待命")
        else:
            log("野狐窗口不在了 —— 暂停落子，等你重新打开客户端（引擎保持就绪）")
            self._emit("on_state", "hidden", "野狐已关闭·待命")

    def _clear_window_gone(self):
        """野狐回来了 → 说一句就继续"""
        if not self._win_gone:
            return
        self._win_gone = None
        log("野狐回来了，继续对局")
        self._emit("on_state", "running", "运行中")

    def _push_opts(self):
        """把规则 / 激进度推给引擎后端（只在**值变了**时推）

        ⚠️ 必须先去重再调 setter（2026-10-23 端到端测出来的 bug）
        ----------------------------------------------------
        set_aggro() 的语义是「清空去重记录并立刻全量下发」——
        它默认调用方是「用户刚改了这一档」。而 _push_opts 在开局和
        每手开头都会被调，如果无脑转发，等于每手都把 7 个参数重发一遍
        （≈0.3s/手），把「尽快落子」直接吃掉。
        这里先比对后端当前的字段，值一样就一个命令都不发。

        ⚠️ 这是**唯一**该调后端 set_aggro/set_rules 的地方（2026-10-23）
        ----------------------------------------------------
        它只会在工作线程里跑（_loop 开头 + 每手 decide 前）。
        GUI 主线程改设置只写 Auto 的字段（Auto.set_aggro / set_rules），
        不直接碰后端 —— 否则两边会抢同一根 GTP 管道的应答。
        """
        eng = self.eng
        try:
            if hasattr(eng, "set_rules"):
                if getattr(eng, "rules", None) != self.rules:
                    eng.set_rules(self.rules)
            if hasattr(eng, "set_aggro"):
                if getattr(eng, "aggro", None) != self.aggro:
                    eng.set_aggro(self.aggro)
            self._opts_dirty = False
        except Exception as e:
            log(f"  规则/激进度下发失败（不影响下棋，用引擎默认）："
                f"{type(e).__name__}: {e}")

    def set_rules(self, rules):
        """对局中改规则 —— **只改字段**，真正的下发给工作线程下一手做

        ⚠️ 为什么不再直接调后端 setter（2026-10-23 改为线程安全）
        ----------------------------------------------------
        老代码这里 `self.eng.set_rules(...)`，后端会**立刻**发一条 GTP
        `kata-set-rules` 并等应答。而 GUI 是在**主线程**调这个方法
        （老板点下拉框 / 拖滑条），此时工作线程正卡在 analyze 里
        用 `_read_reply` 一路消费管道输出 —— 两边同时读同一根管道，
        应答会被对方抢走：轻则 analyze 拿到 `= ` 当成候选行、
        重则直接 TimeoutError 把那一手废掉。

        现在只动 `self.rules`（单个引用赋值，CPython 里原子），
        由 `_push_opts()` 在**工作线程**里于下一手开头下发。
        下一手就生效，用户感知不到延迟。
        """
        self.rules = norm_rules(rules)
        self._opts_dirty = True
        log(f"规则改为 {RULES.get(self.rules or '', self.rules)}（下一手生效）")
        return self.rules

    def set_aggro(self, level):
        """对局中改激进度 —— 同样只改字段，由工作线程下一手下发

        线程安全的理由见 set_rules 的说明（同一根 GTP 管道不能两边抢）。

        ⚠️ 选点层是立刻生效的
        ----------------------------------------------------
        `pick_move(legal, self.aggro, ...)` 每手都读这个字段，
        所以哪怕参数还没下发，选点风格从**这一手**就开始变了；
        引擎侧那 7 个搜索参数在下一手开头由 `_push_opts` 补齐。
        """
        self.aggro = clamp_aggro(level)
        self._opts_dirty = True
        log(f"激进度改为 {self.aggro:+.1f}（{aggro_label(self.aggro)}）"
            f"（下一手生效）")
        return self.aggro

    # ------------------------------------------------ 观测

    def observe(self, force_calib=False, with_mark=True):
        """读一次盘面

        返回 (board, conf, title, last)
        last = ("R12", 0.204, "W") 或 None
            最后一手标记。None 表示没找到（空盘、复盘、或对手刚落子
            还没画上去），此时 side_to_move 会自动退回标题/数子。

        ⚠️ 路数变了必须重标（2026-10-23 查 bug）
        ----------------------------------------------------
        `FB.GEOM` 是模块级缓存，只在 None / 标定失败时才重算。
        但野狐同一窗口可以开 **9/13/19 路**不同对局 —— 老板下完 9 路
        接着开 19 路，屏幕上的棋盘变大了，GEOM 里还是 n=9、step 按 9 路算。
        于是按 9 路的格子去读 19 路的盘：每个采样点都落在格子中间，
        读出来是一堆噪声，表现成「连续读盘失败 / 落子全歪」。

        现在每次读盘前先确认**屏幕上的棋盘路数**是否还等于缓存里的 n，
        不等就重标一次。多花的那点时间只在换局时发生一次。
        """
        t0 = time.time()
        im, title, org = FB.grab()
        # 2026-11-07：留一份最近画面，给「对方停一手」提示条检测用
        self._last_im = im
        need = force_calib or FB.GEOM is None or not FB.GEOM.get("ok")
        recheck = False
        if not need:
            # ⚠️ 缓存有，但先验一下路数是否还匹配（见 docstring 的说明）
            # ----------------------------------------------------
            # 判断必须「失效方向安全」：宁可多跑一次 calibrate（30ms），
            # 也不能因为看不准就放过「路数其实变了」——
            # 那会让整盘都读不出来（采样点全落在格子中间）。
            # 所以只有「有把握 + 与缓存相同」才跳过重标，其余都重标。
            #
            # ⚠️ 但要节流（2026-10-23 想清楚的一个坑）
            # ----------------------------------------------------
            # 棋子密集时 detect 可能**一直**不确定，若每轮都重标，
            # 0.35 秒一轮的对局里有 30ms×N 浪费在标定上，还可能因为
            # 两次标定结果微抖导致落子坐标漂移。
            # 所以：不确定 → 先记一笔，**隔一段时间**才真去重标一次。
            # 而「读不出棋子」才是硬证据（路数不对时读出来几乎全是空），
            # 那个信号走的是下面 read_board 之后的判定，不用等冷却。
            cached_n = FB.GEOM.get("n") or 19
            try:
                now_n, sure = FB.detect_board_n(im)
            except Exception:
                now_n, sure = None, False
            if sure and now_n is not None and now_n != cached_n:
                # 有把握且不一致 → 立刻重标（换局那种场景走这里）
                if self.verbose:
                    log(f"  棋盘路数变了（缓存 {cached_n} 路 → "
                        f"本次 {now_n} 路），重新标定")
                need = True
            elif not sure:
                # 看不准 → 隔 3 秒才重标一次，避免每轮都白算
                if time.time() - getattr(self, "_last_recal", 0) > 3.0:
                    recheck = True
                    if self.verbose:
                        log(f"  棋盘路数看不准（缓存 {cached_n} 路），"
                            f"保守重标一次")
        if need or recheck:
            self._last_recal = time.time()
            g = FB.calibrate(im)
            if not g.get("ok"):
                # ⚠️ 必须在这里停住（2026-10-22）
                # 标定失败时 g 里没有 step/x0/y0，继续往下走就是 KeyError，
                # 而 KeyError 的报错信息对老板毫无意义（只说 'step'）。
                # 真实原因几乎永远是「野狐没停在对局界面」。
                #
                # ⚠️ 但「保守重标」失败时不能抛（2026-10-23）
                # 那条路是「看不准」触发的，看不准很可能是棋子太多而不是
                # 不在棋盘界面。这时应该**沿用原缓存**继续读，
                # 而不是把一次可疑的预检升级成「读盘失败」。
                if recheck and not need:
                    g = None
                else:
                    raise RuntimeError(
                        f"找不到棋盘（{g.get('reason')}）—— "
                        f"确认野狐停在【对局棋盘】界面，大厅/复盘列表里没有棋盘")
            if g:
                FB.GEOM = g
                if self.verbose:
                    log(f"几何标定：x0={g['x0']:.1f} y0={g['y0']:.1f} "
                        f"step={g['step']:.3f} ({g['method']}, "
                        f"{g.get('n')} 路 / 线={g['nlines']})")

        if with_mark:
            # ⚠️ 标记读失败绝不能连带把整次读盘搞挂 —— 它只是「加分项」，
            # 拿不到就退回标题判轮次，对局照跑。
            try:
                b, conf, last = FB.read_board_full(im, geom=FB.GEOM)
            except Exception as e:
                if self.verbose:
                    log(f"  标记读取失败（不影响对局）：{e}")
                b, conf = FB.read_board(im, geom=FB.GEOM)
                last = None
        else:
            b, conf = FB.read_board(im, geom=FB.GEOM)
            last = None

        if last:
            self.stats["marks"] += 1
        self.stats["reads"] += 1
        self.stats["t_read"] += time.time() - t0
        self._last_mark = last
        return b, conf, title, last

    # ------------------------------------------------ 轮次

    def _turn_settled(self, board, title, last, wait=1.2):
        """落子前把轮次确认到「标题和标记说法一致」

        ⚠️ 2026-11-01 现场（老板实测报错）
        ----------------------------------------------------
        标题「第N手」比盘面慢半拍（野狐落子后 0.3~1s 才改数字），
        而最后一手的反色三角是**立刻**画上去的。
        老逻辑两者打架时一律信标题 → 程序以为轮到自己，点下去
        野狐那边「不是你的回合」直接无视，日志表现就是：

            黑3/白3 该白走            ← 6 子却轮到白？矛盾
            落子 D15 2.5s 内未见生效   ← 野狐压根不认这一手

        连续几次之后收工，看着就像「程序不会落子」。

        现在：打架就**等标题追**（只读窗口标题，不截图，很便宜），
        最多等 wait 秒；真追不上才退回标记 —— 盘面是实时的，
        标记跟着盘面走，比滞后的标题可信。

        返回 (turn, 说明)。说明为空 = 两边本来就一致。
        """
        t_mark = turn_by_mark(last)
        t_title = turn_by_title(title)
        if not t_mark or not t_title or t_mark == t_title:
            return side_to_move(board, title, last), ""
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(0.12)
            try:
                hits = FB.find_hwnd()
                if not hits:
                    break
                t_title = turn_by_title(hits[0][1])
            except Exception:
                break
            if t_title == t_mark:
                return t_mark, "标题刚追上标记"
            if t_title is None:
                break
        return t_mark, "标题和标记打架，按标记（盘面是实时的）"

    # ------------------------------------------------ 对方停一手

    def _pass_toast(self):
        """棋盘中央是否显示「对方停一手」提示条。

        ⚠️ 2026-11-07 实战踩到的坑
        ----------------------------------------------------
        野狐里对方可以「停一手」（pass）。pass **不会**在盘面上添子，
        也不会改标题的「第N手」。而本程序判断轮次靠的是
        「盘面多了对方一颗子」/「最后一手标记」——pass 时两者都不变，
        于是它一直以为还是对方回合，等到读秒耗尽，看着就像「不会下棋」。

        野狐的 pass 会在棋盘中央上方弹一条深色圆角提示 `对方停一手`。
        这条提示条的像素特征和棋盘、棋子都不一样：
          - 亮度明显低（半透明黑底）：均值 ~130，棋盘 ~200
          - 方差小（纯色条 + 少量白字）：std ~29，有子的地方 std 60+
          - 暗像素占比高：<160 的像素约 88%
        两个真实截图标定出来的判据：暗像素占比 > 0.65 且 std < 50。

        误判风险：中央恰好堆了一团黑子时 std 会很高，过不了；空盘更过不了。
        返回 True 只代表「屏幕上像是有这条提示」，是否真 pass 由落子结果兜底
        （不是我方回合时野狐会拒绝这一手，不会真的下错）。
        """
        im = getattr(self, "_last_im", None)
        g = FB.GEOM or {}
        if im is None or not g.get("ok"):
            return False
        try:
            n = g.get("n") or 19
            x0, y0, step = g["x0"], g["y0"], g["step"]
            cx = x0 + (n - 1) / 2.0 * step
            # 提示条画在棋盘中心**上方约 0.7 格**（实测 1356×848：棋盘中心 417，
            # 提示条中心 390，格子 36.7）。取 0.7 格、上下各 0.5 格，整条都框住。
            cy = y0 + (n - 1) / 2.0 * step - step * 0.7
            half_w = max(12, int(step * 2.1))
            half_h = max(5, int(step * 0.50))
            a = np.asarray(im.convert("L"), dtype=np.uint8)
            H, W = a.shape
            x1, x2 = max(0, int(cx - half_w)), min(W, int(cx + half_w))
            y1, y2 = max(0, int(cy - half_h)), min(H, int(cy + half_h))
            crop = a[y1:y2, x1:x2]
            if crop.size < 100:
                return False
            dark = float((crop < 160).mean())
            return dark > 0.65 and float(crop.std()) < 50.0
        except Exception:
            return False

    # ------------------------------------------------ 决策

    def decide(self, board, turn, budget=None, visits=None,
               stop_at_visits=None):
        """出主意。budget=这一手的思考时间（秒），None 则留给引擎默认

        ⚠️ 选点分两层（2026-10-23 加激进度后）
        ----------------------------------------------------
        第一层 引擎：按激进度调搜索参数，让候选池更丰富/更收敛
        第二层 选点：pick_move() 在「等价着法」里按风格挑
                     标准档下第二层是原样返回第一名，行为跟以前完全一致。
        """
        t0 = time.time()
        r = self.eng.analyze(board, turn, komi=self.komi, budget=budget,
                             visits=visits, stop_at_visits=stop_at_visits)
        self.stats["t_think"] += time.time() - t0
        if not r.get("ok") or not r.get("moves"):
            return None, r
        # 过滤：已经有点、以及非法坐标
        # 路数按盘面实际尺寸算（2~19 路都支持）
        bh = len(board)
        bw = len(board[0]) if bh else 0
        legal = []
        for d in r["moves"]:
            mv = d.get("move")
            xy = FB.from_gtp(mv, bh) if mv else None
            if xy is None:
                continue
            x, y = xy
            if not (0 <= x < bw and 0 <= y < bh):
                continue
            if board[y][x] != 0:            # 引擎给的点已经有子 → 跳过
                continue
            legal.append(d)
        if not legal:
            return None, r
        best = pick_move(legal, self.aggro, turn=turn) or legal[0]
        return best, dict(r, legal=legal)

    # ------------------------------------------------ 落子

    def place(self, gtp, before):
        """落一手并严格复查

        ⚠️ 复查必须严格，不能「盘面变了就算成功」（2026-10-22 补）
        ----------------------------------------------------
        老逻辑：盘面变了 + 我的点没子 → 也算成功。
        问题：对手的手和我的点击可能撞在同一秒。对手落完后盘面变了，
        我这边复查「变了」就直接判成功，于是**下一手会按错误轮次去算**，
        而且我这一手其实根本没下出去，白丢一手。
        对局里丢一手就是直接崩盘。

        现在只认一种成功：**我要下的那个点，现在有我的子**。
        其余一律当失败，交给调用方换次优点或停手。
        """
        if self._stop:
            return False, "stopped"
        ok, det = FB.play_move(gtp, confirm=False, wait=0.30)
        self.stats["clicks"] += 1
        # 记下这一手实际点到的屏幕坐标（失败时打出来，排查「点歪了」用）
        self._last_click = (det.get("screen")
                            if isinstance(det, dict) else None)
        if isinstance(det, dict) and det.get("activated") is False:
            # 抬不到前台时已经用「点标题栏」叫醒了；如果还频繁出现，
            # 说明前台锁很凶，留一行日志便于对照现场。
            self._log_once("no-fg",
                           "  提示：点击时野狐不在前台，已用标题栏唤醒后再点", 180)
        if not ok:
            # ⚠️ 「野狐最小化/关了」不是落子失败，不能计入 fail_streak
            # （2026-10-23 老板要求：程序不许霸着野狐）
            # 老板去查个东西把窗口收起来，回来发现程序已因「连续 8 手失败」
            # 自己收工了 —— 这种体验是致命的。所以单列一种状态。
            if isinstance(det, dict) and det.get("blocked"):
                # 落点被别的窗口压住了 —— 不是野狐的问题，也不是这一手的问题
                self._log_once("blocked",
                               f"  落点被挡住：{det.get('blocker')}"
                               f" —— 把它挪开/最小化，这一手先不下")
                return False, "blocked"
            if isinstance(det, dict) and det.get("paused"):
                return False, "paused"
            log(f"  点击被拒：{det}")
            self.stats["fail"] += 1
            return False, "fail"

        want = 1 if self.my == "B" else 2
        # 路数从基准盘面取（2~19 路都支持）
        bh = len(before) if before else 19
        xy = FB.from_gtp(gtp, bh)
        if xy is None:
            return False, "fail"
        t0 = time.time()
        while time.time() - t0 < 2.5:
            if self._stop:
                # 点了「关闭引擎」就立刻收手，别等这 2.5 秒复查走完
                return False, "stopped"
            time.sleep(0.12)
            try:
                # 复查不看标记：这里只关心「我的点有没有子」，
                # 每轮额外算 361 个圆纯属浪费。
                b2, _, title2, _ = self.observe(with_mark=False)
            except FB.WindowGone:
                # 点击已经点出去了，复查时野狐被收起来了 ——
                # 无法确认但也无从补救，当成功（下一圈读盘会重新判断轮次）
                return True, "ok"
            except Exception:
                continue
            if b2[xy[1]][xy[0]] == want:
                return True, "ok"
            # 对手抢先落子（我的点仍空）→ 只有当「我要的点被别人占了」
            # 才算这一手作废；盘面其它地方变了不影响判定
            if b2[xy[1]][xy[0]] != 0:
                log(f"  {gtp} 已被对手占据")
                self.stats["fail"] += 1
                return False, "fail"
            k = board_key(b2)
            if k != board_key(before):
                # 盘面变了但我的点还是空 → 对手刚下完，我这手没生效
                # ⚠️ 2026-11-01：必须报出**新出现的子在哪**，否则「没生效」
                #    这句话等于没说 —— 点歪一格和压根没点出去，现象一模一样。
                new_stones = _new_stones(before, b2)
                extra = ("；新出现：" + "、".join(f"{m}{c}" for m, c in new_stones)
                         if new_stones else "；盘面上没有新子")
                log(f"  盘面被对手改了，我 {gtp} 这手未生效{extra}"
                    f"（点击坐标 {self._last_click}）")
                self.stats["fail"] += 1
                return False, "fail"
        new_stones = _new_stones(before, b2)
        extra = ("；新出现：" + "、".join(f"{m}{c}" for m, c in new_stones)
                 if new_stones else "；盘面上没有新子")
        log(f"  落子 {gtp} 2.5s 内未见生效{extra}"
            f"（点击坐标 {self._last_click}）")
        if not new_stones:
            # 盘面一个子都没多 → 野狐**没接受这一手**（不是点歪）
            # 最常见两种：① 还没轮到你 ② 这手违规（打劫/自杀）
            # 这条提示一分钟内只说一次（老板要求别刷屏）
            self._log_once("rejected",
                           "    提示：野狐没接受这一手 —— 多半是轮次不对"
                           "（「我执」选反了，或野狐标题/标记还没刷新）")
        # 现场存证：截图 + 标定网格 + 点击点（最多 3 张）
        self._dump_fail(gtp)
        self.stats["fail"] += 1
        return False, "fail"

    def _dump_fail(self, gtp):
        """落子失败时把现场存下来，给下次排查用（最多 3 张）

        为什么值得存（2026-11-02 现场）：
        日志只能说「点了没生效」，但分不清是①点歪了 ②窗口没激活
        ③根本不是我的回合 ④画面被别的窗口盖住了。把失败那一刻的
        截图 + 我们以为的网格 + 实际点击点画在一起，一眼就能对出来。
        """
        if getattr(self, "_fail_dumps", 0) >= 3:
            return
        try:
            RP.ensure_dirs()
            im, _title, org = FB.grab()
        except Exception as e:
            log(f"  （失败现场截不到图：{e}）")
            return
        try:
            from PIL import ImageDraw
            d = ImageDraw.Draw(im)
            g = FB.GEOM or {}
            if g.get("ok"):
                step = float(g["step"])
                n = int(g.get("n") or 19)
                x0, y0 = float(g["x0"]), float(g["y0"])
                half = step * 0.5
                left, top = x0 - half, y0 - half
                right = x0 + step * (n - 1) + half
                bottom = y0 + step * (n - 1) + half
                for i in range(n):
                    x = x0 + step * i
                    y = y0 + step * i
                    d.line([(left, y), (right, y)], fill=(0, 170, 255), width=1)
                    d.line([(x, top), (x, bottom)], fill=(0, 170, 255), width=1)
            if self._last_click:
                sx, sy = self._last_click
                x, y = sx - org[0], sy - org[1]
                d.line([(x - 20, y), (x + 20, y)], fill=(255, 0, 0), width=3)
                d.line([(x, y - 20), (x, y + 20)], fill=(255, 0, 0), width=3)
            p = os.path.join(RP.LOG_DIR,
                             f"fail-{datetime.now():%H%M%S}-{gtp}.png")
            im.save(p)
            self._fail_dumps = getattr(self, "_fail_dumps", 0) + 1
            log(f"  （失败现场已存：{p}）")
        except Exception as e:
            log(f"  （失败现场标注失败：{type(e).__name__}: {e}）")

    # ------------------------------------------------ 主循环

    def run(self):
        """外壳：不管 _loop 从哪个 return 退出，都保证收尾通知发出
        （GUI 靠 on_state('done') 恢复按钮，漏一次界面就卡在「运行中」）
        """
        try:
            self._loop()
        except Exception as e:
            log(f"对局异常终止：{type(e).__name__}: {e}")
            self._emit("on_state", "error", f"{type(e).__name__}: {e}")
        finally:
            self.summary()
            self._emit("on_state", "done", "")

    def _loop(self):
        log("=" * 62)
        log(f"引擎：{self.eng.info_text}")
        # 规则 / 激进度：开局把最终值推给引擎（界面可能改过造引擎之后的设置）
        self._push_opts()
        log(f"我执{self.my}　komi={self.komi:g}　"
            f"规则={RULES.get(self.rules or '', self.rules or '引擎默认')}　"
            f"激进度={self.aggro:+.1f}（{aggro_label(self.aggro)}）　"
            f"{'不限手数' if not self.max_moves else f'最多 {self.max_moves} 手'}")

        # ⚠️ 启动后先干等（老板要 3 秒）
        # 目的是给人关掉别的弹窗、切回野狐的时间。这期间**不读盘、不落子**，
        # 免得读到一半界面还在动。引擎早就绪了，只是不打子。
        if self.warmup and self.warmup > 0:
            log(f"引擎已就绪，{self.warmup:g} 秒后开始下"
                f"（这期间可以关掉其它弹窗）…")
            for left in range(int(self.warmup), 0, -1):
                if self._stop:
                    log("收到停止信号，收工")
                    return
                self._emit("on_state", "warmup", f"{left} 秒后开始")
                log(f"  {left}…")
                if not self._sleep_unless_stopped(1.0):
                    log("收到停止信号，收工")
                    return
            self._emit("on_state", "running", "运行中")
            log("开始。")
        log("=" * 62)

        idle = 0
        n_my = 0
        last_key = None
        last_played = None      # 我上一手落下去的点（用来判断自己的手有没有生效）
        fail_streak = 0         # 连续落子失败次数
        same_hits = 0           # 盘面连续没变化的次数
        # ⚠️ max_moves=0 表示不限（老板 2026-10-23 要求删掉这个限制）
        # 老代码 `while n_my < self.max_moves` 在 max_moves=0 时直接不进循环
        # → 启动即收工，一手都不下。这就是老板报的「大问题」的头号成因。
        while (not self.max_moves) or (n_my < self.max_moves):
            if self._stop:
                log("收到停止信号，收工")
                break
            if not self._wait_if_paused():      # 暂停：原地待命，引擎不关
                log("收到停止信号，收工")
                break
            try:
                board, conf, title, last = self.observe()
            except FB.WindowGone as e:
                # ⚠️ 野狐被最小化/关掉了 —— 这是**用户意图，不是故障**
                # （老板 2026-10-23 明确要求：不许程序霸着野狐不让关）
                # 老代码这里会报「读盘失败」并重试 30 次然后收工，
                # 老板一最小化野狐程序就自己退了 —— 完全不是他要的。
                # 现在：安静待命，引擎不关，等他把窗口弄回来就自动继续。
                self._note_window_gone(e)
                time.sleep(0.8)
                continue
            except Exception as e:
                log(f"读盘失败：{e}")
                time.sleep(1.0)
                idle += 1
                # 前 3 次就明确报「大概率是没进对局界面」——
                # 标定失败几乎总是这个原因（窗口在，但停在大厅/复盘列表）。
                # 不提示的话会安静空转 30 次、26 秒才收工，像卡死。
                if idle in (3, 8):
                    log("  提示：多半是野狐没开在对局界面。"
                        "大厅/复盘列表里没有棋盘，标定必失败。")
                if idle > 30:
                    log("连续读盘失败，收工")
                    self._emit("on_state", "error",
                               "连续 30 次读不到棋盘 —— 确认野狐停在棋盘界面")
                    return
                continue
            self._clear_window_gone()

            turn, turn_note = self._turn_settled(board, title, last)
            if turn_note and self.verbose:
                log(f"  轮次：{turn_note}")
            nb, nw = _count(board)
            self._emit("on_board", board, title, turn, (nb, nw), last)

            # ⚠️ 对局已经结束就立刻收工（2026-10-23 老板踩到）
            # ----------------------------------------------------
            # 现场：我执黑，盘面 黑17/白16 该白走，标题
            #   「> [高级房1] > 22|124号房间 对局结束 (白 掉线负) - 友谊赛」
            # 对手已经掉线、棋局早结束了，但标题里的「该白走」还留在屏幕上。
            # 老逻辑只看轮次 → turn(W) != my(B) → 一路「等对手」空转到
            # 210 秒才收工，界面上只零星几行，看着就是「不下棋」。
            # 必须按标题里的结束语直接判定收工，不能等轮次自己变。
            if _game_over(title):
                log(f"对局已结束（{title[:40]}），收工")
                self._emit("on_state", "done", "")
                return

            pass_detected = False
            if turn != self.my:
                idle += 1
                # ⚠️ 2026-11-07：先看是不是「对方停一手」
                # pass 不会在盘面添子，光靠「最后一手/子数」永远等不到轮次变化。
                # 看到中央的提示条就当成对方已经过了一手，直接走我方落子流程。
                # 万一不是（误判），野狐会拒绝这一手，不会真下错。
                if idle >= 2 and self._pass_toast():
                    log("  检测到「对方停一手」→ 按对方过一手处理，"
                        "继续走我方落子")
                    pass_detected = True
                    turn = self.my
                    idle = 0
                if not pass_detected:
                    # ⚠️ 等对手是**常态**，不该刷日志（老板 2026-10-23 要求）
                    # ----------------------------------------------------
                    # 老逻辑 `idle % 4 == 1` → 每 1.4 秒一行「等对手」，
                    # 一盘棋几百行全是这句，把真正重要的信息（落子、异常）淹了。
                    # 现在只在**三个时刻**说一句：
                    #   ① 刚开始等（idle == 1）—— 让老板知道程序看懂了轮次
                    #   ② 等了 15 秒 —— 对手在长考，报一次
                    #   ③ 颜色疑似选反（idle 到 40 / 120）—— 这是真异常
                    # 其余时候安静待着，界面上状态灯一直是绿的。
                    if self.verbose and idle == 1:
                        src = f"最后一手 {last[0]}" if last else "标题"
                        log(f"  等对手下 …（黑{nb}/白{nw}，依{src}判断轮到 "
                            f"{'黑' if turn == 'B' else '白'}）")
                    elif self.verbose and idle == 45:
                        log(f"  对手已思考约 16 秒 …（黑{nb}/白{nw}）")
                    # 长时间一直是对方走 → 十有八九「我执」选反了。
                    # 现场这个坑很隐蔽：颜色选错时程序行为完全正确（它确实不该
                    # 在对方回合落子），只是永远等不到，看起来就是「不下棋」。
                    if idle in (120, 360):
                        log(f"  ⚠ 已连续 {idle * 0.35:.0f} 秒读到该"
                            f"{('黑' if turn == 'B' else '白')}走，"
                            f"但我设的是执{('黑' if self.my == 'B' else '白')}。"
                            f"颜色选错的话请停下、改「我执」再开始。")
                    time.sleep(0.35)
                    if idle > 600:
                        log("等太久，收工")
                        self._emit("on_state", "error",
                                   "连续等了 210 秒都没轮到我走 —— "
                                   "检查「我执」颜色是否选反，或对局是否已结束")
                        return
                    continue
            idle = 0

            if pass_detected:
                # 盘面没变（对方没落子），但轮次已经是我方：
                # 把 last_key 清掉，避免下面「同一盘面别重复算」把它挡住。
                last_key = None

            # 同一盘面别重复算（上次点击没生效时会走到这里）
            #
            # ⚠️ 这里原来有一条 `if self.dry: return`，是「启动即结束」的
            # 第二个成因：演练模式不落子 → 盘面永远不变 → 第二圈就命中
            # 这个分支直接 return。演练模式已按老板要求整体删除。
            # 现在盘面没变只有两种可能：① 我上一手没点出去 ② 读盘重复，
            # 两种都只该「稍等再读」，绝不能收工。
            key = board_key(board)
            if key == last_key:
                same_hits += 1
                # 只在「确实该说」的时候说（老板 2026-10-23：别刷屏）
                # 正常读盘重复是常态（对手还没落子），不该每次都说。
                if same_hits in (10, 40):
                    log(f"  盘面 {same_hits} 次没变化 —— 上一手可能没点出去，"
                        f"继续观察（不会自动收工）")
                time.sleep(0.6)
                continue
            same_hits = 0
            last_key = key

            n_my += 1
            # ⚠️ 规则 / 激进度：每手开头对齐一次后端（2026-10-23）
            # ----------------------------------------------------
            # 老板要求「留下来的选项都是能调的」，而这两项现在是由
            # GUI 主线程改字段、工作线程下发（见 set_aggro 的说明）。
            # 放这里而不是 decide 里：decide 会被重试路径调多次，
            # 每手开头只走一遍，语义清楚。
            # _push_opts 内部比对后端字段，值没变就一条命令都不发，
            # 稳态零开销（不影响「尽快落子」）。
            self._push_opts()
            # 这一手要想多久 —— 固定值或随机区间（老板要的「思考时间随机」）
            #
            # ⚠️ 必须**一次性**取四个字段的快照（2026-10-23 查 bug）
            # ----------------------------------------------------
            # 老板能在对局进行中改思考时间（set_think 是跨线程写的）。
            # 老代码这里分三步读：
            #     think = self._think_secs()          ← 持锁读了 mode+lo/hi
            #     if self.think_mode == "random":     ← 又一次不加锁读 mode
            #     f"...{self.think_min:g}~{self.think_max:g}"  ← 再读两次
            # 中间只要 GUI 改了一次设置，日志就会出现
            #     「随机目标 1.0s ∈ 5~5」            （模式已切、区间没跟上）
            # 这种自相矛盾的行，老板看了会以为程序算错了。
            # 现在用 _think_snapshot() 一次拿全，判断和显示都用同一份。
            think, t_mode, t_lo, t_hi, secs = self._think_snapshot()
            if t_mode == "random":
                log(f"[第 {n_my} 手] 黑{nb}/白{nw} 该"
                    f"{'黑' if self.my == 'B' else '白'}走　"
                    f"（随机目标 {secs:.2f}s ∈ {t_lo:g}~{t_hi:g}s）")
            elif t_mode == "visits":
                log(f"[第 {n_my} 手] 黑{nb}/白{nw} 该"
                    f"{'黑' if self.my == 'B' else '白'}走　"
                    f"（按计算量 {self.visits}v）")
            else:
                log(f"[第 {n_my} 手] 黑{nb}/白{nw} 该"
                    f"{'黑' if self.my == 'B' else '白'}走　"
                    f"（固定 {secs:g}s）")
            t0 = time.time()
            # ⚠️ budget 用 secs（这一手的目标时长），不是 think（2026-10-23）
            # ----------------------------------------------------
            # 随机模式下 think 已经被 set_think() 同步成**区间上限**了。
            # 如果这里传 think，那每一手都按上限算 —— 抽到 0.5s 也跑 10s，
            # 「随机」就名存实亡。必须传这一手真正抽中的 secs。
            #
            # ⚠️ 不能写 `budget=secs or None`：随机区间 0~10 时可能真抽到
            # 0 秒，`0 or None` → None → 后端退回默认 budget（8 秒），
            # 于是「抽到 0 秒」反而变成最慢的一手。
            # 显式给 0.5s 地板：够引擎吐一帧候选，也不会卡住。
            if t_mode == "visits":
                # 按计算量：访问量到了就收（stop_at_visits 在分析循环里截断），
                # 300 秒只是防止卡死的硬保险；不做时间补齐
                best, ana = self.decide(board, turn, budget=300.0,
                                        visits=self.visits,
                                        stop_at_visits=self.visits)
                padded = 0.0
            else:
                best, ana = self.decide(board, turn,
                                        budget=(secs if secs and secs > 0
                                                else 1.0))
                padded = self._pad_think(t0, secs)
            if not best:
                log(f"  没得到可下点：{(ana or {}).get('error')}")
                time.sleep(1.0)
                n_my -= 1
                continue

            # ⚠️ 补齐到设定的思考时长（2026-10-23 老板报的「固定时间不固定」）
            # secs 只是引擎的**预算上限**，引擎提前收敛就立刻返回。不补齐的话
            # 固定 5 秒会变成 0.2~5 秒乱跳，看着就是随机落子。
            if padded > 0.05 and self.verbose:
                log(f"  （引擎 {(ana or {}).get('elapsed')}s 就收工了，"
                    f"补足到 {secs:.1f}s 再落　+{padded:.1f}s）")

            # ⚠️ 把「首选点 / 计算量 / 胜率 / 目差」发给界面（老板 2026-10-23）
            # 这是左侧盘面信息栏唯一要显示的四样东西。
            self.last_ana = ana or {}
            self._emit_info(best, ana, turn)

            wr, sl = best.get("winrate"), best.get("scoreLead")
            log(f"  → {best['move']}　{self._wr_text(wr)}　{self._lead_text(sl)}　"
                f"({(ana or {}).get('elapsed')}s / {(ana or {}).get('visits')}v / "
                f"决策 {time.time()-t0:.1f}s)")

            ok, why = _place_ret(self.place(best["move"], board))
            if ok:
                fail_streak = 0
            elif why == "stopped":
                log("收到停止信号，收工")
                break
            elif why == "blocked":
                # 落点被别的窗口压着 —— 不进失败计数，安静等一会儿
                n_my -= 1
                last_key = None
                time.sleep(0.8)
                continue
            elif why == "paused":
                # 野狐被收起来了，这一手没下 —— 不算失败，退回去等
                self._note_window_gone(FB.WindowGone("野狐不可见", minimized=True))
                n_my -= 1
                last_key = None
                time.sleep(0.8)
                continue
            else:
                # 换次优。注意每换一次都要重读盘面当基准 ——
                # 上一次点击可能已经动过盘（或对手动了），用旧 board 判定会错。
                alts = [d for d in (ana or {}).get("legal", [])[1:4]]
                done = False
                for d in alts:
                    if self._stop:
                        break
                    log(f"  改下 {d['move']}")
                    try:
                        cur, _, _, _ = self.observe(with_mark=False)
                    except Exception:
                        cur = board
                    if _place_ret(self.place(d["move"], cur))[0]:
                        best = d
                        done = True
                        break
                    time.sleep(0.25)
                if not done:
                    if self._stop:
                        log("收到停止信号，收工")
                        break
                    # ⚠️ 这里原来直接 `return`，等于「点歪一次就整盘收工」。
                    # 实战里点击被弹窗吃掉、窗口失焦、野狐短暂卡顿都会
                    # 造成一次失败，直接收工太脆。改成退一步重来。
                    fail_streak += 1
                    n_my -= 1
                    wait = min(5.0, 0.8 * fail_streak)
                    if fail_streak in (1, 2, 4, 8):
                        log(f"  这一手没点出去（连续第 {fail_streak} 次），"
                            f"{wait:.1f}s 后重读盘面再来")
                    if fail_streak == 3:
                        # 这句要能在悬浮球上看到（只有两行）：
                        # 连续点不落子时，最常见的就是「我执」选反了
                        log("  ⚠ 连点 3 手都没落子 —— 先确认「我执」这盘选对没有"
                            "（黑/白）")
                    if fail_streak >= 8:
                        log("连续 8 手都落不出去，收工（先确认野狐窗口没被挡住、"
                            "没弹窗、鼠标没被占用）")
                        self._emit("on_state", "error",
                                   "连续 8 手落子失败 —— 检查野狐窗口是否被遮挡/有弹窗")
                        return
                    time.sleep(wait)
                    last_key = None     # 强制重算，别被「盘面没变」挡住
                    continue

            self.history.append((self.my, best["move"], wr, sl,
                                 (ana or {}).get("elapsed")))
            # ⚠️ 2026-11-02 老板反馈：悬浮球日志「一会我方胜率、一会对方胜率」。
            # 根因：on_move 之前直接把**白方视角**的 wr/sl 发出去，而 GUI
            # 那边按「我方视角」直接 ×100 显示 —— 执黑时正好翻反。
            # 这里和 _emit_info 统一口径：给界面的一律是我方视角；
            # history 仍存白方视角（_wr_text/_lead_text 会自己翻）。
            wr_my = None if wr is None else (wr if self.my == "W" else 1.0 - wr)
            sl_my = None if sl is None else (sl if self.my == "W" else -sl)
            self._emit("on_move", self.my, best["move"], wr_my, sl_my)
            # ⚠️ 落子后**不再等**（老板 2026-10-23：想完就立刻落子）
            # 老版本这里有「落子间隔」干等，等于算完再发呆十几秒。
            # 现在时间全部花在「想」上，想完立刻点，点完直接进下一圈读盘。

    # ------------------------------------------------ 界面信息上报

    def _emit_info(self, best, ana, turn):
        """把「首选点 / 计算量 / 胜率 / 目差」算成界面能直接显示的四项

        老板 2026-10-23 的要求：左侧盘面信息栏里
        「把该谁走和末手和黑1/白1都去掉，换成显示计算量，胜率，目差」
        另外「把当前 katago 计算的首选点显示在左边的盘面上」。

        口径统一成**我方视角**（跟日志里的胜率/目差一致）：
          · 首选点 = 引擎 order 第一的那个点（不是 pick_move 挑出来的）
            —— 老板要的是「KataGo 算的首选」，不是「我实际下的」。
            两者在标准档下相同，激进档可能不同，所以分开报。
          · 计算量 = 引擎实际跑了多少访问（visits）+ 花了多少秒
          · 胜率 / 目差 = 首选点的值，翻成我方视角
        """
        if not ana:
            return
        moves = ana.get("moves") or []
        top = moves[0] if moves else None
        gtp = top.get("move") if top else None
        vis = ana.get("visits")
        el = ana.get("elapsed")
        wr = top.get("winrate") if top else None
        sl = top.get("scoreLead") if top else None
        # 视角：数据层是白方视角，我执黑要翻
        if wr is not None:
            wr = wr if self.my == "W" else 1.0 - wr
        if sl is not None:
            sl = sl if self.my == "W" else -sl
        self._emit("on_info", gtp, vis, el, wr, sl)

    # ------------------------------------------------ 胜率/目差显示

    def _is_mine(self, color):
        """color 是否为**我方**颜色。None 当作我方（引擎给的推荐本身就是我的着）"""
        return color is None or str(color).upper() == self.my

    def _wr_text(self, wr, color=None):
        """胜率文案：默认按**我方**视角显示（老板要求）

        数据层统一是**白方视角**（引擎层约定），这里按 my_color 翻转：
          我执白 → 直接用；我执黑 → 1 - x。
        color 参数给了就按那个颜色算（用于对手手数展示，本期不用）。
        """
        if wr is None:
            return "胜率 —"
        c = self.my if color is None else str(color).upper()
        x = wr if c == "W" else 1.0 - wr
        return f"胜率 {x*100:.1f}%"

    def _lead_text(self, sl, color=None):
        """目差文案：同样是**我方**视角（正数=我方领先）"""
        if sl is None:
            return "目差 —"
        c = self.my if color is None else str(color).upper()
        x = sl if c == "W" else -sl
        return f"目差 {x:+.1f}"

    def summary(self):
        s = self.stats
        log("=" * 62)
        log(f"共下 {len(self.history)} 手　读盘 {s['reads']} 次"
            f"（均 {s['t_read']/max(1,s['reads']):.2f}s）　"
            f"点击 {s['clicks']} 次　失败 {s['fail']}")
        log(f"最后一手标记命中 {s['marks']}/{s['reads']} 次"
            f"（命中率 {s['marks']/max(1,s['reads'])*100:.0f}%，"
            f"未命中时退回标题/数子判轮次）")
        if self.history:
            log(f"思考均时 {s['t_think']/max(1,len(self.history)):.1f}s/手")
            wr = [h[2] for h in self.history if h[2] is not None]
            if wr:
                log(self._wr_text(wr[-1]) + "（我方视角，末手）")
        for i, (c, m, w, l, el) in enumerate(self.history, 1):
            log(f"  {i:>2}. {c} {m:>4}  "
                f"{self._wr_text(w)}  {self._lead_text(l)}  {el}s")


# ================================================================ CLI

def main():
    ap = argparse.ArgumentParser(
        description="野狐自动对弈一体化脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python fox_auto.py                     默认云端 B11-FDX，自动落子
  python fox_auto.py --engine local      换本机引擎（快但弱约 490 Elo）
  python fox_auto.py --color B           我执黑
  python fox_auto.py --rules japanese    按日本数目规则（野狐日韩房用）
  python fox_auto.py --aggro 2.3         激进度 +2.3（支持 0.1 步长）
  python fox_auto.py --komi 6.5          贴目改 6.5（0.5 步长，可设 ±150）
  python fox_auto.py --random-think 1-17 每手在 1~17 秒里随机思考
""")
    ap.add_argument("--engine", default=None,
                    help="引擎：序号 / key / cloud / local。"
                         "不带参数时看 python engines.py 的列表")
    ap.add_argument("--cloud-mode", default=None, choices=["auto", "direct", "service"],
                    help="云引擎接法：direct=进程内直连（默认，最稳）；"
                         "service=走 8765 常驻服务；auto=端口开着就用服务")
    ap.add_argument("--kind", default=None, help="云算力档位 vip-share/share/1x/…")
    ap.add_argument("--weight", default=None,
                    help="云默认 fdx；本机 b28/b40/b18/b20")
    ap.add_argument("--color", default="W", choices=["W", "B"], help="我执什么")
    ap.add_argument("--rules", default="chinese",
                    choices=list(RULES_ORDER),
                    help="规则：chinese=中国数子（默认）/ japanese=日本数目 / "
                         "korean=韩国数目")
    ap.add_argument("--aggro", type=float, default=0.0, metavar="-3~3",
                    help="激进度：-3 极保守 … 0 引擎默认 … +3 极激进（0.1 步长）")
    ap.add_argument("--visits", type=int, default=1200)
    ap.add_argument("--budget", type=float, default=8.0,
                    help="引擎兜底预算秒（只在没给 --think / --random-think "
                         "时生效，正常用不着）")
    ap.add_argument("--komi", type=float, default=7.5,
                    help="贴目，实测可设 ±150")
    ap.add_argument("--think", type=float, default=0.0,
                    help="每手固定思考秒数（想完立刻落子，不再额外等待）")
    ap.add_argument("--random-think", default=None, metavar="MIN-MAX",
                    help="每手随机思考区间，如 1-17（每手在里随机挑一个）")
    ap.add_argument("--warmup", type=float, default=3.0,
                    help="开局前干等几秒（默认 3，给你关弹窗的时间）")
    # --dry 已删（老板 2026-10-23：「你把演练模式给取消掉」）
    ap.add_argument("--moves", type=int, default=0,
                    help="最多下几手；0（默认）= 不限，一直下到对局结束")
    ap.add_argument("--recalib", action="store_true", help="强制重新标定几何")
    ap.add_argument("--show", action="store_true", help="开局打印一次盘面")
    ap.add_argument("--pick", action="store_true",
                    help="交互式选引擎（列表同 Lizzie 的「选择要加载的引擎」）")
    ap.add_argument("--list", action="store_true", help="列出所有可用引擎后退出")
    args = ap.parse_args()

    RP.ensure_dirs()

    if args.list:
        import engines as REG
        txt, reg = REG.table()
        print(f"可用引擎 {len(reg)} 个（* = 默认）\n")
        print(txt)
        print("\n用法：python fox_auto.py --engine 1        （按序号）")
        print("      python fox_auto.py --engine local:b28")
        print("      python fox_auto.py --pick            （交互式选）")
        return 0

    if args.pick:
        spec = pick_engine_interactive()
        if spec is None:
            log("已取消")
            return 1
        args.engine = spec["key"]
        log(f"已选：{spec['name']}　（{spec['key']}）")

    # 野狐窗口在不在
    try:
        hits = FB.find_hwnd()
        if not hits:
            log("没找到野狐窗口 —— 先把野狐客户端开起来")
            return 2
        log(f"野狐窗口：{hits[0][1][:40]}  {hits[0][4]}x{hits[0][5]}")
    except Exception as e:
        log(f"窗口检测失败：{e}")
        return 2

    eng = make_engine(args)
    try:
        if args.show:
            im, t, _ = FB.grab()
            g = FB.calibrate(im)
            log(f"标定：{g}")
            if g.get("ok"):
                FB.GEOM = g
                b, _ = FB.read_board(im, geom=g)
                FB.show(b)
        # 思考时间：--random-think 优先，否则用 --think
        mode, tmin, tmax = "fixed", 1.0, 10.0
        think = args.think
        if args.random_think:
            try:
                lo, hi = args.random_think.replace("~", "-").split("-", 1)
                tmin, tmax = float(lo), float(hi)
                mode = "random"
            except Exception:
                log(f"--random-think 格式不对：{args.random_think!r}，"
                    f"要像 1-17 这样")
                return 2
        bot = Auto(eng, my_color=args.color, komi=args.komi,
                   think=think, max_moves=args.moves,
                   think_mode=mode, think_min=tmin, think_max=tmax,
                   warmup=args.warmup,
                   rules=norm_rules(args.rules) or "chinese",
                   aggro=clamp_aggro(args.aggro))
        if args.recalib:
            bot.observe(force_calib=True)
        bot.run()
    except KeyboardInterrupt:
        log("手动中断")
    except Exception:
        log("出错：")
        traceback.print_exc()
    finally:
        eng.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
