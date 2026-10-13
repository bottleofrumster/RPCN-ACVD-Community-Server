"""Public tests need no game installation; native visual evidence is opt-in."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-native-assets",
        action="store_true",
        help="include visual tests requiring retained game screenshots",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-native-assets"):
        return
    marker = pytest.mark.skip(reason="requires retained game assets; use --run-native-assets")
    for item in items:
        if "native_assets" in item.keywords:
            item.add_marker(marker)
