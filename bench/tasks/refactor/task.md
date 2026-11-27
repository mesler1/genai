# Rename a function and give the tax rate one home

In the shop package, rename calc_total to order_total everywhere it is defined or used, tests included.
The tax rate 0.2 is written out in three modules: put it in one place, shop/config.py as TAX_RATE,
and use that everywhere. Behaviour must not change. Run the tests with: python3 -m unittest discover -s tests
