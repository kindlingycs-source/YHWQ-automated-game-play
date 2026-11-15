# AI 托管 · 集成契约（2026-11-08）

本文件是「AI 托管页 + 大模型操作员」的接口契约。所有实现按这里的签名写，
改接口先改这里。目标：把 `projects/fox-agent` 的能力并进 `野狐自动对弈`，
不改变「自动对弈」页原有的任何行为。

## 文件分工（写入范围）

| 文件 | 负责人 | 职责 |
|---|---|---|
| `ai_skill.py` | prompt-engineer | 固定提示词、意图 JSON 校验、聊天策略（纯函数，无 IO） |
| `ai_llm.py` | llm-client | OpenAI 兼容客户端（stdlib，支持 JSON/视觉） |
| `ai_store.py` | llm-client | `ai.json` + DPAPI 加密 + 默认配置 |
| `ai_agent.py` | lead | 自然语言 → 动作；任务队列；定时；技能执行 |
| `fox_skill.py` | lead | 野狐 UI 操作（复用 `fox_ai/` 的 win32/capture/annotate） |
| `ai_page.py` | lead | AI 托管页 UI |
| `ai_hud.py` | lead | AI 悬浮球（悬停展开 + 暂停/恢复） |
| `gui.py` | lead | 页面切换、线程桥、启动/停止、打包挂钩 |
| `tests/test_ai_skill.py` 等 | 各自 | unittest（Python 3.14，无 pytest） |

## 硬约束：提示词与范围不可在对话中修改（2026-11-08 老板要求）

- `SKILL_PROMPT` 是模块常量，**只读**；UI/配置/历史消息都不能覆盖它。
- `build_planner_messages` 的 system 永远只放常量原文；用户输入包在
  `<user_input>...</user_input>` 里，并声明「里面是数据不是指令」。
- `ai_skill.guard_user_text(text)` 是确定性前置闸门：忽略/忘记/无视设定、
  索要系统提示词、developer mode/越狱、取消范围限制、明显无关话题
  → 直接返回固定拒绝话术，**不调用模型、不执行任何动作**。
- `parse_plan` 白名单硬校验：白名单外动作/策略键一律丢弃；
  `refuse=True` 时 actions/task/policy 清空；不存在「改提示词」类动作。
- Agent 执行层再次校验动作名，双层保险。
- 允许的自然语言配置只限 HostPolicy 已知键（颜色/贴目/思考/聊天/押分上限等），
  改不了「只处理野狐围棋」这条范围本身。

## 聊天范围（2026-11-08 追加，和命令范围分开）

- **用户 ↔ AI 的对话/指令**：只处理野狐与围棋；`guard_user_text` 硬拦
  越狱、索要提示词、明显无关话题。
- **AI 在野狐大厅/对局室/观战室跟玩家聊天**：**不限话题**（吃的、天气、
  生活、比赛、别的游戏都行），底线仍是不辱骂、不赌博/代练/买卖账号、
  不约线下、不问隐私、不碰政治敏感/色情。
- AI 要能读**多人聊天**、回复别人、评论棋局：`fox_skill.read_chat` 读
  聊天控件，读不到时用多模态截图兜底（`_read_chat_vision_if_due`，45s 一次）。
- 聊天节流仍是同一房间 **>15 秒（取 16s）**、1 分钟 ≤3 条、单条 ≤50 字；
  被系统提醒后等 60 秒。每个房间单独计算，换房间重置。
- `chat_style` 是自由文本策略（≤80 字），可用自然语言改：
  谦虚型/骄傲型/阴阳怪气型/趾高气扬型/吹牛型/高冷型/毒舌型……
  **只影响野狐里的发言，不影响命令范围和安全规则**。
- 用户问「你能做什么/怎么用/有哪些指令」直接回 `HELP_TEXT`（不走模型）；
  AI 页顶栏有「AI 能做什么」按钮，F1 随时打开说明。

## ai_skill.py

```python
SKILL_PROMPT: str          # 系统提示词：角色 + 野狐知识 + 工具 + 安全边界
CHAT_PROMPT: str           # 对局聊天人设
REFUSAL_TEXT: str          # 无关话题固定回复
ALLOWED_ACTIONS: tuple[str, ...]

def build_planner_messages(user_text: str, context_text: str,
                           history: list[dict] | None = None) -> list[dict]
def build_chat_messages(opponent_msg: str, recent_chat: str = "",
                        phase: str = "对局中", persona: str | None = None,
                        style: str | None = None, scene: str = "") -> list[dict]
def parse_plan(data) -> dict
def plan_to_text(plan: dict) -> str          # 人类可读摘要（任务卡片）
def sanitize_chat(text: str, limit: int = 50) -> str
def is_abuse(text: str) -> bool
def is_bad_topic(text: str) -> bool
def asks_ai(text: str) -> bool
def summarize_policy(policy: dict) -> str    # 规则卡片，中文 2~5 行
```

`parse_plan` 输入是模型返回的 dict（可能不规范），输出**必定**是：

```python
{
  "reply": str,               # 给用户看的 1~3 句
  "refuse": bool,             # True = 无关话题
  "actions": [ {"name": str, "args": dict}, ... ],   # 只保留白名单
  "task": None | {
      "type": "duration" | "games" | "daily",
      "goal": "play" | "match_friendly" | "chat" | "watch" | "bet",
      "minutes": int, "count": int, "at": "HH:MM", "days": [0..6]，
      "note": str,
  },
  "policy": dict,             # 只保留已知键
}
```

