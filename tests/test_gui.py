"""Real Tk integration tests. Enable with CAMPUS_GUI_TESTS=1 on a desktop."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campus_gui as gui
import campus_login as cl
from campus_demo import demo_app
from campus_network import NetworkState


@unittest.skipUnless(os.environ.get("CAMPUS_GUI_TESTS") == "1", "requires desktop; set CAMPUS_GUI_TESTS=1")
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.context = demo_app(gui, scale=1)
        self.root, self.app = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.root.update()

    def tearDown(self):
        self.assertFalse(self.errors, self.errors)

    def test_failure_log_uses_error_color(self):
        self.app._clear_log()
        self.app._on_log("登录未成功: 账号或密码错误")
        self.assertIn("err", self.app.txt_log.tag_names("1.0"))
        self.assertNotIn("ok", self.app.txt_log.tag_names("1.0"))

    def test_network_loss_clears_identity_and_session(self):
        self.app._session_loaded = True
        self.app._on_network(NetworkState(False, "校园网已断开"))
        for value in (self.app.var_account, self.app.var_ip, self.app.var_mac):
            self.assertEqual(value.get(), "—")
        self.assertFalse(self.app._session_loaded)
        self.assertIsNone(self.app._last_online)
        self.assertIn("失效", self.app.var_session.get())
        self.assertEqual(self.app._traffic_profile, "")

    def test_server_failure_clears_stale_session(self):
        self.app._session_loaded = True
        self.app._on_status_err("连接超时")
        self.assertEqual(self.app.var_ip.get(), "—")
        self.assertFalse(self.app._session_loaded)
        self.assertIsNone(self.app._last_online)
        self.assertEqual(self.app.btn_login.cget("text"), "连接校园网")
        self.assertIn("无法确认", self.app.var_session.get())

    def test_failed_save_retains_input_and_dirty_state(self):
        self.app.var_acc.set("changed-demo")
        with patch.object(cl, "save_config", side_effect=PermissionError("read-only")):
            self.app._save_account()
        self.assertEqual(self.app.var_acc.get(), "changed-demo")
        self.assertIn("account", self.app._form_dirty)
        self.assertIn("保存失败", self.app._form_vars["account"].get())

    def test_saved_account_clears_dirty_state(self):
        self.app.var_acc.set("changed-demo")
        self.app._save_account()
        self.assertEqual(cl.load_config()["account"], "changed-demo")
        self.assertNotIn("account", self.app._form_dirty)

    def test_invalid_network_field_is_revealed_at_minimum_size(self):
        self.root.geometry("900x620")
        self.root.focus_force()
        self.app.var_traffic_interval.set("bad")
        self.app._save_network()
        self.root.update()
        field = self.app.network_entries[str(self.app.var_traffic_interval)]
        canvas = self.app.pages["network"].canvas
        self.assertEqual(self.root.focus_get(), field)
        self.assertGreaterEqual(field.winfo_rooty(), canvas.winfo_rooty())
        self.assertLessEqual(field.winfo_rooty() + field.winfo_height(), canvas.winfo_rooty() + canvas.winfo_height())
        self.assertIn("5–3600", self.app._form_vars["network"].get())

    def test_refresh_preserves_selected_date(self):
        tree = self.app.tree_traffic
        selected = tree.get_children()[4]
        tree.selection_set(selected)
        data = self.app.meter.summary(days=14)
        data["days"] = [{"date": selected, "rx": 1024, "tx": 512, "sum": 1536}]
        self.app._on_traffic((data, "WZXY-Student"))
        self.assertEqual(tree.selection(), (selected,))

    def test_keyboard_navigation_and_hidden_focus(self):
        self.app.show_page("account")
        self.root.update()
        self.root.focus_force()
        self.app.ent_account.focus_set()
        self.root.update()
        self.root.event_generate("<Alt-Key-2>")
        self.root.update()
        self.assertEqual(self.app._page, "traffic")
        self.assertEqual(self.root.focus_get(), self.app.nav_items["traffic"])
        self.assertFalse(self.app.pages["account"].winfo_viewable())

    def test_refresh_error_restores_button(self):
        self.app._refreshing = True
        self.app.btn_refresh.config(state="disabled")
        with patch.object(cl, "check_status", side_effect=TimeoutError("offline")):
            self.app._task_refresh()
        self.app._poll()
        self.assertFalse(self.app._refreshing)
        self.assertEqual(str(self.app.btn_refresh.cget("state")), "normal")
        self.assertEqual(self.app.var_state.get(), "无法连接服务器")

    def test_unsaved_exit_can_be_cancelled(self):
        self.app.var_acc.set("unsaved-demo")
        with patch.object(gui.messagebox, "askyesno", return_value=False):
            self.app._request_quit()
        self.assertFalse(self.app._quitting)

    def test_controls_have_readable_dimensions(self):
        self.app.show_page("account")
        self.root.update()
        self.assertGreaterEqual(self.app.ent_account.master.master.winfo_height(), 44)
        self.assertGreaterEqual(self.app.ent_account.winfo_height(), self.app.fonts["body"].metrics("linespace"))
        self.assertGreaterEqual(self.app.fonts["body"].actual("size"), 12)
        self.assertGreaterEqual(self.app.btn_login.winfo_reqheight(), 44)
        self.assertGreaterEqual(int(self.app.style.lookup("Large.TCheckbutton", "indicatorsize")), 24)

    def test_demo_traffic_refresh_retains_sample_history(self):
        before = self.app.var_traffic_total["total"].get()
        self.app._task_traffic_scan()
        self.app._poll()
        self.assertEqual(self.app.var_traffic_total["total"].get(), before)
        self.assertEqual(len(self.app.tree_traffic.get_children()), 14)

    def test_demo_login_logout_updates_synthetic_state(self):
        cl.do_logout(cl.load_config())
        self.app._task_refresh()
        self.app._poll()
        self.assertEqual(self.app.var_state.get(), "离线")
        cl.do_login(cl.load_config())
        self.app._task_refresh()
        self.app._poll()
        self.assertEqual(self.app.var_state.get(), "在线")


if __name__ == "__main__":
    unittest.main()
