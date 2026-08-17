"""Fetcher for the current public pages of book.shencou.com."""

from __future__ import annotations

import re
from typing import Any

from novel_downloader.plugins.base.fetcher import BaseFetcher
from novel_downloader.plugins.registry import registrar


@registrar.register_fetcher()
class ShencouFetcher(BaseFetcher):
    """Fetch book metadata, catalog, and public chapter pages."""

    site_name = "shencou"

    BASE_URL = "https://book.shencou.com"
    BOOK_URL = BASE_URL + "/novel/{book_id}.html"
    CHAPTER_URL = BASE_URL + "/novel/{book_id}/{chapter_id}.html"

    _ID_RE = re.compile(r"^\d+$")

    async def fetch_book_info(
        self,
        book_id: str,
        **kwargs: Any,
    ) -> list[str]:
        self._validate_id(book_id, "book_id")
        url = self.BOOK_URL.format(book_id=book_id)
        page = await self.fetch(
            url,
            headers={"Referer": self.BASE_URL + "/"},
            **kwargs,
        )
        return [page]

    async def fetch_chapter_content(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> list[str]:
        self._validate_id(book_id, "book_id")
        self._validate_id(chapter_id, "chapter_id")
        url = self.CHAPTER_URL.format(
            book_id=book_id,
            chapter_id=chapter_id,
        )
        page = await self.fetch(
            url,
            headers={
                "Referer": self.BOOK_URL.format(book_id=book_id),
            },
            **kwargs,
        )
        return [page]

    @classmethod
    def _validate_id(cls, value: str, name: str) -> None:
        if not value or not cls._ID_RE.fullmatch(str(value)):
            raise ValueError(f"Invalid shencou {name}: {value!r}")
