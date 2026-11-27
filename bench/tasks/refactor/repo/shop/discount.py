"""Discounts, applied before tax."""

from .cart import calc_total


def with_discount(lines, percent):
    """The total in cents after taking percent off the net amount; tax is on what is left."""
    net = sum(price * qty for _, price, qty in lines)
    return round(net * (100 - percent) / 100 * (1 + 0.2))


def saving(lines, percent):
    return calc_total(lines) - with_discount(lines, percent)
