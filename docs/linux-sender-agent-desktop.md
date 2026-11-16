# Linux 原生发送与 agent-desktop

本模块只实现实际观察后配置的 UI 操作，不使用猜测坐标、昵称首项匹配、
二进制修改或协议替代。没有真实布局配置时不能发送。

## 已核实的工具支持

核实日期：2026-11-13；本机官方 npm 包 `agent-desktop@1.2.0`。

- [官方安装说明](https://agent-browser.dev/desktop/installation)：
  `npm install -g agent-desktop@1.2.0` 包含 Linux glibc x64/arm64 二进制与剪贴板 helper。
- [官方 Linux 指南](https://agent-browser.dev/desktop/platforms/linux)：
  AT-SPI over D-Bus；文档示例运行于 Xvfb + Openbox。要求同一个图形会话、
  `at-spi2-core`、窗口管理器、glibc >= 2.35；推荐 Ubuntu >= 24.04。
  Qt 启动环境需要 `QT_ACCESSIBILITY=1`；X11 键盘事件需要 XTEST。
- [官方容器指南](https://agent-browser.dev/desktop/guides/docker-container)：
  安装示例另外包含 `dbus x11-utils libatk-bridge2.0-0`。
  CLI 与微信必须共享 UID、DISPLAY、XAUTHORITY、DBUS_SESSION_BUS_ADDRESS 和运行目录。
- [官方剪贴板说明](https://agent-browser.dev/desktop/commands/clipboard)：
  `clipboard-set --image /absolute/image.png` 支持 <=64 MiB 的普通 PNG 文件；
  `--file-url` 支持文件引用。X11 helper 必须和 CLI 同目录并命名为
  `agent-desktop-linux-clipboard-owner`。Linux 写入结果本身未确认，需另行读回。

官方支持不代表当前微信已经公开无障碍控件。必须先在相同会话执行 `status`、
`skills get desktop`、`skills get platform`、`list-apps`，再读取完整树验证控件。
日志、snapshot、剪贴板和截图可能含有私密内容，应保留在权限为 0700 的私人目录。
不得把私聊历史当作公开调试材料。

## 调用接口与边界

```python
driver = AgentDesktopDriver(observed_layout)
sender = LinuxSender(db, driver, account_guard=verify_active_gui_account)
receipt = sender.send_text(username, text, ats=None, client_id=client_id)
```

`DesktopLayout` 的 app/search/search_result/header/composer/send 都须由实际观察配置。
`Selector` 只接受精确 role/name/native_id/description，name 可以包含 `{title}`。
测试中的 `fixture-*` 控件仅为合成夹具，不是微信控件名称。
带 `hidden` 状态的节点及其全部后代不会成为匹配候选。

`Selector.required_action` 可要求 `available_actions` 中存在指定动作，用于区分
同名、同 bounds 的嵌套节点。可选 `name_first_line` 逐字匹配 name 的首行，忽略后续预览；
不修剪空白，不做子串匹配。它用于显式 `chat_row`，其中 `{header_title}` 渲染为实际标题。
`header_aliases` 只接受已观察的 `{"filehelper": "File Transfer"}` 或空配置；搜索仍用
读取器中的中文 title。发送前还要确认该英文别名未属于其他联系人，缺失别名碰撞校验器也拒绝。

默认 `allow_physical=False`。只有用户明确允许的隔离窗口才可传
`AgentDesktopDriver(observed_layout, allow_physical=True)`；这时必须配置观察得到的
`window_id`，clear/type/click 仅针对已验证的 ref 增加 `--headed`，不发送原始坐标或
未限定窗口的键盘事件。拒绝后不会自动切换模式。该模式只清理搜索框，不清理聊天草稿；
正文通过一次 `type --stdin` 写入，上限为 10000 UTF-8 字节。
输入框空值和正文均须经 `get <ref> --property value` 返回精确可读字符串确认，
snapshot 中缺失 value 不视为空。所有成功改变 UI 的动作后都读取新 snapshot；
发送点击后的观察失败属于结果不明，不能标为可安全重投。

`DesktopLayout.activation_mode` 可显式选择 `semantic`（默认，沿用 ref click）或
`focused_return`，以及下述 `observed_coordinates`。`focused_return` 要求物理输入授权和固定窗口，针对已验证目标依次执行
`focus <ref> --headed`、新 snapshot、`is <fresh-ref> --property focused`，随后只执行一次
`press return --app <app> --window-id <id> --headed`。搜索结果选择和发送都使用该配置，
不因 click 失败自动切换路线。发送时 focus 并执行本人账号校验，回调返回后最后一次检查
标题、正文和焦点，然后才投递 Return；提交后只观察和回读，不再次激活。

上游 v1.2.0 的 [X11 text planner 测试](https://github.com/vercel-labs/agent-desktop/blob/v1.2.0/crates/linux/src/input/keyboard/text_plan_tests.rs)
表明物理 type 会在投递前拒绝换行、Tab 和 CR 等控制字符。当前物理输入路径因此只适合
不含这些字符的文本；真实编辑器如果提供可用的 SetValue，应单独验证文字插入方式后再配置，
不能把导航键拼进正文输入来绕过限制。

## 已观察坐标与剪贴板路线

`input_mode="clipboard_coordinates"` 是独立的显式选项，默认仍为 `"ref"`。
它必须同时有物理输入授权和已绑定窗口 ID。每次坐标操作前读取完整的新 snapshot，
重新确认主窗的 PID/process_instance、可见性、焦点和 bounds；控件须为可见唯一匹配，
其有限正数 bounds 完全落在所属窗口内。点击点只取这次实际 bounds 的中心，不保存比例或像素常量。
`activation_mode="observed_coordinates"` 用同一检查执行一次发送点击。

文本通过 `clipboard-set --stdin` 一次写入，并经 `clipboard-get --format text` 精确回读。
选中搜索框后，Ctrl+A/Ctrl+V 都只发往固定主窗；正文仅在已验证空的编辑器中粘贴一次，
粘贴前再次确认剪贴板，粘贴后从新 snapshot 读取字段值逐字比对。因此该路线可保留多行文本。
若 snapshot 省略空 value，必须由精确 ref 的 `get(value)` 返回空字符串才能放行，
不能把省略视为空值。ref 有隐藏同身份节点而被 CLI 拒绝时也不放行。

搜索结果只在可见、有正数 bounds、title 精确为空、属于主窗同 PID/process_instance 的
浮层中查找。只有一个浮层包含唯一精确结果时才可点击；其他 PID/标题不成为候选，多个匹配拒绝。
可选 `chat_row` 只用于结果浮层已经关闭之后的主窗：逐字匹配标题首行、要求唯一可见，
重新读取 bounds 点击，再验证会话 header。浮层点击没有打开目标 header 且未配置 chat_row 时直接拒绝。
浮层只点击一次；随后若该已知窗口 ID 仍可见，只执行一次有界 `window-closed` 事件等待，
最多 20 秒，以当前只读窗口清单确认最终关闭状态。等待基线竞态可由已消失的窗口清单确认，
不会再点浮层。关闭途中多浮层、所属进程变化或超时仍拒绝。关闭确认与主窗焦点分开判断：
确认关闭后允许显式 `focus-window --window-id ... --headed` 恢复已绑定主窗，
再读取完整新视图、匹配唯一 chat_row 和 header；不能把主窗失焦误报成浮层还在。
主窗激活每次都执行该明确命令，不用可能滞后的 inventory `is_focused=true` 跳过 OS 激活。
chat_row 仅点击一次，随后一次有界只读 snapshot wait 优先等待已配置 composer 的 role/name，
缺少该 name 时用 header 提示；等待最多 20 秒。等待返回仍须验证精确 header/composer/send
及真实空值。完整树不等于点击事件已被处理；等待超时直接拒绝，不再点击行或写入正文。

同会话快路径只使用主窗激活后的这次完整新视图：目标 header 必须唯一、无未关闭浮层、
composer/send 必须精确且唯一，编辑器须经实际值读取确认为空，才直接粘贴正文一次。
目标 header 为零时才运行原搜索/浮层/聊天行导航；header 多个、其他控件歧义或缺失、
值不明及已有草稿都直接拒绝，不改走搜索来掩盖问题。username/别名碰撞及本人账号校验仍在 UI 操作之前。
合成调用计数中，快路径仅有一次正文 clipboard-set、一次 Ctrl+V、一次 composer 点击；
Search/popup/chat_row 点击、Ctrl+A、正文以外剪贴板写入均为零。导航与快路径的真实验收须分别记录。
导航中也复用同一个严格判定：搜索浮层关闭、主窗明确激活后的新视图如果已经有唯一目标
header，就验证完整 composer/send 和真实空值并直接写入，不能再次点击已经打开的聊天行。
只有此时目标 header 为零，才点击配置的 chat_row 一次并等待。目标 header 多个、控件不全、
值不明或已有草稿直接拒绝，不能借聊天行重点击掩盖歧义。

## 显式选择正文 SetValue

`composer_input_mode` 默认 `"clipboard"`，可显式配置为 `"set_value"`，后者只允许与
`input_mode="clipboard_coordinates"` 一起使用。Search 保留原有坐标/剪贴板及隐藏同身份节点保护；
物理输入授权和固定窗口约束不变。正文先验证目标 header、唯一 composer/send，再强制通过
`get(value)` 读取真实空字符串，随后只执行一次不带 `--headed` 的 `set-value <ref> --stdin`。
写入后重新读取完整视图并强制 `get(value)` 逐字比对，多行和尾部换行都保留。
任何拒绝、超时、值不明或不符均停止，不自动改用剪贴板、键盘或再次写入。

两种正文填充路径都在返回前验证完整 header/controls/body，因此已移除 prepare 尾部的重复完整读取。
同会话合成准备计数中，SetValue 路径只有 1 次 set-value、2 次 get、2 次完整 snapshot；
无正文剪贴板写入、点击或快捷键。默认剪贴板准备的完整 snapshot 从 5 次减为 4 次。
全部独立账号校验次数和 submit 在完整 guard 之后的最终新视图检查均保留；实际耗时须另行测量。

本次实际客户端中，`filehelper` 的 DB title/搜索结果是中文，主窗标题及 Chats 首行是英文。
据已观察控件可显式配置 header 的 `required_action="SetFocus"`，chat_row 的
`name_first_line="{header_title}"` 和 `required_action="Select"`；具体 app、窗口和其他 selectors
仍由运行层的实际观察提供，不在代码中自动猜测。

`account_guard(expected_username) -> bool` 是必需的独立 GUI 账号校验器；只有精确返回
`True` 才放行。读取器中的本人 username 只是期望值，不能充当 GUI 身份证据。
每次发送在 GUI 准备之前、准备完成即将提交之前各校验一次；缺失校验器、异常、
账号不符或读取器身份变化都会拒绝发送。运行层须以已固定的 GUI PID/映像、唯一账号目录、
当前页面 HMAC 与活动 db_storage 文件句柄命名空间等当前证据绑定本人账号。
测试不用真实读取器时可显式注入 `own_username`；有读取器时仍以其当前身份为准，
显式值如果存在则必须与其相同。
驱动的 `submit_text(target, text, before_submit=...)` 再次执行同一个账号校验回调。
回调可能耗时，返回后必须重新完成最终 snapshot/get、标题、正文、焦点和 bounds 验证，
不能沿用回调前的正文或坐标。坐标视图同时重新绑定主窗 PID/process_instance。

username 先精确查联系人，再检查界面显示名是否与任何其他联系人昵称、备注或
username 碰撞。搜索结果与会话标题都必须逐字等于该显示名。输入框须可读且为空；
正文通过 stdin 一次写入，确认逐字一致后用新 snapshot 的发送 ref 只提交一次。

发送前对所有已知分片读取目标表的 `MAX(local_id)`。发送后只接受该水位之后、
该分片 `Name2Id` 映射到本人 username、类型为文本、正文逐字相等且有正数 server_id 的行。
新出现且发送前没有观察过的分片不作为成功证据；新会话可在已观察的分片上确认。

返回 `status=confirmed` 表示数据库回读确认，携带 local_id/server_id/shard，
以及桥接层使用的 `confirmed=true` 和 `msg_id`。
`status=uncertain` 表示提交可能已经执行而数据库未确认，调用方必须保留 ClientMsgID
并阻止自动重投；本模块只序列化一个实例内的操作，不替代桥接层的持久化发送日志。
发送前错误抛 `LinuxSenderError`；CLI 有明确 `not_delivered` 且 `retry=safe` 才属于
未投递。未知投递错误只触发回读，绝不再次点击发送。

当前 `send_image` 和非空 `ats` 明确抛 `SendNotSupported`。图片剪贴板的工具能力已
核实，但微信的粘贴、预览、目标确认和提交结构仍需真实观察，不能据此伪报图片发送成功。
