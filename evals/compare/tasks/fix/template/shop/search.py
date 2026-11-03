"""Product search with a simple relevance score."""

from shop.textutil import slugify


def score(product, query):
    q = query.lower().strip()
    if not q:
        return 0
    points = 0
    if product.sku.lower() == q:
        points += 100
    if product.name.lower() == q:
        points += 80
    if product.name.lower().startswith(q):
        points += 40
    if q in product.name.lower():
        points += 20
    if q in slugify(product.category):
        points += 5
    return points


def search(catalog, query, limit=10):
    scored = [(score(p, query), p) for p in catalog.all()]
    hits = [(s, p) for s, p in scored if s > 0]
    hits.sort(key=lambda sp: (-sp[0], sp[1].sku))
    return [p for _, p in hits[:limit]]
