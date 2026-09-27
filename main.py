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


def _clean_html_fragment(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "、", value, flags=re.I)
    value = re.sub(r"<img[^>]*>", "", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip(" 、\t\r\n")


def _normalize_profile_value(label: str, value: str) -> str:
    """Remove citations and template commentary from a profile value."""
    value = re.sub(r"\[[^\]]+\]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" 、，,；;。")
    measurement_labels = {"三围", "三围尺寸", "BWH"}
    if label in measurement_labels:
        # Prefer an explicit B/W/H form and discard citation markers.
        bwh = re.search(
            r"B\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\D+"
            r"W\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\D+"
            r"H\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)",
            value, re.I,
        )
        if bwh:
            return f"B:{bwh.group(1)} / W:{bwh.group(2)} / H:{bwh.group(3)}"
        # Chinese entries sometimes include cup/band notes such as
        # “86.8（68.2底围）70E/58.7/84.8”. Keep only the three measurements.
        plain = re.sub(r"（[^）]*）|\([^)]*\)", "", value)
        numbers = re.findall(r"[0-9]+(?:\.[0-9]+)?", plain)
        if re.search(r"[0-9]+(?:\.[0-9]+)?[A-G]", plain, re.I) and len(numbers) >= 4:
            numbers = [numbers[0], numbers[-2], numbers[-1]]
        if len(numbers) >= 3:
            return " / ".join(numbers[:3])
    return value[:120]


def _extract_profile(source: str) -> dict[str, str]:
    """Extract only character/work facts from the page infobox."""
    labels = {
        "本名", "别号", "别名", "发色", "瞳色", "身高", "年龄", "生日", "星座",
        "萌点", "活动范围", "现所属团体", "所属团体", "学校", "职业", "种族",
        "声优", "配音", "代表色",
        # Body measurements appear under several names across Moegirl
        # templates. Keep all of them so character pages do not lose BWH data.
        "三围", "三围尺寸", "BWH", "胸围", "腰围", "臀围",
        "罩杯", "体重",
    }
    profile: dict[str, str] = {}
    # Parse classic table rows first.  Newer Moegirl templates render the same
    # fields as two-column flex divs, so handle those rows as a second format.
    area = source
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", area, re.I | re.S):
        cells = re.findall(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", row, re.I | re.S)
        if len(cells) < 2:
            continue
        label = re.sub(r"[：:]$", "", _clean_html_fragment(cells[0]))
        if label not in labels:
            continue
        value = _normalize_profile_value(label, _clean_html_fragment(" ".join(cells[1:])))
        if label == "本名":
            value = re.sub(r"\s*\(.*?\)", "", value).strip()
        if label in {"别号", "别名"}:
            value = value.replace("<del>", "").replace("</del>", "")
        if label == "萌点":
            value = "、".join(part.strip() for part in value.split("、") if part.strip()[:1] not in {"一"})
            value = "、".join(value.split("、")[:8])
        if value and label not in profile:
            profile[label] = value[:120]
    flex_rows = re.findall(
        r"<div[^>]*style\s*=\s*['\"]display:\s*flex;\s*margin:\s*3px 0;[^>]*>"
        r"\s*<div[^>]*>(.*?)</div>\s*<div[^>]*>(.*?)</div>\s*</div>",
        area, re.I | re.S,
    )
    for raw_label, raw_value in flex_rows:
        label = re.sub(r"[：:]$", "", _clean_html_fragment(raw_label))
        value = _normalize_profile_value(label, _clean_html_fragment(raw_value))
        if label not in labels or not value or label in profile:
            continue
        if label in {"别号", "别名"}:
            value = value.replace("<del>", "").replace("</del>", "")
        if label == "萌点":
            value = "、".join(part.strip() for part in value.split("、") if part.strip()[:1] not in {"一"})
            value = "、".join(value.split("、")[:8])
        profile[label] = value[:120]
    # Some templates split B/W/H into three separate rows. Present them as a
    # single compact trait when a combined 三围 field is absent.
    if "三围" not in profile and "三围尺寸" not in profile:
        measurements = [
            ("胸围", profile.get("胸围")),
            ("腰围", profile.get("腰围")),
            ("臀围", profile.get("臀围")),
        ]
        values = [value for _, value in measurements if value]
        if len(values) >= 2:
            profile["三围"] = _normalize_profile_value("三围", " / ".join(values))
    return profile


def _parse_page(title: str) -> tuple[str, str, str, dict[str, str]]:
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
    profile = _extract_profile(source)
    return actual_title, _profile_summary(actual_title, profile, text), image_url, profile


def _page_url(title: str) -> str:
    return f"{SITE_URL}wiki/{quote(title.replace(' ', '_'))}"


def _profile_summary(title: str, profile: dict[str, str], fallback: str) -> str:
    """Build a short description from the extracted subject traits."""
    parts: list[str] = []
    visual = []
    if profile.get("发色"):
        visual.append(profile["发色"])
    if profile.get("瞳色"):
        visual.append(profile["瞳色"])
    if visual:
        parts.append(f"外观特征为{'、'.join(visual)}")
    if profile.get("萌点"):
        parts.append(f"主要萌点：{profile['萌点']}")
    if profile.get("职业") or profile.get("所属团体") or profile.get("现所属团体"):
        group = profile.get("所属团体") or profile.get("现所属团体")
        role = profile.get("职业")
        parts.append(f"身份为{role or '角色'}，所属{group}")
    if profile.get("学校"):
        parts.append(f"就读于{profile['学校']}")
    context = _compact_summary(fallback)
    for sentence in re.split(r"(?<=[。！？!?])", context):
        sentence = sentence.strip("。！？!? \t\r\n")
        if len(sentence) < 8:
            continue
        if any(sentence in part or part in sentence for part in parts):
            continue
        parts.append(sentence)
        if len(parts) >= 5:
            break
    if parts:
        return "。".join(parts[:4]) + "。"
    return _compact_summary(fallback)


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


def _compact_summary(value: str, limit: int = 460) -> str:
    """Keep subject facts and discard wiki maintenance/editorial noise."""
    noise = (
        "编辑组", "编辑前请", "条目编辑", "编辑规范", "使用指南",
        "Wiki入门", "诚邀", "欢迎正在阅读", "欢迎加入", "维护",
        "投稿", "招募", "公告", "目录", "参考资料", "外部链接",
        "脚注", "分类:", "模板:", "特殊:", "本页面", "本条目",
        "祝您在萌娘百科度过愉快的时光", "欢迎阅读", "欢迎来到萌娘百科",
    )
    useful = (
        "是", "为", "来自", "登场", "角色", "人物", "作品",
        "主角", "主人公", "身份", "所属", "性格", "特点",
        "特征", "能力", "外貌", "形象", "外号", "别名",
        "昵称", "本名", "原名", "又名", "称为", "擅长",
        "喜欢", "讨厌", "种族", "职业", "配音",
    )
    story_noise = (
        "小时候", "幼年", "童年", "后来", "之后", "故事", "剧情",
        "经历", "学校", "学院", "入学", "毕业", "第几话", "第几集",
        "某日", "某天", "事件", "回忆", "过去", "得知", "发现", "编辑组",
    )
    value = re.sub(r"[ \t]+", " ", value)
    raw_sentences = re.split(r"(?<=[。！？!?；;])\s*|\n+", value)
    kept: list[str] = []
    for raw in raw_sentences:
        sentence = re.sub(r"^[|!*=：:、·\-]+", "", raw).strip()
        sentence = re.sub(r"\s+", " ", sentence)
        if len(sentence) < 6 or any(marker in sentence for marker in noise + story_noise):
            continue
        if any(marker in sentence for marker in useful):
            kept.append(sentence)
        if len(kept) >= 3:
            break
    if not kept:
        kept = [part.strip() for part in re.split(r"\n+", value) if len(part.strip()) >= 6][:3]
    summary = "".join(f"{part}。" if not part.endswith(("。", "！", "？", "!", "?")) else part for part in kept)
    return summary[:360].rstrip("，、；; " ) + ("…" if len(summary) > 360 else "")


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


def _page(title: str) -> tuple[str, str, str, dict[str, str]]:
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


def _paste_contain(canvas: Image.Image, source: Image.Image, frame: tuple[int, int, int, int],
                   background: str = "#0d1828") -> None:
    """Place an image inside a frame without cropping any part of it."""
    left, top, right, bottom = frame
    frame_width, frame_height = right - left, bottom - top
    # Draw the frame first so portrait images have intentional, balanced side
    # margins and landscape images have intentional top/bottom letterboxing.
    canvas.paste(background, (left, top, right, bottom))
    contained = ImageOps.contain(source, (frame_width - 12, frame_height - 12),
                                 method=Image.Resampling.LANCZOS)
    x = left + (frame_width - contained.width) // 2
    y = top + (frame_height - contained.height) // 2
    canvas.paste(contained, (x, y))


def _media_size(source: Image.Image, max_width: int, max_height: int | None = None) -> tuple[int, int]:
    """Return a proportional display size; the returned frame has no letterbox."""
    scale = max_width / max(1, source.width)
    if max_height is not None:
        scale = min(scale, max_height / max(1, source.height))
    # Small source images can be enlarged to use the available card column,
    # while the original aspect ratio remains unchanged.
    return max(1, round(source.width * scale)), max(1, round(source.height * scale))


def _paste_media(canvas: Image.Image, source: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Paste an image into an exact-ratio frame, without black padding."""
    left, top, right, bottom = box
    resized = source.resize((right - left, bottom - top), Image.Resampling.LANCZOS)
    canvas.paste(resized, (left, top))


def _make_card(title: str, summary: str, url: str, kind: str = "词条", image_url: str = "", profile: dict[str, str] | None = None) -> str:
    width, margin = 1200, 72
    title_font, label_font, body_font, small_font = _font(58, True), _font(28), _font(30), _font(22)
    profile = profile or {}
    profile_order = ["本名", "别号", "别名", "发色", "瞳色", "身高", "体重", "三围", "三围尺寸", "胸围", "腰围", "臀围", "罩杯", "年龄", "生日", "星座", "萌点", "所属团体", "现所属团体", "学校", "种族", "职业", "声优", "配音", "代表色"]
    profile_rows = [(label, profile[label]) for label in profile_order if profile.get(label)]
    cover = _download_image(image_url)
    vertical = cover is not None and cover.height > cover.width * 1.15
    media_top = 250
    # Build the media frame from the source aspect ratio. Fixed-height contain
    # boxes create visible dark bars around transparent character renders.
    if cover is not None and vertical:
        media_width, media_height = _media_size(cover, 420, 600)
        portrait_top = media_top + 42
        portrait_box = (width - margin - media_width, portrait_top, width - margin, portrait_top + media_height)
        body_width = portrait_box[0] - margin - 28
    elif cover is not None:
        media_width = width - margin * 2
        media_height = min(360, max(1, round(media_width * cover.height / max(1, cover.width))))
        media_width = max(1, round(media_height * cover.width / max(1, cover.height)))
        media_left = margin + (width - margin * 2 - media_width) // 2
        portrait_box = (media_left, media_top, media_left + media_width, media_top + media_height)
        # The image is centered above the content; the table and description
        # below it use the complete card width.
        body_width = width - margin * 2
    else:
        media_width = media_height = 0
        portrait_box = None
        body_width = width - margin * 2
    # Keep several concise lines of subject traits.  They are measured before
    # creating the canvas so the footer always remains inside the image.
    feature_lines = _wrap_text(summary or "暂无可提取的主体特征。", body_font, width - margin * 2)[:5]
    row_height = 48
    # Measure every row before creating the canvas. This prevents long traits
    # from pushing the footer past the bottom edge or into the image column.
    measured_rows: list[tuple[str, list[str], int]] = []
    for label, value in profile_rows[:16]:
        value_lines = _wrap_text(value, small_font, body_width - 190)[:2]
        measured_rows.append((label, value_lines, max(row_height, len(value_lines) * 28 + 12)))
    table_height = sum(item[2] for item in measured_rows)
    content_bottom = portrait_box[3] if portrait_box else media_top
    table_start = media_top if vertical or cover is None else portrait_box[3] + 34
    table_bottom = table_start + 42 + table_height + 20 if measured_rows else table_start
    summary_bottom = max(table_bottom, content_bottom) + 42 + len(feature_lines) * 42 + 190
    height = max(900, summary_bottom + 36)
    image = Image.new("RGB", (width, height), "#101827")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=30, fill="#17243a", outline="#4cc9f0", width=3)
    draw.rectangle((24, 24, width - 24, 42), fill="#4cc9f0")
    draw.text((margin, 78), title[:28], font=title_font, fill="#f7fbff")
    draw.rounded_rectangle((margin, 160, margin + 150, 204), radius=18, fill="#276b80")
    draw.text((margin + 22, 168), kind, font=label_font, fill="#dff8ff")
    y = media_top
    if cover is not None and not vertical:
        # Works/cover art stays in a centered, wide frame.  contain() keeps the
        # full cover visible instead of cutting off its title or characters.
        cover_box = portrait_box
        _paste_media(image, cover, cover_box)
        draw = ImageDraw.Draw(image)
        draw.rectangle(cover_box, outline="#4cc9f0", width=3)
        y = cover_box[3] + 34
    if cover is not None and vertical:
        # Portrait character art is a separate right-hand media column.  The
        # The portrait shares the first data-row baseline and uses its own
        # aspect ratio, so the subject remains visually anchored to its facts.
        cover_box = portrait_box
        _paste_media(image, cover, cover_box)
        draw = ImageDraw.Draw(image)
        draw.rectangle(cover_box, outline="#4cc9f0", width=3)
        body_width = cover_box[0] - margin - 28
    if profile_rows:
        draw.text((margin, y), "主体特征", font=label_font, fill="#73d8f5")
        y += 42
        for label, value_lines, row_h in measured_rows:
            draw.rectangle((margin, y, margin + 170, y + row_h), fill="#276b80", outline="#38506b", width=2)
            draw.rectangle((margin + 170, y, margin + body_width, y + row_h), fill="#1d3048", outline="#38506b", width=2)
            draw.text((margin + 16, y + 10), label, font=small_font, fill="#dff8ff")
            for index, line in enumerate(value_lines):
                draw.text((margin + 188, y + 8 + index * 27), line, font=small_font, fill="#e7f0f6")
            y += row_h
        y += 20
    # A centered media layout uses the full card width for the description.
    # This keeps the feature text flat under the image instead of leaving it
    # trapped in the narrow left column.
    feature_left = margin
    feature_width = width - margin * 2
    draw.text((feature_left, y), "主体特点", font=label_font, fill="#73d8f5")
    y += 42
    for line in feature_lines:
        draw.text((feature_left, y), line, font=body_font, fill="#d8e5f2")
        y += 42
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


@register("astrbot_plugin_moegirl", "Local developer", "萌娘百科主体档案卡", "1.10.0")
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
            title, summary, image_url, profile = await asyncio.to_thread(_page, item["title"])
            image_path = await asyncio.to_thread(_make_card, title, summary, _page_url(title), "主体", image_url, profile)
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
            actual_title, summary, image_url, profile = await asyncio.to_thread(_page, title)
            image_path = await asyncio.to_thread(_make_card, actual_title, summary, _page_url(actual_title), "词条", image_url, profile)
            try:
                yield event.image_result(image_path)
            finally:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass
        except Exception as exc:
            yield event.plain_result(f"萌娘百科查询失败：{exc}")
