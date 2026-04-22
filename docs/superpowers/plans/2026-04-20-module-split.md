# Module Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `app_v11.py` (10,702 lines) into focused modules so each file has one clear responsibility and `app_v11.py` becomes a thin ~60-line orchestrator.

**Architecture:** Extract constants → DB ops → auth → utilities → shared UI widgets → navigation/layout → pages. Each module imports only from modules below it in the dependency chain, so there are zero circular imports.

**Tech Stack:** Python 3.11+, Streamlit ≥1.37, SQLAlchemy 2, psycopg v3, pandas, folium

---

## Dependency Order (bottom → top)

```
db.py          (already exists — engine only)
config.py      (constants, no imports from project)
db_ops.py      (query helpers — imports db, config)
auth.py        (sessions, user lookup — imports db, config, db_ops)
utils.py       (time helpers, PBI push, misc — imports config)
widgets.py     (customer/location widgets — imports db_ops, config, utils)
ui.py          (layout, login, sidebar, footer — imports auth, config, utils)
pages/         (one file per page — imports whatever they need)
app_v11.py     (orchestrator — imports ui, pages)
```

---

## File Map

| File | Responsibility | Key functions |
|------|---------------|---------------|
| `db.py` | Engine (unchanged) | `engine` |
| `config.py` | All constants | `APP_TITLE`, `TIMEZONE`, `SESSION_TTL_MIN`, `DUP_MINUTES`, `PBI_PUSH_URL` |
| `db_ops.py` | DB CRUD helpers | `query_df`, `exec_sql`, `insert_visit_returning_id`, `insert_visit_atomic`, `insert_project`, `recent_visit_minutes` |
| `auth.py` | Sessions + user lookup | `get_user_by_email`, `get_user_by_id`, `create_session`, `delete_session`, `purge_expired_sessions`, `revoke_session`, `resolve_session_user`, `_ensure_sessions_table_exists`, `_log_event`, `_user_agent` |
| `utils.py` | Time, PBI, misc | `_utcnow`, `_utcnow_iso`, `_utcnow`, `_local_now`, `_local_now_str`, `_client_ip`, `push_visit_to_pbi`, `_get_secret`, `_gen_tmp_pw`, `_img_b64` |
| `widgets.py` | Shared UI widgets | `customer_quick_find_module`, `customer_cascading_selectors`, `get_location_block`, `_on_customer_change`, `_on_bu_change`, `_on_line_change`, `_reset_location_state_for_page`, `_reset_geo_on_user_or_page_change`, `set_current_page`, `_acc_str` |
| `ui.py` | Layout, login, nav, footer | `get_logo_base64`, `get_almadar_logo_base64`, `capture_client_fingerprints`, `apply_role_based_layout`, `login_block`, `logout_button`, `sidebar_nav`, `show_footer`, `set_url_param`, `get_url_param`, `set_url_session_param` |
| `pages/submit_visit.py` | Submit Visit page | `page_submit_visit` |
| `pages/check_in.py` | Check-In page | `page_check_in` |
| `pages/my_submissions.py` | My Submissions page | `page_my_submissions` |
| `pages/user_settings.py` | User Settings page | `page_user_settings` |
| `pages/create_project.py` | Create Project page | `page_create_project` |
| `pages/project_view.py` | Projects list + detail | `page_project_view` |
| `pages/project_management.py` | Project management | `page_project_management`, `local_now`, `k`, `_fetch_projects_for_management`, `_fetch_project_row`, `_fetch_project_history`, `_update_project_with_history` |
| `pages/admin_import.py` | Admin import lookups | `page_admin_import` |
| `pages/admin_data.py` | Admin data browser | `page_admin_data` |
| `pages/admin_users.py` | Admin user management | `page_admin_users` |
| `pages/review_audiences.py` | Review target audiences | `page_review_target_audiences` |
| `pages/review_customers.py` | Review other customers | `page_review_other_customers` |
| `pages/change_request.py` | Visit change requests | `page_change_request` |
| `app_v11.py` | Thin orchestrator | `main()` block only |

---

## How to verify each task

After each task: run `python -c "import <module>"` to confirm no import errors. After all tasks: run `streamlit run app_v11.py` and navigate every page.

---

## Task 1: Create `config.py`

**Files:**
- Create: `config.py`

Extract all top-level constants from `app_v11.py` (lines 48–62). `PBI_PUSH_URL` must be computed at import time using the same try/except logic currently in `app_v11.py`.

- [ ] **Step 1: Create `config.py`**

