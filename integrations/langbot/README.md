# LangBot 原生 Linux 微信桥接补丁

本目录保存配套 LangBot 补丁，便于在维护者现有 LangBot checkout 中复现本 fork 的原生 Linux 桥接。
补丁仅含下列 7 个既有源文件和 2 个新增测试；不包含其他 plugin、运行配置或 `.runtime` 内容。

## 基线与依赖

- LangBot HEAD：`307953188ba2edbaeb37133d2a38d542a218c233`。
- 基线提交：`fix(llm): keep LiteLLM's bundled tiktoken encodings offline on Windows (#2982)`。
- LangBot 版本：`4.11.6`，仓库声明 Python `>=3.11,<4.0`、`langbot-plugin==0.7.24`。
- 配套读取器分支版本：`wechatauto-replica 1.2.10+iosyyy.linux.2`；Linux runtime 安装使用本 fork 的 `[linux-bridge]` extra。
- LangBot 依赖使用该 HEAD 的 `pyproject.toml` / `uv.lock`。补丁没有依赖或数据库迁移变更。
- 本地合成验证环境：Python `3.13.2`、SDK `0.7.24`、requests `2.34.2`、websocket-client `1.9.2`、
  Pydantic `2.14.3`、SQLAlchemy `2.1.8`、greenlet `3.5.6`、pytest `9.2.0`、pytest-asyncio `1.4.1.post1`。

补丁 SHA-256：`9e398bea4d97e4ca5943488da10ccc9c7873d84593e79c3e89aceb55c0f06d3a`

## 内容

```text
src/langbot/libs/wechatpad_api/api/message.py
src/langbot/libs/wechatpad_api/client.py
src/langbot/libs/wechatpad_api/util/http_util.py
src/langbot/pkg/pipeline/controller.py
src/langbot/pkg/pipeline/pipelinemgr.py
src/langbot/pkg/platform/botmgr.py
src/langbot/pkg/platform/sources/wechatpad.py
tests/unit_tests/platform/test_wechatpad_bridge_delivery.py
tests/unit_tests/platform/test_wechatpad_native_completion.py
```

`test_wechatpad_native_completion.py` 是准确的文件名。

## 应用、安装与回退

以下命令在完整 LangBot checkout 的根目录执行。`PATCH` 改为本目录补丁的绝对路径；
这 9 个目标文件应与基线一致，其他工作区改动由维护者自行保留。

```bash
PATCH=/absolute/path/to/wechatauto-replica/integrations/langbot/native-bridge.patch
git rev-parse HEAD
# 应为 307953188ba2edbaeb37133d2a38d542a218c233
shasum -a 256 "$PATCH"
git apply --check --whitespace=error-all "$PATCH"
git apply --whitespace=error-all "$PATCH"
uv sync --frozen --dev
```

把本 fork 的原生桥接单独部署到微信现有桌面会话，按 [桥接配置说明](../../docs/linux-bridge.md)
配置私有 token、同一会话环境与明确的账号及会话允许列表。LangBot 使用 WeChatPad adapter，
HTTP/WS 地址指向私有桥接，并把事件路由绑定到 Pipeline。`wxid` 使用已验证的本人 username，
账号目录末尾的四位存储后缀属于路径身份，不应附加到 `wxid`。

回退前停止处理新 native 消息。若应用后改过这 9 个目标文件，先保存这些改动，再确认反向检查通过：

```bash
git apply --reverse --check "$PATCH"
git apply --reverse "$PATCH"
```

应用和回退均无需修改真实索引。本目录打包验证仅在临时树执行；没有 commit、push 或上游 PR。

## native 行为边界

- 只有 adapter 显式提供 `native_bridge_completion=True` 且消息携带有效 `_bridge_event_id` 才启用 native 完成回执。
  普通 WeChatPad 消息继续使用既有路径，SDK 的 `ClientMsgID` 参数保持可选。
- 带 `ClientMsgID` 的文本/图片 SDK 调用传入 `requests.post(timeout=180)`；普通 SDK 调用保持默认 `60` 秒参数，
  且不会额外传递新关键字。native Pipeline 完成等待默认 `300` 秒，超时失败且不 ACK。
  `180` 是 Requests 的连接/读取等待参数，不代表整个 GUI 发送总耗时的硬上限；外部发送结果不明仍需核对。
- native 消息仅支持 **Pipeline** 事件路由。Agent / event_processor 路由缺少本次验证的完成回执，明确失败且不 ACK。
- native 消息跳过聚合、steering 和 interaction 消费旁路；排队或创建 debounce timer 不代表完成。
  真实 Pipeline 成功且发送阶段没有失败才 ACK；过滤、丢弃、取消、超时和异常均不 ACK。
