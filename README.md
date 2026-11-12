# ua-shop MCP — Rozetka + Prom.ua search for Claude Code

Read-only MCP server to find goods, prices, stock and sellers on **rozetka.com.ua** and **prom.ua**.
No API keys and no browser: it calls the same JSON endpoints and pages the sites' own frontends load.

## Tools

| Tool | What it returns |
|---|---|
| `bulk_search(items[], per_site=5)` | A whole shopping list at once: top in-stock offers per item from both sites |
| `search_both(query, …, with_specs)` | One item, both sites in parallel |
| `rozetka_search(query, min_price, max_price, sort, page, limit, in_stock_only)` | Title, price, stock status, seller + rating, link |
| `rozetka_product(product_id)` | Plus seller phone, category, full specs |
| `prom_search(query, …, wholesale_only, with_specs)` | Title, price, unit, stock, wholesale prices, seller (region, % positive), link; with `with_specs=True` also each result's specs |
| `prom_product(id_or_url)` | Specs, brand, category, description, wholesale tiers (**price per minimum quantity**), seller phones, min order sum |

`sort`: `relevance` (default), `price_asc`, `price_desc` (Rozetka also `popular`, `new`, `rating`).
`with_specs=True` (Prom only): search pages carry no characteristics, so the server opens
each result's product page and adds `specs` (seller-filled: capacity, power, ports, color…).
If a seller filled none, a `description_excerpt` is added instead. It costs one request per
result, so keep `limit` small (≈5–10).

Stock is a status (в наявності / закінчується / під замовлення), not an exact count;
confirm bulk quantities with the seller.

## Install (Claude Code on your computer)

Needs [uv](https://docs.astral.sh/uv/) (it installs Python deps automatically):

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Put `server.py` somewhere permanent, then register it for all your projects:

```bash
claude mcp add ua-shop -s user -- uv run --quiet --script /full/path/to/server.py
```

Check: `claude mcp list` should show `ua-shop … ✓ Connected`.
Alternatively, start `claude` inside this folder: the included `.mcp.json` registers it for this project.

## Example prompts

> Use bulk_search for: папір A4 80г 500 арк, картридж HP 85A, кава в зернах 1 кг.
> Table per item: site, price, stock, seller, link. Cheapest 3 per item.

> Find офісні крісла up to 4000 UAH on Prom with wholesale prices, then get wholesale
> tiers and seller phones for the top 5.

> Find a white 10000 mAh MagSafe power bank on Prom with with_specs, limit 8, and keep only
> those whose specs say 22.5 W and USB-C.

## Notes

- Polite by default: max 8 parallel requests per call. Don't hammer the sites. Automated
  access may conflict with the sites' terms, so keep usage moderate.
- Endpoints are undocumented and can change. If a tool starts returning errors,
  the site changed its layout or API.
