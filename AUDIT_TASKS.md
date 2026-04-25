# Ascenda Audit — Fix Tasks

Each task below is self-contained. Start a fresh session, tell Claude "fix [ID]", and it has everything needed.

---

## C1 — Incomplete `app_sessions` DDL in `auth.py`

**File:** `auth.py`, function `_ensure_sessions_table_exists()`

**Problem:**  
The `CREATE TABLE IF NOT EXISTS app_sessions` statement only creates 4 columns:
`session_id`, `user_id`, `created_at_utc`, `expires_at_utc`

The full schema in `init_db_v11.py` defines additional columns used by the rest of the codebase:
`revoked_at_utc`, `closed_reason`, `last_seen_utc`, `ip`, `user_agent`

On a fresh DB or after a schema reset, any query that touches those missing columns raises a runtime crash (`psycopg.errors.UndefinedColumn`). Logout (`revoke_session`) hits `revoked_at_utc` — so login/logout fails on a fresh install.

**Fix:** Sync the DDL inside `_ensure_sessions_table_exists()` with the full 9-column schema from `init_db_v11.py`. Do not change anything else.

---

## C2 — XSS via unescaped notes field in My Visits

**File:** `app_pages/my_submissions.py`

**Problem:**  
Visit notes are injected directly into an HTML string without escaping:
```python
f'<p style="...;line-height:1.6;margin:0;">{notes}</p>'
```
This string is passed to `st.markdown(..., unsafe_allow_html=True)`. A rep who submits `<script>alert(1)</script>` in notes will have that execute in any viewer's browser.

The fix pattern already exists in `dashboard.py` and `review_audiences.py` — those files call `html.escape()` on all user-generated strings before HTML interpolation.

**Fix:** Wrap `notes` (and any other raw DB string interpolated into HTML in that file) with `html.escape()`. Follow the existing pattern from `dashboard.py`. Do not change anything else.

---

## H1 — Schema drift between `init_db_v11.py` and `auth.py`

**File:** `init_db_v11.py` and `auth.py`

**Problem:**  
There are two DDL sources of truth for `app_sessions`. `init_db_v11.py` is the authoritative schema file. `auth.py` has its own `CREATE TABLE IF NOT EXISTS` that is incomplete (see C1). After fixing C1, this issue is about the process: nothing enforces that the two stay in sync.

Additionally, `init_db_v11.py` includes:
- `'manager'` in the role check constraint — the UI only ever assigns `'sales manager'` and `'biomedical manager'`
- `'IDK'` in the evaluation check constraint — never assigned by application code

These dead constraint values signal drift between schema and application.

**Fix:**
1. Add a comment at the top of `_ensure_sessions_table_exists()` in `auth.py` that says this DDL must be kept in sync with `init_db_v11.py` and lists the authoritative file.
2. Remove `'manager'` from the role check constraint in `init_db_v11.py` if it is truly unused (verify in `auth.py` and `admin_users.py` first).
3. Remove `'IDK'` from the evaluation check constraint in `init_db_v11.py` if it is truly unused (verify in `submit_visit.py` first).
Do not change anything else.

---

## H2 — DB helpers defined inside `page_change_request()` on every render

**Files:** `app_pages/change_request.py`, `app_pages/admin_change_requests.py`

**Problem:**  
`page_change_request()` is ~1200 lines. All DB helper functions (`_load_visit_for_change_request`, `_insert_request_and_details`, `_can_withdraw`, `_do_withdraw`, etc.) are defined as nested functions inside the page function. Streamlit reruns the full page function on every widget interaction, re-defining all those closures every time.

Additionally, several of these helper functions are duplicated in `admin_change_requests.py` — any bug fix must be applied in two places.

**Fix:**
1. Move all DB helper functions out of `page_change_request()` to module scope in `change_request.py`.
2. Identify functions that are identical (or near-identical) in both `change_request.py` and `admin_change_requests.py`. Extract the shared ones into a new file `app_pages/change_request_helpers.py` and import from both pages.
3. Remove the duplicate `import` statements that appear inside the `page_change_request()` function body (they already exist at the top of the file).
Do not change UI logic or SQL queries.

---

## H4 — No minimum password length on user creation

**File:** `app_pages/admin_users.py`

**Problem:**  
The user creation form accepts any non-empty string as a valid password. There is no server-side length check. A 1-character password like `a` passes validation and is hashed/stored. The `.env` file demonstrates this is used in practice (`ADMIN_PASSWORD=a`).

**Fix:** Add a server-side check in the user creation handler (before calling `pbkdf2_sha256.hash()`) that rejects passwords shorter than 8 characters and shows a clear `st.error` message. Do not add complexity requirements — length only. Do not change anything else.

---

## M1 — `top_nav_bar()` defined in `ui.py` but never called

**File:** `ui.py`

**Problem:**  
`top_nav_bar()` is a ~30-line HTML rendering function defined in `ui.py`. It is not imported or called anywhere in the codebase (`app_v11.py`, any `app_pages/` file, or `widgets.py`). It is dead code.

**Fix:** Verify no call sites exist (grep for `top_nav_bar`), then delete the function from `ui.py`. Do not change anything else.

---

## M2 — `kpi_card()` v1 superseded by `kpi_card_v2()`, never called

**File:** `ui.py`

**Problem:**  
Both `kpi_card()` and `kpi_card_v2()` are defined in `ui.py`. All call sites use `kpi_card_v2()`. The original `kpi_card()` is unreferenced dead code.

**Fix:** Verify no call sites exist for `kpi_card` (grep for `kpi_card(` excluding `kpi_card_v2(`), then delete `kpi_card()` from `ui.py`. Do not change anything else.

