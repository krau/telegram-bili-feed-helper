"""Telegram bot（MTProto / Kurigram）

Client 生命周期、命令与消息处理、inline 查询、上传队列接线
"""

import asyncio
import contextlib
import io
import os
import re
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginChannel, QrCodeLoginEvents
from pyrogram import Client, enums, filters
from pyrogram.handlers import InlineQueryHandler, MessageHandler
from pyrogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from ...provider import ProviderRegistry
from ...provider.bilibili.credential import credentialFactory
from ...storage.cache import RedisCache
from ...utils import logger
from .formatting import escape_html, format_caption_for_telegram
from .inline import answer_inline_query, build_help_result, build_media_inline_results
from .uploader import TelegramUploadQueueManager, TelegramUploadTask

BILIBILI_URL_REGEX = (
    r"(?i)(?:https?://)?[\w\.]*?(?:bilibili(?:bb)?\.com|(?:b23(?:bb)?|acg)\.tv|bili2?2?3?3?\.cn)\S+|BV\w{10}"
)
BILIBILI_SHARE_URL_REGEX = r"(?i)【.*】 https://[\w\.]*?(?:bilibili\.com|b23\.tv|bili2?2?3?3?\.cn)\S+"

SOURCE_CODE_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton("源代码", url="https://github.com/simonsmh/telegram-bili-feed-helper")]]
)

SESSION_NAME = "bilifeedbot"
NO_PARSE = enums.ParseMode.DISABLED
HTML = enums.ParseMode.HTML

BOT_COMMANDS = [
    ["start", "关于本 Bot"],
    ["parse", "获取匹配内容"],
    ["file", "获取匹配内容原始文件"],
    ["cover", "获取匹配内容原始文件预览"],
    ["video", "获取匹配清晰度视频，需参数：/video 720P BV号"],
    ["clear", "清除匹配内容缓存"],
    ["tasks", "查看当前任务"],
    ["cancel", "取消正在排队的任务"],
    ["login", "管理员扫码更新 Bilibili 登录凭证"],
]


@dataclass
class BotContext:
    """运行期共享状态"""

    registry: ProviderRegistry
    channel: Any
    queue_manager: TelegramUploadQueueManager | None = None
    login_task: asyncio.Task | None = None


def _get_env_int(name: str, default: int = 0) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        return max(0, int(raw_value))
    except ValueError:
        logger.warning(f"{name} 配置无效: {raw_value}")
        return default


def _get_request_limit_config() -> tuple[int, int]:
    return _get_env_int("REQUEST_LIMIT_COUNT"), _get_env_int("REQUEST_LIMIT_TTL")


def _get_admin_user_id() -> int | None:
    """Read the single Telegram user ID allowed to use administrative commands."""
    raw = os.environ.get("ADMIN_USER_ID", "")
    try:
        return int(raw) if raw else None
    except ValueError:
        logger.warning(f"管理员用户 ID 配置无效: {raw!r}")
        return None


def _is_admin(message: Message) -> bool:
    user = message.from_user
    admin_user_id = _get_admin_user_id()
    return user is not None and admin_user_id is not None and user.id == admin_user_id


async def start(ctx: BotContext, client: Client, message: Message) -> None:
    """Send welcome/help message."""
    await message.reply_text(await get_description(client), reply_markup=SOURCE_CODE_MARKUP, parse_mode=HTML)


