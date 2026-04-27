# app_pages/app_settings.py
import json

import streamlit as st
import streamlit.components.v1 as components

from db_ops import query_df, exec_sql
from ui import section_header
from widgets import set_current_page


def _load_prefs(uid: int) -> dict:
    df = query_df("SELECT preferences FROM users WHERE user_id = :uid", {"uid": uid})
    if df.empty:
        return {}
    raw = df.iloc[0]["preferences"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw or {}


def _save_prefs(uid: int, prefs: dict):
    exec_sql(
        "UPDATE users SET preferences = CAST(:p AS jsonb) WHERE user_id = :uid",
        {"p": json.dumps(prefs, ensure_ascii=False), "uid": uid},
    )


def _section_label(icon_svg: str, text: str):
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:7px;padding:1.1rem 0 0.45rem;">'
        f'<span style="display:flex;align-items:center;color:var(--color-text-subtle);">{icon_svg}</span>'
        f'<span style="font-size:0.68rem;font-weight:700;color:var(--color-text-subtle);'
        f'text-transform:uppercase;letter-spacing:0.09em;">{text}</span></div>',
        unsafe_allow_html=True,
    )


def _coming_soon_pill() -> str:
    return (
        '<span style="display:inline-flex;align-items:center;padding:1px 7px;'
        'border-radius:20px;font-size:0.68rem;font-weight:600;line-height:1.6;'
        'background:#f0f6ff;color:#2563eb;border:1px solid #bfdbfe;'
        'margin-left:7px;vertical-align:middle;">Coming soon</span>'
    )


