#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
梧州学院校园网助手 - GUI
================================
基于 campus_login.py 核心模块, 提供图形界面:
  - 登录 / 注销 / 状态查询 / 守护模式(掉线自动重连)
  - 系统托盘: 手动收起或最小化启动, 双击恢复, 右键菜单, 气泡通知
  - 后台内存优化: 最小化到托盘时压缩工作集(EmptyWorkingSet)并暂停周期刷新
  - 账号·服务器·自动化设置
  - 开机自启(注册表 HKCU Run) + 启动自动登录

命令行:
  python campus_gui.py [--minimized]    # 仅显式指定时最小化启动
  python campus_gui.py --demo           # 隔离的离线界面演示
"""

import ctypes
import gc
import ipaddress
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from campus_ui import PagesMixin, C

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as cl
import campus_traffic as ct

try:
    import winreg
except ImportError:
    winreg = None

try:
    from tray_icon import TrayIcon
except Exception:
    TrayIcon = None

APP_TITLE = "梧州学院校园网助手"
APP_VERSION = "2026.09.24.1"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "CampusNetLogin"
LOG_MAX_LINES = 500

# 流量采样间隔(秒): 图省电兼顾精度。守护线程每轮也会采样一次, 二者去重不叠加。
TRAFFIC_INTERVAL = 5.0
# 采样间隔允许范围, 与 campus_login._INT_FIELDS["traffic_interval"] 保持一致
TRAFFIC_MIN_INTERVAL = 5.0
TRAFFIC_MAX_INTERVAL = 3600.0
# 校园网状态缓存有效期(秒): inspect_network 要拉一次 PowerShell(约 370ms),
# 采样等高频路径不该每轮都重查。必须**大于**采样间隔, 否则每轮都过期, 缓存形同虚设。
TRAFFIC_NET_TTL = 30.0
# 明细表展示的天数
TRAFFIC_HISTORY_DAYS = 14

# ------------------------------------------------------------ 开机自启

def traffic_interval_of(cfg):
    """配置里的采样间隔(秒), 规整到允许范围.

    单独成函数是为了让"夹取"这一条规则只有一处实现: 采样循环与界面文案都
    调它, 否则改了下限就会两边不一致。
    """
    raw = cl._to_int((cfg or {}).get("traffic_interval"))
    if raw is None:
        raw = TRAFFIC_INTERVAL
    return max(TRAFFIC_MIN_INTERVAL, min(TRAFFIC_MAX_INTERVAL, raw))


def _setup_dpi():
    """开启进程 DPI 感知, 必须在 tk.Tk() **之前** 调用.

    这是"字体太小"的根因: 原实现把 SetProcessDpiAwareness 放在 App.__init__ 里,
    那时 tk.Tk() 已经执行完毕。Tk 创建窗口时会向系统查询屏幕 DPI 并缓存为
    tk scaling(96 DPI 基准 = 1.3323), 之后再改进程感知状态并不会让它重算 ——
    结果 150% 缩放的屏幕上, 窗口按真实物理像素绘制, 而字号仍按 96 DPI 计算,
    文字只有应有大小的 2/3。

    返回系统 DPI 倍数(1.0 = 96 DPI, 1.5 = 144 DPI)。
    """
    if sys.platform != "win32":
        return 1.0
    # 依次尝试: Per-Monitor V2 -> 系统级感知 -> 旧 API
    # (exe 清单未声明 dpiAware, 故此处可自由设置)
    try:
        # -4 = PER_MONITOR_AWARE_V2, 多显示器且各屏缩放不同时表现最好
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return _dpi_factor_from_system()
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 1 = SYSTEM_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    return _dpi_factor_from_system()


# 进程级 DPI 信息, 由 main() 在建立窗口前写入
DPI_FACTOR = 1.0     # 系统 DPI 倍数(144 DPI -> 1.5); Tk 会据此自动设 scaling
UI_EXTRA = 1.0       # 用户额外放大倍数(CAMPUS_UI_SCALE), 默认 1.0
UI_SCALE = 1.0       # 实际总倍数 = DPI_FACTOR * UI_EXTRA, 用于换算窗口尺寸


def _dpi_factor_from_system():
    """读取当前屏幕 DPI 相对 96 的倍数."""
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


def _ui_scale_override():
    """允许用环境变量额外放大界面(无障碍/视力不佳时).

    用法: set CAMPUS_UI_SCALE=1.25   (在系统 DPI 基础上再乘该系数)
    """
    raw = os.environ.get("CAMPUS_UI_SCALE", "").strip()
    if not raw:
        return 1.0
    try:
        val = float(raw)
    except ValueError:
        return 1.0
    return min(3.0, max(0.5, val))


def _px(n):
    """把 96 DPI 下设计的像素值换算为当前 DPI 的像素值.

    Tk 只对"点"为单位的字号自动应用 scaling; padx/pady/wraplength 这类
    像素值不会自动放大, 若不换算, 高 DPI 下文字变大而留白不变, 版面会显得拥挤。
    """
    return max(1, int(round(n * UI_SCALE))) if n else 0


def _launch_target():
    if getattr(sys, "frozen", False):
        return '"{}" --minimized'.format(sys.executable)
    py = sys.executable
    if os.path.basename(py).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(cand):
            py = cand
    return '"{}" "{}" --minimized'.format(py, os.path.abspath(__file__))


def autostart_enabled():
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    if not winreg:
        raise OSError("仅支持 Windows")
    if enable:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _launch_target())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, AUTOSTART_NAME)
        except FileNotFoundError:
            pass


def trim_working_set():
    """压缩进程工作集(Windows): 最小化到托盘时调用, 显著降低后台内存占用."""
    try:
        gc.collect()
        if sys.platform == "win32":
            # GetCurrentProcess() 的伪句柄就是 (HANDLE)-1, 直接构造避免符号扩展问题
            ctypes.windll.psapi.EmptyWorkingSet(ctypes.c_void_p(-1))
    except Exception:
        pass


# ------------------------------------------------------------ GUI 主类

class App(PagesMixin):
    def __init__(self, root):
        self.root = root
        self.ui_scale = UI_SCALE
        self.config_path = cl.CONFIG_PATH
        self.q = queue.Queue()
        self.watch_stop = threading.Event()
        # 自动登录与守护是两个独立任务, 原先共用 watch_stop,
        # 用户点"停止守护"会连带中断自动登录的重试。
        self.auto_login_stop = threading.Event()
        self.login_stop = threading.Event()
        self.watch_thread = None
        self.auto_login_thread = None
        self.busy = False
        self._refreshing = False          # refresh_status 在途标志(去重)
        self._quitting = False
        self.days, self.month = [], {}
        self._page = None                 # 当前导航页(供悬停高亮判断)
        self.tray = None
        self.hidden_in_tray = False
        self._network_message = None

        # ---- 流量统计 ----
        self.meter = ct.TrafficMeter()
        self.traffic_stop = threading.Event()
        self.traffic_thread = None
        self.traffic_scan_stop = threading.Event()
        self.traffic_scan_thread = None
        self._traffic_sampling = False     # 采样在途去重(定时器与守护共用)
        self._traffic_profile = ""         # 最近一次采样所用的网络配置名
        self._last_online = None           # 上一次认证状态, 用于识别"刚上线"
        self._session_loaded = False       # 服务端会话是否已自动查询过一次
        self._campus_cache = None          # (monotonic, NetworkState) 供高频采样复用

        cl.LOG_HOOK = self._hook_log

        root.title("{} · {}".format(APP_TITLE, APP_VERSION))
        root.configure(bg=C["bg"])

        # Set one explicit point-to-pixel conversion for every native widget.
        root.tk.call("tk", "scaling", UI_SCALE * 96 / 72)

        # 窗口图标(exe 模式: 先在 _MEIPASS 找 PyInstaller --add-data 打入的 .ico,
        # 再 fallback 到 APP_DIR 旁边的 .ico; 脚本模式直接用 APP_DIR)
        try:
            ico = self._find_icon()
            if ico:
                root.iconbitmap(ico)
        except Exception:
            pass

        # 样式必须在 _build 之前建好(页面标题绑定 var_page)
        self.var_page = tk.StringVar(value="状态总览")

        self._style()
        self._build()
        self._load_settings_ui()
        self._observe_settings()
        # 基准 1180x800 是 96 DPI 下的逻辑尺寸; 高 DPI 下 Tk 的逻辑坐标
        # 已是物理像素, 故按 UI_SCALE 放大才能保持相同的视觉占比。
        self._center_window(_px(1180), _px(800))

        root.after(150, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_periodic()

        self.refresh_status()
        self._start_traffic()
        cfg = cl.load_config()
        if int(cfg.get("auto_login_on_start", 0)) == 1 and not cfg.get("auto_watch"):
            self.auto_login_thread = threading.Thread(
                target=self._task_auto_login, daemon=True)
            self.auto_login_thread.start()
        if int(cfg.get("auto_watch", 0)) == 1:
            self._watch_toggle()

    # ---------- 工具
    def _find_icon(self):
        meipass = getattr(sys, "_MEIPASS", None)
        candidates = []
        if meipass:
            candidates.append(os.path.join(meipass, "campus.ico"))
        candidates.append(os.path.join(cl.APP_DIR, "campus.ico"))
        for ico in candidates:
            if os.path.exists(ico):
                return ico
        return ""

    def _center_window(self, w, h):
        self.root.update_idletasks()
        left, top = 0, 0
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if sys.platform == "win32":
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            api = ctypes.windll.user32
            api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            api.MonitorFromWindow.restype = wintypes.HANDLE
            api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
            monitor = api.MonitorFromWindow(self.root.winfo_id(), 2)
            if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
                left, top = info.rcWork.left, info.rcWork.top
                sw, sh = info.rcWork.right - left, info.rcWork.bottom - top
        w = max(1, min(w, sw - _px(32)))
        h = max(1, min(h, sh - _px(70)))
        x = left + (sw - w) // 2
        y = top + max(_px(16), (sh - h) // 3)
        self.root.geometry("{}x{}+{}+{}".format(w, h, x, y))
        self.root.minsize(min(_px(900), w), min(_px(620), h))
        self.root.update_idletasks()

    # ---------- 队列/日志
    def _qput(self, kind, payload=None):
        self.q.put((kind, payload))

    def _hook_log(self, msg):
        self.q.put(("log", msg))

    def _poll(self):
        """队列轮询.

        原实现把 handler(payload) 放在 try 内、而 self.root.after(...) 放在
        try/except 之外: 任一 handler 抛异常(例如 _real_quit 之后对已销毁控件
        调 config(), 抛 TclError)都会让 after 不再注册 —— 事件泵被永久切断,
        界面从此失去日志/状态/托盘响应, 而 --windowed 下连 stderr 都没有。
        现在 handler 异常逐个吞掉并记入日志, after 放进 finally 保证续期。
        """
        try:
            while True:
                kind, payload = self.q.get_nowait()
                handler = {
                    "log": self._on_log,
                    "status": self._on_status,
                    "status_err": self._on_status_err,
                    "dirty": lambda p: self.refresh_status(),
                    "done": self._on_done,
                    "watch": self._on_watch_state,
                    "tray": self._on_tray_cmd,
                    "balloon": self._on_balloon,
                    "network": self._on_network,
                    "traffic": self._on_traffic,
                    "traffic_session": self._on_traffic_session,
                    "traffic_busy": self._on_traffic_busy,
                    "refresh_done": self._on_refresh_done,
                }.get(kind)
                if handler is None:
                    continue  # 未知消息类型: 跳过, 不中断轮询链
                try:
                    handler(payload)
                except tk.TclError as e:
                    # 控件可能已随窗口销毁。这里只吞掉本次异常, 不终止轮询 ——
                    # 窗口真的没了的话, 下面续期用的 after() 会再次抛 TclError,
                    # 由 finally 里的分支统一收尾(避免一次偶发 TclError 就废掉事件泵)。
                    if "application has been destroyed" in str(e) or \
                            "invalid command name" in str(e):
                        self._quitting = True
                        return
                except Exception as e:
                    # 单个 handler 出错不应终止整个事件泵
                    try:
                        self._on_log("[错误] 界面处理 {} 消息失败: {}".format(kind, e))
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            if not self._quitting:
                try:
                    self.root.after(150, self._poll)
                except tk.TclError:
                    self._quitting = True   # 窗口已销毁, 停止轮询

    def _schedule_periodic(self):
        self.root.after(60000, self._periodic_tick)

    def _periodic_tick(self):
        # 托盘驻留期间暂停周期刷新(状态不可见, 且守护线程自带检测),
        # 降低后台网络与 CPU 开销
        if not self.busy and not self.hidden_in_tray:
            self.refresh_status()
        self._schedule_periodic()

    def _on_log(self, msg):
        ts = time.strftime("%H:%M:%S")
        if any(word in msg for word in ("[错误]", "未成功", "失败", "异常", "密码错误")):
            tag = "err"
        elif msg.startswith("[守护]"):
            tag = "info"
        elif "成功" in msg or "完成" in msg:
            tag = "ok"
        else:
            tag = None
        follow_tail = self.txt_log.yview()[1] >= 0.99
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", "[{}] {}\n".format(ts, msg), tag)
        # 日志上限, 超出裁剪最旧部分(守护模式长跑防内存膨胀)
        lines = int(self.txt_log.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.txt_log.delete("1.0", "{}.0".format(lines - LOG_MAX_LINES))
        if follow_tail:
            self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _clear_log(self):
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")

    def _set_pill(self, kind, text):
        bgfg = {"ok": (C["ok_soft"], C["ok_fg"]),
                "bad": (C["danger_soft"], C["danger_fg"]),
                "na": (C["na_soft"], C["na_fg"])}[kind]
        self.lbl_net_pill.config(text=text, bg=bgfg[0], fg=bgfg[1])

    def _on_status(self, payload):
        st, net = payload
        online = st["online"]
        # 认证刚发生变化时立刻补一次流量采样: 否则最新增量最多要等一个
        # 采样周期才显示出来(状态刷新是 60 秒一次, 与采样周期不同步)。
        previous = self._last_online
        self._last_online = online
        if previous is not None and previous != online and online:
            threading.Thread(target=self._sample_after_reconnect, daemon=True).start()
        self.var_state.set("在线" if online else "离线")
        if hasattr(self, "var_connection_hint"):
            self.var_connection_hint.set(
                "校园网已认证，互联网连接正常。" if online and net == "online" else
                "校园网已认证，但互联网尚未连通。可以运行连接诊断。" if online else
                "校园网尚未认证。点击连接，或先在账号设置中填写登录信息。")
            self.btn_login.config(text="重新认证" if online else "连接校园网")
        self.lbl_state_dot.config(fg=C["ok"] if online else C["danger"])
        self.var_account.set(st["uid"] or "-")
        self.var_ip.set(st["ip"] or "-")
        self.var_mac.set(st["mac"] or "-")
        # 服务类型取自当前配置(不是内核返回值), 便于确认 R3 是否选对
        try:
            isp = str(cl.load_config().get("isp", "1"))
        except Exception:
            isp = "1"
        self.var_isp_now.set(cl.ISP_NAMES.get(isp, isp))
        if net == "online":
            self._set_pill("ok", "互联网 · 已连通")
        elif net == "offline":
            self._set_pill("bad", "互联网 · 未连通")
        else:
            self._set_pill("na", "互联网 · 未知")
        if self.tray and self.tray.visible:
            self.tray.set_tip("{} · {} · {}".format(
                APP_TITLE, "在线" if online else "离线", st["ip"] or "无IP"))
        self.var_status.set("状态已刷新")

    def _sample_after_reconnect(self):
        try:
            state = self._campus_state()
            if state.ready and not self._quitting:
                self._sample_traffic(state.interface_index, state.profile)
        except Exception as exc:
            self._qput("log", "[流量] 重新采样失败: {}".format(exc))

    def _on_status_err(self, msg):
        self._last_online = None
        self._session_loaded = False
        self.var_state.set("无法连接服务器")
        self.lbl_state_dot.config(fg=C["danger"])
        self._set_pill("na", "互联网 · 未知")
        self._clear_identity()
        self.btn_login.config(text="连接校园网")
        self.var_session.set("无法确认当前会话，请恢复连接后刷新")
        self.var_connection_hint.set("请检查校园网连接与服务器设置，再刷新状态。")
        self.btn_refresh.config(text="刷新状态", state="normal")
        self.var_status.set(msg)
        self._on_log("[错误] " + msg)

    def _on_network(self, state):
        if state.message != self._network_message:
            self._on_log("[网络] " + state.message)
            self._network_message = state.message
        if not state.ready:
            self.var_state.set("等待校园网")
            self.lbl_state_dot.config(fg=C["muted"])
            self._set_pill("na", "认证 · 已暂停")
            self.var_status.set(state.message)
            self._clear_identity()
            self.var_connection_hint.set(state.message + "。连接指定校园网后可重试。")
            self.btn_login.config(text="连接校园网")
            self._last_online = None
            self._session_loaded = False
            self.var_session.set("当前未连接校园网，服务端会话已失效")
            self._traffic_profile = ""
            self.var_traffic_net.set("未连接校园网 · 已暂停采样")

    def _clear_identity(self):
        for variable in (self.var_account, self.var_ip, self.var_mac):
            variable.set("—")

    def _campus_ready(self, cfg):
        state = cl.inspect_network(cfg)
        self._campus_cache = (time.monotonic(), state)
        self._qput("network", state)
        return state.ready

    def _campus_state(self, max_age=TRAFFIC_NET_TTL):
        """带 TTL 缓存的校园网状态(供高频采样使用).

        inspect_network 要拉起一次 PowerShell 枚举网络配置, 实测约 370 ms;
        流量采样周期是 5 秒, 每轮都查一次纯属浪费, 而在 Tk 主线程里查还会
        卡住界面。采样真正需要的只是"用哪块网卡", 网络切换由状态刷新(60 秒)
        与守护循环发现 —— 缓存过期后自然跟上。
        """
        cached = getattr(self, "_campus_cache", None)
        now = time.monotonic()
        if cached is not None and cached[1] is not None and now - cached[0] < max_age:
            return cached[1]
        state = cl.inspect_network(cl.load_config())
        self._campus_cache = (now, state)
        return state

    def _on_done(self, _):
        self._set_busy(False, "就绪")

    def _on_watch_state(self, running):
        try:
            if running:
                self.btn_watch.config(text="停止自动重连", state="normal")
                self._btn_colors(self.btn_watch, C["ghost"], C["ghost_h"], C["ghost_fg"])
                self.var_watch.set("自动重连已开启")
                self.lbl_watch.config(fg=C["ok"])
                self.lbl_side_watch.config(text="已开启 · 保持连接", fg=C["ok_fg"])
            else:
                self.btn_watch.config(text="开启自动重连", state="normal")
                self._btn_colors(self.btn_watch, C["ghost"], C["ghost_h"], C["ghost_fg"])
                self.var_watch.set("自动重连未开启")
                self.lbl_watch.config(fg=C["muted"])
                self.lbl_side_watch.config(text="未开启", fg=C["side_fg"])
                # 守护停止时若窗口仍在托盘, 恢复显示让用户看到结果
                if self.hidden_in_tray:
                    self._restore_from_tray()
        except tk.TclError:
            pass  # 窗口已销毁

    # ---------- 托盘
    def _ensure_tray(self):
        if self.tray is not None:
            return True
        if TrayIcon is None:
            return False
        try:
            self.tray = TrayIcon(
                icon_path=self._find_icon(),
                tooltip=APP_TITLE,
                menu=[("open", "打开主界面"),
                      ("login", "立即登录"),
                      ("logout", "注销下线"),
                      None,
                      ("quit", "退出程序")],
                on_command=lambda c: self._qput("tray", c))
            self.tray.start()
            return True
        except Exception as e:
            self._on_log("[错误] 托盘初始化失败: {}".format(e))
            self.tray = None
            return False

    def _minimize_to_tray(self):
        if self.hidden_in_tray:
            return
        if not self._ensure_tray():
            self.root.iconify()  # 托盘不可用时退回任务栏最小化
            return
        try:
            added = self.tray.show()
        except Exception:
            added = False
        if not added:
            self.root.iconify()
            self._on_log("[错误] 托盘图标添加失败，已最小化到任务栏")
            return
        self.root.withdraw()
        self.hidden_in_tray = True
        self.tray.set_tip("{} · {}".format(APP_TITLE, self.var_state.get()))
        trim_working_set()  # 压缩后台内存
        self._on_log("[系统] 已最小化到系统托盘, 双击托盘图标可恢复窗口")

    def _restore_from_tray(self):
        if not self.hidden_in_tray:
            return
        if self.tray:
            try:
                self.tray.hide()
            except Exception:
                pass
        self.hidden_in_tray = False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            # 窗口已被销毁(退出竞态): 忽略即可, 不再刷新状态
            return
        self.refresh_status()

    def _on_tray_cmd(self, cmd):
        if cmd in ("__dblclick__", "open"):
            self._restore_from_tray()
        elif cmd == "login":
            self.login_click()
        elif cmd == "logout":
            self.logout_click()
        elif cmd == "quit":
            self._restore_from_tray()
            self._request_quit()

    def _on_balloon(self, payload):
        if self.tray and self.tray.visible:
            title, text, level = payload
            self.tray.balloon(title, text, level)

    # ---------- 流量统计
    def _start_traffic(self):
        if self.traffic_thread and self.traffic_thread.is_alive():
            return
        self.traffic_stop.clear()
        self.traffic_thread = threading.Thread(target=self._task_traffic_loop,
                                               daemon=True)
        self.traffic_thread.start()

    def _task_traffic_loop(self):
        """周期性采样本机网卡计数器.

        只在校园网就绪时采样: 非校园网期间读的是家庭网络/热点的计数器, 计入
        校园网账上就是错的。配置里 traffic_interval 可调(5-3600 秒)。
        """
        interval = TRAFFIC_INTERVAL
        warned = False
        while not self.traffic_stop.is_set() and not self._quitting:
            try:
                cfg = cl.load_config()
                interval = traffic_interval_of(cfg)
                # TTL 必须大于采样间隔, 否则每轮都过期、缓存毫无作用;
                # 网络切换由状态刷新(60 秒)与守护循环负责发现。
                state = self._campus_state(max_age=max(interval, TRAFFIC_NET_TTL))
                if not state.ready:
                    self.traffic_stop.wait(interval)
                    continue
                self._sample_traffic(state.interface_index, state.profile)
                if not warned and self.meter.last_error:
                    warned = True
                    self._qput("log", "[流量] 统计写入失败: {}".format(self.meter.last_error))
            except Exception as e:
                self._qput("log", "[流量] 采样异常: {}".format(e))
            self.meter.flush()
            self.traffic_stop.wait(interval)
        # 退出前尽量把增量落盘, 否则杀掉进程会丢掉最近一个 flush 周期
        self.meter.flush(force=True)

    def _sample_traffic(self, interface_index, profile=None):
        """采样一次并把结果送到界面. 采样在途时直接跳过(守护线程也会调用).

        首次拿到结果时顺带查一次服务端会话: 否则切到流量页只看到「尚未查询」,
        用户得先手动点一次刷新才有内容。
        """
        if self._traffic_sampling:
            return
        self._traffic_sampling = True
        try:
            delta = self.meter.sample(interface_index)
            if delta is None:
                return
            self._qput("traffic", (self.meter.summary(days=TRAFFIC_HISTORY_DAYS), profile))
            if not self._session_loaded and not self._quitting:
                self._session_loaded = True
                self.traffic_click()
        finally:
            self._traffic_sampling = False

    def traffic_click(self):
        """手动刷新: 采样一次 + 查询服务端会话(走网络, 故放后台线程)."""
        if self.traffic_scan_thread and self.traffic_scan_thread.is_alive():
            return
        self._session_loaded = True
        self._qput("traffic_busy", True)
        self.traffic_scan_stop.clear()
        self.traffic_scan_thread = threading.Thread(target=self._task_traffic_scan,
                                                    daemon=True)
        self.traffic_scan_thread.start()

    def _task_traffic_scan(self):
        try:
            cfg = cl.load_config()
            state = self._campus_state(max_age=TRAFFIC_NET_TTL)
            if not state.ready:
                self._qput("log", "[流量] " + state.message)
                self._qput("traffic", (self.meter.summary(days=TRAFFIC_HISTORY_DAYS),
                                       None))
                self._qput("traffic_session", state.message)
                return
            delta = self.meter.sample(state.interface_index)
            self.meter.flush(force=True)
            self._qput("traffic", (self.meter.summary(days=TRAFFIC_HISTORY_DAYS),
                                   state.profile))
            if delta is None:
                self._qput("log", "[流量] 读取网卡计数器失败, 本次未计入")
            else:
                self._qput("log", "[流量] 本次采样: 下行 {} · 上行 {}".format(
                    ct.format_bytes(delta[0]), ct.format_bytes(delta[1])))
            st = cl.check_status(cfg)
            session = cl.server_session(cfg, status=st)
            self._qput("traffic_session", cl.session_line(session))
        except Exception as e:
            self._qput("log", "[流量] 刷新失败: {}".format(e))
            self._qput("traffic_session", "查询失败: {}".format(e))
        finally:
            self._qput("traffic_busy", False)

    def _on_traffic_busy(self, busy):
        self.btn_traffic.config(text="正在刷新…" if busy else "刷新统计",
                                state="disabled" if busy else "normal")

    def traffic_reset_click(self):
        if not messagebox.askyesno(
                APP_TITLE, "确定清空流量统计的全部历史累计吗?\n此操作不可撤销。"):
            return
        if self.meter.reset():
            self._qput("log", "[流量] 统计已重置; 下次采样重新建立基线")
        else:
            self._qput("log", "[流量] 重置失败: {}".format(self.meter.last_error))
        self._qput("traffic", (self.meter.summary(days=TRAFFIC_HISTORY_DAYS),
                               self._traffic_profile))

    def _on_traffic(self, payload):
        data, profile = payload
        self._traffic_profile = profile or ""
        self.var_traffic_net.set(
            "统计网卡: {} · 采样间隔 {:.0f} 秒 · 本机估算".format(
                profile, traffic_interval_of(cl.load_config())) if profile else
            "未连接校园网 · 已暂停采样")
        # 长路径允许换行。
        self.var_traffic_path.set("统计文件: " + (data.get("path") or ""))
        for key, label in (("today", "今日"), ("month", "本月"), ("total", "累计")):
            bucket = data[key]
            down, up = self.var_traffic[key]
            down.set("↓ " + ct.format_bytes(bucket["rx"]))
            up.set("↑ " + ct.format_bytes(bucket["tx"]))
            self.var_traffic_total[key].set(ct.format_bytes(bucket["sum"]))
        # Stable date IDs preserve selection and scrolling during background samples.
        rows = ct.history_rows(data, TRAFFIC_HISTORY_DAYS)
        retained = {row[0] for row in rows}
        for item in self.tree_traffic.get_children():
            if item not in retained:
                self.tree_traffic.delete(item)
        for index, row in enumerate(rows):
            if self.tree_traffic.exists(row[0]):
                self.tree_traffic.item(row[0], values=row)
                self.tree_traffic.move(row[0], "", index)
            else:
                self.tree_traffic.insert("", index, iid=row[0], values=row)

    def _on_traffic_session(self, text):
        self.var_session.set(text)

    # ---------- 后台任务
    def refresh_status(self):
        """刷新状态卡.

        调用方有三处(60 秒周期刷新、托盘恢复、操作完成后的 dirty 消息), 网络慢
        时(check_status 5s + check_internet 最长 10s)会叠加并发线程, 且结果乱序
        覆盖。加在途标志去重, 单次刷新结束前忽略新的刷新请求。
        """
        if self._refreshing or self._quitting:
            return
        self._refreshing = True
        self.btn_refresh.config(text="正在刷新…", state="disabled")
        threading.Thread(target=self._task_refresh, daemon=True).start()

    def _task_refresh(self):
        try:
            cfg = cl.load_config()
            if not self._campus_ready(cfg):
                return
            st = cl.check_status(cfg)
            net = cl.check_internet(cfg)
            self._qput("status", (st, net))
        except Exception as e:
            self._qput("status_err", str(e))
        finally:
            self._refreshing = False
            self._qput("refresh_done")

    def _on_refresh_done(self, _payload=None):
        self.btn_refresh.config(text="刷新状态", state="normal")

    def login_click(self):
        if self.busy:
            return
        if self._quitting:
            return
        cfg = cl.load_config()
        if not cfg.get("account") or not cfg.get("password"):
            self.show_page("account")
            self.ent_account.focus_set()
            messagebox.showwarning(APP_TITLE,
                                   "请先在『账号』页填写并保存账号密码")
            return
        self._set_busy(True, "正在登录…")
        self.login_stop.clear()
        threading.Thread(target=self._task_login, daemon=True).start()

    def _task_login(self):
        try:
            cfg = cl.load_config()
            ok, msg = cl.do_login(cfg, verbose=True, cancel=self.login_stop)
            self._qput("log", "登录流程完成: {}".format(msg) if ok
                       else "登录未成功: {}".format(msg))
        except Exception as e:
            self._qput("log", "登录请求异常: {}".format(e))
        finally:
            self._qput("dirty")
            self._qput("done")

    def test_click(self):
        """连通性自检, 等价于 CLI 的 `campus_login.py test`."""
        if self.busy:
            return
        self._set_busy(True, "正在测试连通性…")
        threading.Thread(target=self._task_test, daemon=True).start()

    def _task_test(self):
        try:
            cfg = cl.load_config()
            if not self._campus_ready(cfg):
                return
            self._qput("log", "—— 连通性测试: {} ——".format(cfg["server"]))
            try:
                st = cl.check_status(cfg)
                self._qput("log", "chkstatus 正常 -> 在线: {}, IP: {}, MAC: {}".format(
                    st["online"], st["ip"] or "?", st["mac"] or "?"))
            except Exception as e:
                self._qput("log", "[错误] chkstatus 失败: {}".format(e))
                self._qput("log", "[错误] 请确认已连接校园网, drcom_port 配置正确(默认80)")
                return
            portal = cl.load_portal_config(cfg, status=st)
            self._qput("log", "loadConfig -> program_index={} | enable_r3={} | en_md5={}".format(
                portal["program_index"], portal["enable_r3"], portal["en_md5"]))
            net = cl.check_internet(cfg)
            self._qput("log", "互联网连通性: {}".format(
                {"online": "通", "offline": "不通", "unknown": "未知"}[net]))
            if net == "unknown":
                self._qput("log", "[错误] 两个探测目标均不可达, 可能被代理/防火墙拦截")
            self._qput("dirty")
        except Exception as e:
            self._qput("log", "[错误] 连通性测试异常: {}".format(e))
        finally:
            self._qput("done")

    def logout_click(self):
        if self.busy:
            return
        if self._quitting:
            return
        asking = "确定要注销当前登录吗? 会断开校园网连接"
        if self.watch_thread and self.watch_thread.is_alive():
            asking += "\n\n将同时停止自动登录与守护，等待正在进行的认证结束后注销。"
        if not messagebox.askyesno(APP_TITLE, asking):
            return
        # 守护运行中则一并停止, 否则注销成功后会立刻被守护线程重连,
        # 用户"下线"的意图在 <= 一个检测周期内被程序自己推翻。
        self.watch_stop.set()
        self.auto_login_stop.set()
        self.login_stop.set()
        self._qput("log", "[系统] 已取消后台登录，等待认证任务结束后注销")
        self._set_busy(True, "正在注销…")
        threading.Thread(target=self._task_logout, daemon=True).start()

    def _task_logout(self):
        try:
            cfg = cl.load_config()
            ok = cl.do_logout(cfg, verbose=True)
            self._qput("log", "注销{}".format("成功" if ok else "失败"))
        except Exception as e:
            self._qput("log", "注销请求异常: {}".format(e))
        finally:
            self._qput("dirty")
            self._qput("done")

    def _task_auto_login(self):
        self._qput("log", "启动自动登录已开启，等待校园网就绪…")
        max_tries = 6
        attempts = 0
        while attempts < max_tries and not self.auto_login_stop.is_set() and not self._quitting:
            try:
                cfg = cl.load_config()
                if not self._campus_ready(cfg):
                    if self.auto_login_stop.wait(30):
                        return
                    continue  # 等待 Wi-Fi 不消耗登录次数，也不发认证请求
                ok, msg = cl.do_login(cfg, verbose=False, cancel=self.auto_login_stop)
                if self.auto_login_stop.is_set() or self._quitting:
                    return
                if msg.startswith("等待校园网"):
                    if self.auto_login_stop.wait(30):
                        return
                    continue
                attempts += 1
                if ok:
                    self._qput("log", "自动登录: {}".format(msg))
                    self._qput("balloon", (APP_TITLE, "自动登录成功", "info"))
                    self._qput("dirty")
                    return
                if "未配置" in msg:
                    self._qput("log", "自动登录: 未配置账号密码, 已停止")
                    return
                self._qput("log", "自动登录失败({}/{}): {}".format(attempts, max_tries, msg))
            except Exception as e:
                attempts += 1
                self._qput("log", "自动登录异常: {}".format(e))
            if attempts < max_tries:
                if self.auto_login_stop.wait(10):
                    return
        self._qput("log", "自动登录: {} 次尝试均未成功, 请手动登录或检查配置".format(max_tries))
        self._qput("dirty")

    def _watch_toggle(self):
        if self.busy or self._quitting:
            return
        if self.watch_thread and self.watch_thread.is_alive():
            self.watch_stop.set()
            self.btn_watch.config(state="disabled", text="正在停止…")
        else:
            self.watch_stop.clear()
            self.watch_thread = threading.Thread(target=self._task_watch,
                                                 daemon=True)
            self.watch_thread.start()
            # Starting protection keeps the current page visible. Hiding is explicit.

    def _task_watch(self):
        self._qput("watch", True)
        was_offline = False
        interval = 30
        try:
            cfg = cl.load_config()
            interval = max(10, int(cfg.get("watch_interval", 30)))
            self._qput("log",
                       "[守护] 已启动: 每 {} 秒检测一次, 掉线自动重连".format(interval))
            while not self.watch_stop.is_set() and not self._quitting:
                try:
                    # 循环内重读配置: 托盘常驻期间改服务器/端口/间隔即时生效
                    cfg = cl.load_config()
                    new_interval = max(10, int(cfg.get("watch_interval", 30)))
                    if new_interval != interval:
                        interval = new_interval
                        self._qput("log",
                                   "[守护] 检测到间隔已改为 {} 秒".format(interval))
                    if not self._campus_ready(cfg):
                        was_offline = False
                        self.watch_stop.wait(interval)
                        continue
                    net = cl.check_internet(cfg)
                    if self.watch_stop.is_set() or self._quitting:
                        break
                    if net == "online":
                        was_offline = False
                    else:
                        self._qput("log",
                                   "[守护] 互联网不通({}), 尝试重新登录…".format(net))
                        ok, msg = cl.do_login(cfg, verbose=False, cancel=self.watch_stop)
                        if self.watch_stop.is_set() or self._quitting:
                            break
                        # 重新登录后补一次采样: 掉线期间重启过网卡的话, 这里让
                        # TrafficMeter 重新建立基线, 避免把旧计数当成新流量
                        self._sample_traffic(cl.inspect_network(cfg).interface_index)
                        if msg.startswith("等待校园网"):
                            self.watch_stop.wait(interval)
                            continue
                        if ok:
                            connected = cl.check_internet(cfg) == "online"
                            if self.watch_stop.is_set() or self._quitting:
                                break
                            self._qput("log", "[守护] " + (
                                "重连成功" if connected else "已认证，等待互联网恢复"))
                            if connected and was_offline:
                                self._qput("balloon", (APP_TITLE, "守护: 重连成功", "info"))
                            was_offline = not connected
                        else:
                            self._qput("log",
                                       "[守护] 重连失败: {}".format(msg))
                            # 每轮掉线只提醒一次, 避免气泡刷屏
                            if not was_offline:
                                self._qput("balloon",
                                           (APP_TITLE,
                                            "校园网掉线, 重连失败: " + msg,
                                            "warning"))
                            was_offline = True
                        self._qput("dirty")
                except Exception as e:
                    self._qput("log", "[守护] 异常: {}".format(e))
                self.watch_stop.wait(interval)
            self._qput("log", "[守护] 已停止")
        finally:
            self._qput("watch", False)

    # ---------- 设置保存
    def _load_settings_ui(self):
        cfg = cl.load_config()
        self.var_acc.set(cfg.get("account", ""))
        self.var_pwd.set(cfg.get("password", ""))
        isp = str(cfg.get("isp", "1"))
        for v in self.cmb_isp["values"]:
            if v.split(" · ")[0] == isp:
                self.cmb_isp.set(v)
                break
        else:
            self.cmb_isp.current(1)
        self.var_server.set(cfg.get("server", "192.168.0.203"))
        networks = cfg.get("campus_networks", ["WZXY-Student"])
        self.var_networks.set(", ".join(networks) if isinstance(networks, list) else "")
        self.var_p80.set(str(cfg.get("drcom_port", 80)))
        self.var_p803.set(str(cfg.get("eportal_port", 803)))
        self.var_to.set(str(cfg.get("timeout", 5)))
        self.var_interval.set(str(cfg.get("watch_interval", 30)))
        self.var_traffic_interval.set(str(cfg.get("traffic_interval", 5)))
        self.var_autologin.set(int(cfg.get("auto_login_on_start", 0)) == 1)
        self.var_autowatch.set(int(cfg.get("auto_watch", 0)) == 1)
        self.var_autostart.set(autostart_enabled())

    def _save_account(self):
        acc = self.var_acc.get().strip()
        pwd = self.var_pwd.get()
        if not acc:
            self._field_error("account", self.ent_account, "账号不能为空")
            return
        if not pwd:
            self._field_error("account", self.ent_password, "密码不能为空")
            return
        isp = self.var_isp.get().split(" · ")[0]
        cfg = cl.load_config()
        cfg["account"] = acc
        cfg["password"] = pwd
        cfg["isp"] = isp
        if not self._persist_settings("account", cfg):
            return
        self._qput("log",
                   "账号已保存: {} ({})".format(acc, cl.ISP_NAMES.get(isp, isp)))
        self._form_result("account", "账号设置已保存，下次连接时使用。", True)

    def _persist_settings(self, page, cfg):
        try:
            cl.save_config(cfg)
        except OSError as exc:
            self._form_result(page, "保存失败：请检查配置目录是否可写。", False)
            self._qput("log", "[错误] 保存设置失败: {}".format(exc))
            return False
        return True

    def _save_network(self):
        server = self.var_server.get().strip()
        try:
            ipaddress.IPv4Address(server)
        except ipaddress.AddressValueError:
            self._field_error("network", self.ent_server, "请输入有效的服务器 IPv4 地址，例如 192.168.0.203")
            return
        networks = [name.strip() for name in self.var_networks.get().replace("，", ",").split(",") if name.strip()]
        if not networks:
            self._field_error("network", self.ent_networks, "请填写至少一个校园网名称")
            return
        values = []
        for variable, label, low, high in (
                (self.var_p80, "认证端口", 1, 65535),
                (self.var_p803, "门户端口", 1, 65535),
                (self.var_to, "请求超时", 1, 60),
                (self.var_interval, "自动重连检查间隔", 10, 3600),
                (self.var_traffic_interval, "流量采样间隔", 5, 3600)):
            try:
                value = int(variable.get())
                if not low <= value <= high:
                    raise ValueError
            except ValueError:
                self._field_error("network", self.network_entries[str(variable)],
                                  "{}必须是 {}–{} 之间的整数".format(label, low, high))
                return
            values.append(value)
        p80, p803, to, interval, traffic_interval = values
        cfg = cl.load_config()
        cfg.update({"server": server, "drcom_port": p80, "eportal_port": p803,
                    "timeout": to, "watch_interval": interval,
                    "traffic_interval": traffic_interval, "campus_networks": networks})
        if not self._persist_settings("network", cfg):
            return
        self._campus_cache = None
        self._qput("log", "服务器设置已保存: {}:{} / eportal {}".format(
            server, p80, p803))
        self._form_result("network", "网络设置已保存，下次检测时生效。", True)

    def _save_auto(self):
        cfg = cl.load_config()
        cfg["auto_login_on_start"] = 1 if self.var_autologin.get() else 0
        cfg["auto_watch"] = 1 if self.var_autowatch.get() else 0
        if not self._persist_settings("automation", cfg):
            return
        want_auto = self.var_autostart.get()
        try:
            set_autostart(want_auto)
        except OSError as e:
            messagebox.showerror(APP_TITLE, "开机自启设置失败: {}".format(e))
            self._qput("log", "[错误] 开机自启设置失败: {}".format(e))
            self.var_autostart.set(autostart_enabled())
            self._form_result("automation", "启动偏好已保存，但 Windows 开机启动设置失败。", False)
            return
        state = "已开启" if (want_auto and autostart_enabled()) else "已关闭"
        self._qput("log",
                   "自动化已保存: 开机自启{} · 自动登录{} · 自动守护{}".format(
                       state,
                       "开" if cfg["auto_login_on_start"] else "关",
                       "开" if cfg["auto_watch"] else "关"))
        self._form_result("automation", "启动偏好已保存，下次启动时生效。", True)

    def _set_busy(self, busy, text=None):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_login, self.btn_logout, self.btn_test):
            try:
                b.config(state=state)
            except tk.TclError:
                pass
        if text:
            self.var_status.set(text)

    # ---------- 关闭
    def _on_close(self):
        # 守护运行中点关闭 = 最小化到托盘(守护继续); 真正退出走托盘菜单"退出程序"
        if self.watch_thread and self.watch_thread.is_alive():
            self._minimize_to_tray()
            # 托盘不可用时 _minimize_to_tray 会退化成就地最小化, 此时没有托盘菜单
            # 可供"退出程序", 点 X 将永远退不出 —— 这种情况改为直接询问退出。
            if not self.hidden_in_tray:
                if messagebox.askyesno(
                        APP_TITLE,
                        "系统托盘不可用。\n是否停止守护并退出程序?"):
                    self._request_quit()
            return
        self._request_quit()

    def _request_quit(self):
        if self._form_dirty and not messagebox.askyesno(APP_TITLE, "设置尚未保存，确定放弃更改并退出吗？"):
            return
        self._real_quit()

    def _real_quit(self):
        self._quitting = True
        self.watch_stop.set()
        self.auto_login_stop.set()
        self.login_stop.set()
        self.traffic_stop.set()
        self.traffic_scan_stop.set()
        # 把最后一次采样的增量落盘(进程即将退出, 不能等下一个 flush 周期)
        try:
            self.meter.flush(force=True)
        except OSError:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        cl.LOG_HOOK = None
        try:
            for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(callback)
            self.root.destroy()
        except tk.TclError:
            pass


# ------------------------------------------------------------ 入口

def main():
    if "--demo" in sys.argv:
        from campus_demo import run
        return run(sys.modules[__name__])
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        from campus_selftest import run
        return run(sys.modules[__name__], sys.argv[2])
    # 关键顺序: 必须在 tk.Tk() 之前设置 DPI 感知, 否则 Tk 会把屏幕 DPI
    # 按 96 缓存下来, 高缩放屏上字体只有应有的 2/3 大小。
    global DPI_FACTOR, UI_EXTRA, UI_SCALE
    DPI_FACTOR = _setup_dpi()
    UI_EXTRA = _ui_scale_override()
    UI_SCALE = DPI_FACTOR * UI_EXTRA

    root = tk.Tk()
    app = App(root)
    # 仅显式要求最小化启动时收起到托盘。
    if "--minimized" in sys.argv and not app.hidden_in_tray:
        app._minimize_to_tray()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
