"""Presentation components for the standard-library CampusNet desktop app.

This module contains no authentication, network, registry or persistence code.
All sizes are logical pixels; fonts remain in points for native DPI rendering.
"""

import tkinter as tk
from tkinter import font as tkfont, ttk

C = {
    "bg": "#f3f6f5", "card": "#ffffff", "line": "#dce5e1",
    "text": "#19352f", "muted": "#556b64", "entry_bg": "#f8faf9",
    "sbar": "#edf2ef", "side_bg": "#e8eeeb", "side_brand": "#19352f",
    "side_fg": "#405d53", "side_sub": "#556b64", "side_hover": "#dfe9e4",
    "side_active": "#d1e5dc", "side_accent": "#147d64", "side_line": "#d1dcd6",
    "primary": "#147d64", "primary_h": "#0b6550", "primary_soft": "#e8f3ee",
    "ghost": "#eaf0ed", "ghost_h": "#dce7e1", "ghost_fg": "#294b3f",
    "ok": "#147d64", "ok_soft": "#e0f1e8", "ok_fg": "#126047",
    "danger": "#b74343", "danger_h": "#993535", "danger_soft": "#fbeceb",
    "danger_fg": "#a13232", "warn": "#956024", "warn_h": "#754713",
    "amber": "#956024", "amber_h": "#754713", "teal": "#147d64", "teal_h": "#0b6550",
    "na_soft": "#edf2ef", "na_fg": "#556b64", "console_bg": "#f8faf9",
    "console_fg": "#314c41",
}
FS = {"brand": 16, "nav": 12, "body": 12, "sub": 11, "tiny": 10,
      "state": 28, "page": 23, "card": 14, "log": 11, "dot": 22, "metric": 25}


def choose_font(root):
    """Prefer a clear CJK sans serif, with native platform fallbacks."""
    available = set(tkfont.families(root))
    for name in ("Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei UI",
                 "Microsoft YaHei", "PingFang SC", "WenQuanYi Micro Hei", "Segoe UI"):
        if name in available:
            return name
    return tkfont.nametofont("TkDefaultFont", root=root).actual("family")


class ScrollPage(tk.Frame):
    """A page whose content is reachable even on a small/high-DPI display."""

    def __init__(self, parent, px):
        super().__init__(parent, bg=C["bg"])
        self.px = px
        self.canvas = tk.Canvas(self, bg=C["bg"], bd=0, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.body = tk.Frame(self.canvas, bg=C["bg"])
        self.item = self.canvas.create_window(px(28), 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._content_changed)
        self.canvas.bind("<Configure>", self._viewport_changed)

    def _content_changed(self, _event=None):
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), self.body.winfo_reqheight()))

    def _viewport_changed(self, event):
        self.canvas.itemconfigure(self.item, width=max(1, event.width - self.px(56)))
        self._content_changed()

    def reveal(self, widget):
        """Keep keyboard focus visible within a vertically scrolling page."""
        self.update_idletasks()
        y = widget.winfo_rooty() - self.body.winfo_rooty()
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        margin = self.px(16)
        if y < top + margin:
            self.canvas.yview_moveto(max(0, y - margin) / max(1, self.body.winfo_height()))
        elif y + widget.winfo_height() > bottom - margin:
            target = y + widget.winfo_height() + margin - self.canvas.winfo_height()
            self.canvas.yview_moveto(target / max(1, self.body.winfo_height()))

    def wheel(self, event):
        if self.body.winfo_height() <= self.canvas.winfo_height():
            return
        delta = (-1 if event.num == 4 else 1) if getattr(event, "num", None) in (4, 5) else -int(event.delta / 120)
        if delta:
            self.canvas.yview_scroll(delta * 3, "units")
            return "break"


