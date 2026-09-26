"""Telegram inline 查询结果（URL 形式）

MTProto 下 inline 结果直接引用可直链 URL，不再需要 file_id 缓存结果与回退重试。
"""

from uuid import uuid4

from pyrogram import enums
from pyrogram.errors import RPCError
from pyrogram.types import (
    InlineQueryResultAnimation,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    InputTextMessageContent,
)

from ...model import ParsedContent
from ...provider.bilibili.api import referer_url
from ...utils import logger

INLINE_QUERY_EXPIRED_MARKERS = ("QUERY_ID_INVALID", "query is too old")


def _query_expired(err: Exception) -> bool:
    """Telegram 已放弃该 inline query（超时/失效），无需再回答"""
    text = str(err).lower()
    return any(marker.lower() in text for marker in INLINE_QUERY_EXPIRED_MARKERS)


async def answer_inline_query(client, inline_query, results) -> None:
    """回答 inline query；query 已失效时仅记录日志"""
    try:
        await client.answer_inline_query(inline_query.id, results, cache_time=0, is_personal=True)
    except RPCError as err:
        if _query_expired(err):
            logger.error(f"{err} -> Inline请求已失效")
            return
        logger.exception(err)
        raise


def build_media_inline_results(f: ParsedContent, caption: str) -> list:
    """构建 URL 形式的 inline 媒体结果"""
    media = f.media
    if media is None:
        return []
    if media.type == "video":
        return [_url_video_result(f, caption)]
    if media.type == "audio":
        return [_url_audio_result(f, caption)]
    return [_url_image_result(url, f, caption) for url in media.urls]


def _url_video_result(f: ParsedContent, caption: str) -> InlineQueryResultVideo:
    assert f.media is not None
    inline_video_url = f.media.fallback_url or f.media.urls[0]
    return InlineQueryResultVideo(
        id=uuid4().hex,
        video_url=referer_url(inline_video_url, f.url),
        thumb_url=f.media.thumbnail,
        title=f.media.title,
        description=f"{f.author.name}: {f.content}",
        mime_type="video/mp4",
        video_duration=f.media.duration,
        video_width=f.media.dimension.get("width", 0),
        video_height=f.media.dimension.get("height", 0),
        caption=caption,
        parse_mode=enums.ParseMode.HTML,
    )


def _url_audio_result(f: ParsedContent, caption: str) -> InlineQueryResultAudio:
    assert f.media is not None
    return InlineQueryResultAudio(
        id=uuid4().hex,
        audio_url=referer_url(f.media.urls[0], f.url),
        title=f.media.title,
        performer=f.author.name,
        audio_duration=f.media.duration,
        caption=caption,
        parse_mode=enums.ParseMode.HTML,
    )


def _url_image_result(mediaurl: str, f: ParsedContent, caption: str):
    if ".gif" in mediaurl:
        return InlineQueryResultAnimation(
            id=uuid4().hex,
            animation_url=mediaurl,
            thumb_url=mediaurl,
            title=f"{f.author.name}: {f.content}",
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
    return InlineQueryResultPhoto(
        id=uuid4().hex,
        photo_url=mediaurl + "@1280w.jpg",
        thumb_url=mediaurl + "@512w_512h.jpg",
        title=f.author.name,
        description=f.content,
        caption=caption,
        parse_mode=enums.ParseMode.HTML,
    )


def build_help_result(description: str, reply_markup) -> InlineQueryResultArticle:
    """帮助结果（无 query 或无法识别时返回）"""
    return InlineQueryResultArticle(
        id=uuid4().hex,
        title="帮助",
        description="将 Bot 添加到群组或频道可以自动匹配消息，Inline 模式只可发单张图",
        reply_markup=reply_markup,
        input_message_content=InputTextMessageContent(description, parse_mode=enums.ParseMode.HTML),
    )