```python
# config.py
import os

APP_TITLE = "Ascenda"
TIMEZONE = "Asia/Riyadh"
SESSION_TTL_MIN = 20
DUP_MINUTES = 15

try:
    import streamlit as st
    PBI_PUSH_URL = os.environ.get("PBI_PUSH_URL") or st.secrets["PBI_PUSH_URL"]
except Exception:
    PBI_PUSH_URL = os.environ.get("PBI_PUSH_URL", "")
```

- [ ] **Step 2: Verify import**

```bash
python -c "from config import APP_TITLE, PBI_PUSH_URL; print(APP_TITLE)"
```
Expected output: `Ascenda`

---

## Task 2: Create `db_ops.py`

**Files:**
- Create: `db_ops.py`
- Source lines in `app_v11.py`: `query_df` (237), `exec_sql` (242), `insert_visit_returning_id` (247), `insert_visit_atomic` (260), `insert_project` (323), `recent_visit_minutes` (663)

- [ ] **Step 1: Create `db_ops.py`**

Copy functions verbatim from `app_v11.py`. All imports they need go at the top of this file:

```python
# db_ops.py
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import engine
from config import DUP_MINUTES
```

Then copy each function body exactly as it appears in `app_v11.py`:
- `query_df` (app_v11.py:237–241)
- `exec_sql` (app_v11.py:242–246)
- `insert_visit_returning_id` (app_v11.py:247–259)
- `insert_visit_atomic` (app_v11.py:260–322)
- `insert_project` (app_v11.py:323–382)
- `recent_visit_minutes` (app_v11.py:663–690)

- [ ] **Step 2: Verify import**

```bash
python -c "from db_ops import query_df, exec_sql, insert_visit_atomic; print('ok')"
```
Expected: `ok`

---

## Task 3: Create `auth.py`

**Files:**
- Create: `auth.py`
- Source lines in `app_v11.py`: lines 420–658

- [ ] **Step 1: Create `auth.py`**

```python
# auth.py
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import streamlit as st
from sqlalchemy import text

from db import engine
from config import SESSION_TTL_MIN
```

Then copy these functions exactly from `app_v11.py`:
- `_user_agent` (424–430)
- `_log_event` (431–450)
- `get_user_by_email` (451–458)
- `get_user_by_id` (459–466)
- `create_session` (467–521)
- `purge_expired_sessions` (522–536)
- `delete_session` (537–540)
- `_ensure_sessions_table_exists` (541–552)
- `set_url_param` (557–562)
- `get_url_param` (563–565)
- `set_url_session_param` (566–571)
- `resolve_session_user` (576–640)
- `revoke_session` (641–658)

- [ ] **Step 2: Verify import**

```bash
python -c "from auth import resolve_session_user, create_session; print('ok')"
```
Expected: `ok`

---

## Task 4: Create `utils.py`

**Files:**
- Create: `utils.py`
- Source lines in `app_v11.py`: lines 227–419 (minus DB helpers already moved)

- [ ] **Step 1: Create `utils.py`**

```python
# utils.py
import base64
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import requests
import streamlit as st
from dateutil import tz

from config import TIMEZONE, PBI_PUSH_URL
```

Then copy these functions exactly from `app_v11.py`:
- `_get_secret` (227–236)
- `_utcnow_iso` (383–385)
- `_utcnow` (386–388)
- `_client_ip` (389–392)
- `_local_now` (393–395)
- `_local_now_str` (396–401)
- `push_visit_to_pbi` (402–419)
- `_gen_tmp_pw` (691–696)
- `_img_b64` (697–699)

- [ ] **Step 2: Verify import**

```bash
python -c "from utils import _utcnow, push_visit_to_pbi, _gen_tmp_pw; print('ok')"
```
Expected: `ok`

---

## Task 5: Create `widgets.py`

**Files:**
- Create: `widgets.py`
- Source lines in `app_v11.py`: lines 700–1117 (callbacks, customer selectors, location block)

- [ ] **Step 1: Create `widgets.py`**

```python
# widgets.py
import math
import time
import unicodedata
from typing import Optional, Tuple

import folium
import streamlit as st
from dateutil import tz
from sqlalchemy import text
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation

try:
    from streamlit_js_eval import get_geolocation as _get_geo_js
except Exception:
    _get_geo_js = None

from db_ops import query_df
from config import TIMEZONE, DUP_MINUTES
from utils import _local_now
```

Then copy these functions exactly from `app_v11.py`:
- `_on_customer_change` (701–703)
- `_on_bu_change` (704–707)
- `_on_line_change` (708–710)
- `_reset_location_state_for_page` (717–723)
- `_reset_geo_on_user_or_page_change` (724–740)
- `set_current_page` (741–748)
- `_acc_str` (1378–1381)
- `get_location_block` (1382–1499)
- `customer_quick_find_module` (753–947)
- `customer_cascading_selectors` (948–1118)

