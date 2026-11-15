# -*- coding: utf-8 -*-
"""全局热键 —— 焦点在野狐（或任何别的程序）时也能收到按键

要解决什么
------------------------------------------------------------
老板 2026-10-23 原话：
    「ESC 按键要在弹窗没有打开的时候也生效（按 ESC 就相当于暂停/继续键，
      暂停对局并自动打开弹窗，再按一下 ESC 或暂停/继续键，继续对局）」

tkinter 的 `bind_all("<Escape>")` 只能收到**本程序有焦点**时的按键。
老板下棋时焦点在野狐窗口上，这时按 ESC，Tk 一点都收不到 ——
「ESC 不生效」的真正原因就在这里，不是绑定方式写错了。

解法：Win32 `RegisterHotKey`
------------------------------------------------------------
这是系统级的键盘钩子，由内核直接投递到指定线程的消息队列，
跟谁有焦点无关。代价是**这个键会被独占** —— 注册成功后，
ESC 不再传给野狐。所以必须做成可关闭的开关（见 `enabled`）。

⚠️ 三个必须踩准的点
------------------------------------------------------------
① **RegisterHotKey 必须在有消息循环的线程上注册**。
   Windows 的热键消息（WM_HOTKEY）投递到「注册它的那个线程」，
   所以要么注册在 Tk 主线程（然后靠 Tk 的 after 轮询消息泵），
   要么单开一个线程自己跑 GetMessage 循环。这里选后者：
   注册 + 收消息全在同一个后台线程，职责完整、互不干扰。

② **MOD_NOREPEAT (0x4000) 必须带**。
   不带的话按住 ESC 不放，系统会按键盘自动重复速率疯狂发 WM_HOTKEY
   （每秒几十次），暂停/继续瞬间被翻来覆去切，等于抽风。

③ **注册失败要能说清原因**。
   `RegisterHotKey` 返回 0 时 GetLastError 通常是
   1409 (ERROR_HOTKEY_ALREADY_REGISTERED) —— 别的程序已经占了 ESC。
   这种情况必须告诉用户「换个键」或者「关掉那个程序」，
   而不是静默失败、让用户以为功能没做。
"""
import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 热键修饰符
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000        # 按住不重复触发（关键，见模块注释②）

# 常用虚拟键码
VK_ESCAPE = 0x1B
VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_F12 = 0x7B
VK_PAUSE = 0x13
VK_SPACE = 0x20

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001

ERR_ALREADY = 1409

# 键盘码 → 给人看的名字
VK_NAMES = {
    VK_ESCAPE: "ESC", VK_PAUSE: "Pause", VK_SPACE: "空格",
    VK_F1: "F1", VK_F2: "F2", VK_F3: "F3", VK_F4: "F4",
    VK_F5: "F5", VK_F6: "F6", VK_F7: "F7", VK_F8: "F8",
    VK_F9: "F9", VK_F10: "F10", VK_F11: "F11", VK_F12: "F12",
}


