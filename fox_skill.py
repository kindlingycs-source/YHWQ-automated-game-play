# -*- coding: utf-8 -*-
"""野狐客户端操作技能（AI 托管用）

这一层只做「确定性动作」：找窗口、找控件、点按钮、发聊天、读赔率。
所有判断（要不要接、下多少、说什么）都在 ai_agent.py 里，由大模型 + 策略决定。

设计原则
------------------------------------------------------------
1) **能按控件点就不按坐标点**：野狐多数对话框是标准 MFC 控件，
   用 `BM_CLICK` / `WM_COMMAND` 点最稳，也不受窗口被别的东西压住影响。
2) **坐标只做兜底**：邀请框是自绘的，实测 0.24W / 0.84H 是绿色「接受」，
   只在找不到按钮时用，并且同一次最多试两个位置。
3) **失败要说话**：每个动作返回 (ok, 说明)，调用方把说明写进 AI 页日志；
   不要静默失败，否则老板只看到「AI 不动」。
4) **绝不点危险区**：退出房间=认输、对局研究/复盘=可能认输、商城/广告/
   最右竖列一律不进。`leave_room()` 只在调用方确认「对局已结束」后使用。

依赖 `fox_ai/`（从 projects/fox-agent 并过来的 win32 / capture / annotate）。
"""
from __future__ import annotations

import ctypes
import math
import os
import re
import subprocess
import time
from ctypes import wintypes

from fox_ai import annotate, capture
from fox_ai import win32 as W

try:
    _user32 = ctypes.windll.user32
except Exception:                       # pragma: no cover - 非 Windows
    _user32 = None

# 常用消息
WM_SETTEXT = 0x000C
WM_COMMAND = 0x0111
CBN_SELCHANGE = 1
CB_GETCOUNT = 0x0146
CB_GETCURSEL = 0x0147
CB_SETCURSEL = 0x014E

# 不是界面窗口的类（和 fox-agent/tools/auto_accept_fast.py 同口径）
_HELPER_TITLES = {
    "CLoginDialog", "CSelectServerDialog", "CForeDialogBase",
    "CDlgSelServer", "CNavigationPanel",
}


def _now():
    return time.strftime("%H:%M:%S")


