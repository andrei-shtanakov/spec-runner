"""Shared pytest configuration for spec-runner tests."""

from __future__ import annotations


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Regenerate golden fixtures under tests/fixtures/",
    )
