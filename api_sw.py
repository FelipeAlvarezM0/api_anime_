# api_sw.py
# ------------------------------------------------------------
# FastAPI que permite:
#   1) /search?q=...                       -> buscar series
#   2) /anime/{slug}/episodes              -> listar episodios
#   3) /anime/{slug}/episode/{id}/videos   -> obtener "var videos" (code/url)
#   4) /catalog                            -> listar catálogo (slug + título)
#   5) /slugs                              -> lista solo slugs
#   6) /resolve?title=...                  -> resolver título humano a slug
#
# Correr local:
#   python -m uvicorn api_sw:app --reload
# ------------------------------------------------------------

import os
import re
import html
import json
import time
import requests
import difflib
from functools import lru_cache
from typing import List, Optional, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from animeflv import AnimeFLV

# ------------ Config scraping (env vars soportadas) ------------
BASE_CANDIDATES = list(
    filter(None, [x.strip() for x in os.getenv("BASE_CANDIDATES", "").split(",")])
) or [
    "https://www3.animeflv.net",
    "https://www2.animeflv.net",
    "https://www.animeflv.net",
]

UA = os.getenv(
    "SCRAPER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36",
)

VIDEOS_RE = re.compile(r"var\s+videos\s*=\s*(\{.*?\});", re.DOTALL | re.IGNORECASE)
ANIMEID_RE = re.compile(r"var\s+anime_id\s*=\s*(\d+)\s*;", re.IGNORECASE)
EPISODEID_RE = re.compile(r"var\s+episode_id\s*=\s*(\d+)\s*;", re.IGNORECASE)
EPNUM_RE = re.compile(r"var\s+episode_number\s*=\s*(\d+)\s*;", re.IGNORECASE)

# ------------ Models ------------
class SeriesItem(BaseModel):
    id: str
    title: str
    poster: Optional[str] = None
    synopsis: Optional[str] = None

class EpisodeItem(BaseModel):
    id: int
    number: int
    title: str

class VideoItem(BaseModel):
    track: Literal["SUB", "LAT"]
    server: str
    title: Optional[str] = None
    code: Optional[str] = None
    url: Optional[str] = None

class VideosResponse(BaseModel):
    page_url: str
    anime_id: Optional[str] = None
    episode_id: Optional[str] = None
    episode_number: Optional[str] = None
    items: List[VideoItem]

# ------------ App ------------
app = FastAPI(
    title="Anime API (videos embebidos)",
    version="2.2.0",
    description=(
        "API para buscar series, listar episodios y obtener enlaces de 'var videos' tal como aparecen en AnimeFLV. "
        "Incluye catálogo de slugs y resolver de títulos."
    )
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------ Helpers ------------
def with_api():
    return AnimeFLV()

def retry(fn, tries=3, delay=0.8):
    last_exc = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            time.sleep(delay)
    raise last_exc

def http_get(url, referer=None, timeout=20):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r

def fetch_episode_html(slug: str, ep_number: int) -> tuple[str, str]:
    last_err = None
    for base in BASE_CANDIDATES:
        url = f"{base}/ver/{slug}-{ep_number}"
        try:
            html_text = http_get(url).text
            return html_text, url
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"No se pudo cargar la página del episodio. Último error: {last_err}")

def extract_videos_dict(page_html: str) -> dict:
    m = VIDEOS_RE.search(page_html)
    if not m:
        raise RuntimeError("No encontré 'var videos = {...};' en la página.")
    raw_json = html.unescape(m.group(1))
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        cleaned = raw_json.replace("\n", " ").replace("\r", " ")
        return json.loads(cleaned)

def extract_ids(page_html: str) -> dict:
    def _get(rx):
        m = rx.search(page_html)
        return m.group(1) if m else None
    return {
        "anime_id": _get(ANIMEID_RE),
        "episode_id": _get(EPISODEID_RE),
        "episode_number": _get(EPNUM_RE),
    }

def flatten_videos(videos: dict) -> List[VideoItem]:
    out: List[VideoItem] = []
    for track in ("SUB", "LAT"):
        items = videos.get(track)
        if not isinstance(items, list):
            continue
        for it in items:
            out.append(VideoItem(
                track=track,  # type: ignore
                server=(it.get("server") or "").lower(),
                title=it.get("title"),
                code=(it.get("code") or "").replace("\\/", "/") or None,
                url=(it.get("url") or "").replace("\\/", "/") or None,
            ))
    return out

PREFERRED = ("sw", "streamwish", "sb", "streamsb", "sbplay", "stape", "okru", "uqload", "mega")