class FoxSkill:
    """一个薄薄的操作层；所有方法都不抛异常，返回 (ok, msg)。"""

    def __init__(self, log=None, fox_exe: str = ""):
        self._log = log or (lambda _m: None)
        self.fox_exe = fox_exe or r"C:\Program Files (x86)\foxwq\foxwq\foxwq.exe"

    # ------------------------------------------------------------ 日志
    def log(self, msg):
        try:
            self._log(str(msg))
        except Exception:
            pass

    # ------------------------------------------------------------ 窗口
    def running(self) -> bool:
        try:
            return bool(W.is_process_running())
        except Exception:
            return False

    def main(self):
        """主窗口（对局中优先返回标题带「号房间/对弈」的那个）"""
        try:
            return W.find_fox_window(min_area=400 * 400, allow_minimized=True)
        except Exception:
            return None

    def windows(self):
        try:
            return W.list_fox_windows(include_hidden=False)
        except Exception:
            return []

    def dialogs(self):
        """野狐的顶层对话框（邀请框 / 申请对局 / 支持 / 终局…）"""
        out = []
        try:
            main = self.main()
            main_hwnd = main.hwnd if main else 0
            for w in self.windows():
                if not w.visible or w.minimized or w.hwnd == main_hwnd:
                    continue
                if w.cls != "#32770":
                    continue
                if w.title in _HELPER_TITLES:
                    continue
                if w.rect.width < 120 or w.rect.height < 60:
                    continue
                out.append(w)
        except Exception:
            pass
        return out

    def ensure_fox(self, timeout: float = 40.0):
        """找野狐；没开就按配置的 exe 启动。返回 (ok, msg)。"""
        if self.main():
            return True, "野狐已在运行"
        exe = self.fox_exe
        if not exe or not os.path.exists(exe):
            # 也试一下常见安装位置
            cands = [
                r"C:\Program Files\foxwq\foxwq\foxwq.exe",
                r"C:\Program Files (x86)\foxwq\foxwq\foxwq.exe",
            ]
            exe = next((p for p in cands if os.path.exists(p)), "")
        if not exe:
            return False, "找不到野狐 foxwq.exe，请先手动打开野狐"
        try:
            self.log(f"启动野狐：{exe}")
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except Exception as e:
            return False, f"启动野狐失败：{type(e).__name__}: {e}"
        deadline = time.time() + max(5.0, timeout)
        while time.time() < deadline:
            if self.main():
                return True, "野狐已启动"
            time.sleep(0.5)
        return False, "野狐启动超时，请手动打开并登录"

    # ------------------------------------------------------------ 控件
    def _all_controls(self, main=None, depth=5):
        main = main or self.main()
        if not main:
            return []
        try:
            return W.enum_children(main.hwnd, max_depth=depth, max_nodes=2500)
        except Exception:
            return []

    def find_button(self, labels, *, hwnd: int | None = None):
        """在 hwnd（默认主窗口 + 所有对话框）里找按钮。

        labels 可以是字符串或字符串序列；按「完全相等」优先，其次包含。
        返回 (Control, window_hwnd, matched_label) 或 None。
        """
        if isinstance(labels, str):
            labels = (labels,)
        labels = tuple(str(x) for x in labels if x)
        scopes = []
        if hwnd:
            scopes.append(hwnd)
        else:
            main = self.main()
            if main:
                scopes.append(main.hwnd)
            scopes.extend(w.hwnd for w in self.dialogs())
        for h in scopes:
            try:
                ctrls = W.enum_children(h, max_depth=5, max_nodes=2500)
            except Exception:
                continue
            # 先找完全相等
            for c in ctrls:
                if not c.visible or c.cls.lower() != "button":
                    continue
                if c.text.strip() in labels:
                    return c, h, c.text.strip()
            # 再找包含
            for c in ctrls:
                if not c.visible or c.cls.lower() != "button":
                    continue
                t = c.text.strip()
                for lab in labels:
                    if lab and lab in t:
                        return c, h, lab
        return None

    def click_button(self, labels, *, hwnd=None, msg=""):
        """找按钮并 BM_CLICK。找不到返回 (False, 说明)。"""
        hit = self.find_button(labels, hwnd=hwnd)
        if not hit:
            return False, f"没找到按钮：{'/'.join(labels if not isinstance(labels, str) else [labels])}"
        ctrl, _h, label = hit
        try:
            ok = W.bm_click(ctrl.hwnd)
        except Exception as e:
            ok = False
            self.log(f"BM_CLICK 失败：{e}")
        if ok:
            return True, f"已点「{label}」"
        # 兜底：点控件中心
        try:
            W.click_at(*ctrl.center)
            return True, f"已点「{label}」（坐标兜底）"
        except Exception as e:
            return False, f"点「{label}」失败：{type(e).__name__}: {e}"

    def texts(self, hwnd=None, *, limit=4000):
        """读窗口/控件文本，拼成一行（对话框内容识别用）"""
        out = []
        if hwnd:
            try:
                out.append(W.control_text(hwnd, limit))
            except Exception:
                pass
            try:
                for c in W.enum_children(hwnd, max_depth=5, max_nodes=1500):
                    if c.text:
                        out.append(c.text)
            except Exception:
                pass
        else:
            main = self.main()
            if main:
                try:
                    out.append(W.control_text(main.hwnd, limit))
                except Exception:
                    pass
            for w in self.dialogs():
                try:
                    out.append(W.control_text(w.hwnd, limit))
                except Exception:
                    pass
        # 去重、去空
        seen, uniq = set(), []
        for s in out:
            s = str(s or "").strip()
            if s and s not in seen:
                seen.add(s)
                uniq.append(s)
        return "\n".join(uniq)

    # ------------------------------------------------------------ 登录/大厅
    def title(self) -> str:
        w = self.main()
        return (w.title if w else "") or ""

    def ensure_lobby(self, timeout: float = 20.0):
        """确保停在大厅；在房间里就点「退出房间」（对局中会认输，调用方负责）"""
        deadline = time.time() + max(3.0, timeout)
        while time.time() < deadline:
            t = self.title()
            if "高级房" in t and "号房间" not in t:
                return True, f"在大厅：{t}"
            if "请登录" in t:
                return False, "野狐还停在登录框，请先手动登录（AI 不碰密码）"
            ok, msg = self.click_button("退出房间")
            if ok:
                time.sleep(1.0)
                continue
            time.sleep(0.5)
        return False, f"回不到大厅（当前标题：{self.title() or '无'}）"

    # ------------------------------------------------------------ 邀请
    def find_invite(self):
        """中央的邀请框：只显示 10 秒，必须快"""
        try:
            sw = _user32.GetSystemMetrics(0) if _user32 else 1920
            sh = _user32.GetSystemMetrics(1) if _user32 else 1080
            cx, cy = sw / 2, sh / 2
            for w in self.dialogs():
                if not (150 <= w.rect.width <= 900 and 90 <= w.rect.height <= 500):
                    continue
                mx, my = w.rect.center
                if abs(mx - cx) > 420 or abs(my - cy) > 380:
                    continue
                txt = self.texts(w.hwnd)[:400]
                if any(k in txt for k in ("申请对局", "对局信息", "同意", "拒绝")):
                    # 申请对局确认框是另一个流程，别当邀请
                    if "支持" in txt or "选黑" in txt:
                        continue
                return w
        except Exception:
            pass
        return None

    def accept_invite(self, inv=None):
        """点邀请框的「接受」。先找按钮，找不到按 0.24W/0.84H 坐标兜底。"""
        inv = inv or self.find_invite()
        if not inv:
            return False, "没有邀请框"
        hit = self.find_button(("接受", "同意"), hwnd=inv.hwnd)
        if hit:
            ctrl, _h, label = hit
            try:
                W.bm_click(ctrl.hwnd)
                time.sleep(0.4)
                return True, f"已接受邀请（按钮「{label}」）"
            except Exception:
                pass
        x, y, w, h = inv.rect.left, inv.rect.top, inv.rect.width, inv.rect.height
        for fy in (0.84, 0.80):
            try:
                W.click_at(int(x + w * 0.24), int(y + h * fy))
            except Exception as e:
                return False, f"点接受失败：{type(e).__name__}: {e}"
            time.sleep(0.45)
            if not W.is_window(inv.hwnd) or not W.is_visible(inv.hwnd):
                return True, "已接受邀请（坐标兜底）"
        return False, "点了接受但邀请框还在（可能已超时/点偏）"

    # ------------------------------------------------------------ 申请对局
    def request_match(self):
        """点 4A「自动申请对局 / 极速匹配」"""
        return self.click_button(("自动申请对局", "极速匹配"))

    def cancel_match(self):
        return self.click_button(("取消对局申请", "取消申请"))

    def find_game_dialog(self, timeout: float = 8.0):
        """进对局室后自动弹出的「申请对局 / 编辑对局信息」框"""
        deadline = time.time() + max(0.5, timeout)
        while time.time() < deadline:
            for w in self.dialogs():
                if not (200 <= w.rect.width <= 950 and 180 <= w.rect.height <= 700):
                    continue
                txt = self.texts(w.hwnd)
                if any(k in txt for k in ("申请对局", "对局信息", "同意", "申请", "拒绝")):
                    return w, txt
            time.sleep(0.25)
        return None, ""

    def select_combo(self, target, *, hwnd=None):
        """在对话框里把某个下拉框选到包含 target 的项（如「友谊赛」）。

        ⚠️ 跨进程 `CB_GETLBTEXT` 需要目标进程缓冲区，不能直接用；
        这里用 `CB_SETCURSEL` 逐项切换 + `WM_GETTEXT` 读当前选中项文本，
        找到后给父窗口补一个 `CBN_SELCHANGE`，让对话框自己刷新。
        """
        scopes = [hwnd] if hwnd else ([self.main().hwnd] if self.main() else [])
        scopes += [w.hwnd for w in self.dialogs()]
        for h in scopes:
            try:
                ctrls = W.enum_children(h, max_depth=5, max_nodes=2500)
            except Exception:
                continue
            for c in ctrls:
                if not c.visible or c.cls.lower() != "combobox":
                    continue
                try:
                    count = int(_user32.SendMessageW(
                        wintypes.HWND(c.hwnd), CB_GETCOUNT, 0, 0))
                    old = int(_user32.SendMessageW(
                        wintypes.HWND(c.hwnd), CB_GETCURSEL, 0, 0))
                except Exception:
                    continue
                if count <= 0 or count > 200:
                    continue
                for i in range(count):
                    try:
                        _user32.SendMessageW(
                            wintypes.HWND(c.hwnd), CB_SETCURSEL, i, 0)
                        time.sleep(0.02)
                        label = W.window_text(c.hwnd)
                    except Exception:
                        continue
                    if target in (label or ""):
                        try:
                            cid = int(_user32.GetDlgCtrlID(
                                wintypes.HWND(c.hwnd)))
                            _user32.SendMessageW(
                                wintypes.HWND(h), WM_COMMAND,
                                (CBN_SELCHANGE << 16) | (cid & 0xFFFF),
                                c.hwnd)
                        except Exception:
                            pass
                        return True, f"已选「{label}」"
                # 没找到：恢复原来的选择，别把用户配置改乱
                try:
                    if 0 <= old < count:
                        _user32.SendMessageW(
                            wintypes.HWND(c.hwnd), CB_SETCURSEL, old, 0)
                except Exception:
                    pass
        return False, f"下拉框里没找到「{target}」"

    def negotiate(self, *, prefer_friendly=True, accept=True):
        """处理「申请对局」框：读按钮文案 → 同意/接受，或按策略改条件后申请。

        返回 (ok, 说明)。不主动开框（框是自动弹出的）。
        """
        dlg, txt = self.find_game_dialog(timeout=3.0)
        if not dlg:
            return False, "没看到对局协商框"
        # 先看按钮：同意/接受 = 应答方；申请/发送 = 提议方。
        # ⚠️ 不能把「拒绝」混进候选：控件枚举顺序不保证，可能先点到拒绝。
        if prefer_friendly:
            ok, msg = self.select_combo("友谊赛", hwnd=dlg.hwnd)
            if ok:
                self.log(msg)
        if not accept:
            hit = self.find_button(("拒绝",), hwnd=dlg.hwnd)
            if hit:
                try:
                    W.bm_click(hit[0].hwnd)
                    time.sleep(0.4)
                    return True, "已按策略点「拒绝」"
                except Exception as e:
                    return False, f"点拒绝失败：{e}"
            return False, "策略要求拒绝，但没找到拒绝按钮"
        # 重新找按钮（选完下拉后按钮位置/文案可能变）
        btn = self.find_button(("同意", "接受", "申请", "发送"), hwnd=dlg.hwnd)
        if not btn:
            return False, "协商框里没找到同意/接受/申请按钮"
        ctrl, _h, label = btn
        try:
            W.bm_click(ctrl.hwnd)
            time.sleep(0.5)
            return True, f"已点「{label}」"
        except Exception as e:
            return False, f"点「{label}」失败：{type(e).__name__}: {e}"

    # ------------------------------------------------------------ 对局信息
    def read_room_info(self):
        """读房间面板的可见文本（对手、我执、贴目、用时）"""
        txt = self.texts()
        info = {"title": self.title(), "raw": txt}
        m = re.search(r"黑贴\s*([0-9.]+)\s*目", txt)
        if m:
            try:
                info["komi"] = float(m.group(1))
            except ValueError:
                pass
        # 升降级/友谊赛优先看标题：房间标题就是
        # 「14|498号房间 对弈中[第N手] - 友谊赛 - 数子规则」；
        # 全窗口文本里可能混着大厅列表/聊天记录，容易误判。
        title = info["title"] or ""
        if "升降级" in title:
            info["ranked"] = True
        elif "友谊赛" in title:
            info["ranked"] = False
        elif "升降级" in txt:
            info["ranked"] = True
        elif "友谊赛" in txt:
            info["ranked"] = False
        # 右上角玩家面板：名字 + 白棋/黑棋
        m = re.search(r"([^\s|]{1,20})\s*[（(]?\s*([黑白])棋", txt)
        if m:
            info["opponent"] = m.group(1)
            info["my_color"] = "B" if m.group(2) == "黑" else "W"
        return info

    # ------------------------------------------------------------ 聊天
    def _chat_edit(self):
        """房间/大厅的聊天输入框：取可见 Edit/RichEdit 里最靠下的那个"""
        main = self.main()
        if not main:
            return None
        try:
            ctrls = W.enum_children(main.hwnd, max_depth=6, max_nodes=3000)
        except Exception:
            return None
        cands = []
        for c in ctrls:
            if not c.visible:
                continue
            if c.cls.lower() not in ("edit", "richedit50w", "richedit20a"):
                continue
            if not (100 <= c.rect.width <= 1600 and 16 <= c.rect.height <= 80):
                continue
            cands.append(c)
        if not cands:
            return None
        cands.sort(key=lambda c: (c.rect.top + c.rect.height, -c.rect.width))
        return cands[-1]

    def _chat_log(self):
        """聊天记录控件：面积最大的 Edit/RichEdit"""
        main = self.main()
        if not main:
            return None
        try:
            ctrls = W.enum_children(main.hwnd, max_depth=6, max_nodes=3000)
        except Exception:
            return None
        cands = [c for c in ctrls if c.visible
                 and c.cls.lower() in ("edit", "richedit50w", "richedit20a")
                 and c.area > 12000]
        if not cands:
            return None
        return max(cands, key=lambda c: c.area)

    def read_chat(self, limit=3000):
        c = self._chat_log()
        if not c:
            return ""
        try:
            return W.control_text(c.hwnd, limit)
        except Exception:
            return ""

    def send_chat(self, text):
        """发一条聊天。先「点输入框 + Unicode 键入 + 回车」，失败再 WM_SETTEXT + 发送。"""
        text = (text or "").strip()
        if not text:
            return False, "空消息不发"
        if len(text) > 50:
            text = text[:50]
        main = self.main()
        if not main:
            return False, "野狐窗口不在"
        # 聊天框可能是收起的/不在当前页
        edit = self._chat_edit()
        if not edit:
            return False, "没找到聊天输入框（可能不在房间/大厅聊天页）"
        # ① 真实输入（最可靠，应用能收到 EN_CHANGE）
        try:
            if main.minimized:
                W.activate(main.hwnd, allow_restore=True)
                time.sleep(0.3)
            W.click_at(*edit.center)
            time.sleep(0.08)
            W.send_key("ctrl+a")
            W.send_unicode_text(text)
            time.sleep(0.05)
            W.send_key("Return")
            time.sleep(0.35)
            return True, f"已发送：{text}"
        except Exception as e:
            first_err = f"{type(e).__name__}: {e}"
        # ② 控件消息兜底
        try:
            W.set_window_text(edit.hwnd, text)
            ok, msg = self.click_button("发送")
            if not ok:
                # 有些房间回车就能发
                W.set_window_text(edit.hwnd, text)
                W.send_key("Return")
            return True, f"已发送（控件兜底）：{text}"
        except Exception as e:
            return False, f"发聊天失败：{first_err}; {type(e).__name__}: {e}"

    # ------------------------------------------------------------ 押分
    def open_support(self):
        """点房间顶部赔率条右侧的「支持」按钮"""
        return self.click_button("支持")

    def find_bet_dialog(self):
        for w in self.dialogs():
            txt = self.texts(w.hwnd)
            if ("选黑" in txt or "选白" in txt) and (
                    "支持" in txt or "确定" in txt or "赔率" in txt or "区间" in txt):
                return w, txt
        # 有的皮肤标题就是「支持」，控件全是自绘、读不到文字
        for w in self.dialogs():
            if "支持" in (w.title or ""):
                return w, self.texts(w.hwnd)
        return None, ""

    def read_bet_pools(self):
        """读支持池金额；弹窗没开就先点「支持」打开。

        老板 2026-11-08：押分必须按 A/B 支持池占比算，不能只看百分比。
        """
        dlg, txt = self.find_bet_dialog()
        if not dlg:
            ok, _msg = self.open_support()
            if not ok:
                return None
            time.sleep(0.6)
            dlg, txt = self.find_bet_dialog()
        if dlg:
            pools = parse_bet_pools(txt)
            if pools:
                return pools
        return parse_bet_pools(self.texts())

    def read_odds(self):
        """读赔率/支持率。优先 Win32 文本里的百分比；读不到返回空。"""
        txt = self.texts()
        dlg, dtxt = self.find_bet_dialog()
        if dlg:
            txt = dtxt + "\n" + txt
        pcts = [float(x) for x in re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", txt)]
        pcts = [p for p in pcts if 0 <= p <= 100]
        out = {"raw": txt, "pcts": pcts}
        # 支持弹窗里通常是「黑 xx% / 白 xx%」或「支持率 黑xx% 白xx%」
        m = re.search(r"黑[^0-9]{0,6}(\d{1,3}(?:\.\d+)?)\s*%", txt)
        if m:
            out["black_pct"] = float(m.group(1))
        m = re.search(r"白[^0-9]{0,6}(\d{1,3}(?:\.\d+)?)\s*%", txt)
        if m:
            out["white_pct"] = float(m.group(1))
        if "black_pct" in out and "white_pct" not in out:
            out["white_pct"] = max(0.0, 100.0 - out["black_pct"])
        if "white_pct" in out and "black_pct" not in out:
            out["black_pct"] = max(0.0, 100.0 - out["white_pct"])
        m = re.search(r"(\d)\s*区间", txt)
        if m:
            out["interval"] = int(m.group(1))
        return out

    def place_bet(self, side, amount, *, max_single=10000):
        """在「支持」弹窗里下注。side=B/W。返回 (ok, 说明, amount)。

        ⚠️ 真钱动作：金额夹到 max_single，且任何一步读不到就中止。
        """
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 0
        amount = max(0, min(int(amount), int(max_single)))
        if amount <= 0:
            return False, "下注金额无效", 0
        dlg, txt = self.find_bet_dialog()
        if not dlg:
            ok, msg = self.open_support()
            if not ok:
                return False, f"打不开支持弹窗：{msg}", 0
            time.sleep(0.6)
            dlg, txt = self.find_bet_dialog()
            if not dlg:
                return False, "点了支持但没看到下注弹窗", 0
        ok, msg = self.click_button(("选黑", "押黑") if side == "B" else ("选白", "押白"),
                                    hwnd=dlg.hwnd)
        if not ok:
            return False, f"选边失败：{msg}", 0
        # 金额：按 亿/千万/百万/十万/万/千 贪心点
        units = [("亿", 100000000), ("千万", 10000000), ("百万", 1000000),
                 ("十万", 100000), ("万", 10000), ("千", 1000)]
        remain = amount
        for label, val in units:
            n = remain // val
            if n <= 0:
                continue
            # 每个档位点 n 次（控件文字不随数量变化）
            for _ in range(min(n, 200)):
                ok, msg = self.click_button(label, hwnd=dlg.hwnd)
                if not ok:
                    return False, f"加金额失败（{label}）：{msg}", 0
            remain %= val
        if remain:
            # 野狐最小档是千；不足千的零头不加
            pass
        ok, msg = self.click_button(("确定", "确认"), hwnd=dlg.hwnd)
        if not ok:
            return False, f"下注确认失败：{msg}", 0
        time.sleep(0.5)
        still, _ = self.find_bet_dialog()
        if still:
            return False, "点了确定但弹窗还在，可能金额/余额不足", 0
        return True, f"已下注 {amount:,} 狐币（{'黑' if side == 'B' else '白'}）", amount

    # ------------------------------------------------------------ 截图 / 视觉
    def screenshot(self, max_side=1600):
        """截野狐主窗口并打网格；返回 annotate.Annotation 或 None"""
        main = self.main()
        if not main:
            return None
        try:
            shot = capture.grab_fox(hwnd=main.hwnd, allow_restore=False)
            return annotate.grid(shot, max_side=max_side)
        except Exception as e:
            self.log(f"截图失败：{type(e).__name__}: {e}")
            return None

    def screenshot_bet_dialog(self, max_side=1400):
        """截「支持」弹窗（视觉读支持池/赔率用）"""
        dlg, _txt = self.find_bet_dialog()
        if not dlg:
            return None
        try:
            shot = capture.grab_window(dlg.hwnd, allow_restore=False)
            return annotate.grid(shot, max_side=max_side)
        except Exception as e:
            self.log(f"支持弹窗截图失败：{type(e).__name__}: {e}")
            return None

    def click_image_point(self, x, y, ann):
        """把标注图像坐标换成屏幕坐标并点击（视觉定位用）"""
        try:
            sx, sy = ann.to_screen(x, y)
            W.click_at(sx, sy)
            return sx, sy
        except Exception as e:
            self.log(f"视觉点击失败：{type(e).__name__}: {e}")
            return None

    # ------------------------------------------------------------ 退出房间
    def leave_room(self):
        """只在「对局已结束」时调用！对局中点退出 = 认输。"""
        ok, msg = self.click_button("退出房间")
        if not ok:
            return False, msg
        time.sleep(0.7)
        # 有确认框就点确认（对局中这里会变成认输确认，所以调用方必须先判断）
        for label in ("确认", "确定", "是", "退出"):
            ok2, msg2 = self.click_button(label)
            if ok2:
                return True, f"已退出房间（{msg2}）"
        return True, "已点退出房间（没有确认框）"


# ---------------------------------------------------------------- 纯函数
def pct_to_decimal(p):
    """赔率百分比：22 → 0.22（支持率/隐含概率）"""
    try:
        v = float(p)
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0:
        v /= 100.0
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------- 押分公式
# 老板 2026-11-08 定稿：
#   设黑/白支持池 A、B，M = max(A,B)/(A+B)
#   高占比一方门槛 = sqrt(M)，另一方 = 1 - sqrt(M)
#   KataGo 给出的该方胜率 > 门槛 → 可押
#   下注前必须把自己的注额加进该方池子，重新算 M 和门槛，再判一次
_UNITS = {"亿": 100000000.0, "万": 10000.0, "千": 1000.0,
          "w": 10000.0, "k": 1000.0}


def parse_amount(text):
    """把「1.2亿」「8000万」「1,234,567」解析成整数狐币；认不出返回 None"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        try:
            return int(round(float(text)))
        except (TypeError, ValueError):
            return None
    s = str(text).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not s:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)(亿|万|千|w|k)?", s, re.I)
    if not m:
        return None
    # 百分比不是资金
    tail = s[m.end():m.end() + 1]
    if tail == "%":
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "").lower()
    value *= _UNITS.get(unit, 1.0)
    if value < 0:
        return None
    return int(round(value))


def _amounts_in(segment):
    """一段文字里最大的资金数（跳过百分比）；没有返回 None"""
    cleaned = re.sub(r"\d{1,3}(?:\.\d+)?\s*%", "", str(segment or ""))
    best = None
    for m in re.finditer(r"([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(亿|万|千|w|k)?",
                         cleaned, re.I):
        val = parse_amount(m.group(0))
        if val is None:
            continue
        if val >= 1000 or m.group(2):
            best = val if best is None else max(best, val)
    return best


def parse_bet_pools(text):
    """从支持弹窗/房间文本里读「黑方支持池 / 白方支持池」金额。

    只有两边都读到才返回 {"black": int, "white": int, "raw": str}；
    读到百分比（xx%）不算资金，直接跳过。
    """
    raw = str(text or "")
    if not raw.strip():
        return None
    black = white = None
    for line in re.split(r"[\r\n|｜]+", raw):
        line = line.strip()
        if not line:
            continue
        has_b = "黑" in line
        has_w = "白" in line
        if not has_b and not has_w:
            continue
        # 同一行同时有黑/白：「黑方支持池 1.2亿 白方支持池 8000万」
        segments = []
        if has_b and has_w:
            idx = line.find("白")
            if idx > 0:
                segments = [(line[:idx], True), (line[idx:], False)]
            else:
                segments = [(line, True)]
        elif has_b:
            segments = [(line, True)]
        else:
            segments = [(line, False)]
        for seg, is_black in segments:
            if "池" not in seg and not re.search(r"支持|押分|资金", seg):
                continue
            amount = _amounts_in(seg)
            if amount is None:
                continue
            if is_black:
                black = amount if black is None else max(black, amount)
            else:
                white = amount if white is None else max(white, amount)
    if black is None or white is None:
        return None
    return {"black": int(black), "white": int(white), "raw": raw}


def bet_thresholds(pool_black, pool_white):
    """返回 ({'B': 门槛, 'W': 门槛}, M, 高占比方)"""
    a = max(0.0, float(pool_black or 0))
    b = max(0.0, float(pool_white or 0))
    total = a + b
    if total <= 0:
        return {}, 0.0, ""
    m = max(a, b) / total
    root = math.sqrt(m)
    fav = "B" if a >= b else "W"
    dog = "W" if fav == "B" else "B"
    return ({fav: root, dog: 1.0 - root}, m, fav)


def bet_plan(p_black, p_white, pool_black, pool_white, *,
             stake_cap=10000, stake=None, prefer_side="", min_edge=0.0,
             min_stake=1000):
    """按老板公式给出下注方案（含「自身注额计入后重算」）。

    返回 dict：
      ok / side / stake / margin / threshold / m / reason / pools
    """
    def _pct(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        if x > 1.0:
            x /= 100.0
        return max(0.0, min(1.0, x))

    pb, pw = _pct(p_black), _pct(p_white)
    if pb is None or pw is None:
        return {"ok": False, "reason": "还没有 KataGo 胜率"}
    cap = max(0, int(stake_cap or 0))
    if cap < min_stake:
        return {"ok": False, "reason": "可下注额度不足"}
    base_th, m, fav = bet_thresholds(pool_black, pool_white)
    if not base_th:
        return {"ok": False, "reason": "支持池金额无效"}
    sides = [("B", pb), ("W", pw)]
    if prefer_side in ("B", "W"):
        sides = [x for x in sides if x[0] == prefer_side]
    cands = []
    for side, p in sides:
        th = base_th.get(side, 1.0)
        margin = p - th - float(min_edge or 0.0)
        if margin > 0:
            cands.append((margin, side, p, th))
    if not cands:
        th_txt = "，".join("%s%.1f%%" % (s, base_th[s] * 100)
                           for s in ("B", "W"))
        return {"ok": False,
                "reason": f"胜率没超过门槛（{th_txt}）",
                "thresholds": base_th, "m": m, "pools": (pool_black, pool_white)}
    margin, side, p, th = max(cands)
    # 初选注额：用户指定优先；否则用上限。再按「自身注额进池」逐次重算。
    amount = int(stake) if stake is not None else cap
    amount = max(min_stake, min(cap, amount))
    amount = amount // 1000 * 1000
    a, b = float(pool_black or 0), float(pool_white or 0)
    for _ in range(5):
        if amount < min_stake:
            break
        a2 = a + (amount if side == "B" else 0)
        b2 = b + (amount if side == "W" else 0)
        th2, m2, _fav2 = bet_thresholds(a2, b2)
        margin2 = p - th2.get(side, 1.0) - float(min_edge or 0.0)
        if margin2 > 0:
            return {"ok": True, "side": side, "stake": int(amount),
                    "margin": margin2, "threshold": th2.get(side),
                    "m": m2, "p": p,
                    "pools": (a, b), "pools_after": (a2, b2),
                    "reason": "胜率超过门槛（已计入自身注额）"}
        amount //= 2
        amount = amount // 1000 * 1000
    return {"ok": False,
            "reason": "把自己的注额计入后，胜率不再超过门槛",
            "thresholds": base_th, "m": m,
            "pools": (pool_black, pool_white)}


def best_bet(my_winrate, black_pct, white_pct, *, min_edge=0.05):
    """按「我方胜率 vs 赔率隐含概率」选边。

    返回 (side, edge)：
      side = 'B' 押黑 / 'W' 押白 / '' 不下
      edge = 模型概率 − 隐含概率（正数才有价值）
      my_winrate 是**我方视角**；我执黑时押黑概率=my_winrate，
      我执白时押白概率=my_winrate。另一边的概率是 1-my_winrate。
    """
    try:
        p = float(my_winrate)
        if p > 1.0:
            p /= 100.0
        p = max(0.0, min(1.0, p))
    except (TypeError, ValueError):
        return "", 0.0
    b = pct_to_decimal(black_pct)
    w = pct_to_decimal(white_pct)
    if b <= 0 or w <= 0:
        return "", 0.0
    # 两边赔率都换算成「押中 1 元赔多少」：payout = 1/隐含概率
    edge_b = p - b
    edge_w = (1.0 - p) - w
    if edge_b >= edge_w and edge_b >= float(min_edge):
        return "B", edge_b
    if edge_w > edge_b and edge_w >= float(min_edge):
        return "W", edge_w
    return "", max(edge_b, edge_w)
