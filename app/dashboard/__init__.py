"""Phase 13 research & paper-trading dashboard (Streamlit).

A DAILY RESEARCH SYSTEM and monitoring interface, not a live-trading terminal. Layers:

    domain / research engines (app.*)  ->  app.dashboard.services (pure, tested)
                                       ->  app.dashboard.ui (thin Streamlit pages)

The dashboard reads the local database and local files only. It never fetches market data,
never connects to a broker and has no real-money execution capability; its only mutating
actions are LOCAL PAPER SIMULATION actions delegated to `app.paper`.
"""
