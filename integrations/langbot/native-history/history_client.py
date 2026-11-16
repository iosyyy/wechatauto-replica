"""Bounded transport with owner-private and current-group history scopes."""

from dataclasses import dataclass, field
import ipaddress
import json
import re
from urllib.parse import urlsplit

import httpx


PRIVATE_DOCKER_HOST = "wechatauto-linux-desktop-1"


class HistoryError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    server_url: str
    token: str = field(repr=False)
    owners: tuple


def settings_from_config(config):
    try:
        server = config["server_url"]
        parsed = urlsplit(server)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError()
        if parsed.scheme == "http":
            if parsed.hostname == PRIVATE_DOCKER_HOST:
                if config.get("allow_private_http") is not True:
                    raise ValueError()
            elif parsed.hostname != "localhost":
                address = ipaddress.ip_address(parsed.hostname)
                if not address.is_loopback and not (
                        address.is_private and config.get("allow_private_http") is True):
                    raise ValueError()
        token = config["api_token"]
        if not isinstance(token, str) or not 32 <= len(token.encode("utf-8")) <= 4096:
            raise ValueError()
        if any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError()
        owners = json.loads(config["owners_json"])
        if not isinstance(owners, list) or not 1 <= len(owners) <= 5:
            raise ValueError()
        normalized = []
        for owner in owners:
            if not isinstance(owner, dict) or set(owner) != {"bot_uuid", "launcher_id"}:
                raise ValueError()
            pair = tuple(owner[key] for key in ("bot_uuid", "launcher_id"))
            if any(not isinstance(item, str) or not item or len(item) > 256
                   or item != item.strip() or any(ord(char) < 32 for char in item) for item in pair):
                raise ValueError()
            normalized.append(pair)
        return Settings(server.rstrip("/"), token, tuple(normalized))
    except Exception:
        raise HistoryError("History plugin configuration is incomplete or invalid.") from None


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise HistoryError("History pagination or time range exceeds the allowed bounds.")
    return value


def is_group_username(value):
    return (isinstance(value, str) and len(value) <= 256
            and re.fullmatch(r"[A-Za-z0-9_.-]+@chatroom", value) is not None)


class HistoryClient:
    def __init__(self, settings, bot_uuid, launcher, *, launcher_type="person"):
        if launcher_type not in ("person", "group") or (launcher_type == "group" and not is_group_username(launcher)):
            raise HistoryError("Invalid host conversation binding.")
        self.settings = settings
        self.bot_uuid = bot_uuid
        self.launcher = launcher
        self.launcher_type = launcher_type

    async def _request(self, resource, body):
        headers = {"Authorization": "Bearer " + self.settings.token,
                   "X-LangBot-Bot-UUID": self.bot_uuid, "X-LangBot-Launcher-ID": self.launcher,
                   "X-LangBot-Launcher-Type": self.launcher_type}
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                async with client.stream("POST", self.settings.server_url + "/native-history/v1/" + resource,
                                         headers=headers, json=body) as response:
                    if response.status_code != 200:
                        raise HistoryError("Native history request failed (HTTP %d)." % response.status_code)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise HistoryError("Native history response exceeded the size bound.")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            if self.launcher_type == "group":
                if resource == "groups":
                    groups = result.get("groups")
                    if (not isinstance(groups, list) or len(groups) > 1 or result.get("has_more") is not False
                            or result.get("next_offset") is not None
                            or any(not isinstance(row, dict) or row.get("username") != self.launcher for row in groups)):
                        raise HistoryError("Native history returned a different conversation scope.")
                elif result.get("group_username") != self.launcher:
                    raise HistoryError("Native history returned a different conversation scope.")
            return result
        except HistoryError:
            raise
        except Exception:
            raise HistoryError("Native history transport is unavailable.") from None

    async def groups(self, limit=50, offset=0):
        return await self._request("groups", {"limit": _integer(limit, 1, 50),
                                               "offset": _integer(offset, 0, 1000)})

    async def resolve_group(self, value):
        if self.launcher_type == "group":
            if value is not None and value != self.launcher:
                raise HistoryError("Group requests can read only the current group's exact username; omit group to use it.")
            return self.launcher
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
            raise HistoryError("An exact group username or exact group name is required.")
        if value.endswith("@chatroom"):
            return value
        matches, offset = [], 0
        for _ in range(3):
            result = await self.groups(50, offset)
            groups = result.get("groups")
            if not isinstance(groups, list) or len(groups) > 50:
                raise HistoryError("Invalid native group-list response.")
            matches.extend(row for row in groups if isinstance(row, dict) and row.get("name") == value)
            following = result.get("next_offset")
            if not result.get("has_more"):
                break
            if type(following) is not int or not offset < following <= 1000:
                raise HistoryError("Group list was truncated; use an exact group username.")
            offset = following
        else:
            raise HistoryError("Group list was truncated; use an exact group username.")
        if not matches:
            raise HistoryError("No authorized local group has that exact name. List groups first; the bot may need an invitation or allowlist update.")
        if len(matches) != 1:
            raise HistoryError("Group name is ambiguous; ask the owner to choose an exact username: "
                               + ", ".join(str(row.get("username")) for row in matches))
        return matches[0]["username"]

    async def messages(self, group, limit, offset, start, end):
        if self.launcher_type == "group" and group != self.launcher:
            raise HistoryError("Group requests cannot read another group's history.")
        _integer(start, 0, 2 ** 63 - 1)
        _integer(end, 0, 2 ** 63 - 1)
        if not start < end or end - start > 604800:
            raise HistoryError("Choose a time range greater than zero and no longer than seven days.")
        return await self._request("messages", {"group_username": group, "limit": _integer(limit, 1, 50),
                                                  "offset": _integer(offset, 0, 1000),
                                                  "start_time": start, "end_time": end})
