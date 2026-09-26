#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
梧州学院校园网（Dr.COM 哆点 ePortal 4.x）自动登录工具
=====================================================
协议逆向自登录页面脚本 a40.js / a41.js 与 loadConfig 响应。

认证流程:
  1. GET http://<server>/drcom/chkstatus                 -> 查询在线状态、本机IP、MAC
  2. GET http://<server>:803/eportal/portal/page/loadConfig -> 获取 program_index / page_index / rcn
  3. GET http://<server>/drcom/login                     -> JSONP 登录(密码明文, en_md5=0, 无验证码)
  4. GET http://<server>/drcom/logout                    -> 注销

用法:
  python campus_login.py                # 检查并登录(默认)
  python campus_login.py status         # 仅查询在线状态
  python campus_login.py logout         # 注销下线
  python campus_login.py watch          # 守护模式: 定时检测, 掉线自动重连
  python campus_login.py traffic        # 流量统计: 今日/本月/累计 + 服务端会话
  python campus_login.py set -u 账号 -p 密码 [--isp 1]   # 保存账号配置
  python campus_login.py test           # 测试服务器连通性

仅使用 Python 标准库, 无需安装第三方依赖。
"""

import argparse
import base64
import getpass
import http.client
import json
import ipaddress
import os
import random
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import campus_traffic as ct
from campus_network import inspect_network

AUTH_LOCK = threading.RLock()

# PyInstaller exe 模式下, 配置文件放在 exe 同目录(与脚本模式共用同一份)
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "campus_login.json")

DEFAULT_CONFIG = {
    "server": "192.168.0.203",      # 认证服务器(v4serip)
    "drcom_port": 80,               # 内核 drcom 接口端口
    "eportal_port": 803,            # eportal 接口端口(epHTTPPort)
    "account": "",                  # 校园网账号
    "password": "",                 # 校园网密码
    "isp": "1",                     # 服务类型(真实抓包确认用户使用 R3=1 中国移动;
                                    # 页面映射: 0校园网 1移动 2电信 3联通 4广电)
    "terminal_type": "3",           # 终端类型(抓包值: 3)
    "business_type": "3",           # 业务表单类型(抓包值: 3, 对应页面表单 f3)
    "program_index": "",            # 留空则运行时通过 loadConfig 自动获取
    "page_index": "",
    "rcn": "",
    "watch_interval": 30,           # watch 模式检测间隔(秒)
    "timeout": 5,                   # 请求超时(秒)
    "traffic_interval": 5,          # 流量采样间隔(秒, 5-3600); 仅 GUI/守护常驻时生效
    "auto_login_on_start": 0,       # 程序启动后自动登录(配合开机自启=开机自动登录)
    "auto_watch": 0,                # 程序启动后自动开启守护模式
    "campus_networks": ["WZXY-Student"],  # 仅允许在这些 Windows 网络配置上认证
}

# loadConfig 捕获时的静态兜底值(若运行时自动获取失败则使用)
FALLBACK_PORTAL = {
    "program_index": "ACMG4W1787883078",
    "page_index": "3GozJf1788490374",
    "rcn": "ovlCKe9f",
    "enable_r3": 1,
    "en_md5": 0,
    "login_method": 0,
    "account_prefix": 1,
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 服务类型真实映射(从登录页模板 pc.js 的 ISP_select 解出, 与静态页废弃的 carrier 字段不同!)
ISP_NAMES = {"0": "校园网", "1": "中国移动", "2": "中国电信", "3": "中国联通", "4": "中国广电"}

# en_md5=1 时页面使用的加盐常量(reference/a40.js: var PID='1'; var CALG='12345678')
MD5_PID = "1"
MD5_CALG = "12345678"

# /drcom/* 响应 msg 数字码含义(逆向自 a40.js default_login_err 的 switch)
MSG_NAMES = {
    0: "账号或密码不对", 1: "账号或密码不对", 2: "该账号正在使用中",
    3: "本账号只能在指定地址使用", 4: "费用超支或时长流量超限",
    5: "本账号暂停使用", 6: "系统缓存太满", 7: "本账号正在使用,不能修改",
    10: "密码修改成功", 11: "本账号只能在指定地址使用",
    14: "注销成功", 15: "登录成功",
}


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------- 控制台输出

LOG_HOOK = None  # GUI 注入的日志回调, 签名 (message: str) -> None; 注入后不再打印到控制台


def log(msg):
    if LOG_HOOK is not None:
        try:
            LOG_HOOK(msg)
            return
        except Exception:
            pass
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg))


def err(msg):
    if LOG_HOOK is not None:
        try:
            LOG_HOOK("[错误] " + msg)
            return
        except Exception:
            pass
    print("[{}][错误] {}".format(time.strftime("%H:%M:%S"), msg), file=sys.stderr)


# ---------------------------------------------------------------- 配置读写

# 整数型配置项: key -> (最小值, 最大值, 回退默认值)
# 配置文件是人手可编辑的 JSON, 键存在但类型写错(如 "timeout": "5")时,
# 原样使用会一路传到 socket 层抛 TypeError, 或被报成"无法连接认证服务器",
# 把排查方向带偏。这里集中规整一次, CLI 与 GUI 共用同一出口。
_INT_FIELDS = {
    "drcom_port": (1, 65535, 80),
    "eportal_port": (1, 65535, 803),
    "watch_interval": (10, 3600, 30),
    "timeout": (1, 60, 5),
    "traffic_interval": (5, 3600, 5),
    "auto_login_on_start": (0, 1, 0),
    "auto_watch": (0, 1, 0),
}

_STR_FIELDS = ("server", "account", "password", "isp", "terminal_type",
               "business_type", "program_index", "page_index", "rcn")


def normalize_config(cfg):
    """把配置值规整为下游可安全使用的类型, 就地修改并返回 cfg."""
    for key, (lo, hi, default) in _INT_FIELDS.items():
        raw = cfg.get(key, default)
        val = _to_int(raw)
        if val is None:
            err("配置项 {} 的值 {!r} 不是整数, 已回退为 {}".format(key, raw, default))
            val = default
        elif not (lo <= val <= hi):
            err("配置项 {} 的值 {} 超出允许范围 {}-{}, 已回退为 {}".format(
                key, val, lo, hi, default))
            val = default
        cfg[key] = val
    for key in _STR_FIELDS:
        val = cfg.get(key, "")
        if not isinstance(val, str):
            cfg[key] = "" if val is None else str(val)
    if cfg["isp"] not in ISP_NAMES:
        err("配置项 isp 的值 {!r} 无效(应为 0-4), 已回退为 {}".format(
            cfg["isp"], DEFAULT_CONFIG["isp"]))
        cfg["isp"] = DEFAULT_CONFIG["isp"]
    return cfg


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            # utf-8-sig: 容忍记事本等编辑器写入的 BOM, 否则整份配置被判为损坏
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
            else:
                # 顶层是 5 / [1,2] / null / true 时, cfg.update 会抛 TypeError,
                # 原先只捕 ValueError/OSError, 异常会直接冒到调用方。
                err("配置文件顶层不是 JSON 对象({}), 已忽略并改用默认配置".format(
                    type(data).__name__))
        except (ValueError, OSError) as e:
            err("配置文件读取失败, 使用默认配置: {}".format(e))
    return normalize_config(cfg)


def save_config(cfg):
    """原子写入配置.

    直接 open(path, "w") 会先把原文件截断, 写盘途中进程被杀/断电就会留下
    空文件或半截 JSON —— 账号密码一并丢失。改为写同目录临时文件后 os.replace,
    同一卷上是原子操作, 要么是完整新内容, 要么仍是完整旧内容。
    """
    directory = os.path.dirname(CONFIG_PATH) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    fd, tmp_path = tempfile.mkstemp(prefix=".campus_login.", suffix=".tmp",
                                    dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- HTTP / JSONP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动重定向, 用于 302 探测是否被强制跳转到认证页."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 强制直连(不走系统代理/环境变量代理): 校园网内网地址经代理转发会被代理
# 返回 404 或无法解析, 这就是此前"注销提示 404"的根因。
_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect)


class _SourceHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, source_ip):
        super().__init__()
        self.source_ip = source_ip

    def http_open(self, req):
        return self.do_open(http.client.HTTPConnection, req,
                            source_address=(self.source_ip, 0))


class NetworkNotReady(OSError):
    pass


def http_get(url, timeout, referer=None, source_ip=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer or "http://{}/".format(cfg_server_of(url)),
        "Accept": "*/*",
    })
    opener = _DIRECT_OPENER if not source_ip else urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect, _SourceHTTPHandler(source_ip))
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def cfg_server_of(url):
    """提取 URL 的 host[:port] 用于 Referer."""
    try:
        return url.split("/")[2]
    except IndexError:
        return url


def _referer_of(cfg, port):
    """构造 Referer, 与浏览器抓包一致.

    抓包值为 `http://192.168.0.203/`(默认端口不带 `:80`), 而 URL 里的 host:port
    会拼出 `http://192.168.0.203:80/`。虽实测服务端不校验, 但保持字节级一致更稳。
    """
    host = cfg["server"]
    return "http://{}/".format(host if port in (80, None) else "{}:{}".format(host, port))


def _match_brace(text, start):
    """从 text[start](必须为'{')起做括号配对, 返回与之配对的'}'下标. 跳过字符串字面量."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def parse_jsonp(text):
    """解析 JSONP 响应, 如 dr1003({"result":1,...});
    兼容 try{}catch(e){} 包裹、纯 JSON 等多种形式."""
    text = text.strip()
    if text.startswith(("{", "[")):
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("响应顶层必须为 JSON 对象")
        return value
    # 以第一个 '(' 为锚点(try{dr1003({...});}catch(e){} 时 '{' 出现在 '(' 之后)
    i = text.find("(")
    start = text.find("{", i + 1) if i != -1 else text.find("{")
    while start != -1:
        end = _match_brace(text, start)
        if end != -1:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
        start = text.find("{", start + 1)
    raise ValueError("响应不是 JSONP/JSON 格式: " + text[:120])


