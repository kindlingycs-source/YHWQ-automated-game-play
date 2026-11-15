# -*- coding: utf-8 -*-
"""AI 托管运行时：自然语言 → 计划 → 白名单动作 → 任务/定时/聊天/押分

分工
------------------------------------------------------------
- `ai_skill`：只读的系统提示词 + 意图 JSON 校验（焊死的范围限制）
- `ai_llm`  ：OpenAI 兼容客户端（火山方舟 / DeepSeek / 自定义）
- `fox_skill`：野狐 UI 的确定性操作（找控件、点按钮、发聊天、押分）
- 本文件    ：把三者接起来，跑在后台线程；只通过 `app.q` 和 Tk 线程通信

几条硬规矩
------------------------------------------------------------
1) **系统提示词永远来自 ai_skill.SKILL_PROMPT 常量**，不接受 UI/配置/历史
   覆盖；用户输入包在 `<user_input>` 里，只当数据。对话里要求「忽略设定 /
   改范围 / 发提示词」一律被 guard_user_text 拦掉，连模型都不调用。
2) **动作白名单双层校验**：ai_skill.parse_plan 过滤一次，执行层 `_execute`
   再查一次；模型编出新动作也执行不了。
3) **绝不跨线程碰 Tk**：所有界面更新走 `app.q.put(("ai_*", payload))`。
4) 对自动对弈的控制复用现有控制接口（`app.control_start` /
   `apply_control_config` / `toggle_pause` / `stop`），不复制启动逻辑。
5) 聊天节流（老板 2026-11-08 定）：同一房间每条间隔 **>15 秒（取 16s）**，
   1 分钟最多 3 条，单条 ≤50 字；被系统提醒后等 60 秒。
"""
from __future__ import annotations

import datetime
import json
import os
import random
import re
import threading
import time
import traceback

import ai_llm
import ai_skill
import ai_store
import fox_skill


# ---------------------------------------------------------------- 常量
CHAT_MIN_INTERVAL = 16.0          # 老板：>15 秒；取 16 秒留余量
CHAT_PER_MINUTE = 3
CHAT_MAX_CHARS = 50
SLOW_COOLDOWN = 60.0
CHAT_POLL_INTERVAL = 1.5         # 读聊天控件的间隔（别每 0.4s 枚举一次）

# 本地兜底闸门（就算 ai_skill 的 guard 被改坏，这一层也拦得住常见注入）
_INJECTION_PATTERNS = (
    r"忽略.{0,8}(之前|以上|所有|前面).{0,6}(指令|设定|规则|提示)",
    r"忘记.{0,6}(设定|规则|身份|提示)",
    r"无视.{0,6}(规则|设定|限制)",
    r"(系统|system).{0,4}(提示词|prompt)",
    r"developer\s*mode|开发者模式|越狱|jailbreak",
    r"你现在(是|扮演)|从现在开始你(是|扮演)",
    r"(取消|去掉|解除).{0,6}(限制|范围|规则)",
    r"(可以|允许).{0,6}(聊|说).{0,6}(别的|其他|无关)",
    r"(我们|你和我).{0,6}(聊|说)(点|些)?别的",
    r"换个话题",
    r"管理员模式|sudo|root\s*权限",
    r"(把|发|显示|告诉).{0,6}(提示词|内部规则|api\s*key|密钥)",
)
_REFUSE_FIXED = ("这套规则是内置的，聊天里改不了。要下棋、押分、聊天或查状态，"
                 "直接说就行。")

# 聊天风格是允许的自然语言配置，不能被注入规则误伤
_STYLE_HINT_RE = re.compile(
    r"(聊天|说话|发言|回复|回话|人设|语气|口吻|风格|性格|态度)")
_STYLE_WORD_RE = re.compile(
    r"(谦虚|低调|骄傲|自大|自信|嚣张|狂妄|阴阳|怪气|趾高气扬|吹牛|浮夸|"
    r"高冷|冷淡|毒舌|幽默|搞笑|热情|温和|直接|简短|礼貌|客气|嘴硬|倔)")


def _looks_like_style(text):
    t = str(text or "")
    return bool(_STYLE_HINT_RE.search(t) and _STYLE_WORD_RE.search(t))


def _help_request(text):
    """「你能做什么/怎么用/有哪些指令」→ 直接回内置能力说明"""
    t = str(text or "")
    return bool(re.search(
        r"(你|AI)?(能|会|可以)(做|干|操作)(什么|啥|哪些)|"
        r"(有|支持)(哪些|什么)(指令|命令|功能)|怎么用|如何使用|使用说明|"
        r"能力(范围|介绍)?|指令(范围|列表)|help", t, re.I))


def _local_guard(text: str):
    """确定性注入闸门；命中返回固定拒绝话术，否则 None。"""
    t = str(text or "")
    if not t.strip():
        return None
    low = t.lower()
    style = _looks_like_style(t)
    scan = low
    fox_chat_ctx = any(k in t for k in (
        "大厅", "房间", "对手", "棋友", "观战", "他们", "玩家", "聊天区", "对局室"))
    if fox_chat_ctx and not re.search(r"你和我|我们|指令|规则|范围|限制", low):
        scan = re.sub(r"(?:可以|能|允许)?聊(?:点|些)?别的|换个话题", "", scan)
    for pat in _INJECTION_PATTERNS:
        if re.search(pat, scan, re.I):
            if style and not re.search(
                    r"忽略|提示词|内部规则|限制|开发者|越狱|密钥|api[\s_-]*key",
                    low):
                continue
            return _REFUSE_FIXED
    return None


class ChatThrottle:
    """房间聊天节流：16 秒一条 + 每分钟 3 条 + 被提醒后冷却 60 秒。"""

    def __init__(self, min_interval=CHAT_MIN_INTERVAL,
                 per_minute=CHAT_PER_MINUTE):
        self.min_interval = float(min_interval)
        self.per_minute = int(per_minute)
        self.sent = []
        self.last = 0.0
        self.cooldown_until = 0.0

    def can_send(self, now=None):
        now = time.time() if now is None else float(now)
        if now < self.cooldown_until:
            return False, f"冷却中（{self.cooldown_until - now:.0f}s）"
        if now - self.last < self.min_interval:
            return False, f"距上一条不足 {self.min_interval:.0f}s"
        recent = [t for t in self.sent if now - t < 60.0]
        if len(recent) >= self.per_minute:
            return False, "这一分钟已经 3 条"
        return True, ""

    def mark(self, now=None):
        now = time.time() if now is None else float(now)
        self.last = now
        self.sent.append(now)
        self.sent = [t for t in self.sent if now - t < 120.0]

    def on_slow(self, now=None):
        now = time.time() if now is None else float(now)
        self.cooldown_until = max(self.cooldown_until, now + SLOW_COOLDOWN)

    def reset(self):
        self.sent = []
        self.last = 0.0
        self.cooldown_until = 0.0


