# Implementation Status

## Current scope

CivicPilot AI is a local React and FastAPI application. Authentication, private user documents, admin-managed knowledge documents, citations, and local application drafts use the existing SQLite database. The app does not fetch from government sites or submit government applications.

## Implemented

| Area | Status | Notes |
|---|---|---|
| Authentication and roles | Implemented | Sign-up always creates `user`; password hashes use PBKDF2; signed sessions are checked against SQLite on each request; `/profile` and `/auth/me` return the database role. The frontend stores only the bearer token and resolves the role from `/auth/me` on login, reload, and focus. |
| Dashboard routing | Implemented | Admin accounts route to `/admin`; normal users route to `/dashboard`; route guards and backend dependencies enforce the same roles. |
| Admin accounts | Implemented | Local CLI can create or promote the configured admin without deleting profiles, uploads, or applications. Admins cannot deactivate/demote themselves or the final active admin. |
| User documents | Implemented | Private uploads remain owner-scoped; allowed file types and size are checked. Users cannot access another user's files. |
| Legal knowledge repository | Implemented | Admin-only upload, list, preview, download, reprocess, metadata edit, citation-location correction, and delete APIs. Passage text remains extracted from the stored source. |
| Supplied reference PDFs | Imported | PM Vishwakarma PIB article (16 September 2025), revised PM-KISAN operational guidelines (29 March 2020), and the uploaded Scholarship Eligibility Check document are stored as shared knowledge documents with extracted page chunks. |
| Assistant and citations | Implemented | Retrieval queries only active, indexed knowledge documents; citation passages come from database chunks. Generated citation numbers are validated against retrieved passages. No fictional sample documents are used. |
| Demo content cleanup | Implemented | Legacy sample documents/chunks are removed from the active database and retrieval. Unreferenced fictional schemes are removed; schemes needed by historical applications/bookmarks/checks are archived and made inactive to preserve foreign-key-backed records. |
| Scheme catalogue and eligibility | Restricted | No verified schemes are currently catalogued. Fictional scheme CRUD and seeded records are removed; catalogue and eligibility endpoints do not return legacy demo records. Existing application records remain available. |
| Audit history | Implemented | Admin actions and document imports/edits are recorded and admin-only audit APIs require backend role verification. |
| Government sources | Read-only | Candidates remain unverified and disabled. Refresh and mutation operations make no network requests. |
| Gemini | Optional | If configured, retrieved passages are sent to Gemini. Otherwise the assistant returns matching source passages directly. |

## Verification

The latest backend run passed **32 tests with 1 skipped**. The frontend suite passed **9 tests**, and the production build passed. Tests cover public role assignment, server-verified route decisions, repository API permissions for normal/admin tokens, source-only citations, historical record preservation, and the legacy demo cleanup migration.

## Limitations

- The catalogue currently has no active verified schemes, so new scheme eligibility checks and application drafts are unavailable until a verified scheme source is curated.
- Scanned PDFs may need Tesseract for OCR. The supplied PDFs had extractable text and were indexed locally.
- The application remains a local build; production deployment, distributed session storage, and broader security/accessibility review are not included.
- Government portals and source candidates are not contacted. Eligibility output is not an official determination.