def _quote_param(s):
    """按浏览器 encodeURIComponent 的语义做百分号编码.

    urllib.parse.urlencode 默认用 quote_plus, 与 encodeURIComponent 有两处差异:
      - 空格 -> '+'   (encodeURIComponent 为 '%20')
      - ! ' ( ) * 被编码, 而 encodeURIComponent 保持原样
    页面 a41.js 的 formatParams 用的正是 encodeURIComponent, 密码里出现空格或
    这几个符号时字节序列会不同。这里显式对齐浏览器行为。
    """
    return urllib.parse.quote(str(s), safe="-_.!~*'()")


def jsonp(cfg, base, path, params):
    """发起 JSONP GET 请求并解析结果. base 形如 http://192.168.0.203:80

    关键约定(与页面 a41.js formatParams 完全一致):
      - callback 必须是第一个查询参数(页面用 arr.unshift 置于首位)。
        drcom 内核(80端口 /drcom/*)只认首位的 callback, 放在后面直接返回 404;
        eportal(803端口)则不敏感。
      - v(随机数)与 lang 追加在末尾; data 中可另带 lang=zh-cn(登录请求如此)。
    """
    params = dict(params)
    cb = "dr{}".format(random.randint(1001, 9999))
    params.setdefault("jsVersion", "4.5.1")
    pairs = ["{}={}".format(_quote_param(k), _quote_param(v))
             for k, v in params.items() if k != "callback"]
    url = "{}?callback={}".format(base.rstrip("/") + path, cb)
    if pairs:
        url += "&" + "&".join(pairs)
    url += "&v={}&lang=zh".format(random.randint(500, 10500))
    # Referer 按域名+实际端口构造, 默认端口(80)不带 ":80", 与抓包一致
    host_port = base.rstrip("/").split("//", 1)[-1]
    host, _, port = host_port.partition(":")
    referer = _referer_of(cfg, _to_int(port))
    source_ip = None
    if path in ("/drcom/login", "/drcom/logout"):
        network = inspect_network(cfg)
        if not network.ready:
            raise NetworkNotReady(network.message)
        source_ip = network.source_ip
    raw = http_get(url, cfg["timeout"], referer=referer,
                   source_ip=source_ip).decode("utf-8", errors="replace")
    return parse_jsonp(raw)


