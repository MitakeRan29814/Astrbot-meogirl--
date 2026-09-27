"""AstrBot plugin for read-only Moegirl Baike lookup."""

from __future__ import annotations

import asyncio
from io import BytesIO
import html
import os
import re
import ssl
import tempfile
from typing import Any
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import certifi
except ImportError:  # AstrBot installations may not have optional dependencies yet.
    certifi = None

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


SITE_URL = "https://zh.moegirl.org.cn/"
USER_AGENT = "AstrBot-Moegirl/1.0 (read-only API client)"


def _ssl_context() -> ssl.SSLContext:
    """Use certifi's current CA bundle when the host Python lacks root CAs."""
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _fetch_html(path: str) -> str:
    request = Request(
        f"{SITE_URL.rstrip('/')}{path}",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urlopen(request, timeout=20, context=_ssl_context()) as response:
        return response.read().decode("utf-8", errors="replace")


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []
        self._item: dict[str, str] | None = None
        self._section = ""
        self._depth = 0
        self._buffer: list[str] = []
        self._title_anchor = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "li" and "mw-search-result" in classes:
            self._item = {"title": "", "snippet": "", "url": ""}
            self._depth = 1
            self._section = ""
            return
        if self._item is None:
            return
        self._depth += 1
        if tag == "div" and "mw-search-result-heading" in classes:
            self._section = "heading"
            self._buffer = []
        elif tag == "div" and "searchresult" in classes:
            self._section = "snippet"
            self._buffer = []
        if tag == "a" and self._section == "heading" and not self._item["url"]:
            self._item["url"] = attr.get("href") or ""
            self._title_anchor = True

    def handle_endtag(self, tag: str) -> None:
        if self._item is None:
            return
        if tag == "div" and self._section in {"heading", "snippet"}:
            text = _clean_snippet("".join(self._buffer))
            if self._section == "heading":
                self._item["title"] = text.split("  ", 1)[0].strip()
            else:
                self._item["snippet"] = text
            self._section = ""
            self._buffer = []
        if tag == "li":
            if self._item["title"]:
                self._item["url"] = SITE_URL.rstrip("/") + self._item["url"]
                self.items.append(self._item)
            self._item = None
            self._depth = 0
            self._section = ""

    def handle_data(self, data: str) -> None:
        if self._item is not None and self._section:
            self._buffer.append(data)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._content_depth = 0
        self._skip_depth = 0
        self.image_candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "meta" and (attr.get("property") or attr.get("name") or "").lower() in {"og:image", "twitter:image"}:
            value = attr.get("content") or ""
            if value:
                self.image_candidates.insert(0, value)
        if tag == "img" and self._content_depth:
            value = attr.get("src") or attr.get("data-src") or ""
            if value:
                self.image_candidates.append(value)
        if self._skip_depth:
            self._skip_depth += 1
            return
        if tag in {"script", "style", "noscript", "table", "figure"}:
            self._skip_depth = 1
            return
        if tag == "div" and "mw-parser-output" in classes:
            self._content_depth = 1
            return
        if self._content_depth:
            self._content_depth += 1
            if tag in {"p", "br", "li", "h1", "h2", "h3", "h4"}:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if self._content_depth:
            self._content_depth -= 1
            if tag in {"p", "li", "h1", "h2", "h3", "h4"}:
                self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._content_depth and not self._skip_depth:
            self.parts.append(data)


def _parse_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    path = "/index.php?" + urlencode({"title": "Special:搜索", "search": query, "fulltext": "1", "profile": "default"})
    parser = _SearchParser()
    parser.feed(_fetch_html(path))
    return parser.items[: max(1, min(limit, 10))]


def _parse_page(title: str) -> tuple[str, str, str]:
    path = "/index.php?" + urlencode({"title": title.replace(" ", "_")})
    parser = _PageParser()
    source = _fetch_html(path)
    parser.feed(source)
    if not parser.parts:
        raise RuntimeError(f"未找到词条：{title}")
    actual = re.search(r"<title>\s*(.*?)\s+-\s+萌娘百科", source, re.I | re.S)
    actual_title = html.unescape(actual.group(1).strip()) if actual else title
    text = _clean_wikitext("".join(parser.parts))
    image_url = ""
    for candidate in parser.image_candidates:
        candidate_url = urljoin(SITE_URL, candidate)
        lowered = candidate_url.casefold()
        if lowered.endswith(".svg") or "logo" in lowered or "disambig" in lowered or "default" in lowered:
            continue
        image_url = candidate_url
        break
    return actual_title, text[:8000], image_url


def _page_url(title: str) -> str:
    return f"{SITE_URL}wiki/{quote(title.replace(' ', '_'))}"


def _clean_snippet(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _clean_wikitext(value: str) -> str:
    value = re.sub(r"<ref[^>]*>.*?</ref>", "", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[https?://[^ ]+ ([^\]]+)\]", r"\1", value)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"'{2,5}", "", value)
    value = re.sub(r"^\s*[|!].*$", "", value, flags=re.M)
    value = re.sub(r"^\s*=+\s*(.*?)\s*=+\s*$", r"\1", value, flags=re.M)
    value = html.unescape(value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    return _parse_search(query, limit)


def _pick_subject(query: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer a standalone person/work entry over list or navigation pages."""
    blocked = ("分类:", "模板:", "特殊:", "帮助:", "列表", "章节", "目录", "消歧义")
    query = query.strip().casefold()
    candidates = [
        item for item in results
        if item.get("title", "").strip() and not any(mark in item["title"] for mark in blocked)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item["title"].strip().casefold() == query,
            query in item["title"].strip().casefold(),
            -len(item["title"]),
        ),
        reverse=True,
    )
    return candidates[0]


def _page(title: str) -> tuple[str, str, str]:
    return _parse_page(title)


def _format_search(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f"萌娘百科没有找到“{query}”的匹配词条。"
    lines = [f"萌娘百科搜索：{query}"]
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item['title']}\n   {item['snippet']}\n   {item['url']}")
    return "\n".join(lines)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "C:/Windows/Fonts/simhei.ttf" if not bold else "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/Deng.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in re.split(r"\n+", text):
        current = ""
        for char in paragraph.strip():
            candidate = current + char
            if current and font.getlength(candidate) > width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines


def _download_image(image_url: str) -> Image.Image | None:
    if not image_url or not image_url.startswith(("http://", "https://")):
        return None
    try:
        request = Request(image_url, headers={"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})
        with urlopen(request, timeout=15, context=_ssl_context()) as response:
            data = response.read(8 * 1024 * 1024)
        image = Image.open(BytesIO(data)).convert("RGB")
        return image.copy()
    except Exception:
        return None


def _make_card(title: str, summary: str, url: str, kind: str = "词条", image_url: str = "") -> str:
    width, margin = 1200, 72
    title_font, label_font, body_font, small_font = _font(58, True), _font(28), _font(30), _font(22)
    body_lines = _wrap_text(summary or "暂无可提取的简介。", body_font, width - margin * 2)[:16]
    cover = _download_image(image_url)
    cover_height = 320 if cover is not None else 0
    height = 230 + cover_height + len(body_lines) * 48 + 150
    image = Image.new("RGB", (width, height), "#101827")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=30, fill="#17243a", outline="#4cc9f0", width=3)
    draw.rectangle((24, 24, width - 24, 42), fill="#4cc9f0")
    draw.text((margin, 78), title[:28], font=title_font, fill="#f7fbff")
    draw.rounded_rectangle((margin, 160, margin + 150, 204), radius=18, fill="#276b80")
    draw.text((margin + 22, 168), kind, font=label_font, fill="#dff8ff")
    y = 250
    if cover is not None:
        cover_box = (margin, y, width - margin, y + cover_height)
        fitted = ImageOps.fit(cover, (cover_box[2] - cover_box[0], cover_box[3] - cover_box[1]), method=Image.Resampling.LANCZOS)
        image.paste(fitted, (cover_box[0], cover_box[1]))
        draw = ImageDraw.Draw(image)
        draw.rectangle(cover_box, outline="#4cc9f0", width=3)
        y += cover_height + 34
    for line in body_lines:
        draw.text((margin, y), line, font=body_font, fill="#d8e5f2")
        y += 48
    draw.line((margin, y + 12, width - margin, y + 12), fill="#38506b", width=2)
    draw.text((margin, y + 38), "来源：萌娘百科", font=small_font, fill="#9fc1d8")
    draw.text((margin, y + 76), url[:95], font=small_font, fill="#72d6f5")
    fd, path = tempfile.mkstemp(prefix="moegirl-card-", suffix=".png")
    os.close(fd)
    image.save(path, format="PNG", optimize=True)
    if cover is not None:
        cover.close()
    image.close()
    return path


def _argument(message: str, command: str) -> str:
    """Support AstrBot versions that keep the command in message_str."""
    value = message.strip()
    for prefix in (command, f"/{command}"):
        if value == prefix:
            return ""
        if value.startswith(prefix + " "):
            return value[len(prefix) :].strip()
    return value


@register("astrbot_plugin_moegirl", "Local developer", "萌娘百科主体图文卡", "1.2.0")
class MoegirlPlugin(Star):
    """Provide /萌娘搜索 and /萌娘词条 commands."""

    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("萌娘搜索")
    async def moegirl_search(self, event: AstrMessageEvent):
        query = _argument(event.message_str, "萌娘搜索")
        if not query:
            yield event.plain_result("用法：/萌娘搜索 关键词")
            return
        try:
            results = await asyncio.to_thread(_search, query)
            item = _pick_subject(query, results)
            if item is None:
                yield event.plain_result(f"萌娘百科没有找到“{query}”的主体词条。")
                return
            title, summary, image_url = await asyncio.to_thread(_page, item["title"])
            image_path = await asyncio.to_thread(_make_card, title, summary, _page_url(title), "主体", image_url)
            try:
                yield event.image_result(image_path)
            finally:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass
        except Exception as exc:
            yield event.plain_result(f"萌娘百科查询失败：{exc}")

    @filter.command("萌娘词条")
    async def moegirl_page(self, event: AstrMessageEvent):
        title = _argument(event.message_str, "萌娘词条")
        if not title:
            yield event.plain_result("用法：/萌娘词条 词条名")
            return
        try:
            actual_title, summary, image_url = await asyncio.to_thread(_page, title)
            image_path = await asyncio.to_thread(_make_card, actual_title, summary, _page_url(actual_title), "词条", image_url)
            try:
                yield event.image_result(image_path)
            finally:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass
        except Exception as exc:
            yield event.plain_result(f"萌娘百科查询失败：{exc}")
