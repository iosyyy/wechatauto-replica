# Native WeChat history tools

LangBot tools read authorized groups from the native bridge's local WeChat databases. The current pipeline model and persona produce the summary. There is no nested model call, embedding dependency, message cache, global memory scope, automatic joining, or message-sending permission in this plugin.

The tool checks `QueryBasedAPIProxy.get_bot_uuid()` and `get_query_vars()` against the host-provided `Session`. Private requests require configured `(bot_uuid, person launcher_id)` pairs. An optional `filehelper` binding must be configured explicitly as another owner pair. A group request requires a bot UUID from those owner bindings and the current exact `@chatroom` launcher ID; the API additionally requires that room in `allowed_groups`. The API and plugin independently restrict group reads to that same room. Group list calls return only the current group. Group history can omit `group` to use the trusted current room, or supply its exact username; names and other group IDs are rejected before a history read. Tool arguments cannot choose identity.

## Native configuration

`create_app(..., history_config=None)` disables the entire `/native-history/` namespace. Runtime configuration uses the optional `native_history` object:

```json
{
  "native_history": {
    "token": "<dedicated random token, at least 32 bytes, different from bridge token>",
    "owners": [{"bot_uuid": "<verified bot UUID>", "launcher_id": "<verified private WeChat username>"}],
    "allowed_groups": []
  }
}
```

Keep runtime files private (`0600`). Use a private interface/firewall and a dedicated history token. Forward no URL or query-string credentials. Do not expose this capability through a public proxy. The plugin accepts HTTP loopback addresses by default; HTTP to a numeric private IP or the exact internal Docker service hostname `wechatauto-linux-desktop-1` additionally requires `allow_private_http=true`. Other HTTP hostnames are rejected. A configured HTTPS URL is also accepted. Redirects and environment HTTP proxies are disabled.

For this deployment, attach the plugin runtime and the native desktop service to the private Docker network `wechatpad_link` persistently, without publishing port 5080. Set `server_url=http://wechatauto-linux-desktop-1:5080` and `allow_private_http=true`; the stable service hostname avoids relying on a container IP that may change. This is installation configuration and cannot be supplied by tool arguments.

An empty group allowlist is valid and returns an honest empty authorized-group list. After inviting the logged-in bot account into a group, verify its exact `@chatroom` username and add it to `native_history.allowed_groups`. Configure inbound `allowed_chats` and outbound `write_chats` separately for group interactions; enabling history does not authorize sending.

The two POST endpoints require `Authorization: Bearer <history token>`, `X-LangBot-Bot-UUID`, `X-LangBot-Launcher-Type: person|group` and `X-LangBot-Launcher-ID`. The dedicated token is a trusted-client capability: only the plugin/trusted proxy supplies the host-derived identity headers, never raw user input. Private headers must match a configured owner pair. Group headers must bind an authorized bot to the current allowlisted room:

- `/native-history/v1/groups`: `{"limit":50,"offset":0}`; the backend receives `allowed_groups` for SQL-filtered pagination.
- `/native-history/v1/messages`: `{"group_username":"123@chatroom","limit":50,"offset":0,"start_time":<inclusive Unix seconds>,"end_time":<exclusive Unix seconds>}`.

In a group, the API scopes group-list SQL to that one room and rejects any different `group_username`; omitting it defaults to the current room. Responses state `access_scope: current_group` or `owner_private`. Missing/unknown launcher types fail closed. Version 0.1.1 requires this new type header; older private callers must add `person` when upgrading the bridge.

Bounds are 4 KiB request bodies, 50 records per page, offset 0–1000, a seven-day maximum requested time range, 8 KiB text per record, and 64 KiB total response. Media contents are omitted. `next_offset` counts inspected records, including rows outside the time range. Filtering applies to that bounded inspected page, so an empty page with `has_more=true` does not prove an empty period. If pagination or response-byte bounds stop reading, report the limitation. Reads are fresh requests; simultaneous new arrivals can shift offset pages, so this is not a consistent snapshot export.

Every response identifies the source as local-only. Missing sync, retention, deletion and pre-membership records prevent claims about complete group history. Database/backend failures return errors rather than synthetic empty history. Duplicate group names require an explicit username choice.

## Build and install

Build with the locally installed LangBot Plugin SDK 0.7.24 (`lbp`). Use a Python 3.12 environment with the SDK and `httpx==0.28.1`; the bridge itself supports Python 3.9.

```sh
python -m pip install langbot-plugin==0.7.24 httpx==0.28.1
python -m unittest discover -s tests -v
lbp build -o /private/tmp/native-history-dist
```

Run these commands from this directory. The output is `local-NativeWeChatHistory-0.1.1.lbpkg`. Install that local package through LangBot's plugin management. The package declares no workspace operations and no privileged capabilities. Enable its tools on the native pipeline; retain the current model and persona and normal automatic tool selection.

Set installation-scoped configuration, without copying secrets into a chat or a shell command:

```json
{
  "server_url": "http://127.0.0.1:5080",
  "api_token": "<same dedicated history token>",
  "owners_json": "[{\"bot_uuid\":\"<verified bot UUID>\",\"launcher_id\":\"<verified private username>\"}]",
  "allow_private_http": false
}
```

Keep any memory plugin scoped to the current private session/speaker. Do not enable automatic capture or global/bot scope for returned group-history text. For native group conversations use a separate pipeline without PowerContext or another private-memory injector, and exclude the original GroupChatSummary cache tool so summaries use this local database history. This plugin does not write memory or send replies by itself.

Before real acceptance, invite the bot into target groups, verify group identities, update the explicit allowlist, and confirm private owner binding through a real incoming event. Ask in that private conversation, for example “总结测试群过去一小时的讨论”; in an allowlisted group ask “/lb 总结本群”. Confirm actual tool invocation and source records. A non-owner DM, wrong bot identity, and a group requesting any other room must be denied. Until those invitations and reads occur, synthetic tests establish only offline behavior.