# ---------------------------------------------------------------- 核心接口

def base64_str(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def check_status(cfg):
    """查询内核在线状态. 返回 dict: result(1在线/0离线), ip, mac, uid, raw"""
    base = "http://{}:{}".format(cfg["server"], cfg["drcom_port"])
    params = {}
    if cfg.get("program_index"):
        params["program_index"] = cfg["program_index"]
    if cfg.get("page_index"):
        params["page_index"] = cfg["page_index"]
    data = jsonp(cfg, base, "/drcom/chkstatus", params)
    ip = data.get("ss5") or data.get("v46ip") or ""
    if str(data.get("result")) not in ("0", "1") or "ss4" not in data:
        raise ValueError("服务器未返回预期的 Dr.COM 状态，停止认证")
    try:
        ipaddress.IPv4Address(ip)
    except (ValueError, TypeError):
        raise ValueError("服务器未返回有效的本机 IPv4 地址，停止认证") from None
    return {
        "online": str(data.get("result")) == "1",
        "ip": data.get("ss5") or data.get("v46ip") or "",
        "mac": data.get("ss4") or "",
        "uid": data.get("uid") or data.get("DDDDD") or "",
        "raw": data,
    }


def load_portal_config(cfg, status=None):
    """调用 eportal loadConfig 获取 program_index/page_index/rcn 等, 失败则回退静态值.

    status: 已查询到的在线状态(含本机 IP)。do_login 已经查过一次, 传进来即可
    避免重复请求 chkstatus。
    """
    result = dict(FALLBACK_PORTAL)
    if status is not None:
        ip = status.get("ip") or "0.0.0.0"
    else:
        try:
            st = check_status(cfg)
            ip = st["ip"] or "0.0.0.0"
        except Exception:
            ip = "0.0.0.0"
    base = "http://{}:{}".format(cfg["server"], cfg["eportal_port"])
    params = {
        "program_index": cfg.get("program_index", ""),
        "wlan_vlan_id": "1",
        "wlan_user_ip": base64_str(ip),
        "wlan_user_ipv6": base64_str("::"),
        "wlan_user_ssid": "",
        "wlan_user_areaid": "",
        "wlan_ac_ip": "",
        "wlan_ap_mac": "000000000000",
        "gw_id": "000000000000",
        "page_index": "",
    }
    try:
        data = jsonp(cfg, base, "/eportal/portal/page/loadConfig", params)
        d = data.get("data") or {}
        # 逐字段回填: 服务端省略某个键时保留兜底值, 而不是静默降级为 0。
        # enable_r3 尤其关键 —— 它决定登录请求里的 enable_r3, 页面语义是
        # "串接 pppoe 代拨 0 停用 1 启用", 被误置为 0 会静默改变认证行为。
        for key in ("program_index", "page_index", "rcn"):
            if d.get(key):
                result[key] = d[key]
        for key, fallback in (("enable_r3", result["enable_r3"]),
                              ("en_md5", result["en_md5"]),
                              ("login_method", result["login_method"]),
                              ("account_prefix", result["account_prefix"])):
            val = d.get(key)
            if val is None or val == "":
                # 服务端未返回该键: 保留兜底值并留痕, 便于日后排查
                log("loadConfig 未返回 {}, 沿用兜底值 {}".format(key, fallback))
                result[key] = fallback
            else:
                num = _to_int(val)
                result[key] = fallback if num is None else num
        result["enable_alias"] = _to_int(d.get("enable_alias")) or 0
        result["account_suffix"] = d.get("account_suffix") or ""
        # 服务端自述的 eportal 端口(参考抓包中 ep_http_port=801, 与代码默认 803 不同)。
        # 当前该校 803 实测可用, 故仅作为提示信息记录, 不自动改动运行时端口,
        # 免得把一个能用的配置改坏。留此字段便于日后排查端口漂移。
        ep_port = _to_int(d.get("ep_http_port"))
        if ep_port and ep_port != cfg.get("eportal_port"):
            log("提示: 服务端自述 eportal 端口为 {} (当前配置 {}), 如遇连接问题可尝试切换".format(
                ep_port, cfg.get("eportal_port")))
        log("已获取门户配置: program_index={}, page_index={}".format(
            result["program_index"], result["page_index"]))
    except Exception as e:
        log("loadConfig 获取失败, 使用内置兜底配置: {}".format(e))
    return result


def do_login(cfg, verbose=True, status=None, cancel=None):
    """Serialize authentication; a cancelled queued login must never run."""
    with AUTH_LOCK:
        return _do_login(cfg, verbose, status, cancel)


def _do_login(cfg, verbose=True, status=None, cancel=None):
    """执行完整登录流程. 返回 (success, message). status 可传入已查询的在线状态."""
    if cancel is not None and cancel.is_set():
        return False, "已取消登录"
    network = inspect_network(cfg)
    if not network.ready:
        return False, "等待校园网: " + network.message
    account = cfg.get("account", "")
    password = cfg.get("password", "")
    if not account or not password:
        return False, "未配置账号或密码, 请先运行: python campus_login.py set"

    # 1. 查询在线状态(同时获取本机IP/MAC)
    if verbose:
        log("查询在线状态...")
    if status is None:
        try:
            status = check_status(cfg)
        except Exception as e:
            return False, "无法连接认证服务器({}): {}".format(cfg["server"], e)
    st = status

    if st["online"]:
        if verbose:
            log("当前已在线, 无需登录. 账号: {}, IP: {}".format(st["uid"] or "-", st["ip"] or "-"))
        return True, "already-online"

    ip, mac = st["ip"], st["mac"]
    if verbose:
        log("当前离线. 本机IP: {}, MAC: {}".format(ip or "?", mac or "?"))

    # 2. 获取门户配置(复用已查到的 status, 不再重复请求 chkstatus)
    portal = load_portal_config(cfg, status=st)

    # 3. 登录(参数与浏览器抓包逐字段一致: callback首位/jsVersion=4.5.1/
    #    terminal_type=3/lang=zh-cn/business_type=3/R3=服务类型)
    isp = str(cfg.get("isp", "1"))
    if isp not in ISP_NAMES:
        isp = "1"

    # 账号: 页面 default_login 会先剔除所有空白字符
    account = re.sub(r"\s+", "", account)
    upass = password
    # en_md5=1 时按页面算法变形, 并同步置 R2=1(a40.js: R2: page.en_md5 ? 1 : '')
    if portal.get("en_md5"):
        upass = md5_plus(password)
    r2 = "1" if portal.get("en_md5") else ""
    # upass 同样去空白, 与页面一致(在加密之后执行)
    upass = re.sub(r"\s+", "", upass)

    # 账号前缀: loadConfig account_prefix=1 时, user_account 带 ",0," 前缀
    # (抓包确认: user_account=%2C0%2C<账号> → ",0,<账号>")
    prefix = ",0," if portal.get("account_prefix", 1) == 1 else ""

    base = "http://{}:{}".format(cfg["server"], cfg["drcom_port"])
    params = {
        "DDDDD": account,
        "upass": upass,
        "0MKKey": "123456",
        "R1": "0",
        "R2": r2,
        "R3": isp,
        "R6": "0",
        "para": "00",
        "v6ip": "",
        "R7": "0",
        "user_account": prefix + account,
        "user_password": password,
        "wlan_user_ip": ip,
        "wlan_user_ipv6": "",
        "authex_enable": "",
        "wlan_user_mac": mac,
        "wlan_ac_name": "",
        "jsVersion": "4.5.1",
        "terminal_type": str(cfg.get("terminal_type", "3")),
        "lang": "zh-cn",
        "user_agent": UA,
        "enable_r3": str(portal.get("enable_r3", 1)),
        "mac_type": "0",
        "rcn": portal.get("rcn", ""),
        "operate": "portal_login",
        "business_type": str(cfg.get("business_type", "3")),
        "program_index": portal.get("program_index", ""),
        "page_index": portal.get("page_index", ""),
    }
    if verbose:
        log("正在登录(服务类型: {})...".format(ISP_NAMES.get(isp, isp)))

    last_msg = "未知错误"
    for attempt in range(1, 4):  # 最多重试3次
        if cancel is not None and cancel.is_set():
            return False, "已取消登录"
        try:
            resp = jsonp(cfg, base, "/drcom/login", params)
        except NetworkNotReady as e:
            return False, "等待校园网: " + str(e)
        except Exception as e:
            last_msg = "请求异常: {}".format(e)
        else:
            if str(resp.get("result")) == "1" or resp.get("result") == "ok":
                if verbose:
                    log("登录成功! 账号: {}, IP: {}".format(account, ip or "-"))
                return True, "login-ok"
            code = _to_int(resp.get("msg"))
            msga = str(resp.get("msga") or "").strip()
            # msga="clientip online": 该IP已在线, 内核拒绝重复登录 → 视为成功
            if msga == "clientip online":
                if verbose:
                    log("IP 已在线(内核拒绝重复登录), 账号: {}".format(
                        resp.get("uid") or account))
                return True, "already-online(clientip-online)"
            if msga:
                last_msg = "msg={} msga={}".format(code, msga)
            elif code in MSG_NAMES:
                last_msg = "msg={} {}".format(code, MSG_NAMES[code])
            else:
                last_msg = json.dumps(resp, ensure_ascii=False)
            if verbose:
                log("登录失败(第{}次): {}".format(attempt, last_msg))
            # 账号密码类错误(0/1且无msga)不重试
            if code in (0, 1) and not msga:
                break
        if attempt < 3:
            if cancel is not None:
                if cancel.wait(1):
                    return False, "已取消登录"
            else:
                time.sleep(1)
    return False, last_msg


def do_logout(cfg, verbose=True):
    with AUTH_LOCK:
        network = inspect_network(cfg)
        if not network.ready:
            err(network.message)
            return False
        return _do_logout(cfg, verbose)


def _do_logout(cfg, verbose=True):
    # 与浏览器一致: 注销页按钮 -> wc() -> logout.init() -> portal_logout({})
    # JSONP GET /drcom/logout, 仅带 program_index/page_index + jsonp 标准参数
    portal = load_portal_config(cfg)
    base = "http://{}:{}".format(cfg["server"], cfg["drcom_port"])
    params = {
        "program_index": portal.get("program_index", ""),
        "page_index": portal.get("page_index", ""),
    }
    try:
        resp = jsonp(cfg, base, "/drcom/logout", params)
    except Exception as e:
        err("注销请求失败: {}".format(e))
        return False
    ok = str(resp.get("result")) == "1" or resp.get("result") == "ok"
    if verbose:
        if ok:
            log("注销成功 (服务器返回: {})".format(
                resp.get("msg", "")))
        else:
            err("注销失败: {}".format(json.dumps(resp, ensure_ascii=False)))
    return ok


# ---------------------------------------------------------------- 网络连通性

# (url, 期望状态码, 期望响应包含的字节): 校验内容可防止校园网直接返回
# 200 认证页 HTML(不发 302) 时被误判为 online。
INTERNET_TEST_URLS = [
    ("http://connect.rom.miui.com/generate_204", 204, None),
    ("http://www.msftconnecttest.com/connecttest.txt", 200, b"Microsoft Connect Test"),
]

# 探测过程中"该目标不可用"的异常集合, 命中则换下一个 URL。
# 注意 http.client.HTTPException(BadStatusLine/IncompleteRead/LineTooLong 等)
# **不是 OSError 子类**, 必须显式列出: 网关/运营商劫持时返回非 HTTP 字节会抛
# BadStatusLine, 漏掉它会让 check_internet 把异常抛给调用方 —— cmd_status /
# cmd_login / cmd_test 三处调用点都没有 try 包裹, 用户会直接看到 traceback。
_INTERNET_PROBE_ERRORS = (
    urllib.error.URLError,
    OSError,
    http.client.HTTPException,
)


def check_internet(cfg):
    """探测是否真正接入互联网. 返回 'online' / 'offline' / 'unknown'.

    必须走 _DIRECT_OPENER(强制直连 + 禁止跟随重定向):
      - 裸 urlopen 会读系统/环境代理, 挂 VPN 时探测结果失真;
      - 默认 opener 自动跟随 302, 未认证时被劫持到认证页会拿到 200 而误判 online
        (下面的 30x 分支只有在禁重定向后才会真正触发)。
    """
    portal_seen = False
    for url, expect_status, expect_body in INTERNET_TEST_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with _DIRECT_OPENER.open(req, timeout=cfg["timeout"]) as resp:
                if resp.status != expect_status:
                    portal_seen |= resp.status == 200
                    continue
                if expect_body is not None and expect_body not in resp.read():
                    portal_seen = True
                    continue
                return "online"
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "") if e.headers else ""
            if e.code in (301, 302, 303, 307, 308):
                target = urllib.parse.urlsplit(urllib.parse.urljoin(url, loc)).hostname
                portal_seen |= target == cfg["server"]
            continue
        except _INTERNET_PROBE_ERRORS:
            continue              # 该探测目标不可用(含畸形响应), 换下一个
    return "offline" if portal_seen else "unknown"