async def login(ctx: BotContext, client: Client, message: Message) -> None:
    """Send a Bilibili QR code to the configured administrator and wait for login."""
    if message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP, enums.ChatType.FORUM):
        return

    if not _is_admin(message):
        if _get_admin_user_id() is None and message.from_user is not None:
            await message.reply_text(
                f"你的 Telegram 用户 ID 是：{message.from_user.id}\n"
                "请将此 ID 配置到环境变量 ADMIN_USER_ID 后重启 Bot。",
                parse_mode=NO_PARSE,
            )
        return

    if ctx.login_task and not ctx.login_task.done():
        ctx.login_task.cancel()

    # The web QR endpoint can report DONE without including SESSDATA in its
    # redirect URL.  The TV endpoint returns the cookie list directly.
    qr_login = QrCodeLogin(platform=QrCodeLoginChannel.TV)
    try:
        await qr_login.generate_qrcode()
        picture = qr_login.get_qrcode_picture()
        image = io.BytesIO(picture.content)
        image.name = "bilibili-login.png"
        await message.reply_photo(
            photo=image,
            caption="请使用哔哩哔哩客户端扫描二维码登录（二维码有效期约 3 分钟）。",
            parse_mode=NO_PARSE,
        )
    except Exception:
        logger.exception("生成 Bilibili 登录二维码失败")
        await message.reply_text("生成登录二维码失败，请稍后重试", parse_mode=NO_PARSE)
        return

    async def wait_for_login() -> None:
        try:
            while True:
                await asyncio.sleep(3)
                state = await qr_login.check_state()
                if state is QrCodeLoginEvents.DONE:
                    await credentialFactory.set(qr_login.get_credential())
                    await message.reply_text("Bilibili 扫码登录成功，凭证已更新。", parse_mode=NO_PARSE)
                    return
                if state is QrCodeLoginEvents.TIMEOUT:
                    await message.reply_text("登录二维码已过期，请重新发送 /login。", parse_mode=NO_PARSE)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("轮询 Bilibili 扫码登录状态失败")
            await message.reply_text("扫码登录失败，请重新发送 /login。", parse_mode=NO_PARSE)

    ctx.login_task = asyncio.create_task(wait_for_login())


def _format_rate_limit_ttl(ttl_seconds: int) -> str:
    if ttl_seconds <= 0:
        return "稍后"
    hours, remainder = divmod(ttl_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} 小时 {minutes} 分钟后"
    if minutes:
        return f"{minutes} 分钟后"
    return f"{ttl_seconds} 秒后"


async def check_request_limit(user_id: int | str) -> tuple[bool, int, int]:
    limit_count, limit_ttl = _get_request_limit_config()
    if limit_count <= 0 or limit_ttl <= 0:
        return True, 0, 0

    cache = RedisCache()
    key = f"request_limit:{limit_ttl}:{user_id}"
    count = await cache.incr(key)
    ttl = await cache.ttl(key)
    if count == 1 or ttl < 0:
        await cache.expire(key, limit_ttl)
        ttl = limit_ttl

    remaining = max(0, limit_count - count)
    return count <= limit_count, remaining, max(0, ttl)


async def check_message_request_limit(message: Message, reply_on_limit: bool = True) -> bool:
    user_id = message.from_user.id if message.from_user else message.chat.id
    allowed, _remaining, ttl = await check_request_limit(user_id)
    if allowed:
        return True
    if reply_on_limit:
        await message.reply_text(f"请求次数已达到上限，请 {_format_rate_limit_ttl(ttl)} 再试", parse_mode=NO_PARSE)
    logger.info(f"请求被限流: 用户 {user_id}")
    return False


async def get_description(client: Client) -> str:
    bot_me = await client.get_me()
    return (
        f"欢迎使用 @{bot_me.username} 的 Inline 模式来转发动态，您也可以将 Bot 添加到群组或频道自动匹配消息。\n"
        f"Inline 模式限制：只可发单张图，消耗设备流量，安全性低。\n"
        f"群组模式限制：媒体由 Bot 转发上传，单文件不超过 2GB，上传期间会排队。\n"
    )


def message_to_urls_sync(message: Message) -> list[str]:
    """Extract Bilibili URLs from a message (sync helper)."""
    urls = re.findall(BILIBILI_URL_REGEX, message.text or message.caption or "")
    for entity in [*(message.entities or []), *(message.caption_entities or [])]:
        entity_url = getattr(entity, "url", None)
        if entity_url:
            urls.extend(re.findall(BILIBILI_URL_REGEX, entity_url))
    return urls


