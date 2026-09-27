"""Order totals with VAT - demo module for FixFork (contains a planted bug)."""

VAT_RATE = 0.10


def order_total(prices, discount=0.0, vat_rate=VAT_RATE):
    """Total for an order: prices summed, discount applied, then VAT added."""
    subtotal = sum(prices)
    discounted = subtotal * (1 + discount)
    return round(discounted + discounted * vat_rate, 2)