def pick_best(items: List[VideoItem]) -> Optional[VideoItem]:
    def pref_index(server: str) -> int:
        s = (server or "").lower()
        for i, name in enumerate(PREFERRED):
            if s == name:
                return i
        return len(PREFERRED) + 10
    items_sorted = sorted(items, key=lambda it: (pref_index(it.server), 0 if it.code else 1))
    return items_sorted[0] if items_sorted else None

# ------------ Catalog scraping (slugs + títulos) ------------
# /browse ordenado por título y paginado: ?order=title&page=N
BROWSE_PATH = "/browse?order=title&page={page}"

# Regex: <a href="/anime/{slug}" ... title="{Title}">
CAT_ITEM_RE = re.compile(
    r'href="(?:https?://[^/]+)?/anime/([^"]+)"[^>]*title="([^"]+)"',
    re.IGNORECASE
)

def fetch_catalog_page(page: int, base: Optional[str] = None) -> list[tuple[str, str]]:
    """Devuelve [(slug, title), ...] para una página del browse."""
    bases = [base] if base else BASE_CANDIDATES
    last_err = None
    for b in bases:
        url = f"{b}{BROWSE_PATH.format(page=page)}"
        try:
            text = http_get(url, timeout=25).text
            items = CAT_ITEM_RE.findall(text)
            out = []
            for slug, title in items:
                out.append((slug.strip(), html.unescape(title).strip()))
            return out
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"No se pudo cargar browse page={page}. Último error: {last_err}")

def crawl_catalog(start_page: int = 1, max_pages: int = 50, sleep_sec: float = 0.0) -> list[dict]:
    """
    Recorre páginas consecutivas del browse hasta:
      - llegar a max_pages, o
      - que una página no traiga resultados (fin).
    Devuelve [{id: slug, title: str}, ...] sin duplicados.
    """
    seen = set()
    out: list[dict] = []
    for p in range(start_page, start_page + max_pages):
        items = fetch_catalog_page(p)
        if not items:
            break
        added = 0
        for slug, title in items:
            if slug and slug not in seen:
                seen.add(slug)
                out.append({"id": slug, "title": title})
                added += 1
        if added == 0:
            break
        if sleep_sec:
            time.sleep(sleep_sec)
    return out

# ---------- Cache en memoria + archivo opcional ----------
CATALOG_CACHE: list[dict] = []
CATALOG_CACHE_TS: float = 0.0
CATALOG_TTL_SEC = int(os.getenv("CATALOG_TTL_SEC", "21600"))  # 6h por defecto
CATALOG_JSON_PATH = os.getenv("CATALOG_JSON_PATH", "")

def _load_catalog_from_disk() -> list[dict]:
    if not CATALOG_JSON_PATH or not os.path.exists(CATALOG_JSON_PATH):
        return []
    try:
        with open(CATALOG_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict) and "id" in x and "title" in x]
    except Exception:
        pass
    return []

