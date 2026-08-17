"""Client customizations for uxxsw.com."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.protocols import DownloadUI, ExportUI
from novel_downloader.schemas import BookConfig, ExporterConfig


@registrar.register_client()
class UxxswClient(CommonClient):
    """Keep downloaded chapter titles in sync with the catalog metadata."""

    async def download_book(
        self,
        book: BookConfig,
        *,
        ui: DownloadUI | None = None,
        **kwargs: Any,
    ) -> None:
        await super().download_book(book, ui=ui, **kwargs)
        self._sync_downloaded_titles(book.book_id)

    async def download_chapter(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> None:
        await super().download_chapter(book_id, chapter_id, **kwargs)
        self._sync_downloaded_titles(book_id)

    def export_book(
        self,
        book: BookConfig,
        cfg: ExporterConfig | None = None,
        *,
        formats: list[str] | None = None,
        stage: str | None = None,
        ui: ExportUI | None = None,
        **kwargs: Any,
    ) -> dict[str, list[Path]]:
        # Also repair metadata for books downloaded before this client existed.
        self._sync_downloaded_titles(book.book_id)
        return super().export_book(
            book,
            cfg=cfg,
            formats=formats,
            stage=stage,
            ui=ui,
            **kwargs,
        )

    def _sync_downloaded_titles(self, book_id: str) -> None:
        try:
            book_info = self._load_book_info(book_id)
        except (FileNotFoundError, ValueError):
            return

        chapter_ids = [
            chapter.get("chapterId")
            for volume in book_info.get("volumes", [])
            for chapter in volume.get("chapters", [])
            if chapter.get("chapterId")
        ]
        if not chapter_ids:
            return

        raw_base = self._raw_data_dir / book_id
        db_path = raw_base / "chapter.raw.sqlite"
        if not db_path.is_file():
            return

        with ChapterStorage(raw_base, filename="chapter.raw.sqlite") as storage:
            stored = storage.get_chapters(chapter_ids)

        changed = False
        for volume in book_info.get("volumes", []):
            for chapter in volume.get("chapters", []):
                chapter_id = chapter.get("chapterId")
                downloaded = stored.get(chapter_id) if chapter_id else None
                title = (downloaded.get("title") or "").strip() if downloaded else ""
                if title and chapter.get("title") != title:
                    chapter["title"] = title
                    changed = True

        if changed:
            self._save_book_info(book_id, book_info)
