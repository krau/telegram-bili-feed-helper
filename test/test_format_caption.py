"""测试 channel/telegram/formatting.py — HTML caption 与实体渲染"""

from pyrogram.parser.html import HTML

from biliparser.channel.telegram.bot import format_caption_for_telegram
from biliparser.model import Author, Comment, MediaConstraints, ParsedContent


def _mc(max_len=1024):
    return MediaConstraints(
        max_upload_size=2 * 1024 * 1024 * 1024,
        max_download_size=2 * 1024 * 1024 * 1024,
        caption_max_length=max_len,
    )


async def _parse(caption: str) -> tuple[str, list]:
    """用 Kurigram 的 HTML 解析器还原 caption 的文本与实体（等价 Telegram 侧渲染）"""
    result = await HTML(None).parse(caption)
    return result["message"], result["entities"]


def _types(entities: list) -> list[str]:
    return [type(entity).__name__ for entity in entities]


def test_basic_url_only():
    pc = ParsedContent(url="https://bilibili.com/video/BV123", author=Author())
    caption = format_caption_for_telegram(pc, _mc())
    assert "bilibili" in caption


async def test_extra_markdown_becomes_link():
    pc = ParsedContent(
        url="https://bilibili.com/video/BV123",
        author=Author(),
        extra_markdown="[标题](https://bilibili.com/video/BV123)",
    )
    text, entities = await _parse(format_caption_for_telegram(pc, _mc()))

    assert text.startswith("标题")
    link = next(e for e in entities if type(e).__name__ == "MessageEntityTextUrl")
    assert link.url == "https://bilibili.com/video/BV123"
    assert next(line for line in text.splitlines() if line) == "标题"


def test_with_author():
    pc = ParsedContent(url="https://bilibili.com/video/BV123", author=Author(name="UP主", uid="12345"))
    caption = format_caption_for_telegram(pc, _mc())
    assert "@UP主" in caption
    assert "space.bilibili.com/12345" in caption


def test_author_no_uid():
    """没有 uid 时不生成主页链接"""
    pc = ParsedContent(url="https://bilibili.com/video/BV123", author=Author(name="UP主", uid=""))
    caption = format_caption_for_telegram(pc, _mc())
    assert "space.bilibili.com" not in caption


async def test_content_wrapped_in_expandable_blockquote_and_spoiler():
    """content 应渲染为折叠引用 + 剧透"""
    pc = ParsedContent(url="https://bilibili.com", author=Author(), content="测试内容")
    text, entities = await _parse(format_caption_for_telegram(pc, _mc()))

    assert "测试内容" in text
    quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
    assert quote.collapsed is True
    assert "MessageEntitySpoiler" in _types(entities)


async def test_multiline_content_stays_inside_quote():
    pc = ParsedContent(url="https://bilibili.com", author=Author(), content="第一行\n第二行\n第三行")
    text, entities = await _parse(format_caption_for_telegram(pc, _mc()))

    quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
    quoted = text[quote.offset : quote.offset + quote.length]
    assert "第一行" in quoted and "第三行" in quoted
    assert "\n> " not in text


async def test_user_content_is_escaped_not_parsed():
    """正文里的 HTML 标签必须被转义，不能变成实体"""
    pc = ParsedContent(url="https://bilibili.com", author=Author(), content="<b>bold</b> & <a href='x'>link</a>")
    text, entities = await _parse(format_caption_for_telegram(pc, _mc()))

    assert "<b>bold</b>" in text
    assert "MessageEntityBold" not in _types(entities)


async def test_content_markdown_is_unescaped_as_plain_text():
    """content_markdown 里的 MarkdownV2 转义应还原成纯文本"""
    pc = ParsedContent(
        url="https://bilibili.com",
        author=Author(),
        content="plain text",
        content_markdown="already\\_escaped",
    )
    text, entities = await _parse(format_caption_for_telegram(pc, _mc()))

    assert "already_escaped" in text
    assert "already\\_escaped" not in text
    assert "MessageEntityItalic" not in _types(entities)


async def test_video_desc_block_becomes_quote():
    """Provider 的 **>...|| 描述块转成折叠引用"""
    pc = ParsedContent(
        url="https://bilibili.com",
        author=Author(),
        extra_markdown="[标题](https://bilibili.com)\n**>第一行\n>第二行||",
    )
    text, entities = await _parse(format_caption_for_telegram(pc, _mc()))

    assert "第一行\n第二行" in text
    quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
    assert quote.collapsed is True


def test_with_comments():
    pc = ParsedContent(
        url="https://bilibili.com/video/BV123",
        author=Author(),
        comments=[
            Comment(author=Author(name="评论者A", uid="111"), text="好看", is_target=True),
            Comment(author=Author(name="评论者B", uid="222"), text="顶", is_top=True),
        ],
    )
    caption = format_caption_for_telegram(pc, _mc())
    assert "评论者A" in caption
    assert "评论者B" in caption


def test_comment_prefixes():
    pc = ParsedContent(
        url="https://bilibili.com",
        author=Author(),
        comments=[
            Comment(author=Author(name="user", uid="1"), text="msg", is_target=True),
            Comment(author=Author(name="top", uid="2"), text="msg", is_top=True),
        ],
    )
    caption = format_caption_for_telegram(pc, _mc())
    assert "💬&gt;" in caption
    assert "🔝&gt;" in caption


def test_truncation():
    """超长 content 应被截断（不追加到 components）"""
    pc = ParsedContent(
        url="https://bilibili.com/video/BV123",
        author=Author(name="UP主", uid="12345"),
        content="A" * 2000,
    )
    caption = format_caption_for_telegram(pc, _mc(max_len=100))
    assert len(caption) <= 100


def test_empty_content():
    pc = ParsedContent(url="https://bilibili.com", author=Author())
    caption = format_caption_for_telegram(pc, _mc())
    assert caption  # 至少有 URL
