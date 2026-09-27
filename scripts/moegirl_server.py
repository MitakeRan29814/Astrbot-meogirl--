"""Small MCP server exposing read-only Moegirl Baike lookup tools.

The server deliberately uses only Python's standard library so it can run in an
Astrobot installation without a package manager or a virtual environment.
"""

from __future__ import annotations

import html
import json
import re
import ssl
import sys
from typing import Any
from html.parser import HTMLParser
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    import certifi
except ImportError:
    certifi = None


SITE_URL = "https://zh.moegirl.org.cn/"
USER_AGENT = "Astrobot-Moegirl/0.1 (read-only API client)"
MAX_RESULT_CHARS = 12000


def ssl_context() -> ssl.SSLContext:
    """Use certifi's CA bundle when the host Python has no local CA store."""
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def fetch_html(path: str) -> str:
    request = Request(
        f"{SITE_URL.rstrip('/')}{path}",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urlopen(request, timeout=20, context=ssl_context()) as response:
        return response.read().decode("utf-8", errors="replace")


class SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []
        self.item: dict[str, str] | None = None
        self.section = ""
        self.buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "li" and "mw-search-result" in classes:
            self.item = {"title": "", "snippet": "", "url": ""}
            self.section = ""
            self.buffer = []
        elif self.item is not None and tag == "div" and "mw-search-result-heading" in classes:
            self.section = "title"
            self.buffer = []
        elif self.item is not None and tag == "div" and "searchresult" in classes:
            self.section = "snippet"
            self.buffer = []
        elif self.item is not None and tag == "a" and self.section == "title" and not self.item["url"]:
            self.item["url"] = attr.get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if self.item is None:
            return
        if tag == "div" and self.section in {"title", "snippet"}:
            text = clean_html_text("".join(self.buffer))
            self.item["title" if self.section == "title" else "snippet"] = text
            self.section = ""
            self.buffer = []
        elif tag == "li":
            if self.item["title"]:
                self.item["url"] = SITE_URL.rstrip("/") + self.item["url"]
                self.items.append(self.item)
            self.item = None

    def handle_data(self, data: str) -> None:
        if self.item is not None and self.section:
            self.buffer.append(data)


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.content_depth = 0
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag in {"script", "style", "noscript", "table", "figure"}:
            self.skip_depth = 1
            return
        if tag == "div" and "mw-parser-output" in classes:
            self.content_depth = 1
            return
        if self.content_depth:
            self.content_depth += 1
            if tag in {"p", "br", "li", "h1", "h2", "h3", "h4"}:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if self.content_depth:
            self.content_depth -= 1
            if tag in {"p", "li", "h1", "h2", "h3", "h4"}:
                self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.content_depth and not self.skip_depth:
            self.parts.append(data)


def clean_html_text(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def search_page(query: str, limit: int) -> dict[str, Any]:
    path = "/index.php?" + urlencode({"title": "Special:搜索", "search": query, "fulltext": "1", "profile": "default"})
    parser = SearchParser()
    parser.feed(fetch_html(path))
    items = parser.items[: max(1, min(limit, 10))]
    return {"query": query, "count": len(items), "results": items}


def page_html(title: str, include_source: bool = True) -> dict[str, Any]:
    path = "/index.php?" + urlencode({"title": title.replace(" ", "_")})
    source = fetch_html(path)
    parser = PageParser()
    parser.feed(source)
    if not parser.parts:
        raise RuntimeError(f"未找到词条：{title}")
    match = re.search(r"<title>\s*(.*?)\s+-\s+萌娘百科", source, re.I | re.S)
    actual_title = html.unescape(match.group(1).strip()) if match else title
    result = {"title": actual_title, "summary": clean_wikitext("".join(parser.parts))[:MAX_RESULT_CHARS]}
    if include_source:
        result["url"] = page_url(actual_title)
    return result


def clean_wikitext(value: str) -> str:
    """Turn common wiki markup into compact readable text."""
    value = re.sub(r"<ref[^>]*>.*?</ref>", "", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[https?://[^ ]+ ([^\]]+)\]", r"\1", value)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"'{2,5}", "", value)
    value = re.sub(r"^\s*[|!].*$", "", value, flags=re.M)
    value = re.sub(r"^\s*=+\s*(.*?)\s*=+\s*$", r"\1", value, flags=re.M)
    value = html.unescape(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def page_url(title: str) -> str:
    return f"{SITE_URL}wiki/{quote(title.replace(' ', '_'))}"


def search(query: str, limit: int) -> dict[str, Any]:
    return search_page(query, limit)


def page(title: str, include_source: bool = True) -> dict[str, Any]:
    return page_html(title, include_source)


TOOLS = [
    {
        "name": "moegirl_search",
        "description": "Search Moegirl Baike and return matching titles, snippets, and source links.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "moegirl_page",
        "description": "Fetch a Moegirl Baike page and return a readable text summary plus its source URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Exact or near-exact page title"},
                "include_source": {"type": "boolean", "default": True},
            },
            "required": ["title"],
        },
    },
]


def result_text(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized" or request_id is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "astrobot-moegirl", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = message.get("params", {}).get("name")
        arguments = message.get("params", {}).get("arguments", {})
        try:
            if name == "moegirl_search":
                query = str(arguments.get("query", "")).strip()
                if not query:
                    raise ValueError("query 不能为空")
                limit = max(1, min(10, int(arguments.get("limit", 5))))
                return {"jsonrpc": "2.0", "id": request_id, "result": result_text(search(query, limit))}
            if name == "moegirl_page":
                title = str(arguments.get("title", "")).strip()
                if not title:
                    raise ValueError("title 不能为空")
                include_source = bool(arguments.get("include_source", True))
                return {"jsonrpc": "2.0", "id": request_id, "result": result_text(page(title, include_source))}
            raise ValueError(f"未知工具：{name}")
        except Exception as exc:  # Return a useful tool error instead of killing the server.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"isError": True, "content": [{"type": "text", "text": str(exc)}]},
            }
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            return
        response = handle(message)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()