- [ ] **Step 2: Verify import**

```bash
python -c "from widgets import customer_quick_find_module, get_location_block; print('ok')"
```
Expected: `ok`

---

## Task 6: Create `ui.py`

**Files:**
- Create: `ui.py`
- Source lines in `app_v11.py`: lines 131–222 (logo, fingerprints, role layout) + 1119–1367 (login, logout, sidebar)  + 10624–10659 (almadar logo, footer)

- [ ] **Step 1: Create `ui.py`**

```python
# ui.py
import base64
import json
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

from auth import (
    resolve_session_user,
    create_session,
    delete_session,
    revoke_session,
    purge_expired_sessions,
    set_url_param,
    get_url_param,
    set_url_session_param,
)
from config import APP_TITLE, SESSION_TTL_MIN
from utils import _client_ip, _utcnow_iso
```

Then copy these functions exactly from `app_v11.py`:
- `get_logo_base64` (131–148)
- `capture_client_fingerprints` (154–188)
- `apply_role_based_layout` (189–222)
- `login_block` (1122–1182)
- `logout_button` (1183–1197)
- `sidebar_nav` (1198–1367)
- `get_almadar_logo_base64` (10624–10637)
- `show_footer` (10638–10659)

- [ ] **Step 2: Verify import**

```bash
python -c "from ui import login_block, sidebar_nav, show_footer; print('ok')"
```
Expected: `ok`

---

## Task 7: Create `pages/` package and extract `pages/submit_visit.py`

**Files:**
- Create: `pages/__init__.py` (empty)
- Create: `pages/submit_visit.py`
- Source lines in `app_v11.py`: 1512–2560

- [ ] **Step 1: Create `pages/__init__.py`**

```python
# pages/__init__.py
```
(empty file)

- [ ] **Step 2: Create `pages/submit_visit.py`**

```python
# pages/submit_visit.py
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import text
from streamlit_folium import st_folium

from auth import resolve_session_user, set_url_param
from config import TIMEZONE, DUP_MINUTES
from db_ops import query_df, exec_sql, insert_visit_atomic
from utils import _utcnow_iso, _local_now_str, push_visit_to_pbi, _client_ip
from widgets import (
    customer_quick_find_module,
    customer_cascading_selectors,
    get_location_block,
    _on_customer_change,
    _on_bu_change,
    _on_line_change,
    _reset_geo_on_user_or_page_change,
    set_current_page,
)

try:
    from psycopg.errors import UniqueViolation
except Exception:
    UniqueViolation = None
```

Then copy `page_submit_visit` (app_v11.py:1512–2560) exactly.

- [ ] **Step 3: Verify import**

```bash
python -c "from pages.submit_visit import page_submit_visit; print('ok')"
```
Expected: `ok`

---

## Task 8: Extract `pages/check_in.py`

**Files:**
- Create: `pages/check_in.py`
- Source lines in `app_v11.py`: 2564–2816

- [ ] **Step 1: Create `pages/check_in.py`**

```python
# pages/check_in.py
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
from sqlalchemy import text
from streamlit_folium import st_folium

from auth import resolve_session_user
from config import TIMEZONE, DUP_MINUTES
from db_ops import query_df, exec_sql
from utils import _utcnow_iso, _local_now_str, _client_ip
from widgets import get_location_block, _reset_geo_on_user_or_page_change, set_current_page
```

Then copy `page_check_in` (app_v11.py:2564–2816) exactly.

- [ ] **Step 2: Verify import**

```bash
python -c "from pages.check_in import page_check_in; print('ok')"
```
Expected: `ok`

---

## Task 9: Extract remaining user-facing pages

**Files:**
- Create: `pages/my_submissions.py` (source: 2817–2922)
- Create: `pages/user_settings.py` (source: 2926–3022)

- [ ] **Step 1: Create `pages/my_submissions.py`**

```python
# pages/my_submissions.py
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df
from utils import _local_now
```

Copy `page_my_submissions` (app_v11.py:2817–2922) exactly.

- [ ] **Step 2: Create `pages/user_settings.py`**

```python
# pages/user_settings.py
import streamlit as st
from passlib.hash import pbkdf2_sha256
from sqlalchemy import text

from auth import resolve_session_user
from db import engine
from db_ops import query_df, exec_sql
```

Copy `page_user_settings` (app_v11.py:2926–3022) exactly.

- [ ] **Step 3: Verify imports**

```bash
python -c "from pages.my_submissions import page_my_submissions; from pages.user_settings import page_user_settings; print('ok')"
```
Expected: `ok`

