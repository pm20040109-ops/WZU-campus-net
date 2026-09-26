#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园网流量统计: 本机网卡计数器累计 + 按日/月聚合 + 原子持久化.

数据来源与取舍
--------------
1. 内核接口 `/drcom/chkstatus` 虽然返回 `flow`(本次会话流量,KByte)与 `time`,
   但实测本校恒为 0;`live-` 快照中的 `olflow=4294967295` 是未初始化的哨兵值。
2. 门户 `/eportal/portal/online_list` 只描述**当前这一次**会话(上线时间、时长、
   上下行字节), 没有历史, 且本校 uplink/downlink 也是 0。
3. 因此**历史累计**只能由本机网卡计数器推算: `GetIfTable2` 返回 64 位
   InOctets/OutOctets, 单调递增、重启网卡才归零 —— 正适合做差累计。

统计口径
--------
- 只统计**通往认证服务器的那块网卡**(由 campus_network 判定), 不混入其他网络;
- 程序不在运行时不计流量(没有采样就没有数据);
- 采样间隔内的增量记到**采样时刻所在的自然日**, 因此跨零点的那个间隔
  会整段落入新的一天, 误差上界为一个采样间隔;
- 网卡索引变化(换 Wi-Fi/插拔网线)只重建基线, 不把新网卡的累计值当成流量。

本模块不依赖 campus_login / tkinter, 可单独导入与测试。
"""

import ctypes
import datetime
import json
import os
import sys
import tempfile
import threading
import time

STATS_VERSION = 1

# 与 campus_login.APP_DIR 相同口径: exe 模式取 exe 目录, 脚本模式取源码目录
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

STATS_PATH = os.path.join(APP_DIR, "campus_traffic.json")

# 历史保留天数(超出后只裁剪明细, 累计总量不受影响)
HISTORY_DAYS = 400

_COUNTER_ERRORS = (OSError, AttributeError, ValueError, ctypes.ArgumentError)


# ---------------------------------------------------------------- 展示格式

def format_bytes(n):
    """人类可读的字节数(1024 进制, 与浏览器/系统一致的 MByte 口径)."""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    for unit, step in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                       ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= step:
            digits = 2 if step >= 1024 ** 3 else 1
            return "{:.{d}f} {}".format(value / step, unit, d=digits)
    return "{:.0f} B".format(value)


def format_duration(seconds):
    """秒 -> 中文时长(用于服务端会话的 time_long)."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "-"
    if total < 0:
        return "-"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return "{} 天 {} 小时".format(days, hours)
    if hours:
        return "{} 小时 {} 分".format(hours, minutes)
    if minutes:
        return "{} 分 {} 秒".format(minutes, secs)
    return "{} 秒".format(secs)


# ---------------------------------------------------------------- 网卡计数器

class _MIB_IF_ROW2(ctypes.Structure):
    """Windows MIB_IF_ROW2(iphlpapi). 字段顺序/类型必须与 SDK 完全一致,
    否则 InOctets/OutOctets 会读到错位的垃圾值。"""
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("InterfaceGuid", ctypes.c_ubyte * 16),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", ctypes.c_uint32),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TunnelType", ctypes.c_uint32),
        ("MediaType", ctypes.c_uint32),
        ("PhysicalMediumType", ctypes.c_uint32),
        ("AccessType", ctypes.c_uint32),
        ("DirectionType", ctypes.c_uint32),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_uint32),
        ("AdminStatus", ctypes.c_uint32),
        ("MediaConnectState", ctypes.c_uint32),
        ("NetworkGuid", ctypes.c_ubyte * 16),
        ("ConnectionType", ctypes.c_uint32),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64),
        ("InUcastPkts", ctypes.c_uint64),
        ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64),
        ("InErrors", ctypes.c_uint64),
        ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64),
        ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64),
        ("OutOctets", ctypes.c_uint64),
        ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64),
        ("OutDiscards", ctypes.c_uint64),
        ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64),
        ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64),
        ("OutQLen", ctypes.c_uint64),
    ]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", ctypes.c_uint32),
                ("Table", _MIB_IF_ROW2 * 1)]


_iphlpapi = None


def _api():
    global _iphlpapi
    if _iphlpapi is None:
        lib = ctypes.WinDLL("iphlpapi", use_last_error=True)
        lib.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(_MIB_IF_TABLE2))]
        lib.GetIfTable2.restype = ctypes.c_uint32
        lib.FreeMibTable.argtypes = [ctypes.c_void_p]
        lib.FreeMibTable.restype = None
        _iphlpapi = lib
    return _iphlpapi


