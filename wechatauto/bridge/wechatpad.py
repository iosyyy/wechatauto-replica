"""LangBot 使用的 WeChatPad HTTP/WebSocket 子集，不模拟未实现的微信能力。"""

import asyncio
import base64
import binascii
from collections import deque
import hashlib
import hmac
import json
import logging
import uuid

from aiohttp import WSMsgType, web
from aiohttp.abc import AbstractAccessLogger

from .journal import ConflictError, _identifier, _json


MAX_HTTP_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_WS_BYTES = 4096
MAX_TEXT_CHARS = 32768
logger = logging.getLogger(__name__)


class _PrivateAccessLogger(AbstractAccessLogger):
    def log(self, request, response, elapsed):
        # query 中含凭据，访问日志仅记录方法、状态和耗时。
        self.logger.info("bridge request method=%s status=%s duration=%.3f", request.method, response.status, elapsed)


def _response(code=200, data=None, text="", status=200):
    return web.json_response({"Code": code, "Data": data, "Text": text}, status=status)


def _string(value, name, maximum=256, allow_empty=False):
    if (not isinstance(value, str) or (not value and not allow_empty)
            or len(value) > maximum or "\x00" in value):
        raise ValueError("invalid " + name)
    return value


def encode_native_record(record, own_wxid, event_id=None):
    """把读取器的新消息 dict 转为 LangBot 需要的字段，不读取历史。

    record 必须有 username/content/type/create_time，群消息还需 sender_username。
    默认事件键绑定本账号、会话、分片、local_id 和 sort_seq；可显式传入稳定 event_id。
    poller 应只传入新消息，并调用 journal.enqueue_inbound(payload['_bridge_event_id'], payload)。
    """
    own_wxid = _string(own_wxid, "own wxid")
    username = _string(record.get("username"), "conversation username")
    content = _string(record.get("content"), "content", maximum=256 * 1024, allow_empty=True)
    if username.endswith("@chatroom"):
        sender = _string(record.get("sender_username"), "group sender")
        if not content.startswith(sender + ":\n"):
            content = sender + ":\n" + content
    if event_id is None:
        identity = [own_wxid, username, record.get("shard"), record.get("local_id"), record.get("sort_seq")]
        if record.get("local_id") is None or record.get("sort_seq") is None:
            raise ValueError("native message identity is incomplete")
        event_id = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    _identifier(event_id)
    msg_type = record.get("msg_type", record.get("type"))
    if not isinstance(msg_type, int):
        msg_type = {"文本": 1, "图片": 3, "语音": 34, "视频": 43, "动画表情": 47,
                    "文件": 49, "text": 1, "image": 3}.get(msg_type)
    if msg_type is None:
        raise ValueError("unsupported native message type")
    create_time = record.get("create_time")
    if not isinstance(create_time, int) or isinstance(create_time, bool) or create_time < 0:
        raise ValueError("invalid message creation time")
    return {"from_user_name": {"str": username}, "to_user_name": {"str": own_wxid},
            "content": {"str": content}, "msg_type": msg_type, "create_time": create_time,
            "new_msg_id": record.get("server_id") or int(hashlib.sha256(event_id.encode()).hexdigest()[:15], 16),
            "msg_source": record.get("msg_source", ""), "_bridge_event_id": event_id}


def _send_request(body, kind):
    if not isinstance(body, dict) or not isinstance(body.get("MsgItem"), list) or len(body["MsgItem"]) != 1:
        raise ValueError("exactly one MsgItem is required")
    item = body["MsgItem"][0]
    if not isinstance(item, dict):
        raise ValueError("MsgItem must be an object")
    to = _string(item.get("ToUserName"), "ToUserName")
    ats = item.get("AtWxIDList", [])
    if not isinstance(ats, list) or len(ats) > 100:
        raise ValueError("invalid AtWxIDList")
    ats = [_string(value, "at username") for value in ats]
    client_id = item.get("ClientMsgID", body.get("ClientMsgID"))
    if client_id is None:
        client_id = uuid.uuid4().hex
    _identifier(client_id)
    if item.get("ClientMsgID") is not None and body.get("ClientMsgID") not in (None, client_id):
        raise ValueError("conflicting ClientMsgID values")
    field = "TextContent" if kind == "text" else "ImageContent"
    maximum = MAX_TEXT_CHARS if kind == "text" else MAX_HTTP_BYTES
    content = _string(item.get(field), field, maximum=maximum)
    if kind == "image":
        if content.startswith("data:"):
            prefix, separator, content = content.partition(",")
            if not separator or not prefix.startswith("data:image/") or not prefix.endswith(";base64"):
                raise ValueError("invalid image data URI")
        try:
            decoded = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid image base64") from exc
        if not decoded or len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError("image exceeds the size limit")
    return client_id, {"kind": kind, "to": to, "content": content, "ats": ats}


