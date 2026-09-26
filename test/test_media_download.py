from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import biliparser.uploader.download as download_module
from biliparser.channel.telegram.uploader import TelegramUploadQueueManager, TelegramUploadTask
from biliparser.model import Author, MediaConstraints, MediaInfo, ParsedContent
from biliparser.provider import ProviderRegistry
from biliparser.uploader.download import dash_merged_filename


class TruncatedResponse:
    status_code = 200
    headers = {"content-type": "video/mp4", "content-length": str(29_256_993)}
    request = httpx.Request("GET", "https://cdn.invalid/stream.m4s")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aiter_bytes(self):
        yield b"x" * (1024 * 1024)
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body (received 1048576 bytes, expected 29256993)",
            request=self.request,
        )


class TruncatedClient:
    def stream(self, *args, **kwargs):
        return TruncatedResponse()


@pytest.mark.asyncio
async def test_truncated_dash_stream_is_dropped_to_empty_media():
    content = ParsedContent(
        url="https://www.bilibili.com/video/BV-truncated",
        author=Author(),
        media=MediaInfo(
            urls=["https://cdn.invalid/video.m4s", "https://cdn.invalid/audio.m4s"],
            type="video",
            filenames=["video.m4s", "audio.m4s"],
            merge_streams=True,
        ),
    )

    result = await download_module.handle_dash_media(content, TruncatedClient())

    assert result == []


@pytest.mark.asyncio
async def test_file_fetch_sends_thumbnail_when_media_is_empty(monkeypatch):
    message = MagicMock()
    message.reply_document = AsyncMock(return_value=MagicMock(effective_attachment=object()))
    cache_media = AsyncMock()
    monkeypatch.setattr("biliparser.channel.telegram.uploader.cache_media", cache_media)

    url = "https://www.bilibili.com/video/BV-truncated"
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
        mediathumb="cover.jpg",
        urls=[url],
        task_type="fetch",
        fetch_mode="file",
    )
    manager = TelegramUploadQueueManager(
        registry=ProviderRegistry(),
        constraints=MediaConstraints(50 * 1024 * 1024, 2 * 1024 * 1024 * 1024, 1024),
    )

    await manager._process_fetch_task(task)

    message.reply_document.assert_awaited_once()
    assert message.reply_document.await_args.kwargs["document"] == "cover.jpg"
    cache_media.assert_awaited_once()


def test_dash_merged_filename():
    assert dash_merged_filename("CID-1-30080.m4s") == "CID-1-30080_merged.mp4"


@pytest.mark.asyncio
async def test_handle_dash_media_cache_lookup_uses_merged_filename():
    content = ParsedContent(
        url="https://www.bilibili.com/video/BV-cached",
        author=Author(),
        media=MediaInfo(
            urls=["https://cdn.invalid/video.m4s", "https://cdn.invalid/audio.m4s"],
            type="video",
            filenames=["CID-1-30080.m4s", "audio.m4s"],
            merge_streams=True,
        ),
    )
    cache_lookup = AsyncMock(return_value="cached-file-id")

    result = await download_module.handle_dash_media(content, MagicMock(), cache_lookup=cache_lookup)

    cache_lookup.assert_awaited_once_with("CID-1-30080_merged.mp4")
    assert result == ["cached-file-id"]
    assert content.media.filenames == ["CID-1-30080_merged.mp4"]
    assert content.media.merge_streams is False


# ── 下载完整性校验与 MP4 回退 ──────────────────────────────────────────────────


class SizedResponse:
    """按 content-length 返回完整或截断内容"""

    def __init__(self, url, payload, total=None):
        self.status_code = 200
        self.headers = {"content-type": "video/mp4", "content-length": str(len(payload) if total is None else total)}
        self.request = httpx.Request("GET", url)
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aiter_bytes(self):
        yield self._payload


class FlakyClient:
    """第一次返回截断内容，之后返回完整内容"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def stream(self, method, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SizedResponse(url, self.payload[:1024], total=len(self.payload))
        return SizedResponse(url, self.payload)


@pytest.mark.asyncio
async def test_truncated_download_is_retried_once(monkeypatch, tmp_path):
    monkeypatch.setattr(download_module, "LOCAL_MEDIA_FILE_PATH", tmp_path)
    payload = b"x" * 4096
    client = FlakyClient(payload)

    result = await download_module.get_media(
        client, "https://www.bilibili.com/video/BV1", "https://cdn.invalid/v.mp4", "v.mp4", no_cache=True
    )

    assert client.calls == 2
    assert isinstance(result, Path)
    assert result.read_bytes() == payload


@pytest.mark.asyncio
async def test_persistently_truncated_download_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(download_module, "LOCAL_MEDIA_FILE_PATH", tmp_path)

    class AlwaysTruncatedClient(FlakyClient):
        def stream(self, method, url, **kwargs):
            self.calls += 1
            return SizedResponse(url, self.payload[:1024], total=len(self.payload))

    client = AlwaysTruncatedClient(b"x" * 4096)

    result = await download_module.get_media(
        client, "https://www.bilibili.com/video/BV1", "https://cdn.invalid/v.mp4", "v.mp4", no_cache=True
    )

    assert client.calls == download_module.MEDIA_DOWNLOAD_ATTEMPTS
    assert result is None


@pytest.mark.asyncio
async def test_dash_failure_falls_back_to_mp4_direct_link(monkeypatch, tmp_path):
    monkeypatch.setattr(download_module, "LOCAL_MEDIA_FILE_PATH", tmp_path)
    fallback = "https://cdn.invalid/CID-1.mp4"
    content = ParsedContent(
        url="https://www.bilibili.com/video/BV-fallback",
        author=Author(),
        media=MediaInfo(
            urls=["https://cdn.invalid/video.m4s", "https://cdn.invalid/audio.m4s"],
            type="video",
            filenames=["video.m4s", "audio.m4s"],
            merge_streams=True,
            fallback_url=fallback,
        ),
    )
    monkeypatch.setattr(download_module, "handle_dash_media", AsyncMock(return_value=[]))
    get_media = AsyncMock(return_value=tmp_path / "CID-1.mp4")
    monkeypatch.setattr(download_module, "get_media", get_media)

    media, _thumb = await download_module.get_media_for_content(content)

    assert media == [tmp_path / "CID-1.mp4"]
    assert content.media.urls == [fallback]
    assert content.media.filenames == ["CID-1.mp4"]
    assert content.media.merge_streams is False


@pytest.mark.asyncio
async def test_dash_failure_without_fallback_yields_no_media(monkeypatch):
    content = ParsedContent(
        url="https://www.bilibili.com/video/BV-no-fallback",
        author=Author(),
        media=MediaInfo(
            urls=["https://cdn.invalid/video.m4s", "https://cdn.invalid/audio.m4s"],
            type="video",
            filenames=["video.m4s", "audio.m4s"],
            merge_streams=True,
        ),
    )
    monkeypatch.setattr(download_module, "handle_dash_media", AsyncMock(return_value=[]))

    media, _thumb = await download_module.get_media_for_content(content)

    assert media == []
