"""Dashboard entry point:  uv run streamlit run app/dashboard/streamlit_app.py

Daily research system and paper-trading monitor. Not a live-trading terminal.
"""

from collections.abc import Callable

import streamlit as st

from app.core.config import get_settings
from app.core.safety import assert_safe_trading_mode
from app.dashboard.config import SYSTEM_LABEL
from app.dashboard.ui.context import Context, build_context
from app.dashboard.ui.pages import PAGES

st.set_page_config(
    page_title=f"SPY — {SYSTEM_LABEL}", layout="wide", initial_sidebar_state="expanded"
)
assert_safe_trading_mode(get_settings())  # real-money execution stays blocked


def _page(render: Callable[[Context], None]) -> Callable[[], None]:
    def run() -> None:
        render(build_context())

    run.__name__ = render.__name__
    return run


nav = st.navigation(
    {
        "Market": [st.Page(_page(f), title=t, url_path=f.__name__) for t, f in PAGES[:4]],
        "Research": [st.Page(_page(f), title=t, url_path=f.__name__) for t, f in PAGES[4:9]],
        "Paper & System": [st.Page(_page(f), title=t, url_path=f.__name__) for t, f in PAGES[9:]],
    }
)
nav.run()
