"""Lectura y generación de documentos dentro del espacio de trabajo aislado."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from ..security import wrap_untrusted
from . import LOCAL_WRITE, READ, SIDE_EFFECT, Tool, ToolContext, ToolError

MAX_READ_CHARS = 30000


def safe_path(ctx: ToolContext, rel: str, write: bool = False) -> Path:
    """Resuelve rutas impidiendo salir del espacio de trabajo (path traversal)."""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        raise ToolError("ruta vacía")
    base = ctx.workspace.resolve()
    root = Path(ctx.settings.workspace_dir).resolve()
    p = (base / rel).resolve()
    if not write and not p.exists():
        alt = (root / rel).resolve()  # lectura: también archivos compartidos del espacio general
        if alt.exists():
            p = alt
    allowed = base if write else root
    if p != allowed and allowed not in p.parents:
        raise ToolError("ruta fuera del espacio de trabajo: bloqueada")
    if write:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_file(ctx: ToolContext, path: str, start: int = 0, max_chars: int = 12000) -> str:
    p = safe_path(ctx, path)
    if not p.exists():
        raise ToolError(f"no existe: {path}")
    ext = p.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        parts = []
        for i, pg in enumerate(reader.pages):
            parts.append(f"--- página {i + 1} ---\n{pg.extract_text() or ''}")
        text = "\n".join(parts)
        if len(text.strip()) < 40 * max(1, len(reader.pages)):
            text += "\n[AVISO: poco texto extraíble; puede ser un PDF escaneado que requiere OCR]"
    elif ext == ".docx":
        import docx
        d = docx.Document(str(p))
        lines = [para.text for para in d.paragraphs]
        for ti, table in enumerate(d.tables):
            lines.append(f"--- tabla {ti + 1} ---")
            for row in table.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        text = "\n".join(lines)
    elif ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"--- hoja '{ws.title}' ---")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 500:
                    lines.append("[... filas truncadas: usá run_python con openpyxl/pandas para todo]")
                    break
                lines.append(" | ".join("" if v is None else str(v) for v in row))
        text = "\n".join(lines)
    elif ext == ".json":
        text = json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False, indent=1)
    else:
        text = p.read_text(encoding="utf-8", errors="replace")
    ctx.tainted = True  # el contenido de documentos tampoco es confiable
    chunk = text[start:start + min(max_chars, MAX_READ_CHARS)]
    more = "" if start + len(chunk) >= len(text) else f"\n[... quedan {len(text) - start - len(chunk)} caracteres; usá start={start + len(chunk)}]"
    return wrap_untrusted(f"archivo:{path}", chunk + more)


def _docx_from_markdown(md: str, dest: Path) -> None:
    import docx
    d = docx.Document()
    table_buf: list[list[str]] = []

    def flush_table():
        if not table_buf:
            return
        rows = [r for r in table_buf if not all(set(c) <= set("-: ") for c in r)]
        t = d.add_table(rows=len(rows), cols=max(len(r) for r in rows))
        t.style = "Table Grid"
        for i, r in enumerate(rows):
            for j, c in enumerate(r):
                t.cell(i, j).text = c
        table_buf.clear()

    for line in md.splitlines():
        s = line.rstrip()
        if s.startswith("|") and s.endswith("|"):
            table_buf.append([c.strip() for c in s.strip("|").split("|")])
            continue
        flush_table()
        if s.startswith("#"):
            level = min(len(s) - len(s.lstrip("#")), 4)
            d.add_heading(s.lstrip("#").strip(), level=level)
        elif s.startswith(("- ", "* ")):
            d.add_paragraph(s[2:], style="List Bullet")
        elif s[:3].rstrip(".").isdigit() and ". " in s[:4]:
            d.add_paragraph(s.split(". ", 1)[1], style="List Number")
        elif s:
            d.add_paragraph(s)
    flush_table()
    d.save(str(dest))


def _rows_from_content(content: str) -> dict[str, list[list]]:
    content = content.strip()
    if content.startswith("{") or content.startswith("["):
        data = json.loads(content)
        if isinstance(data, list):
            if data and isinstance(data[0], dict):
                cols = list(data[0].keys())
                return {"Hoja1": [cols] + [[row.get(c) for c in cols] for row in data]}
            return {"Hoja1": data}
        if "sheets" in data:
            data = data["sheets"]
        return {str(k)[:31]: v for k, v in data.items()}
    return {"Hoja1": list(csv.reader(io.StringIO(content)))}


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    """Crea un archivo. El formato se deduce de la extensión (.md .txt .csv .json .html .docx .xlsx)."""
    p = safe_path(ctx, path, write=True)
    ext = p.suffix.lower()
    if ext == ".docx":
        _docx_from_markdown(content, p)
    elif ext == ".xlsx":
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, rows in _rows_from_content(content).items():
            ws = wb.create_sheet(name)
            for row in rows:
                ws.append([_num(v) for v in row])
        wb.save(str(p))
    elif ext == ".json":
        json.loads(content)  # valida
        p.write_text(content, encoding="utf-8")
    elif ext in (".md", ".txt", ".csv", ".html", ".py", ".yml", ".yaml", ".xml", ".tsv"):
        p.write_text(content, encoding="utf-8")
    else:
        raise ToolError(f"extensión no soportada: {ext}")
    rel = str(p.relative_to(ctx.workspace))
    if rel not in ctx.created_files:
        ctx.created_files.append(rel)
    return f"Archivo creado: '{rel}' ({p.stat().st_size} bytes). Se adjuntará al resultado."


def _num(v):
    if isinstance(v, str):
        s = v.strip()
        try:
            if s and s.replace(".", "", 1).replace("-", "", 1).isdigit():
                return float(s) if "." in s else int(s)
        except ValueError:
            pass
    return v


def list_files(ctx: ToolContext) -> str:
    root = Path(ctx.settings.workspace_dir).resolve()
    items = []
    for p in sorted(ctx.workspace.rglob("*")):
        if p.is_file():
            items.append(f"{p.relative_to(ctx.workspace)} ({p.stat().st_size} B) [este trabajo]")
    shared = root / "compartido"
    if shared.exists():
        for p in sorted(shared.rglob("*")):
            if p.is_file():
                items.append(f"{p.relative_to(root)} ({p.stat().st_size} B) [compartido]")
    return "\n".join(items) or "(sin archivos)"


def delete_file(ctx: ToolContext, path: str) -> str:
    p = safe_path(ctx, path, write=True)
    if not p.exists():
        raise ToolError("no existe")
    p.unlink()
    return f"Eliminado: {path}"


TOOLS = [
    Tool("read_file", "Lee PDF, DOCX, XLSX, CSV, JSON, TXT/MD del espacio de trabajo.",
         {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer", "default": 0},
                                           "max_chars": {"type": "integer", "default": 12000}},
          "required": ["path"]}, read_file, READ),
    Tool("write_file", "Crea un documento: .md .txt .csv .json .html, .docx (contenido en markdown: #, -, tablas |a|b|) o .xlsx (contenido CSV o JSON {\"Hoja\": [[fila],...]}).",
         {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
          "required": ["path", "content"]}, write_file, LOCAL_WRITE),
    Tool("list_files", "Lista los archivos disponibles.", {"type": "object", "properties": {}},
         list_files, READ),
    Tool("delete_file", "Elimina un archivo del espacio de trabajo (requiere aprobación).",
         {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
         delete_file, SIDE_EFFECT),
]
