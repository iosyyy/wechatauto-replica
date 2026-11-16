"""所有者私聊或当前群专用的有界本机历史接口，与发送凭据隔离。"""

import asyncio
import hashlib
import hmac
import json
import re
import time

from aiohttp import web


PREFIX = "/native-history/v1/"
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 8 * 1024
MAX_RANGE_SECONDS = 7 * 24 * 3600
MAX_OFFSET = 1000
LOCAL_NOTE = ("Only records currently available in this account's local WeChat databases are included. "
              "Unsynced, deleted, expired, or pre-membership records may be missing. "
              "An empty page does not establish that the group had no activity.")


def _identity(value, name, maximum=256):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(char) < 32 for char in value)):
        raise ValueError("invalid " + name)
    return value


def validate_history_config(value, bridge_token=None):
    """缺省完全关闭；空群白名单有效，但缺少专用令牌或所有者绑定一律拒绝。"""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"token", "owners", "allowed_groups"}:
        raise ValueError("invalid native_history configuration")
    token = _identity(value.get("token"), "history token", 4096)
    if len(token.encode("utf-8")) < 32 or token == bridge_token:
        raise ValueError("history requires a distinct token of at least 32 bytes")
    owners = value.get("owners")
    if not isinstance(owners, list) or not 1 <= len(owners) <= 5:
        raise ValueError("history requires explicit private owner bindings")
    normalized = []
    for owner in owners:
        if not isinstance(owner, dict) or set(owner) != {"bot_uuid", "launcher_id"}:
            raise ValueError("invalid history owner binding")
        normalized.append({name: _identity(owner.get(name), "owner " + name) for name in owner})
    groups = value.get("allowed_groups")
    if not isinstance(groups, list) or len(groups) > 100:
        raise ValueError("invalid history group allowlist")
    for group in groups:
        _identity(group, "group username")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+@chatroom", group):
            raise ValueError("history accepts exact group usernames only")
    return {"token": token, "owners": normalized, "allowed_groups": list(dict.fromkeys(groups))}


def _response(data=None, error=None, status=200):
    value = {"error": error} if error else data
    return web.json_response(value, status=status, headers={"Cache-Control": "no-store"},
                             dumps=lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def authorize(request, config):
    """专用 capability 仅接受请求头；桥接 key 与 URL 参数不能授予历史权限。"""
    if config is None:
        return _response(error="History capability is disabled", status=404)
    values = request.headers.getall("Authorization", [])
    expected = ("Bearer " + config["token"]).encode("utf-8")
    supplied = values[0].encode("utf-8") if len(values) == 1 else b""
    if not hmac.compare_digest(expected, supplied):
        return _response(error="Unauthorized", status=401)
    bot = request.headers.getall("X-LangBot-Bot-UUID", [])
    launcher = request.headers.getall("X-LangBot-Launcher-ID", [])
    kind = request.headers.getall("X-LangBot-Launcher-Type", [])
    if len(bot) != 1 or len(launcher) != 1 or len(kind) != 1:
        return _response(error="Trusted conversation binding required", status=403)
    if kind[0] == "person":
        allowed = any(owner["bot_uuid"] == bot[0] and owner["launcher_id"] == launcher[0]
                      for owner in config["owners"])
    elif kind[0] == "group":
        allowed = (bot[0] in {owner["bot_uuid"] for owner in config["owners"]}
                   and launcher[0] in config["allowed_groups"])
    else:
        allowed = False
    if not allowed:
        return _response(error="Conversation is not authorized for history", status=403)
    return None


def _scope(request, config):
    """中间件已鉴权的群会话只能得到自己的精确白名单，不能从正文扩张范围。"""
    if request.headers["X-LangBot-Launcher-Type"] == "group":
        return [request.headers["X-LangBot-Launcher-ID"]], "current_group"
    return config["allowed_groups"], "owner_private"


async def _body(request, allowed):
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_REQUEST_BYTES, actual_size=request.content_length)
    raw = bytearray()
    async for chunk in request.content.iter_chunked(1024):
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_REQUEST_BYTES, actual_size=len(raw))
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid history JSON") from None
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("unknown history request fields")
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("history bounds exceeded")
    return value


def _pagination(value):
    return (_integer(value.get("limit", 50), 1, 50),
            _integer(value.get("offset", 0), 0, MAX_OFFSET))


