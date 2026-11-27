# Same behaviour, the new name only, and the rate written out once, in shop/config.py.
set -e
if grep -rn "calc_total" --include=*.py . ; then echo "calc_total is still there"; exit 1; fi
python3 -m unittest discover -s tests
test "$(grep -rln --include=*.py -E '(^|[^0-9.])0\.2([^0-9]|$)' shop)" = "shop/config.py" || {
    echo "0.2 should be written only in shop/config.py:"; grep -rn --include=*.py -E '(^|[^0-9.])0\.2([^0-9]|$)' shop; exit 1; }
python3 - <<'PY'
from shop import cart, config, discount, invoice
assert config.TAX_RATE == 0.2
lines = [("apple", 50, 4), ("bread", 325, 1), ("tea", 199, 3)]
assert cart.order_total(lines) == 1346, cart.order_total(lines)
assert discount.with_discount(lines, 25) == 1010, discount.with_discount(lines, 25)
assert discount.saving(lines, 25) == 336
assert invoice.invoice(lines).rstrip().endswith("13.46")
print("hidden cases pass")
PY