def create_app(backend, journal, token):
    """创建已鉴权的 aiohttp Application，不启动服务或读取账号。

    backend 是同步接口：profile()->{wxid,nickname}，is_logged_in()->bool，
    send_text(to,text,ats,client_id)/send_image(to,base64,ats,client_id)->{confirmed,msg_id}，
    chatroom_members(room)->[{user_name,nick_name}]。所有调用均在线程中执行。
    journal 由调用方创建/关闭；单个服务串行执行 GUI 发送，只对 confirmed=True 返回 Code=200。
    WebSocket 重连重放全部未确认事件，接收 {type:'ack',event_id:'...'} 后持久确认。
    通过 create_app 配置的访问日志不包含 URL/query。部署反向代理同样必须关闭 query 日志。
    """
    _string(token, "token", maximum=4096)
    secret = token.encode("utf-8")
    send_lock = asyncio.Lock()
    tasks = set()
    sockets = set()

    @web.middleware
    async def authenticate(request, handler):
        if request.path != "/health":
            values = request.query.getall("key", [])
            supplied = values[0].encode("utf-8") if len(values) == 1 else b""
            if not hmac.compare_digest(secret, supplied):
                return _response(401, text="Unauthorized", status=401)
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            return _response(413, text="Request exceeds the size limit", status=413)
        except (ValueError, json.JSONDecodeError):
            return _response(400, text="Invalid request", status=400)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _response(503, text="Bridge backend unavailable", status=503)

    app = web.Application(client_max_size=MAX_HTTP_BYTES, middlewares=[authenticate],
                          handler_args={"access_log_class": _PrivateAccessLogger})

    async def body(request):
        raw = await request.read()
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc

    async def health(request):
        counts = await asyncio.to_thread(journal.counts)
        logged_in = bool(await asyncio.to_thread(backend.is_logged_in))
        return web.json_response({"ready": logged_in, **counts}, status=200 if logged_in else 503)

    async def login(request):
        logged_in = await asyncio.to_thread(backend.is_logged_in)
        return _response(200 if logged_in else 300, {"logged_in": bool(logged_in)},
                         "" if logged_in else "你已退出微信")

    async def profile(request):
        value = await asyncio.to_thread(backend.profile)
        return _response(data={"userInfo": {
            "nickName": {"str": _string(value.get("nickname"), "nickname", allow_empty=True)},
            "userName": {"str": _string(value.get("wxid"), "wxid")}}})

    async def members(request):
        value = await body(request)
        room = _string(value.get("ChatRoomName"), "ChatRoomName")
        if not room.endswith("@chatroom"):
            raise ValueError("ChatRoomName must be a room username")
        result = await asyncio.to_thread(backend.chatroom_members, room)
        if not isinstance(result, list) or len(result) > 10000:
            raise ValueError("invalid chatroom member list")
        normalized = [{"user_name": _string(member.get("user_name"), "member username"),
                       "nick_name": _string(member.get("nick_name"), "member nickname", allow_empty=True)}
                      for member in result]
        return _response(data={"member_data": {"chatroom_member_list": normalized}})

    def sent_response(client_id, receipt):
        return _response(data=[{"ClientMsgID": client_id, "NewMsgId": receipt.get("msg_id"),
                                "confirmed": True}])

    async def dispatch(client_id, value):
        async with send_lock:
            try:
                state = await asyncio.to_thread(journal.claim_outbox, client_id, value)
            except ConflictError:
                return _response(409, {"client_id": client_id}, "ClientMsgID conflicts with another request", 409)
            if not state["claimed"]:
                if state["status"] == "sent":
                    return sent_response(client_id, state["receipt"])
                return _response(409, {"client_id": client_id, "status": state["status"]},
                                 "Send outcome requires manual reconciliation", 409)
            try:
                method = backend.send_text if value["kind"] == "text" else backend.send_image
                receipt = await asyncio.to_thread(method, value["to"], value["content"], value["ats"], client_id)
                if isinstance(receipt, dict) and receipt.get("confirmed") is True and receipt.get("msg_id"):
                    safe_receipt = {"confirmed": True, "msg_id": _string(str(receipt["msg_id"]), "message id")}
                    await asyncio.to_thread(journal.finish_outbox, client_id, safe_receipt)
                    return sent_response(client_id, safe_receipt)
            except Exception as exc:
                frames = deque(maxlen=4)
                current = exc.__traceback__
                while current is not None:
                    frames.append("%s:%d" % (current.tb_frame.f_code.co_name, current.tb_lineno))
                    current = current.tb_next
                logger.warning("bridge dispatch failed exception_type=%s code=%s frames=%s",
                               type(exc).__name__, getattr(exc, "code", "unclassified"), ">".join(frames))
            await asyncio.to_thread(journal.mark_uncertain, client_id)
            return _response(409, {"client_id": client_id, "status": "uncertain"},
                             "Send outcome requires manual reconciliation", 409)

    async def send(request):
        kind = "text" if request.path.endswith("SendTextMessage") else "image"
        client_id, value = _send_request(await body(request), kind)
        task = asyncio.create_task(dispatch(client_id, value))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return await asyncio.shield(task)

    async def sync(request):
        ws = web.WebSocketResponse(max_msg_size=MAX_WS_BYTES, heartbeat=30)
        await ws.prepare(request)
        sockets.add(ws)

        async def replay():
            after = 0
            while not ws.closed:
                rows = await asyncio.to_thread(journal.unacked, after, 100)
                for row in rows:
                    await asyncio.wait_for(ws.send_str(_json(row["payload"])), timeout=10)
                    after = row["seq"]
                if len(rows) < 100:
                    await asyncio.sleep(.1)

        sender = asyncio.create_task(replay())

        def sender_done(completed):
            if not completed.cancelled() and completed.exception() is not None:
                asyncio.create_task(ws.close(code=1011, message=b"Replay failed"))

        sender.add_done_callback(sender_done)
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    await ws.close(code=1003, message=b"JSON ACK required")
                    break
                try:
                    ack = json.loads(message.data)
                    if not isinstance(ack, dict) or ack.get("type") != "ack":
                        raise ValueError("invalid ACK")
                    event_id = _identifier(ack.get("event_id"))
                    await asyncio.to_thread(journal.ack, event_id)
                except (ValueError, TypeError):
                    await ws.close(code=1008, message=b"Invalid ACK")
                    break
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            sockets.discard(ws)
        return ws

    async def unsupported(request):
        return _response(501, text="Unsupported WeChatPad API", status=501)

    async def shutdown(application):
        await asyncio.gather(*(ws.close(code=1001, message=b"Bridge shutting down") for ws in list(sockets)))
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)

    app.router.add_get("/health", health)
    app.router.add_get("/login/GetLoginStatus", login)
    app.router.add_get("/user/GetProfile", profile)
    app.router.add_post("/message/SendTextMessage", send)
    app.router.add_post("/message/SendImageMessage", send)
    app.router.add_post("/group/GetChatroomMemberDetail", members)
    app.router.add_post("/chatroom/GetChatRoomMemberDetail", members)
    app.router.add_get("/ws/GetSyncMsg", sync)
    app.router.add_get("/GetSyncMsg", sync)
    app.router.add_route("*", "/{tail:.*}", unsupported)
    app.on_shutdown.append(shutdown)
    return app