def _rows():
    """枚举所有接口行. 失败时抛 OSError(调用方决定是否降级)."""
    if sys.platform != "win32":
        raise OSError("流量统计仅支持 Windows")
    lib = _api()
    table = ctypes.POINTER(_MIB_IF_TABLE2)()
    code = lib.GetIfTable2(ctypes.byref(table))
    if code != 0 or not table:
        raise OSError("GetIfTable2 失败(错误码 {})".format(code))
    try:
        count = int(table.contents.NumEntries)
        if count <= 0:
            return []
        array = (_MIB_IF_ROW2 * count).from_address(
            ctypes.addressof(table.contents.Table))
        return [array[i] for i in range(count)]
    finally:
        lib.FreeMibTable(table)


def list_interfaces():
    """全部接口的累计收发字节, 供诊断(`traffic --list`)与自检使用."""
    try:
        rows = _rows()
    except _COUNTER_ERRORS:
        return []
    return [{"index": int(r.InterfaceIndex),
             "alias": str(r.Alias),
             "description": str(r.Description),
             "rx": int(r.InOctets),
             "tx": int(r.OutOctets),
             "connected": int(r.MediaConnectState) == 1,
             "up": int(r.OperStatus) == 1} for r in rows]


def read_counters(index):
    """读取指定接口的 (接收字节, 发送字节); 接口不存在/读取失败返回 None."""
    if not index:
        return None
    try:
        for r in _rows():
            if int(r.InterfaceIndex) == int(index):
                return int(r.InOctets), int(r.OutOctets)
    except _COUNTER_ERRORS:
        return None
    return None


# ---------------------------------------------------------------- 统计持久化

def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _empty_stats():
    return {"version": STATS_VERSION, "days": {}, "total": {"rx": 0, "tx": 0},
            "last": None}


def sanitize(stats):
    """把任意读入内容规整为可用结构(配置文件被手改坏时不崩)."""
    if not isinstance(stats, dict):
        return _empty_stats()
    clean = _empty_stats()
    days = stats.get("days")
    if isinstance(days, dict):
        for key, bucket in days.items():
            if not isinstance(bucket, dict) or not isinstance(key, str):
                continue
            rx, tx = _as_int(bucket.get("rx"), -1), _as_int(bucket.get("tx"), -1)
            if rx < 0 or tx < 0:
                continue
            clean["days"][key] = {"rx": rx, "tx": tx}
    total = stats.get("total")
    if isinstance(total, dict):
        clean["total"] = {"rx": max(0, _as_int(total.get("rx"))),
                          "tx": max(0, _as_int(total.get("tx")))}
    last = stats.get("last")
    if isinstance(last, dict):
        index = _as_int(last.get("index"), 0)
        rx, tx = _as_int(last.get("rx"), -1), _as_int(last.get("tx"), -1)
        if index > 0 and rx >= 0 and tx >= 0:
            clean["last"] = {"index": index, "rx": rx, "tx": tx,
                             "ts": float(last.get("ts") or 0)}
    return clean


def load_stats(path=None):
    """读取统计文件; 不存在或损坏时返回空统计(不抛异常)."""
    target = path or STATS_PATH
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            return sanitize(json.load(f))
    except FileNotFoundError:
        return _empty_stats()
    except (ValueError, OSError, UnicodeError) as e:
        # 不静默: 统计文件被写坏时用户必须知道"历史累计为什么没了"
        sys.stderr.write("[警告] 流量统计文件读取失败, 已从零开始: {}\n".format(e))
        return _empty_stats()