class PagesMixin:
    """Builds views and reusable widgets. App supplies the command handlers."""

    def _style(self):
        self.px = lambda value: round(value * self.ui_scale)
        self.font_family = choose_font(self.root)
        self.fonts = {}
        for role, size in FS.items():
            self.fonts[role] = tkfont.Font(root=self.root, family=self.font_family, size=size,
                                          weight="bold" if role in ("brand", "page", "state", "metric") else "normal")
        # Named fonts also style Tk menus, popups and ttk widgets consistently.
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name, root=self.root).configure(family=self.font_family, size=FS["body"])
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.style.configure("TCombobox", font=self.fonts["body"], padding=self.px(12),
                             fieldbackground=C["entry_bg"], background=C["entry_bg"],
                             foreground=C["text"], bordercolor=C["line"], arrowcolor=C["muted"])
        self.style.map("TCombobox", fieldbackground=[("readonly", C["entry_bg"])],
                       foreground=[("readonly", C["text"])], bordercolor=[("focus", C["primary"])])
        self.root.option_add("*TCombobox*Listbox.font", self.fonts["body"])
        self.root.option_add("*TCombobox*Listbox.background", C["card"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["primary"])
        self.style.configure("Large.TCheckbutton", font=self.fonts["body"],
                             background=C["card"], foreground=C["text"],
                             indicatorsize=self.px(26), indicatormargin=(0, 0, self.px(12), 0),
                             padding=(self.px(4), self.px(16)))
        self.style.map("Large.TCheckbutton", background=[("active", C["card"])],
                       indicatorbackground=[("selected", C["primary"]), ("!selected", C["card"])],
                       indicatorforeground=[("selected", "white")])
        self.style.configure("Vertical.TScrollbar", background=C["line"], troughcolor=C["bg"],
                             borderwidth=0, arrowsize=self.px(10), lightcolor=C["line"],
                             darkcolor=C["line"], bordercolor=C["bg"])
        self.style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        self.style.configure("Traffic.Treeview", background=C["card"], fieldbackground=C["card"],
                             foreground=C["text"], font=self.fonts["sub"], rowheight=self.px(38), borderwidth=0)
        self.style.configure("Traffic.Treeview.Heading", font=self.fonts["sub"],
                             background=C["entry_bg"], foreground=C["muted"], padding=self.px(10),
                             relief="flat", borderwidth=0, bordercolor=C["entry_bg"],
                             lightcolor=C["entry_bg"], darkcolor=C["entry_bg"])
        self.style.map("Traffic.Treeview", background=[("selected", C["primary_soft"])],
                       foreground=[("selected", C["text"])])

    def _label(self, parent, text="", role="body", color=None, variable=None, wrap=True, **kw):
        label = tk.Label(parent, text=text, textvariable=variable, font=self.fonts[role],
                         bg=parent.cget("bg"), fg=color or C["text"], anchor="w", justify="left", bd=0, **kw)
        if wrap:
            label.bind("<Configure>", lambda e: label.configure(wraplength=max(1, e.width)))
        return label

    def _flat_btn(self, parent, text, cmd, bg=None, hover=None, fg=None, width=None):
        bg, hover, fg = bg or C["primary"], hover or C["primary_h"], fg or "#ffffff"
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=hover, activeforeground=fg, font=self.fonts["body"],
                      relief="flat", bd=0, highlightthickness=2, highlightbackground=bg,
                      highlightcolor=C["primary"], padx=self.px(20), pady=self.px(11),
                      cursor="hand2", disabledforeground="#7b8e85", takefocus=1)
        if width is not None:
            b.configure(width=width)
        b._bg, b._hv, b._fg = bg, hover, fg
        b.bind("<Enter>", lambda e: None if str(b["state"]) == "disabled" else b.config(bg=b._hv))
        b.bind("<Leave>", lambda e: b.config(bg=b._bg))
        b.bind("<Return>", lambda e: b.invoke())
        return b

    def _secondary_btn(self, parent, text, cmd):
        return self._flat_btn(parent, text, cmd, C["ghost"], C["ghost_h"], C["ghost_fg"])

    def _btn_colors(self, b, bg, hover, fg):
        b._bg, b._hv, b._fg = bg, hover, fg
        b.config(bg=bg, fg=fg, activebackground=hover, activeforeground=fg, highlightbackground=bg)

    def _card(self, parent, title=None, subtitle=None, bg=None):
        border = tk.Frame(parent, bg=bg or C["card"], highlightthickness=1, highlightbackground=C["line"])
        border.pack(fill="x", pady=(0, self.px(20)))
        body = tk.Frame(border, bg=bg or C["card"])
        body.pack(fill="both", expand=True, padx=self.px(24), pady=self.px(22))
        if title:
            self._label(body, title, "card").pack(fill="x", pady=(0, self.px(6)))
        if subtitle:
            self._label(body, subtitle, "sub", C["muted"]).pack(fill="x", pady=(0, self.px(18)))
        return body

    def _entry(self, parent, label, var, show=None, hint=None):
        group = tk.Frame(parent, bg=parent.cget("bg"))
        group.pack(fill="x", pady=(0, self.px(18)))
        self._label(group, label, "sub").pack(fill="x", pady=(0, self.px(7)))
        border = tk.Frame(group, bg=C["line"], height=self.px(48))
        border.pack(fill="x")
        border.pack_propagate(False)
        inner = tk.Frame(border, bg=C["entry_bg"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        e = tk.Entry(inner, textvariable=var, show=show or "", font=self.fonts["body"],
                     relief="flat", bd=0, bg=C["entry_bg"], fg=C["text"],
                     insertbackground=C["text"], highlightthickness=0)
        e.pack(fill="both", expand=True, padx=self.px(14))
        e.bind("<FocusIn>", lambda _: border.configure(bg=C["primary"]))
        e.bind("<FocusOut>", lambda _: border.configure(bg=C["line"]))
        if hint:
            self._label(group, hint, "tiny", C["muted"]).pack(fill="x", pady=(self.px(6), 0))
        return e

    def _check(self, parent, text, variable, desc=None, command=None):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=(0, self.px(12)))
        check = ttk.Checkbutton(row, text=text, variable=variable, style="Large.TCheckbutton", command=command)
        check.pack(anchor="w")
        if desc:
            label = self._label(row, desc, "sub", C["muted"])
            label.pack(fill="x", padx=(self.px(42), 0), pady=(0, self.px(8)))
            label.bind("<Button-1>", lambda _: check.invoke())
        return check

    def _page_header(self, page, title, sub, action=None):
        head = tk.Frame(page, bg=C["bg"])
        head.pack(fill="x", pady=(self.px(27), self.px(22)))
        if action:
            self._secondary_btn(head, action[0], action[1]).pack(side="right", padx=(self.px(16), 0))
        words = tk.Frame(head, bg=C["bg"])
        words.pack(side="left", fill="x", expand=True)
        self._label(words, title, "page").pack(fill="x")
        self._label(words, sub, "sub", C["muted"]).pack(fill="x", pady=(self.px(6), 0))

    def _note(self, parent, text):
        self._label(parent, text, "tiny", C["muted"]).pack(fill="x", pady=(0, self.px(20)))

    def _form_footer(self, parent, key, text, command):
        self._form_vars[key] = tk.StringVar(value="修改后请保存")
        notice = self._label(parent, variable=self._form_vars[key], role="sub", color=C["muted"])
        notice.pack(fill="x", pady=(self.px(4), self.px(12)))
        self._form_labels[key] = notice
        self._flat_btn(parent, text, command).pack(anchor="w")

    def _build(self):
        self._form_vars, self._form_labels, self._form_dirty = {}, {}, set()
        self._saved_forms = {}
        self.var_page = tk.StringVar(value="连接总览")
        self.var_status = tk.StringVar(value="准备就绪")
        self.var_watch = tk.StringVar(value="自动重连未开启")
        sbar = tk.Frame(self.root, bg=C["sbar"])
        sbar.pack(side="bottom", fill="x")
        self.lbl_watch = self._label(sbar, variable=self.var_watch, role="tiny", color=C["muted"], wrap=False)
        self.lbl_watch.pack(side="right", padx=self.px(22), pady=self.px(8))
        self._label(sbar, variable=self.var_status, role="tiny", color=C["muted"]).pack(
            side="left", fill="x", expand=True, padx=self.px(22), pady=self.px(8))
        shell = tk.Frame(self.root, bg=C["bg"])
        shell.pack(fill="both", expand=True)
        self._build_sidebar(shell)
        content = tk.Frame(shell, bg=C["bg"])
        content.pack(side="left", fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.pages = {}
        self.page_titles = dict(status="连接总览", traffic="流量统计", account="账号设置", network="网络设置", automation="自动化")
        # The same summary variables drive the overview and detailed usage page.
        self.var_traffic_total = {k: tk.StringVar(value="—") for k in ("today", "month", "total")}
        for key in self.page_titles:
            page = ScrollPage(content, self.px)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
            getattr(self, "_build_page_" + key)(page.body)
            page.grid_remove()
        self.show_page("status")
        self.root.bind_all("<MouseWheel>", self._page_wheel, add="+")
        self.root.bind_all("<Button-4>", self._page_wheel, add="+")
        self.root.bind_all("<Button-5>", self._page_wheel, add="+")
        for i, key in enumerate(self.page_titles, 1):
            self.root.bind(f"<Alt-Key-{i}>", lambda e, k=key: self.show_page(k))
        self.root.bind("<Control-s>", self._save_current_page)
        self.root.bind_all("<FocusIn>", self._reveal_focus, add="+")

    def _reveal_focus(self, event):
        widget = event.widget
        parent = widget
        while parent is not None:
            if isinstance(parent, ScrollPage):
                parent.reveal(widget)
                return
            parent = getattr(parent, "master", None)

    def _page_wheel(self, event):
        widget = event.widget
        # Let the table and log retain their own native scrolling.
        if widget.winfo_class() in ("Treeview", "Text", "TCombobox", "Scrollbar", "TScrollbar"):
            return
        while widget is not None:
            if isinstance(widget, ScrollPage):
                return widget.wheel(event)
            widget = getattr(widget, "master", None)

    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=C["side_bg"], width=self.px(218))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=C["side_bg"])
        brand.pack(fill="x", padx=self.px(22), pady=(self.px(34), self.px(32)))
        self._label(brand, "CAMPUS NET", "tiny", C["primary"], wrap=False).pack(anchor="w")
        self._label(brand, "校园网助手", "brand", wrap=False).pack(anchor="w", pady=(self.px(8), 0))
        self._label(brand, "让连接，简单一点", "tiny", C["muted"], wrap=False).pack(anchor="w", pady=(self.px(9), 0))
        self.nav_items = {}
        labels = (("status", "连接总览"), ("traffic", "流量统计"), ("account", "账号设置"),
                  ("network", "网络设置"), ("automation", "自动化"))
        for key, label in labels:
            button = self._flat_btn(side, label, lambda k=key: self.show_page(k),
                                    C["side_bg"], C["side_hover"], C["side_fg"])
            button.configure(anchor="w", font=self.fonts["nav"])
            button.pack(fill="x", padx=self.px(14), pady=self.px(4))
            self.nav_items[key] = button
        foot = tk.Frame(side, bg=C["side_bg"])
        foot.pack(side="bottom", fill="x", padx=self.px(22), pady=self.px(26))
        self._label(foot, "自动重连", "tiny", C["muted"]).pack(fill="x")
        self.lbl_side_watch = self._label(foot, "未开启", "sub")
        self.lbl_side_watch.pack(fill="x", pady=(self.px(4), self.px(16)))
        self._secondary_btn(foot, "收起到托盘", self._minimize_to_tray).pack(fill="x")

    def show_page(self, key):
        if key not in self.pages:
            return
        for other, page in self.pages.items():
            if other == key:
                page.grid()
            else:
                page.grid_remove()
        self._page = key
        self.var_page.set(self.page_titles[key])
        for other, button in self.nav_items.items():
            bg = C["side_active"] if other == key else C["side_bg"]
            self._btn_colors(button, bg, C["side_hover"], C["text"] if other == key else C["side_fg"])
        # A page may have been switched via mouse/shortcut while an old entry held focus.
        focus = self.root.focus_get()
        if focus and not focus.winfo_viewable():
            self.nav_items[key].focus_set()

    def _build_page_status(self, page):
        self._page_header(page, "连接总览", "网络状态、流量用量和常用操作，一目了然。")
        card = self._card(page, bg=C["primary_soft"])
        top = tk.Frame(card, bg=card.cget("bg"))
        top.pack(fill="x")
        self.lbl_net_pill = self._label(top, "互联网 · 检测中", "tiny", C["ok_fg"], wrap=False, padx=self.px(12), pady=self.px(7))
        self.lbl_net_pill.pack(side="right")
        self.lbl_state_dot = self._label(top, "●", "dot", C["muted"], wrap=False)
        self.lbl_state_dot.pack(side="left", padx=(0, self.px(10)))
        self.var_state = tk.StringVar(value="正在检查连接")
        self._label(top, variable=self.var_state, role="state").pack(side="left", fill="x", expand=True)
        self.var_connection_hint = tk.StringVar(value="正在确认校园网与互联网状态，请稍候。")
        self._label(card, variable=self.var_connection_hint, role="sub", color=C["muted"]).pack(fill="x", pady=(self.px(8), self.px(22)))
        actions = tk.Frame(card, bg=card.cget("bg"))
        actions.pack(fill="x")
        self.btn_login = self._flat_btn(actions, "连接校园网", self.login_click)
        self.btn_login.pack(side="left", padx=(0, self.px(10)))
        self.btn_watch = self._secondary_btn(actions, "开启自动重连", self._watch_toggle)
        self.btn_watch.pack(side="left", padx=(0, self.px(10)))
        self.btn_logout = self._flat_btn(actions, "断开连接", self.logout_click, C["primary_soft"], C["danger_soft"], C["danger_fg"])
        self.btn_logout.pack(side="left")
        detail = tk.Frame(card, bg=card.cget("bg"))
        detail.pack(fill="x", pady=(self.px(22), 0))
        self.var_account, self.var_ip, self.var_isp_now, self.var_mac = [tk.StringVar(value="—") for _ in range(4)]
        cells = []
        for i, (name, variable) in enumerate((("当前账号", self.var_account), ("本机 IP", self.var_ip),
                                              ("服务类型", self.var_isp_now), ("MAC 地址", self.var_mac))):
            cell = tk.Frame(detail, bg=card.cget("bg"))
            cell.grid(row=i//2, column=i%2, sticky="ew", padx=(0, self.px(20)), pady=self.px(6))
            self._label(cell, name, "tiny", C["muted"]).pack(fill="x")
            self._label(cell, variable=variable, role="sub").pack(fill="x", pady=(self.px(4), 0))
            cells.append(cell)
        def reflow(event):
            columns = 4 if event.width >= self.px(780) else 2
            for i in range(4):
                detail.columnconfigure(i, weight=1 if i < columns else 0, uniform="identity" if i < columns else "")
            for i, cell in enumerate(cells):
                cell.grid_configure(row=i//columns, column=i%columns)
        detail.bind("<Configure>", reflow)
        row = tk.Frame(page, bg=C["bg"])
        row.pack(fill="x", pady=(0, self.px(20)))
        for i, (name, key) in enumerate((("今日用量", "today"), ("本月用量", "month"))):
            tile = tk.Frame(row, bg=C["card"], highlightbackground=C["line"], highlightthickness=1)
            tile.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else self.px(10), self.px(10) if i == 0 else 0))
            self._label(tile, name, "sub", C["muted"]).pack(fill="x", padx=self.px(22), pady=(self.px(18), self.px(4)))
            self._label(tile, variable=self.var_traffic_total[key], role="metric").pack(fill="x", padx=self.px(22), pady=(0, self.px(18)))
            tile.bind("<Button-1>", lambda _: self.show_page("traffic"))
            row.columnconfigure(i, weight=1, uniform="overview")
        logcard = self._card(page)
        head = tk.Frame(logcard, bg=C["card"])
        head.pack(fill="x", pady=(0, self.px(12)))
        self._label(head, "运行记录", "card", wrap=False).pack(side="left")
        self.btn_refresh = self._secondary_btn(head, "刷新状态", self.refresh_status)
        self.btn_refresh.pack(side="right", padx=(self.px(8), 0))
        self.btn_test = self._secondary_btn(head, "连接诊断", self.test_click)
        self.btn_test.pack(side="right", padx=(self.px(8), 0))
        self._secondary_btn(head, "清空", self._clear_log).pack(side="right")
        logbody = tk.Frame(logcard, bg=C["console_bg"])
        logbody.pack(fill="both", expand=True)
        self.txt_log = tk.Text(logbody, height=6, font=self.fonts["log"], bg=C["console_bg"],
                               fg=C["console_fg"], state="disabled", relief="flat", bd=0,
                               padx=self.px(12), pady=self.px(12), wrap="word", spacing1=self.px(4),
                               selectbackground=C["side_active"])
        scroll = ttk.Scrollbar(logbody, command=self.txt_log.yview)
        scroll.pack(side="right", fill="y")
        self.txt_log.config(yscrollcommand=scroll.set)
        self.txt_log.pack(fill="both", expand=True)
        for tag, color in (("err", C["danger_fg"]), ("ok", C["ok_fg"]), ("info", C["text"]), ("muted", C["muted"])):
            self.txt_log.tag_configure(tag, foreground=color)

    def _build_page_traffic(self, page):
        self._page_header(page, "流量统计", "查看这台设备的校园网用量，掌握每天的变化。")
        row = tk.Frame(page, bg=C["bg"])
        row.pack(fill="x", pady=(0, self.px(20)))
        self.var_traffic = {}
        for i, (key, label) in enumerate((("today", "今日"), ("month", "本月"), ("total", "累计"))):
            tile = tk.Frame(row, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
            tile.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else self.px(7), 0 if i == 2 else self.px(7)))
            inner = tk.Frame(tile, bg=C["card"])
            inner.pack(fill="both", expand=True, padx=self.px(18), pady=self.px(20))
            self._label(inner, label, "sub", C["muted"]).pack(fill="x")
            self._label(inner, variable=self.var_traffic_total[key], role="metric").pack(fill="x", pady=self.px(10))
            down, up = tk.StringVar(value="下行 —"), tk.StringVar(value="上行 —")
            self.var_traffic[key] = (down, up)
            self._label(inner, variable=down, role="tiny", color=C["muted"]).pack(fill="x")
            self._label(inner, variable=up, role="tiny", color=C["muted"]).pack(fill="x", pady=(self.px(5), 0))
            row.columnconfigure(i, weight=1, uniform="traffic")
        self.var_traffic_net = tk.StringVar(value="正在确认采样状态…")
        self._label(page, variable=self.var_traffic_net, role="sub", color=C["muted"]).pack(fill="x", pady=(0, self.px(14)))
        card = self._card(page)
        bar = tk.Frame(card, bg=C["card"])
        bar.pack(fill="x", pady=(0, self.px(16)))
        self._label(bar, "每日明细 · 最近 14 天", "card", wrap=False).pack(side="left")
        self.btn_traffic = self._secondary_btn(bar, "刷新统计", self.traffic_click)
        self.btn_traffic.pack(side="right")
        table = tk.Frame(card, bg=C["card"])
        table.pack(fill="both", expand=True)
        self.tree_traffic = ttk.Treeview(table, columns=("date", "down", "up", "sum"), show="headings",
                                         style="Traffic.Treeview", height=7, selectmode="browse")
        for key, label, width in (("date", "日期", 150), ("down", "下行", 130), ("up", "上行", 130), ("sum", "合计", 140)):
            self.tree_traffic.heading(key, text=label, anchor="w" if key == "date" else "e")
            self.tree_traffic.column(key, width=self.px(width), minwidth=self.px(80), stretch=True,
                                       anchor="w" if key == "date" else "e")
        scroll = ttk.Scrollbar(table, command=self.tree_traffic.yview)
        scroll.pack(side="right", fill="y")
        self.tree_traffic.config(yscrollcommand=scroll.set)
        self.tree_traffic.pack(fill="both", expand=True)
        self._label(card, "根据两次网卡读数的差值估算，不等于学校账单；首次采样只建立基线。", "tiny", C["muted"]).pack(fill="x", pady=(self.px(14), 0))
        reference = self._card(page)
        self.btn_details = self._secondary_btn(reference, "展开统计说明与服务端参考", self._toggle_traffic_details)
        self.btn_details.pack(anchor="w")
        details = self.traffic_details = tk.Frame(reference, bg=C["card"])
        self.var_session = tk.StringVar(value="尚未查询服务端会话")
        self._label(details, variable=self.var_session, role="sub").pack(fill="x", pady=(0, self.px(12)))
        self._note(details, "服务端只提供当前会话，不代表累计用量；部分校园网不会上报上下行流量。")
        self.var_traffic_path = tk.StringVar(value="")
        self._label(details, variable=self.var_traffic_path, role="tiny", color=C["muted"]).pack(fill="x", pady=(0, self.px(16)))
        self.btn_traffic_reset = self._flat_btn(details, "重置历史累计…", self.traffic_reset_click,
                                                C["danger_soft"], "#f6dcdc", C["danger_fg"])
        self.btn_traffic_reset.pack(anchor="w")

    def _toggle_traffic_details(self):
        if self.traffic_details.winfo_manager():
            self.traffic_details.pack_forget()
            self.btn_details.configure(text="展开统计说明与服务端参考")
        else:
            self.traffic_details.pack(fill="x", pady=(self.px(18), 0))
            self.btn_details.configure(text="收起统计说明与服务端参考")

    def _build_page_account(self, page):
        self._page_header(page, "账号设置", "配置一次，连接时自动使用。")
        card = self._card(page, "校园网账号", "填写学校分配的学号或工号，并选择正确的服务类型。")
        self.var_acc, self.var_pwd, self.var_isp = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.var_show_pwd = tk.BooleanVar(value=False)
        self.ent_account = self._entry(card, "账号", self.var_acc, hint="本校账号通常不需要添加运营商后缀。")
        self.ent_password = self._entry(card, "密码", self.var_pwd, show="•")
        self._check(card, "显示密码", self.var_show_pwd,
                    command=lambda: self.ent_password.config(show="" if self.var_show_pwd.get() else "•"))
        self._label(card, "服务类型", "sub").pack(fill="x", pady=(0, self.px(8)))
        self.cmb_isp = ttk.Combobox(card, textvariable=self.var_isp, state="readonly", font=self.fonts["body"],
                                     values=["0 · 校园网", "1 · 中国移动", "2 · 中国电信", "3 · 中国联通", "4 · 中国广电"])
        self.cmb_isp.pack(fill="x", pady=(0, self.px(10)))
        self._note(card, "服务类型选错也可能提示密码错误，请与已开通的校园网服务保持一致。")
        self._form_footer(card, "account", "保存账号设置", self._save_account)
        self._note(page, "账号信息只保存在本机配置文件中。密码为明文存储，请勿分享该文件。")

    def _build_page_network(self, page):
        self._page_header(page, "网络设置", "通常保持默认即可，只有学校网络参数变化时才需要调整。")
        card = self._card(page, "连接范围", "只在指定校园网中自动认证。")
        self.var_server, self.var_networks = tk.StringVar(), tk.StringVar()
        self.ent_server = self._entry(card, "认证服务器 IPv4 地址", self.var_server, hint="例如 192.168.0.203")
        self.ent_networks = self._entry(card, "允许自动登录的网络名称", self.var_networks,
                    hint="与 Windows 中的网络名称一致；多个名称用英文逗号分隔。")
        card = self._card(page, "端口与检测间隔")
        self.var_p80, self.var_p803, self.var_to, self.var_interval, self.var_traffic_interval = [tk.StringVar() for _ in range(5)]
        self.network_entries = {}
        for label, var, hint in (("认证端口", self.var_p80, "默认 80；范围 1–65535"),
                                  ("门户端口", self.var_p803, "默认 803；范围 1–65535"),
                                  ("请求超时 / 秒", self.var_to, "范围 1–60"),
                                  ("自动重连检查间隔 / 秒", self.var_interval, "范围 10–3600；保存后下一轮生效"),
                                  ("流量采样间隔 / 秒", self.var_traffic_interval, "范围 5–3600；保存后下一轮生效")):
            self.network_entries[str(var)] = self._entry(card, label, var, hint=hint)
        self._form_footer(card, "network", "保存网络设置", self._save_network)

    def _build_page_automation(self, page):
        self._page_header(page, "自动化", "为下一次启动设定偏好，让校园网自动保持连接。")
        card = self._card(page, "启动偏好")
        self.var_autostart, self.var_autologin, self.var_autowatch = [tk.BooleanVar() for _ in range(3)]
        self._check(card, "登录 Windows 后启动", self.var_autostart,
                    "开机登录后在托盘中运行，无需手动打开。")
        self._check(card, "启动时自动登录校园网", self.var_autologin,
                    "等待指定校园网就绪后登录；其他网络会暂停认证。")
        self._check(card, "启动时开启自动重连", self.var_autowatch,
                    "掉线后自动尝试恢复，也会负责启动时的首次登录。")
        self._form_footer(card, "automation", "保存启动偏好", self._save_auto)
        self._note(page, "以上选项控制下次启动。要立刻开启或停止自动重连，请使用连接总览中的按钮。")
        card = self._card(page, "本机配置", "脚本版与独立程序分别使用各自目录中的配置文件。")
        self.var_config_path = tk.StringVar(value=self.config_path)
        self._label(card, variable=self.var_config_path, role="tiny", color=C["muted"]).pack(fill="x", pady=(0, self.px(14)))
        self._secondary_btn(card, "复制配置路径", lambda: self._copy_text(self.config_path)).pack(anchor="w")

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.var_status.set("已复制到剪贴板")

    def _save_current_page(self, _event=None):
        handler = {"account": self._save_account, "network": self._save_network, "automation": self._save_auto}.get(self._page)
        if handler:
            handler()
        return "break"

    def _observe_settings(self):
        self._form_fields = {
            "account": (self.var_acc, self.var_pwd, self.var_isp),
            "network": (self.var_server, self.var_networks, self.var_p80, self.var_p803, self.var_to, self.var_interval, self.var_traffic_interval),
            "automation": (self.var_autostart, self.var_autologin, self.var_autowatch),
        }
        for key, fields in self._form_fields.items():
            self._saved_forms[key] = tuple(v.get() for v in fields)
            for variable in fields:
                variable.trace_add("write", lambda *_, k=key: self._form_changed(k))

    def _form_changed(self, key):
        dirty = tuple(v.get() for v in self._form_fields[key]) != self._saved_forms[key]
        if dirty:
            self._form_dirty.add(key)
        else:
            self._form_dirty.discard(key)
        self._form_vars[key].set("有未保存的更改 · Ctrl+S 保存" if dirty else "设置与已保存内容一致")
        self._form_labels[key].configure(fg=C["warn"] if dirty else C["muted"])

    def _form_result(self, key, message, success):
        self._form_vars[key].set(message)
        self._form_labels[key].configure(fg=C["ok_fg"] if success else C["danger_fg"])
        self.var_status.set(message)
        if success:
            self._saved_forms[key] = tuple(v.get() for v in self._form_fields[key])
            self._form_dirty.discard(key)

    def _field_error(self, page, widget, message):
        self.show_page(page)
        self._form_result(page, message, False)
        widget.focus_set()
        widget.selection_range(0, "end")
        self.pages[page].reveal(widget)
