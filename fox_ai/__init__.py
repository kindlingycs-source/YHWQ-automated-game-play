# -*- coding: utf-8 -*-
"""野狐大模型托管的 Agent 基础层。

模块（按 DESIGN.md 第 5 节）：
    config     读写 config.json，默认值见 DESIGN.md 第 6 节
    win32      窗口/控件/点击/键盘（纯 ctypes，不依赖 pywin32）
    capture    野狐窗口截图（PrintWindow 优先，mss 兜底）
    annotate   截图加坐标网格，压缩到模型友好尺寸
    llm        OpenAI 兼容多模态客户端（JSON 输出、重试、token 记账、mock）
    actions    动作解析/校验/执行
    trace      JSONL 轨迹 + 截图落盘

典型接法（flow 里）::

    from agent import capture, annotate, llm, actions, config, trace

    cfg = config.load_config()
    tr = trace.Trace(root=cfg["agent"]["trace_dir"])
    shot = capture.grab_fox()
    ann = annotate.grid(shot, out_path=tr.screens_dir)
    res = client.ask(phase.prompt, image=ann.jpeg, system=system_prompt)
    act = actions.parse_action(res, allowed=phase.allowed, mapper=ann)
    actions.Executor(trace=tr).execute(act)

这里刻意不在包初始化时 import 子模块：`import agent` 不该拉起 mss / PIL，
也避免循环依赖。用哪个子模块就 `from agent import xxx`。
"""

__version__ = "0.1.0"

__all__ = [
    "config",
    "win32",
    "capture",
    "annotate",
    "llm",
    "actions",
    "trace",
]
