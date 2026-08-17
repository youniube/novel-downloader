"""Fetcher for the public pages of uxxsw.com."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from lxml import html

from novel_downloader.plugins.base.fetcher import BaseFetcher
from novel_downloader.plugins.registry import registrar

logger = logging.getLogger(__name__)


@registrar.register_fetcher()
class UxxswFetcher(BaseFetcher):
    """Fetch the catalog from chapter URL patterns and page navigation."""

    site_name = "uxxsw"

    BASE_URL = "https://www.uxxsw.com"
    CATALOG_URL = BASE_URL + "/chapter/{book_id}.html"
    CHAPTER_URL = BASE_URL + "/book/{book_id}-{chapter_id}.html"

    _ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
    _CHAPTER_PAGE_RE_TEMPLATE = r"^/book/{book_id}-{chapter_id}-\d+\.html$"
    _CHAPTER_HREF_TEMPLATE = r"^/book/{book_id}-(\d+)\.html$"
    _CATALOG_RANGE_RE = re.compile(r"(?P<start>\d+)\s*-\s*(?P<end>\d+)\s*章")
    _UNREADABLE_PAGE_MARKERS = (
        "读取当前章节内容失败",
        "点击下方报告错误提交",
    )

    MAX_CHAPTER_PAGES = 50
    MAX_CATALOG_CHAPTERS = 10000

    async def fetch_book_info(
        self,
        book_id: str,
        **kwargs: Any,
    ) -> list[str]:
        """Return the catalog HTML plus synthetic rows from the URL pattern.

        The site's catalog pagination is an AJAX endpoint that currently returns
        HTTP 403 even when called from the site's own public catalog page. The
        first catalog page exposes a chapter-range upper bound, not necessarily
        the real last chapter. We probe that bound downward one URL at a time,
        confirm the terminal chapter's real navigation, and keep正文 fetches strict.
        """
        self._validate_id(book_id, "book_id")

        catalog_url = self.CATALOG_URL.format(book_id=book_id)
        first_page = await self.fetch(
            catalog_url,
            headers={"Referer": self.BASE_URL + "/"},
            **kwargs,
        )

        first_rows = self._extract_catalog_rows(first_page, book_id)
        if not first_rows:
            raise ValueError("Unable to extract the first uxxsw catalog page")

        first_last_id = max(int(row["chapterorder"]) for row in first_rows)
        advertised_last_id = self._catalog_last_chapter_id(first_page)
        if advertised_last_id is None:
            raise ValueError(
                "uxxsw catalog did not expose a chapter range; "
                "refusing to guess the book boundary"
            )
        if advertised_last_id < first_last_id:
            raise ValueError(
                "uxxsw catalog range ends before its first page: "
                f"first={first_last_id}, last={advertised_last_id}"
            )
        if advertised_last_id > self.MAX_CATALOG_CHAPTERS:
            raise ValueError(
                f"uxxsw catalog has too many chapters: {advertised_last_id} "
                f"(limit={self.MAX_CATALOG_CHAPTERS})"
            )

        # The selector's last range is only an upper bound. It may be a
        # standard bucket such as 1301-1400 even when chapter 1400 does not
        # exist. Probe candidates one by one from that upper bound downward,
        # and accept only a readable chapter whose real navigation says that
        # there is no next chapter.
        await self._sleep()
        actual_last_id, boundary_probe_count = await self._find_last_chapter(
            book_id,
            first_last_id=first_last_id,
            advertised_last_id=advertised_last_id,
            referer=catalog_url,
            **kwargs,
        )
        logger.info(
            "uxxsw chapter boundary resolved: advertised=%d, actual=%d, probes=%d",
            advertised_last_id,
            actual_last_id,
            boundary_probe_count,
        )

        # Follow the real pagination of the last chapter on the first catalog
        # page. This proves the site's next-chapter link before synthesizing
        # entries for the later chapters.
        if first_last_id < actual_last_id:
            await self._sleep()
            first_last_page = await self.fetch(
                self.CHAPTER_URL.format(
                    book_id=book_id,
                    chapter_id=first_last_id,
                ),
                headers={"Referer": catalog_url},
                **kwargs,
            )
            _, terminal_kind, terminal_href = await self._walk_chapter_pages(
                book_id,
                str(first_last_id),
                first_page=first_last_page,
                **kwargs,
            )
            if terminal_kind != "下一章" or not terminal_href:
                raise ValueError(
                    "uxxsw chapter URL chain did not expose a next chapter "
                    f"after chapter {first_last_id}"
                )
            expected_next = f"/book/{book_id}-{first_last_id + 1}.html"
            if urlparse(urljoin(self.BASE_URL + "/", terminal_href)).path != expected_next:
                raise ValueError(
                    "uxxsw next-chapter URL pattern changed: "
                    f"expected={expected_next}, got={terminal_href}"
                )

        virtual_rows = [
            {
                "chapterorder": str(chapter_id),
                # The exact title is parsed from the正文 when the chapter is
                # downloaded. This placeholder is only for the pre-download
                # catalog plan.
                "chaptername": f"章节ID {chapter_id}",
                "chapterurl": f"/book/{book_id}-{chapter_id}.html",
            }
            for chapter_id in range(first_last_id + 1, actual_last_id + 1)
        ]

        if not virtual_rows:
            return [first_page]
        return [
            first_page,
            json.dumps(
                {"code": 0, "data": virtual_rows},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ]

    async def _find_last_chapter(
        self,
        book_id: str,
        *,
        first_last_id: int,
        advertised_last_id: int,
        referer: str,
        **kwargs: Any,
    ) -> tuple[int, int]:
        """Find the real last chapter below the catalog's advertised bound."""
        probe_count = 0
        for chapter_id in range(advertised_last_id, first_last_id - 1, -1):
            chapter_url = self.CHAPTER_URL.format(
                book_id=book_id,
                chapter_id=chapter_id,
            )
            raw_page = await self.fetch(
                chapter_url,
                headers={"Referer": referer},
                **kwargs,
            )
            probe_count += 1

            try:
                self._validate_chapter_page(raw_page, chapter_url)
            except ValueError:
                # The site uses a normal HTTP 200 page for nonexistent or
                # unreadable chapters. Continue with the previous URL instead
                # of treating that page as book content.
                await self._sleep()
                continue

            _, terminal_kind, _ = await self._walk_chapter_pages(
                book_id,
                str(chapter_id),
                first_page=raw_page,
                **kwargs,
            )
            if terminal_kind == "没有了":
                return chapter_id, probe_count

            # A readable candidate that still points to another chapter means
            # the range has a gap or the navigation contract changed. Do not
            # silently truncate the book at this candidate.
            raise ValueError(
                "uxxsw readable boundary candidate does not terminate the book: "
                f"chapter={chapter_id}, navigation={terminal_kind}"
            )

        raise ValueError(
            "uxxsw could not find a readable terminal chapter between "
            f"{first_last_id} and {advertised_last_id}"
        )

    async def fetch_chapter_content(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> list[str]:
        """Follow this chapter's real 下一页 links until 下一章/没有了."""
        self._validate_id(book_id, "book_id")
        self._validate_id(chapter_id, "chapter_id")
        pages, _, _ = await self._walk_chapter_pages(
            book_id,
            chapter_id,
            **kwargs,
        )
        return pages

    async def _walk_chapter_pages(
        self,
        book_id: str,
        chapter_id: str,
        *,
        first_page: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[str], str, str]:
        current_url = self.CHAPTER_URL.format(
            book_id=book_id,
            chapter_id=chapter_id,
        )
        expected_path = re.compile(
            self._CHAPTER_PAGE_RE_TEMPLATE.format(
                book_id=re.escape(book_id),
                chapter_id=re.escape(chapter_id),
            )
        )

        pages: list[str] = []
        seen_urls: set[str] = set()
        for page_index in range(self.MAX_CHAPTER_PAGES):
            absolute_url = urljoin(self.BASE_URL + "/", current_url)
            if absolute_url in seen_urls:
                raise ValueError(f"uxxsw chapter pagination loop: {absolute_url}")
            seen_urls.add(absolute_url)

            if page_index == 0 and first_page is not None:
                raw_page = first_page
            else:
                raw_page = await self.fetch(
                    absolute_url,
                    headers={"Referer": self.BASE_URL + "/"},
                    **kwargs,
                )
            self._validate_chapter_page(raw_page, absolute_url)
            pages.append(raw_page)

            next_kind, next_href = self._next_navigation(raw_page)
            if next_kind in {"下一章", "没有了"}:
                return pages, next_kind, next_href
            if next_kind is None:
                raise ValueError(
                    "Missing uxxsw chapter navigation; refusing to return "
                    f"possibly truncated content: {absolute_url}"
                )
            if next_kind != "下一页":
                raise ValueError(f"Unknown uxxsw chapter navigation: {next_kind}")

            next_url = urljoin(absolute_url, next_href)
            parsed = urlparse(next_url)
            if parsed.netloc and parsed.netloc.lower() != urlparse(self.BASE_URL).netloc:
                raise ValueError(f"uxxsw navigation leaves site: {next_url}")
            if not expected_path.fullmatch(parsed.path):
                raise ValueError(
                    "uxxsw next-page link does not belong to the current chapter: "
                    f"{next_url}"
                )

            current_url = parsed.path
            await self._sleep()

        raise ValueError(
            f"uxxsw chapter has more than {self.MAX_CHAPTER_PAGES} pages: "
            f"book={book_id}, chapter={chapter_id}"
        )

    @classmethod
    def _extract_catalog_rows(
        cls,
        raw_html: str,
        book_id: str,
    ) -> list[dict[str, str]]:
        tree = html.fromstring(raw_html)
        pattern = re.compile(cls._CHAPTER_HREF_TEMPLATE.format(book_id=re.escape(book_id)))
        rows: list[dict[str, str]] = []
        anchors = tree.xpath(
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' chapListBody ')]//a"
        )
        for anchor in anchors:
            path = urlparse(
                urljoin(cls.BASE_URL + "/", (anchor.get("href") or "").strip())
            ).path
            match = pattern.fullmatch(path)
            if not match:
                continue
            title = " ".join(anchor.text_content().split())
            if not title:
                raise ValueError(f"Empty uxxsw chapter title: {path}")
            rows.append(
                {
                    "chapterorder": match.group(1),
                    "chaptername": title,
                    "chapterurl": path,
                }
            )
        return rows

    @classmethod
    def _catalog_last_chapter_id(cls, raw_html: str) -> int | None:
        tree = html.fromstring(raw_html)
        ends: list[int] = []
        for label in tree.xpath("//select//option/text()"):
            match = cls._CATALOG_RANGE_RE.search(" ".join(str(label).split()))
            if match:
                ends.append(int(match.group("end")))
        return max(ends, default=None)

    @classmethod
    def _next_navigation(cls, raw_html: str) -> tuple[str | None, str]:
        tree = html.fromstring(raw_html)
        terminal = False
        xpath = (
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' btnW ')]"
            "//*[self::a or self::span]"
        )
        for node in tree.xpath(xpath):
            label = " ".join(node.text_content().split())
            href = (node.get("href") or "").strip()
            if label == "下一页" and href and not href.startswith("javascript:"):
                return "下一页", href
            if label == "下一章" and href and not href.startswith("javascript:"):
                return "下一章", href
            if label == "没有了":
                terminal = True
        return ("没有了", "") if terminal else (None, "")

    @classmethod
    def _validate_chapter_page(cls, raw_html: str, url: str) -> None:
        tree = html.fromstring(raw_html)
        body_text = " ".join(tree.xpath("//body//text()"))
        if any(marker in body_text for marker in cls._UNREADABLE_PAGE_MARKERS):
            raise ValueError(f"Unreadable uxxsw chapter page: {url}")
        title = " ".join(
            tree.xpath(
                "//h1[contains(concat(' ', normalize-space(@class), ' '), ' title ')]//text()"
            )
        ).strip()
        content_blocks = tree.xpath(
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' content ')]"
        )
        has_paragraph = any(block.xpath(".//p") for block in content_blocks)
        if not title or not has_paragraph:
            raise ValueError(f"Invalid uxxsw chapter page: {url}")

    @classmethod
    def _validate_id(cls, value: str, name: str) -> None:
        if not value or not cls._ID_RE.fullmatch(str(value)):
            raise ValueError(f"Invalid uxxsw {name}: {value!r}")