def _save_catalog_to_disk(items: list[dict]) -> None:
    if not CATALOG_JSON_PATH:
        return
    try:
        with open(CATALOG_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception:
        pass

def get_cached_catalog(force_refresh: bool = False) -> list[dict]:
    global CATALOG_CACHE, CATALOG_CACHE_TS
    now = time.time()
    if (not force_refresh) and CATALOG_CACHE and (now - CATALOG_CACHE_TS) < CATALOG_TTL_SEC:
        return CATALOG_CACHE
    # intenta disco
    if not force_refresh and not CATALOG_CACHE:
        disk_items = _load_catalog_from_disk()
        if disk_items:
            CATALOG_CACHE = disk_items
            CATALOG_CACHE_TS = now
            return CATALOG_CACHE
    # refresca raspando
    items = crawl_catalog(start_page=1, max_pages=int(os.getenv("CATALOG_MAX_PAGES", "80")), sleep_sec=0.15)
    CATALOG_CACHE = items
    CATALOG_CACHE_TS = now
    _save_catalog_to_disk(items)
    return CATALOG_CACHE

@lru_cache(maxsize=1)
def get_slug_set() -> set[str]:
    return set(x["id"] for x in get_cached_catalog(force_refresh=False))

def fuzzy_resolve_title(title: str, cutoff: float = 0.6) -> Optional[dict]:
    """Intenta emparejar por título; devuelve {id, title} o None."""
    items = get_cached_catalog(force_refresh=False)
    titles = [x["title"] for x in items]
    match = difflib.get_close_matches(title, titles, n=1, cutoff=cutoff)
    if not match:
        return None
    best_title = match[0]
    for x in items:
        if x["title"] == best_title:
            return x
    return None

# ------------ Endpoints ------------
@app.get("/search", response_model=List[SeriesItem])
def search_series(q: str = Query(..., min_length=1, description="Nombre del anime a buscar")):
    try:
        with with_api() as api:
            results = retry(lambda: api.search(q)) or []
            return [
                SeriesItem(
                    id=e.id,
                    title=e.title,
                    poster=getattr(e, "poster", None),
                    synopsis=getattr(e, "synopsis", None),
                )
                for e in results
            ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al buscar series: {e}")

@app.get("/anime/{anime_id}/episodes", response_model=List[EpisodeItem])
def get_episodes(anime_id: str):
    try:
        if anime_id.isdigit():
            raise HTTPException(
                status_code=400,
                detail="anime_id inválido. Debe ser el slug devuelto por /search (ej: 'dragon-ball-daima')."
            )
        with with_api() as api:
            info = retry(lambda: api.get_anime_info(anime_id))
            eps = list(info.episodes or [])
            eps.sort(key=lambda x: x.id)
            return [
                EpisodeItem(id=ep.id, number=ep.id, title=(getattr(ep, "title", None) or f"Episodio {ep.id}"))
                for ep in eps
            ]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener episodios: {e}")

@app.get("/anime/{anime_id}/episode/{episode_id}/videos", response_model=VideosResponse)
def get_episode_videos(
    anime_id: str,
    episode_id: int,
    only: Optional[str] = Query(None, description="Filtra por servidor (ej: 'sw', 'mega', 'stape')"),
    prefer_best: bool = Query(False, description="Si true, devuelve solo el mejor enlace según prioridad"),
):
    try:
        if anime_id.isdigit():
            raise HTTPException(
                status_code=400,
                detail="anime_id inválido. Debe ser el slug devuelto por /search (ej: 'dragon-ball-daima')."
            )
        page_html, page_url = fetch_episode_html(anime_id, episode_id)
        videos = extract_videos_dict(page_html)
        ids = extract_ids(page_html)
        items = flatten_videos(videos)
        if not items:
            raise HTTPException(status_code=502, detail="No se encontraron enlaces en 'var videos'.")
        if only:
            k = only.strip().lower()
            items = [it for it in items if it.server == k]
            if not items:
                raise HTTPException(status_code=404, detail=f"No hay enlaces para el servidor '{only}'.")
        if prefer_best:
            best = pick_best(items)
            if best:
                items = [best]
        return VideosResponse(
            page_url=page_url,
            anime_id=ids.get("anime_id"),
            episode_id=ids.get("episode_id"),
            episode_number=ids.get("episode_number"),
            items=items,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudieron obtener los enlaces: {e}")

# ------------ Endpoints de catálogo/slug ------------
@app.get("/catalog", response_model=List[SeriesItem])
def list_catalog(
    refresh: bool = Query(False, description="Si true, fuerza recrawl/refresh del catálogo"),
    start_page: int = Query(1, ge=1),
    max_pages: int = Query(0, ge=0, description="Si >0, ignora cache y recorre N páginas desde start_page")
):
    """
    Devuelve el catálogo en forma [{id: slug, title, poster=None, synopsis=None}, ...].
    Si max_pages>0, hace crawl ad-hoc sin tocar la caché global (útil para pruebas).
    """
    try:
        if max_pages > 0:
            raw = crawl_catalog(start_page=start_page, max_pages=max_pages, sleep_sec=0.15)
        else:
            raw = get_cached_catalog(force_refresh=refresh)
        return [SeriesItem(id=x["id"], title=x["title"]) for x in raw]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo obtener el catálogo: {e}")

@app.get("/slugs", response_model=List[str])
def list_slugs():
    """Solo devuelve la lista de slugs válidos."""
    try:
        return [x["id"] for x in get_cached_catalog(force_refresh=False)]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo obtener slugs: {e}")

@app.get("/resolve")
def resolve_title_to_slug(
    title: str = Query(..., min_length=2, description="Título aproximado a resolver a slug"),
    cutoff: float = Query(0.6, ge=0.0, le=1.0)
):
    """
    Devuelve {"id": slug, "title": title} para un título aproximado.
    Útil cuando tenés el nombre humano y querés el identificador que usan tus endpoints.
    """
    try:
        match = fuzzy_resolve_title(title, cutoff=cutoff)
        if not match:
            raise HTTPException(status_code=404, detail="No se encontró un slug probable para ese título.")
        return match
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al resolver título: {e}")

# ------------ Main ------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run("api_sw:app", host="0.0.0.0", port=port, reload=True)
