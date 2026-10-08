"""Visual system for the Streamlit dashboard."""

import streamlit as st


CSS = r"""
<style>
:root {
  --dq-navy: #0d2340;
  --dq-navy-soft: #183453;
  --dq-ink: #14213a;
  --dq-muted: #667694;
  --dq-border: #dfe7f1;
  --dq-surface: #ffffff;
  --dq-soft: #f5f8fc;
  --dq-blue: #2563eb;
}
.stApp {
  background: var(--dq-soft); color: var(--dq-ink);
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
header[data-testid="stHeader"], .stAppHeader { background: transparent !important; }
.stApp h1, .stApp h2, .stApp h3, .stApp h4 { color: var(--dq-ink); font-weight: 700; letter-spacing: -.02em; }
[data-testid="stMainBlockContainer"] {
  width: min(96%, 1440px); max-width: 1440px;
  padding: .35rem 1.5rem 3rem;
}
.app-header {
  align-items: center; background: var(--dq-surface); border: 1px solid var(--dq-border);
  border-radius: 12px; display: flex; justify-content: space-between;
  margin: 0 9rem 1rem 0; min-height: 64px; padding: .75rem 1rem;
  position: relative; z-index: 1;
  box-shadow: 0 1px 2px rgba(15, 35, 64, .04);
}
.app-title { color: var(--dq-ink); font-size: 1.1rem; font-weight: 750; }
.app-subtitle, .page-subtitle, .last-refresh { color: var(--dq-muted); font-size: .82rem; margin: .18rem 0 0; }
.app-header-meta { align-items: center; display: flex; gap: .75rem; }
.page-header { margin: .1rem 0 1rem; }
.page-header h1 { font-size: 1.75rem; margin: 0; }
.storage-badge, .status-badge {
  background: #eef4ff; border: 1px solid #c7d7fe; border-radius: 999px; color: #1d4ed8;
  display: inline-block; font-size: .68rem; font-weight: 750; letter-spacing: .035em;
  padding: .22rem .52rem; text-transform: uppercase;
}
.metric-card, .info-item {
  background: var(--dq-surface); border: 1px solid var(--dq-border); border-radius: 10px;
  min-height: 94px; padding: .85rem .9rem; margin-bottom: .65rem;
  box-shadow: 0 1px 2px rgba(15, 35, 64, .025);
}
.metric-label, .info-label { color: var(--dq-muted); font-size: .7rem; font-weight: 700; letter-spacing: .025em; text-transform: uppercase; }
.metric-value { color: var(--dq-ink); font-size: 1.4rem; font-weight: 750; margin-top: .28rem; }
.metric-helper { color: var(--dq-muted); font-size: .7rem; margin-top: .15rem; }
.info-value { color: var(--dq-ink); font-size: .9rem; font-weight: 650; margin-top: .28rem; overflow-wrap: anywhere; }
.quota-risk-low { border-color: #a7e5bf; background: #f0fdf4; }
.quota-risk-medium { border-color: #f8cf7a; background: #fffbeb; }
.quota-risk-high, .quota-risk-insufficient { border-color: #fecaca; background: #fff1f2; }
.quota-risk-low .info-value { color: #067647; }
.quota-risk-medium .info-value { color: #a15c00; }
.quota-risk-high .info-value, .quota-risk-insufficient .info-value { color: #b42318; }
.agent-status-label { color: var(--dq-muted); font-size: .7rem; font-weight: 650; margin: .25rem 0 .2rem; }
.empty-state {
  background: var(--dq-surface); border: 1px dashed #c9d5e5; border-radius: 10px;
  padding: 1.35rem; text-align: center;
}
.empty-state-title { font-weight: 700; }
.empty-state-description { color: var(--dq-muted); font-size: .86rem; margin-top: .25rem; }
.status-accepted, .status-ok, .status-completed, .status-complete, .status-success { background: #eafbf1; border-color: #abe8c3; color: #067647; }
.status-warning, .status-partial { background: #fff7e6; border-color: #fedb8b; color: #a15c00; }
.status-rejected, .status-error, .status-failed { background: #fff0ef; border-color: #fecac5; color: #b42318; }
.status-not-run, .status-unknown, .status-incomplete, .status-no-file-processed, .status-n\/a {
  background: #f1f4f8; border-color: #ced7e3; color: #526177;
}
[data-testid="stVerticalBlockBorderWrapper"] > div {
  background: var(--dq-surface); border-color: var(--dq-border); border-radius: 11px;
}
[data-testid="stMetric"] { background: var(--dq-surface); border: 1px solid var(--dq-border); border-radius: 10px; padding: .7rem; }
[data-testid="stCaptionContainer"], .stCaption { color: var(--dq-muted) !important; }
div[data-testid="stDataFrame"] { border: 1px solid var(--dq-border); border-radius: 8px; overflow: hidden; }
div[data-testid="stDataFrame"] [role="columnheader"] { background: #edf3fa; color: #34445d; font-size: .82rem; font-weight: 700; }
div[data-testid="stDataFrame"] [role="gridcell"] { font-size: .82rem; min-height: 36px; }
.artifact-reference { border-bottom: 1px solid var(--dq-border); padding: .65rem 0; }
.artifact-reference:last-child { border-bottom: 0; }
.artifact-label { color: var(--dq-muted); font-size: .75rem; font-weight: 650; margin-bottom: .25rem; }
.artifact-reference code {
  background: #f7f9fc; border: 1px solid #e2e8f0; border-radius: 6px; color: #344054;
  display: block; overflow-wrap: anywhere; padding: .5rem .6rem; white-space: pre-wrap;
}

/* Stable dark navigation rail. Native widgets retain light input surfaces. */
[data-testid="stSidebar"] { background: linear-gradient(180deg, #0b213c 0%, #112d4d 100%); border-right: 0; }
[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
  height: 2.5rem; margin: 0; min-height: 2.5rem; padding: 0;
  position: absolute; right: .45rem; top: .35rem; width: 2.5rem; z-index: 5;
}
[data-testid="stSidebar"] [data-testid="stSidebarContent"] { padding-top: 0; }
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { padding-top: .35rem; }
[data-testid="stSidebar"] .sidebar-brand { border-bottom: 1px solid rgba(255,255,255,.16); margin-bottom: .85rem; padding: .25rem 0 .9rem; }
[data-testid="stSidebar"] .sidebar-brand-title { color: white; font-size: 1.05rem; font-weight: 750; }
[data-testid="stSidebar"] .sidebar-brand-subtitle { color: #aebed1; font-size: .74rem; }
[data-testid="stSidebar"] .dq-sidebar-section { color: #9fb2c9; font-size: .67rem; font-weight: 750; letter-spacing: .08em; margin: .9rem 0 .35rem; }
[data-testid="stSidebar"] p, [data-testid="stSidebar"] label, [data-testid="stSidebar"] .stCaption { color: #d9e3ee !important; }
[data-testid="stSidebar"] [role="radiogroup"] label { border-radius: 7px; padding: .2rem .35rem; }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) { background: #dbeafe; }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) p { color: #1d4ed8 !important; font-weight: 700; }
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-baseweb="input"] > div,
[data-testid="stSidebar"] [data-testid="stDateInput"] input { background: white; color: #17223b; }
[data-testid="stSidebar"] [data-baseweb="tag"] {
  background: #e8edff !important; border: 1px solid #c7d2fe; color: #293f83 !important;
}
[data-testid="stSidebar"] summary p { color: #eaf0f7 !important; }
[data-testid="stSidebar"] button { border-color: #7890aa; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { line-height: 1.25; margin-bottom: -.2rem; }
[data-testid="stSidebar"] .sidebar-data-line { color: #d9e3ee; font-size: .78rem; margin-top: .35rem; }
button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"] {
  background: var(--dq-blue) !important; border-color: var(--dq-blue) !important; color: white !important;
}
button[kind="primary"]:hover, [data-testid="stFormSubmitButton"] button[kind="primary"]:hover {
  background: #1d4ed8 !important; border-color: #1d4ed8 !important;
}
h2, h3, h4 { scroll-margin-top: 1rem; }

@media (max-width: 1000px) {
  [data-testid="stMainBlockContainer"] {
    width: 100%; padding: .35rem 1rem 3rem;
  }
  .app-header { margin-right: 0; z-index: 1; }
  .last-refresh { display: none; }
}
</style>
"""


def inject_custom_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def apply_styles() -> None:
    inject_custom_css()


load_css = inject_custom_css
inject_css = inject_custom_css
apply_custom_css = inject_custom_css
apply_global_styles = inject_custom_css
inject_global_styles = inject_custom_css
