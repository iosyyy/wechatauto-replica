from __future__ import annotations

import json
import time

from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from history_client import HistoryClient, HistoryError, settings_from_config
from history_client import is_group_username


class NativeHistoryPlugin(BasePlugin):
    """Resolve settings and host identity per invocation; retain no message data."""

    async def authorized_client(self, session, query_id):
        settings = settings_from_config(self.get_config())
        api = QueryBasedAPIProxy(query_id=query_id, plugin_runtime_handler=self.plugin_runtime_handler)
        bot_uuid = await api.get_bot_uuid()
        variables = await api.get_query_vars()
        launcher = variables.get("launcher_id")
        kind = variables.get("launcher_type")
        session_kind = getattr(getattr(session, "launcher_type", None), "value", None)
        session_launcher = getattr(session, "launcher_id", None)
        if kind not in ("person", "group") or session_kind != kind or launcher != session_launcher:
            raise HistoryError("Host conversation identity mismatch.")
        if kind == "person":
            allowed = (bot_uuid, launcher) in settings.owners
        else:
            allowed = is_group_username(launcher) and bot_uuid in {owner[0] for owner in settings.owners}
        if not allowed:
            raise HistoryError("History is not authorized for this conversation.")
        session_bot = getattr(session, "bot_uuid", bot_uuid)
        if session_bot not in (None, "", bot_uuid):
            raise HistoryError("Host identity mismatch.")
        return HistoryClient(settings, bot_uuid, launcher, launcher_type=kind)

    async def invoke(self, operation, params, session, query_id):
        try:
            client = await self.authorized_client(session, query_id)
            if not isinstance(params, dict):
                raise HistoryError("Tool parameters must be an object.")
            if operation == "groups":
                if set(params) - {"limit", "offset"}:
                    raise HistoryError("Unknown tool parameter.")
                result = await client.groups(params.get("limit", 50), params.get("offset", 0))
            elif operation == "messages":
                if set(params) - {"group", "limit", "offset", "start_time", "end_time"}:
                    raise HistoryError("Unknown tool parameter.")
                group = await client.resolve_group(params.get("group"))
                end = params.get("end_time", int(time.time()))
                start = params.get("start_time", end - 86400 if type(end) is int else None)
                result = await client.messages(group, params.get("limit", 50), params.get("offset", 0), start, end)
            else:
                raise HistoryError("Unknown history operation.")
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except HistoryError as exc:
            return "Error: " + str(exc)
        except Exception:
            return "Error: History is unavailable; no chat content was returned."