def _setting_row(
    title: str,
    description: str,
    key: str,
    value: bool,
    coming_soon: bool = False,
) -> bool:
    col_text, col_switch = st.columns([5, 1])
    with col_text:
        pill = _coming_soon_pill() if coming_soon else ""
        st.markdown(
            f'<div style="padding:6px 0;">'
            f'<div style="font-size:0.9375rem;font-weight:500;color:var(--color-text);'
            f'display:flex;align-items:center;flex-wrap:wrap;gap:0;">'
            f'{title}{pill}</div>'
            f'<div style="font-size:0.8rem;color:var(--color-text-muted);margin-top:3px;">{description}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with col_switch:
        return st.toggle("", value=value, key=key, label_visibility="collapsed")


def page_app_settings():
    section_header("App Settings", "Manage your app preferences")
    set_current_page("app_settings")

    st.markdown("""
    <style>
    div[data-testid="stHorizontalBlock"] { align-items: center !important; }
    div[data-testid="stToggle"] { justify-content: flex-end !important; }
    div[data-testid="stToggle"] label { padding: 0 !important; }
    </style>
    """, unsafe_allow_html=True)

    u = st.session_state.user
    uid = int(u.get("user_id") or u.get("id"))
    prefs = _load_prefs(uid)

    lang_is_ar    = prefs.get("language", "en") == "ar"
    theme_is_dark = prefs.get("theme", "light") == "dark"

    # ── Language & Appearance ─────────────────────────────────────────────────
    _section_label(
        '<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">'
        '<circle cx="12" cy="12" r="10"/>'
        '<path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>'
        '</svg>',
        "Language & Appearance",
    )

    with st.container(border=True):
        new_lang_ar = _setting_row(
            "Arabic Language",
            "Switch the interface language to العربية",
            "app_settings_lang",
            lang_is_ar,
            coming_soon=True,
        )
        st.divider()
        new_dark = _setting_row(
            "Dark Mode",
            "Darker interface for low-light environments",
            "app_settings_theme",
            theme_is_dark,
        )
        st.divider()
        col_save, col_msg = st.columns([1, 3])
        with col_save:
            save_clicked = st.button(
                "Save preferences",
                type="primary",
                key="save_prefs_btn",
                use_container_width=True,
            )
        if save_clicked:
            prefs["language"] = "ar" if new_lang_ar else "en"
            prefs["theme"]    = "dark" if new_dark else "light"
            _save_prefs(uid, prefs)
            _theme_val = prefs["theme"]
            components.html(
                f'<script>'
                f'try{{'
                f'localStorage.setItem("_ascenda_theme",{repr(_theme_val)});'
                f'var doc=document;'
                f'try{{if(window.parent!==window)doc=window.parent.document;}}catch(e){{}}'
                f'doc.documentElement.setAttribute("data-theme",{repr(_theme_val)});'
                f'}}catch(e){{}}'
                f'</script>',
                height=0,
            )
            with col_msg:
                st.markdown(
                    '<div style="display:flex;align-items:center;height:100%;padding-top:6px;">'
                    '<span style="font-size:0.875rem;color:var(--status-success-text);font-weight:500;">'
                    '&#10003;&nbsp; Preferences saved</span></div>',
                    unsafe_allow_html=True,
                )

    # ── Device ────────────────────────────────────────────────────────────────
    _section_label(
        '<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">'
        '<rect x="5" y="2" width="14" height="20" rx="2"/>'
        '<line x1="12" y1="18" x2="12.01" y2="18"/>'
        '</svg>',
        "Device",
    )

    with st.container(border=True):
        st.markdown(
            '<div style="display:flex;align-items:flex-start;gap:12px;padding:4px 0 8px;">'
            '<div style="width:36px;height:36px;border-radius:9px;background:var(--color-primary-subtle);'
            'display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px;">'
            '<svg width="18" height="18" fill="none" stroke="#2563eb" stroke-width="2" viewBox="0 0 24 24">'
            '<path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"/>'
            '</svg></div>'
            '<div>'
            '<div style="font-size:0.9375rem;font-weight:500;color:var(--color-text);">Install App</div>'
            '<div style="font-size:0.8rem;color:var(--color-text-muted);margin-top:2px;line-height:1.5;">'
            'Add Ascenda to your home screen for quick, full-screen access — no app store needed.</div>'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        components.html("""
<style>
  #iw { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  #install-btn {
    display: inline-flex; align-items: center; gap: 7px;
    background: #2563eb; color: #fff; border: none; border-radius: 8px;
    padding: 9px 18px; font-size: 14px; font-weight: 600; cursor: pointer;
    letter-spacing: -0.01em; transition: background 0.15s, box-shadow 0.15s;
    box-shadow: 0 1px 2px rgba(37,99,235,0.18);
  }
  #install-btn:hover { background: #1d4ed8; box-shadow: 0 2px 6px rgba(37,99,235,0.28); }
  #install-btn:disabled { background: #94a3b8; cursor: default; box-shadow: none; }
  #ios-guide {
    display: none; margin-top: 12px;
    background: #f0f6ff; border: 1px solid #bfdbfe;
    border-radius: 10px; padding: 13px 16px; font-size: 13.5px; line-height: 1.7;
    color: #1e3a5f;
  }
  #ios-guide ol { margin: 6px 0 0 18px; padding: 0; }
  #already-msg {
    display: none; color: #0e8a4f; font-size: 14px; font-weight: 500;
    display: none; align-items: center; gap: 6px;
  }
</style>
<div id="iw">
  <button id="install-btn">
    <svg width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"/>
    </svg>
    Add to Home Screen
  </button>
  <div id="ios-guide">
    <strong>On iPhone / iPad:</strong>
    <ol>
      <li>Tap the <strong>Share</strong> button at the bottom of Safari</li>
      <li>Scroll down and tap <strong>"Add to Home Screen"</strong></li>
      <li>Tap <strong>"Add"</strong> — done!</li>
    </ol>
  </div>
  <div id="already-msg">&#10003;&nbsp;Ascenda is already installed on this device.</div>
</div>
<script>
(function() {
  const btn = document.getElementById('install-btn');
  const iosGuide = document.getElementById('ios-guide');
  const alreadyMsg = document.getElementById('already-msg');
  function isIOS() { return /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream; }
  function isInstalled() {
    return window.matchMedia('(display-mode: standalone)').matches ||
           window.navigator.standalone === true;
  }
  function resize() {
    const h = document.getElementById('iw').scrollHeight + 16;
    window.parent.document.querySelectorAll('iframe').forEach(function(f) {
      try { if (f.contentWindow === window) f.style.height = h + 'px'; } catch(e) {}
    });
  }
  if (isInstalled()) {
    btn.style.display = 'none';
    alreadyMsg.style.display = 'flex';
    resize();
  } else if (isIOS()) {
    btn.innerHTML = '<svg width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><path stroke-linecap="round" stroke-linejoin="round" d="M8.684 13.342C8.886 12.938 9 12.482 9 12c0-.482-.114-.938-.316-1.342m0 2.684a3 3 0 110-2.684m0 2.684l6.632 3.316m-6.632-6l6.632-3.316m0 0a3 3 0 105.367-2.684 3 3 0 00-5.367 2.684zm0 9.316a3 3 0 105.368 2.684 3 3 0 00-5.368-2.684z"/></svg> How to install on iPhone';
    btn.onclick = function() {
      const open = iosGuide.style.display === 'block';
      iosGuide.style.display = open ? 'none' : 'block';
      resize();
    };
    resize();
  } else {
    function tryBind() {
      const prompt = window.parent.__ascendaDeferredPrompt;
      if (prompt) {
        btn.disabled = false;
        btn.onclick = function() {
          prompt.prompt();
          prompt.userChoice.then(function(r) {
            if (r.outcome === 'accepted') {
              btn.textContent = '✓ Installing…';
              btn.disabled = true;
              window.parent.__ascendaDeferredPrompt = null;
            }
          });
        };
      } else {
        btn.disabled = true;
        btn.title = 'Open in Chrome and browse the app for a moment, then try again.';
        btn.innerHTML = btn.innerHTML.replace('Add to Home Screen', 'Install not available yet');
        setTimeout(tryBind, 2000);
      }
    }
    tryBind();
  }
})();
</script>
""", height=56, scrolling=False)
