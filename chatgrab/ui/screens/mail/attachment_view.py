"""П3: one viewer dialog, one widget panel per file type chosen by
extension — "единая панель просмотра с подстановкой движка по типу
файла" from PLAN.md. Adding a format later means adding one branch in
_build_panel(), nothing else in this screen changes.

Text extraction for search happens here, on first open, rather than as a
separate background job — viewing a docx/xlsx/PDF already has to read
its content to display it, so persisting that same text costs one extra
db call and needs no pipeline of its own. PDF is the one type handled
inline in this file (via QPdfDocument.getAllText(), a Qt call that has
to run on the GUI thread); docx/xlsx go through core/mail_attachment_text
off the GUI thread via run_blocking, same as everything else in
MailService.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QPointF, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QPixmap, QTransform
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QPlainTextEdit, QScrollArea, QTabWidget, QTableWidget,
    QTableWidgetItem, QTreeView, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import human_size
from ...util import fire, run_blocking
from ...widgets import button, muted
from ....core import mail_attachment_text

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
_LEGACY_EXTENSIONS = {".doc", ".xls"}
# getAllText() runs on the GUI thread (see class docstring) — a PDF with
# an absurd page count is the same file-bomb shape as an oversized zip,
# just via page count instead of bytes, so text extraction (not viewing)
# stops early past this many pages rather than freezing the UI.
_MAX_PDF_TEXT_PAGES = 500


def _save_attachment_as(parent: QWidget, path: str, filename: str) -> None:
    """Shared by the viewer's own "Сохранить как…" and MessagePane's
    "Сохранить все" (mail/__init__.py) — one copy, not two, of the same
    three lines."""
    dest, _ = QFileDialog.getSaveFileName(parent, "Сохранить вложение", filename or Path(path).name)
    if dest:
        shutil.copy2(path, dest)


class AttachmentViewerDialog(QDialog):
    def __init__(self, ctx: AppContext, attachment, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.attachment = attachment
        self.setWindowTitle(attachment["filename"] or "Вложение")
        self.resize(880, 660)

        outer = QVBoxLayout(self)

        info = QHBoxLayout()
        info.addWidget(muted(attachment["content_type"] or ""))
        size_text = human_size(attachment["size_bytes"])
        if size_text:
            info.addWidget(muted(size_text))
        info.addStretch(1)
        outer.addLayout(info)

        path = attachment["path"]
        ext = Path(attachment["filename"] or "").suffix.lower()
        if not path or not Path(path).exists():
            outer.addWidget(muted("Файл не найден на диске."))
        elif ext == ".pdf":
            self._build_pdf(outer, path)
        elif ext == ".xlsx":
            self._build_xlsx(outer, path)
        elif ext == ".docx":
            self._build_docx(outer, path)
        elif ext in _IMAGE_EXTENSIONS:
            self._build_image(outer, path)
        else:
            self._build_fallback(outer, path, ext)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        if path and Path(path).exists():
            save_btn = button("Сохранить как…", "ghost")
            save_btn.clicked.connect(lambda: _save_attachment_as(self, path, attachment["filename"]))
            buttons.addWidget(save_btn)
            open_btn = button("Открыть во внешнем приложении", "ghost")
            open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
            buttons.addWidget(open_btn)
        close_btn = button("Закрыть", "secondary")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        outer.addLayout(buttons)

    # ---- text already extracted for search — persisted once per attachment
    def _remember_text(self, text: str | None) -> None:
        if text and self.attachment["extracted_text"] is None:
            self.ctx.db.set_attachment_extracted_text(self.attachment["id"], text)

    # ---- PDF: QPdfView + bookmarks + in-document search -----------------
    def _build_pdf(self, lay: QVBoxLayout, path: str) -> None:
        from PySide6.QtPdf import QPdfBookmarkModel, QPdfDocument, QPdfSearchModel
        from PySide6.QtPdfWidgets import QPdfView

        doc = QPdfDocument(self)
        error = doc.load(path)
        while error == QPdfDocument.Error.IncorrectPassword:
            pw, ok = QInputDialog.getText(
                self, "Пароль", f"«{self.attachment['filename']}» защищён паролем:",
                QLineEdit.Password)
            if not ok:
                lay.addWidget(muted("Открытие отменено — пароль не введён."))
                return
            doc.setPassword(pw)
            error = doc.load(path)
        if error != QPdfDocument.Error.None_:
            lay.addWidget(muted(f"Не удалось открыть PDF ({error.name})."))
            return

        row = QHBoxLayout()
        bookmark_model = QPdfBookmarkModel(self)
        bookmark_model.setDocument(doc)
        if bookmark_model.rowCount() > 0:
            tree = QTreeView()
            tree.setModel(bookmark_model)
            tree.setHeaderHidden(True)
            tree.setMaximumWidth(220)

            def _jump(index):
                page = index.data(QPdfBookmarkModel.Role.Page)
                if page is not None:
                    view.pageNavigator().jump(int(page), QPointF())

            tree.clicked.connect(_jump)
            row.addWidget(tree)

        col = QVBoxLayout()
        top_row = QHBoxLayout()
        search_input = QLineEdit()
        search_input.setPlaceholderText("Найти в документе…")
        search_model = QPdfSearchModel(self)
        search_model.setDocument(doc)
        search_input.textChanged.connect(search_model.setSearchString)
        top_row.addWidget(search_input, 1)

        view = QPdfView(self)
        view.setDocument(doc)
        view.setPageMode(QPdfView.PageMode.MultiPage)
        view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        view.setSearchModel(search_model)

        def _zoom(factor: float) -> None:
            view.setZoomMode(QPdfView.ZoomMode.Custom)
            view.setZoomFactor(max(0.2, min(8.0, view.zoomFactor() * factor)))

        zoom_out = button("-", "ghost")
        zoom_out.clicked.connect(lambda: _zoom(0.8))
        zoom_in = button("+", "ghost")
        zoom_in.clicked.connect(lambda: _zoom(1.25))
        top_row.addWidget(zoom_out)
        top_row.addWidget(zoom_in)
        col.addLayout(top_row)
        col.addWidget(view, 1)

        col_widget = QWidget()
        col_widget.setLayout(col)
        row.addWidget(col_widget, 1)
        lay.addLayout(row, 1)

        if self.attachment["extracted_text"] is None:
            page_count = min(doc.pageCount(), _MAX_PDF_TEXT_PAGES)
            text = "\n".join(doc.getAllText(i).text() for i in range(page_count)).strip()
            self._remember_text(text or None)

    # ---- Excel: tabs, one QTableWidget per sheet -------------------------
    def _build_xlsx(self, lay: QVBoxLayout, path: str) -> None:
        placeholder = muted("Читаю книгу…")
        lay.addWidget(placeholder, 1)

        async def _run():
            return await run_blocking(mail_attachment_text.read_xlsx_grid, path)

        def on_error(e):
            placeholder.setText(f"Не удалось открыть таблицу: {e}")

        task = fire(_run(), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            sheets = t.result()
            placeholder.setParent(None)
            tabs = QTabWidget()
            text_parts = []
            for name, rows in sheets:
                table = QTableWidget()
                if rows:
                    table.setColumnCount(len(rows[0]))
                    table.setRowCount(len(rows) - 1)
                    table.setHorizontalHeaderLabels(rows[0])
                    for r, values in enumerate(rows[1:]):
                        for c, value in enumerate(values):
                            table.setItem(r, c, QTableWidgetItem(value))
                    text_parts.append(" ".join(v for row in rows for v in row if v))
                table.setEditTriggers(QTableWidget.NoEditTriggers)
                tabs.addTab(table, name)
            lay.addWidget(tabs, 1)
            self._remember_text("\n".join(text_parts) or None)

        task.add_done_callback(_apply)

    # ---- Word: paragraphs + table cells as plain text --------------------
    def _build_docx(self, lay: QVBoxLayout, path: str) -> None:
        placeholder = muted("Читаю документ…")
        lay.addWidget(placeholder, 1)

        async def _run():
            return await run_blocking(mail_attachment_text.extract_docx_text, path)

        def on_error(e):
            placeholder.setText(f"Не удалось открыть документ: {e}")

        task = fire(_run(), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            text = t.result()
            placeholder.setParent(None)
            view = QPlainTextEdit(text)
            view.setReadOnly(True)
            lay.addWidget(view, 1)
            self._remember_text(text or None)

        task.add_done_callback(_apply)

    # ---- photo: zoom + rotate, EXIF date if present -----------------------
    def _build_image(self, lay: QVBoxLayout, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            lay.addWidget(muted("Не удалось прочитать изображение."))
            return

        self._base_pixmap = pixmap
        self._rotation = 0
        self._zoom = 1.0

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignCenter)
        scroll.setWidget(self._image_label)
        lay.addWidget(scroll, 1)

        controls = QHBoxLayout()
        rotate_btn = button("Повернуть", "ghost")
        rotate_btn.clicked.connect(self._rotate_image)
        controls.addWidget(rotate_btn)
        zoom_in = button("+", "ghost")
        zoom_in.clicked.connect(lambda: self._zoom_image(1.25))
        controls.addWidget(zoom_in)
        zoom_out = button("-", "ghost")
        zoom_out.clicked.connect(lambda: self._zoom_image(0.8))
        controls.addWidget(zoom_out)
        taken = _exif_date_taken(path)
        if taken:
            controls.addWidget(muted(f"Снято: {taken}"))
        controls.addStretch(1)
        lay.addLayout(controls)

        self._render_image()

    def _rotate_image(self) -> None:
        self._rotation = (self._rotation + 90) % 360
        self._render_image()

    def _zoom_image(self, factor: float) -> None:
        self._zoom = max(0.1, min(8.0, self._zoom * factor))
        self._render_image()

    def _render_image(self) -> None:
        transform = QTransform().rotate(self._rotation)
        pixmap = self._base_pixmap.transformed(transform, Qt.SmoothTransformation)
        size = pixmap.size() * self._zoom
        self._image_label.setPixmap(
            pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ---- unknown/legacy (.doc, .xls, …): properties + external open only --
    def _build_fallback(self, lay: QVBoxLayout, path: str, ext: str) -> None:
        note = (
            f"Формат «{ext}» не открывается внутри приложения — "
            f"по решению не тянуть отдельную зависимость ради устаревших форматов "
            f"({ext}). Откройте во внешнем приложении."
            if ext in _LEGACY_EXTENSIONS else
            f"Формат «{ext or '?'}» не распознан для предпросмотра."
        )
        label = muted(note)
        label.setWordWrap(True)
        lay.addWidget(label, 1)


# ---- minimal EXIF date-taken, stdlib only, JPEG only -----------------------
def _exif_date_taken(path: str) -> str | None:
    """Just enough of the EXIF/TIFF structure to read tag 0x9003
    (DateTimeOriginal) out of a JPEG's APP1 segment — not a general EXIF
    reader. Anything else (a non-JPEG image, no EXIF block, an
    unrecognized structure) quietly returns None; a missing shooting
    date isn't worth a dependency."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9):
            pos += 2
            continue
        seg_len = int.from_bytes(data[pos + 2:pos + 4], "big")
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00":
            return _parse_exif_date(data[pos + 10:pos + 2 + seg_len])
        if marker == 0xDA:  # start of scan — no more APPn segments follow
            break
        pos += 2 + seg_len
    return None


