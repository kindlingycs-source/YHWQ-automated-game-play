# -*- coding: utf-8 -*-
"""一键打包 → 修标签 → (可选)换桌面 exe

用法（在本目录下）：
    python build_exe.py              # 只打包，产物在 dist/
    python build_exe.py --install    # 打包 + 备份旧桌面 exe + 换新的

⚠️ 为什么非要有这个脚本，不能手敲 PyInstaller（2026-11-01 踩到）
------------------------------------------------------------
DSH 工作区目录带 **[低完整性] 标签**（S-1-16-4096，禁止向上写），
在这里跑 PyInstaller，产出的 exe **会继承这个标签**。
带低完整性标签的 exe 双击后，进程本身也是低完整性 →
往 %LOCALAPPDATA% 写东西一律 PermissionError：
  · selftest.log 写不出来（自检说"写入失败: Permission denied"）
  · settings.json 存不下来（下次启动回到默认值）
  · 对局日志、GUI-崩溃日志.txt 全都没有
从资源管理器、任务管理器上看**没有任何异常**，只有"设置不保存"这种诡异现象。
实测：同一份源码，dist 里打出来 = 写不了；桌面那个 10-27 的老 exe = 正常。

所以打包的最后一步必须处理完整性标签，而且**要标成 High，不是 Medium**：

⚠️ 2026-11-02 现场（比上面更狠）
------------------------------------------------------------
标成 Medium 后，进程完整性被**封顶在 Medium**。而老板的野狐是以
「管理员身份运行」的（High 完整性）。Windows 的 UIPI 会**静默丢弃**
Medium 进程发向 High 窗口的模拟鼠标输入 —— `SetCursorPos` 连光标
都推不过去，`mouse_event` 也点不进去。现象就是：
  · 日志写「点击坐标 (1435,166)」，但鼠标没动、野狐一步没下
  · 换成管理员权限的 python 直接调同一个函数，立刻就落子了
  · 野狐自己完全不知道对面是脚本，纯粹是 Windows 拦掉的
所以 `icacls /setintegritylevel High` 是必须的：exe 和野狐同为 High
才点得进去。检查方法：日志首行「权限=High(管理员)」。
"""
import datetime
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, "野狐自动对弈.spec")
EXE_NAME = "野狐自动对弈.exe"
DIST = os.path.join(HERE, "dist", EXE_NAME)

# ⚠️ 桌面只留一个快捷方式，实体和旧版本一律进「杂物间」（老板 2026-11-02 定）
# 桌面根目录再出现 exe / .bak 就是违规 —— 他明确说过桌面别堆这些东西。
DESKTOP_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
JUNK_DIR = os.path.join(DESKTOP_DIR, "新建文件夹")      # 杂物间
INSTALL_DIR = os.path.join(JUNK_DIR, "野狐自动对弈")   # 程序本体 + 备份
TARGET = os.path.join(INSTALL_DIR, EXE_NAME)
LINK = os.path.join(DESKTOP_DIR, "野狐自动对弈.lnk")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()



def make_shortcut():
    """在桌面建/刷新一个 1KB 的快捷方式（不是 31MB 的 exe）"""
    try:
        import win32com.client
        sh = win32com.client.Dispatch("WScript.Shell")
        lnk = sh.CreateShortcut(LINK)
        lnk.TargetPath = TARGET
        lnk.WorkingDirectory = INSTALL_DIR
        lnk.IconLocation = f"{TARGET},0"
        lnk.Description = "野狐自动对弈"
        lnk.Save()
        print(f"桌面快捷方式：{LINK}")
    except Exception as e:
        print(f"!! 快捷方式没建成：{type(e).__name__}: {e}")


