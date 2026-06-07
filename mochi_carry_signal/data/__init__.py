"""Data layer: a MINIMAL Hyperliquid funding fetcher + spot-availability check.

Vendored (a small copy, not a dependency) from the backtester's
``mochi_carry_backtester/data/hyperliquid.py`` so this service stays light. The
single HTTP seam is ``_post`` — tests monkeypatch it to run offline.
"""