class _FakeLLM:
    """测试用：按顺序返回预设文本。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, json_mode=False, image_jpeg=None,
             max_tokens=None):
        self.calls.append({"messages": messages, "json_mode": json_mode,
                           "image": image_jpeg})
        if not self.replies:
            raise ai_llm.LLMError("no reply")
        return self.replies.pop(0)


class AIAgent:
    """AI 托管的单例运行时（由 gui.App 持有）。"""

    def __init__(self, app, *, settings=None, llm_factory=None):
        self.app = app
        self._lock = threading.RLock()
        self.settings = settings or ai_store.load_ai()
        self.llm = None
        self.fox = fox_skill.FoxSkill(log=self._log)
        self._llm_factory = llm_factory

        self._thread = None
        self._stop = threading.Event()
        self._hover = threading.Event()
        self._inbox = []
        self._busy = False
        self._state = "idle"          # idle / thinking / running / paused / error
        self._state_text = "待命"
        self._active_task = None
        self._tasks = list(self.settings.get("tasks") or [])
        # 程序重启后没跑完的任务继续（时长任务按绝对 ends_at 续）
        for _t in self._tasks:
            if _t.get("status") == "active":
                self._active_task = _t
                break
        self._history = list(self.settings.get("history") or [])[-40:]
        self._chat = ChatThrottle()
        self._chat_raw = ""
        self._chat_seen = set()
        self._sent_chat = set()
        self._chat_room = ""
        self._chat_phase = ""
        self._chat_vision_at = 0.0
        self._chat_lines = []
        self._pending_reply = ""
        self._chat_poll_at = 0.0
        self._opening_sent = False
        self._post_game_sent = False
        self._seen_running = False
        self._running_since = 0.0
        self._last_request = 0.0
        self._drive_after = 0.0
        self._pending_leave_at = 0.0
        self._bet_spent = 0
        self._bet_day = ""
        self._tasks_emit_at = 0.0
        self._last_status = {}
        self._log_lines = []

        self._host = dict(self.settings.get("host") or {})
        self._host.setdefault(
            "chat_style",
            ai_skill.DEFAULT_POLICY.get("chat_style", "自然、礼貌、偶尔幽默"))
        self._make_llm()

    # ============================================================ 配置
    def _make_llm(self):
        llm_cfg = dict(self.settings.get("llm") or {})
        key = str(llm_cfg.get("api_key") or "").strip()
        if self._llm_factory is not None:
            self.llm = self._llm_factory()
            return self.llm
        if not key:
            self.llm = None
            return None
        self.llm = ai_llm.LLMClient(
            base_url=llm_cfg.get("base_url") or ai_store.DEFAULT_SETTINGS["llm"]["base_url"],
            api_key=key,
            model=llm_cfg.get("model") or ai_store.DEFAULT_SETTINGS["llm"]["model"],
            temperature=float(llm_cfg.get("temperature", 0.3)),
            max_tokens=int(llm_cfg.get("max_tokens", 1500)),
            timeout=float(llm_cfg.get("timeout", 90)),
        )
        return self.llm

    @property
    def api_ready(self):
        return self.llm is not None

    def configure_llm(self, base_url, model, api_key, *, temperature=None,
                      max_tokens=None, vision=None):
        """保存 API 配置（GUI 线程调用）。返回 (ok, msg)。"""
        base_url = str(base_url or "").strip().rstrip("/")
        model = str(model or "").strip()
        api_key = str(api_key or "").strip()
        if not base_url.startswith(("http://", "https://")):
            return False, "base_url 要以 http:// 或 https:// 开头"
        if not model:
            return False, "模型名不能为空"
        with self._lock:
            llm = dict(self.settings.get("llm") or {})
            llm.update({"base_url": base_url, "model": model,
                        "api_key": api_key})
            if temperature is not None:
                llm["temperature"] = float(temperature)
            if max_tokens is not None:
                llm["max_tokens"] = int(max_tokens)
            if vision is not None:
                llm["vision"] = bool(vision)
            self.settings["llm"] = llm
            ok, msg = ai_store.save_ai(self.settings)
            self._make_llm()
        self._state_text = "API 已配置" if api_key else "未配置 API"
        return ok, msg or "已保存"

    def test_llm(self):
        """测试连接（后台线程调用，返回模型回复或抛异常）"""
        if self.llm is None:
            raise ai_llm.LLMError("还没有配置 API key")
        return self.llm.test()

    # ============================================================ 生命周期
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ai-host",
                                        daemon=True)
        self._thread.start()
        self._set_state("idle", "托管待命")
        self._emit_tasks()

    def stop(self, reason="已停止"):
        self._stop.set()
        self._inbox.clear()
        self._set_state("idle", reason)
        self._log(reason)

    def shutdown(self):
        self.stop("程序退出")
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def cancel_task(self):
        """GUI 上的「取消当前任务」"""
        self._finish_task("用户取消", stop_engine=True)
        self._emit_tasks()

    def set_hover(self, inside):
        """鼠标进入/离开 AI 悬浮球：暂停/恢复 Agent 动作"""
        if inside:
            self._hover.set()
            if self._state in ("running", "thinking"):
                self._set_state("paused", "鼠标悬停·暂停")
        else:
            self._hover.clear()
            if self._state == "paused":
                self._set_state("running" if self._active_task else "idle",
                                "继续")

    # ============================================================ 对外
    def send(self, text):
        """GUI 线程调用：用户发一句话。"""
        text = str(text or "").strip()
        if not text:
            return
        self.start()
        self._emit_msg("user", text)
        with self._lock:
            self._inbox.append(text)
        self._set_state("thinking", "思考中…")

    def task_snapshot(self):
        with self._lock:
            return {"tasks": [dict(t) for t in self._tasks],
                    "active": (self._active_task or {}).get("id")}

    def policy_snapshot(self):
        with self._lock:
            return dict(self._host)

    def status_snapshot(self):
        with self._lock:
            return {"state": self._state, "text": self._state_text,
                    "active": (self._active_task or {}).get("id"),
                    "api_ready": self.api_ready}

    # ============================================================ 线程
    def _loop(self):
        self._log("AI 托管线程已启动")
        while not self._stop.is_set():
            try:
                if self._hover.is_set():
                    time.sleep(0.2)
                    continue
                text = None
                with self._lock:
                    if self._inbox:
                        text = self._inbox.pop(0)
                if text is not None:
                    self._handle_user(text)
                    continue
                self._tick_tasks()
                self._tick_chat()
                self._emit_tasks_if_due()
            except Exception as e:
                self._log(f"运行时异常：{type(e).__name__}: {e}")
                try:
                    self._log(traceback.format_exc().strip().split("\n")[-2])
                except Exception:
                    pass
            time.sleep(0.4)
        self._log("AI 托管线程已退出")

    # ============================================================ 对话
    def _handle_user(self, text):
        self._busy = True
        self._set_state("thinking", "思考中…")
        guard = self._guard(text)
        if guard:
            self._append_history("user", text)
            self._append_history("assistant", guard)
            self._emit_msg("assistant", guard)
            self._set_state("idle", "已拒绝越界请求")
            self._busy = False
            return

        # 「你能做什么/怎么用」直接给内置说明，不花 tokens、也不会被模型改写
        if _help_request(text):
            help_text = getattr(ai_skill, "HELP_TEXT", "")
            if help_text:
                self._append_history("user", text)
                self._append_history("assistant", help_text)
                self._emit_msg("assistant", help_text)
                self._set_state("idle", "能力说明")
                self._busy = False
                return

        if self.llm is None:
            msg = ("还没配置大模型 API。点右上角「API 设置」填火山方舟的 key，"
                   "或者直接告诉我「跳过」，先手动下棋。")
            self._emit_msg("assistant", msg)
            self._set_state("idle", "待配置 API")
            self._busy = False
            return

        plan = self._plan(text)
        if plan is None:
            self._set_state("idle", "这句话没听懂")
            self._busy = False
            return

        reply = plan.get("reply") or "收到。"
        self._append_history("user", text)
        self._append_history("assistant", reply)
        self._emit_msg("assistant", reply)

        with self._lock:
            if plan.get("policy"):
                self._apply_policy(plan["policy"])
            if plan.get("task"):
                self._add_task(plan["task"])
                self._emit_tasks()

        for action in plan.get("actions") or []:
            if self._stop.is_set():
                break
            if action.get("name") not in ai_skill.ALLOWED_ACTIONS:
                self._log(f"拒绝白名单外动作：{action.get('name')}")
                continue
            try:
                self._execute(action["name"], action.get("args") or {})
            except Exception as e:
                self._log(f"动作 {action.get('name')} 失败："
                          f"{type(e).__name__}: {e}")

        self._save_settings()
        with self._lock:
            if self._active_task or self._status().get("running"):
                self._set_state("running", "托管中")
            else:
                self._set_state("idle", "待命")
        self._busy = False

    def _guard(self, text):
        """双层闸门：ai_skill.guard_user_text + 本地兜底"""
        g = _local_guard(text)
        if g:
            self._log("本地闸门拦下越界请求")
            return g
        fn = getattr(ai_skill, "guard_user_text", None)
        if callable(fn):
            try:
                g2 = fn(text)
                if g2:
                    self._log("提示词闸门拦下越界请求")
                    return str(g2)
            except Exception as e:
                self._log(f"guard_user_text 出错（忽略）：{e}")
        return None

    def _plan(self, text):
        try:
            messages = ai_skill.build_planner_messages(
                text, self._context_text(), self._history)
            raw = self.llm.chat(messages, json_mode=True)
            data = _loads_json(raw)
            return ai_skill.parse_plan(data)
        except Exception as e:
            self._log(f"规划失败：{type(e).__name__}: {e}")
            self._emit_msg("assistant", f"大模型调用失败：{e}")
            return None

    def _context_text(self):
        st = self._status()
        lines = [
            f"当前时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            f"野狐：{self.fox.title() or '没在运行/没找到窗口'}",
            ("自动对弈：状态={state} 运行={running} 暂停={paused} "
             "我执={my_color} 第{move_no}手 末手={last_move} "
             "胜率={winrate} 目差={score_lead}").format(
                state=st.get("state"), running=st.get("running"),
                paused=st.get("paused"), my_color=st.get("my_color"),
                move_no=st.get("move_no"), last_move=st.get("last_move"),
                winrate=_pct(st.get("winrate")), score_lead=st.get("score_lead")),
            "当前策略：" + ai_skill.summarize_policy(self._host),
        ]
        active = self._active_task
        if active:
            lines.append("当前任务：" + _task_text(active))
        if self._tasks:
            lines.append("任务队列：" + "；".join(
                _task_text(t, short=True) for t in self._tasks[-6:]))
        return "\n".join(lines)

    # ============================================================ 状态
    def _status(self):
        try:
            cs = getattr(self.app, "control_state", None)
            if cs is not None:
                snap = cs.snapshot()
                if isinstance(snap, dict):
                    self._last_status = snap
                    return snap
        except Exception:
            pass
        return dict(self._last_status)

    def _set_state(self, state, text):
        self._state = state
        self._state_text = text
        self._emit("ai_state", {"state": state, "text": text,
                                "api_ready": self.api_ready})

    def _emit(self, kind, payload):
        try:
            q = getattr(self.app, "q", None)
            if q is not None:
                q.put((kind, payload))
                return
        except Exception:
            pass
        self._log(f"[{kind}] {payload}")

    def _emit_msg(self, role, text):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._emit("ai_msg", {"role": role, "text": str(text), "ts": ts})
        self._log(f"{'我' if role == 'user' else 'AI'}：{text}")

    def _log(self, msg):
        line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
        self._log_lines.append(line)
        del self._log_lines[:-200]
        self._emit("ai_log", msg)

    # ============================================================ 动作
    def _execute(self, name, args):
        if name not in ai_skill.ALLOWED_ACTIONS:
            raise ValueError(f"不允许的动作：{name}")
        handler = getattr(self, "_act_" + name.replace(".", "_"), None)
        if handler is None:
            raise ValueError(f"动作没有实现：{name}")
        return handler(args or {})

    # ---- 野狐 / 自动对弈
    def _act_fox_launch(self, args):
        ok, msg = self.fox.ensure_fox()
        self._log(msg)
        if ok:
            self.fox.ensure_lobby(timeout=10)

    def _act_autoplay_start(self, args):
        self._start_autoplay(args.get("config") or {})

    def _act_autoplay_stop(self, args):
        self._ui(lambda: self.app.stop(), 10)
        self._log("已关闭自动对弈引擎")

    def _act_autoplay_pause(self, args):
        st = self._status()
        if st.get("running") and not st.get("paused"):
            self._ui(lambda: self.app.toggle_pause(), 8)
            self._log("已暂停落子")

    def _act_autoplay_resume(self, args):
        st = self._status()
        if st.get("running") and st.get("paused"):
            self._ui(lambda: self.app.toggle_pause(), 8)
            self._log("已继续落子")

    def _act_autoplay_set(self, args):
        allowed = {"my_color", "komi", "rules", "engine_key", "think_mode",
                   "think", "think_min", "think_max", "visits"}
        vals = {k: v for k, v in (args or {}).items() if k in allowed}
        if not vals:
            return
        applied = self._ui(lambda: self.app.apply_control_config(vals), 10)
        self._log(f"自动对弈参数已更新：{applied}")

    def _act_policy_set(self, args):
        with self._lock:
            self._apply_policy(args)
        self._log("策略已更新：" + ai_skill.summarize_policy(self._host))

    def _act_status_get(self, args):
        st = self._status()
        self._emit_msg("assistant", (
            f"自动对弈：{st.get('state')}；我执{st.get('my_color')}；"
            f"第 {st.get('move_no')} 手；胜率 {_pct(st.get('winrate'))}；"
            f"野狐窗口：{self.fox.title() or '未运行'}"))

    def _act_invite_accept(self, args):
        ok, msg = self.fox.accept_invite()
        self._log(msg)
        if ok:
            time.sleep(0.8)
            ok2, msg2 = self.fox.negotiate(prefer_friendly=True)
            self._log(msg2)

    def _act_match_request(self, args):
        ok, msg = self.fox.request_match()
        if not ok:
            self._log(f"按控件点不到「自动申请对局」，改用视觉：{msg}")
            self._vision_click("自动申请对局")
        else:
            self._log(msg)

    def _act_match_cancel(self, args):
        ok, msg = self.fox.cancel_match()
        self._log(msg)

    def _act_chat_send(self, args):
        text = str(args.get("text") or "").strip()
        if not text:
            return
        self._chat_say(text, kind="manual")

    def _act_chat_auto(self, args):
        on = bool(args.get("enabled", True))
        with self._lock:
            self._host["chat_auto"] = on
        self._save_settings()
        self._log(f"自动聊天：{'开' if on else '关'}")

    def _act_game_leave(self, args):
        # 只允许在没在跑引擎（对局已结束）时退房，避免误认输
        st = self._status()
        if st.get("running"):
            self._log("对局还在进行，拒绝退出房间（=认输）")
            return
        ok, msg = self.fox.leave_room()
        self._log(msg)

    def _act_scoring_decide(self, args):
        self._log("数子策略：赢了接受；胜率≥5% 被判负则拒绝（按钮需人工确认）")

    # ---- 任务
    def _act_task_cancel(self, args):
        with self._lock:
            tid = str(args.get("id") or "")
            if self._active_task and (not tid or self._active_task["id"] == tid):
                self._finish_task("用户取消", stop_engine=True)
            self._tasks = [t for t in self._tasks if t.get("id") != tid
                           or t.get("status") == "done"]
        self._emit_tasks()

    def _act_task_list(self, args):
        if not self._tasks:
            self._emit_msg("assistant", "现在没有托管任务。")
            return
        self._emit_msg("assistant", "\n".join(_task_text(t) for t in self._tasks))

    # ---- 押分
    def _act_bet_auto(self, args):
        on = bool(args.get("enabled", True))
        cap = args.get("max_single")
        with self._lock:
            self._host["bet_auto"] = on
            if cap is not None:
                try:
                    self._host["bet_max_single"] = max(
                        1000, min(200000000, int(cap)))
                except (TypeError, ValueError):
                    pass
        self._save_settings()
        self._log(f"自动押分：{'开' if on else '关'}，单注上限 "
                  f"{self._host.get('bet_max_single', 10000):,}")

    def _act_bet_place(self, args):
        self._place_bet(args)

    # ============================================================ 自动对弈
    def _start_autoplay(self, extra=None):
        st = self._status()
        if st.get("running"):
            return True
        vals = {}
        for k in ("my_color", "komi", "rules", "engine_key", "think_mode",
                  "think", "think_min", "think_max", "visits"):
            if k in self._host and self._host[k] not in (None, ""):
                vals[k] = self._host[k]
        # 房间面板能读到我执/贴目时，以真实对局为准（老板 2026-11-06 的修正）
        if "my_color" not in vals:
            # 先信自动对弈页当前设置（用户亲手选的），再信房间面板
            try:
                cur = self._ui(lambda: self.app._my_color(), 5)
                if cur in ("B", "W"):
                    vals["my_color"] = cur
            except Exception:
                pass
        try:
            info = self.fox.read_room_info()
        except Exception:
            info = {}
        if "my_color" not in vals and info.get("my_color") in ("B", "W"):
            vals["my_color"] = info["my_color"]
        if "komi" not in vals and info.get("komi") is not None:
            vals["komi"] = info["komi"]
        if info.get("ranked") and not self._host.get("allow_ranked", False):
            self._log("这是升降级/排位局，但策略 allow_ranked=false，不自动开引擎")
            self._emit_msg("assistant", "这局是升降级/排位，你之前没允许排位。"
                                        "要下就说「允许升降级」。")
            return False
        vals.update(extra or {})
        if vals:
            try:
                self._ui(lambda: self.app.apply_control_config(vals), 10)
            except Exception as e:
                self._log(f"写自动对弈参数失败：{e}")
        try:
            res = self._ui(lambda: self.app.control_start(), 25)
        except Exception as e:
            self._log(f"启动自动对弈失败：{type(e).__name__}: {e}")
            return False
        if isinstance(res, dict) and res.get("ok"):
            self._log("自动对弈已启动（引擎在后台热身）")
            self._seen_running = True
            return True
        why = (res or {}).get("error") if isinstance(res, dict) else str(res)
        self._log(f"自动对弈没启动：{why}")
        self._emit_msg("assistant", f"引擎没启动：{why}")
        return False

    def _ui(self, fn, timeout=8.0):
        """后台线程 → Tk 线程（复用控制接口的通道）"""
        call = getattr(self.app, "_control_call", None)
        if callable(call):
            return call(fn, timeout=timeout)
        return fn()

    def _stop_engine(self):
        """关引擎：Tk 线程直接调，后台线程走 _control_call。

        ⚠️ 从 AI 页「取消当前任务」进来时就在 Tk 线程上；如果再
        _control_call 投队列等自己，会死等 10 秒（2026-11-08 自查）。
        """
        if threading.current_thread() is threading.main_thread():
            try:
                self.app.stop()
            except Exception as e:
                self._log(f"关引擎失败：{e}")
            return
        try:
            self._ui(lambda: self.app.stop(), 10)
        except Exception as e:
            self._log(f"关引擎失败：{e}")

    # ============================================================ 任务
    def _add_task(self, spec):
        spec = dict(spec or {})
        now = time.time()
        task = {
            "id": "t" + datetime.datetime.now().strftime("%H%M%S") + str(random.randint(10, 99)),
            "type": spec.get("type") or "duration",
            "goal": spec.get("goal") or "play",
            "minutes": int(spec.get("minutes") or 0),
            "count": int(spec.get("count") or 0),
            "at": spec.get("at") or "",
            "days": list(spec.get("days") or range(7)),
            "note": spec.get("note") or "",
            "status": "active",
            "created_at": now,
            "ends_at": 0.0,
            "games_done": 0,
            "last_run": "",
        }
        if task["type"] == "duration":
            if task["minutes"] <= 0:
                task["minutes"] = 30
            task["ends_at"] = now + task["minutes"] * 60
        elif task["type"] == "games":
            if task["count"] <= 0:
                task["count"] = 1
        elif task["type"] == "daily":
            task["status"] = "pending"
        # 同一时间只跑一个：新任务顶掉旧的
        if task["status"] == "active":
            if self._active_task:
                self._finish_task("被新任务替换", stop_engine=False)
            self._active_task = task
            self._seen_running = False
            self._opening_sent = False
            self._post_game_sent = False
            self._chat.reset()
        self._tasks.append(task)
        self._tasks = [t for t in self._tasks if t.get("status") != "done"][-12:]
        self._log("新任务：" + _task_text(task))

    def _tick_tasks(self):
        now = time.time()
        with self._lock:
            task = self._active_task
        if task is None:
            with self._lock:
                self._maybe_start_daily(now)
                task = self._active_task
        if task is None:
            return
        # 到时 / 到盘数 —— 注意不要拿着锁去调 _finish_task/_ui，
        # 否则关引擎要等 Tk 线程，会把整个 Agent 卡住。
        if task["type"] == "duration" and now >= (task.get("ends_at") or 0):
            self._finish_task("时间到", stop_engine=True)
            return
        if (task["type"] == "games"
                and task["games_done"] >= task["count"]):
            self._finish_task("盘数到", stop_engine=True)
            return
        self._drive_task(task)

    def _maybe_start_daily(self, now):
        dt = datetime.datetime.fromtimestamp(now)
        hhmm = dt.strftime("%H:%M")
        today = dt.strftime("%Y-%m-%d")
        for t in self._tasks:
            if t.get("status") != "pending" or t.get("type") != "daily":
                continue
            if not _daily_due(t.get("at"), dt):
                continue
            if int(dt.weekday()) not in (t.get("days") or range(7)):
                continue
            if t.get("last_run") == today:
                continue
            # 激活一份运行副本；定时任务本身留在队列里等下一次。
            # 每天任务可以带「盘数」或「时长」，激活时翻成可执行的类型。
            count = max(0, int(t.get("count") or 0))
            minutes = max(0, int(t.get("minutes") or 0))
            run = dict(t)
            if count > 0:
                run.update(type="games", count=count, games_done=0,
                           ends_at=0)
            elif minutes > 0:
                run.update(type="duration", minutes=minutes,
                           ends_at=now + minutes * 60, games_done=0)
            else:
                run.update(type="games", count=1, games_done=0, ends_at=0)
            run["status"] = "active"
            run["created_at"] = now
            t["last_run"] = today
            self._tasks.append(run)
            self._active_task = run
            self._seen_running = False
            self._opening_sent = False
            self._post_game_sent = False
            self._chat.reset()
            self._log("定时任务触发：" + _task_text(run))
            self._emit_tasks()
            return

    def _finish_task(self, reason, *, stop_engine=False):
        task = self._active_task
        if task is None:
            return
        task["status"] = "done"
        task["finished_at"] = time.time()
        task["finish_reason"] = reason
        self._active_task = None
        if stop_engine:
            try:
                st = self._status()
                if st.get("running"):
                    self._stop_engine()
            except Exception:
                pass
        self._log(f"任务结束（{reason}）：" + _task_text(task))
        self._set_state("idle", f"任务结束：{reason}")
        self._emit_tasks()

    def _emit_tasks(self):
        self._emit("ai_task", self.task_snapshot())
        self._tasks_emit_at = time.time()

    def _emit_tasks_if_due(self):
        if time.time() - self._tasks_emit_at > 3.0:
            self._emit_tasks()

    def _drive_task(self, task):
        if time.time() < self._drive_after:
            return
        st = self._status()
        running = bool(st.get("running"))
        goal = task.get("goal")

        if self._pending_leave_at and time.time() >= self._pending_leave_at:
            self._pending_leave_at = 0.0
            ok, msg = self.fox.leave_room()
            self._log(msg)
            self._last_request = 0.0
            self._drive_after = time.time() + 2.0
            return

        if running:
            if not self._seen_running:
                self._seen_running = True
                self._running_since = time.time()
            self._ensure_opening()
            return

        # 从「运行中」掉下来 = 这一盘结束（引擎自己收工）
        if self._seen_running:
            self._seen_running = False
            self._running_since = 0.0
            task["games_done"] = int(task.get("games_done") or 0) + 1
            self._log(f"这一盘结束，已完成 {task['games_done']} 盘")
            self._post_game_chat()
            self._emit_tasks()
            if task.get("type") == "games" and task["games_done"] >= task["count"]:
                self._finish_task("盘数到", stop_engine=False)
                return
            if goal == "play":
                self._finish_task("这一盘下完", stop_engine=False)
                return
            # 连续匹配：稍等再退房、再申请
            self._pending_leave_at = time.time() + 4.0
            self._drive_after = time.time() + 4.0
            return

        if goal == "chat":
            # 纯聊天任务：不在大厅/房间就回大厅；在房间/观战就直接聊
            if not self._chat_context():
                ok, msg = self.fox.ensure_lobby(timeout=6)
                self._log(msg)
            self._drive_after = time.time() + 8.0
            return
        if goal not in ("play", "match_friendly"):
            return

        # 1) 协商框
        dlg, _txt = self.fox.find_game_dialog(timeout=0.2)
        if dlg:
            ok, msg = self.fox.negotiate(prefer_friendly=True)
            self._log(msg)
            self._drive_after = time.time() + 1.2
            return
        # 2) 邀请框（10 秒，优先接）
        inv = self.fox.find_invite()
        if inv:
            ok, msg = self.fox.accept_invite(inv)
            self._log(msg)
            self._drive_after = time.time() + 1.0
            return
        # 3) 已经在房间里
        title = self.fox.title()
        if "对弈中" in title:
            self._start_autoplay()
            self._drive_after = time.time() + 5.0
            return
        if "号房间" in title:
            self._drive_after = time.time() + 1.0
            return
        # 4) 大厅：发起匹配
        if time.time() - self._last_request >= 25.0:
            ok, msg = self.fox.request_match()
            if not ok:
                self._log(f"按控件点不到「自动申请对局」，改用视觉：{msg}")
                self._vision_click("自动申请对局")
            else:
                self._log(msg)
            self._last_request = time.time()
        self._drive_after = time.time() + 2.0

    # ============================================================ 聊天
    def _ensure_opening(self):
        if not self._host.get("chat_auto", True):
            return
        if self._opening_sent:
            return
        if not self._running_since or time.time() - self._running_since < 2.0:
            return
        self._opening_sent = True
        text = _pick_open_template()
        self._chat_say(text, kind="opening", allow_wait=False)

    def _post_game_chat(self):
        if not self._host.get("chat_auto", True):
            return
        if self._post_game_sent:
            return
        self._post_game_sent = True
        text = _pick_post_template()
        self._chat_say(text, kind="post_game", allow_wait=False)

    def _chat_say(self, text, *, kind="reply", allow_wait=True):
        text = ai_skill.sanitize_chat(text, CHAT_MAX_CHARS)
        if not text:
            return False
        ok_now, why = self._chat.can_send()
        if not ok_now:
            if allow_wait:
                self._log(f"聊天节流跳过（{why}）：{text}")
            return False
        ok, msg = self.fox.send_chat(text)
        if not ok:
            self._log(msg)
            return False
        self._chat.mark()
        self._sent_chat.add(text)
        if len(self._sent_chat) > 50:
            self._sent_chat = set(list(self._sent_chat)[-25:])
        self._emit_msg("chat", text)
        self._log(f"聊天已发：{text}")
        return True

    def _tick_chat(self):
        if not self._host.get("chat_auto", True):
            return
        # 只有托管任务在跑时才自动聊天；单纯问一句「现在状态」不应
        # 让 AI 突然开始跟对手搭话（2026-11-08）。
        if self._active_task is None:
            return
        if self._hover.is_set():
            return
        now = time.time()
        if now - self._chat_poll_at < CHAT_POLL_INTERVAL:
            return
        self._chat_poll_at = now
        phase = self._chat_context()
        if not phase:
            return
        self._chat_phase = phase
        title = self.fox.title()
        room_key = _chat_room_key(title)
        changed = room_key != self._chat_room
        if changed:
            # 换房间/大厅：节流与已读记录都重新来（每个房间单独算额度）
            self._chat_room = room_key
            self._chat.reset()
            self._chat_raw = ""
            self._chat_seen = set()
            self._sent_chat = set()
            self._chat_lines = []
            self._chat_vision_at = 0.0
            self._pending_reply = ""

        # 上一条被节流挡下的回复，到时间先补发
        if self._pending_reply:
            ok, _why = self._chat.can_send()
            if ok:
                if self._chat_say(self._pending_reply, kind="reply"):
                    self._pending_reply = ""
            return
        try:
            raw = self.fox.read_chat()
        except Exception:
            raw = ""
        if changed:
            # 刚换房间：把现有聊天记录当基线，不追着旧消息回
            self._chat_raw = raw
            return
        if raw and not raw.startswith(self._chat_raw):
            # 换了房间/清空：先把已有内容当基线，不逐行补回
            self._chat_raw = raw
            return
        new = raw[len(self._chat_raw):] if raw else ""
        if raw:
            self._chat_raw = raw
        if new.strip():
            # 从最新一条往回看：先回刚出现的那句，别回半天前的旧话
            for line in reversed(new.splitlines()[-6:]):
                if self._consider_chat_line(line, phase):
                    return
            return

        # Win32 读不到 RichEdit 文本时，用多模态兜底（最多 45 秒一次）。
        # 野狐的聊天面板有时不响应 WM_GETTEXT，只能看图。
        for line in self._read_chat_vision_if_due():
            if self._consider_chat_line(line, phase):
                return

    def _chat_context(self):
        """当前处于哪种聊天场景；不在可聊天的界面返回 None"""
        title = self.fox.title() or ""
        if not title:
            return ""
        if "对弈中" in title:
            return "对局中"
        if "观战" in title:
            return "观战中"
        if "号房间" in title or "对局室" in title:
            return "对局室"
        if "高级房" in title or "大厅" in title:
            return "大厅"
        return ""

    def _consider_chat_line(self, line, phase):
        line = str(line or "").strip()
        if not line or line in self._chat_seen:
            return False
        self._chat_seen.add(line)
        if len(self._chat_seen) > 300:
            self._chat_seen = set(list(self._chat_seen)[-150:])
        if _is_system_line(line):
            return False
        if any(s and s in line for s in self._sent_chat):
            return False
        speaker, text = _split_chat_line(line)
        if speaker and speaker in ("系统", "公告", "野狐", "官方", "管理员",
                                   "我", "自己", "本人"):
            return False
        if not speaker and _looks_like_ui_line(text):
            return False
        if len(text) < 1:
            return False
        self._chat_lines.append(line)
        del self._chat_lines[:-20]
        reply = self._make_chat_reply(line, phase)
        if not reply:
            return False
        ok, why = self._chat.can_send()
        if not ok:
            # 节流中：先存着，到时间补发（不能直接丢掉，否则对方那句话永远没回）
            self._pending_reply = reply
            self._log(f"聊天节流，回复排队（{why}）：{reply}")
            return False
        return self._chat_say(reply, kind="reply")

    def _read_chat_vision_if_due(self):
        """用视觉读聊天区（Win32 文本读不到时兜底）"""
        if self.llm is None:
            return []
        now = time.time()
        if now - self._chat_vision_at < 45.0:
            return []
        self._chat_vision_at = now
        data = self._vision_json(
            "读取这张野狐截图里聊天区最近 5 条玩家发言。只输出 JSON："
            "{\"lines\": [\"名字：内容\", ...]}；没有聊天就输出 {\"lines\": []}。"
            "不要包含系统提示、按钮文字、对局信息。")
        if not isinstance(data, dict):
            return []
        lines = data.get("lines")
        if not isinstance(lines, list):
            return []
        return [str(x).strip() for x in lines if str(x).strip()][-5:]

    def _make_chat_reply(self, line, phase=None):
        if self.llm is None:
            return ai_skill.sanitize_chat(
                "是 AI 在练棋，见笑了" if ai_skill.asks_ai(line) else "")
        try:
            recent = "\n".join(self._chat_lines[-8:]) or self._chat_raw[-600:]
            st = self._status()
            scene = "野狐 %s；第 %s 手；我执%s" % (
                (self.fox.title() or "").strip(),
                st.get("move_no") or "?",
                "黑" if st.get("my_color") == "B" else "白")
            msgs = ai_skill.build_chat_messages(
                line, recent, phase=phase or self._chat_phase or "对局中",
                style=self._host.get("chat_style") or None,
                scene=scene)
            raw = self.llm.chat(msgs, max_tokens=100)
            return ai_skill.sanitize_chat(raw, CHAT_MAX_CHARS)
        except Exception as e:
            self._log(f"聊天回复生成失败：{e}")
            return ""

    # ============================================================ 押分
    def _place_bet(self, args):
        cap = int(self._host.get("bet_max_single") or 10000)
        daily_cap = int(self._host.get("bet_max_daily") or 50000)
        today = datetime.date.today().isoformat()
        if self._bet_day != today:
            self._bet_day = today
            self._bet_spent = 0
        left = max(0, daily_cap - self._bet_spent)
        if left < 1000:
            self._emit_msg("assistant", "今天的押分额度已经用完了。")
            self._log("今日押分额度已用完")
            return

        side = str(args.get("side") or "").upper()
        if side in ("黑", "BLACK"):
            side = "B"
        if side in ("白", "WHITE"):
            side = "W"
        if side not in ("B", "W"):
            side = ""
        amount = 0
        try:
            amount = int(args.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0

        # ① 必须先标定棋盘 + 用 KataGo 拿当前胜率；拿不到就不押
        kata = self._kata_winrate()
        if not kata.get("ok"):
            msg = "押分没做：" + (kata.get("reason") or "拿不到 KataGo 胜率")
            self._emit_msg("assistant", msg)
            self._log(msg)
            return
        p_b, p_w = kata["p_black"], kata["p_white"]
        self._log("引擎胜率：黑 %.1f%% / 白 %.1f%%（%s）" % (
            p_b * 100, p_w * 100, kata.get("source") or "引擎"))

        # ② 读双方支持池（拿不到 A/B 就不押，不能拿百分比硬凑）
        pools = self.fox.read_bet_pools()
        if not pools:
            if not self.fox.find_bet_dialog()[0]:
                ok, _msg = self.fox.open_support()
                if not ok:
                    self._vision_click("支持")
                time.sleep(0.8)
            pools = self.fox.read_bet_pools()
        if not pools:
            pools = self._vision_pools()
        if not pools:
            self._emit_msg("assistant", "押分没做：读不到黑白双方的支持池金额。")
            self._log("读不到双方支持池，按规则不押")
            return
        self._log("支持池：黑 {:,} / 白 {:,}".format(
            int(pools["black"]), int(pools["white"])))

        # ③ 老板公式：M=max(A,B)/(A+B)，高占比方门槛 sqrt(M)，另一方 1-sqrt(M)
        plan = fox_skill.bet_plan(
            p_b, p_w, pools["black"], pools["white"],
            stake_cap=min(cap, left),
            stake=(amount or None),
            prefer_side=side,
            min_edge=float(self._host.get("bet_min_edge") or 0.0))
        if not plan.get("ok"):
            why = plan.get("reason") or "没有达到门槛"
            self._emit_msg("assistant", "这手不押：" + why)
            self._log("押分放弃：" + why)
            return

        # ④ 弹窗没开就打开；然后按算好的边/额下注
        if not self.fox.find_bet_dialog()[0]:
            ok, msg = self.fox.open_support()
            if not ok:
                # 控件点不到「支持」时，用视觉找黄色按钮兜底
                if self._vision_click("支持"):
                    time.sleep(0.8)
                else:
                    self._emit_msg("assistant",
                                   "押分没做：打不开支持弹窗（" + msg + "）")
                    return
            else:
                time.sleep(0.6)
        ok, msg, real = self.fox.place_bet(
            plan["side"], plan["stake"], max_single=min(cap, left))
        self._log(msg)
        if ok:
            self._bet_spent += int(real or 0)
            self._emit_msg(
                "assistant",
                "押分完成：{side} {stake:,} 狐币（门槛 {th:.1f}%，"
                "胜率 {p:.1f}%）".format(
                    side="黑" if plan["side"] == "B" else "白",
                    stake=plan["stake"], th=plan["threshold"] * 100,
                    p=plan["p"] * 100))
        else:
            self._emit_msg("assistant", "押分没成：" + msg)

    # ---------------------------------------------------------- 胜率
    def _bet_engine_spec(self):
        """押分分析用的引擎：当前选中的优先；不可用就找第一个可用的。

        老板 2026-11-08：**智子云还是 KataGo 不重要，有胜率就行**。
        所以这里 local / cloud 都接受，只排除死活引擎（lifego）。
        """
        def _is_engine(e):
            if not isinstance(e, dict):
                return False
            kind = str(e.get("kind") or "")
            key = str(e.get("key") or "")
            if kind == "lifego":
                return False
            return (kind in ("local", "cloud")
                    or key.startswith("local:") or key.startswith("cloud:"))

        try:
            cur = self._ui(lambda: self.app._cur_engine(), 6)
        except Exception:
            cur = None
        if _is_engine(cur):
            return cur
        try:
            reg = self._ui(lambda: list(getattr(self.app, "reg", []) or []), 6)
        except Exception:
            reg = []
        for e in reg:
            if not _is_engine(e):
                continue
            try:
                ok, _why = self._ui(lambda: self.app._engine_ready(e), 6)
                if ok:
                    self._log("押分分析改用引擎：" + str(e.get("name")))
                    return e
            except Exception:
                continue
        return None

    def _kata_winrate(self):
        """拿当前胜率：引擎在跑就用实时胜率，否则标定后用当前引擎分析一次。

        返回 {ok, p_black, p_white, source}；不限定 KataGo/智子云。
        """
        st = self._status()
        if st.get("running"):
            if st.get("winrate") is None:
                return {"ok": False,
                        "reason": "引擎还在算，暂未拿到胜率"}
            try:
                import fox_board as FB
                geom = getattr(FB, "GEOM", None)
                if not (geom and geom.get("ok")):
                    return {"ok": False, "reason": "棋盘几何还没标定"}
                p_my = float(st["winrate"])
                if p_my > 1.0:
                    p_my /= 100.0
                p_my = max(0.0, min(1.0, p_my))
                my = (st.get("my_color") or "B").upper()
                p_b = p_my if my == "B" else 1.0 - p_my
                return {"ok": True, "p_black": p_b,
                        "p_white": 1.0 - p_b,
                        "source": "当前引擎实时胜率"}
            except Exception as e:
                return {"ok": False, "reason": f"读实时胜率失败：{e}"}
        return self._kata_winrate_oneshot()

    def _kata_winrate_oneshot(self):
        """标定当前棋盘 → 当前引擎分析一次 → 黑白胜率（不落子）"""
        try:
            import fox_board as FB
            import fox_auto as FA
        except Exception as e:
            return {"ok": False, "reason": f"读盘/引擎模块不可用：{e}"}
        try:
            im, title, _org = FB.grab(raise_window=False)
        except Exception as e:
            return {"ok": False, "reason": f"截不到野狐棋盘：{e}"}
        try:
            geom = FB.ensure_geom(im, force=True)
            board, conf, last = FB.read_board_full(im, geom=geom)
        except Exception as e:
            return {"ok": False, "reason": f"棋盘标定/读盘失败：{e}"}
        if not board:
            return {"ok": False, "reason": "读到的棋盘是空的"}
        try:
            # read_board_full 的 conf 是二维 [(标记, 置信度), ...]，不是单个数字
            vals = []
            for row in conf or []:
                for item in row:
                    if isinstance(item, (tuple, list)) and len(item) >= 2:
                        vals.append(float(item[1]))
                    else:
                        vals.append(float(item))
            conf_avg = sum(vals) / len(vals) if vals else 0.0
        except (TypeError, ValueError):
            conf_avg = 0.0
        if conf_avg < 0.50:
            return {"ok": False,
                    "reason": f"读盘置信度太低（{conf_avg:.2f}），不拿它押分"}
        turn = _turn_for_analysis(title, last)
        spec = self._bet_engine_spec()
        if not spec:
            return {"ok": False,
                    "reason": "没有可用的引擎，拿不到胜率"}
        try:
            komi = float(self._host.get("komi")
                         or self._status().get("komi") or 7.5)
        except (TypeError, ValueError):
            komi = 7.5
        rules = self._host.get("rules") or self._status().get("rules") or "chinese"
        try:
            visits = int(self._host.get("visits") or 10000)
        except (TypeError, ValueError):
            visits = 10000
        visits = max(1000, min(20000, visits))
        budget = 8.0
        engine = None
        try:
            engine = FA.make_engine_from_spec(
                spec, visits=visits, budget=budget, komi=komi, rules=rules)
        except Exception as e:
            return {"ok": False, "reason": f"引擎起不来：{e}"}
        try:
            auto = FA.Auto(engine, my_color="B", komi=komi,
                           think=budget, visits=visits, rules=rules,
                           think_mode="visits")
            _best, ana = auto.decide(board, turn, budget=budget,
                                     visits=visits)
            raw = None
            moves = (ana or {}).get("moves") or []
            if moves:
                raw = moves[0].get("winrate")
            if raw is None and ana:
                raw = ana.get("winrate", ana.get("rootWinrate"))
            if raw is None:
                return {"ok": False, "reason": "引擎这次没有给出胜率"}
            p_white = float(raw)
            if p_white > 1.0:
                p_white /= 100.0
            p_white = max(0.0, min(1.0, p_white))
            self._log("标定成功：step=%.2f，第 %s 手后轮 %s，"
                      "引擎分析 %.0f visits" % (
                          float(geom.get("step") or 0),
                          (re.search(r"第\s*(\d+)\s*手", title or "") or
                           [None, "?"])[1],
                          turn, visits))
            return {"ok": True, "p_black": 1.0 - p_white,
                    "p_white": p_white, "source": "单次引擎分析",
                    "turn": turn, "board_n": len(board)}
        except Exception as e:
            return {"ok": False, "reason": f"KataGo 分析失败：{e}"}
        finally:
            try:
                if engine is not None:
                    engine.close()
            except Exception:
                pass

    def _vision_pools(self):
        """支持池 Win32 读不到时，用视觉读弹窗"""
        if self.llm is None:
            return None
        ann = self.fox.screenshot_bet_dialog() or self.fox.screenshot()
        if ann is None:
            return None
        data = self._vision_json(
            "读这张野狐「支持」弹窗：黑方支持池、白方支持池各多少狐币。"
            "单位是亿/万就换算成整数。只输出 JSON："
            "{\"black\": 数字, \"white\": 数字}；读不到输出 {}。", ann=ann)
        if not isinstance(data, dict):
            return None
        b = fox_skill.parse_amount(data.get("black"))
        w = fox_skill.parse_amount(data.get("white"))
        if b is None or w is None:
            return None
        return {"black": int(b), "white": int(w), "raw": str(data)}

    def _vision_odds(self):
        data = self._vision_json(
            "读这张野狐截图里房间顶部/支持弹窗的押分赔率或支持率。"
            "只输出 JSON：{\"black_pct\": 数字, \"white_pct\": 数字, \"interval\": 数字}。"
            "读不到就输出 {}。")
        if not isinstance(data, dict):
            return None
        out = {}
        for k in ("black_pct", "white_pct"):
            try:
                if data.get(k) is not None:
                    out[k] = float(data[k])
            except (TypeError, ValueError):
                pass
        if data.get("interval") is not None:
            try:
                out["interval"] = int(data["interval"])
            except (TypeError, ValueError):
                pass
        if "black_pct" in out and "white_pct" not in out:
            out["white_pct"] = max(0.0, 100.0 - out["black_pct"])
        if "white_pct" in out and "black_pct" not in out:
            out["black_pct"] = max(0.0, 100.0 - out["white_pct"])
        return out or None

    def _vision_json(self, prompt, ann=None):
        if self.llm is None:
            return None
        if not bool((self.settings.get("llm") or {}).get("vision", True)):
            return None
        if ann is None:
            ann = self.fox.screenshot()
        if ann is None:
            return None
        try:
            raw = self.llm.chat(
                [{"role": "system", "content": "你是野狐界面识别器。只输出 JSON，不要解释。"},
                 {"role": "user", "content": [
                     {"type": "text", "text": prompt},
                     {"type": "image_url",
                      "image_url": {"url": ann.data_uri()}}]}],
                json_mode=True, image_jpeg=None, max_tokens=300)
            return _loads_json(raw)
        except Exception as e:
            self._log(f"视觉识别失败：{type(e).__name__}: {e}")
            return None

    def _vision_click(self, label):
        """控件点不到时，用多模态模型在截图里找按钮并点。"""
        if self.llm is None:
            return False
        if not bool((self.settings.get("llm") or {}).get("vision", True)):
            return False
        ann = self.fox.screenshot()
        if ann is None:
            return False
        prompt = (
            f"在截图中找野狐的按钮「{label}」，返回它的中心像素坐标。"
            "只输出 JSON：{\"found\": true, \"x\": 数字, \"y\": 数字}；"
            "找不到输出 {\"found\": false}。坐标以图片左上角为原点。")
        try:
            raw = self.llm.chat(
                [{"role": "system", "content": "你是界面定位器。只输出 JSON。"},
                 {"role": "user", "content": [
                     {"type": "text", "text": prompt},
                     {"type": "image_url",
                      "image_url": {"url": ann.data_uri()}}]}],
                json_mode=True, max_tokens=200)
            data = _loads_json(raw)
            if data.get("found") and data.get("x") is not None:
                pos = self.fox.click_image_point(float(data["x"]),
                                                float(data["y"]), ann)
                if pos:
                    self._log(f"视觉点击「{label}」→ {pos}")
                    return True
        except Exception as e:
            self._log(f"视觉点击失败：{type(e).__name__}: {e}")
        return False

    # ============================================================ 策略/持久化
    def _apply_policy(self, patch):
        keys = {"my_color", "komi", "rules", "think_mode", "think",
                "think_min", "think_max", "visits", "engine_key",
                "chat_auto", "chat_style", "accept_invites", "allow_ranked",
                "bet_auto", "bet_max_single", "bet_max_daily", "bet_min_edge"}
        for k, v in (patch or {}).items():
            if k not in keys:
                continue
            self._host[k] = v
        self.settings["host"] = dict(self._host)

    def _append_history(self, role, text):
        # ai_skill.build_planner_messages 读的是 content 字段；
        # 这里同时保留 text/ts 给界面和旧数据用。
        self._history.append({"role": role, "content": str(text)[:1000],
                              "text": str(text)[:1000],
                              "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        self._history = self._history[-40:]

    def _save_settings(self):
        with self._lock:
            self.settings["host"] = dict(self._host)
            self.settings["tasks"] = [t for t in self._tasks
                                      if t.get("status") != "done"][-12:]
            self.settings["history"] = self._history[-40:]
            try:
                ai_store.save_ai(self.settings)
            except Exception as e:
                self._log(f"保存 AI 设置失败：{e}")


# ---------------------------------------------------------------- 纯函数
def _loads_json(raw):
    """模型回复 → dict。容忍 ```json 代码块和前后废话。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        raise ValueError("模型返回空内容")
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"不是 JSON：{text[:120]}")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")
    return obj


def _chat_room_key(title):
    """把「第N手」这类每秒都变的标题归一成房间键，避免聊天节流被重置。"""
    t = str(title or "")
    t = re.sub(r"\[\s*第\s*\d+\s*手\s*\]", "", t)
    t = re.sub(r"第\s*\d+\s*手", "", t)
    return t.strip()


def _split_chat_line(line):
    """「名字：内容」→ (名字, 内容)；没有名字返回 ("", 原文)。"""
    m = re.match(r"^\s*([^\s:：]{1,20})\s*[:：]\s*(.+)$", str(line or ""))
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", str(line or "").strip()


def _looks_like_ui_line(line):
    """聊天控件里混进来的玩家列表/房间信息，不是玩家发言。"""
    t = str(line or "").strip()
    if not t:
        return True
    if t in ("空闲", "对局中", "观战", "在线", "离线"):
        return True
    if re.search(r"\d+\s*胜\s*\d+\s*负", t):
        return True
    if "财富" in t or "黑贴" in t or "号房间" in t or "高级房" in t:
        return True
    if re.search(r"^\d+\s*[|｜]", t):
        return True
    if len(t) < 30 and "第" in t and "手" in t:
        return True
    return False


def _turn_for_analysis(title, last=None):
    """当前该谁走：优先标题「第N手」奇偶，其次最后一手颜色。"""
    m = re.search(r"第\s*(\d+)\s*手", str(title or ""))
    if m:
        return "B" if int(m.group(1)) % 2 == 0 else "W"
    try:
        if isinstance(last, (tuple, list)) and len(last) >= 3:
            c = str(last[2]).upper()
            if c in ("B", "W"):
                return "W" if c == "B" else "B"
    except Exception:
        pass
    return "B"


def _daily_due(at, dt, window=120.0):
    """定时任务到点判断：允许晚 2 分钟（模型调用/落子可能错过整分）。"""
    try:
        hh, mm = [int(x) for x in str(at or "").split(":")[:2]]
        target = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (dt - target).total_seconds()
        return 0.0 <= delta < float(window)
    except Exception:
        return False


def _pct(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f <= 1.0:
        f *= 100.0
    return f"{f:.1f}%"


def _task_text(t, short=False):
    if not t:
        return "无"
    kind = {"duration": "时长", "games": "盘数", "daily": "每天"}.get(
        t.get("type"), str(t.get("type")))
    goal = {"play": "下一盘", "match_friendly": "连续匹配友谊赛",
            "chat": "聊天", "watch": "观战", "bet": "押分"}.get(
        t.get("goal"), str(t.get("goal")))
    if t.get("type") == "daily":
        when = f"每天 {t.get('at')}"
    elif t.get("type") == "games":
        when = f"{t.get('count')} 盘"
    else:
        when = f"{t.get('minutes')} 分钟"
    status = {"pending": "待触发", "active": "进行中", "done": "已完成"}.get(
        t.get("status"), t.get("status"))
    extra = ""
    if t.get("type") == "duration" and t.get("ends_at"):
        left = max(0, int(t["ends_at"] - time.time()))
        extra = f" 剩 {left // 60}:{left % 60:02d}"
    note = (" " + t["note"]) if t.get("note") and not short else ""
    return f"[{status}] {when} · {goal}{extra}{note}"


def _pick_open_template():
    return random.choice([
        "你好，请多指教~", "来，请多指教", "开局了，请多指教", "您好，手下留情啊",
    ])


def _pick_post_template():
    return random.choice([
        "谢谢指教，学到了", "这盘下得挺舒服，谢啦", "好棋，受教了",
        "这盘有意思，回头再下", "多谢指教，下次再来",
    ])


def _is_system_line(line):
    keys = ("棋友离线", "棋友回到房间", "您说话太快了", "系统提示", "公告",
            "对局协商玩家", "已离开房间", "请重新申请", "进入房间", "离开房间",
            "准备好", "开始对局", "拒绝了", "接受了对局", "坐下", "站起来",
            "观战人数", "欢迎来到", "押分成功", "支持成功")
    return any(k in line for k in keys)
