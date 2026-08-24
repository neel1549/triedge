from inventory import Inventory


def test_reserve_in_stock():
    inv = Inventory({"widget": 5})
    assert inv.reserve("widget", 3) is True


def test_reserve_unknown_sku_returns_false():
    inv = Inventory({"widget": 5})
    # Reserving a SKU that isn't stocked should return False, not raise.
    assert inv.reserve("gadget", 1) is False