async def message_to_urls(client: Client, message: Message | None) -> tuple[Message | None, list[str]]:
    """Extract message and Bilibili URLs, filtering bot's own forwards."""
    if message is None:
        return message, []

    origin = message.forward_origin
    bot_me = client.me
    if isinstance(origin, MessageOriginUser):
        if origin.sender_user.is_bot and origin.sender_user.username == bot_me.username:
            return message, []
    elif isinstance(origin, MessageOriginHiddenUser):
        if origin.sender_user_name == bot_me.first_name:
            return message, []
    elif isinstance(origin, MessageOriginChat):
        if origin.author_signature == bot_me.first_name:
            return message, []
    elif isinstance(origin, MessageOriginChannel):
        if origin.author_signature == bot_me.first_name:
            return message, []
        try:
            member = await client.get_chat_member(origin.chat.id, "me")
            if member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
                return message, []
        except Exception:
            logger.debug("Failed to check channel admin status")

    return message, message_to_urls_sync(message)


async def parse(ctx: BotContext, client: Client, message: Message) -> None:
    """Handle Bilibili URL parse requests."""
    message, urls = await message_to_urls(client, message)
    if message is None:
        return

    is_parse = bool(message.text and message.text.startswith("/parse"))
    is_video = bool(message.text and message.text.startswith("/video"))
    extra = None

    if is_video:
        texts = (message.text or "").split(" ")
        if len(texts) < 2:
            await message.reply_text("参数不正确，例如：/video 720P BV1Y25Nz4EZ3", parse_mode=NO_PARSE)
            return
        extra = {"quality": texts[1]}

    if not urls:
        if is_parse or is_video or message.chat.type == enums.ChatType.PRIVATE:
            await message.reply_text("链接不正确", parse_mode=NO_PARSE)
        return

    should_reply_on_limit = is_parse or is_video or message.chat.type == enums.ChatType.PRIVATE
    if not await check_message_request_limit(message, reply_on_limit=should_reply_on_limit):
        return

    logger.info(f"Parse: {urls} (用户: {message.from_user.id if message.from_user else 'unknown'})")

    with contextlib.suppress(Exception):
        await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)

    mc = ctx.channel.media_constraints
    parsed_results = await ctx.registry.parse(urls, mc, extra=extra)

    for f in parsed_results:
        if isinstance(f, Exception):
            logger.warning(f"解析错误: {f}")
            if is_parse or is_video:
                await message.reply_text(escape_html(str(f)), parse_mode=HTML)
            continue

        if not f.media or not f.media.urls:
            caption = format_caption_for_telegram(f, mc)
            await message.reply_text(caption, parse_mode=HTML)
            continue

        user_id = message.from_user.id if message.from_user else message.chat.id
        task = TelegramUploadTask(
            user_id=user_id,
            context=message,
            message=message,
            parsed_content=f,
            media=[],
            mediathumb=None,
            urls=urls,
        )

        assert ctx.queue_manager is not None
        await ctx.queue_manager.submit(task)
        logger.info(f"已提交上传任务: {f.url} (用户: {user_id})")


async def fetch(ctx: BotContext, client: Client, message: Message) -> None:
    """Handle /file and /cover commands."""
    message, urls = await message_to_urls(client, message)
    if message is None or not message.text:
        return
    if not urls:
        await message.reply_text("链接不正确", parse_mode=NO_PARSE)
        return
    if not await check_message_request_limit(message):
        return

    fetch_mode = "cover" if message.text.startswith("/cover") else "file"
    logger.info(f"Fetch ({fetch_mode}): {urls} (用户: {message.from_user.id if message.from_user else 'unknown'})")

    mc = ctx.channel.media_constraints
    parsed_results = await ctx.registry.parse(urls, mc)

    for f in parsed_results:
        if isinstance(f, Exception):
            logger.warning(f"解析错误: {f}")
            await message.reply_text(escape_html(str(f)), parse_mode=HTML)
            continue

        if not f.media or not f.media.urls:
            continue

        user_id = message.from_user.id if message.from_user else message.chat.id
        task = TelegramUploadTask(
            user_id=user_id,
            context=message,
            message=message,
            parsed_content=f,
            media=[],
            mediathumb=None,
            urls=urls,
            task_type="fetch",
            fetch_mode=fetch_mode,
        )

        assert ctx.queue_manager is not None
        await ctx.queue_manager.submit(task)
        logger.info(f"已提交 fetch 任务: {f.url} (用户: {user_id}, 模式: {fetch_mode})")


