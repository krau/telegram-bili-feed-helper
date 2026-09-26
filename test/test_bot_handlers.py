"""测试 bot handler 路由：命令与普通消息不重复匹配"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from pyrogram import enums
from pyrogram.client import Client
from pyrogram.handlers import MessageHandler

import biliparser.channel.telegram.bot as bot


def _message(text=None, caption=None):
    return SimpleNamespace(text=text, caption=caption, chat=SimpleNamespace(type=enums.ChatType.PRIVATE))


def _build_client() -> MagicMock:
    """只记录 handler 注册，filters 逻辑仍由 pyrogram 提供"""
    client = MagicMock(spec=Client)
    client.me = SimpleNamespace(username="testbot", first_name="TestBot")
    ctx = bot.BotContext(registry=MagicMock(), channel=MagicMock())
    bot.add_handlers(client, ctx)
    return client


async def _matching_message_handlers(client: MagicMock, message) -> int:
    handlers = [call.args[0] for call in client.add_handler.call_args_list]
    count = 0
    for handler in handlers:
        if not isinstance(handler, MessageHandler) or handler.filters is None:
            continue
        if await handler.filters(client, message):
            count += 1
    return count


async def test_command_matches_single_handler():
    """/parse <url> 只能命中命令 handler，不能同时命中普通消息 handler"""
    client = _build_client()
    assert await _matching_message_handlers(client, _message("/parse https://b23.tv/xxxxxxx")) == 1


async def test_video_command_matches_single_handler():
    client = _build_client()
    assert await _matching_message_handlers(client, _message("/video 720P BV1Y25Nz4EZ3")) == 1


async def test_plain_url_matches_catch_all():
    client = _build_client()
    assert await _matching_message_handlers(client, _message("https://b23.tv/xxxxxxx")) == 1


async def test_caption_url_matches_catch_all():
    client = _build_client()
    assert await _matching_message_handlers(client, _message(caption="看这个 BV1Y25Nz4EZ3")) == 1


async def test_other_commands_match_single_handler():
    client = _build_client()
    for text in ("/file https://b23.tv/x", "/cover https://b23.tv/x", "/login", "/tasks", "/cancel"):
        assert await _matching_message_handlers(client, _message(text)) == 1, text


async def test_inline_handler_registered():
    client = _build_client()
    handlers = [call.args[0] for call in client.add_handler.call_args_list]
    assert any(type(handler).__name__ == "InlineQueryHandler" for handler in handlers)
