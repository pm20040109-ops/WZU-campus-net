#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 系统托盘图标 —— 纯 ctypes 实现(Shell_NotifyIconW + 隐藏消息窗口)
=========================================================================
零第三方依赖, 仅使用标准库, 供 campus_gui.py 使用, 也可独立复用。

用法:
    tray = TrayIcon(icon_path="campus.ico", tooltip="我的应用",
                    menu=[("open", "打开主界面"), None, ("quit", "退出")],
                    on_command=lambda cmd: print(cmd))
    tray.start()            # 启动托盘消息线程(内部隐藏窗口 + GetMessage 循环)
    tray.show()             # 显示托盘图标
    tray.balloon("标题", "内容", level="info")   # 气泡通知 info/warning/error
    tray.set_tip("新提示")   # 更新悬浮提示(<=127 字符)
    tray.hide()             # 移除图标
    tray.stop()             # 结束消息线程

注意:
  - 双击左键固定回调 on_command("__dblclick__"); 右键菜单项回调对应 cmd 字符串。
  - 所有公开方法均可从任意线程调用(内部加锁, Shell_NotifyIconW 由 Shell 序列化)。
  - 菜单弹出用 TPM_RETURNCMD 同步取回选择, 配合 SetForegroundWindow + 事后
    PostMessage(WM_NULL), 这是 MSDN 规定的托盘菜单标准写法, 缺一不可。
