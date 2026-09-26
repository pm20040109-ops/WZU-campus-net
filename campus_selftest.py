"""Offline source/packaged smoke test; isolate data before creating App."""

import hashlib
import json
import sys
import traceback
from pathlib import Path

import campus_login as cl
import campus_traffic as ct
from campus_demo import demo_app
from campus_network import NetworkState


def run(gui, output):
    report = {"version": gui.APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
              "checks": [], "ok": False}
    icon = None
    try:
        payload = {"result": 1, "message": "ok (cached)", "data": {"x": 1}}
        assert cl.parse_jsonp(json.dumps(payload)) == payload
        report["checks"].append("json-top-level")
        password = "test" + chr(0x1F600)
        salted = cl.MD5_PID + password + cl.MD5_CALG
        expected = hashlib.md5(salted.encode("utf-16le")[::2]).hexdigest() + cl.MD5_CALG + cl.MD5_PID
        assert cl.md5_plus(password) == expected
        report["checks"].append("md5-utf16")
        assert ct.format_bytes(1024 ** 2) == "1.0 MB"
        assert ct.record_sample(ct._empty_stats(), 19, 5, 5, now=1.0) == (0, 0)
        report["checks"].append("traffic-accounting")
        with demo_app(gui) as (root, app):
            errors = []
            root.report_callback_exception = lambda *error: errors.append(str(error))
            root.withdraw()
            root.update()
            for key in app.pages:
                app.show_page(key)
                root.update_idletasks()
                assert app.var_page.get() == app.page_titles[key]
            report["checks"].append("tk-five-pages")
            assert len(app.tree_traffic.get_children()) == 14
            assert app.var_traffic_total["total"].get() == "90.00 GB"
            report["checks"].append("tk-traffic-page")
            app._on_network(NetworkState(False, "自检：非校园网暂停认证"))
            assert app.var_state.get() == "等待校园网"
            assert app.var_account.get() == "—"
            assert not app._refreshing
            report["checks"].append("tk-app-network-paused")
            app.var_acc.set("saved-demo")
            app._save_account()
            assert not app._form_dirty
            assert cl.load_config()["account"] == "saved-demo"
            report["checks"].append("isolated-settings-save")
            app.traffic_stop.set()
            app._task_traffic_loop()
            assert not errors, errors
            report["checks"].append("traffic-loop-idle")
            icon_path = app._find_icon()
        if sys.platform == "win32":
            assert gui.TrayIcon is not None, "Tray module failed to import"
            icon = gui.TrayIcon(icon_path, "CampusNet self-test", [], lambda _: None,
                                class_name="CampusNetSmokeTestTray")
            icon.start()
            assert icon.show(), "Shell tray registration failed"
            icon.stop()
            icon = None
            report["checks"].append("native-tray-add-remove")
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if icon is not None:
            icon.stop()
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
