"""测试 BV 号检测链路：从 Telegram 消息文本到 URL 提取、自身转发过滤、provider 路由。

验证 https://www.bilibili.com/video/BV1zvQbBkEcG?spm_id_from=... 和裸 BV1zvQbBkEcG
都能命中处理逻辑。
"""

import datetime
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyrogram import enums
from pyrogram.types import Chat, MessageOriginChannel, MessageOriginHiddenUser, MessageOriginUser, User

from biliparser.channel.telegram.bot import (
    BILIBILI_URL_REGEX,
    BotContext,
    message_to_urls,
    message_to_urls_sync,
    parse,
)
from biliparser.provider import ProviderRegistry
from biliparser.provider.bilibili import BilibiliProvider

BOT_USERNAME = "testbot"
BOT_FIRST_NAME = "TestBot"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_message(text: str, entities=None, caption=None, caption_entities=None) -> SimpleNamespace:
    """构造一个最小化的 Message fake。"""
    return SimpleNamespace(
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=caption_entities or [],
        chat=SimpleNamespace(id=123, type=enums.ChatType.PRIVATE),
        from_user=SimpleNamespace(id=456, is_bot=False, first_name="Test"),
        forward_origin=None,
        message_id=1,
        reply_text=AsyncMock(),
    )


def _origin_user(username: str = BOT_USERNAME, is_bot: bool = True):
    return MessageOriginUser(
        type=enums.MessageOriginType.USER,
        date=datetime.datetime.now(),
        sender_user=User(id=999, is_bot=is_bot, first_name="TestBot", username=username),
    )


def _origin_hidden_user(name: str):
    return MessageOriginHiddenUser(
        type=enums.MessageOriginType.HIDDEN_USER, date=datetime.datetime.now(), sender_user_name=name
    )


def _origin_channel(author_signature: str | None = None):
    return MessageOriginChannel(
        type=enums.MessageOriginType.CHANNEL,
        date=datetime.datetime.now(),
        chat=Chat(id=-100123, type=enums.ChatType.CHANNEL, title="Test Channel"),
        message_id=1,
        author_signature=author_signature,
    )


def _make_client(member_status=enums.ChatMemberStatus.MEMBER) -> MagicMock:
    client = MagicMock()
    client.me = SimpleNamespace(username=BOT_USERNAME, first_name=BOT_FIRST_NAME)
    client.get_me = AsyncMock(return_value=client.me)
    client.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=member_status))
    client.send_chat_action = AsyncMock()
    return client


