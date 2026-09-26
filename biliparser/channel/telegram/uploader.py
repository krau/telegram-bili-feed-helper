"""Telegram 上传逻辑（MTProto / Kurigram）

TelegramUploadTask: 在基类 UploadTask 上增加 message: Message 字段
TelegramUploadQueueManager: 实现 _do_upload/_do_cache/_handle_upload_error
"""

import asyncio
import contextlib
import io
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyrogram import enums
from pyrogram.errors import (
    ChannelPrivate,
    ChatAdminRequired,
    ChatForbidden,
    ChatWriteForbidden,
    FloodWait,
    PeerIdInvalid,
    RPCError,
    TopicClosed,
    TopicDeleted,
    UserIsBlocked,
)
from pyrogram.types import (
    Document,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from ...model import ParsedContent
from ...storage.models import TelegramFileCache
from ...uploader.download import cleanup_medias
from ...uploader.queue import MediaUnavailable, UploadPhase, UploadQueueManager, UploadTask
from ...utils import logger
from .formatting import format_caption_for_telegram

BILIBILI_SHARE_URL_REGEX = r"(?i)【.*】 https://[\w\.]*?(?:bilibili\.com|b23\.tv|bili2?2?3?3?\.cn)\S+"

DOCUMENT_CACHE_PREFIX = "document:"

# 发送这些异常说明请求已被拒绝，重新下载媒体后重试是安全的
_REJECTED_ERRORS = (ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UserIsBlocked)
_FORBIDDEN_ERRORS = (ChatForbidden, ChatWriteForbidden)
_TERMINAL_ERRORS = (TopicClosed, TopicDeleted)


def cache_key_for_attachment(filename: str, attachment) -> str:
    """Document 使用独立键，避免覆盖 Video/Photo 等原生媒体缓存。"""
    if isinstance(attachment, Document):
        return f"{DOCUMENT_CACHE_PREFIX}{filename}"
    return filename


class NeutralMimeFile(io.BufferedReader):
    """以中性后缀向 Pyrogram 暴露文件名，使 mime 推断为 application/octet-stream。

    Telegram 相册不允许 document 与 photo/video 混排，而 .mp4/.mp3 按后缀会被判定为
    video/audio；Pyrogram 相册分支没有暴露 MTProto 的 force_file 开关，
    因此只能通过 mime 让它们保持 document（发送时的文件名仍由 file_name 指定）。
    """

    def __init__(self, path: Path, name: str):
        super().__init__(io.FileIO(path, "rb"))
        self._neutral_name = name

    @property
    def name(self) -> str:
        return self._neutral_name


def document_album_item(media_item, filename: str):
    """相册条目：视频/音频文件换成中性 mime，文件名保持不变"""
    if not isinstance(media_item, Path):
        return media_item
    media_kind = (mimetypes.guess_type(filename)[0] or "").split("/")[0]
    if media_kind in ("video", "audio"):
        return NeutralMimeFile(media_item, f"{Path(filename).stem}.bin")
    return media_item


def message_attachment(message: Message | None):
    """已发送消息中的媒体对象（用于缓存 file_id）"""
    if message is None:
        return None
    for attribute in ("video", "audio", "photo", "animation", "document", "voice"):
        attachment = getattr(message, attribute, None)
        if attachment is not None:
            return attachment
    return None


async def get_cached_media_file_id(filename: str) -> str | None:
    file = await TelegramFileCache.get_or_none(mediafilename=filename)
    if file:
        return file.file_id
    return None


async def cache_media(mediafilename: str, attachment) -> None:
    if not attachment:
        return
    try:
        key = cache_key_for_attachment(mediafilename, attachment)
        await TelegramFileCache.update_or_create(mediafilename=key, defaults=dict(file_id=attachment.file_id))
    except Exception as e:
        logger.exception(e)


@dataclass
class TelegramUploadTask(UploadTask):
    """在基类基础上增加 Telegram Message 引用"""

    message: Message | None = field(default=None)

    def __post_init__(self) -> None:
        if self.message is None and isinstance(self.context, Message):
            self.message = self.context


class TelegramUploadQueueManager(UploadQueueManager):
    """Telegram 专属上传队列管理器"""

    def __init__(self, *args, client=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = client

    async def _cache_lookup(self, filename: str) -> str | None:
        return await get_cached_media_file_id(filename)

    async def _do_upload(self, task: UploadTask) -> Any:
        assert isinstance(task, TelegramUploadTask)
        if task.task_type == "fetch":
            await self._process_fetch_task(task)
            return None
        result = await self._upload_media(task)
        await self._try_delete_share_message(task)
        return result

    async def _do_cache(self, content: ParsedContent, result: Any) -> None:
        await self._cache_upload_result(content, result)

    async def _skip_chat(self, task: UploadTask) -> None:
        """Bot 无发送权限时退出该聊天，避免后续任务继续失败"""
        assert isinstance(task, TelegramUploadTask)
        message = task.message
        if self.client is None or message is None:
            return
        with contextlib.suppress(Exception):
            await self.client.leave_chat(message.chat.id)

    async def _handle_upload_error(
        self,
        err: Exception,
        task: UploadTask,
        attempt: int,
        max_retries: int,
        phase: UploadPhase = UploadPhase.SEND,
    ) -> bool:
        """返回 True 表示应重试。

        发送阶段只有在确定未投递时才重试：MTProto 调用非幂等，
        超时等结果未知的错误重发会让用户收到重复的媒体。
        """
        assert isinstance(task, TelegramUploadTask)
        f = task.parsed_content

        if isinstance(err, FloodWait):
            # 平台要求等待，请求未投递，重试安全
            await asyncio.sleep(err.value + 1)
            return True

        if isinstance(err, _FORBIDDEN_ERRORS):
            await self._skip_chat(task)
            logger.error(f"任务 {task.task_id[:8]} 无权发送，已退出聊天: {err}")
            return False

        if isinstance(err, _TERMINAL_ERRORS):
            logger.error(f"任务 {task.task_id[:8]} 话题不可用，放弃: {err}")
            return False

        if isinstance(err, RPCError):
            if phase is UploadPhase.PREPARE:
                return True
            if isinstance(err, _REJECTED_ERRORS):
                logger.error(f"任务 {task.task_id[:8]} 第 {attempt}/{max_retries} 次被拒绝: {err}")
                return False
            logger.error(f"任务 {task.task_id[:8]} 第 {attempt}/{max_retries} 次上传失败: {err}")
            # 请求被平台拒绝说明没有投递，丢弃缓存重新下载后重试是安全的
            if f.media:
                f.media.need_download = True
            return True

        if isinstance(err, OSError):
            # 连接根本未建立 ⇒ 请求确定未发出，重试安全；超时/连接中断则结果未知
            if phase is UploadPhase.PREPARE or isinstance(err, ConnectionRefusedError):
                return True
            logger.error(f"任务 {task.task_id[:8]} 发送结果未知，不重试以免重复发送: {err}")
            return False

        return await super()._handle_upload_error(err, task, attempt, max_retries, phase)

    async def _handle_final_failure(self, task: UploadTask) -> None:
        await super()._handle_final_failure(task)
        assert isinstance(task, TelegramUploadTask)
        message = task.message
        if not message:
            return
        with contextlib.suppress(Exception):
            await message.reply_text(
                f"媒体获取失败，请稍后重试\n{task.parsed_content.url}",
                parse_mode=enums.ParseMode.DISABLED,
            )

    async def _reply_caption(self, message: Message, caption: str) -> None:
        """媒体发送后单独发送 caption：失败只影响 caption，不能让媒体重发"""
        try:
            await message.reply_text(caption, parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            logger.error(f"caption 发送失败，改用纯文本: {e}")
            with contextlib.suppress(Exception):
                await message.reply_text(caption, parse_mode=enums.ParseMode.DISABLED)

    async def _upload_media(self, task: TelegramUploadTask) -> Any:
        f = task.parsed_content
        message = task.message
        media = task.media
        mediathumb = task.mediathumb

        if not media or not f.media or not message:
            raise MediaUnavailable(f"媒体为空，无法发送: {f.url}")

        caption = format_caption_for_telegram(f, self.constraints)

        if f.media.type == "video":
            result = await message.reply_video(
                media[0],
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
                supports_streaming=True,
                thumb=mediathumb,
                duration=f.media.duration,
                file_name=f.media.filenames[0] if f.media.filenames else None,
                width=f.media.dimension.get("width", 0),
                height=f.media.dimension.get("height", 0),
            )
        elif f.media.type == "audio":
            result = await message.reply_audio(
                media[0],
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
                duration=f.media.duration,
                performer=f.author.name,
                thumb=mediathumb,
                title=f.media.title,
                file_name=f.media.filenames[0] if f.media.filenames else None,
            )
        elif len(f.media.urls) == 1:
            if ".gif" in f.media.urls[0]:
                result = await message.reply_animation(
                    media[0],
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                    file_name=f.media.filenames[0] if f.media.filenames else None,
                )
            else:
                result = await message.reply_photo(
                    media[0],
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                )
        else:
            result = await self._upload_media_group(message, f, media, caption)

        return result

    async def _upload_media_group(self, message: Message, f: ParsedContent, media: list, caption: str) -> list:
        assert f.media is not None
        if len(f.media.urls) <= 10:
            splits = [(media, f.media.urls, f.media.filenames)]
        else:
            mid = len(f.media.urls) // 2
            splits = [
                (media[:mid], f.media.urls[:mid], f.media.filenames[:mid]),
                (media[mid:], f.media.urls[mid:], f.media.filenames[mid:]),
            ]
        result: list = []
        for index, (sub_media, sub_urls, sub_fns) in enumerate(splits):
            sub_caption = caption if index == 0 else ""
            sub_result = await message.reply_media_group(
                [
                    (
                        InputMediaVideo(
                            img,
                            caption=sub_caption,
                            parse_mode=enums.ParseMode.HTML,
                            file_name=fn,
                            supports_streaming=True,
                        )
                        if ".gif" in mu
                        else InputMediaPhoto(img, caption=sub_caption, parse_mode=enums.ParseMode.HTML)
                    )
                    for img, mu, fn in zip(sub_media, sub_urls, sub_fns, strict=False)
                ],
            )
            result += sub_result
        await self._reply_caption(message, caption)
        return result

    async def _cache_upload_result(self, f: ParsedContent, result: Any) -> None:
        if result is None or not f.media or not f.media.filenames:
            return
        if isinstance(result, list):
            for filename, item in zip(f.media.filenames, result, strict=False):
                await cache_media(filename, message_attachment(item))
        else:
            await cache_media(f.media.filenames[0], message_attachment(result))

    async def _process_fetch_task(self, task: TelegramUploadTask) -> None:
        f = task.parsed_content
        message = task.message

        if not message or not f.media or not f.media.urls:
            return

        caption = format_caption_for_telegram(f, self.constraints)
        # Media preparation and the per-content lock are owned by the base
        # UploadQueueManager. Fetch tasks only format and send the prepared media.
        medias = list(task.media)
        mediathumb = task.mediathumb
        handles: list = []
        try:
            if mediathumb:
                medias.insert(0, mediathumb)
                mediafilenames = [f.media.thumbnail_filename, *f.media.filenames]
            else:
                mediafilenames = f.media.filenames

            if len(medias) == 1:
                # force_document 对应 MTProto 的 force_file，保证按文件而不是 video 发送
                result = await message.reply_document(
                    document=medias[0],
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                    file_name=mediafilenames[0],
                    force_document=True,
                )
                await cache_media(mediafilenames[0], message_attachment(result))
            else:
                if len(medias) <= 10:
                    splits = [(medias, mediafilenames)]
                else:
                    mid = len(medias) // 2
                    splits = [
                        (medias[:mid], mediafilenames[:mid]),
                        (medias[mid:], mediafilenames[mid:]),
                    ]
                results: list = []
                for sub_m, sub_fn in splits:
                    album = [
                        InputMediaDocument(document_album_item(media_item, filename), file_name=filename)
                        for media_item, filename in zip(sub_m, sub_fn, strict=False)
                    ]
                    handles.extend(item.media for item in album if isinstance(item.media, io.IOBase))
                    results += await message.reply_media_group(album)
                await self._reply_caption(message, caption)
                for filename, item in zip(mediafilenames, results, strict=False):
                    await cache_media(filename, message_attachment(item))
        except Exception as err:
            logger.exception(f"fetch 任务失败: {err} - {f.url}")
            raise  # 让 _try_upload_once 的错误处理感知到失败
        finally:
            for handle in handles:
                with contextlib.suppress(Exception):
                    handle.close()
            cleanup_medias(medias)

    async def _try_delete_share_message(self, task: TelegramUploadTask) -> None:
        message = task.message
        if not message:
            return
        urls = task.urls
        try:
            if (
                len(urls) == 1
                and message.chat.type != enums.ChatType.CHANNEL
                and not message.reply_to_message
                and message.text is not None
                and not message.automatic_forward
            ):
                match = re.match(BILIBILI_SHARE_URL_REGEX, message.text)
                if urls[0] == message.text or (match and match.group(0) == message.text):
                    await message.delete()
        except Exception as e:
            logger.debug(f"无法删除消息: {e}")
