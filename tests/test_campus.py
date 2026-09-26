import hashlib
import http.client
import http.server
import io
import json
import queue
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campus_login as cl
import campus_gui as gui
import campus_network as network
import campus_traffic as ct
import tray_icon as tray

READY = network.NetworkState(True, 'campus-ready', '192.0.2.10', 19, 'WZXY-Student')
WAIT = network.NetworkState(False, 'waiting-for-campus')


def config(**changes):
    return dict(cl.DEFAULT_CONFIG, **dict({'account': 'test-user', 'password': 'test-only'}, **changes))


def app_stub():
    app = gui.App.__new__(gui.App)
    app.q = queue.Queue()
    app.watch_stop = threading.Event()
    app.auto_login_stop = threading.Event()
    app.login_stop = threading.Event()
    app.watch_thread = None
    app.auto_login_thread = None
    app.busy = False
    app._quitting = False
    app._refreshing = False
    app.traffic_stop = threading.Event()
    app.traffic_thread = None
    app.traffic_scan_stop = threading.Event()
    app.traffic_scan_thread = None
    app._traffic_sampling = False
    app._traffic_profile = ''
    app._last_online = None
    # 默认视为"已查询过服务端会话": 否则任何一次采样都会拉起联网线程,
    # 让无关的用例意外打到真实校园网接口。需要验证该行为的用例自行置 False。
    app._session_loaded = True
    app._campus_cache = None
    app.meter = ct.TrafficMeter(
        path=str(Path(tempfile.mkdtemp()) / 'campus_traffic.json'))
    app._set_busy = Mock()
    return app


def response(status=200, body=b'Microsoft Connect Test'):
    res = Mock(status=status)
    res.read.return_value = body
    res.__enter__ = Mock(return_value=res)
    res.__exit__ = Mock(return_value=False)
    return res


class LocalNetworkTests(unittest.TestCase):
    def inspect(self, profiles, routes=None, **cfg):
        with patch.object(network, '_connection_profiles', return_value=profiles), \
             patch.object(network, '_best_route', side_effect=routes or [(19, '192.0.2.10')] * 2):
            return network.inspect_network(config(**cfg))

    def test_campus_local_only_connectivity_is_enough(self):
        self.assertTrue(self.inspect([{'Name': 'WZXY-Student', 'InterfaceIndex': 19,
                                       'IPv4Connectivity': 1}]).ready)

    def test_foreign_wifi_is_rejected(self):
        self.assertFalse(self.inspect([{'Name': 'Home', 'InterfaceIndex': 19,
                                        'IPv4Connectivity': 4}]).ready)

    def test_other_adapter_campus_profile_cannot_authorize_route(self):
        self.assertFalse(self.inspect([{'Name': 'WZXY-Student', 'InterfaceIndex': 8,
                                        'IPv4Connectivity': 4}]).ready)

    def test_disconnected_or_missing_profile(self):
        self.assertFalse(self.inspect([]).ready)
        self.assertFalse(self.inspect([{'Name': 'WZXY-Student', 'InterfaceIndex': 19,
                                        'IPv4Connectivity': 0}]).ready)

    def test_route_switch_during_detection(self):
        self.assertFalse(self.inspect([], routes=[(19, '192.0.2.10'), (8, '192.0.2.11')]).ready)

    def test_no_dhcp_address(self):
        self.assertFalse(self.inspect([{'Name': 'WZXY-Student', 'InterfaceIndex': 19,
                                        'IPv4Connectivity': 1}],
                                      routes=[(19, '169.254.1.2')] * 2).ready)

    def test_invalid_allowlist_fails_closed(self):
        for allowed in ([], '', None, [''], [1]):
            with self.subTest(allowed=allowed):
                self.assertFalse(self.inspect([], campus_networks=allowed).ready)

    def test_unavailable_network_service_fails_closed(self):
        with patch.object(network, '_best_route', side_effect=OSError('not ready')):
            self.assertFalse(network.inspect_network(config()).ready)


