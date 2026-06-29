# config.py
import os

APP_TITLE = "Ascenda"
TIMEZONE = "Asia/Riyadh"
SESSION_TTL_MIN = 20
DUP_MINUTES = 15
ACCURACY_METERS = 250

try:
    import streamlit as st
    PBI_PUSH_URL = os.environ.get("PBI_PUSH_URL") or st.secrets["PBI_PUSH_URL"]
except Exception:
    PBI_PUSH_URL = os.environ.get("PBI_PUSH_URL", "")
