"""Product reviews and rating summaries."""

from dataclasses import dataclass

from shop import money, validate


@dataclass(frozen=True)
class Review:
    sku: str
    customer_id: str
    stars: int
    text: str
    helpful: int = 0


class ReviewBook:
    def __init__(self):
        self._reviews = []

    def add(self, sku, customer_id, stars, text=""):
        if stars not in (1, 2, 3, 4, 5):
            raise validate.ValidationError("stars must be 1..5")
        for r in self._reviews:
            if r.sku == sku and r.customer_id == customer_id:
                raise validate.ValidationError("%s already reviewed %s" % (customer_id, sku))
        review = Review(sku, customer_id, stars, text)
        self._reviews.append(review)
        return review

    def for_sku(self, sku):
        return [r for r in self._reviews if r.sku == sku]

    def average_tenths(self, sku):
        """Average rating in tenths of a star (4.25 stars is 43), halves rounded up."""
        stars = [r.stars for r in self.for_sku(sku)]
        if not stars:
            return None
        return money.round_div(sum(stars) * 10, len(stars))

    def histogram(self, sku):
        counts = {n: 0 for n in range(1, 6)}
        for r in self.for_sku(sku):
            counts[r.stars] += 1
        return counts

    def best_rated(self, minimum_reviews):
        skus = sorted({r.sku for r in self._reviews})
        rated = [(self.average_tenths(s), s) for s in skus if len(self.for_sku(s)) >= minimum_reviews]
        return [s for _, s in sorted(rated, key=lambda t: (-t[0], t[1]))]
