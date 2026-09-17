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

try:
    import streamlit as st
    CARTO_API_KEY = os.environ.get("CARTO_API_KEY") or st.secrets["CARTO_API_KEY"]
except Exception:
    CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "")

# CARTO Positron raster basemap (matches folium's built-in "CartoDB positron"),
# with the API key appended so tiles aren't watermarked.
CARTO_POSITRON_TILES = (
    f"https://basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png?key={CARTO_API_KEY}"
    if CARTO_API_KEY
    else "CartoDB positron"
)
CARTO_ATTR = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    'contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
)
