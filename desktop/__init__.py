"""PumpFun PAPER desktop window (variant A): a WebView2 window around the existing terminal.

The core modules of this package (constants, paths, release_check, engine_client, controller, actions,
logs, single_instance, session_end, settings_store) import no GUI library, so they run and are tested in
the bot's own .venv. The window never owns the campaign: the engine host runs detached and is stopped
only through its stop file (docs/DESKTOP_APP_SPEC_RU.md)."""
