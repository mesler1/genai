# The tests must pass unchanged, and a few cases the model never saw must too.
set -e
cmp tests/test_durations.py "$BENCH/repo/tests/test_durations.py" || { echo "the tests were changed"; exit 1; }
python3 -m unittest discover -s tests
python3 - <<'PY'
import durations
assert durations.parse("1d1h1m1s") == 90061
assert durations.parse("100m") == 6000
for bad in ("1h 2", "m5", "1hh", "1h-1m", "\t"):
    try:
        durations.parse(bad)
    except ValueError:
        continue
    raise SystemExit(f"accepted {bad!r}")
print("hidden cases pass")
PY
