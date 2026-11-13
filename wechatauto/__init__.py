"""wechatauto —— Windows版本微信客户端（非网页版）自动化。

基于 UIAutomation 技术驱动当前微信4.x客户端，可实现简单的发送、
接收微信消息，编写简单的微信机器人。
"""

from __future__ import annotations

from importlib import import_module
import sys


# 读取数据库不能顺带加载 Windows 窗口、鼠标或剪贴板驱动。
_EXPORT_GROUPS = {
    ".wx": ("WeChat", "Chat", "Listener"),
    ".param": ("WxParam", "WxResponse", "PROJECT_NAME"),
    ".logger": ("wxlog",),
    ".moment": ("Moment", "MomentDB"),
    ".moment_observer": ("MomentObserver",),
    ".db": ("WeChatDB", "GroupMemberWatcher", "auto_detect_db_dir", "list_accounts"),
    ".media": ("MediaDownloader",),
    ".recall": ("RecallGuard",),
    ".guia": ("WeChatGUI", "quick_send", "quick_send_file", "quick_send_image",
              "quick_reply", "WinInput", "ScreenOCR"),
    ".exceptions": ("NetWorkError", "WechatautoError", "WechatautoNoteLoadTimeoutError",
                    "WechatautoUINotFoundError", "WechatautoNotLoggedInError"),
    ".utils.lock": ("LockManager", "uilock"),
    ".msgs": ("Message", "BaseMessage", "HumanMessage", "TextMessage", "ImageMessage",
              "VideoMessage", "VoiceMessage", "FileMessage", "QuoteMessage",
              "LinkMessage", "LocationMessage", "PersonalCardMessage", "OtherMessage",
              "SystemMessage", "FriendMessage", "SelfMessage", "parse_msg"),
}
_EXPORTS = {name: module for module, names in _EXPORT_GROUPS.items() for name in names}
_PORTABLE_MODULES = {".db", ".param", ".logger", ".exceptions", ".rhythm"}


def __getattr__(name):
    """保留公开名称，按需加载其原始模块。"""
    module_name = ".rhythm" if name == "rhythm" else _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    if sys.platform != "win32" and module_name not in _PORTABLE_MODULES:
        raise RuntimeError("%s 需要 Windows GUI 后端；Linux 当前仅提供数据库读取接口" % name)
    module = import_module(module_name, __name__)
    value = module if name == "rhythm" else getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

__version__ = "1.2.10+iosyyy.linux.1"

__all__ = [
    "WeChat",
    "Chat",
    "Listener",
    "WeChatDB",
    "GroupMemberWatcher",
    "auto_detect_db_dir",
    "list_accounts",
    "MediaDownloader",
    "RecallGuard",
    "WeChatGUI",
    "quick_send",
    "quick_send_file",
    "quick_send_image",
    "quick_reply",
    "WinInput",
    "ScreenOCR",
    "WxParam",
    "WxResponse",
    "wxlog",
    "Moment",
    "MomentDB",
    "MomentObserver",
    "rhythm",
    "LockManager",
    "uilock",
    "WechatautoError",
    "NetWorkError",
    "WechatautoUINotFoundError",
    "WechatautoNoteLoadTimeoutError",
    "WechatautoNotLoggedInError",
    "Message",
    "BaseMessage",
    "HumanMessage",
    "TextMessage",
    "ImageMessage",
    "VideoMessage",
    "VoiceMessage",
    "FileMessage",
    "QuoteMessage",
    "LinkMessage",
    "LocationMessage",
    "PersonalCardMessage",
    "OtherMessage",
    "SystemMessage",
    "FriendMessage",
    "SelfMessage",
    "parse_msg",
    "PROJECT_NAME",
    "__version__",
]
