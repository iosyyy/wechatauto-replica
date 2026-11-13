# Linux 读取后端

本页说明 `iosyyy/wechatauto-replica` 维护分支。上游 README 的 Windows GUI
功能与实测记录不能当作本分支的 Linux 验收结果。

## 当前范围

第一阶段提供不依赖 Windows 组件的数据库读取接口：账号发现、已提供密钥下的
SQLCipher 解密、群消息分库查询、发送者映射和增量监听。

**当前阶段不提供 Linux 自动取钥或 GUI 发消息。** 密钥必须来自你自己账号的
已验证本地来源。Linux 微信客户端的版本、数据库结构和真实消息仍需单独联调。
使用合成数据库通过测试，不能证明某个 Linux 微信版本已兼容。

后续发送仍需官方 Linux 微信常驻并登录，以及可操作的图形会话；这不是无客户端
的协议服务。Windows 后端保留，Linux 不使用其进程内存偏移或自动切换账号逻辑。

## 安装维护分支

```bash
git clone https://github.com/iosyyy/wechatauto-replica.git
cd wechatauto-replica
git switch codex/linux-native-backend
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Linux 安装不包含 Windows UIAutomation、pywin32、WinRT OCR 或 Windows GUI
媒体依赖。无需先安装或启动微信窗口即可读取已具备合法密钥的数据库副本。

## 数据与密钥

官方 Linux 客户端常见账号目录为：

```text
~/Documents/xwechat_files/<account>/db_storage/
~/文档/xwechat_files/<account>/db_storage/
```

传入的 `db_dir` 是包含账号子目录的 `xwechat_files`，`account` 是该账号的
完整目录名，不能用群名或昵称代替。多账号时必须显式指定账号。

`keys_file` 是私有 JSON 文件，键为 `db_storage` 内的相对文件路径，例如
`contact/contact.db`、`message/message_0.db`；值为该库的 64 位十六进制裸密钥。
兼容上游的 96 位密钥与 salt 形式。密钥必须通过目标库 HMAC 校验才会使用。
文件应为当前用户所有、权限 `0600`，所在私有目录为 `0700`。

不要把真实密钥写进本仓库、命令行参数、日志或聊天窗口。显式输入文件与读取源
数据库都不应被工具修改。缓存和解密副本放在另一处私有工作目录。
`workdir` 是缓存父目录，实际缓存位于其按数据根目录与账号计算的
`reader-<sha256>` 私有子目录。默认父目录为
`$XDG_CACHE_HOME/wechatauto/linux` 或 `~/.cache/wechatauto/linux`。

## 读取群消息

以下路径和账号为占位说明，需替换为自己的私有路径；不要填写真实密钥到代码中。

```python
from pathlib import Path
from wechatauto import WeChatDB

private = Path.home() / ".local/share/wechatauto/private"
db = WeChatDB(
    db_dir=str(Path.home() / "Documents/xwechat_files"),
    account="wxid_example_abcd",
    keys_file=str(private / "source-keys.json"),
    workdir=str(private / "reader-cache"),
)

for message in db.get_messages("example@chatroom", limit=20):
    # 消费方按自己的隐私策略处理正文；不要直接上传所有会话。
    print(message["create_time"], message["sender_username"], message["content"])
```

`local_id` 只在所在分库内唯一，事件身份还需包含账号、会话和 `shard`。

## 增量监听与持久桥接边界

真实监听类型是 `wechatauto.db.Listener`；顶层 `wechatauto.Listener` 仍保留
上游的 Windows 兼容类型，避免悄悄更换旧 API。

集成方创建 `Listener(db)`，用 `add_listener(chat_username, callback)` 注册回调，
再调用 `start()`；服务退出时调用 `stop()` 保存水位。回调签名为
`callback(message, listener)`，必须持久化成功才返回，不能只启动异步 HTTP。
Linux 仅允许默认的 `db.workdir/listener_watermark.json` 水位文件；传空字符串
可以关闭水位持久化。不允许把水位重定向到密钥文件或任意外部路径。

新注册会话默认从当前最新消息之后开始，历史总结使用 `get_messages` 或
`export_history` 单独读取。监听器支持水位恢复，但上游回调重试耗尽会推进水位；
因此还不能直接把 HTTP 转发回调当作可靠 LangBot 桥接。后续需要持久收件箱，
持久化失败则暂停源读取，发送结果未确认时不能盲目重发。

## 离线验证

```bash
python -m pip install packaging
python -m unittest discover -s tests -p test_platform_imports.py -v
python -m unittest discover -s tests -p test_package_metadata.py -v
python -m unittest discover -s tests -p 'test_linux*.py' -v
```

测试使用本地构造并加密的 SQLite 文件；不连接微信、不操作窗口、不发送消息。
Linux 持续集成验证读取基础，Windows 持续集成验证上游纯逻辑；它们都不能替代
真实客户端的取钥、GUI 发送和最终微信送达验收。
