"""Telegram caption 格式化（HTML 解析模式）

Provider 的 extra_markdown / content_markdown 按 MarkdownV2 转义（历史约定），
这里还原为纯文本再转成 HTML；其余字段本身是纯文本，直接转义。
"""

import html
import re

from ...model import Comment, MediaConstraints, ParsedContent

# 与 utils.escape_markdown 的转义字符集保持一致
_ESCAPE_RE = re.compile(r"\\([_*\[\]()~`>#+\-=|{}.!\\])")
_LINK_RE = re.compile(r"\[((?:\\.|[^\]\\])*)\]\(((?:\\.|[^)\\])*)\)")
_BLOCK_OPEN = "**>"
_BLOCK_CLOSE = "||"


def escape_html(text: str) -> str:
    """HTML 解析模式下的文本转义"""
    return html.escape(text) if text else ""


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(r"\1", text)


def _inline_html(text: str) -> str:
    """链接转 <a> 标签，其余部分剥离 MarkdownV2 转义后做 HTML 转义"""
    parts: list[str] = []
    pos = 0
    for match in _LINK_RE.finditer(text):
        parts.append(escape_html(_unescape(text[pos : match.start()])))
        label = escape_html(_unescape(match.group(1)))
        url = html.escape(_unescape(match.group(2)), quote=True)
        parts.append(f'<a href="{url}">{label}</a>')
        pos = match.end()
    parts.append(escape_html(_unescape(text[pos:])))
    return "".join(parts)


def markdown_v2_to_html(text: str) -> str:
    """Provider 的 MarkdownV2 字符串转 HTML，含 **>...|| 折叠引用块"""
    if not text:
        return ""
    parts: list[str] = []
    rest = text
    while _BLOCK_OPEN in rest:
        head, _, tail = rest.partition(_BLOCK_OPEN)
        block, _, rest = tail.partition(_BLOCK_CLOSE)
        # MarkdownV2 的行首 ">" 只是引用标记，HTML 用 blockquote 实体表达
        lines = [line.removeprefix(">") for line in block.split("\n")]
        parts.append(_inline_html(head))
        quoted = _inline_html("\n".join(lines))
        parts.append(f"<blockquote expandable>{quoted}</blockquote>")
    parts.append(_inline_html(rest))
    return "".join(parts)


def _user_html(name: str, uid: str) -> str:
    if name and uid:
        return f'<a href="https://space.bilibili.com/{uid}">@{escape_html(name)}</a>'
    return ""


def _format_comment_html(comments: list[Comment]) -> str:
    result = ""
    for c in comments:
        user = _user_html(c.author.name, c.author.uid)
        prefix = "💬" if c.is_target else "🔝"
        result += f"{prefix}&gt; {user}:\n{escape_html(c.text)}\n"
    return result


def _try_append_within_limit(components: list[str], text: str, max_len: int) -> bool:
    if not text:
        return True
    test_content = "".join([*components, text])
    if len(test_content) < max_len:
        components.append(text)
        return True
    return False


def _quote(body: str) -> str:
    return f"\n<blockquote expandable>{body}</blockquote>"


def format_caption_for_telegram(content: ParsedContent, constraints: MediaConstraints) -> str:
    """Format ParsedContent into a Telegram HTML caption string."""
    max_len = constraints.caption_max_length

    components = [f"{markdown_v2_to_html(content.extra_markdown) or escape_html(content.url)}\n"]

    if content.author.name:
        user_html = _user_html(content.author.name, content.author.uid)
        if not _try_append_within_limit(components, f"{user_html}:", max_len):
            return "".join(components)

    body = markdown_v2_to_html(content.content_markdown) if content.content_markdown else escape_html(content.content)
    if body and not body.endswith("\n"):
        body += "\n"

    for text in [body, _format_comment_html(content.comments)]:
        if text and not _try_append_within_limit(components, _quote(text), max_len):
            return "".join(components)

    return "".join(components)
