# 开发与架构

开发目标：Windows 桌面、Python 3.11–3.13，包含 Tkinter。实际验证版本见审查报告；其他版本以 CI 结果为准。

| 模块 | 职责 |
|---|---|
| campus_login.py | 配置、门户协议、认证、CLI |
| campus_network.py | Windows 网络、路由和源地址检查 |
| campus_traffic.py | 网卡计数、持久化和汇总 |
| campus_gui.py | 生命周期、后台任务、队列和保存 |
| campus_ui.py | 配色、字体、控件、布局和焦点滚动 |
| campus_demo.py | 在创建窗口前隔离配置、网络、注册表和统计 |
| campus_selftest.py | 源码与独立程序离线自检 |
| tray_icon.py | Windows 原生托盘 |

后台线程用队列通知主线程，不得直接操作 Tk；呈现模块不包含网络和持久化。

## 环境与验证

项目目录下，PowerShell：

~~~powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
.venv/Scripts/python.exe -m unittest discover -s tests -v
$env:CAMPUS_GUI_TESTS = "1"
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe campus_gui.py --demo
git diff --check
~~~

GUI 测试需要桌面，默认跳过，显式启用才运行；业务测试使用桩和本机 loopback 服务。

## 构建

~~~powershell
./build.bat
./build.bat --distpath .artifacts/dist --workpath .artifacts/build
./.artifacts/dist/CampusNet.exe --self-test .artifacts/self-test.json
~~~

解释器选择：CAMPUS_PYTHON → 项目 .venv → PATH 中 python。构建前执行测试，支持独立输出目录。

## 界面验证

用 --demo 检查五页，在 100%、150%、200% 和最小窗口下验证文字、勾选、滚动可达、Tab/Enter/Space、Alt+1…5、Ctrl+S。演示设置仅在当前进程有效。

字体根据本机安装情况 fallback。DPI 在启动时确定；跨不同 DPI 显示器移动后建议重启，动态迁移不在本次实现内。