# ---------------------------------------------------------------- 流量统计

def server_session(cfg, status=None, smac=None):
    """查询服务端记录的**当前会话**(eportal online_list), 失败返回 None.

    对应页面 a41.js 的 `checkStatus()`: 当 `check_online_method==1` 时, 在线
    判断改走 `online_list` 而不是内核 chkstatus。返回体形如:

        {"result":1,"list":[{"online_session":21638,"online_time":"...",
          "time_long":"17256","uplink_bytes":"0","downlink_bytes":"0", ...}]}

    字段口径提醒: 这些是**本次会话**的计数, 由 NAS 上报, 本校当前恒为 0;
    `time_long` 单位是秒。真正可靠的历史累计只能由本机网卡计数器得到,
    见 campus_traffic。本函数只做"服务端视角"的对照展示, 不参与累计。
    """
    st = status
    if st is None:
        try:
            st = check_status(cfg)
        except Exception:
            return None
    mac = (smac or st.get("mac") or "").upper()
    base = "http://{}:{}".format(cfg["server"], cfg["eportal_port"])
    params = {
        "user_account": "",
        "user_password": "",
        "wlan_user_mac": mac,
        "wlan_user_ip": base64_str(st.get("ip") or ""),
        "wlan_user_ipv6": base64_str("::"),
    }
    try:
        data = jsonp(cfg, base, "/eportal/portal/online_list", params)
    except Exception as e:
        log("查询服务端在线会话失败: {}".format(e))
        return None
    if str(data.get("result")) not in ("1", "ok"):
        log("服务端未返回在线会话: {}".format(
            data.get("msg") or json.dumps(data, ensure_ascii=False)[:120]))
        return None
    records = data.get("list")
    if not isinstance(records, list) or not records:
        return None
    # 优先取本机 IP 的那条; 服务端按账号返回全部在线终端
    mine = next((r for r in records if isinstance(r, dict)
                 and str(r.get("online_ip")) == str(st.get("ip"))), None)
    record = mine or (records[0] if isinstance(records[0], dict) else None)
    if not record:
        return None
    return {
        "account": record.get("user_account") or "",
        "ip": record.get("online_ip") or "",
        "mac": record.get("online_mac") or "",
        "login_time": record.get("online_time") or "",
        "seconds": _to_int(record.get("time_long")),
        "uplink": _to_int(record.get("uplink_bytes")),
        "downlink": _to_int(record.get("downlink_bytes")),
        "isp_bind_account": record.get("isp_bind_account") or "",
        "devices": len(records),
        "raw": record,
    }


