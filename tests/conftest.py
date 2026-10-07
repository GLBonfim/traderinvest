from collections.abc import Iterator

import pytest

from app.core.config import get_settings
from app.database.session import get_engine, get_session_factory


@pytest.fixture(autouse=True)
def _clear_cached_singletons() -> Iterator[None]:
    """Settings/engine are cached per process; isolate every test."""
    for fn in (get_settings, get_engine, get_session_factory):
        fn.cache_clear()
    yield
    for fn in (get_settings, get_engine, get_session_factory):
        fn.cache_clear()
