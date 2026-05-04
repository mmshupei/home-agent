"""Markdown -> Telegram HTML renderer.

Why HTML and not Markdown:
- Telegram's legacy "Markdown" parse mode fails the *whole* message on a
  single unmatched `_` or `*`. That's intolerable for an agent that talks
  about filenames and code.
- Telegram's HTML parse mode requires only `<`, `>`, `&` to be escaped,
  supports `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a href>`, and
  `<blockquote>`, and ignores tags it doesn't recognise.

Design constraints:
- Pure-function leaf module. No I/O, no module-level state beyond the regexes.
  This is what makes hot-reload via `importlib.reload` safe in the bot loop.
- "Good enough" beats "complete". We render the markdown shapes the model
  actually emits (bold/italic/code/headings/bullets/links). We do NOT try to
  be a full CommonMark parser.
- Predictable degradation. Unrecognised constructs pass through as escaped
  text. Caller is expected to wrap the call in a try/except and fall back
  to plain-text send if Telegram still rejects the result.
"""
from __future__ import annotations

import html
import re

__all__ = ["render"]


# Order of operations matters; see render() docstring.
_FENCED_RE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_BOLD_DBL_AST_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_BOLD_DBL_UND_RE = re.compile(r"__([^_\n]+?)__")
# Italic: don't match across word characters (avoids `foo_bar_baz` and `2*3*4`).
_ITAL_AST_RE = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?!\*)")
_ITAL_UND_RE = re.compile(r"(?<![_\w])_([^_\n]+?)_(?!_)")
_STRIKE_RE = re.compile(r"~~([^~\n]+?)~~")

# Sentinel used to stash already-rendered HTML segments past the markdown
# transforms. \x00 is stripped by Telegram and never appears in real text.
_PLACEHOLDER_RE = re.compile(r"\x00PH(\d+)\x00")


def render(text: str) -> str:
    """Convert markdown-ish text to Telegram-safe HTML.

    Pipeline:
      1. Stash fenced code blocks   (```...```)            -> <pre>
      2. Stash inline code          (`...`)                -> <code>
      3. Stash links                ([text](url))          -> <a href>
      4. HTML-escape remaining text (so stray <, >, & are safe)
      5. Apply block transforms     (# headings, - bullets)
      6. Apply inline transforms    (**bold**, *italic*, ~~strike~~)
      7. Restore stashed segments

    Stashing first means the markdown transforms can't corrupt URLs or code,
    and the html.escape pass can't double-escape the segments we already
    escaped on the way in.
    """
    if not text:
        return text

    placeholders: list[str] = []

    def _stash(rendered: str) -> str:
        placeholders.append(rendered)
        return f"\x00PH{len(placeholders) - 1}\x00"

    def _fence_sub(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        body = html.escape(m.group(2))
        if lang:
            return _stash(
                f'<pre><code class="language-{html.escape(lang)}">{body}</code></pre>'
            )
        return _stash(f"<pre>{body}</pre>")

    def _inline_code_sub(m: re.Match) -> str:
        return _stash(f"<code>{html.escape(m.group(1))}</code>")

    def _link_sub(m: re.Match) -> str:
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        return _stash(f'<a href="{url}">{label}</a>')

    text = _FENCED_RE.sub(_fence_sub, text)
    text = _INLINE_CODE_RE.sub(_inline_code_sub, text)
    text = _LINK_RE.sub(_link_sub, text)

    # Escape everything else. Use quote=False because we're not in an attribute
    # value here; quoting " would just produce visual noise.
    text = html.escape(text, quote=False)

    # Block-level transforms (operate on whole lines).
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(2).strip()}</b>", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)

    # Inline transforms. Bold first (so ** doesn't get eaten as two italics).
    text = _BOLD_DBL_AST_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_DBL_UND_RE.sub(r"<b>\1</b>", text)
    text = _ITAL_AST_RE.sub(r"<i>\1</i>", text)
    text = _ITAL_UND_RE.sub(r"<i>\1</i>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)

    # Restore stashed segments last.
    text = _PLACEHOLDER_RE.sub(lambda m: placeholders[int(m.group(1))], text)

    return text


# ---------------------------------------------------------------------------
# Self-test: `python -m triggers.telegram_format`
# Keep these cases tied to real things the model emits in chat replies; if
# something renders wrong in production, add the failing case here first.
# ---------------------------------------------------------------------------
def _run_tests() -> int:
    cases: list[tuple[str, str, str]] = [
        ("plain", "hello world", "hello world"),
        ("amp escape", "tom & jerry", "tom &amp; jerry"),
        ("angle escape", "1 < 2 > 0", "1 &lt; 2 &gt; 0"),
        ("bold double-ast", "**bold**", "<b>bold</b>"),
        ("bold double-und", "__bold__", "<b>bold</b>"),
        ("italic single-ast", "this is *italic*.", "this is <i>italic</i>."),
        ("italic single-und", "this is _italic_.", "this is <i>italic</i>."),
        ("identifier underscore not italic", "use foo_bar_baz here", "use foo_bar_baz here"),
        ("inline code", "see `parse_mode` arg", "see <code>parse_mode</code> arg"),
        (
            "fenced code python",
            "```python\nx = 1\n```",
            '<pre><code class="language-python">x = 1\n</code></pre>',
        ),
        (
            "fenced code no lang",
            "```\nplain\n```",
            "<pre>plain\n</pre>",
        ),
        (
            "link",
            "see [the docs](https://example.com/x?y=1)",
            'see <a href="https://example.com/x?y=1">the docs</a>',
        ),
        ("heading", "# Title", "<b>Title</b>"),
        ("heading h3", "### Subtitle", "<b>Subtitle</b>"),
        ("bullet dash", "- item one\n- item two", "• item one\n• item two"),
        ("strike", "~~gone~~", "<s>gone</s>"),
        (
            "nested bold+italic",
            "**bold _italic_ done**",
            "<b>bold <i>italic</i> done</b>",
        ),
        (
            "code is sacred",
            "look at `<script>` literally",
            "look at <code>&lt;script&gt;</code> literally",
        ),
        (
            "asterisk inside code untouched",
            "`a*b*c` is code",
            "<code>a*b*c</code> is code",
        ),
        (
            "multiline mixed",
            "## Plan\n\n- step **one**\n- step *two*\n\nrun `make`.",
            "<b>Plan</b>\n\n• step <b>one</b>\n• step <i>two</i>\n\nrun <code>make</code>.",
        ),
        ("empty", "", ""),
    ]

    failed = 0
    for name, src, expected in cases:
        got = render(src)
        if got != expected:
            failed += 1
            print(f"FAIL  {name}")
            print(f"  input:    {src!r}")
            print(f"  expected: {expected!r}")
            print(f"  got:      {got!r}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_run_tests())
