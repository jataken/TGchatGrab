"""Export engine: Excel (.xlsx) / JSONL / Markdown, merged or per-chat,
split by token budget / month / single file, incremental, with an
optional zip of the photos referenced by the selection."""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db.database import Database, now_iso
from ..paths import Paths

DEFAULT_TOKEN_LIMIT = 180_000
DEFAULT_MD_HEADER = (
    "# Выгрузка сообщений Telegram\n\n"
    "Чаты: {chats}\n"
    "Период: {period}\n"
    "Сообщений: {count}\n\n"
    "Поля: автор и его @ник, дата, текст, вложенное фото (путь рядом с базой, "
    "не встроено в файл), ссылка на исходное сообщение в Telegram.\n"
    "Фото лежат в подпапках photos/<chat_id>/<message_id>.jpg рядом с базой данных "
    "(или в приложенном zip, если он был запрошен при выгрузке).\n\n---\n\n"
)

_SLUG_RE = re.compile(r"[^a-zA-Zа-яА-ЯёЁ0-9]+")


def slugify(text: str, max_len: int = 28) -> str:
    s = _SLUG_RE.sub("_", text.strip()).strip("_").lower()
    return (s or "chat")[:max_len]


def estimate_tokens(text: str) -> int:
    """chars/4 approximation — good enough for gauging file counts, no
    extra tokenizer dependency needed."""
    return max(1, len(text) // 4)


@dataclass
class ExportParams:
    chat_ids: list[int]
    format: str = "xlsx"          # xlsx | jsonl | markdown
    merge: bool = False
    split_mode: str = "tokens"     # tokens | month | none
    token_limit: int = DEFAULT_TOKEN_LIMIT
    date_from: str | None = None
    date_to: str | None = None
    incremental: bool = False
    zip_photos: bool = False
    include_hidden: bool = False
    folder: str = ""
    query: str = ""
    author: str = ""
    photos_only: bool = False
    forwards_only: bool = False
    replies_only: bool = False
    markdown_header: str = ""
    preset_name: str | None = None


@dataclass
class ExportEstimate:
    row_count: int
    token_count: int
    file_count: int
    file_names: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    output_paths: list[str]
    row_count: int
    export_log_id: int


ROW_COLUMNS = [
    "chat_title", "message_id", "date", "edited_date", "sender_display_name",
    "sender_username", "text", "reply_to_message_id", "forwarded_from",
    "media_type", "media_caption", "photo_path", "views", "link",
]


def text_with_markers(r) -> str:
    """Post text with a leading reply/forward marker, matching what a
    human reading the export needs to make sense of it out of context —
    e.g. "_ответ на сообщение 91713_ Тера поставляет..."."""
    text = r["text"] or ""
    marks = []
    if r["is_reply"]:
        marks.append(f"ответ на сообщение {r['reply_to_message_id']}")
    if r["is_forward"]:
        marks.append(f"переслано от {r['forwarded_from']}" if r["forwarded_from"] else "переслано")
    if not marks:
        return text
    return f"_{'; '.join(marks)}_ {text}"


def photo_marker(r) -> str:
    return f"[фото: {r['photo_path']}]" if r["photo_path"] else ""


class ExportService:
    def __init__(self, db: Database, paths: Paths):
        self.db = db
        self.paths = paths

    # ---- selection -----------------------------------------------------
    def _selected_rows(self, params: ExportParams):
        min_id_by_chat = None
        if params.incremental:
            min_id_by_chat = self.db.incremental_baseline(params.chat_ids)
        return self.db.export_select(
            chat_ids=params.chat_ids, date_from=params.date_from, date_to=params.date_to,
            include_hidden=params.include_hidden, query=params.query, author=params.author,
            photos_only=params.photos_only, forwards_only=params.forwards_only,
            replies_only=params.replies_only, min_id_by_chat=min_id_by_chat,
        )

    def _chat_title(self, chat_id: int) -> str:
        chat = self.db.get_chat(chat_id)
        return chat["title"] if chat else f"чат {chat_id}"

    def _group_by_chat(self, rows) -> dict[int, list]:
        groups: dict[int, list] = {}
        for r in rows:
            groups.setdefault(r["chat_id"], []).append(r)
        return groups

    def _base_name(self, params: ExportParams, chat_id: int | None) -> str:
        if params.merge or chat_id is None:
            return "chatgrab_все-чаты"
        return f"chatgrab_{slugify(self._chat_title(chat_id))}"

    def _ext(self, params: ExportParams) -> str:
        return {"xlsx": "xlsx", "jsonl": "jsonl", "markdown": "md"}[params.format]

    # ---- chunking --------------------------------------------------------
    def _chunk_rows(self, rows: list, params: ExportParams) -> list[tuple[str, list]]:
        """Return [(label, rows), ...] where label is used in the filename."""
        if not rows:
            return []
        if params.split_mode == "none":
            return [("", rows)]
        if params.split_mode == "month":
            chunks: dict[str, list] = {}
            for r in rows:
                month = str(r["date"])[:7]
                chunks.setdefault(month, []).append(r)
            return sorted(chunks.items())
        # tokens
        out: list[tuple[str, list]] = []
        current: list = []
        current_tokens = 0
        for r in rows:
            t = estimate_tokens(r["text"] or "")
            if current and current_tokens + t > params.token_limit:
                out.append(current)
                current, current_tokens = [], 0
            current.append(r)
            current_tokens += t
        if current:
            out.append(current)
        total = len(out)
        return [(f"часть-{i + 1}-из-{total}" if total > 1 else "", chunk)
                for i, chunk in enumerate(out)]

    # ---- estimate (no writes) -------------------------------------------
    def estimate(self, params: ExportParams) -> ExportEstimate:
        rows = self._selected_rows(params)
        token_total = sum(estimate_tokens(r["text"] or "") for r in rows)
        names = self._plan_filenames(rows, params)
        return ExportEstimate(row_count=len(rows), token_count=token_total,
                               file_count=len(names), file_names=names)

    def _plan_filenames(self, rows: list, params: ExportParams) -> list[str]:
        names: list[str] = []
        ext = self._ext(params)
        if params.merge:
            for label, _ in self._chunk_rows(rows, params):
                base = self._base_name(params, None)
                names.append(f"{base}{'_' + label if label else ''}.{ext}")
        else:
            for chat_id, chat_rows in self._group_by_chat(rows).items():
                base = self._base_name(params, chat_id)
                for label, _ in self._chunk_rows(chat_rows, params):
                    names.append(f"{base}{'_' + label if label else ''}.{ext}")
        if params.zip_photos and any(r["photo_path"] for r in rows):
            names.append("chatgrab_photos.zip")
        return names

    # ---- writers -----------------------------------------------------
    def _write_xlsx(self, path: Path, rows: list) -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "Сообщения"
        headers = ["Дата и время", "Чат", "Автор", "Ник (@)", "Текст", "Фото"]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        wrap = Alignment(vertical="top", wrap_text=True)
        for r in rows:
            username = f"@{r['sender_username']}" if r["sender_username"] else ""
            ws.append([
                r["date"], r["chat_title"], r["sender_display_name"] or "",
                username, text_with_markers(r), photo_marker(r),
            ])
            row_idx = ws.max_row
            for col in range(1, 7):
                ws.cell(row=row_idx, column=col).alignment = wrap
            if r["photo_path"]:
                photo_cell = ws.cell(row=row_idx, column=6)
                # Relative to the exported file — resolves whether the
                # photos sit on disk as-is next to the export, or the
                # accompanying zip gets extracted into the same folder
                # (it preserves this same photos/<chat_id>/<id>.jpg layout).
                photo_cell.hyperlink = r["photo_path"].replace("\\", "/")
                photo_cell.style = "Hyperlink"

        widths = {1: 24, 2: 30, 3: 20, 4: 18, 5: 90, 6: 32}
        for col, width in widths.items():
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        wb.save(path)

    def _write_jsonl(self, path: Path, rows: list) -> None:
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({c: r[c] for c in ROW_COLUMNS}, ensure_ascii=False))
                f.write("\n")

    def _write_markdown(self, path: Path, rows: list, params: ExportParams,
                         chats_label: str) -> None:
        header_tpl = params.markdown_header or DEFAULT_MD_HEADER
        period = f"{params.date_from or 'начало'} — {params.date_to or 'сегодня'}"
        header = header_tpl.format(chats=chats_label, period=period, count=len(rows))
        buf = io.StringIO()
        buf.write(header)
        last_day = None
        for r in rows:
            day = str(r["date"])[:10]
            if day != last_day:
                buf.write(f"\n## {day}\n\n")
                last_day = day
            author = r["sender_display_name"] or "—"
            handle = f" (@{r['sender_username']})" if r["sender_username"] else ""
            buf.write(f"**{author}{handle}** · {r['chat_title']} · {r['date']}\n\n")
            if r["forwarded_from"]:
                buf.write(f"_переслано от {r['forwarded_from']}_\n\n")
            if r["reply_to_message_id"]:
                buf.write(f"_ответ на сообщение {r['reply_to_message_id']}_\n\n")
            buf.write(f"{r['text'] or ''}\n\n")
            if r["photo_path"]:
                buf.write(f"[фото: {r['photo_path']}]\n\n")
            buf.write(f"<{r['link']}>\n\n---\n\n")
        path.write_text(buf.getvalue(), encoding="utf-8")

    def _write_chunk(self, path: Path, rows: list, params: ExportParams, chats_label: str) -> None:
        if params.format == "xlsx":
            self._write_xlsx(path, rows)
        elif params.format == "jsonl":
            self._write_jsonl(path, rows)
        else:
            self._write_markdown(path, rows, params, chats_label)

    # ---- run -----------------------------------------------------------
    def run(self, params: ExportParams) -> ExportResult:
        rows = self._selected_rows(params)
        folder = Path(params.folder or self.paths.exports_dir)
        folder.mkdir(parents=True, exist_ok=True)
        ext = self._ext(params)
        output_paths: list[str] = []
        chats_label = ", ".join(self._chat_title(c) for c in params.chat_ids)

        if params.merge:
            for label, chunk in self._chunk_rows(rows, params):
                base = self._base_name(params, None)
                name = f"{base}{'_' + label if label else ''}.{ext}"
                path = folder / name
                self._write_chunk(path, chunk, params, chats_label)
                output_paths.append(str(path))
        else:
            for chat_id, chat_rows in self._group_by_chat(rows).items():
                base = self._base_name(params, chat_id)
                for label, chunk in self._chunk_rows(chat_rows, params):
                    name = f"{base}{'_' + label if label else ''}.{ext}"
                    path = folder / name
                    self._write_chunk(path, chunk, params, self._chat_title(chat_id))
                    output_paths.append(str(path))

        if params.zip_photos:
            photo_rows = [r for r in rows if r["photo_path"]]
            if photo_rows:
                zip_path = folder / "chatgrab_photos.zip"
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for r in photo_rows:
                        src = self.paths.data_dir / r["photo_path"]
                        if src.exists():
                            zf.write(src, arcname=r["photo_path"])
                output_paths.append(str(zip_path))

        max_id_by_chat: dict[str, int] = {}
        for r in rows:
            cid = str(r["chat_id"])
            max_id_by_chat[cid] = max(max_id_by_chat.get(cid, 0), r["message_id"])

        log_id = self.db.add_export_log(
            created_at=now_iso(),
            chat_ids=json.dumps(params.chat_ids),
            format=params.format,
            date_from=params.date_from,
            date_to=params.date_to,
            merge=1 if params.merge else 0,
            split_mode=params.split_mode,
            token_limit=params.token_limit,
            incremental=1 if params.incremental else 0,
            zip_photos=1 if params.zip_photos else 0,
            include_hidden=1 if params.include_hidden else 0,
            max_message_id_by_chat=json.dumps(max_id_by_chat),
            output_paths=json.dumps(output_paths),
            preset_name=params.preset_name,
        )
        return ExportResult(output_paths=output_paths, row_count=len(rows), export_log_id=log_id)

    # ---- presets -----------------------------------------------------
    def save_preset(self, name: str, params: ExportParams) -> None:
        self.db.save_preset(name, params.__dict__)

    def load_preset(self, name: str) -> ExportParams | None:
        for row in self.db.list_presets():
            if row["name"] == name:
                data = json.loads(row["params"])
                return ExportParams(**data)
        return None
