"""Parser for the current public pages of book.shencou.com."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)


@registrar.register_parser()
class ShencouParser(BaseParser):
    """Parse book.shencou.com metadata, catalog, and chapter paragraphs."""

    site_name = "shencou"
    BASE_URL = "https://book.shencou.com"
    _CHAPTER_PATH_RE = re.compile(r"^/novel/(\d+)/(\d+)\.html$")

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if not raw_pages:
            return None

        tree = html.fromstring(raw_pages[0])
        book_name = self._first_str(tree.xpath('//h1[contains(@class,"book-title")]/text()'))
        author = self._first_str(
            tree.xpath('//span[contains(@class,"authorname")]/a/text()')
        )
        if not book_name or not author:
            return None

        cover_path = self._first_str(
            tree.xpath('//img[starts-with(@src,"/images/covers/")]/@src')
        )
        cover_url = urljoin(self.BASE_URL + "/", cover_path) if cover_path else ""

        update_time = self._first_str(
            tree.xpath('//span[contains(@class,"time")]/text()'),
            replaces=[("更新时间：", "")],
        )
        serial_status = self._first_str(
            tree.xpath(
                '//div[contains(@class,"zh-tags")]'
                '/span[contains(@class,"tag-blue")]/text()'
            )
        )

        word_count = ""
        for item in tree.xpath('//div[contains(@class,"stat-item")]'):
            unit = self._norm_space(" ".join(item.xpath('.//span[contains(@class,"stat-unit")]//text()')))
            if unit != "万字数":
                continue
            number = self._norm_space(" ".join(item.xpath('.//span[contains(@class,"stat-num")]//text()')))
            if number:
                word_count = f"{number}万字"
            break

        summary_nodes = tree.xpath('//*[@id="bookDescription"]/text()')
        summary = "\n".join(
            line
            for node in summary_nodes
            for line in (self._norm_space(str(node)),)
            if line
        )

        chapters: list[ChapterInfoDict] = []
        seen_ids: set[str] = set()
        anchors = tree.xpath(
            '//div[contains(@class,"chapter-list-container")]//ol/li/a[@href]'
        )
        for anchor in anchors:
            href = (anchor.get("href") or "").strip()
            path = urlparse(urljoin(self.BASE_URL + "/", href)).path
            match = self._CHAPTER_PATH_RE.fullmatch(path)
            if not match or match.group(2) in seen_ids:
                continue

            chapter_id = match.group(2)
            title = self._norm_space(" ".join(anchor.xpath("./span[2]//text()")))
            if not title:
                title = self._norm_space(anchor.text_content())
                title = re.sub(r"^#\d+\s*[：:]\s*", "", title)
            if not title:
                raise ValueError(f"Empty shencou chapter title: {path}")

            seen_ids.add(chapter_id)
            chapters.append(
                {
                    "title": title,
                    "url": path,
                    "chapterId": chapter_id,
                }
            )

        if not chapters:
            return None

        volume: VolumeInfoDict = {
            "volume_name": "正文",
            "chapters": chapters,
        }
        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
            "summary": summary,
            "volumes": [volume],
            "word_count": word_count,
            "serial_status": serial_status,
            "extra": {
                "site": self.site_name,
                "catalog_chapters": len(chapters),
            },
        }

    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        if not raw_pages:
            return None

        tree = html.fromstring(raw_pages[0])
        title = self._first_str(tree.xpath('//*[@id="chapterTitle"]//text()'))
        blocks = tree.xpath('//*[@id="TextContent"]')
        if not title or not blocks:
            return None

        paragraphs = [
            self._norm_space(" ".join(node.itertext()))
            for node in blocks[0].xpath("./p")
        ]
        paragraphs = [paragraph for paragraph in paragraphs if paragraph]
        if paragraphs and paragraphs[0] == title:
            paragraphs.pop(0)
        if not paragraphs:
            return None

        return {
            "id": str(chapter_id),
            "title": title,
            "content": "\n".join(paragraphs),
            "extra": {"site": self.site_name},
        }
