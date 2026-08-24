"""A small inventory module for the Triedge examples.

`reserve` has a deliberate bug: it does not guard against unknown SKUs, so the
test triggers a KeyError. Another "real code fix is plausible" scenario.
"""


class Inventory:
    def __init__(self, stock=None):
        self._stock = dict(stock or {})

    def add_stock(self, sku, quantity):
        self._stock[sku] = self._stock.get(sku, 0) + quantity

    def reserve(self, sku, quantity):
        # BUG: no membership check; unknown SKUs raise KeyError instead of
        # returning False. `reserve` should handle missing SKUs gracefully.
        if self._stock[sku] >= quantity:
            self._stock[sku] -= quantity
            return True
        return False
