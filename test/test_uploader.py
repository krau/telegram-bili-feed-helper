"""测试 biliparser/uploader/download.py — cleanup_medias"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyrogram import enums
from pyrogram.errors import BadRequest, ChatWriteForbidden, FloodWait
from pyrogram.types import Document, Photo, Video

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
    from biliparser.channel.telegram import TELEGRAM_UPLOAD_SIZE, TelegramChannel

    ch = TelegramChannel()
    mc = ch.media_constraints
    assert mc.max_upload_size == TELEGRAM_UPLOAD_SIZE  # MTProto 单文件上限 2GB
    assert mc.max_download_size == TELEGRAM_UPLOAD_SIZE
    assert mc.caption_max_length == 1024
    assert mc.force_download is True


def _media_constraints() -> MediaConstraints:
    return MediaConstraints(
        max_upload_size=50 * 1024 * 1024,
        max_download_size=2 * 1024 * 1024 * 1024,
        caption_max_length=1024,
    )


def test_telegram_upload_task_uses_context_message():
    from pyrogram.types import Message

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


def _document(file_id: str):
    attachment = MagicMock(spec=Document)
    attachment.file_id = file_id
    return attachment


def _video(file_id: str):
    attachment = MagicMock(spec=Video)
    attachment.file_id = file_id
    return attachment


def _photo(file_id: str):
    attachment = MagicMock(spec=Photo)
    attachment.file_id = file_id
    return attachment


def test_cache_key_document_is_namespaced():
    from biliparser.channel.telegram.uploader import cache_key_for_attachment

    assert cache_key_for_attachment("video.mp4", _document("doc-id")) == "document:video.mp4"


def test_cache_key_video_is_native():
    from biliparser.channel.telegram.uploader import cache_key_for_attachment

    assert cache_key_for_attachment("video.mp4", _video("vid-id")) == "video.mp4"


@pytest.mark.asyncio
async def test_cache_media_document_uses_namespaced_key(monkeypatch):
    from biliparser.channel.telegram.uploader import cache_media

    update_or_create = AsyncMock()
    monkeypatch.setattr(
        "biliparser.channel.telegram.uploader.TelegramFileCache.update_or_create",
        update_or_create,
    )

    await cache_media("video.mp4", _document("doc-id"))

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

    await cache_media("video.mp4", _video("vid-id"))

    update_or_create.assert_awaited_once_with(
        mediafilename="video.mp4",
        defaults={"file_id": "vid-id"},
    )


@pytest.mark.asyncio
async def test_cache_media_photo_uses_native_key(monkeypatch):
    """Kurigram 的 Photo 已是最佳尺寸，直接用其 file_id 落库"""
    from biliparser.channel.telegram.uploader import cache_media

    update_or_create = AsyncMock()
    monkeypatch.setattr(
        "biliparser.channel.telegram.uploader.TelegramFileCache.update_or_create",
        update_or_create,
    )

    await cache_media("img.jpg", _photo("large"))

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
async def test_empty_media_is_not_sent_as_text(monkeypatch):
    """解析类任务媒体准备失败时不得静默降级为纯文本"""
    from biliparser.uploader.queue import UploadResult

    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.m4s", "https://cdn.invalid/a.m4s"], type="video", merge_streams=True)
    task = _telegram_task(message, "https://www.bilibili.com/video/av1", media=media)
    _patch_queue(monkeypatch, ([], "cover.jpg"))
    manager = _telegram_manager(monkeypatch)

    result = await manager._try_upload_once(task, 1, 1)

    assert result is UploadResult.RETRY
    message.reply_text.assert_not_awaited()
    message.reply_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_timeout_is_not_retried(monkeypatch):
    """发送结果未知（超时）时不得重发，只通知用户"""
    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.mp4"], type="video", filenames=["v.mp4"])
    task = _telegram_task(message, "https://www.bilibili.com/video/av2", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/v.mp4")], None))
    manager = _telegram_manager(monkeypatch)
    message.reply_video.side_effect = TimeoutError("Timed out")

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    message.reply_video.assert_awaited_once()
    notice = message.reply_text.await_args
    assert "媒体获取失败" in notice.args[0]
    assert notice.kwargs["parse_mode"] == enums.ParseMode.DISABLED


@pytest.mark.asyncio
async def test_send_connect_error_is_retried(monkeypatch):
    """确定未发出的连接错误仍可重试"""
    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.mp4"], type="video", filenames=["v.mp4"])
    task = _telegram_task(message, "https://www.bilibili.com/video/av3", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/v.mp4")], None))
    manager = _telegram_manager(monkeypatch)
    calls = {"n": 0}

    async def reply_video(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionRefusedError("connection refused")
        return MagicMock(effective_attachment=object())

    message.reply_video.side_effect = reply_video

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    assert calls["n"] == 2
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_reset_is_not_retried(monkeypatch):
    """连接中断时请求可能已发出，禁止重发"""
    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.mp4"], type="video", filenames=["v.mp4"])
    task = _telegram_task(message, "https://www.bilibili.com/video/av5", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/v.mp4")], None))
    manager = _telegram_manager(monkeypatch)

    async def reply_video(*args, **kwargs):
        raise ConnectionResetError("connection reset by peer")

    message.reply_video.side_effect = reply_video

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    message.reply_video.assert_awaited_once()
    assert "媒体获取失败" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_flood_wait_is_retried(monkeypatch):
    """FloodWait 表示请求被拒，等待后可安全重试"""
    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.mp4"], type="video", filenames=["v.mp4"])
    task = _telegram_task(message, "https://www.bilibili.com/video/av4", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/v.mp4")], None))
    manager = _telegram_manager(monkeypatch)
    calls = {"n": 0}

    async def reply_video(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWait(value=0)
        return MagicMock(effective_attachment=object())

    message.reply_video.side_effect = reply_video

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    assert calls["n"] == 2
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_write_forbidden_is_given_up(monkeypatch):
    """无发送权限时退出该聊天，不再重试"""
    message = _fake_message()
    media = MediaInfo(urls=["https://cdn.invalid/v.mp4"], type="video", filenames=["v.mp4"])
    task = _telegram_task(message, "https://www.bilibili.com/video/av5", media=media)
    _patch_queue(monkeypatch, ([Path("/tmp/v.mp4")], None))
    manager = _telegram_manager(monkeypatch)
    manager.client = MagicMock()
    manager.client.leave_chat = AsyncMock()
    calls = {"n": 0}

    async def reply_video(*args, **kwargs):
        calls["n"] += 1
        raise ChatWriteForbidden("not enough rights")

    message.reply_video.side_effect = reply_video
    message.chat.id = -100123
    task.message = message

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    assert calls["n"] == 1
    manager.client.leave_chat.assert_awaited_once_with(message.chat.id)


@pytest.mark.asyncio
async def test_caption_failure_does_not_resend_media_group(monkeypatch):
    """caption 是独立消息，它的失败不能让媒体组重发"""
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
        assert kwargs.get("parse_mode") == enums.ParseMode.DISABLED
        return MagicMock()

    message.reply_text.side_effect = reply_text

    manager.active_tasks[1] = {task.task_id: task}
    await manager._process_upload(task)

    message.reply_media_group.assert_awaited_once()
    assert captions["n"] == 2


@pytest.mark.asyncio
async def test_submit_keeps_distinct_urls_from_same_message(monkeypatch):
    """同一条消息里的多个不同链接都要入队，只有同内容才算重复"""
    _patch_queue(monkeypatch, ([], None))
    manager = _telegram_manager(monkeypatch)

    first_url = "https://www.bilibili.com/video/av1"
    second_url = "https://www.bilibili.com/video/av2"
    first = _telegram_task(_fake_message(), first_url)
    second = _telegram_task(_fake_message(), second_url)
    first.urls = second.urls = [first_url, second_url]

    await manager.submit(first)
    await manager.submit(second)
    await manager.submit(_telegram_task(_fake_message(), first_url))

    assert [t.parsed_content.url for t in manager.active_tasks[1].values()] == [first_url, second_url]


# ── /file 多文件：相册 + 保持 document 语义 ─────────────────────────────────────


def test_document_album_item_keeps_video_files_as_documents(tmp_path):
    """视频/音频文件在相册里必须保持 document：mime 改为中性后缀"""
    from biliparser.channel.telegram.uploader import NeutralMimeFile, document_album_item

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    item = document_album_item(clip, "clip.mp4")
    try:
        assert isinstance(item, NeutralMimeFile)
        assert item.name == "clip.bin"
        assert item.read() == b"x"
    finally:
        item.close()

    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    assert document_album_item(png, "a.png") == png
    assert document_album_item("cached-file-id", "a.png") == "cached-file-id"


@pytest.mark.asyncio
async def test_fetch_multi_documents_sent_as_one_album(monkeypatch, tmp_path):
    """多个文件走一个相册，且用 force_document 的单发路径不被调用"""
    from biliparser.channel.telegram.uploader import TelegramUploadQueueManager, TelegramUploadTask

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video-bytes")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png-bytes")

    message = MagicMock()
    message.reply_media_group = AsyncMock(
        return_value=[MagicMock(document=MagicMock(file_id="doc-1")), MagicMock(document=MagicMock(file_id="doc-2"))]
    )
    message.reply_document = AsyncMock()
    message.reply_text = AsyncMock()
    monkeypatch.setattr("biliparser.channel.telegram.uploader.cache_media", AsyncMock())

    task = TelegramUploadTask(
        user_id=1,
        context=message,
        message=message,
        parsed_content=ParsedContent(
            url="https://www.bilibili.com/video/av1",
            author=Author(),
            media=MediaInfo(urls=[str(cover), str(clip)], type="video", filenames=[cover.name, clip.name]),
        ),
        media=[cover, clip],
        mediathumb=None,
        urls=["u"],
        task_type="fetch",
        fetch_mode="file",
    )
    manager = TelegramUploadQueueManager(registry=ProviderRegistry(), constraints=_media_constraints())

    await manager._process_fetch_task(task)

    message.reply_document.assert_not_awaited()
    album = message.reply_media_group.await_args.args[0]
    assert [item.file_name for item in album] == ["cover.png", "clip.mp4"]
    # mp4 走了中性 mime 读取器，png 直接用路径
    assert isinstance(album[0].media, Path)
    assert album[1].media.name == "clip.bin"
    assert not clip.exists() and not cover.exists()  # cleanup_medias 生效
