"""Local Windows network identity checks. No packets or credentials are sent."""

import ctypes
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkState:
    ready: bool
    message: str
    source_ip: str = ""
    interface_index: int = 0
    profile: str = ""


def _best_route(server):
    # MIB_IPFORWARDROW has fourteen DWORD fields (also on 64-bit Windows).
    class Route(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in (
            "dest", "mask", "policy", "next_hop", "interface_index",
            "type", "proto", "age", "next_hop_as", "metric1", "metric2",
            "metric3", "metric4", "metric5")]

    api = ctypes.WinDLL("iphlpapi").GetBestRoute
    api.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(Route)]
    api.restype = ctypes.c_uint32
    route = Route()
    destination = struct.unpack("=I", socket.inet_aton(server))[0]
    if api(destination, 0, ctypes.byref(route)) != 0:
        raise OSError("尚无可用 IPv4 路由")
    # UDP connect only selects a local source address; it sends no datagram.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((server, 80))
        source = sock.getsockname()[0]
    return route.interface_index, source


def _connection_profiles():
    # Network-profile metadata is available without requesting WLAN location
    # access. No SSID-query permission or elevation is required.
    script = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "$items=@(Get-NetConnectionProfile | "
        "Select-Object Name,InterfaceIndex,IPv4Connectivity); "
        "ConvertTo-Json -InputObject $items -Compress"
    )
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, encoding="utf-8-sig", errors="replace", timeout=8,
        creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    data = json.loads(result.stdout)
    return data if isinstance(data, list) else [data]


def inspect_network(cfg):
    """Fail closed unless the target route uses an allowed, connected profile."""
    if sys.platform != "win32":
        return NetworkState(False, "当前系统不支持校园网识别，已暂停认证")
    allowed = cfg.get("campus_networks", ["WZXY-Student"])
    if not isinstance(allowed, list) or not allowed or any(
            not isinstance(name, str) or not name.strip() for name in allowed):
        return NetworkState(False, "请配置允许登录的校园网络名称")
    try:
        server = str(ipaddress.IPv4Address(cfg["server"]))
        interface, source = _best_route(server)
        profiles = _connection_profiles()
        # Check again after the profile query, which can overlap a Wi-Fi switch.
        if (interface, source) != _best_route(server):
            return NetworkState(False, "网络正在切换，等待校园网连接稳定")
        profile = next((p for p in profiles if isinstance(p, dict)
                        and p.get("InterfaceIndex") == interface), None)
        if profile is None or profile.get("IPv4Connectivity") in (None, 0, "Disconnected"):
            return NetworkState(False, "等待 Wi-Fi 或校园网连接就绪")
        name = profile.get("Name", "")
        if name not in allowed:
            return NetworkState(False, "当前不是指定校园网，已暂停认证")
        address = ipaddress.IPv4Address(source)
        if address.is_unspecified or address.is_loopback or address.is_link_local:
            return NetworkState(False, "等待校园网分配 IPv4 地址")
        return NetworkState(True, "已连接校园网", source, interface, name)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return NetworkState(False, "网络尚未就绪或无法确认校园网，已暂停认证")
