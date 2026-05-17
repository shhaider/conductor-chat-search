"""conductor_chat — local web GUI for searching and exporting Conductor.app chat sessions.

Reads ~/Library/Application Support/com.conductor.app/conductor.db in read-only mode
and serves a single-page UI on 127.0.0.1 via the Python stdlib HTTP server.
"""

__version__ = "0.1.0"