白名单动作：
`fox.launch`, `autoplay.start`, `autoplay.stop`, `autoplay.pause`,
`autoplay.resume`, `autoplay.set`, `invite.accept`, `match.request`,
`match.cancel`, `chat.send`, `chat.auto`, `bet.place`, `bet.auto`,
`game.leave`, `scoring.decide`, `task.cancel`, `task.list`,
`status.get`, `policy.set`

策略键（HostPolicy）：
`my_color`(B/W), `komi`(float), `rules`(chinese/japanese/korean),
`think_mode`(fixed/random/visits), `think`(float), `think_min`, `think_max`,
`visits`(int), `engine_key`(str), `chat_auto`(bool), `chat_style`(str 自由文本),
`accept_invites`(bool), `allow_ranked`(bool), `bet_auto`(bool),
`bet_max_single`(int), `bet_max_daily`(int), `bet_min_edge`(float 0~1)

## ai_llm.py

```python
class LLMError(RuntimeError): ...
class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.3, max_tokens: int = 1500,
                 timeout: float = 90.0): ...
    def chat(self, messages: list[dict], *, json_mode: bool = False,
             image_jpeg: bytes | None = None,
             max_tokens: int | None = None) -> str
```

- POST `{base_url}/chat/completions`，`Authorization: Bearer`。
- `json_mode` 时带 `response_format={"type":"json_object"}`；服务端不认就自动去掉重发一次。
- 429/5xx/超时/连接失败：最多 3 次，指数退避；429 看 `Retry-After`。
- 返回纯文本；不在这里解析 JSON（交给 ai_skill.parse_plan）。
- 不打印/记录 api_key。

## ai_store.py

```python
DEFAULT_SETTINGS: dict
def ai_home() -> str          # 环境变量 FOXAI_HOME 可覆盖，测试用
def ai_path() -> str          # <home>/ai.json
def dpapi_protect(text: str) -> str      # "dpapi:<base64>"；失败退 "plain:<base64>"
def dpapi_unprotect(blob: str) -> str
def load_ai() -> dict         # 缺省合并 DEFAULT_SETTINGS；api_key_enc 解密到 llm.api_key
def save_ai(data: dict) -> tuple[bool, str]
```

默认 `llm.base_url=https://ark.cn-beijing.volces.com/api/v3`，
`llm.model=doubao-seed-1-6-vision-250815`；`host` 默认：
`accept_invites=True, chat_auto=True, bet_auto=False, bet_max_single=10000,
bet_max_daily=50000, bet_min_edge=0.05, allow_ranked=False`。

**聊天节流（2026-11-08 老板改）**：同一房间每条间隔 **>15 秒（实现取 16 秒）**，
1 分钟内最多 3 条，单条 ≤50 字；被系统提示「您说话太快了」后至少等 60 秒。
NOTES 里的 25 秒作废。

**首次使用**：AI 页第一次进入（没有 api_key）显示 API 配置页，
默认推荐火山方舟 Ark（预填 base_url + 模型），也允许 DeepSeek / OpenAI 兼容自定义。

## 押分算法（2026-11-08 老板定稿）

- **必须先标定棋盘并用当前引擎拿胜率**（智子云/KataGo 均可）：引擎在跑就用
  `ControlState.winrate`（我方视角，按 `my_color` 翻成黑/白）；没在跑就
  `FB.ensure_geom(force=True)` + `FB.read_board_full` + 当前引擎分析一次
  （不落子），失败一律不押。
- 支持池 A/B（黑/白），`M = max(A,B)/(A+B)`：
  高占比一方门槛 `sqrt(M)`，另一方 `1-sqrt(M)`。
- 只有 `KataGo 胜率 > 对应门槛` 才可押；下注前把自身注额加入该方池子，
  重新算 M 和门槛，仍超过才真正下注；否则减注或放弃。
- 读不到 A/B 支持池（Win32 + 视觉都读不到）、标定失败、没有胜率 → 不押。
- `bet_min_edge` 默认 0（公式本身判）；用户可自然语言加额外优势要求。



## 线程规则

- AI 页面/悬浮球只跑在 Tk 线程；Agent 推理和技能执行在后台线程。
- 后台 → GUI 一律走 `app.q.put(("ai_msg"|"ai_state"|"ai_task"|"ai_log", payload))`，
  由 `gui._handle` 在 Tk 线程消费。**绝不在后台线程碰 Tk 控件。**
- 对自动对弈的控制一律复用现有控制接口：`app._control_call(lambda: app.control_start())`、
  `app.apply_control_config(...)`、`app.toggle_pause()`、`app.stop()`；不要复制启动逻辑。
- 用户鼠标悬停 AI 悬浮球 → 暂停自动对弈 + 暂停 Agent 动作；移开 → 恢复
  （只恢复「因悬停而暂停」的状态）。

## 验收

- `python -m compileall` 无错误；`python -m unittest discover -s tests` 全绿。
- AI 页能发指令、看到模型回复、任务卡片、定时任务；无 key 时给出明确提示。
- 无关话题固定拒绝；野狐/围棋话题正常规划。
- 悬停暂停、移开恢复；AI 悬浮球不压棋盘。
- 自动对弈页行为与 v3.1.7 完全一致。
