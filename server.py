# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27"]
# ///
"""ua-shop MCP server: find goods and stock on Rozetka and Prom.ua.

Read-only. Uses the same public JSON/pages the sites' own frontends load:
  Rozetka: search.rozetka.com.ua (search -> product ids), product-api.rozetka.com.ua
           (product price / sell status, seller info)
  Prom.ua: prom.ua/ua/search and product pages (embedded Apollo cache JSON)

Run:  uv run server.py        (stdio transport, spawned by Claude Code)
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

import logging

import httpx
from mcp.server.mcpserver import MCPServer

logging.getLogger("httpx").setLevel(logging.WARNING)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "uk,ru;q=0.8,en;q=0.6"}
RZ_COMMON = {"front-type": "xl", "country": "UA", "lang": "ua"}
CONCURRENCY = 8  # parallel requests per site; keep it polite

_client: httpx.AsyncClient | None = None
_sem = asyncio.Semaphore(CONCURRENCY)
_rz_seller_cache: dict[int, asyncio.Task] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=True)
    return _client


async def _get(url: str, params: dict | None = None) -> httpx.Response:
    async with _sem:
        for attempt in range(3):
            try:
                r = await client().get(url, params=params)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code in (429, 502, 503) and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 403 and "Just a moment" in r.text[:2000]:
                raise RuntimeError(f"Blocked by Cloudflare challenge: {url}")
            r.raise_for_status()
            return r
    raise RuntimeError("unreachable")


def _num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


# --------------------------------------------------------------------------- Rozetka

RZ_STATUS = {
    "available": "в наявності",
    "limited": "закінчується",
    "out_of_stock": "немає в наявності",
    "unavailable": "недоступний",
    "waiting_for_supply": "очікується",
    "call_to_order": "під замовлення",
    "archive": "архів",
}
RZ_IN_STOCK = {"available", "limited"}
RZ_SORT = {
    "relevance": None,
    "price_asc": "cheap",
    "price_desc": "expensive",
    "popular": "popularity",
    "new": "novelty",
    "rating": "rank",
}


async def _rz_seller_fetch(seller_id: int) -> dict | None:
    try:
        r = await _get(
            "https://product-api.rozetka.com.ua/v4/sellers/get",
            {"country": "UA", "lang": "ua", "id": seller_id},
        )
        d = (r.json() or {}).get("data") or {}
    except Exception:
        return None
    if not d:
        return None
    return {
        "name": d.get("title"),
        "rating": d.get("rank"),
        "votes": d.get("votes"),
        "phone": d.get("client_phone"),
        "url": d.get("link"),
    }


async def rz_seller(seller_id: int | None) -> dict | None:
    """Seller info, cached per process; concurrent lookups share one request."""
    if not seller_id:
        return None
    task = _rz_seller_cache.get(seller_id)
    if task is None:
        task = asyncio.ensure_future(_rz_seller_fetch(seller_id))
        _rz_seller_cache[seller_id] = task
    return await asyncio.shield(task)


async def rz_product_main(pid: int) -> dict | None:
    r = await _get(
        "https://product-api.rozetka.com.ua/v4/goods/get-main", {**RZ_COMMON, "goodsId": pid}
    )
    return (r.json() or {}).get("data")


def _rz_row(d: dict, seller: dict | None) -> dict:
    st = d.get("sell_status")
    cq = d.get("cart_quantity") or {}
    row = {
        "site": "rozetka",
        "id": d.get("id"),
        "title": d.get("title"),
        "price": d.get("price"),
        "old_price": d.get("old_price") or None,
        "currency": "UAH",
        "stock_status": st,
        "stock": RZ_STATUS.get(st, st),
        "in_stock": st in RZ_IN_STOCK,
        "seller": (seller or {}).get("name"),
        "seller_rating": (seller or {}).get("rating"),
        "seller_url": (seller or {}).get("url"),
        "brand": d.get("brand"),
        "article": d.get("article") or None,
        "condition": d.get("state"),
        "url": d.get("href"),
    }
    if cq.get("max"):
        row["max_per_order"] = cq.get("max")
    return row


async def rozetka_search_impl(
    query: str,
    min_price: float | None = None,
    max_price: float | None = None,
    sort: str = "relevance",
    page: int = 1,
    limit: int = 20,
    in_stock_only: bool = True,
) -> dict:
    params: dict[str, Any] = {**RZ_COMMON, "text": query}
    if page > 1:
        params["page"] = page
    if RZ_SORT.get(sort):
        params["sort"] = RZ_SORT[sort]
    if min_price is not None or max_price is not None:
        lo = int(min_price or 0)
        hi = int(max_price) if max_price is not None else 100_000_000
        params["price"] = f"{lo}-{hi}"
    r = await _get("https://search.rozetka.com.ua/ua/search/api/v6/", params)
    data = (r.json() or {}).get("data") or {}
    ids = [g["id"] for g in data.get("goods") or [] if g.get("id")]
    total = (data.get("quantities") or {}).get("goods_quantity_total_found")
    pag = data.get("pagination") or {}

    # Fetch more candidates than `limit` when filtering by stock.
    want = max(1, min(limit, 60))
    cand = ids[: min(len(ids), want * 2 if in_stock_only else want)]
    mains = await asyncio.gather(*(rz_product_main(i) for i in cand), return_exceptions=True)
    mains = [m for m in mains if isinstance(m, dict)]
    if in_stock_only:
        mains = [m for m in mains if m.get("sell_status") in RZ_IN_STOCK]
    mains = mains[:want]
    sellers = await asyncio.gather(*(rz_seller(m.get("seller_id")) for m in mains))
    rows = [_rz_row(m, s) for m, s in zip(mains, sellers)]
    if min_price is not None:
        rows = [x for x in rows if x["price"] is not None and x["price"] >= min_price]
    if max_price is not None:
        rows = [x for x in rows if x["price"] is not None and x["price"] <= max_price]
    return {
        "site": "rozetka",
        "query": query,
        "total_found": total,
        "page": pag.get("shown_page", page),
        "total_pages": pag.get("total_pages"),
        "returned": len(rows),
        "results": rows,
    }


async def rozetka_product_impl(product_id: int) -> dict:
    d = await rz_product_main(product_id)
    if not d:
        return {"error": f"Rozetka product {product_id} not found"}
    seller, chars = await asyncio.gather(
        rz_seller(d.get("seller_id")),
        _get(
            "https://product-api.rozetka.com.ua/v4/goods/get-characteristic",
            {**RZ_COMMON, "goodsId": product_id},
        ),
        return_exceptions=True,
    )
    seller = seller if isinstance(seller, dict) else None
    row = _rz_row(d, seller)
    row["seller_phone"] = (seller or {}).get("phone")
    row["seller_votes"] = (seller or {}).get("votes")
    row["category"] = (d.get("last_category") or {}).get("title")
    row["warnings"] = d.get("warning") or None
    specs: dict[str, str] = {}
    if isinstance(chars, httpx.Response):
        for grp in (chars.json() or {}).get("data") or []:
            for opt in grp.get("options") or []:
                vals = [v.get("title") for v in opt.get("values") or [] if v.get("title")]
                if opt.get("title") and vals:
                    specs[opt["title"]] = ", ".join(vals)
    row["specs"] = specs
    return row


# --------------------------------------------------------------------------- Prom.ua

PROM_SORT = {"relevance": None, "price_asc": "price", "price_desc": "-price"}
PROM_STATUS = {
    "avail": "в наявності",
    "order": "під замовлення",
    "waiting": "очікується",
    "not_avail": "немає в наявності",
    "none": "немає в наявності",
}


def _apollo_state(html: str) -> dict:
    marker = "window.ApolloCacheState = "
    i = html.find(marker)
    if i < 0:
        raise RuntimeError("Prom page has no ApolloCacheState (layout changed or blocked)")
    state, _ = json.JSONDecoder().raw_decode(html, i + len(marker))
    return state


def _prom_url(p: dict) -> str:
    return f"https://prom.ua/ua/p{p.get('id')}-{p.get('urlText') or 'product'}.html"


def _prom_row(p: dict) -> dict:
    pres = p.get("presence") or {}
    cpres = p.get("catalogPresence") or {}
    comp = p.get("company") or {}
    stats = comp.get("combinedOpinionStats") or comp.get("opinionStats") or {}
    sell = next(iter((p.get("sellingType") or {}).values()), {}) or {}
    code = pres.get("presence")
    in_stock = bool(pres.get("isAvailable") or pres.get("isEnding"))
    wholesale = sorted(
        {_num(w.get("price")) for w in p.get("wholesalePrices") or [] if _num(w.get("price"))}
    )
    return {
        "site": "prom",
        "id": p.get("id"),
        "title": p.get("name"),
        "price": _num(p.get("discountedPrice") or p.get("price")),
        "price_from": bool(p.get("priceFrom")),
        "currency": p.get("priceCurrency") or "UAH",
        "unit": p.get("measureUnit"),
        "stock_status": code,
        "stock": cpres.get("title") or PROM_STATUS.get(code, code),
        "in_stock": in_stock,
        "ending_soon": bool(pres.get("isEnding")),
        "wholesale_prices": wholesale or None,
        "selling_type": sell.get("label"),
        "seller": comp.get("name"),
        "seller_id": comp.get("id"),
        "seller_region": comp.get("regionName"),
        "seller_positive_pct": stats.get("opinionPositivePercent"),
        "seller_reviews": stats.get("opinionTotal"),
        "seller_url": f"https://prom.ua/ua/c{comp['id']}-{comp.get('slug', '')}.html"
        if comp.get("id")
        else None,
        "url": _prom_url(p),
    }


async def prom_search_impl(
    query: str,
    min_price: float | None = None,
    max_price: float | None = None,
    sort: str = "relevance",
    page: int = 1,
    limit: int = 20,
    in_stock_only: bool = True,
    wholesale_only: bool = False,
    with_specs: bool = False,
) -> dict:
    params: dict[str, Any] = {"search_term": query}
    if page > 1:
        params["page"] = page
    if PROM_SORT.get(sort):
        params["sort"] = PROM_SORT[sort]
    r = await _get("https://prom.ua/ua/search", params)
    fc = _apollo_state(r.text).get("_FAST_CACHE") or {}
    key = next((k for k in fc if k.startswith("SearchListingQuery")), None)
    if not key:
        return {"site": "prom", "query": query, "total_found": 0, "returned": 0, "results": []}
    pg = ((fc[key].get("result") or {}).get("listing") or {}).get("page") or {}
    rows = [_prom_row(it["product"]) for it in pg.get("products") or [] if it.get("product")]
    if in_stock_only:
        rows = [x for x in rows if x["in_stock"]]
    if wholesale_only:
        rows = [x for x in rows if x["wholesale_prices"] or "опт" in (x["selling_type"] or "")]
    # Prom's URL price filter is unreliable, so filter here.
    if min_price is not None:
        rows = [x for x in rows if x["price"] is not None and x["price"] >= min_price]
    if max_price is not None:
        rows = [x for x in rows if x["price"] is not None and x["price"] <= max_price]
    rows = rows[: max(1, limit)]
    if with_specs:  # search pages carry no specs: one product-page fetch per row
        await asyncio.gather(*(_prom_attach_specs(x) for x in rows))
    return {
        "site": "prom",
        "query": query,
        "total_found": pg.get("total"),
        "page": page,
        "page_size": 29,
        "returned": len(rows),
        "results": rows,
    }


def _walk(o: Any):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _prom_specs(p: dict) -> dict[str, str]:
    """Seller-filled characteristics ("Ємність акумулятору": "10000 мА/год", ...)."""
    specs: dict[str, str] = {}
    for a in p.get("attributes") or []:
        vals = [str(v.get("value")).strip() for v in a.get("values") or [] if v.get("value")]
        if a.get("name") and vals:
            specs[a["name"]] = ", ".join(vals)
    return specs


async def _prom_fetch_product(product: str | int) -> tuple[dict | None, dict, str]:
    """Load a Prom product page; returns (product, apollo_state, final_url)."""
    s = str(product).strip()
    if s.startswith("http"):
        url = s
        m = re.search(r"/p(\d+)", s)
        pid = int(m.group(1)) if m else None
    else:
        pid = int(re.sub(r"\D", "", s))
        url = f"https://prom.ua/ua/p{pid}-product.html"
    r = await _get(url)
    state = _apollo_state(r.text)
    prods = [
        d for d in _walk(state)
        if d.get("__typename") == "Product" and (pid is None or d.get("id") == pid)
    ]
    p = max(prods, key=len) if prods else None  # the most complete copy
    return p, state, str(r.url)


def _clip(text: str | None, n: int) -> str | None:
    text = re.sub(r"[ \t]+", " ", (text or "")).strip()
    if not text:
        return None
    return text if len(text) <= n else text[:n].rstrip() + "…"


async def _prom_attach_specs(row: dict) -> None:
    """Add specs (and a description excerpt if the seller filled no specs) to a search row."""
    try:
        p, _, _ = await _prom_fetch_product(row["id"])
    except Exception as e:
        row["specs_error"] = f"{type(e).__name__}: {e}"
        return
    if not p:
        row["specs_error"] = "product page not found"
        return
    row["specs"] = _prom_specs(p)
    if not row["specs"]:
        row["description_excerpt"] = _clip(p.get("descriptionPlain"), 600)


async def prom_product_impl(product: str) -> dict:
    p, state, final_url = await _prom_fetch_product(product)
    if not p:
        return {"error": f"Prom product not found: {product}"}
    row = _prom_row(p)
    row["url"] = final_url
    tiers = []
    for w in p.get("wholesalePrices") or []:
        tiers.append(
            {
                "price": _num(w.get("price")),
                "min_qty": _num(w.get("minimumOrderQuantity")),
                "unit": w.get("measureUnit"),
            }
        )
    row["wholesale_tiers"] = sorted(tiers, key=lambda t: t["min_qty"] or 0) or None
    row["orders_count"] = p.get("ordersCount")
    row["min_order_qty"] = _num(p.get("minimumOrderQuantity"))
    row["sku"] = p.get("sku") or None
    row["brand"] = (p.get("manufacturerInfo") or {}).get("name")
    row["category"] = (p.get("category") or {}).get("caption") or (p.get("category") or {}).get("name")
    row["warranty"] = p.get("warranty") or None
    row["specs"] = _prom_specs(p)
    # Many sellers put power/ports only in the free-text description.
    row["description"] = _clip(p.get("descriptionPlain"), 3000)
    comp_id = (p.get("company") or {}).get("id") or p.get("company_id")
    comps = [d for d in _walk(state) if d.get("__typename") == "Company" and d.get("id") == comp_id]
    if comps:
        c = max(comps, key=len)
        row["seller_min_order_sum"] = c.get("minOrderSum") or p.get("minOrderSum")
        phones = c.get("phones") or []
        row["seller_phones"] = [
            ph.get("number") or ph.get("phone") for ph in phones if isinstance(ph, dict)
        ] or None
        row["seller_address"] = c.get("addressText") or c.get("address") or None
    return row


# --------------------------------------------------------------------------- MCP tools

mcp = MCPServer(
    name="ua-shop",
    instructions=(
        "Find goods, prices and stock on Rozetka (rozetka.com.ua) and Prom.ua for purchasing. "
        "Use search_both / bulk_search for overviews, then rozetka_product / prom_product for "
        "seller contacts, specs and Prom wholesale tiers (price per min quantity). "
        "Stock is a status (in stock / ending / to order), not an exact count; for bulk "
        "quantities the buyer must confirm with the seller. Present results as tables with "
        "price, stock, seller and link."
    ),
)

Sort = Literal["relevance", "price_asc", "price_desc"]


@mcp.tool()
async def rozetka_search(
    query: str,
    min_price: float | None = None,
    max_price: float | None = None,
    sort: Literal["relevance", "price_asc", "price_desc", "popular", "new", "rating"] = "relevance",
    page: int = 1,
    limit: int = 20,
    in_stock_only: bool = True,
) -> dict:
    """Search rozetka.com.ua. Returns title, price (UAH), stock status, seller, link.
    Rozetka pages have 60 items; `limit` (max 60) caps how many are returned."""
    return await rozetka_search_impl(query, min_price, max_price, sort, page, limit, in_stock_only)


@mcp.tool()
async def rozetka_product(product_id: int) -> dict:
    """Full Rozetka product: price, stock, seller (name, rating, phone, url), specs."""
    return await rozetka_product_impl(product_id)


@mcp.tool()
async def prom_search(
    query: str,
    min_price: float | None = None,
    max_price: float | None = None,
    sort: Sort = "relevance",
    page: int = 1,
    limit: int = 29,
    in_stock_only: bool = True,
    wholesale_only: bool = False,
    with_specs: bool = False,
) -> dict:
    """Search prom.ua. Returns title, price, unit, stock status, wholesale prices (if any),
    seller (name, region, rating), link. ~29 items per page; use `page` for more.
    with_specs=True also opens each result's page and adds `specs` (seller-filled
    characteristics, e.g. capacity, power, ports, color); slower, so use a
    small `limit`. Results with no specs get a `description_excerpt` instead."""
    return await prom_search_impl(
        query, min_price, max_price, sort, page, limit, in_stock_only, wholesale_only, with_specs
    )


@mcp.tool()
async def prom_product(product: str) -> dict:
    """Full Prom product by id or URL: stock, specs (seller-filled characteristics),
    brand, category, description, wholesale tiers (price + minimum quantity), seller min order sum
    and contacts where shown."""
    return await prom_product_impl(product)


async def _safe(coro) -> dict:
    try:
        return await coro
    except Exception as e:  # report per-site failure, keep the other site's results
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def search_both(
    query: str,
    min_price: float | None = None,
    max_price: float | None = None,
    sort: Sort = "relevance",
    limit_per_site: int = 15,
    in_stock_only: bool = True,
    with_specs: bool = False,
) -> dict:
    """Search Rozetka and Prom.ua in parallel for one item.
    with_specs=True adds `specs` to Prom results (one extra page fetch per result)."""
    rz, pr = await asyncio.gather(
        _safe(rozetka_search_impl(query, min_price, max_price, sort, 1, limit_per_site, in_stock_only)),
        _safe(
            prom_search_impl(
                query, min_price, max_price, sort, 1, limit_per_site, in_stock_only,
                with_specs=with_specs,
            )
        ),
    )
    return {"query": query, "rozetka": rz, "prom": pr}


@mcp.tool()
async def bulk_search(
    items: list[str],
    per_site: int = 5,
    sort: Sort = "relevance",
    in_stock_only: bool = True,
) -> list[dict]:
    """Search a whole shopping list at once (e.g. ["папір A4 80г", "картридж HP 85A"]).
    Returns the top `per_site` in-stock offers from each site per item."""
    async def one(q: str) -> dict:
        return await search_both(q, None, None, sort, per_site, in_stock_only)

    return list(await asyncio.gather(*(one(q) for q in items[:30])))


if __name__ == "__main__":
    mcp.run()
