from ...model import MediaConstraints, ParsedContent
from ...model import PreparedMedia as PreparedMedia
from ...provider import ProviderRegistry
from ...storage.models import TelegramFileCache
from ...utils import logger
from .. import Channel
from .formatting import format_caption_for_telegram

# MTProto 单文件上限（非 Premium bot 为 2GB）
TELEGRAM_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
TELEGRAM_CAPTION_LENGTH = 1024


class TelegramChannel(Channel):
    def __init__(self):
        self._registry: ProviderRegistry | None = None

    @property
    def media_constraints(self) -> MediaConstraints:
        return MediaConstraints(
            max_upload_size=TELEGRAM_UPLOAD_SIZE,
            max_download_size=TELEGRAM_UPLOAD_SIZE,
            caption_max_length=TELEGRAM_CAPTION_LENGTH,
            force_download=True,
        )

    def format_caption(self, content: ParsedContent) -> str:
        return format_caption_for_telegram(content, self.media_constraints)

    async def send_content(self, content, media, context):
        pass

    async def send_text(self, text, context):
        pass

    async def cache_sent_media(self, content: ParsedContent, result) -> None:
        pass

    async def get_cached_media(self, filename: str) -> str | None:
        file = await TelegramFileCache.get_or_none(mediafilename=filename)
        if file:
            return file.file_id
        return None

    async def start(self, provider_registry: ProviderRegistry) -> None:
        self._registry = provider_registry
        logger.info("TelegramChannel starting...")

    async def stop(self) -> None:
        logger.info("TelegramChannel stopped")