def sweep_desktop():
    """桌面根目录残留的 exe / .bak 一律收进杂物间（一次性迁移 + 日常兜底）"""
    moved = []
    try:
        for name in os.listdir(DESKTOP_DIR):
            low = name.lower()
            if not (low.startswith("野狐自动对弈") or low.startswith("野狐自动对弈.exe")):
                continue
            if not (low.endswith(".exe") or ".exe.bak" in low):
                continue
            src = os.path.join(DESKTOP_DIR, name)
            if not os.path.isfile(src):
                continue
            shutil.move(src, os.path.join(INSTALL_DIR, name))
            moved.append(name)
    except Exception as e:
        print(f"!! 桌面清理失败：{e}")
    if moved:
        print("从桌面收进杂物间：", "、".join(moved))


def prune_backups(keep=3):
    """旧版本只留最近 keep 份，别把杂物间也塞满"""
    try:
        baks = sorted(
            [os.path.join(INSTALL_DIR, n) for n in os.listdir(INSTALL_DIR)
             if ".bak-" in n])
        for old in baks[:-keep]:
            os.remove(old)
            print("清掉更早的备份：", os.path.basename(old))
    except Exception:
        pass

def integrity_label(path):
    """读文件完整性标签，返回 SID 字符串（没有标签返回 None）"""
    try:
        import win32security
        sd = win32security.GetFileSecurity(
            path, win32security.LABEL_SECURITY_INFORMATION)
        sacl = sd.GetSecurityDescriptorSacl()
        if not sacl or sacl.GetAceCount() == 0:
            return None
        ace = sacl.GetAce(0)
        return win32security.ConvertSidToStringSid(ace[2])
    except Exception as e:
        return f"(读不到: {e})"


def main():
    install = "--install" in sys.argv
    print(f"打包：{SPEC}")
    r = subprocess.run([sys.executable, "-m", "PyInstaller", SPEC,
                        "--noconfirm", "--clean"], cwd=HERE)
    if r.returncode != 0:
        print(f"✗ PyInstaller 失败（退出码 {r.returncode}）")
        return r.returncode
    if not os.path.isfile(DIST):
        print(f"✗ 没找到产物 {DIST}")
        return 1

    # 去掉继承来的低完整性标签，并**直接标成 High** —— 见文件头的说明
    # ⚠️ 2026-11-02 现场：标成 Medium 会把进程完整性**封顶在 Medium**，
    #    而野狐（老板以管理员身份启动的）是 High 完整性；Windows 的 UIPI
    #    会静默丢弃 Medium 进程发向 High 窗口的模拟鼠标 —— 表现就是
    #    「点了没反应、鼠标都不动」。必须和野狐同级（High）才点得进去。
    before = integrity_label(DIST)
    subprocess.run(["icacls", DIST, "/setintegritylevel", "High"],
                   capture_output=True)
    after = integrity_label(DIST)
    print(f"完整性标签：{before} → {after}")
    if not after or after.startswith("S-1-16-4096"):
        print("✗ 标签没改成 High，exe 会被 UIPI 挡住（别发出去）")
        return 1

    size = os.path.getsize(DIST)
    digest = md5(DIST)
    print(f"产物：{DIST}")
    print(f"大小 {size:,} 字节　MD5 {digest}")

    if install:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        # ① 旧版本先备份到同一个杂物间目录
        if os.path.isfile(TARGET):
            bak = TARGET + datetime.datetime.now().strftime(".bak-%Y%m%d-%H%M")
            shutil.copy2(TARGET, bak)
            print(f"旧版已备份：{bak}")
        # ② 新 exe 放进杂物间（桌面不放实体文件）
        shutil.copy2(DIST, TARGET)
        subprocess.run(["icacls", TARGET, "/setintegritylevel", "High"],
                       capture_output=True)
        print(f"已装到：{TARGET}（标签 {integrity_label(TARGET)}）")
        print(f"文件 MD5 {md5(TARGET)}（应与上面一致）")
        # ③ 桌面只留快捷方式
        make_shortcut()
        # ④ 兜底：桌面根目录上如果还有 exe / 备份，一并扫进杂物间
        sweep_desktop()
        prune_backups()
    return 0


if __name__ == "__main__":
    sys.exit(main())
