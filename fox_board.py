# -*- coding: utf-8 -*-
"""
野狐围棋盘面识别 + 落子
============================================================
实测参数（2560x1531 窗口，DPI 感知）：
    棋盘主体     (261, 59) 1352x1352
    网格起点     (297.5, 95.5)   ← 第 0 列/行中心（绝对坐标）
    网格间距     71.0556 px
    星位         (3,3) (9,3) (15,3) ...

判断某一交叉点是什么：
    黑子 —— 该点周围 0.28*间距 半径内几乎全暗
    白子 —— 几乎全亮（接近纯白）
    空   —— 木色（R>G>B，偏黄）
"""
import ctypes, math, random, time
from ctypes import wintypes

# ⚠️ DPI 感知：这里设一次是**自保**，不是主入口
# ----------------------------------------------------------------------
# 正式入口 gui.py 已在文件第一行设好（见它的 _init_dpi）。
# 但 fox_board 也会被单独 import：--serve 子服务、各种测试脚本、
# 以及 fox_auto 直跑。那些场景没人替它设，而截屏坐标全是物理像素，
# 一旦进程是 DPI-unaware，2560 的屏会被虚拟化成 1707，
# 抓到的图和标定参数差 1.5 倍 —— 读盘全错。
#
# awareness 只能设一次，设过再设返回 E_ACCESSDENIED（这里吞掉）。
# 所以这段代码的真实作用是「没有别人设的时候我设」，顺序无关。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import numpy as np
import mss
from PIL import Image

COLS = "ABCDEFGHJKLMNOPQRST"

# ---------------------------------------------------------------- 几何
# 早先把 2560x1531 下实测的 (297.5, 95.5) / 71.0556 写死了，
# 结果窗口一变（938x942）整个读盘全乱 —— 19 路被当成 40px 间距，
# 识别出「黑64白5」这种荒谬结果。
# 现在改成**从截图自动标定**：找木色连通区 → 量出网格边界 → 反推间距。
# 只要棋盘在窗口里完整可见，任何分辨率/缩放都能用。

GEOM = None          # 当前标定结果，dict


def _line_peaks(score, min_gap, thresh_ratio=0.25):
    """在一维投影里找网格线峰位"""
    thr = len(score) * thresh_ratio
    idx = []
    for i in range(1, len(score) - 1):
        if score[i] >= score[i - 1] and score[i] >= score[i + 1] and score[i] > thr:
            if not idx or i - idx[-1] > min_gap:
                idx.append(i)
            elif score[i] > score[idx[-1]]:
                idx[-1] = i
    return idx


def _pick_grid(peaks, n):
    """从峰位里挑出「最像 n 条等距网格线」的那一组，返回 (score, picked)

    ⚠️ 为什么容忍「峰数多于 n」（2026-10-23 修）
    ----------------------------------------------------
    暗像素投影对**棋子边缘**也有响应。密集盘面上，一颗棋子会在一列里
    同时贡献中心峰和两侧边缘峰，于是 9 路盘可能找到 10 个峰。
    早先要求 `len(peaks) == n` 才给分 —— 一票否决，9 路盘直接报废。
    现在改成「允许有假峰，挑出最规整的 n 个」，再用等距性 + 覆盖度把关。

    score 0 最好（= 完全等距），越小越差；-1e9 = 这个 n 不可能。
    """
    m = len(peaks)
    if m < n or n < 2:
        return -1e9, None
    p = np.asarray(peaks, float)
    ii = np.arange(n, dtype=float)

    subsets = []
    if m == n:
        subsets.append(p)
    else:
        # ① 首尾锚定：棋盘线必占两端，等分插值后取最近的峰
        step0 = (p[-1] - p[0]) / (n - 1)
        guess = np.array([p[0] + step0 * i for i in ii])
        picked = [int(np.argmin(np.abs(p - g))) for g in guess]
        if len(set(picked)) == n:
            subsets.append(p[sorted(picked)])
        # ② 滑动窗口：连续 n 个峰（假峰偏向一侧时最有效）
        if 0 < m - n <= 12:
            for s in range(0, m - n + 1):
                subsets.append(p[s:s + n])
        # ③ 均匀抽取（假峰不多时最有效）
        idx = np.linspace(0, m - 1, n).round().astype(int)
        if len(set(idx.tolist())) == n:
            subsets.append(p[idx])
        if not subsets:
            return -1e9, None

    best_s, best_sub = -1e9, None
    for sb in subsets:
        step, a = np.polyfit(ii, sb, 1)
        if step <= 2:
            continue
        resid = float(np.abs(sb - (a + step * ii)).max())
        rel = resid / step
        sc = -rel
        if sc > best_s:
            best_s, best_sub = sc, sb
    return best_s, best_sub


def _grid_usage(peaks, grid, tol=0.25):
    """这组网格线能解释掉多少个峰（0~1）

    ⚠️ 注意它是**单向**的：网格越密，越容易把稀疏的峰都解释掉
    ----------------------------------------------------
    9 个峰放上去，19 路网格（18 格）几乎必然全部覆盖 → usage≈1；
    但 19 路网格有 19 条线而只有 9 个峰「撑得起来」，多出的 10 条线
    是凭空插的。所以选中路数不能只看这个值，还要看**网格密度是否
    被峰真正支撑**（见 _grid_support）。
    """
    if len(grid) < 2 or not peaks:
        return 0.0
    step = abs(grid[-1] - grid[0]) / (len(grid) - 1)
    if step <= 0:
        return 0.0
    g = np.asarray(grid, float)
    hit = 0
    for p in peaks:
        if float(np.abs(g - p).min()) <= step * tol:
            hit += 1
    return hit / float(len(peaks))


def _grid_support(grid, peaks, tol=0.30):
    """网格线里有多少条**真的有峰落在上面**（0~1）

    这是 _grid_usage 的反向约束，专门用来挡「插空网格」：
    19 路网格铺在 9 路盘上，只有 9 条线有峰，另 10 条是空的 → 0.47。
    真 9 路网格则每条线都有峰 → 1.0。
    两个指标一起用，才能既不漏（9 路别认成 2 路）也不虚（9 路别认成 19 路）。
    """
    if len(grid) < 2 or not peaks:
        return 0.0
    step = abs(grid[-1] - grid[0]) / (len(grid) - 1)
    if step <= 0:
        return 0.0
    p = np.asarray(peaks, float)
    hit = 0
    for gv in grid:
        if float(np.abs(p - gv).min()) <= step * tol:
            hit += 1
    return hit / float(len(grid))


