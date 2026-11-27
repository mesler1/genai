"""A shopping cart: lines of (name, unit price in cents, quantity)."""


def calc_total(lines):
    """The cart's total in cents, tax included, rounded to the cent."""
    net = sum(price * qty for _, price, qty in lines)
    return round(net * (1 + 0.2))


def add(lines, name, price, qty=1):
    for i, (n, p, q) in enumerate(lines):
        if n == name and p == price:
            lines[i] = (n, p, q + qty)
            return lines
    lines.append((name, price, qty))
    return lines
