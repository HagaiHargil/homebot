"""URL -> saved recipe.

Invariant: a capture is never lost. Every step that can fail is wrapped; failures are
recorded in `fetch_error` and the row is inserted regardless.

Blocking (httpx sync); the bot calls `save_url` via asyncio.to_thread.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

import db

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}
TIMEOUT = 10
MAX_HTML = 5 * 1024 * 1024
MAX_IMAGE = 2 * 1024 * 1024

TRACKING_PARAMS = {"fbclid", "igshid", "gclid", "ref", "si"}
# second-level labels under which the registrable domain is three labels deep (x.co.il, x.co.uk)
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "muni", "ltd", "plc", "or", "ne"}
# titles that mean "we got a login wall", not a recipe name
JUNK_TITLES = {
    "instagram", "tiktok", "facebook", "log in", "login", "log in or sign up to view",
    "tiktok - make your day", "just a moment...", "access denied", "attention required!",
    "403 forbidden", "404 not found", "page not found",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class TooLarge(Exception):
    pass


# --------------------------------------------------------------------------- URLs

def find_url(text: str | None) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,;:!?)]}»”'") if m else None


def canonicalize(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80) or (parts.scheme == "https" and parts.port == 443)
    ):
        netloc = f"{host}:{parts.port}"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)
        ],
        doseq=True,
    )
    path = parts.path.rstrip("/")
    return urlunsplit(((parts.scheme or "https").lower(), netloc, path, query, ""))


def registrable_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


# --------------------------------------------------------------------------- fetching

def _get_limited(client: httpx.Client, url: str, limit: int) -> httpx.Response:
    """GET with a hard body-size cap. Returns a response whose .content is populated."""
    with client.stream("GET", url) as resp:
        length = resp.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            raise TooLarge(f"response too large ({int(length)} bytes)")
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise TooLarge(f"response larger than {limit} bytes")
            chunks.append(chunk)
        resp._content = b"".join(chunks)  # noqa: SLF001 - make .text/.content usable after close
        return resp


def _client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=TIMEOUT)


# --------------------------------------------------------------------------- parsing

def _clean(s) -> str | None:
    if not isinstance(s, str):
        return None
    s = re.sub(r"\s+", " ", s).strip()
    if not s or s.lower() in JUNK_TITLES:
        return None
    return s[:300]


def _image_url(img) -> str | None:
    if isinstance(img, str):
        return img or None
    if isinstance(img, list):
        for item in img:
            u = _image_url(item)
            if u:
                return u
    if isinstance(img, dict):
        return _image_url(img.get("url") or img.get("contentUrl") or img.get("@id"))
    return None


def _ingredients(val) -> list[str] | None:
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return None
    out = [re.sub(r"\s+", " ", x).strip() for x in val if isinstance(x, str) and x.strip()]
    return out or None


def _parse_recipe_scrapers(html: str, url: str) -> dict | None:
    from recipe_scrapers import scrape_html

    scraper = scrape_html(html, org_url=url)
    out = {}
    for key, fn in (("title", scraper.title), ("ingredients", scraper.ingredients),
                    ("image", scraper.image)):
        try:
            out[key] = fn()
        except Exception:  # individual fields are optional
            out[key] = None
    title = _clean(out["title"])
    if not title:
        return None
    return {"title": title, "ingredients": _ingredients(out["ingredients"]),
            "image": _image_url(out["image"])}


def _walk_jsonld(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk_jsonld(node[key])


def _is_recipe(obj: dict) -> bool:
    t = obj.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and "recipe" in x.lower() for x in types)


def _parse_jsonld(html: str, url: str) -> dict | None:
    import extruct

    data = extruct.extract(html, base_url=url, syntaxes=["json-ld"], uniform=True)
    for obj in _walk_jsonld(data.get("json-ld", [])):
        if _is_recipe(obj):
            title = _clean(obj.get("name") or obj.get("headline"))
            if title:
                return {"title": title, "ingredients": _ingredients(obj.get("recipeIngredient")),
                        "image": _image_url(obj.get("image"))}
    return None


def _parse_meta(html: str, url: str) -> dict | None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return tag.get("content") if tag else None

    title = _clean(meta("og:title")) or _clean(soup.title.string if soup.title else None)
    if not title:
        return None
    return {"title": title, "ingredients": None, "image": _image_url(meta("og:image"))}


PARSERS = (_parse_recipe_scrapers, _parse_jsonld, _parse_meta)


def parse_html(html: str, url: str) -> tuple[dict | None, list[str]]:
    """First parser to yield a title wins. Returns (result, parser errors)."""
    errors = []
    for parser in PARSERS:
        try:
            result = parser(html, url)
            if result:
                return result, errors
        except Exception as e:
            errors.append(f"{parser.__name__.removeprefix('_parse_')}: {type(e).__name__}: {str(e)[:120]}")
    return None, errors


# --------------------------------------------------------------------------- pipeline

def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:500]


def save_url(url: str, local_date: str, conn=None, data_dir: Path | None = None) -> dict:
    """Run the full pipeline. Returns {'recipe_id', 'already_exists', 'fetch_error'}.

    Opens its own DB connection unless given one (so it is safe inside to_thread).
    """
    own = conn is None
    conn = conn or db.connect()
    data_dir = Path(data_dir or db.data_dir())
    try:
        return _save_url(conn, url, local_date, data_dir)
    finally:
        if own:
            conn.close()


def _save_url(conn, url: str, local_date: str, data_dir: Path) -> dict:
    errors: list[str] = []

    # 1-2. canonicalize the URL as given and dedupe before spending a fetch on it
    try:
        canonical = canonicalize(url)
    except Exception as e:
        canonical, errors = url, [f"canonicalize: {_err(e)}"]
    existing = db.get_recipe_by_canonical(conn, canonical)
    if existing:
        return {"recipe_id": existing["id"], "already_exists": True,
                "fetch_error": existing["fetch_error"]}

    # 3. fetch (redirects followed; the final URL is canonicalized and deduped again)
    final_url, html, client = url, None, None
    try:
        client = _client()
        resp = _get_limited(client, url, MAX_HTML)
        final_url = str(resp.url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype and ctype:
            raise ValueError(f"not an HTML page ({ctype.split(';')[0]})")
        html = resp.text
    except Exception as e:
        errors.append(f"fetch: {_err(e)}")

    try:
        final_canonical = canonicalize(final_url)
    except Exception:
        final_canonical = canonical
    if final_canonical != canonical:
        existing = db.get_recipe_by_canonical(conn, final_canonical)
        if existing:
            if client:
                client.close()
            return {"recipe_id": existing["id"], "already_exists": True,
                    "fetch_error": existing["fetch_error"]}
        canonical = final_canonical

    # 4. parse
    parsed = None
    if html:
        parsed, parse_errors = parse_html(html, final_url)
        if not parsed:
            errors.extend(parse_errors)

    try:
        source = registrable_domain(final_url) or None
    except Exception:
        source = None

    if parsed:
        title = parsed["title"]
    else:
        title = f"{source or 'link'} – {local_date}"
        errors.insert(0, "no title found")  # first, so the 500-char cap never cuts it

    # insert — this must happen no matter what went wrong above
    fields = dict(url=final_url, url_canonical=canonical, source=source,
                  ingredients=(parsed or {}).get("ingredients"),
                  fetch_error=("; ".join(errors)[:500] if errors else None))
    try:
        recipe_id = db.insert_recipe(conn, title=title, **fields)
    except Exception as e:
        # a concurrent save of the same URL, or an unexpected value: retry minimal
        existing = db.get_recipe_by_canonical(conn, canonical)
        if existing:
            return {"recipe_id": existing["id"], "already_exists": True,
                    "fetch_error": existing["fetch_error"]}
        log.exception("insert failed, retrying with minimal row")
        recipe_id = db.insert_recipe(conn, title=title, url=url, url_canonical=canonical,
                                     source=source, fetch_error=_err(e))

    # 5. artifacts (failures only annotate the row)
    updates = {}
    if html:
        try:
            snap = data_dir / "snapshots" / f"{recipe_id}.html"
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(html, encoding="utf-8")
            updates["snapshot_path"] = str(snap.relative_to(data_dir))
        except Exception as e:
            errors.append(f"snapshot: {_err(e)}")

    image_url = (parsed or {}).get("image")
    if image_url and client:
        try:
            from urllib.parse import urljoin

            resp = _get_limited(client, urljoin(final_url, image_url), MAX_IMAGE)
            resp.raise_for_status()
            if not resp.headers.get("content-type", "").lower().startswith("image/"):
                raise ValueError(f"not an image ({resp.headers.get('content-type')})")
            img = data_dir / "images" / f"{recipe_id}.jpg"
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(resp.content)
            updates["image_path"] = str(img.relative_to(data_dir))
        except TooLarge:
            pass  # spec: skip silently if > 2 MB
        except Exception as e:
            errors.append(f"image: {_err(e)}")

    if client:
        client.close()

    if errors:
        updates["fetch_error"] = "; ".join(errors)[:500]
    if updates:
        try:
            db.update_recipe(conn, recipe_id, **updates)
        except Exception:
            log.exception("could not record artifacts for recipe %s", recipe_id)

    return {"recipe_id": recipe_id, "already_exists": False,
            "fetch_error": updates.get("fetch_error", fields["fetch_error"])}


if __name__ == "__main__":
    # Dry run: python ingest.py URL [URL...]  — fetch + parse only, writes nothing.
    import sys

    logging.basicConfig(level=logging.INFO)
    for u in sys.argv[1:]:
        print("==", u)
        print("   canonical:", canonicalize(u))
        try:
            with _client() as c:
                r = _get_limited(c, u, MAX_HTML)
                print("   final:", r.url, r.status_code)
                r.raise_for_status()
                res, errs = parse_html(r.text, str(r.url))
                print("   parsed:", json.dumps(res, ensure_ascii=False)[:400])
                if errs:
                    print("   parser errors:", errs)
        except Exception as e:
            print("   fetch error:", _err(e))
        print("   domain:", registrable_domain(u), "|", datetime.now().date())
