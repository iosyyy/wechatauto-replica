# AgentWeChat LangBot bridge

This optional Linux bridge connects the existing WeChatPad-compatible LangBot
adapter to an AgentWeChat native daemon. It uses the existing durable `Journal`,
HTTP/WebSocket gateway, explicit LangBot event ACKs, and independently authorized
history routes. LangBot's bot UUID, pipeline, persona, conversation history,
PowerContext, and registered history tools stay in LangBot.

The protocol source is AgentWeChat's Rust HTTP API: `wechat-api/src/lib.rs`,
`contracts.rs`, `media.rs`, and `http_route_table.rs`. This adapter implements
plain text only. Image sends and nonempty mention lists fail before any daemon
send request. It does not log into WeChat, attach a profile, refresh contacts,
start a GUI, or read a second history database.

## Prerequisites

- Python 3.9 or newer and the existing `linux-bridge` optional dependencies.
- A native AgentWeChat daemon already authenticated to the intended account.
- Use the reviewed [maintained protocol fork](https://github.com/iosyyy/agent-wechat)
  with the profile-ownership and media safety fixes. Before login, set the account's
  `autoDownloadMaxBytes` to `0`. That policy disables automatic thumbnails and voice
  downloads too. Authenticated CDN media transfers remain unavailable in this fork
  until a verified secure transport exists; this bridge exposes plain text only.
- The bridge and daemon in the same network namespace. The daemon URL must use
  a literal loopback IP, plain HTTP, and an explicit port. DNS names, credentials
  in the URL, non-root paths, query strings, fragments, redirects, and environment
  proxies are not supported.
- An account-scoped daemon API token with `read` and `send` permissions, stored
  in a caller-owned `0600` regular file with one hard link. Every path component
  must be free of symlinks. Configuration has the same file requirements.
- A new private journal directory reserved for this protocol source. Retain the
  old GUI bridge journal separately.

Read `/v1/status` and `/v1/accounts/{account}` through a private administrative
channel when preparing configuration. Pin the real daemon `serverId` and exact
account `id`/`ownUsername`. The adapter checks native backend identity, API
version 1, text/event capabilities, authentication, online native push,
completed contact sync, and absence of pending profile recovery/login publication.
A simulator, another account, an unexpected daemon data directory, or an
unverified connection remains unavailable.

## Configuration

Create a private JSON file. The values below are placeholders, not working
credentials or account identifiers. Use absolute paths for runtime files.

```json
{
  "daemon_url": "http://127.0.0.1:12345",
  "daemon_token_file": "/run/bridge/daemon.token",
  "account": "ACCOUNT_ID",
  "expected_wxid": "ACCOUNT_USERNAME",
  "expected_server_id": "DAEMON_SERVER_ID",
  "state_dir": "/var/lib/bridge-agent-wechat",
  "token": "REPLACE_WITH_PRIVATE_BRIDGE_TOKEN",
  "allowed_chats": ["filehelper", "OWNER_USERNAME", "GROUP_ID@chatroom"],
  "write_chats": ["filehelper", "OWNER_USERNAME", "GROUP_ID@chatroom"],
  "request_prefix": "/lb ",
  "group_request_prefix": "/lb ",
  "listen_host": "127.0.0.1",
  "port": 5080,
  "poll_interval": 0.5,
  "request_timeout": 5,
  "send_confirm_timeout": 30,
  "send_poll_interval": 0.2,
  "event_page_size": 100
}
```

`allowed_chats` controls incoming requests and group member access;
`write_chats` separately controls sends. Both lists are required, exact,
nonempty, and limited to 100 usernames. Display names never grant access.
Use the previously approved lists when replacing another bridge.

The daemon token, bridge token, and optional history token must be distinct.
Requests to the daemon use a bearer header. The existing WeChatPad gateway
continues to use its bridge `key` query parameter, so reverse proxies must omit
query strings from access logs. The standalone command disables access logging.
The default listener is loopback; set an explicit private listener only when
the existing LangBot network setup requires it.

```sh
python -m wechatauto.bridge.agent_wechat --config /run/bridge/agent-wechat.json
```

Only the `--config` argument is accepted; secrets do not belong in command-line
arguments. Configuration is validated before the listener starts. Existing
health, login, profile, send, member, and WebSocket routes are provided by
`wechatpad.create_app`.

## Incoming messages and startup

The bridge consumes bounded JSON pages from
`GET /v1/accounts/{account}/events?after=...&limit=...`. Its first verified poll
reads a one-item profile thread page and persists that page's atomic
`asOfEventCursor` as its startup baseline. It then immediately reads events
**after that exact cursor**, rather than taking another end-of-log snapshot.
Messages that arrive after the baseline but before the first event poll are
therefore retained. Events at or before the first baseline are startup history
and are deliberately excluded. Reopening the same journal resumes its committed
cursor and does not take a new baseline.

Only `message.committed` events with `origin: "live"` can become LangBot requests.
Catch-up records, local-send events, non-text messages, and recalled messages do
not enter the model. The referenced saved message is read by its opaque ID and
checked against the exact account, event thread, message ID, sender, and direction.

- Allowed direct chats accept incoming messages from that exact peer, including
  an approved owner's actual DM. The file-transfer request prefix does not apply
  to direct chats.
- `filehelper` accepts only this account's own outgoing text beginning with
  `request_prefix`. The prefix is removed, and an empty request is ignored.
  A self-message synced from another client can qualify; a `local_send` echo
  cannot.
- Allowed groups accept incoming non-self text beginning with
  `group_request_prefix`. The original prefix and group sender are preserved for
  LangBot's existing trigger processing.

Each event and its monotonic source cursor commit in the same SQLite transaction.
Only after those commits does the bridge ACK its daemon consumer cursor. The
daemon ACK means the bridge has a durable copy, not that LangBot has processed it.
LangBot's separate WebSocket ACK removes the event from journal replay. Event IDs
derive from the protocol source and opaque message ID; timestamps are never
used as deduplication keys.

Malformed pages, identity changes, unreadable referenced messages, expired event
cursors, and storage failures do not advance past the failing event. There is no
automatic cursor reset or history replay. Investigate a retained-cursor failure
before deliberately creating a new baseline: doing so omits any unconsumed gap.
The upstream event log retains approximately 10,000 events, so monitor outages
and retained cursor age. A journal is bound to protocol version, daemon origin,
daemon data-directory ID, account, and own username; a GUI journal or another
account's journal is rejected.

## Send confirmation and uncertain outcomes

`ClientMsgID` is protected by the existing gateway journal. The protocol
`Idempotency-Key` is a deterministic bounded hash of that ID and the pinned
source identity. The bridge issues one `POST /v1/accounts/{account}/sends` with
`{threadId,text}`. HTTP 202 establishes a send intent only.

The bridge polls `GET /v1/accounts/{account}/sends/{intent}` and reports
`confirmed: true` only when the intent still belongs to the expected account and
destination, its state is `accepted`, and its server message ID is a positive
decimal unsigned 64-bit ID. This is WeChat server acceptance, not evidence that
the recipient read the message. The bridge never treats queued, submitting,
failed, cancelled, `needs_review`, malformed receipts, or a timeout as confirmed.
The gateway records an uncertain outcome and rejects automatic re-submission of
that `ClientMsgID`, including after restart. Inspect uncertain sends manually;
do not clear the journal or generate a new client ID to retry blindly.

Request and response JSON are capped at 1 MiB. A daemon HTTP request has a
wall-clock deadline of at most ten seconds; send confirmation is bounded at
60 seconds. Text is bounded by both the existing gateway character limit and
the daemon's 64 KiB UTF-8 limit. Errors and routine polling logs omit credentials,
URLs, message bodies, and provider response bodies.

## Independent history capability

The optional `native_history` object has the unchanged existing gateway schema:

```json
{
  "token": "REPLACE_WITH_DISTINCT_HISTORY_TOKEN_AT_LEAST_32_BYTES",
  "owners": [{"bot_uuid": "EXISTING_BOT_UUID", "launcher_id": "OWNER_USERNAME"}],
  "allowed_groups": ["GROUP_ID@chatroom"]
}
```

The existing `/native-history/v1/groups` and `/native-history/v1/messages` routes
require this independent bearer token plus trusted LangBot bot/launcher headers.
An approved owner's private conversation can access only the configured history
groups. An approved group conversation can access only its own group. Neither a
bridge query key nor a request body can enlarge that scope.

History comes from AgentWeChat's saved profile APIs. Group listing performs exact
detail reads only for a bounded authorized group list; messages follow server
opaque profile cursors. A request reads at most six pages of 200 profile records
to adapt the existing maximum offset of 1,000 and result limit of 100. Oversized
pages, cursor loops, or exhausted traversal bounds fail explicitly. Missing
timestamps do not establish activity in a requested time range. Existing gateway
response byte limits, seven-day query range, text clipping, non-text placeholders,
and local-only completeness caveats still apply. There is no parallel history DB
or UI/HTML scraping path.

## Verification and rollout boundary

```sh
python -m unittest discover -s tests -p test_agent_wechat.py -v
python -m unittest discover -s tests -p test_linux_wechatpad.py -v
```

These tests use synthetic loopback HTTP daemons and real private SQLite journals.
They cover startup arrivals, exact account/allowlist checks, live/catch-up filtering,
transaction rollback before daemon ACK, journal restart, final send receipts,
uncertain-send deduplication, independent history authorization, and WebSocket ACK.
They do not prove real login, protocol compatibility with a running WeChat account,
recipient delivery, LangBot model execution, or improved live latency.

For a controlled replacement, stop the old bridge before enabling this backend,
keep its journal intact, preserve the existing LangBot bot and routing settings,
and use the new protocol journal directory. Verify the daemon/account pins, then
verify an authorized incoming request and its single accepted reply through the
existing LangBot pipeline. Record separate timings for protocol receipt, model
execution, send acceptance, and the observed recipient message. Rollback restores
the prior bridge and its original journal; never run both responders against the
same approved conversations simultaneously.
