import asyncio
import importlib
import importlib.util
from types import SimpleNamespace
from pathlib import Path

from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain, Plain
from langbot_plugin.api.entities.builtin.provider.message import Message


def listener(plugin):
    assert (Path(__file__).resolve().parents[1] / 'components/event_listener/chat_style.py').is_file(), 'Existing chats need live persona refresh'
    obj = importlib.import_module('components.event_listener.chat_style').ChatStyle()
    obj.plugin = plugin
    return obj


class Context:
    def __init__(self, event):
        self.event = event
        self.variables = {}

    async def set_query_var(self, key, value):
        self.variables[key] = value


def test_refreshes_cached_persona_without_erasing_history_or_memory(make_plugin):
    async def scenario():
        latest = '<!-- langbot-persona:dafeiyu-v3 -->\nCurrent style'
        plugin, _ = make_plugin({'persona_override': latest})
        history = [Message(role='user', content='Earlier conversation'), Message(role='system', content='Historical memory context')]
        event = SimpleNamespace(default_prompt=[Message(role='system', content='Base rules'),
            Message(role='system', content='<!-- langbot-persona:dafeiyu-v2 -->\nOld style')], prompt=history)
        ctx = Context(event)
        await listener(plugin).refresh_persona(ctx)
        assert event.default_prompt[0].content == 'Base rules'
        assert event.default_prompt[1].content == latest
        assert event.prompt is history
        assert ctx.variables['_dafeiyu_persona']['version'] == 'v3'
    asyncio.run(scenario())


def test_output_removes_whale_suffixes_and_preserves_images_and_code(make_plugin):
    async def scenario():
        plugin, _ = make_plugin({'strip_whale_suffixes': True})
        image = Image(base64='data:image/png;base64,AA==')
        event = SimpleNamespace(response_text='给你。 🐋\n\n好耶🐳！',
            reply_message_chain=MessageChain([Plain(text='给你。 🐋\n\n好耶🐳！'), image]))
        obj = listener(plugin)
        await obj.clean_response(Context(event))
        assert event.reply_message_chain[0].text == '给你。\n\n好耶！'
        assert event.reply_message_chain[1] is image
        clean = importlib.import_module('components.event_listener.chat_style').strip_whale_suffixes
        assert clean('鲸鱼🐋在游泳\n```text\n🐳\n```') == '鲸鱼🐋在游泳\n```text\n🐳\n```'
    asyncio.run(scenario())


def test_style_features_are_off_by_default(make_plugin):
    async def scenario():
        plugin, _ = make_plugin()
        ctx = Context(SimpleNamespace(default_prompt=[], prompt=[], response_text='你好 🐳', reply_message_chain=None))
        obj = listener(plugin)
        await obj.refresh_persona(ctx)
        await obj.clean_response(ctx)
        assert ctx.event.default_prompt == [] and ctx.event.reply_message_chain is None
    asyncio.run(scenario())


def test_rendered_media_keeps_host_original_delivery_chain(make_plugin):
    async def scenario():
        plugin, _ = make_plugin({'strip_whale_suffixes': True})
        for marker in ['[Image]', '[File]invoice.pdf']:
            event = SimpleNamespace(response_text='好了🐋\n' + marker, reply_message_chain=None)
            await listener(plugin).clean_response(Context(event))
            assert event.reply_message_chain is None
            assert marker in event.response_text
    asyncio.run(scenario())
