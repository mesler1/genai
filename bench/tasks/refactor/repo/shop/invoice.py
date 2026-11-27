"""Invoice text for a cart."""

from .cart import calc_total


def tax(lines):
    net = sum(price * qty for _, price, qty in lines)
    return round(net * 0.2)


def invoice(lines):
    rows = [f"{name:<20}{qty:>4} x {price / 100:>8.2f}" for name, price, qty in lines]
    rows.append(f"{'tax (20%)':<20}{tax(lines) / 100:>17.2f}")
    rows.append(f"{'total':<20}{calc_total(lines) / 100:>17.2f}")
    return "\n".join(rows)