- 同一事件重连时合并等待已有 in-flight Task；已完成事件在当前 adapter 进程保留最多 1000 项 ID 缓存。
  ACK 失败、处理失败或回调队列满时，只关闭对应 origin WebSocket 以请求重放。
- 发送 ID 由事件、reply sequence 与 send slot 构成，不以重新生成的正文或图片内容作为身份。
  桥接 journal 保存 outbox 状态和请求哈希；不明结果或相同 ID 内容冲突须人工核对，不能换 ID 盲目重发。
- 这不承诺模型只运行一次，也不承诺跨进程重启的外部微信 exactly-once。
  本次原生发送验收范围为文本；图片、语音、文件和 @mention 尚未完成原生发送验收。

## 合成验证

补丁打包时已从准确 HEAD 导出上述 7 个源文件，在临时树执行：

1. `git apply --check --whitespace=error-all`。
2. 实际应用补丁，核对 9 个文件与本次选中工作树内容逐字节一致。
3. `git apply --reverse --check` 及实际回退，核对 7 个基线文件恢复、新增测试删除。
4. 核对打包前后的 LangBot HEAD、真实索引校验和及所选文件内容保持一致。

完整依赖环境可直接运行：

```bash
uv run --no-sync python -m pytest \
  tests/unit_tests/platform/test_wechatpad_bridge_delivery.py \
  tests/unit_tests/platform/test_wechatpad_native_completion.py \
  tests/unit_tests/platform/test_wechatpad_limits.py -q
```

SDK 与 limits 独立验证 **45 passed**，覆盖文本/图片 native `180` 与普通 `60` 秒的实际 Mock transport 参数。

本地组合回归 **204 passed**：新增完成边界 17 项，加上 adapter、limits、controller、tenancy、terminal monitoring、
QueryPool、aggregator、platform execution trace 的相邻用例。该次测试入口只给 Application 类型导入提供轻量占位，
实际执行 RuntimeBot、QueryPool、Controller、RuntimePipeline、SendResponseBackStage 与 adapter；provider、
发送 HTTP 和 monitoring IO 使用合成替身。没有启动完整 LangBot 服务，也没有调用真实微信。
复现同一局部单元环境的完整入口如下；在完整依赖环境也可去掉占位，直接运行列出的测试路径：

```bash
uv run --no-sync python - <<'PYTEST'
import sys
from types import ModuleType
import pytest
application = ModuleType('langbot.pkg.core.app')
application.Application = object
sys.modules['langbot.pkg.core.app'] = application
raise SystemExit(pytest.main([
    'tests/unit_tests/platform/test_wechatpad_native_completion.py',
    'tests/unit_tests/platform/test_wechatpad_bridge_delivery.py',
    'tests/unit_tests/platform/test_wechatpad_limits.py',
    'tests/unit_tests/pipeline/test_controller.py',
    'tests/unit_tests/pipeline/test_controller_tenancy.py',
    'tests/unit_tests/pipeline/test_terminal_monitoring.py',
    'tests/unit_tests/pipeline/test_query_pool.py',
    'tests/unit_tests/pipeline/test_aggregator.py',
    'tests/unit_tests/platform/test_botmgr_execution_trace.py', '-q']))
PYTEST
```

测试还完成了发送不明后重新生成不同内容/类型仍命中同一发送 ID、ACK 失败重放、in-flight 合并、
真实 QueryPool 入队后不提前 ACK、Pipeline 发送成功后 ACK，以及失败/取消/丢弃/超时不 ACK 的回归。
Ruff 检查、格式检查及 `git diff --check` 通过。

## 真实服务验收状态（更新至 2026-11-16）

官方 Linux 微信已完成本人文件传输助手的多行文本发送、数据库正文与正数 `server_id` 核对。
兼容 API 完整导航发送返回确认回执；实际微信请求经 DeepSeek Pipeline 后收到正确的 `437` 回复，
inbox ACK 已核实。单次问答观测由 90.52 秒降至 66.74 秒，仍存在明显桌面自动化延迟。

上述实际链路使用现有 Pipeline 的模型、人格和插件配置；记忆、人格、联网的追加问答验收在进行。
当前仅开放本人文件传输助手 `/lb ` 文本路径，不扩展为群聊、图片或所有插件发送能力的验收。
详见 [验证记录](../../docs/linux-verification.md)。真实账号、正文、token 和私有服务配置不写入补丁。
