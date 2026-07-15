"""Fixtures for the C4 scheduler tests.

This suite is pure-function testing, so it currently declares no fixtures. All
builders and fakes live in ``scheduler_helpers`` — import them from there, never
from ``conftest`` (pytest imports each ``conftest.py`` under a private internal
name, so ``from conftest import X`` breaks when test trees are mixed).
"""