async def inline_parse(ctx: BotContext, client: Client, inline_query: InlineQuery) -> None:
    """Handle inline queries."""
    query = inline_query.query or ""
    help_result = build_help_result(await get_description(client), SOURCE_CODE_MARKUP)
    if not query:
        await answer_inline_query(client, inline_query, [help_result])
        return

    url_re = re.search(BILIBILI_URL_REGEX, query)
    if url_re is None:
        await answer_inline_query(client, inline_query, [help_result])
        return

    url = url_re.group(0)
    logger.info(f"Inline: {url}")

    mc = ctx.channel.media_constraints
    parsed_list = await ctx.registry.parse([url], mc)
    if not parsed_list:
        await answer_inline_query(client, inline_query, [help_result])
        return

    f = parsed_list[0]
    if isinstance(f, Exception):
        logger.warning(f"解析错误! {f}")
        results = [
            InlineQueryResultArticle(
                id=uuid4().hex,
                title="解析错误！",
                description=str(f),
                input_message_content=InputTextMessageContent(escape_html(str(f)), parse_mode=HTML),
            )
        ]
        await answer_inline_query(client, inline_query, results)
        return

    caption = format_caption_for_telegram(f, mc)

    if not f.media or not f.media.urls:
        results = [
            InlineQueryResultArticle(
                id=uuid4().hex,
                title=f.author.name,
                description=f.content,
                input_message_content=InputTextMessageContent(caption, parse_mode=HTML),
            )
        ]
        await answer_inline_query(client, inline_query, results)
        return

    results = build_media_inline_results(f, caption) or [help_result]
    await answer_inline_query(client, inline_query, results)


async def clear(ctx: BotContext, client: Client, message: Message) -> None:
    """Clear Redis cache for given URLs."""
    message, urls = await message_to_urls(client, message)
    if message is None:
        return
    if not urls:
        await message.reply_text("链接不正确", parse_mode=NO_PARSE)
        return
    logger.info(f"Clear: {urls}")

    mc = ctx.channel.media_constraints
    for f in await ctx.registry.parse(urls, mc):
        if isinstance(f, Exception):
            await message.reply_text(escape_html(str(f)), parse_mode=HTML)
            continue
        for _key, value in f.cache_keys.items():
            if value:
                await RedisCache().delete(value)
        await message.reply_text(
            f"清除缓存成功：{escape_html(f.url)}\n请重新获取",
            parse_mode=HTML,
        )


async def cancel(ctx: BotContext, client: Client, message: Message) -> None:
    """Cancel all queued tasks for the user."""
    user_id = message.from_user.id if message.from_user else message.chat.id
    assert ctx.queue_manager is not None
    cancelled_count = await ctx.queue_manager.cancel_user_tasks(user_id)

    if cancelled_count > 0:
        await message.reply_text(f"已取消 {cancelled_count} 个排队中的任务", parse_mode=NO_PARSE)
        logger.info(f"用户 {user_id} 通过 /cancel 命令取消了 {cancelled_count} 个任务")
    else:
        await message.reply_text("当前没有正在排队的任务", parse_mode=NO_PARSE)


async def tasks(ctx: BotContext, client: Client, message: Message) -> None:
    """Show current tasks for the user."""
    user_id = message.from_user.id if message.from_user else message.chat.id
    assert ctx.queue_manager is not None
    user_tasks = await ctx.queue_manager.get_user_tasks(user_id)

    if user_tasks:
        await message.reply_text(
            "当前正在进行的任务:\n" + "\n".join(escape_html(task) for task in user_tasks),
            parse_mode=HTML,
        )
    else:
        await message.reply_text("当前没有正在进行的任务", parse_mode=NO_PARSE)


