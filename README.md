# CivicPilot AI

CivicPilot AI is an independent local application for exploring public-service information and preparing application drafts. It is not a government website or government-approved service. The active knowledge repository contains the three supplied reference PDFs. Fictional schemes and sample documents have been removed from the active catalogue and assistant index.

## What works

- Responsive React, TypeScript, Vite dashboard with English, Kannada, and Hindi navigation labels.
- FastAPI API with SQLite accounts, sessions, profiles, roles, audit events, and an optional admin account.
- Role-verified authentication: public sign-up always creates a `user`, and the server reloads the account role for each session. Admin and user sign-in use the same page and are routed to their respective dashboards after `/auth/me` confirms the role.
- Private local document uploads (PDF/JPEG/PNG), text extraction where supported, OCR hooks, extracted field suggestions, and admin knowledge-document endpoints. User uploads are owner-scoped and stored under `backend/data/private_uploads` by default.
- Assistant retrieval over administrator-curated knowledge documents only. With no Gemini key, matching excerpts are returned without inventing a generated answer. Gemini generation is optional and restricted to retrieved passages. Citation text always comes from extracted PDF chunks; admins may correct page/section location metadata without changing source text.
- Application drafts, status history, PDF export, and a local simulated receipt flow.
- Role-protected Admin Dashboard with user and document counts, user role and activation management, legal knowledge upload/extraction/metadata editing, citation-location correction, and audit history. Fictional scheme management has been removed.
- Browser session persistence stores the bearer token only; account role is never read from local storage. Login, reload, and focus refresh the role from the authenticated backend session.
- The knowledge repository includes the supplied PM Vishwakarma information article (PIB, 16 September 2025), PM-KISAN operational guidelines (Ministry of Agriculture & Farmers' Welfare, 29 March 2020), and the uploaded Scholarship Eligibility Check (National Scholarship Portal source label). PDF text and page chunks are stored locally.
- Government source registry with four candidates. All candidates remain unverified and disabled. Public refresh routes return without making a network request; the old connector is usable only by mocked tests. No government website, login, API, CAPTCHA, OTP, or submission is contacted.

## Run locally

Requirements: Python 3.11+, Node.js 20+, and npm. For OCR, install the Tesseract executable separately and make it available on `PATH`. For optional Gemini answers, configure a Gemini API key. No scheme catalogue is seeded.

From the repository root, open two terminals.

### Backend (PowerShell)

```powershell
# Run from the repository root.
python -m venv backend\.venv
.\backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m pip install -r backend\requirements-dev.txt  # needed to run the backend test suite
Copy-Item backend\.env.example backend\.env
```

`backend/pyproject.toml` sets the project name/version, the Python `>=3.11` requirement, and pytest configuration; runtime dependencies are listed in `backend/requirements.txt`, with pytest isolated in `backend/requirements-dev.txt`.

Edit `backend/.env` before starting if needed. Set `APP_SECRET_KEY` to a private random value, `ADMIN_EMAIL` and `ADMIN_PASSWORD` to configure the admin account, and optionally `GEMINI_API_KEY`. From the repository root, then run:

```powershell
cd backend
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API docs are at `http://localhost:8000/docs`; health is at `http://localhost:8000/api/v1/health`. In development, the configured frontend origin and its `localhost`/`127.0.0.1` loopback alias are accepted. Production accepts only the configured origin. Stop and restart an older backend process after updating the code so it loads the new routes.

### Admin account setup

Keep admin credentials only in the ignored local file `backend/.env`; do not put them in `.env.example`, source code, or a commit. Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` there (10–128 characters under the current password policy), then run this one-time command from the `backend` directory:

```powershell
.\.venv\Scripts\python.exe -m app.cli bootstrap-admin
```

The command creates the account if it does not exist, or promotes the matching active account and updates its password hash in place. It revokes that account's old sessions; profiles, documents, and application records are preserved. It does not print the password. Restart the backend, sign in again, and the Admin navigation item will appear for the admin role. A browser tab with a revoked session now clears its stale authentication state on the next 401 response and asks the user to sign in again. Normal users receive HTTP 403 from admin APIs. `ADMIN_EMAIL` and `ADMIN_PASSWORD` are read from `backend/.env` or process environment variables; the file path is resolved from the backend source location, regardless of the shell's working directory.

After placing the supplied PDFs on disk, an administrator can import or refresh their repository records with this command (run from `backend`):

```powershell
.\.venv\Scripts\python.exe -m app.cli import-reference-pdfs --pm-vishwakarma "C:\path\pm vishwakarma.pdf" --pm-kisan "C:\path\RevisedPM-KISANOperationalGuidelines(English).pdf" --scholarship "C:\path\Scholarship Eligibility Check.pdf"
```

The command requires an active admin account matching `ADMIN_EMAIL`, stores files in the private backend upload directory, processes PDFs locally, and is safe to rerun. The third document's source URL is not inferred; its source label records that it was uploaded as NSP eligibility information.

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Set `VITE_API_BASE_URL` in `frontend/.env` only if the backend is hosted elsewhere. Set `FRONTEND_ORIGIN` on the backend to the exact frontend origin when changing its port.

## Main API routes

- `/api/v1/auth/*`, `/api/v1/profile`
- `/api/v1/dashboard/summary`, `/api/v1/schemes`, `/api/v1/eligibility/*`
- `/api/v1/documents`, `/api/v1/assistant/*`, `/api/v1/applications/*`
- `/api/v1/admin/*` for admin-role content and account management
- `/api/v1/sources` for read-only source candidate status

Admin overview and management endpoints include `/api/v1/admin/overview`, `/users`, `/users/{id}`, `/users/{id}/role`, `/users/{id}/active`, `/knowledge-documents`, `/knowledge-documents/{id}/passages/{chunk_id}`, `/processing-jobs`, and `/audit-logs`. Legal repository uploads accept PDF, plain text, or HTML and are processed locally. Every repository administration endpoint requires an admin role; normal users receive HTTP 403. Users can view curated public document details and answers/citations, while private uploads remain owner-scoped. Role changes revoke active sessions; an administrator cannot remove their own access or the last active admin.

All write routes except registration/login require a bearer session. Admin routes require an admin role verified by the backend. Government source controls are read-only; source mutation and refresh endpoints return HTTP 410 for admins and do not make network requests.

## Data and limitations

SQLite defaults to `backend/data/civicpilot.db`; local document files are under `backend/data/private_uploads`. Keep the database and upload directory private and back them up together. An additive migration removes legacy sample documents and fictional schemes from active retrieval/catalogues. It preserves users, user-owned uploads, audit history, and application records; a scheme referenced by historical records is retained in an inactive archived form. This build has no verified scheme records, official eligibility decisions, or real government application portal. OCR requires system Tesseract; scanned document processing reports unavailable when it is missing. Gemini is optional, sends retrieved snippets to Google's service when configured, and can fail independently.

## Verify

```powershell
cd frontend
npm test
npm run build
cd ..
backend\.venv\Scripts\python.exe -m pytest -q backend
```

Tests use mocked source responses. The optional live government-source test is deliberately skipped/disabled to preserve the local-only access policy.