def _parse_exif_date(tiff: bytes) -> str | None:
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if tiff[:2] == b"II" else ">"
    import struct

    (ifd0_offset,) = struct.unpack_from(endian + "I", tiff, 4)
    seen: set[int] = set()
    offset = ifd0_offset
    exif_ifd_offset = None
    while offset and offset not in seen and offset + 2 <= len(tiff):
        seen.add(offset)
        (count,) = struct.unpack_from(endian + "H", tiff, offset)
        for i in range(count):
            entry = offset + 2 + i * 12
            if entry + 12 > len(tiff):
                break
            tag, = struct.unpack_from(endian + "H", tiff, entry)
            if tag == 0x8769:  # Exif IFD pointer
                exif_ifd_offset, = struct.unpack_from(endian + "I", tiff, entry + 8)
        next_offset_pos = offset + 2 + count * 12
        if next_offset_pos + 4 > len(tiff):
            break
        (offset,) = struct.unpack_from(endian + "I", tiff, next_offset_pos)

    for ifd_offset in (exif_ifd_offset, ifd0_offset):
        if not ifd_offset or ifd_offset + 2 > len(tiff):
            continue
        (count,) = struct.unpack_from(endian + "H", tiff, ifd_offset)
        for i in range(count):
            entry = ifd_offset + 2 + i * 12
            if entry + 12 > len(tiff):
                break
            tag, = struct.unpack_from(endian + "H", tiff, entry)
            if tag in (0x9003, 0x0132):  # DateTimeOriginal, else plain DateTime
                value_offset, = struct.unpack_from(endian + "I", tiff, entry + 8)
                raw = tiff[value_offset:value_offset + 19]
                text = raw.split(b"\x00")[0].decode("ascii", errors="replace")
                if len(text) == 19 and text[4] == ":":
                    return text[:10].replace(":", "-") + text[10:]
    return None