---

## Task 10: Extract project pages

**Files:**
- Create: `pages/create_project.py` (source: 3026–3485)
- Create: `pages/project_view.py` (source: 3486–3838)
- Create: `pages/project_management.py` (source: 3839–4405)

- [ ] **Step 1: Create `pages/create_project.py`**

```python
# pages/create_project.py
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
from sqlalchemy import text
from streamlit_folium import st_folium

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df, exec_sql, insert_project
from utils import _utcnow_iso, _local_now_str
from widgets import (
    customer_quick_find_module,
    customer_cascading_selectors,
    get_location_block,
    _on_customer_change,
    _reset_geo_on_user_or_page_change,
    set_current_page,
)
```

Copy `page_create_project` (app_v11.py:3026–3485) exactly.

- [ ] **Step 2: Create `pages/project_view.py`**

```python
# pages/project_view.py
import json
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df
from utils import _local_now
```

Copy `page_project_view` (app_v11.py:3486–3838) exactly.

- [ ] **Step 3: Create `pages/project_management.py`**

```python
# pages/project_management.py
import json
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st
from dateutil import tz
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db import engine
from db_ops import query_df, exec_sql
from utils import _utcnow_iso
```

Copy all helpers and `page_project_management` (app_v11.py:3854–4405): `local_now`, `k`, `_fetch_projects_for_management`, `_fetch_project_row`, `_fetch_project_history`, `_update_project_with_history`, `page_project_management`.

- [ ] **Step 4: Verify imports**

```bash
python -c "from pages.create_project import page_create_project; from pages.project_view import page_project_view; from pages.project_management import page_project_management; print('ok')"
```
Expected: `ok`

---

## Task 11: Extract admin pages

**Files:**
- Create: `pages/admin_import.py` (source: 4409–7492)
- Create: `pages/admin_data.py` (source: 7493–8104)
- Create: `pages/admin_users.py` (source: 8105–8373)

- [ ] **Step 1: Create `pages/admin_import.py`**

```python
# pages/admin_import.py
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db import engine
from db_ops import query_df, exec_sql
from utils import _utcnow_iso, _local_now_str
```

Copy `page_admin_import` (app_v11.py:4409–7492) exactly.

- [ ] **Step 2: Create `pages/admin_data.py`**

```python
# pages/admin_data.py
import io
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from db_ops import query_df
```

Copy `page_admin_data` (app_v11.py:7493–8104) exactly.

- [ ] **Step 3: Create `pages/admin_users.py`**

Note: there is a local `_gen_tmp_pw` defined inside this section (app_v11.py:8105). Keep it local to this module.

```python
# pages/admin_users.py
import secrets
import string
from typing import Optional

import pandas as pd
import streamlit as st
from passlib.hash import pbkdf2_sha256
from sqlalchemy import text

from auth import resolve_session_user
from db_ops import query_df, exec_sql
from utils import _gen_tmp_pw
```

Copy `_gen_tmp_pw` (8105–8119, if different from utils version — check and deduplicate) and `page_admin_users` (8120–8373) exactly.

- [ ] **Step 4: Verify imports**

```bash
python -c "from pages.admin_import import page_admin_import; from pages.admin_data import page_admin_data; from pages.admin_users import page_admin_users; print('ok')"
```
Expected: `ok`

---

## Task 12: Extract review and change-request pages

**Files:**
- Create: `pages/review_audiences.py` (source: 8374–8992)
- Create: `pages/review_customers.py` (source: 8993–9572)
- Create: `pages/change_request.py` (source: 9573–10623)

- [ ] **Step 1: Create `pages/review_audiences.py`**

```python
# pages/review_audiences.py
import json
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df, exec_sql
from utils import _utcnow_iso
```

Copy `page_review_target_audiences` (app_v11.py:8374–8992) exactly.

- [ ] **Step 2: Create `pages/review_customers.py`**

```python
# pages/review_customers.py
import json
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df, exec_sql
from utils import _utcnow_iso
```

Copy `page_review_other_customers` (app_v11.py:8993–9572) exactly.

- [ ] **Step 3: Create `pages/change_request.py`**

```python
# pages/change_request.py
import json
import re
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
from sqlalchemy import text
from streamlit_folium import st_folium

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df, exec_sql
from utils import _utcnow_iso, _local_now_str
from widgets import get_location_block, _reset_geo_on_user_or_page_change, set_current_page
```

Copy `page_change_request` (app_v11.py:9573–10623) exactly.

- [ ] **Step 4: Verify imports**

