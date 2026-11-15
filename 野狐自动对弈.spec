# -*- mode: python ; coding: utf-8 -*-
"""野狐自动对弈 · PyInstaller 打包配置

用法（在 weiqi 目录下）
------------------------------------------------------------
    py -3.14 -m PyInstaller 野狐自动对弈.spec --noconfirm --clean

产物
------------------------------------------------------------
    dist/野狐自动对弈.exe     单文件，双击即用
    窗口图标 + 启动封面都已内置

关键取舍
------------------------------------------------------------
1) onefile 而不是 onedir
   老板要「发给别人用的软件」—— 单文件最省事，微信发一个 exe 就完事。
   代价是首次启动要解压 numpy/mss/Pillow 到临时目录（约 3~6 秒），
   所以配了 splash 启动封面，不能让老板对着空屏等。

2) --windowed（无控制台）
   双击不弹黑窗口。代价是 stdout/stderr 全丢，
   所以 gui.py 里挂了 sys.excepthook 写 GUI-崩溃日志.txt。

3) 只打包真正用到的模块
   fox_auto 主流程只依赖 fox_board / engines / zhizi_cloud / katago_cloud
   以及 GUI 的 local_engine。项目里那些 exp*.py / selfplay*.py / _tests
   全是不相干的历史实验，绝不能扫进去（会拖进 scipy 之类的重依赖）。

4) numpy / mss / PIL 必须显式 hiddenimports
   它们在 fox_board 里是**函数内 import**（延迟导入），
   PyInstaller 的静态分析扫不到，不写就会运行到「读盘」那一步崩。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(SPEC))   # noqa: F821  (SPEC 由 PyInstaller 注入)

# ---------------------------------------------------------------- 数据文件
# 这些是「运行时按路径打开」的文件，静态分析发现不了，必须显式带上。
datas = [
    # 本机 KataGo 的 analysis 配置（local_engine 用）
    (os.path.join(HERE, "engine", "analysis_fast.cfg"), "engine"),
    # 图标：窗口 + 任务栏（gui._set_icon 会从 HERE 找）
    (os.path.join(HERE, "app.ico"), "."),
    (os.path.join(HERE, "icon-256.png"), "."),
]

# ---------------------------------------------------------------- 隐藏导入
hiddenimports = [
    # fox_board 里在函数体内 import，静态分析看不到
    "numpy",
    "mss",
    "PIL",
    "PIL.Image",
    "PIL.ImageGrab",
    # 云端 / 本地引擎链（都是被间接调用的）
    "zhizi_cloud",
    "katago_cloud",
    "local_engine",
    "engines",
    "fox_board",
    "fox_auto",
    "runtime_paths",
    # ⚠️ 2026-10-23 补：这 4 个全是 gui.py 里的**函数内 import**，
    #    静态分析扫不到，漏了就是「源码能跑、exe 一点就崩」。
    #    现场就是这样：点「设置」→ ModuleNotFoundError: setup_wizard
    #    （setup_wizard 内部还延迟 import engine_detect / settings，
    #      所以这三个必须一起带上，链条缺一环都炸）
    "settings",         # gui 顶部 import；engine_detect 也要
    "setup_wizard",     # gui.open_setup() 里 import
    "engine_detect",    # gui.__init__ / setup_wizard 里 import
    "hotkey",           # 历史模块（2026-11-02 起 gui 不再用，留着兼容老配置）
    "hud",              # 悬浮球：gui._ensure_ball() 里 import，静态分析可能漏
    "updater",          # 内置自动更新：gui._auto_check_update() 里 import
    # AI 托管（2026-11-08）：gui 里都是函数内 import，必须显式带上
    "ai_agent",
    "ai_page",
    "ai_hud",
    "ai_skill",
    "ai_llm",
    "ai_store",
    "fox_skill",
    "fox_ai",
    "fox_ai.win32",
    "fox_ai.capture",
    "fox_ai.annotate",
    # tkinter 子模块
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.filedialog",
    # 标准库但被动态 import 的
    "http.server",
    "socketserver",
    "ctypes.wintypes",
]

# 明确排除：项目里一堆历史实验脚本引了这些，别被误扫进来
excludes = [
    "scipy", "pandas", "matplotlib", "torch", "tensorflow",
    "IPython", "jupyter", "pytest", "setuptools", "pip",
    "PyQt5", "PySide2", "wx",
    "_tests", "lizzie_live", "lifego_engine", "gtp_engine",
    "fox_play", "selfplay", "selfplay2", "解说",
    "exp2", "exp3", "exp4", "exp5", "exp6", "exp7", "exp8",
    "exp9", "exp10", "probe_cmds", "build_final", "eval_final",
    "show_board", "test_setpos", "test_perspective", "server",
]


a = Analysis(
    [os.path.join(HERE, "gui.py")],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="野狐自动对弈",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压完容易被杀软误报，且启动更慢
    # ⚠️ 2026-11-02 现场根因：野狐是「以管理员身份运行」的（High 完整性），
    #    普通权限(Medium)的 exe 用 SetCursorPos/mouse_event 点它，会被
    #    Windows UIPI **静默丢弃** —— 鼠标都不动，野狐一个字都收不到。
    #    exe 必须以管理员启动（UAC 已关的本机不会有弹窗），才能和野狐同级。
    uac_admin=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # 无黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, "app.ico"),
    # 版本资源：exe 属性页里能看到 3.0.0.0（改版本时连同 gui.APP_VERSION 一起改）
    version=os.path.join(HERE, "version_info.txt"),
    # ⚠️ 启动封面：onefile 解压 numpy 要几秒，没这个就是白屏等待
    splash=os.path.join(HERE, "splash.png"),
)
