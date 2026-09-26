# 门户协议参考（历史验证）

以下为原项目的本地验证记录，不保证服务器后续配置保持不变。

## 已验证的协议流程

| 步骤 | 接口 | 说明 |
|---|---|---|
| 1 | `http://192.168.0.203/drcom/chkstatus` | 查询在线状态，返回本机 IP、MAC、在线账号 |
| 2 | `http://192.168.0.203:803/eportal/portal/page/loadConfig` | 获取 `program_index`、`page_index`、`rcn`、`account_prefix` 等动态参数 |
| 3 | `http://192.168.0.203/drcom/login` | JSONP 登录，参数与浏览器抓包逐字段一致（见下） |
| 4 | `http://192.168.0.203/drcom/logout` | 注销下线（`program_index`/`page_index`） |

**关键协议规则（实测确认）**：
1. **`callback` 必须是第一个查询参数**——drcom 内核（80 端口 `/drcom/*`）只认首位的
   callback，放在其他位置直接返回 404（此前"注销 404"的根因）。eportal（803 端口）不受影响。
   页面 a41.js 的 `formatParams` 正是用 `arr.unshift` 把 callback 置于首位。
2. 登录请求参数（抓包值，程序已完全复刻）：`DDDDD`=账号、`upass`=密码（明文，`en_md5=0`）、
   `0MKKey=123456`、`R1=0`、`R2=`、`R3=1`、`R6=0`、`para=00`、`v6ip=`、`R7=0`、
   `user_account=",0,"+账号`、`user_password`=密码、`wlan_user_ip`=本机IP、`authex_enable=`、
   `wlan_user_mac`、`jsVersion=4.5.1`、`terminal_type=3`、`lang=zh-cn`、`enable_r3=1`、
   `mac_type=0`、`rcn`=loadConfig动态值、`operate=portal_login`、`business_type=3`。
3. 响应解读：`result:1` 成功；`result:0` 且 `msga="clientip online"` 表示 IP 已在线
   （内核拒绝重复登录，视为成功）；`msg=0/1` 无 msga 时为账号密码错误。
4. **登录请求的 `upass` 与页面对齐**：账号会先剔除所有空白字符；`en_md5=1` 时
   `upass = MD5(PID+密码+CALG) + CALG + PID`（`PID='1'`、`CALG='12345678'`，取自 a40.js），
   同时 `R2=1`；`en_md5=0`（本校当前）时两者均为原值、`R2` 为空。
   注意该 MD5 按 JS `charCodeAt(i) & 0xFF` 逐字符取低字节，**不是** UTF-8 字节序列。
5. **参数编码按 `encodeURIComponent`**：空格为 `%20`（不是 `+`），
   `! ' ( ) *` 保持原样。密码含这些字符时，用 `urlencode` 会与服务端预期不一致。

**服务类型（R3，登录页 ISP_select 实测映射）**：
`0`=校园网、`1`=中国移动、`2`=中国电信、`3`=中国联通、`4`=中国广电。

历史快照配置：无验证码（`enable_verify=0`）、`login_method=0`、`enable_r3=1`、`account_prefix=1`。

程序所有请求**强制直连**（忽略系统代理与环境变量代理）。

