"""Static offline document paths and resource safety, without a browser engine."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import PurePosixPath
from pathlib import Path
import os
import stat
import re
from urllib.parse import urlsplit

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def html_path(source: str) -> str:
    if not isinstance(source, str) or not source or "\\" in source or "\x00" in source or ":" in source or source.startswith("/") or any(p in {"", ".", ".."} for p in source.split("/")):
        raise ValueError("Document path is unsafe.")
    if source == "LICENSE":
        return "docs/html/legal/SOFTWARE_LICENSE_AGREEMENT.html"
    if source == "THIRD_PARTY_NOTICES.md":
        return "docs/html/legal/THIRD_PARTY_NOTICES.html"
    if not source.endswith(".md"):
        raise ValueError("Document format is unsupported.")
    if source.startswith("docs/"):
        relative = source[5:]
    else:
        relative = "project/" + source
    return "docs/html/" + str(PurePosixPath(relative).with_suffix(".html"))


class StaticHTML(HTMLParser):
    _TAGS = {"html", "head", "body", "title", "meta", "style", "header", "main", "footer", "nav", "section", "article", "aside", "details", "summary", "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "strong", "b", "em", "i", "u", "s", "a", "ol", "ul", "li", "pre", "code", "blockquote", "table", "thead", "tbody", "tr", "th", "td", "caption", "br", "hr", "dl", "dt", "dd", "sup", "sub"}
    _ATTRS = {"class", "id", "style", "title", "lang", "role", "aria-label", "href", "name", "content", "http-equiv", "charset", "colspan", "rowspan", "open"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.meta: dict[str, str] = {}
        self.has_html = False
        self.csp = False

    def handle_starttag(self, tag, attrs):
        if tag not in self._TAGS or any(name not in self._ATTRS for name, _ in attrs) or len({name for name, _ in attrs}) != len(attrs):
            raise ValueError("Document contains unsupported or ambiguous markup.")
        values = dict(attrs)
        if (tag == "style" or "style" in values) and not self.csp:
            raise ValueError("Document styles require the offline security policy first.")
        if tag in {"script", "iframe", "object", "embed", "base", "img", "link", "form", "input", "video", "audio", "source", "svg", "math"}:
            raise ValueError("Document contains active or external resources.")
        if any(name.lower().startswith("on") or name.lower() in {"src", "srcset", "action", "background"} for name, _ in attrs):
            raise ValueError("Document contains active resources.")
        if tag == "html":
            self.has_html = True
        if tag == "meta":
            if values.get("http-equiv", "").casefold() not in {"", "content-security-policy"}:
                raise ValueError("Document contains active metadata.")
            if values.get("http-equiv", "").casefold() == "content-security-policy":
                if self.csp or values.get("content") != CONTENT_SECURITY_POLICY:
                    raise ValueError("Document security policy is invalid.")
                self.csp = True
            if values.get("name"):
                self.meta[values["name"]] = values.get("content", "")
        if values.get("id"):
            if values["id"] in self.ids:
                raise ValueError("Document has duplicate section identities.")
            self.ids.add(values["id"])
        if values.get("href"):
            target = values["href"]
            parsed = urlsplit(target)
            if parsed.scheme.lower() not in {"", "https", "http", "mailto", "tel"} or not parsed.scheme and (parsed.netloc or parsed.path.startswith("/") or "\\" in target):
                raise ValueError("Document link is unsafe.")
            self.links.append(target)


def inspect_html(text: str) -> StaticHTML:
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("Document is missing or too large.")
    if re.search(r"(?:url\s*\(|@import|expression\s*\(|-moz-binding)", text, re.I):
        # CSS is supplied by the renderer and never needs resource URLs.
        # Restrict this check to actual style elements/attributes, not prose.
        styles = re.findall(r"<style[^>]*>(.*?)</style>|\bstyle=[\"'](.*?)[\"']", text, re.I | re.S)
        if any(re.search(r"url\s*\(|@import|expression\s*\(|-moz-binding|\\", a + b, re.I) for a, b in styles):
            raise ValueError("Document style contains an external resource.")
    parser = StaticHTML()
    parser.feed(text)
    parser.close()
    if not parser.has_html:
        raise ValueError("Document is not HTML.")
    return parser


def read_html_file(path: Path) -> str:
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400 or before.st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Help resource is unavailable.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Help resource changed while reading.")
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_DOCUMENT_BYTES or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Help resource is unavailable or changed.")
    text = data.decode("utf-8-sig")
    inspect_html(text)
    return text
