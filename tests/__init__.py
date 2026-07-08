"""Smoke tests that don't require a running Qt event loop.

We only validate that the package imports cleanly and that the ruyi facade
can be wired to a stub logger. The full GUI flow needs a display + the ruyi
metadata repo and is therefore covered by manual testing.
"""
