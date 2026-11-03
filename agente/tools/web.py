"""Investigación web: búsqueda y lectura de páginas con protección SSRF e inyección."""
from __future__ import annotations

import io
import json
import os
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..security import URLBlocked, check_url, wrap_untrusted
from . import EXTERNAL, LOCAL_WRITE, SIDE_EFFECT, Tool, ToolContext, ToolError

UA = "Mozilla/5.0 (X11; Linux x86_64) AgenteSupremo/1.0 (+asistente personal; respeta robots)"
MAX_BYTES = 6_000_000


def normalize_url(u: str) -> str:
    p = urlparse(u.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def _get(url: str, timeout: int = 25, max_redirects: int = 5) -> requests.Response:
    """GET que valida cada salto de redirección contra SSRF."""
    for _ in range(max_redirects + 1):
        check_url(url)
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "es,en;q=0.8"},
                         timeout=timeout, allow_redirects=False, stream=True)
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            r.close()
            if not loc:
                raise ToolError("redirección sin destino")
            url = urljoin(url, loc)
            continue
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                r.close()
                raise ToolError(f"respuesta demasiado grande (> {MAX_BYTES // 1_000_000} MB)")
            chunks.append(chunk)
        r._content = b"".join(chunks)
        r.url = url
        return r
    raise ToolError("demasiadas redirecciones")


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


# -- búsqueda --------------------------------------------------------------------
def _ddg(query: str, n: int) -> list[dict]:
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "wt-wt"},
                      headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for res in soup.select("div.result"):
        a = res.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if "duckduckgo.com/l/" in href:
            q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
            href = unquote(q.get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:  # anuncios
            continue
        snip = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": href,
                    "snippet": snip.get_text(" ", strip=True) if snip else ""})
        if len(out) >= n:
            break
    return out


def _wikipedia(query: str, n: int, lang: str) -> list[dict]:
    r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n},
        headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    out = []
    for it in r.json().get("query", {}).get("search", []):
        title = it["title"]
        snippet = BeautifulSoup(it.get("snippet", ""), "lxml").get_text()
        out.append({"title": title, "url": f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}",
                    "snippet": snippet})
    return out


def _searxng(query: str, n: int) -> list[dict]:
    base = os.environ["SEARXNG_URL"].rstrip("/")
    r = requests.get(f"{base}/search", params={"q": query, "format": "json"}, timeout=20,
                     headers={"User-Agent": UA})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])[:n]]


def _brave(query: str, n: int) -> list[dict]:
    r = requests.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": n},
                     headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
                     timeout=20)
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
            for x in r.json().get("web", {}).get("results", [])[:n]]


def web_search(ctx: ToolContext, query: str, max_results: int = 6) -> str:
    max_results = max(1, min(int(max_results), 10))
    backends = []
    if os.environ.get("SEARXNG_URL"):
        backends.append(("searxng", _searxng))
    if os.environ.get("BRAVE_API_KEY"):
        backends.append(("brave", _brave))
    backends += [("duckduckgo", _ddg), ("wikipedia-es", lambda q, n: _wikipedia(q, n, "es")),
                 ("wikipedia-en", lambda q, n: _wikipedia(q, n, "en"))]
    errors, results, used = [], [], ""
    for name, fn in backends:
        try:
            results = fn(query, max_results)
        except Exception as e:  # noqa: BLE001 - se informa y se usa el siguiente motor
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if results:
            used = name
            break
        errors.append(f"{name}: sin resultados")
    if not results:
        return "BÚSQUEDA SIN RESULTADOS. Errores: " + " | ".join(errors)
    ctx.tainted = True
    lines = []
    for i, r in enumerate(results, 1):
        ctx.seen_urls.add(normalize_url(r["url"]))
        lines.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   Extracto: {r['snippet'][:300]}")
    note = ("Los extractos de búsqueda NO alcanzan para afirmar hechos: abrí las fuentes con fetch_url "
            "antes de marcarlas como verificadas.")
    return wrap_untrusted(f"búsqueda:{used}", "\n".join(lines)) + "\n" + note