def save_stats(stats, path=None):
    """原子写入: 先写同目录临时文件再 os.replace, 断电不会留下半截 JSON."""
    target = path or STATS_PATH
    directory = os.path.dirname(target) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".campus_traffic.", suffix=".tmp",
                               dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sanitize(stats), f, ensure_ascii=False, indent=1,
                      sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _day_key(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def record_sample(stats, index, rx, tx, now=None):
    """把一次采样并入统计, 返回本次增量 (drx, dtx).

    基线建立、网卡切换、计数器回绕(网卡重置)三种情况都只更新基线、不计增量,
    因为此时 `当前值 - 上次值` 并不是"这段时间的流量"。
    """
    ts = time.time() if now is None else float(now)
    last = stats.get("last")
    drx = dtx = 0
    if isinstance(last, dict) and _as_int(last.get("index"), 0) == int(index):
        prev_rx, prev_tx = _as_int(last.get("rx"), -1), _as_int(last.get("tx"), -1)
        if prev_rx >= 0 and prev_tx >= 0 and rx >= prev_rx and tx >= prev_tx:
            drx, dtx = rx - prev_rx, tx - prev_tx
    stats["last"] = {"index": int(index), "rx": int(rx), "tx": int(tx),
                     "ts": round(ts, 3)}
    if drx or dtx:
        day = _day_key(ts)
        days = stats.setdefault("days", {})
        bucket = days.get(day)
        if not isinstance(bucket, dict):
            bucket = {"rx": 0, "tx": 0}
            days[day] = bucket
        bucket["rx"] = _as_int(bucket.get("rx")) + drx
        bucket["tx"] = _as_int(bucket.get("tx")) + dtx
        total = stats.setdefault("total", {"rx": 0, "tx": 0})
        total["rx"] = _as_int(total.get("rx")) + drx
        total["tx"] = _as_int(total.get("tx")) + dtx
        if len(days) > HISTORY_DAYS:
            for old in sorted(days)[:-HISTORY_DAYS]:
                days.pop(old, None)
    return drx, dtx


def _bucket(rx, tx):
    return {"rx": int(rx), "tx": int(tx), "sum": int(rx) + int(tx)}


def summary(stats, now=None, days=14):
    """汇总为可直接展示/序列化的普通字典."""
    ts = time.time() if now is None else float(now)
    moment = datetime.datetime.fromtimestamp(ts)
    today_key = moment.strftime("%Y-%m-%d")
    month_key = moment.strftime("%Y-%m")
    day_map = stats.get("days") if isinstance(stats.get("days"), dict) else {}
    today = day_map.get(today_key) or {}
    month_rx = month_tx = 0
    for key, bucket in day_map.items():
        if isinstance(bucket, dict) and key.startswith(month_key):
            month_rx += _as_int(bucket.get("rx"))
            month_tx += _as_int(bucket.get("tx"))
    total = stats.get("total") if isinstance(stats.get("total"), dict) else {}
    history = []
    for key in sorted(day_map)[-max(1, int(days)):]:
        bucket = day_map[key]
        if isinstance(bucket, dict):
            history.append(dict(_bucket(_as_int(bucket.get("rx")),
                                        _as_int(bucket.get("tx"))), date=key))
    return {
        "today": dict(_bucket(_as_int(today.get("rx")), _as_int(today.get("tx"))),
                      date=today_key),
        "month": dict(_bucket(month_rx, month_tx), month=month_key),
        "total": _bucket(_as_int(total.get("rx")), _as_int(total.get("tx"))),
        "days": history,
        "last": stats.get("last"),
        "path": STATS_PATH,
    }


def history_rows(data, limit=14):
    """把 summary() 的结果排成表格行(最新日期在前), 供 GUI 的 Treeview 使用."""
    rows = []
    for row in reversed(list(data.get("days") or [])[-max(1, int(limit)):]):
        rows.append((row["date"], format_bytes(row["rx"]),
                     format_bytes(row["tx"]), format_bytes(row["sum"])))
    return rows


class TrafficMeter:
    """采样 + 累计 + 落盘. 线程安全(采样在线程里跑, 展示在 UI 线程).

    落盘策略: 基线**必须**在建立/切换时立刻写盘, 否则一次性运行的
    `traffic` 子命令每次都只采样一次、每次都从头建立基线, 永远统计不到增量。
    基线之外的增量按 flush_interval 批量写(守护常驻时避免每 5 秒一次磁盘写)。
    """

    def __init__(self, path=None, flush_interval=300.0):
        self.path = path or STATS_PATH
        self.flush_interval = float(flush_interval)
        self._lock = threading.RLock()
        self.stats = load_stats(self.path)
        self._dirty = False
        self._last_flush = time.monotonic()
        self.last_error = ""

    def _write_locked(self):
        try:
            save_stats(self.stats, self.path)
        except OSError as e:
            # 磁盘满/目录只读等: 只记录, 不打断采样(下次 flush 会重试)
            self.last_error = str(e)
            return False
        self.last_error = ""
        self._dirty = False
        self._last_flush = time.monotonic()
        return True

    def _flush_locked(self, force):
        if not force and (not self._dirty or
                          time.monotonic() - self._last_flush < self.flush_interval):
            return False
        return self._write_locked()

    def sample(self, index, now=None):
        """读一次计数器并入账. 返回 (drx, dtx); 读不到返回 None."""
        counters = read_counters(index)
        if counters is None:
            return None
        with self._lock:
            before = self.stats.get("last")
            delta = record_sample(self.stats, index, counters[0], counters[1], now)
            self._dirty = True
            # 首次建立基线或网卡变化: 立刻落盘, 保证下次运行能接上
            if not isinstance(before, dict) or _as_int(before.get("index"), 0) != int(index):
                self._write_locked()
            return delta

    def flush(self, force=False):
        """按需落盘. 返回是否真的写了盘(写失败时 last_error 有原因)."""
        with self._lock:
            return self._flush_locked(force)

    def note(self, index, rx, tx, now=None):
        """直接记账(测试/离线导入用), 不读网卡."""
        with self._lock:
            delta = record_sample(self.stats, index, rx, tx, now)
            self._dirty = True
            return delta

    def summary(self, now=None, days=14):
        with self._lock:
            return summary(self.stats, now=now, days=days)

    def reset(self):
        with self._lock:
            self.stats = _empty_stats()
            self._dirty = True
            return self._write_locked()
