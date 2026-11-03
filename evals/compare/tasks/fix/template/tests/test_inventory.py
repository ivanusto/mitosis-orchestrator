import unittest

from shop.errors import InvalidState, OutOfStock, ValidationError
from shop.inventory import Inventory


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()
        self.inv.receive("A", 10)

    def test_reserve_and_available(self):
        self.inv.reserve("A", 4)
        self.assertEqual((self.inv.on_hand("A"), self.inv.reserved("A"), self.inv.available("A")), (10, 4, 6))

    def test_cannot_over_reserve(self):
        self.inv.reserve("A", 8)
        with self.assertRaises(OutOfStock) as ctx:
            self.inv.reserve("A", 3)
        self.assertEqual(ctx.exception.available, 2)

    def test_release_and_commit(self):
        self.inv.reserve("A", 5)
        self.inv.release("A", 2)
        self.assertEqual(self.inv.reserved("A"), 3)
        self.inv.commit("A", 3)
        self.assertEqual(self.inv.snapshot()["A"], (7, 0))
        with self.assertRaises(InvalidState):
            self.inv.release("A", 1)
        with self.assertRaises(InvalidState):
            self.inv.commit("A", 1)

    def test_restock_and_low_stock(self):
        self.inv.receive("B", 2)
        self.inv.restock("B", 1)
        self.assertEqual(self.inv.on_hand("B"), 3)
        self.assertEqual(self.inv.low_stock(5), ["B"])

    def test_quantities_must_be_positive(self):
        with self.assertRaises(ValidationError):
            self.inv.reserve("A", 0)


if __name__ == "__main__":
    unittest.main()