class ProtocolTests(unittest.TestCase):
    def test_source_bound_http_on_loopback(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'dr1001({"result":1});'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.handle_request, daemon=True)
        worker.start()
        try:
            data = cl.http_get('http://127.0.0.1:{}/'.format(server.server_port), 2,
                               source_ip='127.0.0.1')
            self.assertEqual(cl.parse_jsonp(data.decode()), {'result': 1})
        finally:
            server.server_close()
            worker.join(3)

    def test_network_switch_before_credential_submission_blocks_request(self):
        status = {'online': False, 'ip': '192.0.2.10', 'mac': '', 'uid': ''}
        with patch.object(cl, 'inspect_network', side_effect=[READY, WAIT]), \
             patch.object(cl, 'check_status', return_value=status), \
             patch.object(cl, 'load_portal_config', return_value=dict(cl.FALLBACK_PORTAL)), \
             patch.object(cl, 'http_get') as http:
            ok, msg = cl.do_login(config(), verbose=False)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith('等待校园网'))
        http.assert_not_called()

    def test_cancellation_during_portal_load_prevents_login(self):
        cancel = threading.Event()
        status = {'online': False, 'ip': '192.0.2.10', 'mac': '', 'uid': ''}
        def portal(*args, **kwargs):
            cancel.set()
            return dict(cl.FALLBACK_PORTAL)
        with patch.object(cl, 'inspect_network', return_value=READY), \
             patch.object(cl, 'check_status', return_value=status), \
             patch.object(cl, 'load_portal_config', side_effect=portal), patch.object(cl, 'jsonp') as request:
            self.assertFalse(cl.do_login(config(), verbose=False, cancel=cancel)[0])
            request.assert_not_called()

    def test_numeric_error_message_is_handled(self):
        status = {'online': False, 'ip': '192.0.2.10', 'mac': '', 'uid': ''}
        with patch.object(cl, 'inspect_network', return_value=READY), \
             patch.object(cl, 'check_status', return_value=status), \
             patch.object(cl, 'load_portal_config', return_value=dict(cl.FALLBACK_PORTAL)), \
             patch.object(cl, 'jsonp', return_value={'result': 0, 'msg': 9, 'msga': 123}), \
             patch.object(cl.time, 'sleep'):
            ok, msg = cl.do_login(config(), verbose=False)
        self.assertFalse(ok)
        self.assertIn('123', msg)

    def test_plain_json_parentheses_preserve_top_level(self):
        data = {'result': 1, 'message': 'ok (cached)', 'data': {'x': 1}}
        self.assertEqual(cl.parse_jsonp(json.dumps(data)), data)
        self.assertEqual(cl.parse_jsonp('{"result":0,"msga":"failed (temporary)"}')['result'], 0)

    def test_jsonp_wrapping_and_quoted_braces(self):
        data = {'result': 1, 'msg': 'a } ( \\" b'}
        self.assertEqual(cl.parse_jsonp('try{dr1001(' + json.dumps(data) + ');}catch(e){}'), data)

    def test_array_response_rejected(self):
        with self.assertRaises(ValueError):
            cl.parse_jsonp('[{"result":1}]')

    def test_md5_utf16_low_bytes(self):
        for password in ('ascii', '\u4e2d\u6587', 'test\U0001f600'):
            salted = cl.MD5_PID + password + cl.MD5_CALG
            expected = hashlib.md5(salted.encode('utf-16le')[::2]).hexdigest() + cl.MD5_CALG + cl.MD5_PID
            self.assertEqual(cl.md5_plus(password), expected)

    def test_parameter_encoding(self):
        self.assertEqual(cl._quote_param("a b!()"), 'a%20b!()')

    def test_foreign_network_never_calls_portal(self):
        with patch.object(cl, 'inspect_network', return_value=WAIT), \
             patch.object(cl, 'check_status') as status, patch.object(cl, 'jsonp') as request:
            self.assertFalse(cl.do_login(config())[0])
            status.assert_not_called()
            request.assert_not_called()

    def test_write_guard_checks_network_again(self):
        with patch.object(cl, 'inspect_network', return_value=WAIT), patch.object(cl, 'http_get') as http:
            with self.assertRaises(cl.NetworkNotReady):
                cl.jsonp(config(), 'http://192.0.2.1', '/drcom/login', {'user_password': 'test'})
            http.assert_not_called()

    def test_write_request_uses_selected_source_address(self):
        with patch.object(cl, 'inspect_network', return_value=READY), \
             patch.object(cl, 'http_get', return_value=b'dr1001({"result":1});') as http:
            cl.jsonp(config(), 'http://192.0.2.1', '/drcom/login', {})
            self.assertEqual(http.call_args.kwargs['source_ip'], READY.source_ip)

    def test_unrecognized_portal_response_rejected(self):
        with patch.object(cl, 'jsonp', return_value={'result': 1}):
            with self.assertRaises(ValueError):
                cl.check_status(config())

    def test_cancelled_login_skips_network_and_authentication(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(cl, 'inspect_network') as inspect:
            self.assertFalse(cl.do_login(config(), cancel=cancel)[0])
            inspect.assert_not_called()

    def test_logout_waits_for_active_login(self):
        entered, release, logout_started = threading.Event(), threading.Event(), threading.Event()
        order = []
        def login(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test synchronization timeout')
            order.append('login-complete')
            return True, 'ok'
        def logout(*args):
            order.append('logout-complete')
            return True
        def call_logout():
            logout_started.set()
            cl.do_logout(config())
        with patch.object(cl, '_do_login', side_effect=login), \
             patch.object(cl, '_do_logout', side_effect=logout), \
             patch.object(cl, 'inspect_network', return_value=READY):
            t1 = threading.Thread(target=lambda: cl.do_login(config()), daemon=True)
            t2 = threading.Thread(target=call_logout, daemon=True)
            t1.start()
            self.assertTrue(entered.wait(3))
            t2.start()
            self.assertTrue(logout_started.wait(3))
            self.assertEqual(order, [])
            release.set()
            t1.join(3)
            t2.join(3)
            self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(order, ['login-complete', 'logout-complete'])

    def test_normalization_and_portal_defaults(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cfg = cl.normalize_config(config(timeout='bad', auto_watch=None))
            self.assertEqual((cfg['timeout'], cfg['auto_watch']), (5, 0))
            with patch.object(cl, 'jsonp', return_value={'data': {}}):
                self.assertEqual(cl.load_portal_config(cfg, status={'ip': '192.0.2.10'})['enable_r3'], 1)


class ConnectivityTests(unittest.TestCase):
    def probe(self, side_effect):
        with patch.object(cl._DIRECT_OPENER, 'open', side_effect=side_effect) as opened:
            return cl.check_internet(config()), opened.call_count

    def test_foreign_redirect_not_online(self):
        error = urllib.error.HTTPError('http://probe/', 302, 'redirect', {'Location': 'http://other/login'}, None)
        self.assertEqual(self.probe([error, OSError('unavailable')]), ('unknown', 2))

    def test_first_service_failure_uses_healthy_backup(self):
        error = urllib.error.HTTPError('http://probe/', 503, 'unavailable', {}, None)
        self.assertEqual(self.probe([error, response()]), ('online', 2))

    def test_portal_redirect_matches_hostname_not_substring(self):
        for host, expected in [('192.168.0.203', 'offline'), ('192.168.0.203.example.invalid', 'unknown')]:
            error = urllib.error.HTTPError('http://probe/', 308, 'redirect', {'Location': 'http://' + host + '/login'}, None)
            self.assertEqual(self.probe([error, OSError()])[0], expected)

    def test_html_not_online_and_malformed_http_falls_back(self):
        self.assertEqual(self.probe([response(200, b'<html>login</html>'), OSError()]), ('offline', 2))
        self.assertEqual(self.probe([http.client.BadStatusLine('bad'), response()]), ('online', 2))


class GuiTests(unittest.TestCase):
    def test_login_exception_always_releases_busy(self):
        app = app_stub()
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'do_login', side_effect=RuntimeError('test failure')):
            app._task_login()
        self.assertEqual([k for k, _ in app.q.queue].count('done'), 1)

    def test_config_failure_also_releases_busy(self):
        app = app_stub()
        with patch.object(cl, 'load_config', side_effect=OSError('test failure')):
            app._task_login()
        self.assertIn(('done', None), list(app.q.queue))

    def test_refresh_skips_all_http_on_other_network(self):
        app = app_stub()
        app._refreshing = True
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', return_value=WAIT), patch.object(cl, 'check_status') as st:
            app._task_refresh()
            st.assert_not_called()
        self.assertFalse(app._refreshing)

    def test_auto_login_wait_does_not_exhaust_attempts(self):
        app = app_stub()
        app.auto_login_stop = Mock()
        app.auto_login_stop.is_set.return_value = False
        app.auto_login_stop.wait.return_value = False
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', side_effect=[WAIT] * 9 + [READY]), \
             patch.object(cl, 'do_login', return_value=(True, 'login-ok')) as login:
            app._task_auto_login()
        self.assertEqual(app.auto_login_stop.wait.call_count, 9)
        login.assert_called_once()

    def test_watch_never_probes_or_authenticates_on_foreign_network(self):
        app = app_stub()
        def inspect(_):
            app.watch_stop.set()
            return WAIT
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', side_effect=inspect), \
             patch.object(cl, 'do_login') as login, patch.object(cl, 'check_internet') as internet:
            app._task_watch()
            login.assert_not_called()
            internet.assert_not_called()

    def test_stop_during_probe_prevents_login(self):
        app = app_stub()
        def probe(_):
            app.watch_stop.set()
            return 'offline'
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', return_value=READY), \
             patch.object(cl, 'check_internet', side_effect=probe), patch.object(cl, 'do_login') as login:
            app._task_watch()
            login.assert_not_called()

    def test_logout_cancels_all_login_sources(self):
        app = app_stub()
        with patch.object(gui.messagebox, 'askyesno', return_value=True), patch.object(gui.threading, 'Thread'):
            app.logout_click()
        self.assertTrue(app.watch_stop.is_set() and app.auto_login_stop.is_set() and app.login_stop.is_set())

    def test_tray_failure_preserves_window_restore_path(self):
        app = app_stub()
        app.hidden_in_tray = False
        app._ensure_tray = Mock(return_value=True)
        app.root = Mock()
        app.tray = Mock()
        app.tray.show.return_value = False
        app._on_log = Mock()
        app._minimize_to_tray()
        app.root.withdraw.assert_not_called()
        app.root.iconify.assert_called_once()
        self.assertFalse(app.hidden_in_tray)

    def test_actual_tray_add_reports_failure(self):
        icon = tray.TrayIcon(None, 'test', [], lambda _: None)
        icon._nid = tray.NOTIFYICONDATAW()
        with patch.object(tray._s32, 'Shell_NotifyIconW', return_value=0):
            self.assertFalse(icon.show())
            self.assertFalse(icon.visible)


class TrafficMeterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / 'campus_traffic.json')

    def tearDown(self):
        self.dir.cleanup()

    def test_first_sample_only_sets_baseline(self):
        stats = ct._empty_stats()
        self.assertEqual(ct.record_sample(stats, 19, 1000, 500, now=100.0), (0, 0))
        self.assertEqual(stats['total'], {'rx': 0, 'tx': 0})

    def test_deltas_accumulate_into_day_and_total(self):
        meter = ct.TrafficMeter(path=self.path, flush_interval=0)
        meter.note(19, 1000, 500, now=100.0)
        meter.note(19, 3000, 900, now=101.0)
        data = meter.summary(now=101.0)
        self.assertEqual(data['today']['rx'], 2000)
        self.assertEqual(data['today']['tx'], 400)
        self.assertEqual(data['total']['sum'], 2400)

    def test_counter_reset_is_not_counted_as_traffic(self):
        meter = ct.TrafficMeter(path=self.path, flush_interval=0)
        meter.note(19, 5000, 5000, now=100.0)
        self.assertEqual(meter.note(19, 10, 10, now=101.0), (0, 0))
        self.assertEqual(meter.summary(now=101.0)['total']['sum'], 0)

    def test_switching_adapter_rebuilds_baseline(self):
        meter = ct.TrafficMeter(path=self.path, flush_interval=0)
        meter.note(19, 1000, 1000, now=100.0)
        meter.note(19, 2000, 2000, now=101.0)
        # 换到另一块网卡: 它的累计值与本机校园网用量无关, 不能计入
        self.assertEqual(meter.note(7, 9_000_000, 9_000_000, now=102.0), (0, 0))
        self.assertEqual(meter.summary(now=102.0)['total']['sum'], 2000)

    def test_baseline_persists_so_separate_runs_keep_counting(self):
        # 一次性运行的 traffic 子命令每次都只采样一次: 基线必须落盘,
        # 否则第二次运行又只建立基线, 永远统计不到增量。
        first = ct.TrafficMeter(path=self.path)
        first.note(19, 1000, 1000, now=100.0)
        self.assertTrue(first.flush(force=True))
        self.assertTrue(Path(self.path).exists())
        second = ct.TrafficMeter(path=self.path)
        self.assertEqual(second.summary(now=101.0)['total']['sum'], 0)
        self.assertEqual(second.note(19, 4000, 1000, now=101.0), (3000, 0))
        self.assertEqual(second.summary(now=101.0)['today']['rx'], 3000)

    def test_sample_writes_baseline_immediately(self):
        # sample() 建立基线后必须立刻落盘(而不是等 flush 周期),
        # 这是"一次性运行也能累计"的关键。
        with patch.object(ct, 'read_counters', return_value=(5000, 7000)):
            first = ct.TrafficMeter(path=self.path)
            self.assertEqual(first.sample(19), (0, 0))
        self.assertTrue(Path(self.path).exists())
        with patch.object(ct, 'read_counters', return_value=(9000, 7000)):
            second = ct.TrafficMeter(path=self.path)
            self.assertEqual(second.sample(19), (4000, 0))
            self.assertEqual(second.summary()['today']['rx'], 4000)

    def test_summary_splits_month_and_orders_history(self):
        import datetime
        meter = ct.TrafficMeter(path=self.path, flush_interval=0)
        # 固定用 1 月 1/2/3 日: 跨越月份与年份, 便于校验"本月"只统计当月
        day1 = datetime.datetime(2026, 1, 1, 12, 0, 0).timestamp()
        meter.note(19, 0, 0, now=day1)
        meter.note(19, 100, 0, now=day1 + 86400)          # 1-02
        meter.note(19, 300, 0, now=day1 + 2 * 86400)      # 1-03
        data = meter.summary(now=day1 + 2 * 86400, days=2)
        self.assertEqual(data['month']['rx'], 300)
        self.assertEqual(data['month']['month'], '2026-01')
        self.assertEqual([r['date'] for r in data['days']], ['2026-01-02', '2026-01-03'])
        days = meter.summary(now=day1 + 2 * 86400, days=14)['days']
        self.assertEqual([r['date'] for r in days],
                         sorted(r['date'] for r in days))

    def test_previous_month_not_counted_in_this_month(self):
        import datetime
        meter = ct.TrafficMeter(path=self.path, flush_interval=0)
        jan = datetime.datetime(2026, 1, 31, 12, 0, 0).timestamp()
        meter.note(19, 0, 0, now=jan)
        meter.note(19, 500, 0, now=jan + 600)              # 仍是 1 月
        meter.note(19, 900, 0, now=jan + 86400)            # 已到 2 月
        data = meter.summary(now=jan + 86400)
        self.assertEqual(data['month']['month'], '2026-02')
        self.assertEqual(data['month']['rx'], 400)
        self.assertEqual(data['total']['rx'], 900)

    def test_hourly_bucket_rolls_over_to_next_day(self):
        meter = ct.TrafficMeter(path=self.path, flush_interval=0)
        import datetime
        day1 = datetime.datetime(2026, 9, 21, 23, 59, 50).timestamp()
        day2 = day1 + 20
        meter.note(19, 0, 0, now=day1)
        meter.note(19, 500, 0, now=day2)
        data = meter.summary(now=day2)
        self.assertEqual(data['days'][-1]['date'], '2026-09-22')
        self.assertEqual(data['days'][-1]['rx'], 500)

    def test_corrupt_file_starts_from_zero_without_raising(self):
        Path(self.path).write_text('{not json', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as noise:
            stats = ct.load_stats(self.path)
        self.assertEqual(stats['total'], {'rx': 0, 'tx': 0})
        self.assertIn('读取失败', noise.getvalue())

    def test_sanitize_drops_hostile_values(self):
        clean = ct.sanitize({'days': {'2026-09-21': {'rx': 'x', 'tx': 1},
                                      '2026-09-20': {'rx': 5, 'tx': 6},
                                      'bad': 7},
                             'total': {'rx': -3, 'tx': 'oops'},
                             'last': {'index': 'nope', 'rx': 1, 'tx': 1}})
        self.assertEqual(sorted(clean['days']), ['2026-09-20'])
        self.assertEqual(clean['total'], {'rx': 0, 'tx': 0})
        self.assertIsNone(clean['last'])

    def test_formatting_helpers(self):
        self.assertEqual(ct.format_bytes(0), '0 B')
        self.assertEqual(ct.format_bytes(1024), '1.0 KB')
        self.assertEqual(ct.format_bytes(1024 ** 2), '1.0 MB')
        self.assertEqual(ct.format_bytes(1024 ** 3 * 1.5), '1.50 GB')
        self.assertEqual(ct.format_bytes('nonsense'), '-')
        self.assertEqual(ct.format_duration(3672), '1 小时 1 分')
        self.assertEqual(ct.format_duration(0), '0 秒')

    def test_sample_without_counters_returns_none(self):
        meter = ct.TrafficMeter(path=self.path)
        with patch.object(ct, 'read_counters', return_value=None):
            self.assertIsNone(meter.sample(19))


class TrafficCliTests(unittest.TestCase):
    def report(self, **cfg):
        meter = ct.TrafficMeter(path=str(Path(tempfile.mkdtemp()) / 'stats.json'))
        with patch.object(cl, 'inspect_network', return_value=READY), \
             patch.object(ct, 'read_counters', return_value=(1024, 512)), \
             patch.object(cl, 'jsonp', return_value={'result': 1, 'list': []}):
            return cl.traffic_report(config(**cfg), meter)

    def test_report_prints_all_buckets(self):
        with redirect_stdout(io.StringIO()) as out:
            ok, data = self.report()
        text = out.getvalue()
        self.assertTrue(ok)
        self.assertIn('今日', text)
        self.assertIn('本月', text)
        self.assertIn('累计', text)
        self.assertEqual(data['total']['sum'], 0)   # 第一次采样只建立基线

    def test_foreign_network_never_reads_counters(self):
        meter = ct.TrafficMeter(path=str(Path(tempfile.mkdtemp()) / 'stats.json'))
        with patch.object(cl, 'inspect_network', return_value=WAIT), \
             patch.object(ct, 'read_counters') as counters, \
             redirect_stdout(io.StringIO()) as out:
            ok, _ = cl.traffic_report(config(), meter)
        self.assertTrue(ok)
        counters.assert_not_called()
        self.assertIn('非校园网期间不计流量', out.getvalue())

    def test_reset_requires_confirmation(self):
        args = type('Args', (), {'reset': True, 'yes': False})()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cl.cmd_traffic(config(), args), 2)

    def test_reset_clears_and_keeps_baseline(self):
        path = str(Path(tempfile.mkdtemp()) / 'stats.json')
        meter = ct.TrafficMeter(path=path, flush_interval=0)
        meter.note(19, 1000, 1000, now=100.0)
        meter.note(19, 2000, 2000, now=101.0)
        self.assertEqual(meter.summary(now=101.0)['total']['sum'], 2000)
        args = type('Args', (), {'reset': True, 'yes': True})()
        with patch.object(cl.ct, 'TrafficMeter', return_value=meter), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cl.cmd_traffic(config(), args), 0)
        self.assertEqual(ct.load_stats(path)['total'], {'rx': 0, 'tx': 0})

    def test_session_line_handles_missing_and_present(self):
        self.assertIn('暂无', cl.session_line(None))
        text = cl.session_line({'login_time': '2026-09-21 17:09:13', 'seconds': 3661,
                                'uplink': 0, 'downlink': 2048, 'devices': 2})
        self.assertIn('2026-09-21 17:09:13', text)
        self.assertIn('1 小时 1 分', text)
        self.assertIn('2.0 KB', text)
        self.assertIn('在线终端 2 台', text)

    def test_server_session_parses_online_list(self):
        payload = {'result': 1, 'list': [
            {'online_ip': '10.99.1.1', 'user_account': 'x', 'online_time': 't',
             'time_long': '90', 'uplink_bytes': '1', 'downlink_bytes': '2'},
            {'online_ip': '10.99.240.37', 'user_account': 'me', 'online_time': 'T',
             'time_long': '17256', 'uplink_bytes': '10', 'downlink_bytes': '20'}]}
        with patch.object(cl, 'jsonp', return_value=payload):
            session = cl.server_session(config(), status={'ip': '10.99.240.37',
                                                          'mac': 'aabbccddeeff'})
        self.assertEqual(session['account'], 'me')          # 取本机 IP 那条
        self.assertEqual(session['seconds'], 17256)
        self.assertEqual(session['devices'], 2)

    def test_server_session_missing_list_is_none(self):
        with patch.object(cl, 'jsonp', return_value={'result': 1, 'list': []}), \
             redirect_stdout(io.StringIO()):
            self.assertIsNone(cl.server_session(config(), status={'ip': '10.0.0.1'}))


