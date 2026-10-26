# MULEGUARD AI backend

FastAPI + SQLite. On first start, an empty database is seeded from `data/soa_transactions.csv.gz` (201,783 rows across eight uploaded SOA workbooks). Dataset assumptions and source-file row counts are documented in `data/manifest.json`.

Run: `python -m venv .venv`, activate it, `pip install -r requirements.txt`, then `uvicorn app:app --reload --port 8000`. API docs: `http://127.0.0.1:8000/docs`.

Demo login for local use: `admin@muleguard.local` / `ChangeMe123!`. Set `MULEGUARD_ADMIN_PASSWORD` before first database creation to change the initial password.

Important data limitation: `AC_Name` is constant within each workbook while `Ac_No` varies row-by-row. The app infers holder/counterparty direction from `Dr_Amt` and `Cr_Amt` to make graphing possible. Those edges are not explicitly supplied transfer endpoints and must be validated. Currency is displayed as INR provisionally because `Crncy` is not a standard currency code.

Key API groups: `/api/auth`, `/api/dashboard`, `/api/accounts`, `/api/transactions`, `/api/complaints`, `/api/detection`, `/api/graph`, `/api/cases`, `/api/alerts`, `/api/audit-logs`, `/api/demo`, `/api/reports/generate`; dataset metadata at `/api/dataset/info`.