def session_line(session):
    """把 server_session 的结果格式化为一行摘要(CLI 与 GUI 共用)."""
    if not session:
        return "服务端会话: 暂无(未上线或接口不可用)"
    up = session.get("uplink")
    down = session.get("downlink")
    return ("服务端会话: {} 上线 · 时长 {} · 上行 {} · 下行 {}"
            "{}".format(session.get("login_time") or "-",
                        ct.format_duration(session.get("seconds")),
                        ct.format_bytes(up) if up is not None else "未提供",
                        ct.format_bytes(down) if down is not None else "未提供",
                        " · 在线终端 {} 台".format(session["devices"])
                        if session.get("devices", 0) > 1 else ""))


def traffic_report(cfg, meter, status=None, days=14, list_only=False):
    """采集一次并打印统计. 返回 (ok, 摘要字典).

    采样前先做校园网检查: 非校园网时**不读**本机计数器, 免得把家庭网络/
    热点/手机共享的流量算进校园网账上。
    """
    if list_only:
        rows = ct.list_interfaces()
        if not rows:
            err("无法读取本机网卡计数器(仅 Windows 支持)")
            return False, None
        log("本机网卡累计收发(诊断用, 与统计口径无关):")
        for row in sorted(rows, key=lambda r: r["index"]):
            log("  [{}] {:<28} 收 {} 发 {}{}".format(
                row["index"], (row["alias"] or row["description"])[:28],
                ct.format_bytes(row["rx"]), ct.format_bytes(row["tx"]),
                "" if row["up"] else " (未启用)"))
        return True, None

    network = inspect_network(cfg)
    if not network.ready:
        log(network.message)
        log("非校园网期间不计流量(避免把其他网络的用量算进校园网)。")
    else:
        delta = meter.sample(network.interface_index)
        if delta is None:
            err("读取网卡计数器失败, 本次未计入")
        else:
            meter.flush()
            log("本次采样增量: 下行 {} · 上行 {}".format(
                ct.format_bytes(delta[0]), ct.format_bytes(delta[1])))

    data = meter.summary(days=days)
    log("本机流量统计(网卡: {}):".format(network.profile or "-"))
    log("  今日   下行 {:<12} 上行 {:<12} 合计 {}".format(
        ct.format_bytes(data["today"]["rx"]), ct.format_bytes(data["today"]["tx"]),
        ct.format_bytes(data["today"]["sum"])))
    log("  本月   下行 {:<12} 上行 {:<12} 合计 {}".format(
        ct.format_bytes(data["month"]["rx"]), ct.format_bytes(data["month"]["tx"]),
        ct.format_bytes(data["month"]["sum"])))
    log("  累计   下行 {:<12} 上行 {:<12} 合计 {}".format(
        ct.format_bytes(data["total"]["rx"]), ct.format_bytes(data["total"]["tx"]),
        ct.format_bytes(data["total"]["sum"])))
    if data["days"]:
        log("  最近 {} 天明细:".format(len(data["days"])))
        for row in data["days"]:
            log("    {}  下行 {:<12} 上行 {:<12} 合计 {}".format(
                row["date"], ct.format_bytes(row["rx"]), ct.format_bytes(row["tx"]),
                ct.format_bytes(row["sum"])))
    log("  统计文件: {}".format(data["path"]))

    st = status
    if st is None:
        try:
            st = check_status(cfg)
        except Exception as e:
            log("无法查询认证状态: {}".format(e))
    if st is not None:
        log("  " + session_line(server_session(cfg, status=st)))
    return True, data