```bash
python -c "from pages.review_audiences import page_review_target_audiences; from pages.review_customers import page_review_other_customers; from pages.change_request import page_change_request; print('ok')"
```
Expected: `ok`

---

## Task 13: Rewrite `app_v11.py` as thin orchestrator

**Files:**
- Rewrite: `app_v11.py`

- [ ] **Step 1: Replace `app_v11.py` with the orchestrator**

```python
# app_v11.py — Ascenda Sales Daily Feedback
import streamlit as st
from PIL import Image

from config import APP_TITLE
from ui import (
    apply_role_based_layout,
    capture_client_fingerprints,
    login_block,
    logout_button,
    show_footer,
    sidebar_nav,
)
from pages.submit_visit import page_submit_visit
from pages.check_in import page_check_in
from pages.my_submissions import page_my_submissions
from pages.user_settings import page_user_settings
from pages.create_project import page_create_project
from pages.project_view import page_project_view
from pages.project_management import page_project_management
from pages.admin_import import page_admin_import
from pages.admin_data import page_admin_data
from pages.admin_users import page_admin_users
from pages.review_audiences import page_review_target_audiences
from pages.review_customers import page_review_other_customers
from pages.change_request import page_change_request

st.set_page_config(
    page_title=APP_TITLE,
    page_icon=Image.open("static/ascenda_180.png"),
    layout="centered",
)

# PWA icons + CSS tweaks (must come after set_page_config)
import streamlit.components.v1 as components
components.html("""
<script>
(function() {
  const head = document.head;
  function add(tag, attrs) {
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k, v));
    head.appendChild(el);
  }
  add('link', { rel: 'apple-touch-icon', href: '/static/ascenda_180.png' });
  add('meta', { name: 'apple-mobile-web-app-capable', content: 'yes' });
  add('meta', { name: 'apple-mobile-web-app-title', content: 'Ascenda' });
  add('link', { rel: 'manifest', href: '/static/manifest.webmanifest' });
  add('meta', { name: 'theme-color', content: '#0ea5e9' });
  add('link', { rel: 'icon', type: 'image/png', sizes: '192x192', href: '/static/ascenda_192.png' });
})();
</script>
""", height=0)

st.markdown("""
<style>
[data-testid="stAppViewContainer"] .block-container {
    padding-top: 1.5rem !important;
    padding-bottom: 1.5rem !important;
}
[data-testid="stHeading"] a,
.stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
.stMarkdown h4 a, .stMarkdown h5 a, .stMarkdown h6 a {
    display: none !important;
}
</style>
""", unsafe_allow_html=True)

capture_client_fingerprints()

user = st.session_state.get("user")

if not user:
    login_block()
    show_footer()
else:
    apply_role_based_layout()
    logout_button()
    page = sidebar_nav()

    PAGE_MAP = {
        "Submit Visit": page_submit_visit,
        "Check-In": page_check_in,
        "My Submissions": page_my_submissions,
        "User Settings": page_user_settings,
        "Project Creation": page_create_project,
        "Projects View": page_project_view,
        "Project Management": page_project_management,
        "Admin: Import Lookups": page_admin_import,
        "Admin: Data Browser": page_admin_data,
        "Admin: Users": page_admin_users,
        "Review Target Audiences": page_review_target_audiences,
        "Review Other Customers": page_review_other_customers,
        "Visit Change Requests": page_change_request,
    }

    fn = PAGE_MAP.get(page)
    if fn:
        fn()
    else:
        st.warning(f"Unknown page: {page}")

    show_footer()
```

- [ ] **Step 2: Verify the app starts**

```bash
streamlit run app_v11.py --server.headless true &
sleep 5 && curl -s http://localhost:8501 | grep -o "Ascenda" | head -1
```
Expected: `Ascenda`

- [ ] **Step 3: Commit**

```bash
git add config.py db_ops.py auth.py utils.py widgets.py ui.py pages/ app_v11.py
git commit -m "refactor: split app_v11.py into focused modules"
```

---

## Final Checklist

- [ ] `python -c "import config, db_ops, auth, utils, widgets, ui"` — no errors
- [ ] `python -c "import pages.submit_visit, pages.check_in, pages.my_submissions"` — no errors
- [ ] `python -c "import pages.user_settings, pages.create_project, pages.project_view"` — no errors
- [ ] `python -c "import pages.project_management, pages.admin_import, pages.admin_data"` — no errors
- [ ] `python -c "import pages.admin_users, pages.review_audiences, pages.review_customers, pages.change_request"` — no errors
- [ ] `streamlit run app_v11.py` — app loads, login page shows
- [ ] Navigate every page — no `NameError` or `ImportError`
- [ ] `app_v11.py` is ≤ 80 lines