---

## M3 — Dead `_sid`-in-URL branch in `my_submissions.py`

**File:** `app_pages/my_submissions.py`

**Problem:**  
After login, the SID is stripped from query params and stored only in `sessionStorage`. So:
```python
_sid = st.query_params.get("sid", "")
```
…is always `""` at this point. The branch that builds `href = f"?page=My+Visits&sid={_sid}&visit_id=..."` is never taken. This misleads future maintainers into thinking SID can appear in the URL.

**Fix:** Remove the dead `_sid` variable and the conditional `href` construction. Replace with the simpler URL form (the one without `_sid`) unconditionally. Do not change any other navigation logic.

---

## M4 — Unused `streamlit_geolocation` import in `widgets.py`

**File:** `widgets.py`

**Problem:**  
```python
from streamlit_geolocation import streamlit_geolocation
```
This import exists at the top of `widgets.py` but `streamlit_geolocation` is never called. Actual geolocation uses `streamlit_js_eval.get_geolocation`. If `streamlit-geolocation` is not installed, this raises `ImportError` at startup even though the feature works.

**Fix:**
1. Remove the unused import line from `widgets.py`.
2. Check `requirements.txt` — if `streamlit-geolocation` appears there and is not needed anywhere else, remove it from `requirements.txt` too.
Do not change the geolocation logic itself.

---

## M6 — No rate limiting on login attempts

**File:** `ui.py`, `login_block()` function

**Problem:**  
The login form calls `resolve_session_user()` with no attempt counter, lockout, or delay. An attacker can brute-force passwords at the speed of Streamlit reruns. Combined with H4 (weak passwords allowed), this is a real risk on any public-facing deployment.

**Fix:** Add a per-username attempt counter stored in `st.session_state`. After 5 failed attempts for the same username in a session, disable the login form and show a message telling the user to wait or contact admin. Reset the counter on successful login. This is session-scoped (resets on browser close), which is acceptable for an internal tool. Do not add external dependencies (no Redis, no DB table). Do not change anything else.

---

## M7 — Force-adjust audit log missing "before" values

**File:** `app_pages/admin_change_requests.py`, `_apply_force_adjustment()` function

**Problem:**  
When an admin force-adjusts a visit, the audit log records the admin's `user_id`, timestamp, and the new field values. It does not snapshot the **before** values of the changed fields. The history tab shows "changed visit_date to Friday" but not "from Monday." Post-hoc dispute resolution is harder without the before state.

**Fix:** In `_apply_force_adjustment()`, before executing the UPDATE, SELECT the current values of the fields being changed. Store both the old and new values in the audit log entry. Update the history display in the History tab to show "from X → to Y" format. Do not change the whitelist logic or the UPDATE itself.

---

## P4 — Visit card hover CSS in `app_v11.py`, component in `ui.py`

**File:** `app_v11.py` (CSS block) and `ui.py` (`visit_card()` function)

**Problem:**  
The CSS for `.ascenda-visit-card:hover` is defined in the global `st.markdown` block in `app_v11.py`. The HTML that uses the class `ascenda-visit-card` is generated by `visit_card()` in `ui.py`. These are separated — a developer editing `visit_card()` might not know the hover CSS lives in a different file.

**Fix:** Move the `.ascenda-visit-card:hover` CSS rule out of `app_v11.py` and into the CSS injected by or near `visit_card()` in `ui.py` (e.g., inject it once via a helper or include it in `theme.py`). Ensure the CSS is still injected exactly once. Do not change any other CSS.

---

## P5 — Accuracy gate error message doesn't tell rep the required threshold

**File:** `widgets.py`, `get_location_block()` or the accuracy check in `submit_visit.py`

**Problem:**  
When a rep's GPS accuracy exceeds `ACCURACY_METERS=2500`, submission is blocked. The error message shown does not include the required accuracy value — it only says location accuracy is insufficient. Reps don't know what they're aiming for and generate support requests.

**Fix:** Include the `ACCURACY_METERS` constant value in the error message, e.g.:  
`"Location accuracy is {accuracy}m — must be within {ACCURACY_METERS}m. Move to an area with better GPS signal and try again."`  
Import `ACCURACY_METERS` from `config` if not already imported. Do not change the threshold logic itself.

---

## P6 — `purge_expired_sessions()` runs synchronously on every login

**File:** `auth.py`, `purge_expired_sessions()` and `app_v11.py` call site

**Problem:**  
`DELETE FROM app_sessions WHERE expires_at_utc < NOW()` is called inside the login/session-resolve flow on every app load. On a large session table this adds latency to every login. It's also unnecessary to purge on every single request.

**Fix:** Add probabilistic execution — only run the purge roughly 1 in 20 times using `random.randint(1, 20) == 1`. This keeps the table clean without blocking every login. Do not change the purge query itself or the session logic.

---

## Fix Order Reference

Work through issues in this sequence for least risk of regressions:

1. C1 — schema DDL (foundational, fixes crash risk)
2. C2 — XSS escape in notes (security, one-line fix)
3. H1 — schema drift cleanup (follow-on to C1)
4. H4 — password length validation (isolated, UI only)
5. M4 — remove unused import (may affect requirements.txt)
6. M1 — delete dead `top_nav_bar()`
7. M2 — delete dead `kpi_card()` v1
8. M3 — remove dead `_sid` URL branch
9. P6 — probabilistic session purge
10. P5 — accuracy error message
11. P4 — move hover CSS to `ui.py`
12. M6 — login rate limiting
13. M7 — force-adjust before/after audit log
14. H2 — refactor `change_request.py` helpers (largest change, do last)