def _make_ctx(registry=None, channel=None, queue_manager=None) -> BotContext:
    return BotContext(
        registry=registry if registry is not None else MagicMock(spec=ProviderRegistry),
        channel=channel if channel is not None else MagicMock(),
        queue_manager=queue_manager if queue_manager is not None else MagicMock(submit=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# 1. BILIBILI_URL_REGEX 匹配测试
# ---------------------------------------------------------------------------


class TestBilibiliUrlRegex:
    """验证 BILIBILI_URL_REGEX 能匹配各种 bilibili URL 和裸 BV 号。"""

    def test_full_url_with_params(self):
        text = "https://www.bilibili.com/video/BV1zvQbBkEcG?spm_id_from=333.1007.tianma.1-2-2.click"
        assert re.findall(BILIBILI_URL_REGEX, text) == [text]

    def test_bare_bv(self):
        assert re.findall(BILIBILI_URL_REGEX, "BV1zvQbBkEcG") == ["BV1zvQbBkEcG"]

    def test_bare_bv_in_sentence(self):
        text = "看看这个 BV1zvQbBkEcG 视频"
        assert re.findall(BILIBILI_URL_REGEX, text) == ["BV1zvQbBkEcG"]

    def test_bare_bv_no_space(self):
        text = "推荐BV1zvQbBkEcG不错"
        assert re.findall(BILIBILI_URL_REGEX, text) == ["BV1zvQbBkEcG"]

    def test_url_and_bare_bv_coexist(self):
        text = "https://www.bilibili.com/video/BV1zvQbBkEcG 还有 BV1Y25Nz4EZ3"
        urls = re.findall(BILIBILI_URL_REGEX, text)
        assert len(urls) == 2

    def test_b23_short_link(self):
        text = "https://b23.tv/xZCcov"
        assert re.findall(BILIBILI_URL_REGEX, text)

    def test_no_match_random_text(self):
        assert re.findall(BILIBILI_URL_REGEX, "hello world") == []


# ---------------------------------------------------------------------------
# 2. message_to_urls 自身转发过滤测试
# ---------------------------------------------------------------------------


class TestForwardFiltering:
    """验证 Bot 自己转发出去的消息不会被再次解析。"""

    @pytest.mark.asyncio
    async def test_own_forward_from_bot_filtered(self):
        client = _make_client()
        msg = _make_message("BV1zvQbBkEcG")
        msg.forward_origin = _origin_user(username=BOT_USERNAME)
        result_msg, urls = await message_to_urls(client, msg)
        assert result_msg is msg
        assert urls == []

    @pytest.mark.asyncio
    async def test_forward_from_other_bot_kept(self):
        client = _make_client()
        msg = _make_message("BV1zvQbBkEcG")
        msg.forward_origin = _origin_user(username="otherbot")
        _result_msg, urls = await message_to_urls(client, msg)
        assert urls == ["BV1zvQbBkEcG"]

    @pytest.mark.asyncio
    async def test_forward_sender_name_matching_bot_filtered(self):
        client = _make_client()
        msg = _make_message("BV1zvQbBkEcG")
        msg.forward_origin = _origin_hidden_user(BOT_FIRST_NAME)
        _result_msg, urls = await message_to_urls(client, msg)
        assert urls == []

    @pytest.mark.asyncio
    async def test_forward_sender_name_other_kept(self):
        client = _make_client()
        msg = _make_message("BV1zvQbBkEcG")
        msg.forward_origin = _origin_hidden_user("Someone Else")
        _result_msg, urls = await message_to_urls(client, msg)
        assert urls == ["BV1zvQbBkEcG"]

    @pytest.mark.asyncio
    async def test_forward_from_chat_where_bot_is_admin_filtered(self):
        client = _make_client(member_status=enums.ChatMemberStatus.ADMINISTRATOR)
        msg = _make_message("BV1zvQbBkEcG")
        msg.forward_origin = _origin_channel()
        _result_msg, urls = await message_to_urls(client, msg)
        assert urls == []
        client.get_chat_member.assert_awaited_once_with(-100123, "me")

    @pytest.mark.asyncio
    async def test_forward_from_chat_where_bot_is_member_kept(self):
        client = _make_client(member_status=enums.ChatMemberStatus.MEMBER)
        msg = _make_message("BV1zvQbBkEcG")
        msg.forward_origin = _origin_channel()
        _result_msg, urls = await message_to_urls(client, msg)
        assert urls == ["BV1zvQbBkEcG"]

    @pytest.mark.asyncio
    async def test_forward_signature_matching_bot_filtered(self):
        client = _make_client()
        msg = _make_message("BV1zvQbBkEcG")
        msg.forward_origin = _origin_channel(author_signature=BOT_FIRST_NAME)
        _result_msg, urls = await message_to_urls(client, msg)
        assert urls == []
        client.get_chat_member.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_message(self):
        client = _make_client()
        result_msg, urls = await message_to_urls(client, None)
        assert result_msg is None
        assert urls == []
        client.get_me.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. message_to_urls_sync 提取测试
# ---------------------------------------------------------------------------


class TestMessageToUrlsSync:
    def test_extract_full_url(self):
        msg = _make_message("https://www.bilibili.com/video/BV1zvQbBkEcG?spm_id_from=333.1007.tianma.1-2-2.click")
        urls = message_to_urls_sync(msg)
        assert len(urls) == 1
        assert "BV1zvQbBkEcG" in urls[0]

    def test_extract_bare_bv(self):
        msg = _make_message("BV1zvQbBkEcG")
        assert message_to_urls_sync(msg) == ["BV1zvQbBkEcG"]

    def test_extract_bare_bv_in_sentence(self):
        msg = _make_message("看看这个 BV1zvQbBkEcG 视频")
        assert message_to_urls_sync(msg) == ["BV1zvQbBkEcG"]

    def test_extract_from_entity_url(self):
        """TEXT_LINK entity 的 url 属性也应被提取。"""
        entity = SimpleNamespace(url="https://www.bilibili.com/video/BV1zvQbBkEcG")
        msg = _make_message("点击这里", entities=[entity])
        urls = message_to_urls_sync(msg)
        assert any("BV1zvQbBkEcG" in u for u in urls)

    def test_no_match(self):
        msg = _make_message("hello world")
        assert message_to_urls_sync(msg) == []


# ---------------------------------------------------------------------------
# 4. message_to_urls (async) 提取测试
# ---------------------------------------------------------------------------


class TestMessageToUrls:
    @pytest.mark.asyncio
    async def test_extract_full_url(self):
        msg = _make_message("https://www.bilibili.com/video/BV1zvQbBkEcG?spm_id_from=333.1007.tianma.1-2-2.click")
        client = _make_client()
        result_msg, urls = await message_to_urls(client, msg)
        assert result_msg is msg
        assert len(urls) == 1
        assert "BV1zvQbBkEcG" in urls[0]

    @pytest.mark.asyncio
    async def test_extract_bare_bv(self):
        msg = _make_message("BV1zvQbBkEcG")
        client = _make_client()
        result_msg, urls = await message_to_urls(client, msg)
        assert result_msg is msg
        assert urls == ["BV1zvQbBkEcG"]

    @pytest.mark.asyncio
    async def test_extract_bare_bv_in_sentence(self):
        msg = _make_message("看看这个 BV1zvQbBkEcG 视频不错")
        client = _make_client()
        _result_msg, urls = await message_to_urls(client, msg)
        assert urls == ["BV1zvQbBkEcG"]


# ---------------------------------------------------------------------------
# 5. BilibiliProvider.can_handle 路由测试
# ---------------------------------------------------------------------------


class TestProviderCanHandle:
    def setup_method(self):
        self.p = BilibiliProvider()

    def test_full_url(self):
        assert self.p.can_handle("https://www.bilibili.com/video/BV1zvQbBkEcG?spm_id_from=333.1007.tianma.1-2-2.click")

    def test_bare_bv(self):
        assert self.p.can_handle("BV1zvQbBkEcG")

    def test_bare_bv_lowercase(self):
        """_BILIBILI_RE 使用 IGNORECASE，小写 bv 也应匹配。"""
        assert self.p.can_handle("bv1zvQbBkEcG")

    def test_youtube_rejected(self):
        assert not self.p.can_handle("https://youtube.com/watch?v=abc")


# ---------------------------------------------------------------------------
# 6. ProviderRegistry 路由测试 — 裸 BV 号不应被静默丢弃
# ---------------------------------------------------------------------------


class TestRegistryRouting:
    def setup_method(self):
        self.registry = ProviderRegistry()
        self.registry.register(BilibiliProvider())

    def test_find_provider_full_url(self):
        assert self.registry.find_provider("https://www.bilibili.com/video/BV1zvQbBkEcG") is not None

    def test_find_provider_bare_bv(self):
        assert self.registry.find_provider("BV1zvQbBkEcG") is not None

    def test_find_provider_bare_bv_lowercase(self):
        assert self.registry.find_provider("bv1zvQbBkEcG") is not None

    def test_find_provider_unrelated(self):
        assert self.registry.find_provider("https://youtube.com/watch") is None


# ---------------------------------------------------------------------------
# 7. _route 路由测试 — 裸 BV 号应命中 Video 策略
# ---------------------------------------------------------------------------


class TestRouteBareBV:
    """验证 _route 函数对裸 BV 号的路由正确性。"""

    def test_route_regex_matches_bare_bv(self):
        """_route 内部的正则应匹配裸 BV 号。"""
        route_re = r"(?:^|/)(?:BV\w{10}|av\d+|ep\d+|ss\d+)"
        assert re.search(route_re, "BV1zvQbBkEcG")

    def test_route_regex_matches_bv_in_path(self):
        route_re = r"(?:^|/)(?:BV\w{10}|av\d+|ep\d+|ss\d+)"
        assert re.search(route_re, "b23.tv/BV1zvQbBkEcG")

    def test_startswith_check_uppercase_bv(self):
        """BilibiliProvider.parse 的 startswith 检查：大写 BV 应直接传递，不加 http://。"""
        url = "BV1zvQbBkEcG"
        result = f"http://{url}" if not url.startswith(("http:", "https:", "av", "BV")) else url
        assert result == "BV1zvQbBkEcG"

    def test_startswith_check_lowercase_bv_gets_http(self):
        """小写 bv 不在 startswith 列表中，会被加上 http:// 前缀。"""
        url = "bv1zvQbBkEcG"
        result = f"http://{url}" if not url.startswith(("http:", "https:", "av", "BV")) else url
        assert result == "http://bv1zvQbBkEcG"

    def test_route_regex_no_match_lowercase_bv_with_http(self):
        """http://bv... 不会被 _route 的正则匹配到（正则区分大小写），会走 redirect 逻辑。"""
        route_re = r"(?:^|/)(?:BV\w{10}|av\d+|ep\d+|ss\d+)"
        assert not re.search(route_re, "http://bv1zvQbBkEcG")

    def test_full_chain_uppercase_bv(self):
        """完整链路：大写 BV 号从 startswith 到 _route 到 Video.handle 正则全部通过。"""
        url = "BV1zvQbBkEcG"
        # Step 1: startswith — 大写 BV 直接传递
        processed = f"http://{url}" if not url.startswith(("http:", "https:", "av", "BV")) else url
        assert processed == "BV1zvQbBkEcG"
        # Step 2: _route 正则匹配
        route_re = r"(?:^|/)(?:BV\w{10}|av\d+|ep\d+|ss\d+)"
        assert re.search(route_re, processed)
        # Step 3: Video URL 构造
        video_url = processed if "/" in processed else f"b23.tv/{processed}"
        assert video_url == "b23.tv/BV1zvQbBkEcG"
        # Step 4: Video.handle 内部正则
        video_re = r"(?:bilibili\.com(?:/video|/bangumi/play)?|b23\.tv|acg\.tv)/(?:(?P<bvid>BV\w{10})|av(?P<aid>\d+)|ep(?P<epid>\d+)|ss(?P<ssid>\d+)|)"
        m = re.search(video_re, video_url)
        assert m and m.group("bvid") == "BV1zvQbBkEcG"


# ---------------------------------------------------------------------------
# 8. 端到端：parse handler 对裸 BV 号的处理
# ---------------------------------------------------------------------------


class TestParseHandlerBareBV:
    """验证 parse handler 对裸 BV 号消息的完整处理链路。"""

    @pytest.mark.asyncio
    async def test_parse_handler_receives_bare_bv(self, monkeypatch):
        """模拟一条含裸 BV 号的消息，验证 parse handler 能提取 URL 并调用 registry.parse。"""
        monkeypatch.setattr("biliparser.channel.telegram.bot.check_message_request_limit", AsyncMock(return_value=True))
        registry = MagicMock(spec=ProviderRegistry)
        registry.parse = AsyncMock(return_value=[])
        ctx = _make_ctx(registry=registry)
        client = _make_client()
        msg = _make_message("BV1zvQbBkEcG")

        await parse(ctx, client, msg)

        registry.parse.assert_awaited_once()
        urls = registry.parse.await_args.args[0]
        assert urls == ["BV1zvQbBkEcG"]

    @pytest.mark.asyncio
    async def test_parse_handler_receives_full_url(self, monkeypatch):
        """模拟一条含完整 bilibili URL 的消息，验证 parse handler 能提取并调用 registry.parse。"""
        monkeypatch.setattr("biliparser.channel.telegram.bot.check_message_request_limit", AsyncMock(return_value=True))
        full_url = "https://www.bilibili.com/video/BV1zvQbBkEcG?spm_id_from=333.1007.tianma.1-2-2.click"
        registry = MagicMock(spec=ProviderRegistry)
        registry.parse = AsyncMock(return_value=[])
        ctx = _make_ctx(registry=registry)
        client = _make_client()
        msg = _make_message(full_url)

        await parse(ctx, client, msg)

        registry.parse.assert_awaited_once()
        urls = registry.parse.await_args.args[0]
        assert any("BV1zvQbBkEcG" in u for u in urls)

    @pytest.mark.asyncio
    async def test_parse_handler_bv_in_sentence(self, monkeypatch):
        """消息文本中夹杂 BV 号也应被提取。"""
        monkeypatch.setattr("biliparser.channel.telegram.bot.check_message_request_limit", AsyncMock(return_value=True))
        registry = MagicMock(spec=ProviderRegistry)
        registry.parse = AsyncMock(return_value=[])
        ctx = _make_ctx(registry=registry)
        client = _make_client()
        msg = _make_message("看看这个 BV1zvQbBkEcG 视频")

        await parse(ctx, client, msg)

        registry.parse.assert_awaited_once()
        urls = registry.parse.await_args.args[0]
        assert urls == ["BV1zvQbBkEcG"]

    @pytest.mark.asyncio
    async def test_parse_handler_does_not_queue_when_no_result(self, monkeypatch):
        """registry.parse 返回空列表时不创建上传任务。"""
        monkeypatch.setattr("biliparser.channel.telegram.bot.check_message_request_limit", AsyncMock(return_value=True))
        registry = MagicMock(spec=ProviderRegistry)
        registry.parse = AsyncMock(return_value=[])
        queue_manager = MagicMock(submit=AsyncMock())
        ctx = _make_ctx(registry=registry, queue_manager=queue_manager)
        client = _make_client()
        msg = _make_message("BV1zvQbBkEcG")

        await parse(ctx, client, msg)

        queue_manager.submit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 9. ProviderRegistry.parse 异常处理 — 不应 raise，应返回在列表中
# ---------------------------------------------------------------------------


class TestRegistryExceptionHandling:
    @pytest.mark.asyncio
    async def test_provider_exception_returned_not_raised(self):
        """Provider.parse 抛异常时，ProviderRegistry.parse 应将其作为列表元素返回，而非 raise。"""

        class FailingProvider(BilibiliProvider):
            async def parse(self, urls, constraints, extra=None):
                raise RuntimeError("模拟解析失败")

        registry = ProviderRegistry()
        registry.register(FailingProvider())

        from biliparser.model import MediaConstraints

        mc = MediaConstraints(
            max_upload_size=50 * 1024 * 1024,
            max_download_size=2 * 1024 * 1024 * 1024,
            caption_max_length=1024,
        )

        # 不应 raise
        results = await registry.parse(["BV1zvQbBkEcG"], mc)
        assert len(results) == 1
        assert isinstance(results[0], Exception)
