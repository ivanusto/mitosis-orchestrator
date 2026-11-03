The Python package in `shop/` takes an order from basket to invoice, shipment and return. Running `python3 -m unittest discover -s tests` from this directory currently reports failures.

Find the root causes and fix them in the package source, not by special-casing results at the point where the failures show up. Do not edit anything under `tests/`. When the whole suite passes, stop and say in a few lines what was wrong and what you changed.