"""

import ctypes
import threading
from ctypes import wintypes

# ---------------------------------------------------------------- 常量

WM_USER = 0x0400
WM_TRAYICON = WM_USER + 1            # 托盘回调消息(自定义)

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3

WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0203, 0x0205
WM_DESTROY, WM_QUIT = 0x0002, 0x0012

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RETURNCMD, TPM_NONOTIFY = 0x0100, 0x0080

IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
IDI_APPLICATION = 32512

HWND_MESSAGE = wintypes.HWND(-3)     # 仅接收消息的隐藏窗口父句柄

_LEVELS = {"info": NIIF_INFO, "warning": NIIF_WARNING, "error": NIIF_ERROR}

_u32 = ctypes.windll.user32
_s32 = ctypes.windll.shell32
_k32 = ctypes.windll.kernel32


# ---------------------------------------------------------------- 结构体

class NOTIFYICONDATAW(ctypes.Structure):
    # 与 Win32 NOTIFYICONDATAW 布局一致(x64), guidItem 以 16 字节占位
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


# LRESULT 在 64 位下为 64 位有符号整数, 用 c_ssize_t 兼容 32/64 位
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                             wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _setup_signatures():
    """显式声明 argtypes/restype —— 64 位下句柄是指针, 默认 c_int 会截断崩溃."""
    _u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _u32.CreateWindowExW.restype = wintypes.HWND
    _u32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _u32.DefWindowProcW.restype = ctypes.c_ssize_t
    _u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    _u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                 wintypes.UINT, wintypes.UINT]
    _u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
    _u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
    _u32.PostQuitMessage.argtypes = [ctypes.c_int]
    _u32.LoadImageW.restype = wintypes.HICON
    _u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
    _u32.LoadIconW.restype = wintypes.HICON
    _u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _u32.DestroyIcon.argtypes = [wintypes.HICON]
    _u32.CreatePopupMenu.restype = wintypes.HMENU
    _u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                 ctypes.c_size_t, wintypes.LPCWSTR]
    _u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.HWND, ctypes.c_void_p]
    _u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _u32.DestroyMenu.argtypes = [wintypes.HMENU]
    _u32.DestroyWindow.argtypes = [wintypes.HWND]
    _k32.GetModuleHandleW.restype = wintypes.HMODULE
    _k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _k32.GetCurrentThreadId.restype = wintypes.DWORD
    _s32.Shell_NotifyIconW.restype = wintypes.BOOL
    _s32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                       ctypes.POINTER(NOTIFYICONDATAW)]


_setup_signatures()


# ---------------------------------------------------------------- 托盘类

class TrayIcon(object):
    """一个托盘图标 + 右键菜单. 公开方法线程安全."""

    CMD_BASE = 0x1000

    def __init__(self, icon_path, tooltip, menu, on_command,
                 class_name="CampusNetTrayWnd"):
        """
        menu: [(cmd, "显示文字"), ...]; None 表示分隔线。
        on_command(cmd): 托盘事件回调; 左键双击固定为 "__dblclick__"。
        """
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.menu = menu
        self.on_command = on_command
        self.class_name = class_name
        self._hwnd = None
        self._hicon = None
        self._nid = None
        self._added = False
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread = None
        self._tid = None
        self._wndproc = WNDPROC(self._wnd_proc)   # 必须持有引用防止被 GC

    # ---------------- 公开 API(任意线程可调) ----------------

    def start(self, timeout=5):
        """启动托盘消息线程并等待隐藏窗口就绪."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("托盘窗口创建超时")
        if self._hwnd is None:
            raise RuntimeError("托盘窗口创建失败")

    def show(self):
        with self._lock:
            if self._nid is None:
                return False
            if self._added:
                return True
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            if _s32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
                self._added = True
            return self._added

    def hide(self):
        with self._lock:
            if self._nid is None or not self._added:
                return
            _s32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._added = False

    def set_tip(self, tip):
        with self._lock:
            if not self._added:
                return
            self._nid.szTip = tip[:127]
            self._nid.uFlags = NIF_TIP
            _s32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def balloon(self, title, text, level="info"):
        """托盘气泡通知. level: info / warning / error."""
        with self._lock:
            if not self._added:
                return
            self._nid.szInfo = text[:255]
            self._nid.szInfoTitle = title[:63]
            self._nid.dwInfoFlags = _LEVELS.get(level, NIIF_INFO)
            self._nid.uTimeout = 3000
            self._nid.uFlags = NIF_INFO
            _s32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def stop(self):
        """移除图标并结束消息线程."""
        try:
            self.hide()
        except Exception:
            pass
        if self._tid:
            _u32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def visible(self):
        return self._added

    # ---------------- 托盘消息线程内部 ----------------

    def _run(self):
        self._tid = _k32.GetCurrentThreadId()
        inst = _k32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = inst
        wc.lpszClassName = self.class_name
        # 类名已存在(重复注册)时返回 0, 属正常, 直接沿用已注册类
        _u32.RegisterClassW(ctypes.byref(wc))
        hwnd = _u32.CreateWindowExW(
            0, self.class_name, "CampusNetTray", 0,
            0, 0, 0, 0, HWND_MESSAGE, None, inst, None)
        if hwnd:
            self._hwnd = hwnd
            hicon = None
            if self.icon_path:
                hicon = _u32.LoadImageW(None, self.icon_path, IMAGE_ICON,
                                        16, 16, LR_LOADFROMFILE)
            if not hicon:
                # 图标文件缺失时退回系统默认应用图标, 保证托盘可用
                hicon = _u32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION))
            self._hicon = hicon
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = hicon
            nid.szTip = self.tooltip[:127]
            self._nid = nid
        self._ready.set()

        msg = wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))

        # 消息循环退出后的清理
        try:
            self.hide()
            if self._hicon:
                _u32.DestroyIcon(self._hicon)
            if self._hwnd:
                _u32.DestroyWindow(self._hwnd)
        except Exception:
            pass

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam == WM_LBUTTONDBLCLK:
                self._emit("__dblclick__")
            elif lparam == WM_RBUTTONUP:
                self._popup_menu(hwnd)
            return 0
        if msg == WM_DESTROY:
            _u32.PostQuitMessage(0)
            return 0
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self, hwnd):
        hmenu = _u32.CreatePopupMenu()
        if not hmenu:
            return
        id_map = {}
        for i, item in enumerate(self.menu):
            cid = self.CMD_BASE + i
            if item is None:
                _u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            else:
                cmd, label = item
                _u32.AppendMenuW(hmenu, MF_STRING, cid, label)
                id_map[cid] = cmd
        pt = wintypes.POINT()
        _u32.GetCursorPos(ctypes.byref(pt))
        # 关键两步(MSDN): 弹菜单前把宿主窗口置前台, 菜单后补一条空消息,
        # 否则菜单点击别处不会自动消失。
        _u32.SetForegroundWindow(hwnd)
        picked = _u32.TrackPopupMenu(
            hmenu, TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, hwnd, None)
        _u32.PostMessageW(hwnd, 0, 0, 0)
        _u32.DestroyMenu(hmenu)
        cmd = id_map.get(picked)
        if cmd:
            self._emit(cmd)

    def _emit(self, cmd):
        try:
            self.on_command(cmd)
        except Exception:
            pass