def add_handlers(client: Client, ctx: BotContext) -> None:
    """Register all handlers on the client."""
    client.add_handler(MessageHandler(partial(start, ctx), filters.command("start")))
    client.add_handler(MessageHandler(partial(fetch, ctx), filters.command(["file", "cover"])))
    client.add_handler(MessageHandler(partial(cancel, ctx), filters.command("cancel")))
    client.add_handler(MessageHandler(partial(tasks, ctx), filters.command("tasks")))
    client.add_handler(MessageHandler(partial(clear, ctx), filters.command("clear")))
    client.add_handler(MessageHandler(partial(parse, ctx), filters.command(["parse", "video"])))
    client.add_handler(MessageHandler(partial(login, ctx), filters.command("login")))
    # 命令已由上面的 handler 处理，这里排除命令避免同一条消息被处理两次
    catch_all = (filters.text | filters.caption) & ~filters.command([command for command, _ in BOT_COMMANDS])
    client.add_handler(MessageHandler(partial(parse, ctx), catch_all))
    client.add_handler(InlineQueryHandler(partial(inline_parse, ctx)))


def _get_token() -> str:
    if os.environ.get("TOKEN"):
        return os.environ["TOKEN"]
    if len(sys.argv) >= 2:
        return sys.argv[1]
    logger.error("Need TOKEN.")
    sys.exit(1)


def _get_api_credentials() -> tuple[int, str]:
    """MTProto 应用凭据（my.telegram.org 申请，bot 与本地 Bot API 服务器共用）"""
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        logger.error("Need TELEGRAM_API_ID / TELEGRAM_API_HASH.")
        sys.exit(1)
    try:
        return int(api_id), api_hash
    except ValueError:
        logger.error(f"TELEGRAM_API_ID 配置无效: {api_id!r}")
        sys.exit(1)


def build_client(ctx: BotContext) -> Client:
    """Build the Kurigram client."""
    api_id, api_hash = _get_api_credentials()
    session_dir = Path(os.environ.get("TELEGRAM_SESSION_DIR", "."))
    session_dir.mkdir(parents=True, exist_ok=True)
    client = Client(
        name=SESSION_NAME,
        api_id=api_id,
        api_hash=api_hash,
        bot_token=_get_token(),
        workdir=str(session_dir),
        workers=_get_env_int("DISPATCHER_WORKERS", 24),
        max_concurrent_transmissions=_get_env_int("UPLOAD_WORKERS", 4),
        sleep_threshold=_get_env_int("FLOOD_SLEEP_THRESHOLD", 60),
    )
    add_handlers(client, ctx)
    return client


async def post_init(ctx: BotContext, client: Client) -> None:
    ctx.queue_manager = TelegramUploadQueueManager(
        registry=ctx.registry,
        constraints=ctx.channel.media_constraints,
        max_workers=_get_env_int("UPLOAD_WORKERS", 4),
        max_user_tasks=_get_env_int("MAX_USER_TASKS", 5),
        max_queue_size=_get_env_int("MAX_QUEUE_SIZE", 200),
        client=client,
    )
    await ctx.queue_manager.start_workers()
    await ctx.channel.start(ctx.registry)

    await client.set_bot_commands([BotCommand(command, description) for command, description in BOT_COMMANDS])
    bot_me = await client.get_me()
    logger.info(f"Bot @{bot_me.username} started.")
    logger.info(
        f"上传队列管理器已启动 ({ctx.queue_manager.max_workers} 个 worker, "
        f"单用户任务上限: {ctx.queue_manager.max_user_tasks})"
    )


async def post_shutdown(ctx: BotContext, client: Client) -> None:
    if ctx.login_task and not ctx.login_task.done():
        ctx.login_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ctx.login_task

    if ctx.queue_manager:
        await ctx.queue_manager.stop_workers()
        logger.info("上传队列管理器已停止")

    await ctx.channel.stop()


async def run_bot_async(channel, provider_registry: ProviderRegistry) -> None:
    """Run the Telegram bot inside an existing asyncio loop."""
    ctx = BotContext(registry=provider_registry, channel=channel)
    client = build_client(ctx)

    await client.start()
    try:
        await post_init(ctx, client)
        await asyncio.Event().wait()
    finally:
        with contextlib.suppress(Exception):
            await post_shutdown(ctx, client)
        with contextlib.suppress(Exception):
            await client.stop()
