"""Shared test scaffolding (factories, base test cases) used by every app's
own `tests/` package.

Deliberately one shared package rather than a `testing.py` per app: the
models are heavily cross-app (a Product needs a Supplier, a StockMovement
needs an InvoiceLine, a Recipe needs a StockType), so per-app factory
modules would end up importing each other anyway.
"""