def cmd_traffic(cfg, args):
    meter = ct.TrafficMeter()
    if getattr(args, "reset", False):
        if not getattr(args, "yes", False):
            err("重置会清空全部历史累计, 请加 --yes 确认")
            return 2
        meter.reset()
        log("流量统计已重置: {}".format(meter.path))
        return 0
    days = max(1, min(400, _to_int(getattr(args, "days", None)) or 14))
    ok, _ = traffic_report(cfg, meter, days=days,
                           list_only=getattr(args, "list", False))
    return 0 if ok else 1


def cmd_set(cfg, args):
    if args.server:
        cfg["server"] = args.server
    if args.account is not None:
        cfg["account"] = args.account
    if args.password is not None:
        cfg["password"] = args.password
    if args.isp:
        cfg["isp"] = str(args.isp)
    if args.terminal_type:
        cfg["terminal_type"] = str(args.terminal_type)
    if args.business_type:
        cfg["business_type"] = str(args.business_type)
    if args.interval:
        cfg["watch_interval"] = args.interval
    if args.drcom_port:
        cfg["drcom_port"] = args.drcom_port
    if args.eportal_port:
        cfg["eportal_port"] = args.eportal_port
    cfg = normalize_config(cfg)   # 校验范围, 非法值会被挡下并提示
    save_config(cfg)
    log("配置已保存到 {}".format(CONFIG_PATH))
    log("当前账号: {} | 服务类型: {} | 服务器: {}:{}".format(
        cfg["account"] or "(未设置)", ISP_NAMES.get(str(cfg["isp"]), cfg["isp"]),
        cfg["server"], cfg["drcom_port"]))