def calibrate(im, board_n=None):
    """从截图自动标定棋盘几何（**自动识别 2~19 路**）

    两步走（第一步给出粗略范围，第二步精确对齐网格线）：

    1) 找木色区（亮 + 明显偏黄）→ 得到棋盘大致方框与边长。
    2) 在方框内做「暗像素投影」找网格线：格线是深褐色细线，
       投影会形成一排等距尖峰。直接取这些峰位做线性回归，
       比拿木块边界推算准得多。

    早先版本只做第 1 步、用「木块边界 + 半格」反推第 0 线，
    实测偏了约 8px（19 路累计），红点全落在交叉点外侧。
    实测（938x942 窗口）：木色区 725px，19 条线，间距 38.11。

    ⚠️ board_n 现在是可选参数（2026-10-23 老板要求支持 2~19 路）
    ----------------------------------------------------
    不传 = 自动识别。做法是拿投影峰位在 2~19 里逐个试，
    取「等距性最好」的那个 n。传了数字就只认那个数（兼容老调用）。
    识别结果放在返回值的 n / nlines 里，上层据此决定读盘尺寸。

    返回 {"x0","y0","step","n","w","h","ok"}
        x0,y0 = 第 0 列/第 0 行中心的**截图内**坐标
        n     = 棋盘路数（自动识别或调用方指定）
    """
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    H, W = a.shape[:2]
    R, B = a[:, :, 0], a[:, :, 2]
    wood = (R > 140) & (R - B > 40)

    colr = wood.mean(axis=0)
    rowr = wood.mean(axis=1)
    cx = np.where(colr > 0.40)[0]
    ry = np.where(rowr > 0.40)[0]
    if len(cx) < 50 or len(ry) < 50:
        return {"ok": False, "reason": "没找到棋盘木色区"}

    x_lo, x_hi = int(cx.min()), int(cx.max())
    y_lo, y_hi = int(ry.min()), int(ry.max())
    span_x, span_y = x_hi - x_lo, y_hi - y_lo
    if abs(span_x - span_y) > max(30, 0.08 * max(span_x, span_y)):
        return {"ok": False,
                "reason": f"木色区不是正方形 x={span_x} y={span_y}"}

    # ---- 第二步：在木色区内找网格线
    sub = a[y_lo:y_hi + 1, x_lo:x_hi + 1]
    sR, sG, sB = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
    lum = 0.299 * sR + 0.587 * sG + 0.114 * sB
    dark = lum < 120

    # ⚠️ min_gap 必须按**最密**的路数估（2026-10-23 修）
    # ----------------------------------------------------
    # `_line_peaks` 的 min_gap 是「两个峰至少隔多远才算两条线」。
    # 早先按 19 路算（span/38），对 19 路够用，但小路数棋盘格子大得多，
    # 4 路盘的线间距能到 200px+，这个 gap 会把相邻线合并成一条
    # —— 实测 2 路盘只找到 3 个横峰。
    # 现在按 19 路估一个**足够小**的 gap（绝不合并真线），
    # 多出来的假峰交给后面的「等距性打分」筛掉。
    gap = max(3, span_x // 60)          # 19 路时格间距约 span/18，再除 3
    cp = _line_peaks(dark.sum(axis=0), gap)
    rp = _line_peaks(dark.sum(axis=1), gap)

    if board_n:
        # 调用方指定了路数 → 只认这个，但仍要从多余峰里挑出最规整的一组
        n = board_n
        sc_x, gp_x = _pick_grid(cp, n)
        sc_y, gp_y = _pick_grid(rp, n)
    else:
        # 自动识别：2~19 逐个试，用「被峰支撑」当主判据
        #
        # ⚠️ 两个方向都要卡（2026-10-23 反复栽跟头）
        # ----------------------------------------------------
        # 只比等距性 → 永远选 2 路（首尾两峰残差恒为 0）。
        # 只比「峰被解释率」→ 永远选 19 路（网格越密越容易覆盖稀疏峰）。
        # 必须两条一起：
        #   usage  = 峰有多少被网格解释掉   （挡「漏线」，9 路别认成 2 路）
        #   support= 网格有多少条真有峰     （挡「插空」，9 路别认成 19 路）
        # 乘积 usage*support 在真路数处最大。
        cands = {}
        for k in range(2, 20):
            sx, gx = _pick_grid(cp, k)
            sy, gy = _pick_grid(rp, k)
            if gx is None or gy is None:
                continue
            ux = _grid_usage(cp, gx)
            uy = _grid_usage(rp, gy)
            sxx = _grid_support(gx, cp)
            syy = _grid_support(gy, rp)
            usage = (ux + uy) / 2.0
            support = (sxx + syy) / 2.0
            # 等距性：_pick_grid 返回 -残差/步长，0 最好 → 转成 0~1
            fit = max(0.0, 1.0 - abs((sx + sy) / 2.0))
            score = usage * support * fit
            cands[k] = (score, gx, gy, usage, support)
        if not cands:
            return {"ok": False,
                    "reason": f"识别不出路数（横向峰 {len(cp)}、纵向峰 "
                              f"{len(rp)}）—— 确认野狐停在棋盘界面"}
        best = max(cands, key=lambda k: cands[k][0])
        n = best
        _, gp_x, gp_y, best_use, best_sup = cands[best]
        if best_use < 0.55 or best_sup < 0.55:
            return {"ok": False,
                    "reason": f"网格不可靠（{n} 路，峰解释 {best_use*100:.0f}%、"
                              f"线支撑 {best_sup*100:.0f}%）"
                              f"—— 确认野狐停在棋盘界面"}

    n_cp, n_rp = len(cp), len(rp)

    if gp_x is not None and gp_y is not None:
        # 等距回归：line[i] = a + step*i
        ii = np.arange(n)
        sx = float(np.polyfit(ii, gp_x, 1)[0])
        sy = float(np.polyfit(ii, gp_y, 1)[0])
        x0, y0 = float(gp_x[0]) + x_lo, float(gp_y[0]) + y_lo
        step = (sx + sy) / 2.0
        method = "gridline"
    elif board_n:
        # 只有「调用方指定了路数却对不上」才走兜底 —— 自动识别时宁可报错
        step = (span_x + span_y) / 2.0 / (n + 1)
        x0, y0 = x_lo + step, y_lo + step
        method = f"bbox-fallback(线数 {len(cp)}/{len(rp)})"
    else:
        return {"ok": False,
                "reason": f"识别到 {n} 路，但网格线数对不上"
                          f"（横 {len(cp)}、纵 {len(rp)}）"}

    return {"ok": True, "x0": x0, "y0": y0, "step": step, "n": n,
            "w": W, "h": H, "box": (x_lo, y_lo, x_hi, y_hi),
            "method": method, "nlines": (n_cp, n_rp),
            "auto_n": not bool(board_n)}


def detect_board_n(im):
    """快速判断截图里棋盘大概是几路。

    返回 (n, confident)：
        n          路数；**只在 confident=True 时有效**，否则恒为 None
        confident  True  = 有把握（两轴一致、等距性通过）
                   False = 看不准（看不清 / 棋子太密 / 半张棋盘）

    ⚠️ 为什么看不准时**不给数字**（2026-10-23 再改一次）
    ----------------------------------------------------
    上一版是「不确定也给个粗估 n」，想让调用方自己决定要不要用。
    自测发现这等于埋了个雷：9 路撒 30 颗子时会报「11 路」、撒 80 颗报
    「17 路」，13 路撒 80 颗报「16 路」—— **全都是错的**，只是标了
    sure=False。今天唯一那个调用方确实只看 sure，但下一个人（或明天的
    我）看到有个 n 就会顺手用上，然后按 11 路去标定一张 9 路盘。

    一个「已知错误但标了不确定」的返回值，价值是负的 —— 它只提供
    误用的机会。所以现在：没把握就返回 None，让「不能用」变成硬事实，
    而不是一句注释里的君子协定。

    ⚠️ 这个函数只做一件事：**判断缓存的 GEOM 还能不能用**
    ----------------------------------------------------
    野狐同一个窗口可以开 9/13/19 路不同对局。老板下完 9 路接着开 19 路，
    `FB.GEOM` 里还留着 n=9、step 按 9 路算 → 按 9 路的格子去读 19 路的盘，
    采样点全落在格子中间，读出来是噪声（表现成「连续读盘失败」）。

    ⚠️ 为什么要有 confident 这第二返回值（2026-10-23 自测打回重做）
    ----------------------------------------------------
    第一版只返回 n 或 None，然后调用方写「`n != 缓存n` 就重标」。
    自测发现两个漏洞：

      ① **棋子多时认不出**（9 路撒 30 颗子就返回 None）→
         调用方拿到 None 只会「不重标」，于是路数真的变了也发现不了，
         功能等于没做。这是**失效方向错误**：拿不准时应该倾向于重标，
         因为重标的代价只是多算 30ms，漏检的代价是整盘读不出来。

      ② **半张棋盘会瞎猜**（裁掉一半的 19 路盘报「5 路」）→
         调用方拿到 5 就去重标，然后 calibrate 也失败，
         每轮读盘都白跑一次 calibrate。

    现在把判断权交给调用方，并附上 confidence：
        有把握且与缓存不同 → 重标
        没把握（None 或 confident=False）→ 也重标（宁可多算一次）
        有把握且相同 → 什么都不做（正常情况，99% 的轮次走这条）

    ⚠️ 为什么不直接调 calibrate()：
      它要对 2~19 路逐个 `_pick_grid` 打分，每次读盘（0.35 秒一轮）
      都跑一遍太浪费。这里只用**峰数和等距性**粗判，成本约 calibrate 的 1/1.6。
    """
    try:
        a = np.asarray(im.convert("RGB")).astype(np.int16)
        R, B = a[:, :, 0], a[:, :, 2]
        wood = (R > 140) & (R - B > 40)

        colr = wood.mean(axis=0)
        rowr = wood.mean(axis=1)
        cx = np.where(colr > 0.40)[0]
        ry = np.where(rowr > 0.40)[0]
        if len(cx) < 50 or len(ry) < 50:
            return None, False
        x_lo, x_hi = int(cx.min()), int(cx.max())
        y_lo, y_hi = int(ry.min()), int(ry.max())
        span_x, span_y = x_hi - x_lo, y_hi - y_lo
        span = (span_x + span_y) / 2.0
        if span < 100:
            return None, False
        # 棋盘必须是方的。差太多说明不是完整棋盘（半张 / 窗口没对齐）
        if abs(span_x - span_y) > max(20, 0.06 * max(span_x, span_y)):
            return None, False

        sub = a[y_lo:y_hi + 1, x_lo:x_hi + 1]
        lum = 0.299 * sub[:, :, 0] + 0.587 * sub[:, :, 1] + 0.114 * sub[:, :, 2]
        dark = lum < 120
        gap = max(3, int(span) // 60)

        # ⚠️ 两个方向**必须分开**算，不能合并成一个序列（2026-10-23 实测）
        # ----------------------------------------------------
        # 棋盘是正方形，x/y 两轴的峰位数值几乎一模一样（19 路盘都是
        # 24/73/121/169…）。合并排序后相邻两项交替来自两个轴，
        # 差值一半是 0、一半是格距，中位数直接变成 0 → 除零。
        # 实测就是这个坑（自测时 detect 全返回 None）。
        #
        # ⚠️ 用「峰数」而不是「span/格距」算路数（2026-10-23 实测）
        # ----------------------------------------------------
        # `span` 是木色区边长，含棋盘**外缘留白**（约一格），
        # 所以 `span/格距 + 1` 会多算一路：实测 19 路盘 span=914、
        # 格距=48 → 914/48+1 = 20。而 `_line_peaks` 找到的**峰数**
        # 恰好就是线数（19 路 → 19 个峰，实测验证）。
        # 格距只用来做 sanity check，不参与算路数。
        ns = []
        strict = True          # 两轴都通过严格检查才算「有把握」
        for peaks in (_line_peaks(dark.sum(axis=0), gap),
                      _line_peaks(dark.sum(axis=1), gap)):
            if len(peaks) < 3:
                strict = False
                continue
            diffs = sorted(peaks[i + 1] - peaks[i]
                           for i in range(len(peaks) - 1))
            med = diffs[len(diffs) // 2]
            if med <= 3:
                strict = False
                continue
            # 等距性：真网格的格距极差很小；被棋子边缘带乱时会明显偏大。
            # 不通过就仍然记入 ns（粗估仍有参考价值），但降级为不自信。
            if diffs[-1] - diffs[0] > max(6, 0.15 * med):
                strict = False
            ns.append(len(peaks))

        if not ns:
            return None, False
        n = max(set(ns), key=ns.count)
        if not (2 <= n <= 19):
            return None, False
        # 两轴给出不同答案 → 有一边被棋子带偏了，不算有把握
        if len(set(ns)) > 1:
            strict = False
        # ⚠️ 没把握就不给数字（见 docstring 的说明）
        return (n, True) if strict else (None, False)
    except Exception:
        return None, False


def ensure_geom(im=None, board_n=19, force=False):
    """拿到当前几何；没标定过就先标定一次

    ⚠️ 标定失败必须 raise，不能把没有 step 的 dict 传出去
    ----------------------------------------------------
    野狐停在大厅/复盘列表时 calibrate() 找不到棋盘线，返回的是
    {"ok": False, "reason": ...} —— 里面**没有 step/x0/y0**。
    老版本只判断 `GEOM is None`，于是这种失败结果被当成"已标定"，
    后面所有 g["step"] 全部 KeyError。
    GUI 初次读盘时就是这么炸的（日志里 `KeyError: 'step'`），
    而且因为包在 try 里，只打了行"跳过"，看着像无害，其实每次都在失败。
    """
    global GEOM
    if force or GEOM is None or not GEOM.get("ok"):
        if im is None:
            raise RuntimeError("还没有几何标定，需要先给一张截图")
        GEOM = calibrate(im, board_n)
        if not GEOM.get("ok"):
            raise RuntimeError(f"棋盘标定失败：{GEOM.get('reason')}")
    return GEOM


def cell_xy(x, y, board_h=19):
    """(0-based 列, 0-based 行) → 截图内坐标（窗口相对）"""
    g = ensure_geom()
    return (g["x0"] + g["step"] * x, g["y0"] + g["step"] * y)


FOX_PROC = "foxwq.exe"

# ---------------------------------------------------------------- 窗口策略
# ⚠️ 老板 2026-10-23 的硬要求（原话）：
#   「野狐自动对弈开着的时候，野狐围棋没法最小化或者关掉，这个得改一下，
#     让野狐自动对弈启动的时候，不会强制控制野狐围棋的打开或者关闭」
#
# 病根：老代码在三条路径上都无条件 ShowWindow(hwnd, 9)=SW_RESTORE ——
#   ensure_ready() / grab() / activate()
# 于是只要野狐被最小化，下一秒就被程序拽回前台，用户手动最小化永远失败，
# 观感就是「野狐被这个软件劫持了」。
#
# 现在的规矩（分三档，从松到紧）：
#   grab()          读盘**只读**：窗口最小化就直接抛 WindowGone，
#                   绝不动它。PrintWindow 本来就能读被遮住的窗口。
#   ensure_ready()  读几何时若最小化 → 抛 WindowGone（不去还原）。
#   activate()      真要点击时才允许抬到前台 —— 但**只在「可见但被遮挡」时**，
#                   最小化一律视为用户意图，不动。
#
# ⚠️ AUTO_RESTORE 默认 False，而且是**唯一的**还原开关（2026-10-23）
# ----------------------------------------------------
# 第一版把「最小化还原」拆成了两处默认值（ensure_ready 的 allow_restore
# 和 AUTO_RESTORE），结果裸调 ensure_ready() 时又偷偷还原了窗口 ——
# 实测一跑就抓出来。现在统一成：默认一律不还原，任何需要还原的调用点
# 必须**显式**写 allow_restore=True。
#
# 为什么连落子也不还原：
#   落子点的是屏幕绝对坐标，窗口最小化时确实点不出去。但正确做法是
#   **这一手先不下、等下**，而不是把用户的窗口拽出来 ——
#   老板原话「不会强制控制野狐围棋的打开或者关闭」，一个字都不能打折扣。
#   窗口一恢复，主循环下一圈读盘成功就自动接着下，一手都不会丢。
AUTO_RESTORE = False    # 全局策略：永不主动还原野狐窗口


class WindowGone(RuntimeError):
    """野狐窗口处于「用户不想让它出现」的状态（最小化 / 已关闭）

    这不是错误，是用户意图。调用方应当**安静等待**，不要刷错误日志，
    也不要试图把窗口弄出来。
    """

    def __init__(self, msg, hwnd=None, minimized=False):
        super().__init__(msg)
        self.hwnd = hwnd
        self.minimized = minimized      # True=最小化  False=进程/窗口没了


def window_state():
    """只探状态，不动窗口。返回 (hwnd, minimized) 或 (None, False)

    给上层做「现在是该等一等还是该收工」判断用，零副作用。
    """
    try:
        hits = find_hwnd()
    except Exception:
        return None, False
    if not hits:
        return None, False
    hwnd, _t, _l, _tp, _w, _h, mini = hits[0]
    return hwnd, bool(mini)


def _pids_of(name=FOX_PROC):
    """用 toolhelp 快照列出某进程名的所有 PID（比 tasklist 快且不弹窗）"""
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    out = []
    try:
        ok = k32.Process32First(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.decode("mbcs", "ignore").lower() == name.lower():
                out.append(pe.th32ProcessID)
            ok = k32.Process32Next(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def find_hwnd():
    """找野狐主窗口

    教训（2026-10-22）：早先靠标题里的「号房间 / 对弈中」匹配，
    结果老板的窗口标题是 "> [高级房1]"，一条都不命中 → 报「没找到野狐窗口」。
    改成按进程名 foxwq.exe 反查 PID 再匹配窗口，跟标题无关，稳。

    注意最小化状态：GetWindowRect 会返回 158x26 这种假尺寸，
    调用方得先 restore —— 见 ensure_ready()。
    """
    user32 = ctypes.windll.user32
    pids = set(_pids_of())
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lp):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        if not user32.GetWindowTextLengthW(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 511)
        t = buf.value
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        mini = bool(user32.IsIconic(hwnd))
        # 过滤掉 158x26 这类工具条/托盘小窗。
        # 最小化窗口的 rect 也是假的 158x26，所以不能按尺寸砍，得单独放行。
        if not mini and (w < 400 or h < 400):
            return True
        hits.append((hwnd, t, r.left, r.top, w, h, mini))
        return True

    user32.EnumWindows(cb, 0)
    # ⚠️ 排序规则（2026-10-22 重写，原来会抓错窗口）
    # ----------------------------------------------------
    # 老规则只按「标题含对弈中」+ 面积排序，实测出问题：
    # 老板同时开着**登录框**（CForeDialogBase 463x732）和已最小化的
    # **对局窗**（237x39 的标题条）时，登录框面积大 → 排第一 →
    # 抓了一张登录界面去标定 → 「没找到棋盘木色区」→ 每次启动都失败。
    #
    # 现在的优先级：
    #   0. 标题里带「号房间 / 对局 / 对弈」→ 这一定是棋局窗（不管大小/是否最小化）
    #   1. 非最小化的大窗口
    #   2. 其它
    # 同档内按面积降序。
    def _rank(z):
        t = z[1] or ""
        mini = z[6]
        if any(k in t for k in ("号房间", "对局", "对弈", "房间")):
            tier = 0
        elif not mini:
            tier = 1
        else:
            tier = 2
        return (tier, -z[4] * z[5])

    hits.sort(key=_rank)
    return hits


def ensure_ready(hwnd=None, allow_restore=False):
    """量野狐窗口的几何。返回 (hwnd, left, top, w, h, title)

    ⚠️ 最小化时**默认抛 WindowGone，不还原窗口**（2026-10-23 老板要求）
    ----------------------------------------------------
    老代码：`if mini: ShowWindow(hwnd, 9)` —— 于是老板手动把野狐最小化，
    下一轮读盘就把它拽回来，永远关不掉。
    现在：读几何是**纯读操作**，用户最小化就是不想让它露面，必须尊重。
    只有真要点击落子（play_move → activate）时才有资格还原。

    ⚠️ 默认值必须是 False（2026-10-23 血的教训）
    ----------------------------------------------------
    第一版写成 allow_restore=None → 回落 AUTO_RESTORE=True，
    结果 `ensure_ready()` 裸调用照样还原窗口，测试一跑就抓出来。
    「默认不动窗口」要写死在签名里，不能藏在间接层 —— 这种
    间接默认值最容易在后续加调用点时不声不响地复活老 bug。
    """
    user32 = ctypes.windll.user32
    hits = find_hwnd()
    if not hits:
        raise WindowGone("没找到野狐窗口（foxwq.exe 未运行？）", minimized=False)
    if hwnd is None:
        hwnd, t, l, tp, w, h, mini = hits[0]
    else:
        t = l = tp = w = h = mini = None
        for z in hits:
            if z[0] == hwnd:
                _, t, l, tp, w, h, mini = z
                break
        if w is None:
            raise WindowGone("指定的野狐窗口已消失", hwnd=hwnd, minimized=False)

    if mini:
        if not allow_restore:
            # 用户最小化了 → 交给上层安静等待
            raise WindowGone("野狐窗口已最小化（不做干预）",
                             hwnd=hwnd, minimized=True)
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        time.sleep(0.25)
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return hwnd, r.left, r.top, r.right - r.left, r.bottom - r.top, t


def grab(title_filter=None, raise_window=True, dpi=None, allow_restore=False):
    """截取野狐窗口

    ⚠️ 这是**纯读操作**，绝不改变野狐的显示状态（2026-10-23 老板要求）
    ----------------------------------------------------
    老板原话：「野狐自动对弈开着的时候，野狐围棋没法最小化或者关掉」。
    病根就在这个函数：老版 `if mini: ShowWindow(hwnd, 9)`。
    现在改成：
      · 窗口最小化 → 直接抛 WindowGone（上层安静等待，不还原）
      · 窗口被别的程序盖住 → PrintWindow 自己能画一份，不需要抬窗口
      · 只有 PrintWindow 失败、且显式 allow_restore=True 时才碰窗口

    raise_window 保留原意（允许退到屏幕截屏），但它**不再等于**允许还原窗口 ——
    这是两个独立开关，混在一起就是老 bug 的来源。

    PrintWindow 的另一个好处：不抢焦点。老板在别的窗口打字，
    程序每 0.35 秒抓一次图也不会把他光标弄丢。
    """
    hits = find_hwnd()
    if not hits:
        raise WindowGone("没找到野狐窗口（foxwq.exe 未运行？）", minimized=False)
    hwnd, title, l, t, w, hh, mini = hits[0]

    if mini:
        # 最小化：rect 是假的（158x26），PrintWindow 也拿不到内容。
        # 用户就是不想看它 → 抛给上层等，**不还原**。
        raise WindowGone("野狐窗口已最小化（不做干预）",
                         hwnd=hwnd, minimized=True)

    if raise_window:
        # PrintWindow：让窗口自己重绘一份。被遮挡、失焦都能读，
        # 而且不改 Z 序、不抢焦点。
        im = _print_window(hwnd, w, hh)
        if im is not None and _looks_valid(im):
            return im, title, (l, t)
        # PrintWindow 失败（少数 GDI 程序返回全黑）才退屏幕截屏。
        # 截屏要求窗口真的在屏幕上露出来 —— 这一步才需要抬窗口，
        # 且受 allow_restore 显式控制。
        if allow_restore:
            _bring_front(hwnd)
            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            l, t, w, hh = r.left, r.top, r.right - r.left, r.bottom - r.top
        else:
            if (mini or not _window_visible_on_screen(hwnd)):
                raise WindowGone(
                    "野狐窗口不在屏幕上（被遮挡或不可见），且不允许干预窗口",
                    hwnd=hwnd, minimized=mini)

    with mss.MSS() as sct:
        g = sct.grab({"left": l, "top": t, "width": w, "height": hh})
        im = Image.frombytes("RGB", g.size, g.rgb)
    return im, title, (l, t)


def _window_visible_on_screen(hwnd):
    """窗口是不是真的在屏幕上露着（没被全遮、没被裁到屏外）

    只做判断，不动窗口。用于决定「退到屏幕截屏」这条路是否可行。
    """
    try:
        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(hwnd):
            return False
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        # 完全在屏幕外（含 VirtualScreen 之外）就当不可见
        vs_x = user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
        vs_y = user32.GetSystemMetrics(77)
        vs_w = user32.GetSystemMetrics(78)
        vs_h = user32.GetSystemMetrics(79)
        if r.right <= vs_x or r.bottom <= vs_y:
            return False
        if r.left >= vs_x + vs_w or r.top >= vs_y + vs_h:
            return False
        return True
    except Exception:
        return True


def _bring_front(hwnd):
    user32 = ctypes.windll.user32
    try:
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass
    time.sleep(0.12)


def _print_window(hwnd, w, hh):
    """用 PrintWindow 让窗口自己画一份（哪怕被遮挡）"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hdc_screen = user32.GetDC(0)
        hdc = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, hh)
        gdi32.SelectObject(hdc, hbmp)

        # PW_RENDERFULLCONTENT = 2（Win8.1+，对含子窗口的界面才有效）
        ok = user32.PrintWindow(hwnd, hdc, 2)
        if not ok:
            ok = user32.PrintWindow(hwnd, hdc, 0)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -hh           # 负值 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0

        buf = ctypes.create_string_buffer(w * hh * 4)
        got = gdi32.GetDIBits(hdc, hbmp, 0, hh, buf, ctypes.byref(bi), 0)

        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(0, hdc_screen)

        if not got:
            return None
        im = Image.frombuffer("RGBA", (w, hh), buf, "raw", "BGRA", 0, 1).convert("RGB")
        return im
    except Exception:
        return None


def _looks_valid(im):
    """判断 PrintWindow 是否真的拿到了内容（有时候会返回全黑 / 全白）"""
    import numpy as _np
    a = _np.asarray(im.convert("RGB"))
    if a.size == 0:
        return False
    # 采样棋盘中部，木色应该占主导
    h, w = a.shape[:2]
    c = a[int(h*0.3):int(h*0.6), int(w*0.2):int(w*0.5)]
    if c.size == 0:
        return False
    R, G, B = c[:, :, 0].astype(_np.int16), c[:, :, 1].astype(_np.int16), c[:, :, 2].astype(_np.int16)
    wood = (R > 150) & (R - B > 45)
    return float(wood.mean()) > 0.35


def classify(a, cx, cy, step=None):
    """判断交叉点 (cx,cy) 是黑/白/空，返回 (标记, 置信度)

    为什么用「面积占比」而不是「平均亮度」
    ------------------------------------------------------
    早先版本取圆盘平均亮度 + 阈值。两个坑：
      1) 交叉点正中央就是网格线（深棕，lum≈48），平均值被它拉低，
         空点也能测成 110 以下，于是空点被判成黑子。
      2) 黑子的颜色随客户端皮肤/缩放变化很大：
         实测有 RGB(45,45,45) 的深灰，也有 (99,99,99) 的中灰。
         单一阈值不可能两头都罩住。

    改成对圆盘内像素做三分类面积统计，取占比最大的那类：
        dark  (lum < 120)                        → 黑子
        white (lum > 185 且 |R-B| < 38)          → 白子（米白、中性）
        wood  (lum > 140 且 R-B > 42)            → 空点（木黄）

    木色和米白的关键差别不是亮度（都 190~230），是**色偏**：
    木色 R-B ≈ 90~110，白子 R-B ≈ 15~25。这一条是稳定的。

    ⚠️ 整圆取平均会被「中心标记」污染（2026-10-22 踩到大坑）
    ----------------------------------------------------
    实测：野狐在一颗**白子**上画了居中的黑色方块（边长 0.50 格），
    整圆暗占比从 0% 冲到 45.9%，顶穿了 42% 的判黑阈值 →
    白子被读成黑子，整盘读错。

    现在改成：**身份只由外圈环带（65%~88%）决定**，中心区域
    （<50%）只用来判断「有没有标记」，不参与黑白判定。
    外圈是棋子本体的颜色，标记盖不到那里。
    """
    h, w = a.shape[:2]
    if step is None:
        step = ensure_geom()["step"]
    rad = step * 0.42                    # 0.42 格 ≈ 盖住棋子但少沾邻居
    r = int(rad) + 2
    x0, x1 = max(0, int(cx) - r), min(w, int(cx) + r + 1)
    y0, y1 = max(0, int(cy) - r), min(h, int(cy) + r + 1)
    if x1 <= x0 or y1 <= y0:
        return "?", 0.0

    patch = a[y0:y1, x0:x1].astype(np.int16)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d2 = (xx - cx) ** 2 + (yy - cy) ** 2
    m = d2 <= rad * rad
    if m.sum() < 30:
        return "?", 0.0

    px = patch[m]
    R, G, B = px[:, 0], px[:, 1], px[:, 2]
    lum = 0.299 * R + 0.587 * G + 0.114 * B
    rb = R.astype(np.int32) - B.astype(np.int32)

    dark = (lum < 120)
    white = (lum > 185) & (np.abs(rb) < 38)
    wood = (lum > 140) & (rb > 42)

    nd, nw, nwd = float(dark.mean()), float(white.mean()), float(wood.mean())

    # ---- 身份判定：优先看外圈环带（不受中心标记影响）
    ring = (d2[m] >= (rad * 0.55) ** 2)     # 环带：55%~100% 半径
    if ring.sum() >= 40:
        rd = float(dark[ring].mean())
        rw = float(white[ring].mean())
        rwd = float(wood[ring].mean())
        if rd > 0.55 and rd >= rw:
            return "B", rd
        if rw > 0.45 and rw >= rwd:
            return "W", rw
        if rwd > 0.40 and rwd >= rw:
            return ".", rwd

    # ---- 兜底：整圆占比（环带样本不足时才走这里）
    if nd > 0.42 and nd >= nw:
        return "B", nd
    if nw > 0.40 and nw >= nwd:
        return "W", nw
    if nwd > 0.35:
        return ".", nwd
    # 糊在一起了：退回平均值判断
    if nd > nw and nd > nwd:
        return "B", nd
    if nw > nwd:
        return "W", nw
    return ".", max(nwd, 0.0)


def read_board(im, w=None, h=None, geom=None):
    """读盘：返回 (board, conf)

    board[y][x] ∈ {0 空, 1 黑, 2 白}
    conf[y][x]  = (标记, 置信度)

    ⚠️ w/h 现在可以不传（2026-10-23 支持 2~19 路）
    ----------------------------------------------------
    不传就按 geometries 里标定出来的路数走（g["n"]）。
    这样调用方不用再自己盯着「这盘是几路」，标定一次全链路都对。
    显式传了就按传的来（老调用兼容）。

    最后一手标记请用 read_board_full() —— 故意不塞进这个返回值，
    因为全项目有 7 处在解包两元组，改签名会全部炸。
    """
    a = np.asarray(im.convert("RGB"))
    g = geom or ensure_geom(im, w or h or 19)
    n = g.get("n") or 19
    w = int(w or n)
    h = int(h or n)
    step = g["step"]
    board = [[0] * w for _ in range(h)]
    conf = [[None] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            cx = g["x0"] + step * x
            cy = g["y0"] + step * y
            c, cf = classify(a, cx, cy, step)
            conf[y][x] = (c, cf)
            if c == "B":
                board[y][x] = 1
            elif c == "W":
                board[y][x] = 2
    return board, conf


def read_board_full(im, w=None, h=None, geom=None):
    """读盘 + 最后一手标记：返回 (board, conf, last)

    last = ("R12", 0.204, "W") 或 None
    棋盘只扫一遍，标记复用同一张图，比分别调用省一次全盘扫描。
    w/h 不传时按标定出来的路数走（2~19 路）。
    """
    b, conf = read_board(im, w, h, geom)
    hh = len(b)
    ww = len(b[0]) if hh else 0
    hits, _ = find_last_move(im, b, geom=geom, w=ww, h=hh)
    return b, conf, (hits[0] if hits else None)


# ---------------------------------------------------------------- 最后一手标记

LAST_DARK = 120      # 白子上「黑三角」的亮度上限（白子本体 218）
LAST_LIGHT = 190     # 黑子上「白三角」的亮度下限（黑子本体 44）
LAST_RATIO = 0.05    # 反差像素占棋子圆面积的阈值


def find_last_move(im, board, geom=None, w=19, h=19):
    """靠野狐给最后一手画的「反色标记」判定最后落子在哪

    规则（2026-10-22 老板给的口径 + 本机实测）
    ----------------------------------------------------------
    最后一手的棋子上会盖一个**反色**的小三角：
        白子当最后一手 → 盖黑三角
        黑子当最后一手 → 盖白三角
    这正是「大多数对弈平台」的通用做法，所以这套判据不只对野狐有效。

    实测数据（1770x1092，step=48.02，多盘交叉验证）：

        棋子            暗像素(<120)   亮像素(>190)   象限分布
        白子正常           0.00%         81.67%          —
        白子+黑三角       20.40%         70.92%       0/0/0/100  ← 单一角落
        黑子正常          84.78%          0.00%          —
        黑子+白三角       39.12%         (待补)      （黑子上白三角更亮更实）

    三条判据（必须同时成立，缺一不可）：
        ① 反色像素占比 > 5%          —— 有东西
        ② 反色像素集中在**单一象限** —— 是三角不是方块
        ③ 占比 < 60%                —— 不是整个棋子被换色

    ⚠️ 为什么必须查象限（2026-10-22 大坑）
    --------------------------------------
    野狐还有一种**居中黑方块**（边长 0.50 格）会出现在棋子上，
    实测把一颗白子的暗占比顶到 45.9%、反色占比 39%，看起来完美命中。
    但它的四象限分布是 25/25/25/25（居中对称），而真三角是
    0/0/0/100。靠这一条就能把界面元素和真标记分开。

    返回 (hits, detail)
        hits   按占比降序的 [(gtp, ratio, color), ...]
        detail {"ratios": {...}, "quads": {...}, "n": 命中数}
    空盘 / 复盘无标记时 hits 为空，不算错误。
    """
    a = np.asarray(im.convert("RGB")).astype(np.int32)
    g = geom or ensure_geom(im, max(w, h))
    step = g["step"]
    R = max(3, int(step * 0.42))
    lum = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    H, W = lum.shape

    yy, xx = np.mgrid[-R:R, -R:R]
    circ = (yy * yy + xx * xx) <= R * R
    n_circ = int(circ.sum())
    if n_circ <= 0:
        return [], {"ratios": {}, "quads": {}, "n": 0, "R": R}

    # 四象限掩码（用于判断「是不是居中的方块」）
    quads = [circ & (yy >= 0) & (xx >= 0), circ & (yy >= 0) & (xx < 0),
             circ & (yy < 0) & (xx >= 0), circ & (yy < 0) & (xx < 0)]

    ratios, qdist, hits = {}, {}, []
    for y in range(h):
        for x in range(w):
            v = board[y][x]
            if not v:
                continue
            cx = int(round(g["x0"] + step * x))
            cy = int(round(g["y0"] + step * y))
            if (cx - R < 0 or cy - R < 0
                    or cx + R > W or cy + R > H):
                continue
            pl = lum[cy - R:cy + R, cx - R:cx + R]
            # 反色：白子找暗块、黑子找亮块
            con = ((pl < LAST_DARK) if v == 2 else (pl > LAST_LIGHT)) & circ
            n_con = int(con.sum())
            ratio = float(n_con) / n_circ
            gtp = to_gtp(x, y, h)
            ratios[gtp] = ratio
            if n_con == 0:
                continue

            qs = [int((con & q).sum()) for q in quads]
            # 集中度：最大象限占全部反色像素的比例
            conc = max(qs) / float(n_con) if n_con else 0.0
            qdist[gtp] = (qs, conc)

            if ratio <= LAST_RATIO:
                continue
            if ratio >= 0.60:            # 整个棋子被换色 → 不是标记
                continue
            if conc < 0.62:              # 居中对称 → 方块/高亮，不是三角
                continue
            hits.append((gtp, ratio, "B" if v == 1 else "W"))

    hits.sort(key=lambda z: -z[1])
    return hits, {"ratios": ratios, "quads": qdist, "n": len(hits), "R": R}


def to_gtp(x, y, h=19):
    """(列, 行) → GTP 坐标。h = 棋盘路数（2~19）"""
    return f"{COLS[x]}{h - y}"


def from_gtp(s, h=19):
    """GTP 坐标 → (列, 行)。h = 棋盘路数（2~19）"""
    s = s.upper().strip()
    if s in ("PASS", ""):
        return None
    c = s[0]
    if c == "I":        # 野狐/GTP 都不用 I
        return None
    if c not in COLS:
        return None
    try:
        row = int(s[1:])
    except ValueError:
        return None
    x = COLS.index(c)
    y = h - row
    return x, y


def n_of_board(board):
    """从二维盘面取路数（取高，方盘通常相等）"""
    return len(board) if board else 19


def show(board, h=None, w=None):
    h = h or len(board)
    w = w or (len(board[0]) if board else h)
    print("    " + " ".join(COLS[:w]))
    for y in range(h):
        row = "".join("X" if board[y][x] == 1 else ("O" if board[y][x] == 2 else ".") for x in range(w))
        print(f" {h-y:2d} " + " ".join(row))
    nb = sum(1 for r in board for v in r if v == 1)
    nw = sum(1 for r in board for v in r if v == 2)
    print(f" 黑 {nb} 白 {nw}")


# ---------------------------------------------------------------- 点击

# ⚠️ 落子点击**永远**带一点随机偏移（2026-11-01 老板要求，写成底层规则）
# ----------------------------------------------------
# 老板原话：「鼠标点击的时候随机偏移少许（微微随机偏离，不要影响落子
# 位置，不用做成选项，写成底层规则就可以）」
#
# 为什么要有：老代码每次都点交叉点的精确中心（x0+step*i, y0+step*j），
# 同一局里同一个点出现的坐标一位不差，两次对局之间也一样 ——
# 这是机器最容易露馅的地方（人不可能两次点在同一像素上）。
#
# 两条约束，都写死在这里，不做成选项：
#   ① 老板 2026-11-05 定：随机偏移在**方格宽度的 10% 以内**。
#      19 路 38px 格距下 ≤3.8px；常见 71px 格距下 ≤7.1px。
#   ② **绝不能改变落点**：偏移必须远小于半个格距（step/2 ≈ 35px），
#      野狐是按「离哪个交叉点最近」判定的，10% 连半格都不到。
#      _click_jitter() 里再夹一次 1/5 格距，哪怕几何标定缩放异常也不越界。
CLICK_JITTER_MAX = 12.0      # 像素硬上限（只防异常大格距；常规 10% 远小于此）
CLICK_JITTER_RATIO = 0.10    # 老板 2026-11-05 定：随机偏移 ≤ 格距 10%
CLICK_JITTER_SAFE = 0.20     # 硬上限：格距的 1/5，仍远小于半格


def _click_jitter(step, rnd=None):
    """返回落子点击的 (dx, dy)，单位像素；拿不到格距就返回 (0, 0)。

    圆盘内均匀取样：半径要开方，否则点会挤在圆心附近（面积不均匀）。
    传 rnd 是为了测试能固定随机种子。
    """
    rnd = rnd or random
    try:
        step = float(step)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if step <= 0:
        return 0.0, 0.0
    r = min(CLICK_JITTER_MAX,
            step * CLICK_JITTER_RATIO,
            step * CLICK_JITTER_SAFE)
    if r <= 0:
        return 0.0, 0.0
    ang = rnd.uniform(0.0, 2.0 * math.pi)
    dist = r * math.sqrt(rnd.random())
    return dist * math.cos(ang), dist * math.sin(ang)


def window_at_point(sx, sy):
    """屏幕坐标下面是哪个窗口（返回顶层 hwnd，找不到返回 0）

    2026-11-02 加的：**点击能不能送到野狐，只有这个函数说了算**。
    activate() 只能保证野狐是"前台"，但如果有个置顶窗口压在上面
    （本程序自己、聊天窗口、任何 always-on-top 的框），
    点击会落在那个人身上 —— 野狐一个字都收不到，
    日志表现就是「落子 X 2.5s 内未见生效」，而且怎么重试都没用。
    """
    u = ctypes.windll.user32
    hwnd = u.WindowFromPoint(wintypes.POINT(int(sx), int(sy)))
    if not hwnd:
        return 0
    root = u.GetAncestor(hwnd, 2)      # GA_ROOT
    return root or hwnd


def window_desc(hwnd):
    """给日志用的「这是谁」：标题 + 类名"""
    if not hwnd:
        return "（没有窗口）"
    u = ctypes.windll.user32
    ln = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(ln + 1)
    u.GetWindowTextW(hwnd, buf, ln + 1)
    cls = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(hwnd, cls, 64)
    t = buf.value.strip() or cls.value
    return f"{t[:24]}（{cls.value}）"


def force_top(hwnd):
    """把窗口硬推到 z 序最上层（不是只抢前台）

    SetForegroundWindow 对「后台进程抢焦点」有限制，有时返回成功
    但 z 序没动。SetWindowPos(SWP_SHOWWINDOW) 更直接。
    """
    u = ctypes.windll.user32
    HWND_TOP = 0
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    try:
        u.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    except Exception:
        pass


def click_at(sx, sy):
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(sx), int(sy))
    time.sleep(0.012)
    user32.mouse_event(0x0002, 0, 0, 0, 0)      # LEFTDOWN
    time.sleep(0.012)
    user32.mouse_event(0x0004, 0, 0, 0, 0)      # LEFTUP


def activate(hwnd, allow_restore=True):
    """把野狐窗口提到最前 —— **这是唯一允许动窗口的地方**

    ⚠️ 为什么这里必须抬窗口（2026-10-22 补，2026-10-23 收紧）
    ----------------------------------------------------
    点击打的是**屏幕绝对坐标**，不认窗口层级。野狐要是被别的窗口
    压住、或者最小化，点下去就落到别人身上 ——
    轻则白点一下，重则点到聊天框把半句话发出去，严重的点到
    「认输/退出」直接输棋。
    所以**真要落子**时，抬窗口是安全性要求，不是任性。

    ⚠️ 但 2026-10-23 老板明确要求：不允许程序霸着野狐不让最小化/关闭。
       所以这里加了 allow_restore 开关，并且**最小化视为用户的明确意图**：
       allow_restore=False 时绝不还原，直接返回 False 让上层等。

    返回 True = 确实在前台，可以安全点击。
    """
    user32 = ctypes.windll.user32
    try:
        if user32.IsIconic(hwnd):
            if not allow_restore:
                # 用户最小化了 → 尊重，别拽回来
                return False
            user32.ShowWindow(hwnd, 9)     # SW_RESTORE
            time.sleep(0.25)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        if user32.GetForegroundWindow() == hwnd:
            return True
        # ⚠️ 2026-11-02：后台进程抢前台会被 Windows 挡住（前台锁）。
        # 挂到当前前台线程的输入队列上再 SetForegroundWindow，
        # 这是系统留给「确实需要」的进程的合法通道。
        fg = user32.GetForegroundWindow()
        if fg:
            tid_cur = ctypes.windll.kernel32.GetCurrentThreadId()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            attached = False
            try:
                if tid_fg and tid_fg != tid_cur:
                    attached = bool(user32.AttachThreadInput(
                        tid_cur, tid_fg, True))
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(tid_cur, tid_fg, False)
            time.sleep(0.12)
    except Exception:
        pass
    return user32.GetForegroundWindow() == hwnd


def is_front(hwnd):
    return ctypes.windll.user32.GetForegroundWindow() == hwnd


def _wake_window(hwnd):
    """点一下标题栏把野狐「叫醒」—— 不动棋盘、不碰任何按钮

    为什么需要（2026-11-02 现场）：程序自己最小化后成了后台进程，
    `SetForegroundWindow` 常被前台锁挡掉。而有些界面在**非激活**状态下
    收到棋盘点击只当「激活窗口」，不认这一手 —— 表现就是点在哪都没子。
    先点标题栏正中（标题文字附近，不是按钮）把它变成真正的前台，
    再点棋盘，就和人手动点的第一下一样。
    """
    try:
        u = ctypes.windll.user32
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return
        x = (r.left + r.right) // 2
        y = r.top + 12
        click_at(x, y)
        time.sleep(0.18)
    except Exception:
        pass


def play_move(gtp, confirm=True, wait=0.35, geom=None, must_activate=True,
              allow_restore=None):
    """在野狐棋盘上落子。返回 (ok, detail)

    坐标换算链：GTP → (x,y) 格 → 截图内像素 → **屏幕绝对像素**
    最后一步必须加窗口原点。早先 cell_xy 直接返回绝对坐标（把原点
    297.5 写死在里面），窗口一动就全错；现在原点从 find_hwnd 实时取。

    安全约束（must_activate=True 时生效）：
      窗口不在前台就**不点**，先抢前台；抢不到直接返回失败。
      宁可不下这一手，也不能把点击甩到别的窗口上。

    ⚠️ 窗口最小化时**直接返回失败，不还原窗口**（2026-10-23 老板要求）
      落子确实需要窗口可见（点的是屏幕绝对坐标），但正确做法是
      **这一手先不下**，而不是把用户的窗口拽出来。返回值带
      `paused=True`，上层据此安静等待 —— 窗口一恢复就自动接着下。
    """
    # ⚠️ 先拿几何再换算坐标（2026-10-23 支持 2~19 路）
    # 老代码先 from_gtp(gtp) 再 ensure_geom()，而 from_gtp 默认 h=19，
    # 于是 9 路盘上「E5」会被当成 19 路的 E5 → 点到完全不同的位置。
    # 现在先标定出色盘路数，再按那个 h 换算。
    #
    # ⚠️ ensure_geom() 会 raise，必须在这里拦掉（2026-10-23 查 bug 补）
    # ----------------------------------------------------
    # 野狐停在大厅/复盘列表时 calibrate() 找不到棋盘线 →
    # ensure_geom() 抛 RuntimeError("棋盘标定失败：…")。
    # 老代码这行在 try 之外，异常直接冲到 Auto.place()（那里没 try）
    # → 再冲到 _loop 的最外层 → 被判成「对局异常终止」直接收工。
    # 老板只是切到大厅看一眼，程序就自己退了 —— 和「霸着窗口」同一类毛病。
    # 现在当成「这一手先不下」，返回 paused，上层安静等待下一圈重试。
    try:
        g = geom or ensure_geom()
    except Exception as e:
        return False, {"paused": True, "error": f"棋盘几何不可用：{e}",
                       "minimized": False, "screen": None}
    hh = g.get("n") or 19
    xy = from_gtp(gtp, hh)
    if xy is None:
        return False, "无效着法"
    x, y = xy
    want_restore = AUTO_RESTORE if allow_restore is None else bool(allow_restore)
    try:
        hwnd, wx, wy, ww, wh, title = ensure_ready(allow_restore=want_restore)
    except WindowGone as e:
        return False, {"paused": True, "error": str(e),
                       "minimized": e.minimized, "screen": None}
    lx = g["x0"] + g["step"] * x
    ly = g["y0"] + g["step"] * y
    # ⚠️ 落子坐标带随机偏移（上层规则见 _click_jitter，2026-11-05 改）
    # 偏移 ≤ 方格宽度的 10%，离交叉点中心仍远小于半个格距，
    # 野狐判定的仍然是同一个点；但两次点击的像素坐标不再一模一样。
    jx, jy = _click_jitter(g.get("step"))
    sx, sy = wx + lx + jx, wy + ly + jy

    # ⚠️ 落子流程（2026-11-02 定稿：**先抬野狐，再点**）
    # ----------------------------------------------------
    # 老板原话：「改回最初的落子方式吧，不用保持弹窗了，
    #           还是落子就自动最小化弹窗好了」。
    # 所以这里回到最初的三步：
    #   ① 把野狐抬到前台（activate）—— 旧流程，别丢
    #   ② 确认落点真的属于野狐（防止有置顶窗口压在棋盘上）
    #   ③ 点
    # 程序自己的窗口在开跑时已经自动最小化（见 gui._minimize_self），
    # 所以正常情况下 ② 一定通过；② 只是兜底，防止别的置顶窗口挡路时
    # 把鼠标点到别人身上（那就是「一手都不下」的真凶）。
    # ⚠️ 2026-11-02 修正：抢不到前台**不再等于不能点**
    # ----------------------------------------------------
    # v3.0.3 这里是「activate() 返回 False 就直接放弃」。实测后果：
    # 本窗口一开始落子就自动最小化，自己变成后台进程，Windows 的
    # 前台锁（foreground lock）经常不让后台进程 SetForegroundWindow；
    # 于是每一手都在这里被拒，日志一行「野狐不在前台，拒绝点击」，
    # 表现就是老板说的「他不下子报错」。
    #
    # 但点击是**屏幕绝对坐标**，只要落点像素真属于野狐，点下去一定
    # 能送到（点后台窗口本身就会把它带到前台）—— 所以真正的门槛
    # 只有一个：window_at_point() == 野狐。抬窗口降级成「尽力而为」。
    activated = True
    if must_activate:
        activated = activate(hwnd, allow_restore=want_restore)
        if not activated:
            # 有些界面（野狐也是）在非激活状态下只把第一下当「激活」，
            # 不认落子。先点标题栏叫醒，再确认一次前台。
            _wake_window(hwnd)
            activated = activate(hwnd, allow_restore=want_restore)

    owner = window_at_point(sx, sy)
    if owner != hwnd:
        force_top(hwnd)
        time.sleep(0.12)
        owner = window_at_point(sx, sy)
        if owner != hwnd:
            return False, {"error": f"落点被别的窗口挡住：{window_desc(owner)}",
                           "paused": True, "blocked": True,
                           "blocker": window_desc(owner),
                           "title": title, "screen": (sx, sy)}

    click_at(sx, sy)
    if not activated:
        # ⚠️ 2026-11-02：还是没能把野狐变成前台时补点一次。
        # 第一下很可能只被当成「激活窗口」，野狐不认落子 —— 第二下才算数。
        # 同一个点连点两下不会多落子：第一下成功的话这一点已经有子，
        # 野狐对已有子的点只会忽略。
        time.sleep(0.35)
        click_at(sx, sy)
    if not confirm:
        time.sleep(wait)
        return True, {"screen": (sx, sy), "activated": activated}
    time.sleep(wait)
    for _ in range(10):
        im, _, _ = grab()
        b2, conf = read_board(im)
        v = b2[y][x]
        if v != 0:
            return True, {"board": b2, "value": v, "screen": (sx, sy)}
        time.sleep(0.12)
    return False, {"board": b2, "value": b2[y][x], "screen": (sx, sy)}


if __name__ == "__main__":
    im, title, org = grab()
    print(title, org, im.size)
    b, conf = read_board(im)
    show(b)
    im2 = im.crop((261, 59, 261 + 1352, 59 + 1352))
    im2.save("fox_board_only.png")
