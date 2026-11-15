# -*- coding: utf-8 -*-
"""野狐自动对弈 · 运行环境路径

为什么单独抽一个模块
------------------------------------------------------------
打包成 exe（PyInstaller --onefile）之后，两种路径全部变了：

    __file__        →  C:\\...\\_MEI123456\\gui.py
                       ↑ 那是**临时解压目录**，程序退出就被删掉。
                         往里写 logs/ 或崩溃日志 = 关掉就没了。

    sys.executable  →  C:\\...\\野狐自动对弈.exe
                       ↑ 不再是 python.exe。原来用
                         `[sys.executable, "katago_cloud.py"]` 拉子服务，
                         打包后变成「启动自己」，参数全错。

所以整个项目里凡是要「读随程序走的资源」或「写用户数据」，
都从这里取，不许再自己拼 __file__：

    HERE        只读资源目录（源码态=项目目录，打包态=_MEIPASS）
                engine/analysis_fast.cfg 这种随包文件从这里找

    USER_DIR    可写数据目录（源码态=项目目录，打包态=exe 所在目录）
                logs/、GUI-崩溃日志.txt 写这里

    is_frozen() 是否打包运行
    python_exe()    真 Python 解释器路径（打包态返回 None）

关于 python_exe()
------------------------------------------------------------
打包后**没有** Python 解释器可用 —— exe 里塞的是运行时不带解释器入口。
所以原来「subprocess 拉起 katago_cloud.py」的写法在打包态必须换路子：
改成用 exe 自己带 `--serve` 参数重新启动（见 fox_auto.CloudBackend）。
"""
import os
import sys


def is_frozen():
    """是否 PyInstaller 打包运行"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


# ---------------------------------------------------------------- 只读资源
if is_frozen():
    # 打包态：_MEIPASS 是解压出来的临时目录，**只读**
    HERE = sys._MEIPASS
else:
    HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 可写数据
def _user_dir():
    """用户数据写在哪

    源码态：项目目录（和以前行为一致，老板的 logs/ 不会跑丢）
    打包态：**%LOCALAPPDATA%\野狐自动对弈**

    ⚠️ 为什么打包态不再写「exe 所在目录」（2026-10-27 改）
    ----------------------------------------------------
    老板原话：「这个应用放到桌面上一打开之后，桌面就多出了 log 跟
              setting 这两个东西」

    这是老设计的直接后果：exe 放桌面 → USER_DIR = 桌面 →
    打开一次就多出 `logs/` 和 `settings.json`。装到下载夹就往下载夹拉屎，
    装到哪儿脏哪儿。绿色软件也不该这样 —— 只有**显式开便携模式**
    才该往盘里写。

    现在的口径：
      · 默认（推荐）→ %LOCALAPPDATA%\野狐自动对弈
        Windows 规范位置，不进桌面、不污染安装目录，
        不想要了删掉这一个文件夹就干净
      · 显式便携 → exe 同目录下放一个 **portable.txt**（内容随便）
        给「拷到 U 盘、日志就在旁边」那种用法。
        必须用户主动建这个文件才生效 —— 不能靠「exe 恰好在可写目录」
        去猜，那个猜测正是污染桌面的原因
      · WEIQI_HOME 环境变量优先级最高（测试用，见下）

    ⚠️ 环境变量 WEIQI_HOME 可以强制指定（2026-10-23 加）
    ----------------------------------------------------
    动机：测试脚本会 `import gui` → App.__init__ 读 settings.json；
    某些测试还会调 `_save_ui_state()` 写回。于是**跑一次测试就把老板
    真实配置改了** —— 实际发生过：settings.json 被写成 `enabled: []`
    （一个引擎都不要），老板下次打开发现引擎列表是空的。

    现在测试可以在开头设 `WEIQI_HOME=<临时目录>`，所有读写都落到那里，
    真实配置一个字节都不动。这是唯一可靠的办法 —— 靠「测试里记得别
    调这个函数」是守不住的，将来加一条新测试就会漏。
    """
    env = os.environ.get("WEIQI_HOME")
    if env:
        try:
            os.makedirs(env, exist_ok=True)
            return env
        except Exception:
            pass                    # 指定的目录不可用 → 退回正常逻辑
    if not is_frozen():
        return os.path.dirname(os.path.abspath(__file__))

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    # 便携模式：用户主动在 exe 旁边放了 portable.txt
    if os.path.isfile(os.path.join(exe_dir, "portable.txt")):
        try:
            os.makedirs(os.path.join(exe_dir, "logs"), exist_ok=True)
            probe = os.path.join(exe_dir, ".wb_write_test")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return exe_dir
        except Exception:
            pass                    # 便携模式目录不可写 → 落到 LOCALAPPDATA
    d = os.path.join(os.environ.get("LOCALAPPDATA")
                     or os.path.expanduser("~"), "野狐自动对弈")
    os.makedirs(d, exist_ok=True)
    return d


USER_DIR = _user_dir()

LOG_DIR = os.path.join(USER_DIR, "logs")


def ensure_dirs():
    """建好所有需要的目录，返回 LOG_DIR"""
    os.makedirs(LOG_DIR, exist_ok=True)
    return LOG_DIR


def python_exe():
    """可用的 Python 解释器；打包态返回 None（exe 里没有解释器）

    打包态之所以还能跑子服务，是因为改用 `exe --serve` 自启动
    （同一个 exe 加不同参数走不同入口），不依赖外部 python。
    """
    if is_frozen():
        return None
    return sys.executable


def serve_argv():
    """怎么「以子服务模式」重新启动自己

    返回可以直接丢给 subprocess.Popen 的 [exe, arg...]；
    打包态和源码态都能用 —— fox_auto 不用再判断环境。
    """
    if is_frozen():
        return [sys.executable, "--serve"]
    return [sys.executable, os.path.join(HERE, "katago_cloud.py")]
