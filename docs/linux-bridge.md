# 官方 Linux 微信与 LangBot 原生文本桥接

此路线保留官方 Linux 微信进程和图形会话，通过本地 SQLCipher 快照读取，
通过明确授权的 agent-desktop 界面操作发送。它提供 WeChatPad 兼容接口，
无需运行 WeChatPad 协议服务。现有 LangBot 模型、人格和插件仍由原 Pipeline 管理。

## 组件与范围

```text
官方 Linux 微信 → 私有数据库快照 → 持久 inbox → WebSocket
                                                 ↓
                                       LangBot Pipeline / DeepSeek
                                                 ↓
官方微信 ← agent-desktop + 数据库回执 ← 持久 outbox ← WeChatPad HTTP API
```

原生桥接只开放配置允许的 username。默认只有 `filehelper`，其本人消息仅将
`/lb ` 开头的文本作为请求，转发时去掉前缀。普通本人消息和桥接自身回复不会形成回环。
首次启动只建立分片水位，不把已有历史一次性发给模型。

当前发送接口验证文本；图片、语音、文件和 @mention 仍未完成原生发送验收。
不能把 API 路径存在当作这些能力可用。群历史可通过有界内部读取接口查询，
没有公开的“读取所有聊天记录”HTTP 端点。

## 安装与登录

1. 按 [私人登录桌面](../examples/linux-desktop/README.md) 运行官方客户端。
2. Linux 安装 `pip install '.[linux-bridge]'`；桌面安装 AT-SPI、共享 D-Bus 会话和
   经过核对的 agent-desktop CLI/helper，详见 [发送说明](linux-sender-agent-desktop.md)。
3. 由本人扫码并确认登录。在同一显示会话观察实际控件，再填写布局。
4. 使用本人账号的已验证私有密钥文件。可选取钥观察器仅接受已固定映像的现有进程；
   目前固定官方 x86_64 微信 4.1.13.26：
   `2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132`。

取钥命令入口是 `python -m wechatauto.platforms.linux_capture --help`。
它不重启或自动登录客户端；在有界附加观察期间，由已授权 UI 触发本人登录 KDF，
候选经数据库 HMAC 校验后仅保存裸数据库密钥。观察器失败必须清理并分离，
不写目标代码、不给微信容器添加 ptrace 权限、不保存完整进程内存。

## 私有配置

配置、密钥文件为运行 UID 所有的普通 `0600` 文件；状态目录 `0700`。
真实 token 应在服务器生成并写入私有 JSON，不填进仓库、聊天或命令行。
下面仅是布局示例；路径、账号和 token 为占位符。

```json
{
  "db_dir": "/home/wechat/Documents/xwechat_files",
  "account": "wxid_example_abcd",
  "keys_file": "/home/wechat/.config/wechatauto-bridge/keys.json",
  "state_dir": "/home/wechat/.local/state/wechatauto-bridge",
  "token": "GENERATE_A_PRIVATE_RANDOM_TOKEN",
  "expected_exe_sha256": "2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132",
  "listen_host": "0.0.0.0",
  "port": 5080,
  "allowed_chats": ["filehelper"],
  "write_chats": ["filehelper"],
  "request_prefix": "/lb ",
  "allow_physical": true,
  "bind_current_window": true,
  "desktop_timeout": 20,
  "desktop_layout": {
    "app": "wechat",
    "input_mode": "clipboard_coordinates",
    "composer_input_mode": "set_value",
    "activation_mode": "focused_return",
    "header_aliases": {"filehelper": "File Transfer"},
    "search": {"role": "textfield", "name": "Search"},
    "search_result": {"role": "option", "name": "{title}"},
    "header": {"role": "statictext", "name": "{title}", "required_action": "SetFocus"},
    "composer": {"role": "textfield", "name": "File Transfer"},
    "send": {"role": "button", "name": "Send"},
    "chat_row": {"role": "option", "name_first_line": "{header_title}"}
  }
}
```

`bind_current_window` 默认关闭。显式打开时，仍须固定进程映像、唯一账号目录、
活动数据库 FD、当前 HMAC、自身联系人和可见主窗，全部复核后才绑定窗口。
该开关不会自动更换账号。物理输入仅用于事先授权的服务器独立微信窗口。

`composer_input_mode` 默认 `clipboard`。示例显式启用已验证的 `set_value`：
正文通过唯一 ref 的语义接口写入一次，写前实时读取空值、写后读取新界面并核对全文。
搜索仍使用剪贴板；失败不自动换输入方式。同一目标会话已完整就绪时不再重复搜索。

## 运行和 LangBot 配置

直接入口：

```bash
/usr/bin/python3 -m wechatauto.bridge.native --config /private/config.json
```

Docker 私人桌面可采用 [systemd 示例](../examples/linux-desktop/wechatauto-native-bridge.service)。
它只托管桥接，复用微信当前 D-Bus/显示会话，不重启微信；停止通过 pidfd 定位本桥接，
不会对微信进程发送信号。示例路径按实际安装位置调整。
不同 Python 安装的依赖可能不同，桌面示例中的 apt 依赖对应 `/usr/bin/python3`。

桥接 API 只接 LangBot 私有网络；不需要新增公网监听端口。LangBot 选择 WeChatPad：

- `wechatpad_url`：`http://<bridge-container>:5080`
- `wechatpad_ws`：`ws://<bridge-container>:5080/ws`
- `token`：私有配置中的 token
- `wxid`：当前已验证本人 username
- 事件绑定：现有 DeepSeek Pipeline

普通 WeChatPad adapter 的 callback 入队完成并不代表处理完成。
本分支配套 LangBot 修复让带 `_bridge_event_id` 的消息等待实际 Pipeline 完成和发送回执后 ACK；
聚合、steering、过滤、取消和无完成回执的路由不能伪装成功。
当前 native 完成回执仅支持 Pipeline 事件路由，Agent/event_processor 路由明确拒绝。

## 恢复与验收

inbox 与水位在同一事务持久化。WebSocket 重连重放未 ACK 事件，LangBot 合并同一
in-flight 事件；发送身份由事件和回复序号确定，重新生成正文不会绕过去重。
结果不明或相同身份内容冲突会保留 `uncertain`/409，需先核对真实数据库，不能换 ID 盲目重发。
这不承诺模型只运行一次或外部微信严格 exactly-once。

验收时仅向文件传输助手发送合成文本，分别检查：API 回执、本人数据库中准确正文和
正数 server_id、LangBot 实际 Pipeline 完成、inbox ACK、相同 ClientMsgID 不重复发送。
真实记录不应进入测试夹具、日志或公开 PR。当前阶段实测记录见 [验证记录](linux-verification.md)。