def cmd_login(cfg):
    network = inspect_network(cfg)
    if not network.ready:
        log(network.message)
        return 2
    # 先查状态: 已在线则无需账号配置
    log("查询在线状态...")
    try:
        st = check_status(cfg)
    except Exception as e:
        err("无法连接认证服务器({}): {}".format(cfg["server"], e))
        err("请确认本机已连接校园网(网线/Wi-Fi)。")
        return 2
    if st["online"]:
        log("当前已在线, 账号: {}, IP: {}, 无需重复登录。".format(st["uid"] or "-", st["ip"] or "-"))
        net = check_internet(cfg)
        if net == "online":
            log("互联网连通性验证通过, 一切正常。")
        else:
            log("互联网连通性: {} (认证服务器显示在线, 可能有个别网站异常)".format(net))
        return 0
    if not cfg.get("account") or not cfg.get("password"):
        interactive_config(cfg)
    ok, msg = do_login(cfg, status=st)
    net = check_internet(cfg)
    if ok:
        if net == "online":
            log("互联网连通性验证通过, 一切正常。")
        else:
            log("登录接口返回成功, 但互联网连通性为: {} (刚登录可能需要几秒生效)".format(net))
    else:
        err("登录失败: {}".format(msg))
    return 0 if ok else 1


def cmd_status(cfg):
    try:
        st = check_status(cfg)
    except Exception as e:
        err("无法连接认证服务器({}): {}".format(cfg["server"], e))
        return 2
    net = check_internet(cfg)
    state = "在线" if st["online"] else "离线"
    log("认证状态: {} | 互联网: {} | 账号: {} | 本机IP: {} | MAC: {}".format(
        state, {"online": "通", "offline": "不通", "unknown": "未知"}[net],
        st["uid"] or "-", st["ip"] or "-", st["mac"] or "-"))
    return 0 if st["online"] else 1


def cmd_logout(cfg):
    ok = do_logout(cfg)
    return 0 if ok else 1


def cmd_watch(cfg, overrides=None):
    if not cfg.get("account") or not cfg.get("password"):
        if sys.stdin.isatty():
            interactive_config(cfg)
        else:
            err("未配置账号或密码, 请先运行: python campus_login.py set")
            return 1
    interval = max(10, int(cfg.get("watch_interval", 30)))
    log("进入守护模式, 每 {} 秒检测一次, Ctrl+C 退出。".format(interval))
    log("提示: 守护期间修改配置会即时生效(无需重启)。")
    fail_count = 0
    network_message = None
    while True:
        try:
            # 循环内重读配置: 守护常驻期间改服务器/端口/间隔即时生效
            cfg = load_config()
            cfg.update(overrides or {})
            cfg = normalize_config(cfg)
            new_interval = max(10, int(cfg.get("watch_interval", 30)))
            if new_interval != interval:
                interval = new_interval
                log("检测到守护间隔已改为 {} 秒。".format(interval))
            network = inspect_network(cfg)
            if network.message != network_message:
                log(network.message)
                network_message = network.message
            if not network.ready:
                time.sleep(interval)
                continue
            net = check_internet(cfg)
            if net == "online":
                fail_count = 0
                log("网络正常。")
            else:
                log("互联网不通({}), 尝试重新认证...".format(net))
                ok, msg = do_login(cfg)
                if ok:
                    fail_count = 0
                else:
                    fail_count += 1
                    if fail_count >= 3:
                        err("连续 {} 次认证失败, 最近错误: {}".format(fail_count, msg))
        except KeyboardInterrupt:
            log("已退出守护模式。")
            return 0
        except Exception as e:
            err("守护循环异常: {}".format(e))
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("已退出守护模式。")
            return 0


def cmd_test(cfg):
    log("测试认证服务器 {}/drcom ...".format(cfg["server"]))
    try:
        st = check_status(cfg)
        log("  chkstatus 正常 -> 在线: {}, IP: {}, MAC: {}".format(
            st["online"], st["ip"] or "?", st["mac"] or "?"))
    except Exception as e:
        err("  chkstatus 失败: {}".format(e))
        err("  提示: 请确认本机已连接校园网, 且 drcom_port 配置正确(默认80)。")
        return 1
    log("测试 eportal :{}/eportal ...".format(cfg["eportal_port"]))
    try:
        portal = load_portal_config(cfg, status=st)
    except Exception as e:
        err("  loadConfig 失败: {}".format(e))
        portal = dict(FALLBACK_PORTAL)
    log("  loadConfig -> program_index={} | enable_r3={} | en_md5={} | account_prefix={}".format(
        portal["program_index"], portal["enable_r3"], portal["en_md5"],
        portal["account_prefix"]))
    net = check_internet(cfg)
    log("互联网连通性: {}".format(net))
    ok = net != "unknown"
    if not ok:
        err("  提示: 两个探测目标均不可达, 可能是代理/防火墙拦截或本机确实离线。")
    return 0 if ok else 1


def interactive_config(cfg):
    print("=" * 52)
    print("首次使用, 请配置校园网账号(保存到 {})".format(CONFIG_PATH))
    print("=" * 52)
    account = input("账号: ").strip()
    password = getpass.getpass("密码: ")
    print("服务类型: " + " ".join(
        "{}= {}".format(k, v) for k, v in sorted(ISP_NAMES.items())))
    # 默认值与 DEFAULT_CONFIG["isp"] / GUI 兜底保持一致, 避免一路回车写入错误的 R3
    default_isp = DEFAULT_CONFIG["isp"]
    raw = input("请选择 [{}]: ".format(default_isp)).strip() or default_isp
    if raw not in ISP_NAMES:
        err("{} 不是有效的服务类型, 已使用默认值 {}".format(raw, default_isp))
        raw = default_isp
    cfg["account"], cfg["password"], cfg["isp"] = account, password, raw
    save_config(cfg)
    log("配置已保存。\n")


