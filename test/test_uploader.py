"""测试 biliparser/uploader/download.py — cleanup_medias"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from biliparser.model import Author, MediaConstraints, MediaInfo, ParsedContent
from biliparser.provider import ProviderRegistry
from biliparser.uploader.download import cleanup_medias


def test_cleanup_medias_paths():
    """Path 类型的文件应被删除"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        p = Path(f.name)
        f.write(b"test")
    assert p.exists()
    cleanup_medias([p])
    assert not p.exists()


def test_cleanup_medias_strings():
    """字符串类型（file_id）不应被删除"""
    cleanup_medias(["file_id_123", "another_id"])  # 不应抛异常


def test_cleanup_medias_mixed():
    """混合类型应只删除 Path"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
        p = Path(f.name)
        f.write(b"test")
    cleanup_medias(["file_id", p, "another_id"])
    assert not p.exists()


def test_cleanup_medias_missing_file():
    """不存在的文件不应抛异常"""
    cleanup_medias([Path("/tmp/nonexistent_file_12345.jpg")])


def test_cleanup_medias_empty():
    cleanup_medias([])


def test_telegram_channel_constraints():
    """TelegramChannel.media_constraints 应返回正确的默认值"""
    from biliparser.channel.telegram import TelegramChannel

    ch = TelegramChannel()
    mc = ch.media_constraints
    assert mc.max_upload_size == 50 * 1024 * 1024  # 50MB (non-local mode)
    assert mc.max_download_size == 2 * 1024 * 1024 * 1024
    assert mc.caption_max_length == 1024


def _media_constraints() -> MediaConstraints:
    return MediaConstraints(
        max_upload_size=50 * 1024 * 1024,
        max_download_size=2 * 1024 * 1024 * 1024,
        caption_max_length=1024,
    )


def test_telegram_upload_task_uses_context_message():
    from telegram import Message

    from biliparser.channel.telegram.uploader import TelegramUploadTask

    message = MagicMock(spec=Message)
    task = TelegramUploadTask(
        user_id=1,
        context=message,
        parsed_content=ParsedContent(url="https://example.com", author=Author()),
        media=[],
        mediathumb=None,
        urls=["https://example.com"],
    )

    assert task.message is message


@pytest.mark.asyncio
async def test_telegram_upload_success_deletes_share_message(monkeypatch):
    from biliparser.channel.telegram.uploader import TelegramUploadQueueManager, TelegramUploadTask

    manager = TelegramUploadQueueManager(
        registry=ProviderRegistry(),
        constraints=_media_constraints(),
    )
    message = MagicMock()
    task = TelegramUploadTask(
        user_id=1,
        context=message,
        parsed_content=ParsedContent(url="https://example.com", author=Author()),
        media=[],
        mediathumb=None,
        urls=["https://example.com"],
    )
    upload_media = AsyncMock(return_value=object())
    delete_share_message = AsyncMock()
    monkeypatch.setattr(manager, "_upload_media", upload_media)
    monkeypatch.setattr(manager, "_try_delete_share_message", delete_share_message)

    await manager._do_upload(task)

    upload_media.assert_called_once_with(task)
    delete_share_message.assert_called_once_with(task)


@pytest.mark.asyncio
async def test_fetch_upload_prepares_media_once_under_single_content_lock(monkeypatch):
    import biliparser.uploader.queue as queue_module
    from biliparser.channel.telegram.uploader import TelegramUploadQueueManager, TelegramUploadTask
    from biliparser.uploader.queue import UploadResult

    class CountingLock:
        def __init__(self):
            self.enter_count = 0

        async def __aenter__(self):
            self.enter_count += 1
            return self

        async def __aexit__(self, *args):
            return False

    class FakeCache:
        def __init__(self, lock):
            self.lock_instance = lock

        def lock(self, key, timeout):
            return self.lock_instance

    lock = CountingLock()
    monkeypatch.setattr(queue_module, "RedisCache", lambda: FakeCache(lock))

    prepare_media = AsyncMock(return_value=(["prepared-video"], None))
    monkeypatch.setattr(queue_module, "get_media_for_content", prepare_media)

    message = MagicMock()
    message.reply_document = AsyncMock(return_value=MagicMock(effective_attachment=object()))
    monkeypatch.setattr(
        "biliparser.channel.telegram.uploader.cache_media",
        AsyncMock(),
    )

    url = "https://example.com/video"
    task = TelegramUploadTask(
        user_id=1,
        context=message,
        message=message,
        parsed_content=ParsedContent(
            url=url,
            author=Author(),
            media=MediaInfo(urls=["source"], type="video", filenames=["video.mp4"]),
        ),
        media=[],
        mediathumb=None,
        urls=[url],
        task_type="fetch",
        fetch_mode="file",
    )
    manager = TelegramUploadQueueManager(
        registry=ProviderRegistry(),
        constraints=_media_constraints(),
    )

    assert await manager._try_upload_once(task, 1, 1) is UploadResult.SUCCESS
    assert lock.enter_count == 1
    prepare_media.assert_awaited_once()
    message.reply_document.assert_awaited_once()


class Video:
    def __init__(self, file_id):
        self.file_id = file_id


class Document:
    def __init__(self, file_id):
        self.file_id = file_id


class PhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


def test_cache_key_document_is_namespaced():
    from biliparser.channel.telegram.uploader import cache_key_for_attachment

    assert cache_key_for_attachment("video.mp4", Document("doc-id")) == "document:video.mp4"


def test_cache_key_video_is_native():
    from biliparser.channel.telegram.uploader import cache_key_for_attachment

    assert cache_key_for_attachment("video.mp4", Video("vid-id")) == "video.mp4"


@pytest.mark.asyncio
async def test_cache_media_document_uses_namespaced_key(monkeypatch):
    from biliparser.channel.telegram.uploader import cache_media

    update_or_create = AsyncMock()
    monkeypatch.setattr(
        "biliparser.channel.telegram.uploader.TelegramFileCache.update_or_create",
        update_or_create,
    )

    await cache_media("video.mp4", Document("doc-id"))

    update_or_create.assert_awaited_once_with(
        mediafilename="document:video.mp4",
        defaults={"file_id": "doc-id"},
    )


@pytest.mark.asyncio
async def test_cache_media_video_uses_native_key(monkeypatch):
    from biliparser.channel.telegram.uploader import cache_media

    update_or_create = AsyncMock()
    monkeypatch.setattr(
        "biliparser.channel.telegram.uploader.TelegramFileCache.update_or_create",
        update_or_create,
    )

    await cache_media("video.mp4", Video("vid-id"))

    update_or_create.assert_awaited_once_with(
        mediafilename="video.mp4",
        defaults={"file_id": "vid-id"},
    )


@pytest.mark.asyncio
async def test_cache_media_photosize_tuple_uses_largest(monkeypatch):
    from biliparser.channel.telegram.uploader import cache_media

    update_or_create = AsyncMock()
    monkeypatch.setattr(
        "biliparser.channel.telegram.uploader.TelegramFileCache.update_or_create",
        update_or_create,
    )

    await cache_media("img.jpg", (PhotoSize("small"), PhotoSize("large")))

    update_or_create.assert_awaited_once_with(
        mediafilename="img.jpg",
        defaults={"file_id": "large"},
    )


# ── 重试策略：发送阶段不重发已投递的媒体 ────────────────────────────────────────


class FakeCache:
    """避免测试触碰真实 Redis/FakeRedis 文件"""

    class FakeLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    def lock(self, key, timeout):
        return self.FakeLock()


def _patch_queue(monkeypatch, media_result):
    import biliparser.uploader.queue as queue_module

    monkeypatch.setattr(queue_module, "RedisCache", FakeCache)
    monkeypatch.setattr(queue_module, "get_media_for_content", AsyncMock(return_value=media_result))


def _telegram_manager(monkeypatch):
    from biliparser.channel.telegram.uploader import TelegramUploadQueueManager

    monkeypatch.setattr("biliparser.channel.telegram.uploader.cache_media", AsyncMock())
    manager = TelegramUploadQueueManager(registry=ProviderRegistry(), constraints=_media_constraints())
    manager._try_delete_share_message = AsyncMock()
    manager._do_cache = AsyncMock()
    manager._retry_parse_url = AsyncMock(return_value=True)
    return manager


def _fake_message():
    message = MagicMock()
    for name in [
        "reply_text",
        "reply_video",
        "reply_audio",
        "reply_photo",
        "reply_animation",
        "reply_media_group",
        "reply_document",
    ]:
        setattr(message, name, AsyncMock(return_value=MagicMock(effective_attachment=object())))
    message.reply_media_group.return_value = (MagicMock(effective_attachment=object()),)
    return message


def _telegram_task(message, url, media=None, task_type="parse", fetch_mode=None):
    from biliparser.channel.telegram.uploader import TelegramUploadTask

    return TelegramUploadTask(
        user_id=1,
        context=message,
        message=message,
        parsed_content=ParsedContent(url=url, author=Author(), media=media),
        media=[],
        mediathumb=None,
        urls=[url],
        task_type=task_type,
        fetch_mode=fetch_mode,
    )


@pytest.mark.asyncio
async def test_send_timeout_is_not_retried(monkeypatch):
    """发送结果未知（超时）时不得重发，只通知用户"""
    from telegram.error import TimedOut

    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.mp4"], type="video", filenames=["v.mp4"])
    task = _telegram_task(message, "https://www.bilibili.com/video/av2", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/v.mp4")], None))
    manager = _telegram_manager(monkeypatch)
    message.reply_video.side_effect = TimedOut("Timed out")

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    message.reply_video.assert_awaited_once()
    notice = message.reply_text.await_args
    assert "媒体获取失败" in notice.args[0]
    assert notice.kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_send_connect_error_is_retried(monkeypatch):
    """确定未发出的连接错误仍可重试"""
    from telegram.error import NetworkError

    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.mp4"], type="video", filenames=["v.mp4"])
    task = _telegram_task(message, "https://www.bilibili.com/video/av3", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/v.mp4")], None))
    manager = _telegram_manager(monkeypatch)
    calls = {"n": 0}

    async def reply_video(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NetworkError("httpx.ConnectError: connection failed")
        return MagicMock(effective_attachment=object())

    message.reply_video.side_effect = reply_video

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    assert calls["n"] == 2
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_caption_failure_does_not_resend_media_group(monkeypatch):
    """caption 是独立消息，它的失败不能让媒体组重发"""
    from telegram.error import BadRequest

    message = _fake_message()
    media = MediaInfo(
        urls=["https://cdn.invalid/1.jpg", "https://cdn.invalid/2.jpg"],
        type="image",
        filenames=["1.jpg", "2.jpg"],
    )
    task = _telegram_task(message, "https://www.bilibili.com/opus/1", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/1.jpg"), Path("/tmp/2.jpg")], None))
    manager = _telegram_manager(monkeypatch)

    captions = {"n": 0}

    async def reply_text(text, **kwargs):
        captions["n"] += 1
        if captions["n"] == 1:
            raise BadRequest("Can't parse entities")
        assert kwargs.get("parse_mode") is None
        return MagicMock()

    message.reply_text.side_effect = reply_text

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    message.reply_media_group.assert_awaited_once()
    assert captions["n"] == 2
