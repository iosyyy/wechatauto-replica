from __future__ import annotations

import re

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import events
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
from langbot_plugin.api.entities.builtin.provider.message import Message

PERSONA = re.compile(r'^<!-- langbot-persona:dafeiyu-(v\d+) -->')
WHALE_SUFFIX = re.compile(r'[ \t]*(?:[🐳🐋]\ufe0f?[ \t]*)+(?=[。！？!?.,，]*[ \t]*$)')


def strip_whale_suffixes(text):
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^\s*(```+|~~~+)', line)
        if marker:
            token = marker.group(1)[0]
            fence = None if fence == token else token if fence is None else fence
        if fence is None and not marker:
            ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
            body = line[:-len(ending)] if ending else line
            line = WHALE_SUFFIX.sub('', body) + ending
        lines.append(line)
    return ''.join(lines)


class ChatStyle(EventListener):
    def __init__(self):
        super().__init__()

        @self.handler(events.PromptPreProcessing)
        async def on_prompt(event_context):
            await self.refresh_persona(event_context)

        @self.handler(events.NormalMessageResponded)
        async def on_response(event_context):
            await self.clean_response(event_context)

    async def refresh_persona(self, ctx):
        persona = str(self.plugin.get_config().get('persona_override') or '')
        version = PERSONA.match(persona)
        if not version:
            return
        updated = []
        previous = []
        for item in ctx.event.default_prompt:
            content = getattr(item, 'content', '')
            tagged = PERSONA.match(content) if isinstance(content, str) else None
            if str(getattr(item, 'role', '')) == 'system' and tagged:
                previous.append(tagged.group(1))
                if len(previous) == 1:
                    updated.append(Message(role='system', content=persona))
            else:
                updated.append(item)
        if not previous:
            updated.append(Message(role='system', content=persona))
        ctx.event.default_prompt = updated
        try:
            await ctx.set_query_var('_dafeiyu_persona', {'version': version.group(1), 'previous': previous})
        except Exception:
            pass

    async def clean_response(self, ctx):
        if not self.plugin.get_config().get('strip_whale_suffixes', False):
            return
        event = ctx.event
        chain = event.reply_message_chain
        if chain is None:
            # The event exposes rendered text, not the original media bytes.
            if re.search(r'\[(?:Image|File|Voice|Video|WeChatEmoji|Unknown)\]', event.response_text):
                return
            cleaned = strip_whale_suffixes(event.response_text)
            if cleaned == event.response_text:
                return
            chain = MessageChain([Plain(text=cleaned)])
        else:
            chain = MessageChain([part.model_copy(update={'text': strip_whale_suffixes(part.text)})
                                  if isinstance(part, Plain) else part for part in chain])
        event.reply_message_chain = chain
        event.response_text = strip_whale_suffixes(event.response_text)
