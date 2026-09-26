"""Isolated demo data for UI previews and integration tests.

Never reads real credentials, writes the Run registry key, authenticates or
samples real adapters. All patches remain active for the lifetime of the app.
"""

import datetime as dt
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from campus_network import NetworkState
import campus_login as cl
import campus_traffic as ct


@contextmanager
def demo_app(gui, scale=None):
    cfg = dict(cl.DEFAULT_CONFIG, account="demo-20260001", password="demo-only",
               auto_login_on_start=0, auto_watch=0)
    state = NetworkState(True, "已连接校园网", "192.0.2.88", 12, "WZXY-Student")
    status = dict(online=True, uid="demo-20260001", ip="192.0.2.88", mac="02:00:00:00:00:01")
    auto = [False]
    workers = []
    thread_type = gui.threading.Thread
    refresh_status = gui.App.refresh_status

    def login(*_args, **_kwargs):
        status.update(online=True, uid=cfg["account"])
        return True, "演示连接成功"

    def logout(*_args, **_kwargs):
        status.update(online=False, uid="")
        return True

    def tracked_thread(*args, **kwargs):
        worker = thread_type(*args, **kwargs)
        workers.append(worker)
        return worker

    with ExitStack() as stack:
        previous_hook = cl.LOG_HOOK
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="campusnet-demo-")))
        stack.enter_context(patch.object(gui.threading, "Thread", side_effect=tracked_thread))
        for owner, name, options in (
            (cl, "CONFIG_PATH", {"new": str(folder / "campus_login.json")}),
            (ct, "STATS_PATH", {"new": str(folder / "campus_traffic.json")}),
            (cl, "load_config", {"side_effect": lambda: dict(cfg)}),
            (cl, "save_config", {"side_effect": lambda updated: cfg.update(updated)}),
            (cl, "inspect_network", {"return_value": state}),
            (cl, "check_status", {"side_effect": lambda _: dict(status)}),
            (cl, "check_internet", {"return_value": "online"}),
            (cl, "do_login", {"side_effect": login}),
            (cl, "do_logout", {"side_effect": logout}),
            (cl, "load_portal_config", {"return_value": {"program_index":"demo", "enable_r3":"1", "en_md5":"0"}}),
            (cl, "http_get", {"side_effect": AssertionError("Demo must not issue HTTP requests")}),
            (cl, "server_session", {"return_value": {"login_time":"2026-09-23 08:00", "seconds":7260, "uplink":0, "downlink":0}}),
            (ct, "read_counters", {"return_value": (10*1024**3, 2*1024**3)}),
            (gui, "autostart_enabled", {"side_effect": lambda: auto[0]}),
            (gui, "set_autostart", {"side_effect": lambda enabled: auto.__setitem__(0, enabled)}),
            (gui, "TrayIcon", {"new": None}),
            (gui.App, "refresh_status", {}),
            (gui.App, "_start_traffic", {}),
            (gui.App, "_schedule_periodic", {}),
        ):
            stack.enter_context(patch.object(owner, name, **options))
        stack.enter_context(patch.object(gui, "DPI_FACTOR", gui._setup_dpi()))
        stack.enter_context(patch.object(gui, "UI_SCALE", scale or gui.DPI_FACTOR))
        root = gui.tk.Tk()
        app = gui.App(root)
        root.title("CampusNet · 离线界面演示")
        stats = ct._empty_stats()
        for i in range(14):
            date = (dt.date.today() - dt.timedelta(days=i)).isoformat()
            stats["days"][date] = {"rx": (i % 5 + 1) * 512*1024**2, "tx": 128*1024**2}
        stats["total"] = {"rx": 82*1024**3, "tx": 8*1024**3}
        # Keep preview refreshes consistent with the initially displayed data.
        ct.record_sample(stats, state.interface_index, 10*1024**3, 2*1024**3)
        app.meter.stats = stats
        gui.App.refresh_status.side_effect = lambda: refresh_status(app)
        data = ct.summary(stats)
        app._on_status((status, "online"))
        app._on_traffic((data, state.profile))
        app._on_traffic_session("本次会话已连接 2 小时。服务端未提供有效的流量读数。")
        app._on_log("[系统] 离线演示：所有账号、网络和用量均为虚构数据")
        app._on_log("连接成功，校园网与互联网均可用")
        app.var_status.set("离线演示 · 修改仅在本次预览中有效")
        try:
            yield root, app
        finally:
            if not app._quitting:
                app._real_quit()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(timeout=5)
            app.meter.flush()
            cl.LOG_HOOK = previous_hook
            if any(worker.is_alive() for worker in workers):
                raise RuntimeError("Demo worker did not stop before isolation ended")


def run(gui):
    with demo_app(gui) as (root, _app):
        root.mainloop()
    return 0
