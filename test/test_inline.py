"""测试 Telegram inline 查询：URL 结果构建与 answer_inline_query 错误分支。

MTProto 下 inline 结果直接引用可直链 URL，不再有 file_id 缓存结果与回退重试。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyrogram import enums
from pyrogram.errors import RPCError
from pyrogram.types import (
    InlineQueryResultAnimation,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
)

from biliparser.channel.telegram.inline import (
    answer_inline_query,
    build_help_result,
    build_media_inline_results,
)
from biliparser.model import Author, MediaInfo, ParsedContent
from biliparser.provider.bilibili.api import referer_url

CONTENT_URL = "https://www.bilibili.com/video/BVxxx"


def _content(media: MediaInfo | None) -> ParsedContent:
    return ParsedContent(url=CONTENT_URL, author=Author(name="author"), content="hi", media=media)


# ---------------------------------------------------------------------------
# build_media_inline_results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_none_returns_empty_list():
    assert build_media_inline_results(_content(None), "caption") == []


def test_video_result_uses_fallback_url():
    media = MediaInfo(
        urls=["https://cdn.invalid/v.m4s", "https://cdn.invalid/a.m4s"],
        type="video",
        merge_streams=True,
        title="title",
        fallback_url="https://cdn.invalid/v.mp4",
        thumbnail="https://cdn.invalid/t.jpg",
        duration=42,
        dimension={"width": 1920, "height": 1080},
    )

    results = build_media_inline_results(_content(media), "caption")

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultVideo)
    assert result.video_url == referer_url(media.fallback_url, CONTENT_URL)
    assert result.thumb_url == media.thumbnail
    assert result.title == "title"
    assert result.mime_type == "video/mp4"
    assert result.video_duration == 42
    assert result.video_width == 1920
    assert result.video_height == 1080
    assert result.caption == "caption"
    assert result.parse_mode == enums.ParseMode.HTML


def test_video_result_without_fallback_uses_first_url():
    media = MediaInfo(urls=["https://cdn.invalid/v.m4s"], type="video", title="title")

    result = build_media_inline_results(_content(media), "caption")[0]

    assert isinstance(result, InlineQueryResultVideo)
    assert result.video_url == referer_url(media.urls[0], CONTENT_URL)


def test_audio_result():
    media = MediaInfo(urls=["https://cdn.invalid/a.m4s"], type="audio", title="song", duration=30)

    results = build_media_inline_results(_content(media), "caption")

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultAudio)
    assert result.audio_url == referer_url(media.urls[0], CONTENT_URL)
    assert result.title == "song"
    assert result.performer == "author"
    assert result.audio_duration == 30
    assert result.parse_mode == enums.ParseMode.HTML


def test_image_results_append_size_suffixes():
    urls = ["https://i.example.com/1.jpg", "https://i.example.com/2.jpg"]
    media = MediaInfo(urls=urls, type="image")

    results = build_media_inline_results(_content(media), "caption")

    assert len(results) == 2
    for url, result in zip(urls, results, strict=True):
        assert isinstance(result, InlineQueryResultPhoto)
        assert result.photo_url == f"{url}@1280w.jpg"
        assert result.thumb_url == f"{url}@512w_512h.jpg"
        assert result.caption == "caption"
        assert result.parse_mode == enums.ParseMode.HTML


def test_gif_url_becomes_animation():
    url = "https://i.example.com/a.gif"
    media = MediaInfo(urls=[url], type="image")

    results = build_media_inline_results(_content(media), "caption")

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultAnimation)
    assert result.animation_url == url
    assert result.thumb_url == url
    assert result.parse_mode == enums.ParseMode.HTML


def test_inline_result_ids_are_unique():
    media = MediaInfo(urls=["https://i.example.com/1.jpg", "https://i.example.com/2.jpg"], type="image")

    results = build_media_inline_results(_content(media), "caption")

    assert len({result.id for result in results}) == 2


# ---------------------------------------------------------------------------
# build_help_result
# ---------------------------------------------------------------------------


def test_build_help_result_embeds_description_and_markup():
    markup = MagicMock()

    result = build_help_result("描述文本", markup)

    assert isinstance(result, InlineQueryResultArticle)
    assert result.title == "帮助"
    assert result.reply_markup is markup
    assert result.input_message_content.message_text == "描述文本"
    assert result.input_message_content.parse_mode == enums.ParseMode.HTML


# ---------------------------------------------------------------------------
# answer_inline_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_inline_query_passes_results():
    client = MagicMock()
    client.answer_inline_query = AsyncMock()
    inline_query = SimpleNamespace(id="query-id")
    results = [SimpleNamespace(id="a")]

    await answer_inline_query(client, inline_query, results)

    client.answer_inline_query.assert_awaited_once_with("query-id", results, cache_time=0, is_personal=True)


@pytest.mark.asyncio
async def test_answer_inline_query_expired_only_logs():
    client = MagicMock()
    client.answer_inline_query = AsyncMock(side_effect=RPCError("QUERY_ID_INVALID: query is too old"))
    inline_query = SimpleNamespace(id="query-id")

    await answer_inline_query(client, inline_query, [object()])

    client.answer_inline_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_inline_query_other_rpc_error_reraises():
    client = MagicMock()
    client.answer_inline_query = AsyncMock(side_effect=RPCError("FLOOD_WAIT_5"))
    inline_query = SimpleNamespace(id="query-id")

    with pytest.raises(RPCError, match="FLOOD_WAIT_5"):
        await answer_inline_query(client, inline_query, [object()])