class GlobalHotkey:
    """一个全局热键。用 start() 起、stop() 停，可反复开关。

    回调 `on_fire()` 在**后台线程**里被调用 —— 千万不要在回调里直接
    操作 tkinter 控件（Tk 不是线程安全的，会随机崩或卡死）。
    正确做法：回调里只往 queue 里塞一条消息，让主线程的 _poll 去处理。
    """

    def __init__(self, vk=VK_ESCAPE, mods=MOD_NOREPEAT, on_fire=None,
                 name=None):
        self.vk = vk
        self.mods = mods
        self.on_fire = on_fire
        self.name = name or VK_NAMES.get(vk, f"VK_{vk:02X}")
        self._id = 0             # 0 = 没注册
        self._thread = None
        self._alive = False
        self._tid = None         # 注册热键的线程 id（PostThreadMessage 用）
        self._tid_ready = threading.Event()
        self.last_error = None
        self.fire_count = 0

    # ------------------------------------------------------------ 生命周期

    def start(self):
        """注册并开始收消息。返回 True=成功。

        阻塞到注册结果确定才返回（最多等 1.5 秒），这样调用方
        能立刻知道成功/失败并给出提示，不用瞎猜。
        """
        if self._alive:
            return True
        self._alive = True
        self.last_error = None
        self._tid_ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="GlobalHotkey", daemon=True)
        self._thread.start()
        self._tid_ready.wait(1.5)
        return self._id != 0

    def stop(self, wait=1.2):
        """注销热键（把 ESC 还给系统）。返回 True = 确认已释放

        ⚠️ 必须在**注册它的那个线程**里注销（2026-10-23 实测抓到）
        ----------------------------------------------------
        `UnregisterHotKey(None, id)` 里 hWnd=NULL 的含义是
        「**当前线程**关联的热键」—— 从主线程调它去注销后台线程
        注册的热键，**不生效也不报错**（返回 0 被当成功忽略了）。
        第一版就是这样：先在这里调一次（无效），再靠 WM_QUIT
        让线程退出时自己注销一次（有效，但要等 join）。

        现在的做法：只发退出信号，让线程在 finally 里自己注销，
        然后**轮询确认 _id 真的变 0** 才返回。
        这样上层（GUI）能确定「ESC 已还给系统」，而不是「大概还了」。
        """
        if not self._alive and not self._id:
            return True
        self._alive = False
        tid = self._tid
        if tid:
            try:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(wait)
        # 再确认：线程退出路径的 finally 里会 UnregisterHotKey
        t0 = time.time()
        while self._id and time.time() - t0 < 0.5:
            time.sleep(0.01)
        self._thread = None
        self._tid = None
        return self._id == 0

    def restart(self, vk=None, mods=None):
        """换键或重新注册（先停再起）"""
        self.stop()
        if vk is not None:
            self.vk = vk
            self.name = VK_NAMES.get(vk, f"VK_{vk:02X}")
        if mods is not None:
            self.mods = mods
        return self.start()

    @property
    def enabled(self):
        return self._id != 0

    # ------------------------------------------------------------ 内部

    def _run(self):
        """后台线程：注册 → 消息循环 → 注销"""
        tid = kernel32.GetCurrentThreadId()
        self._tid = tid

        # ⚠️ 注册前先确保这个线程有自己的消息队列。
        #    GetMessage 会隐式创建，但 RegisterHotKey 需要一个队列来
        #    投递 WM_HOTKEY；先 PeekMessage 一下是 Win32 的经典做法，
        #    避免「注册成功了但消息永远收不到」这种玄学故障。
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        hid = 0xB001        # 自定义 id，只要本进程内唯一即可
        ok = user32.RegisterHotKey(None, hid, self.mods, self.vk)
        if not ok:
            err = kernel32.GetLastError()
            self.last_error = err
            self._tid_ready.set()
            self._alive = False
            # 	err==1409 表示被别的程序占了，不算程序 bug，
            # 	调用方据此给出「换个键」的提示
            return
        self._id = hid
        self._tid_ready.set()

        try:
            while self._alive:
                # ⚠️ 用 PeekMessage + sleep 而不是阻塞 GetMessage
                #    （2026-10-23）
                #    阻塞式 GetMessage 在 Windows 上偶尔会因为
                #    「消息已取出但线程被挂起」而让 stop() 卡在 join 上。
                #    PeekMessage 轮询加 8ms 睡眠，CPU 几乎为 0，
                #    退出也更干脆。
                got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE)
                if got:
                    if msg.message == WM_QUIT:
                        break
                    if msg.message == WM_HOTKEY and msg.wParam == hid:
                        self.fire_count += 1
                        # 回调在后台线程 —— 只许塞队列，别碰 Tk
                        cb = self.on_fire
                        if cb:
                            try:
                                cb()
                            except Exception:
                                pass
                    continue
                time.sleep(0.008)
        finally:
            if self._id:
                try:
                    user32.UnregisterHotKey(None, self._id)
                except Exception:
                    pass
                self._id = 0
            self._alive = False

    # ------------------------------------------------------------ 自检

    def describe(self):
        """给人看的一句状态"""
        if self._id:
            return f"全局热键 {self.name}：已生效（任何窗口都管用）"
        if self.last_error == ERR_ALREADY:
            return (f"全局热键 {self.name}：被别的程序占用（错误 {ERR_ALREADY}）"
                    f" —— 关掉占用的程序，或换个键")
        if self.last_error:
            return f"全局热键 {self.name}：注册失败（错误 {self.last_error}）"
        return f"全局热键 {self.name}：未启用"


if __name__ == "__main__":
    import sys

    print("测全局热键 —— 请把焦点切到别的窗口，然后按 ESC")
    print("按 3 次后自动结束（或直接关掉窗口）\n")

    n = [0]

    def fired():
        n[0] += 1
        print(f"  第 {n[0]} 次收到 ESC  （线程：后台）", flush=True)

    hk = GlobalHotkey(on_fire=fired)
    ok = hk.start()
    print(hk.describe())
    if not ok:
        sys.exit(2)

    t0 = time.time()
    while n[0] < 3 and time.time() - t0 < 25:
        time.sleep(0.1)

    hk.stop()
    print(f"\n共收到 {n[0]} 次；停用后 enabled={hk.enabled}")