class TrafficGuiTests(unittest.TestCase):
    def test_sampling_skipped_when_campus_not_ready(self):
        app = app_stub()
        def inspect(_):
            app.traffic_stop.set()
            return WAIT
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', side_effect=inspect), \
             patch.object(ct, 'read_counters') as counters:
            app._task_traffic_loop()
        counters.assert_not_called()

    def test_loop_samples_and_persists_on_campus(self):
        app = app_stub()
        def sample(index):
            app.traffic_stop.set()
            return (4096, 2048)
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', return_value=READY), \
             patch.object(ct, 'read_counters', side_effect=sample):
            app._task_traffic_loop()
        self.assertTrue(Path(app.meter.path).exists())
        self.assertIn('traffic', [k for k, _ in app.q.queue])

    def test_campus_state_is_cached_between_samples(self):
        # inspect_network 会拉起 PowerShell(约 370ms), 采样周期只有 5 秒:
        # 同一 TTL 内的多次采样必须复用结果, 不能每轮重查。
        app = app_stub()
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', return_value=READY) as inspect:
            app._campus_state()
            app._campus_state()
            app._campus_state()
        self.assertEqual(inspect.call_count, 1)

    def test_campus_state_cache_expires(self):
        app = app_stub()
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', return_value=READY) as inspect:
            app._campus_state()
            app._campus_cache = (app._campus_cache[0] - gui.TRAFFIC_NET_TTL - 1, READY)
            app._campus_state()
        self.assertEqual(inspect.call_count, 2)

    def test_loop_reuses_cache_across_rounds(self):
        # 采样间隔(默认 5s)小于 TTL(30s), 因此连续几轮采样只应查一次网络;
        # 若 TTL 被写成等于采样间隔, 这里会退化成每轮一次。
        # 把间隔下限压到 0.05s, 否则这个用例要真等两个 5 秒周期。
        app = app_stub()
        rounds = []
        def sample(index):
            rounds.append(index)
            if len(rounds) >= 3:
                app.traffic_stop.set()
            return (10, 5)
        with patch.object(gui, 'TRAFFIC_MIN_INTERVAL', 0.05), \
             patch.object(cl, 'load_config', return_value=config(traffic_interval=1)), \
             patch.object(cl, 'inspect_network', return_value=READY) as inspect, \
             patch.object(ct, 'read_counters', side_effect=sample):
            app._task_traffic_loop()
        self.assertEqual(len(rounds), 3)
        self.assertEqual(inspect.call_count, 1)
        self.assertGreaterEqual(gui.TRAFFIC_NET_TTL, gui.TRAFFIC_INTERVAL)

    def test_loop_interval_is_clamped_to_config_range(self):
        self.assertEqual(gui.traffic_interval_of({}), gui.TRAFFIC_INTERVAL)
        self.assertEqual(gui.traffic_interval_of({'traffic_interval': 'nonsense'}),
                         gui.TRAFFIC_INTERVAL)
        self.assertEqual(gui.traffic_interval_of({'traffic_interval': -99}),
                         gui.TRAFFIC_MIN_INTERVAL)
        self.assertEqual(gui.traffic_interval_of({'traffic_interval': 99999}),
                         gui.TRAFFIC_MAX_INTERVAL)
        self.assertEqual(gui.traffic_interval_of({'traffic_interval': 60}), 60)
        # 与配置校验表保持一致, 免得 GUI 允许的值被 CLI 判为非法
        lo, hi, _ = cl._INT_FIELDS['traffic_interval']
        self.assertEqual((lo, hi), (gui.TRAFFIC_MIN_INTERVAL, gui.TRAFFIC_MAX_INTERVAL))

    def test_concurrent_sampling_is_serialized(self):
        app = app_stub()
        app._traffic_sampling = True
        with patch.object(ct, 'read_counters') as counters:
            app._sample_traffic(19)
        counters.assert_not_called()

    def test_status_handler_updates_traffic_on_reconnect(self):
        app = app_stub()
        app._last_online = False
        app.txt_log = None
        app._on_status = gui.App._on_status.__get__(app)
        app.var_state, app.var_account, app.var_ip = Mock(), Mock(), Mock()
        app.var_mac, app.var_isp_now, app.var_status = Mock(), Mock(), Mock()
        app.lbl_state_dot, app.lbl_net_pill, app.tray = Mock(), Mock(), None
        app.meter = Mock()
        app.meter.summary.return_value = {'today': {'rx': 0, 'tx': 0, 'sum': 0},
                                          'month': {'rx': 0, 'tx': 0, 'sum': 0},
                                          'total': {'rx': 0, 'tx': 0, 'sum': 0},
                                          'days': [], 'path': 'x'}
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', return_value=READY), \
             patch.object(ct, 'read_counters', return_value=(10, 10)), patch.object(gui.threading, 'Thread') as worker:
            app._on_status(({'online': True, 'uid': 'u', 'ip': '10.0.0.1',
                             'mac': 'm'}, 'online'))
            worker.return_value.start.assert_called_once()
            app.meter.sample.assert_not_called()
            worker.call_args.kwargs['target']()
        self.assertTrue(app.meter.sample.called)

    def test_reset_asks_before_clearing(self):
        app = app_stub()
        app._traffic_profile = ''
        app.meter.note(19, 0, 0, now=1.0)
        app.meter.note(19, 900, 900, now=2.0)
        with patch.object(gui.messagebox, 'askyesno', return_value=False):
            app.traffic_reset_click()
        self.assertEqual(app.meter.summary(now=2.0)['total']['sum'], 1800)
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            app.traffic_reset_click()
        self.assertEqual(app.meter.summary(now=2.0)['total']['sum'], 0)


class CliTests(unittest.TestCase):
    def test_watch_preserves_explicit_overrides_on_reload(self):
        overrides = {'server': '192.0.2.100', 'drcom_port': 8080, 'eportal_port': 8081}
        seen = []
        def inspect(cfg):
            seen.append({k: cfg[k] for k in overrides})
            raise KeyboardInterrupt
        with patch.object(cl, 'load_config', return_value=config()), \
             patch.object(cl, 'inspect_network', side_effect=inspect), redirect_stdout(io.StringIO()):
            cl.cmd_watch(config(**overrides), overrides)
        self.assertEqual(seen, [overrides])

    def test_main_passes_only_explicit_cli_options(self):
        with patch.object(sys, 'argv', ['campus_login.py', 'watch', '--drcom-port', '8080']), \
             patch.object(cl, 'load_config', return_value=config()), patch.object(cl, 'cmd_watch') as watch, \
             redirect_stdout(io.StringIO()):
            cl.main()
        self.assertEqual(watch.call_args.kwargs['overrides'], {'drcom_port': 8080})


if __name__ == '__main__':
    unittest.main()