def md5_plus(password):
    """按页面算法变形密码, 用于 en_md5=1 的学校.

    逆向自 reference/a40.js:
        var PID='1'; var CALG='12345678';
        if(page.en_md5){ upass = calcMD5(PID+upass+CALG) + CALG + PID; }
        function calcMD5(str){ return binl2hex(coreMD5(str2binl(str))); }
        function str2binl(str){ ... blks[i>>2] |= (str.charCodeAt(i) & 0xFF) << ... }

    三个易错点:
      1. **不是裸 MD5** —— 明文前后各拼一次盐, 结果后面再接 `CALG+PID`;
      2. `str2binl` 对每个字符做 `charCodeAt(i) & 0xFF`, 即取 UTF-16 码元的
         **低字节**, 而非 UTF-8 字节序列。含中文的密码必须按此规则编码,
         否则摘要完全不同;
      3. 大小写: `binl2hex` 输出小写十六进制。
    当前学校 en_md5=0, 走不到这里; 该校日后开启时本函数即为正确实现。
    """
    import hashlib
    salted = MD5_PID + password + MD5_CALG
    # 复刻 JS 的 charCodeAt(i) & 0xFF: 逐字符取低字节
    raw = salted.encode("utf-16le", errors="surrogatepass")[::2]
    digest = hashlib.md5(raw).hexdigest()
    return digest + MD5_CALG + MD5_PID


# ---------------------------------------------------------------- 入口

def build_parser():
    p = argparse.ArgumentParser(description="梧州学院校园网自动登录工具 (Dr.COM 哆点)")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--server", help="认证服务器地址(仅本次运行生效, 不写盘)")
        sp.add_argument("--drcom-port", type=int, help="drcom 内核端口(默认80, 仅本次运行生效)")
        sp.add_argument("--eportal-port", type=int, help="eportal 端口(默认803, 仅本次运行生效)")

    sp_login = sub.add_parser("login", help="检查并登录(默认)")
    sp_status = sub.add_parser("status", help="查询在线状态")
    sp_logout = sub.add_parser("logout", help="注销下线")
    sp_watch = sub.add_parser("watch", help="守护模式, 掉线自动重连")
    sp_set = sub.add_parser("set", help="保存账号配置")
    sp_test = sub.add_parser("test", help="测试服务器连通性")
    sp_traffic = sub.add_parser("traffic", help="流量统计: 今日/本月/累计")

    # 六个子命令都支持临时覆盖连接参数; 其中 set 会写盘, 其余仅本次运行生效。
    for sp in (sp_login, sp_status, sp_logout, sp_watch, sp_set, sp_test):
        common(sp)

    sp_set.add_argument("-u", "--account", help="账号")
    sp_set.add_argument("-p", "--password", help="密码")
    sp_set.add_argument("--isp", choices=["0", "1", "2", "3", "4"],
                        help="服务类型: 0校园网 1中国移动 2中国电信 3中国联通 4中国广电")
    sp_set.add_argument("--terminal-type", help="终端类型(默认3, 抓包值)")
    sp_set.add_argument("--business-type", help="业务表单类型(默认3, 抓包值)")
    sp_set.add_argument("--interval", type=int, help="watch 模式检测间隔(秒)")

    sp_traffic.add_argument("--days", type=int, help="明细天数, 1-400(默认14)")
    sp_traffic.add_argument("--reset", action="store_true",
                            help="清空全部历史累计(下次采样重新建立基线)")
    sp_traffic.add_argument("--yes", action="store_true", help="与 --reset 搭配, 确认清空")
    sp_traffic.add_argument("--list", action="store_true",
                            help="只列出本机网卡累计收发(诊断), 不采样不写盘")
    return p


def apply_cli_overrides(cfg, args):
    """把 --server/--drcom-port/--eportal-port 应用到本次运行的配置副本.

    原先这三个选项只挂在子命令上、却仅在 cmd_set 里被读取, 其余子命令
    "接受但静默忽略" —— 用户执行 `status --server 10.0.0.1` 会拿到旧服务器的
    结果而毫无提示。这里统一生效(不写盘, 避免一次查询改动持久配置)。
    """
    changed = []
    for attr, key in (("server", "server"), ("drcom_port", "drcom_port"),
                      ("eportal_port", "eportal_port")):
        val = getattr(args, attr, None)
        if val is not None and val != "":
            cfg[key] = val
            changed.append("{}={}".format(key, val))
    if changed:
        log("本次运行临时覆盖配置: {}".format(" ".join(changed)))
    return normalize_config(cfg)


def main():
    if sys.platform == "win32":
        # 只保证不抛 UnicodeEncodeError, 不强行改编码:
        # 原实现把重定向输出从 gbk 改成 utf-8, 导致 `type log.txt` 在 936 代码页
        # 的 cmd 里全是乱码。errors="replace" 已足以避免崩溃。
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

    parser = build_parser()
    args = parser.parse_args()
    cfg = load_config()

    if args.cmd == "set":
        return cmd_set(cfg, args)
    if args.cmd == "traffic":
        return cmd_traffic(cfg, args)
    cfg = apply_cli_overrides(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "logout":
        return cmd_logout(cfg)
    if args.cmd == "watch":
        overrides = {key: cfg[key] for key in ("server", "drcom_port", "eportal_port")
                     if getattr(args, key, None) not in (None, "")}
        return cmd_watch(cfg, overrides=overrides)
    if args.cmd == "test":
        return cmd_test(cfg)
    return cmd_login(cfg)  # 默认 login


if __name__ == "__main__":
    sys.exit(main())
