# MULEGUARD AI frontend

The supplied single-file design is preserved in `index.html`. `integration.js` connects dashboard counters, transaction rows, login, CSV import, detection actions, case creation, and PDF export to the FastAPI backend. `config.js` contains the local API base URL.

Serve this folder over HTTP (do not double-click `index.html`): `python -m http.server 5500`, then open `http://127.0.0.1:5500`. Start the backend separately on port 8000. The SOA dataset is bundled and loads into a new empty backend database automatically.

Some secondary visual sections remain prototype surfaces and are not fully API-driven. The dataset's sender/receiver edges are inferred from debit/credit and statement-holder fields; read the root README before interpreting network/risk results.