def _page(rows, limit, offset):
    more = len(rows) > limit
    next_offset = offset + limit if more and offset + limit <= MAX_OFFSET else None
    return {"has_more": more, "next_offset": next_offset,
            "pagination_bound_reached": more and next_offset is None}


def _clip(text, maximum):
    encoded = text.encode("utf-8")
    return encoded[:maximum].decode("utf-8", errors="ignore"), len(encoded) > maximum


def _message(row, group):
    if not isinstance(row, dict):
        raise RuntimeError("invalid history backend response")
    created = _integer(row.get("create_time"), 0, 2 ** 63 - 1)
    sender = row.get("sender_username") or ""
    kind = row.get("type", row.get("msg_type", "unknown"))
    if not isinstance(sender, str) or len(sender) > 256 or not isinstance(kind, (str, int)):
        raise RuntimeError("invalid history message metadata")
    text = row.get("content") or ""
    if not isinstance(text, str):
        raise RuntimeError("invalid history text")
    is_text = kind in (1, "文本", "text")
    text, clipped = _clip(text if is_text else "[Non-text message; content not retrieved]", MAX_MESSAGE_BYTES)
    identity = [group, row.get("shard"), row.get("local_id"), row.get("sort_seq")]
    return {"message_id": hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()[:24],
            "create_time": created, "sender_username": sender, "type": str(kind)[:64],
            "text": text, "text_truncated": clipped, "non_text_content_omitted": not is_text}


def register_routes(app, backend, config):
    """只注册两个独立的只读入口；权限中间件由宿主在 catch-all 之前调用。"""
    async def groups(request):
        value = await _body(request, {"limit", "offset"})
        limit, offset = _pagination(value)
        allowed_groups, scope = _scope(request, config)
        rows = await asyncio.to_thread(backend.list_groups, limit=limit + 1, offset=offset,
                                       allowed_groups=allowed_groups)
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise RuntimeError("invalid history group response")
        normalized = []
        for row in rows:
            if (not isinstance(row, dict) or row.get("username") not in allowed_groups
                    or not isinstance(row.get("name"), str)):
                raise RuntimeError("history backend returned an unauthorized group")
            normalized.append({"username": row["username"], "name": _clip(row["name"], 512)[0]})
        return _response({"groups": normalized[:limit], "offset": offset, **_page(rows, limit, offset),
                          "status": "ok" if rows else "no_authorized_local_groups_on_this_page",
                          "source": "local_wechat_database", "completeness": "local_only",
                          "access_scope": scope, "note": LOCAL_NOTE})

    async def messages(request):
        value = await _body(request, {"group_username", "limit", "offset", "start_time", "end_time"})
        limit, offset = _pagination(value)
        allowed_groups, scope = _scope(request, config)
        group = value.get("group_username", allowed_groups[0] if scope == "current_group" else None)
        if not isinstance(group, str) or group not in allowed_groups:
            return _response(error="Group is not authorized for history", status=403)
        start = _integer(value.get("start_time"), 0, 2 ** 63 - 1)
        end = _integer(value.get("end_time"), 0, int(time.time()) + 300)
        if not start < end or end - start > MAX_RANGE_SECONDS:
            raise ValueError("history time range exceeds seven days")
        rows = await asyncio.to_thread(backend.get_history, group, limit=limit + 1, offset=offset)
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise RuntimeError("invalid history message response")
        result, inspected, used, byte_bound = [], 0, 0, False
        for row in rows[:limit]:
            message = _message(row, group)
            if start <= message["create_time"] < end:
                size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
                if used + size > MAX_RESPONSE_BYTES - 4096:
                    byte_bound = True
                    break
                result.append(message)
                used += size
            inspected += 1
        more = byte_bound or len(rows) > limit
        following = offset + inspected if more and inspected and offset + inspected <= MAX_OFFSET else None
        return _response({"group_username": group, "messages": result, "start_time": start, "end_time": end,
                          "offset": offset, "inspected_count": inspected, "has_more": more,
                          "next_offset": following, "pagination_bound_reached": more and following is None,
                          "response_byte_bound_reached": byte_bound,
                          "status": "ok" if result else "no_matching_records_in_inspected_page",
                          "source": "local_wechat_database", "order": "newest_sort_seq_first",
                          "time_filter": "applied_to_inspected_page", "completeness": "local_only",
                          "access_scope": scope, "note": LOCAL_NOTE})

    app.router.add_post(PREFIX + "groups", groups)
    app.router.add_post(PREFIX + "messages", messages)