def fetch_url(ctx: ToolContext, url: str, max_chars: int = 8000, start: int = 0) -> str:
    max_chars = max(500, min(int(max_chars), 20000))
    try:
        r = _get(url)
    except URLBlocked as e:
        raise ToolError(f"URL bloqueada por seguridad: {e}") from e
    except requests.RequestException as e:
        raise ToolError(f"no se pudo descargar: {type(e).__name__}: {str(e)[:150]}") from e
    ctype = r.headers.get("Content-Type", "").lower()
    status = r.status_code
    title = ""
    if "pdf" in ctype or r.url.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:60])
    elif "json" in ctype:
        try:
            text = json.dumps(r.json(), ensure_ascii=False, indent=1)
        except ValueError:
            text = r.text
    elif "html" in ctype or "<html" in r.text[:500].lower():
        title, text = html_to_text(r.text)
    else:
        text = r.content.decode(r.encoding or "utf-8", errors="replace")
    ctx.tainted = True
    ctx.seen_urls.add(normalize_url(url))
    ctx.seen_urls.add(normalize_url(r.url))
    total = len(text)
    chunk = text[start:start + max_chars]
    more = f"\n[... {total - start - len(chunk)} caracteres más; usá start={start + len(chunk)}]" if start + len(chunk) < total else ""
    header = f"HTTP {status} | URL final: {r.url} | título: {title or '-'} | {total} caracteres\n"
    if status >= 400:
        header = f"ATENCIÓN: la página respondió HTTP {status}; no la cites como fuente válida.\n" + header
    return wrap_untrusted(r.url, header + chunk + more)


def download_file(ctx: ToolContext, url: str, filename: str) -> str:
    from .files import safe_path
    dest = safe_path(ctx, filename, write=True)
    try:
        r = _get(url, timeout=60)
    except URLBlocked as e:
        raise ToolError(f"URL bloqueada por seguridad: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"HTTP {r.status_code} al descargar")
    dest.write_bytes(r.content)
    ctx.tainted = True
    ctx.seen_urls.add(normalize_url(url))
    rel = str(dest.relative_to(ctx.workspace))
    ctx.created_files.append(rel)
    return f"Descargado {len(r.content)} bytes en '{rel}' ({r.headers.get('Content-Type', '?')}). Usá read_file para leerlo."


def http_request(ctx: ToolContext, method: str, url: str, body: str = "", headers: dict | None = None) -> str:
    method = method.upper()
    try:
        check_url(url)
    except URLBlocked as e:
        raise ToolError(f"URL bloqueada: {e}") from e
    hdrs = {"User-Agent": UA}
    hdrs.update({k: str(v) for k, v in (headers or {}).items()})
    r = requests.request(method, url, data=body.encode() if body else None, headers=hdrs, timeout=30,
                         allow_redirects=False)
    ctx.tainted = True
    ctx.seen_urls.add(normalize_url(url))
    return wrap_untrusted(url, f"HTTP {r.status_code}\n{r.text[:6000]}")


def _http_policy(ctx: ToolContext, args: dict) -> str | None:
    if str(args.get("method", "GET")).upper() not in ("GET", "HEAD"):
        return f"petición HTTP {args.get('method')} que puede modificar un servicio externo"
    return None


TOOLS = [
    Tool("web_search", "Busca en internet. Devuelve títulos, URLs y extractos (no verificados).",
         {"type": "object", "properties": {
             "query": {"type": "string"}, "max_results": {"type": "integer", "default": 6}},
          "required": ["query"]}, web_search, EXTERNAL),
    Tool("fetch_url", "Descarga y lee una página web, PDF o JSON público. Usalo para verificar fuentes.",
         {"type": "object", "properties": {
             "url": {"type": "string"}, "max_chars": {"type": "integer", "default": 8000},
             "start": {"type": "integer", "default": 0}}, "required": ["url"]}, fetch_url, EXTERNAL),
    Tool("download_file", "Descarga un archivo público (PDF, XLSX, CSV, etc.) al espacio de trabajo.",
         {"type": "object", "properties": {"url": {"type": "string"}, "filename": {"type": "string"}},
          "required": ["url", "filename"]}, download_file, LOCAL_WRITE),
    Tool("http_request", "Llama a una API HTTP pública. GET/HEAD libres; POST/PUT/PATCH/DELETE requieren aprobación del usuario.",
         {"type": "object", "properties": {
             "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
             "url": {"type": "string"}, "body": {"type": "string"},
             "headers": {"type": "object", "additionalProperties": {"type": "string"}}},
          "required": ["method", "url"]}, http_request, EXTERNAL, approval_policy=_http_policy),
]
