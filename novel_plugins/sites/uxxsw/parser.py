"""Parser for the public pages of 悠闲小说网 (uxxsw.com)."""

from __future__ import annotations

import json
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
class UxxswParser(BaseParser):
    """Parse directory HTML/JSON and paginated chapter HTML."""

    site_name = "uxxsw"
    BASE_URL = "https://www.uxxsw.com"
    ADS = {
        "本书由书山中文网",
        "版权所有",
        "侵权必究",
        "作者势无双亲推",
    }

    _CHAPTER_HREF_TEMPLATE = r"^/book/{book_id}-(\d+)\.html$"
    _INLINE_AD_RES = (
        re.compile(
            r"作者势无双亲推：希望您在悠闲小说网享受《[^》]+》的故事。?"
        ),
        re.compile(
            r"在[“\"]人人书库[”\"]APP上可阅读《[^》]+》.*?APP官网"
        ),
    )
    _CATALOG_TITLE_RE = re.compile(r"^(?P<book>.+?)章节目录_(?P<author>[^_]+)(?:_|$)")

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if not raw_pages:
            return None

        first_tree = html.fromstring(raw_pages[0])
        book_name, author = self._book_metadata(first_tree)
        chapters_by_id: dict[str, ChapterInfoDict] = {}

        book_id = self._book_id_from_page(raw_pages[0])
        for chapter in self._extract_html_chapters(first_tree, book_id):
            self._add_chapter(chapters_by_id, chapter)

        for raw_json in raw_pages[1:]:
            result = json.loads(raw_json)
            if not isinstance(result, dict) or result.get("code") != 0:
                raise ValueError(f"Invalid uxxsw catalog response: {result!r}")
            rows = result.get("data")
            if not isinstance(rows, list):
                raise ValueError("Invalid uxxsw catalog data")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(f"Invalid uxxsw chapter row: {row!r}")
                self._add_chapter(
                    chapters_by_id,
                    self._chapter_for_json_row(row, book_id),
                )

        chapters = [chapters_by_id[key] for key in sorted(chapters_by_id, key=int)]
        if not chapters:
            return None
        self._validate_chapter_sequence(chapters)

        volume: VolumeInfoDict = {
            "volume_name": "正文",
            "chapters": chapters,
        }
        return {
            "book_name": book_name,
            "author": author,
            "cover_url": "",
            "update_time": "",
            "summary": "",
            "volumes": [volume],
            "extra": {
                "site": self.site_name,
                "catalog_pages": len(raw_pages),
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

        title = ""
        paragraphs: list[str] = []
        for page_index, raw_html in enumerate(raw_pages):
            tree = html.fromstring(raw_html)
            page_title = self._chapter_title(tree)
            if not page_title:
                raise ValueError(f"Missing uxxsw chapter title for chapter {chapter_id}")
            if title and page_title != title:
                raise ValueError(
                    f"uxxsw chapter page title changed: {title!r} -> {page_title!r}"
                )
            title = page_title

            content_block = self._content_block(tree)
            if content_block is None:
                raise ValueError(f"Missing uxxsw content for chapter {chapter_id}")

            page_lines = [
                self._clean_line(" ".join(node.text_content().split()))
                for node in content_block.xpath("./p")
            ]
            page_lines = [line for line in page_lines if line and not self._is_ad_line(line)]

            if page_index == 0:
                # Some books repeat the h1 inside the first content paragraph;
                # others keep it only in the title box. Strip it when present,
                # but do not reject a valid page just because the body starts
                # directly with prose.
                if title in page_lines:
                    title_index = page_lines.index(title)
                    page_lines = page_lines[title_index + 1 :]

            paragraphs.extend(page_lines)

        content = "\n".join(paragraphs).strip()
        if not content:
            return None
        return {
            "id": str(chapter_id),
            "title": title,
            "content": content,
            "extra": {
                "site": self.site_name,
                "page_count": len(raw_pages),
            },
        }

    def _book_metadata(self, tree: html.HtmlElement) -> tuple[str, str]:
        page_title = self._first_str(tree.xpath("//title/text()"))
        match = self._CATALOG_TITLE_RE.match(page_title)
        if match:
            return match.group("book").strip(), match.group("author").strip()

        body_text = " ".join(tree.xpath("//body//text()"))
        body_match = re.search(r"【([^】]+)】全部章节", body_text)
        if body_match:
            return body_match.group(1).strip(), ""
        return page_title.strip(), ""

    def _book_id_from_page(self, raw_html: str) -> str:
        tree = html.fromstring(raw_html)
        for href in tree.xpath(
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' chapListBody ')]//a/@href"
        ):
            path = urlparse(urljoin(self.BASE_URL + "/", str(href))).path
            match = re.match(r"^/book/([A-Za-z0-9_-]+)-\d+\.html$", path)
            if match:
                return match.group(1)
        raise ValueError("Unable to identify uxxsw book id from catalog")

    def _extract_html_chapters(
        self,
        tree: html.HtmlElement,
        book_id: str,
    ) -> list[ChapterInfoDict]:
        pattern = re.compile(self._CHAPTER_HREF_TEMPLATE.format(book_id=re.escape(book_id)))
        anchors = tree.xpath(
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' chapListBody ')]//a"
        )
        chapters: list[ChapterInfoDict] = []
        for anchor in anchors:
            path = urlparse(
                urljoin(self.BASE_URL + "/", (anchor.get("href") or "").strip())
            ).path
            match = pattern.fullmatch(path)
            if not match:
                continue
            title = " ".join(anchor.text_content().split())
            if not title:
                raise ValueError(f"Empty uxxsw chapter title: {path}")
            chapters.append(
                {
                    "title": title,
                    "url": path,
                    "chapterId": match.group(1),
                }
            )
        return chapters

    def _chapter_for_json_row(
        self,
        row: dict[str, Any],
        book_id: str,
    ) -> ChapterInfoDict:
        chapter_id = str(row.get("chapterorder", "")).strip()
        title = " ".join(str(row.get("chaptername", "")).split())
        raw_url = str(row.get("chapterurl", "")).strip()
        if not chapter_id.isdigit() or not title or not raw_url:
            raise ValueError(f"Invalid uxxsw chapter row: {row!r}")

        path = urlparse(urljoin(self.BASE_URL + "/", raw_url)).path
        pattern = re.compile(
            self._CHAPTER_HREF_TEMPLATE.format(book_id=re.escape(book_id))
        )
        match = pattern.fullmatch(path)
        if not match or match.group(1) != chapter_id:
            raise ValueError(
                f"Mismatched uxxsw chapter id and URL: id={chapter_id}, url={path}"
            )
        return {
            "title": title,
            "url": path,
            "chapterId": chapter_id,
        }

    @staticmethod
    def _add_chapter(
        chapters_by_id: dict[str, ChapterInfoDict],
        chapter: ChapterInfoDict | None,
    ) -> None:
        if chapter is None:
            return
        chapter_id = chapter["chapterId"]
        previous = chapters_by_id.get(chapter_id)
        if previous is not None and previous != chapter:
            raise ValueError(f"Conflicting uxxsw chapter metadata: {chapter_id}")
        chapters_by_id[chapter_id] = chapter

    @staticmethod
    def _validate_chapter_sequence(chapters: list[ChapterInfoDict]) -> None:
        ids = [int(chapter["chapterId"]) for chapter in chapters]
        expected = list(range(ids[0], ids[-1] + 1))
        if ids != expected:
            raise ValueError(
                "Non-contiguous uxxsw catalog chapter IDs: "
                f"{ids[:3]} ... {ids[-3:]}"
            )

    def _content_block(self, tree: html.HtmlElement) -> html.HtmlElement | None:
        blocks = tree.xpath(
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' content ')]"
        )
        if not blocks:
            return None
        return max(blocks, key=lambda block: len(block.xpath(".//p")))

    def _chapter_title(self, tree: html.HtmlElement) -> str:
        return self._norm_space(
            " ".join(
                tree.xpath(
                    "//h1[contains(concat(' ', normalize-space(@class), ' '), ' title ')]//text()"
                )
            )
        )

    def _clean_line(self, line: str) -> str:
        for pattern in self._INLINE_AD_RES:
            line = pattern.sub("", line)
        return line.strip()
