"""ChainShield AI - Streamlit interface.

Design rules for this UI:
  * Lead with the answer (verdict + one sentence), then the story, then the evidence.
  * No raw JSON on the main path. Evidence is shown as plain facts and small tables.
  * Every technical tool name is translated into the question it answers.
"""
from __future__ import annotations

import html as _html
import json
import re
import sqlite3
from copy import deepcopy
from datetime import datetime

import pandas as pd
import streamlit as st

import hashlib

import llm_assist
import qa
import testsuite
import voice
from agent_engine import (ACTIONS, DEFAULT_POLICY, InvalidTransaction, execute_action, graph_to_dot,
                          investigate_transaction, needs_approval, report_to_markdown)
from database import Database
from scenarios import SCENARIOS

st.set_page_config(page_title="ChainShield AI | Security Operations", page_icon="🛡️", layout="wide",
                   initial_sidebar_state="expanded")

PAGES = [
    "◈  Dashboard",
    "◉  Workflow Lab",
    "◇  Security Analysis",
    "⌘  Dependency Graph",
    "≡  Audit Trail",
    "▦  Test Lab",
    "⚙  Settings",
]
P_HOME, P_CHECK, P_ANALYSIS, P_GRAPH, P_HIST, P_TEST, P_SETTINGS = PAGES
POLICY = dict(DEFAULT_POLICY)

STATUS_STYLE = {
    "PASS": ("✅", "Passed", "#34d399"),
    "FLAG": ("⚠️", "Looks suspicious", "#fbbf24"),
    "VIOLATION": ("⛔", "Rule broken", "#f87171"),
    "INFO": ("ℹ️", "Background info", "#60a5fa"),
    "ERROR": ("❗", "Check failed", "#f87171"),
}
# decision -> (icon, short label, colour, plain-words headline, plain-words instruction)
DECISION_STYLE = {
    "ALLOW": ("✅", "Cleared", "#34d399", "Safe to release", "Every check passed. This refund can go ahead."),
    "ESCALATE": ("⚠️", "Needs review", "#fbbf24", "Needs a person to review", "Do not release this refund yet."),
    "BLOCK": ("⛔", "Blocked", "#f87171", "Stop: a rule was broken", "This refund should not go ahead."),
}
RISK_LEVELS = ["Low", "Medium", "High"]
EVENT_ICON = {
    "case_received": "＋", "tool_selected": "⌕", "tool_result": "⌕", "decision_made": "◇",
    "human_approval": "✓", "auto_approved_by_policy": "✓", "action_executed": "→",
    "outcome_verification": "✓", "question_answered": "?", "action_refused": "×",
    "tool_call_rejected": "×", "planner_error": "!",
}
ROLE_LABEL = {"finance_manager": "Finance manager", "risk_officer": "Risk officer",
              "support_agent": "Support agent", "intern": "Intern", "guest": "Guest", "unknown": "Unknown"}

SCENARIO_SUMMARIES = {
    "Everyday refund (clean)": ("Everyday refund", "Routine request from an approved employee.", "Routine"),
    "Large refund, no approval (the pitch example)": ("Large refund, no approval", "Large amount with no matching approval.", "Approval"),
    "Large refund WITH valid approval": ("Large refund, approved", "Valid approval covers the amount.", "Approval"),
    "Approval too small for the amount": ("Approval too small", "Approval does not cover the full amount.", "Approval"),
    "Unauthorised employee role": ("Request from an intern", "This employee role cannot issue refunds.", "Permission"),
    "Re-used transaction ID": ("Previously used request ID", "This request ID already exists.", "Duplicate"),
    "Second refund on the same order": ("Repeat refund on one order", "Earlier refunds exist for this order.", "Repeat activity"),
    "Rapid refund pattern on one account": ("Several recent refunds", "Multiple recent refunds share this account.", "Repeat activity"),
    "Incomplete request": ("Missing request details", "Order and account details are missing.", "Incomplete data"),
    "Blank form (type your own)": ("Custom request", "Enter your own request details.", "Custom"),
}

CSS = """
<style>
:root {color-scheme:dark; --bg:#0b0f17; --panel:#111823; --panel2:#151e2b; --line:#273343;
  --ink:#edf3f8; --muted:#91a0b2; --cyan:#35d5e8; --violet:#9b8cff; --red:#ff6677;
  --green:#41d695; --amber:#f2b84b;}
.stApp {background:var(--bg); color:var(--ink);}
.block-container {padding:1.55rem 2.2rem 3rem; max-width:1500px;}
body, [class*="css"] {font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;}
h1,h2,h3,h4 {color:var(--ink); letter-spacing:-.025em;}
h1 {font-size:2rem !important;} h2 {font-size:1.35rem !important;} h3 {font-size:1.08rem !important;}
p, label, [data-testid="stCaptionContainer"] {color:var(--muted);}

[data-testid="stSidebar"] {background:#0d131d; border-right:1px solid var(--line);}
[data-testid="stSidebar"] > div:first-child {padding:1.35rem .95rem;}
.cs-brand {display:flex; align-items:center; gap:.75rem; padding:.35rem .35rem 1.1rem; border-bottom:1px solid var(--line); margin-bottom:1.1rem;}
.cs-brand .logo {width:42px; height:42px; border:1px solid #386779; border-radius:10px; background:#122735;
  display:flex; align-items:center; justify-content:center; font-size:1.25rem; color:var(--cyan);}
.cs-brand b {font-size:1rem; letter-spacing:.01em; color:var(--ink);}
.cs-brand span {display:block; margin-top:2px; color:var(--muted); font-size:.72rem; letter-spacing:.08em; text-transform:uppercase;}
[data-testid="stSidebar"] [role="radiogroup"] {gap:.22rem;}
[data-testid="stSidebar"] [role="radiogroup"] label {padding:.48rem .6rem; border:1px solid transparent; border-radius:7px; color:#aab8c8;}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {background:#172331;}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {background:#152631; border-color:#245261; color:#e8fbff;}
[data-testid="stSidebar"] [role="radiogroup"] label > div:first-child {display:none;}

.cs-page-head {display:flex; align-items:flex-end; justify-content:space-between; gap:1rem; margin:.1rem 0 1.35rem;}
.cs-eyebrow {color:var(--cyan); font-size:.72rem; font-weight:750; letter-spacing:.14em; text-transform:uppercase; margin-bottom:.45rem;}
.cs-page-head h1 {margin:0; font-weight:720; line-height:1.15;}
.cs-page-head p {margin:.45rem 0 0; max-width:760px; line-height:1.55;}
.cs-status {white-space:nowrap; border:1px solid #285440; color:var(--green); background:#10231b; border-radius:5px; padding:.4rem .65rem; font-size:.74rem; font-weight:700; letter-spacing:.07em;}
.cs-hero {position:relative; overflow:hidden; border:1px solid #263d4c; border-left:3px solid var(--cyan); background:#101a25;
  border-radius:8px; padding:1.65rem 1.8rem; margin:0 0 1.3rem;}
.cs-hero h1 {font-size:1.85rem; max-width:700px; margin:0 0 .5rem; font-weight:720; line-height:1.2;}
.cs-hero p {max-width:760px; margin:0; line-height:1.6;}
.cs-title {font-size:1.8rem; font-weight:720; color:var(--ink); line-height:1.2; margin:.2rem 0 .35rem;}
.cs-sub {color:var(--muted); margin:0 0 1rem; line-height:1.55;}
.cs-section {margin:1.35rem 0 .65rem; font-size:1.03rem; font-weight:680; color:var(--ink);}

.cs-verdict {display:flex; align-items:center; gap:1.4rem; flex-wrap:wrap; border:1px solid; border-radius:8px; padding:1.2rem 1.35rem; margin:.2rem 0 1rem;}
.cs-vmain {flex:1 1 320px;}
.cs-vlabel {display:inline-block; font-size:.78rem; font-weight:750; padding:.22rem .62rem; border-radius:4px; border:1px solid; margin-bottom:.55rem;}
.cs-vhead {font-size:1.85rem; font-weight:740; color:#fff; line-height:1.15; margin:0 0 .3rem;}
.cs-vline {color:#c4d0dc; font-size:.98rem;}
.cs-vmeta {color:var(--muted); font-size:.78rem; margin-top:.75rem;}
.cs-gauge {position:relative; width:126px; height:126px; flex:0 0 126px;}
.cs-gauge svg {transform:rotate(-90deg);} .cs-gauge .ring {transition:stroke-dashoffset .6s ease;}
.cs-gauge .c {position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center;}
.cs-gauge .c b {font-size:1.2rem; color:#fff;} .cs-gauge .c span {font-size:.7rem; color:var(--muted);}
.cs-pill {display:inline-block; padding:3px 9px; border-radius:4px; border:1px solid; font-weight:700; font-size:.76rem; white-space:nowrap;}
.cs-why {margin:.1rem 0; padding-left:0; list-style:none;}
.cs-why li {padding:.5rem 0 .5rem 1.15rem; position:relative; border-bottom:1px solid #202b38; color:#d7e1eb; font-size:.94rem; line-height:1.45;}
.cs-why li:before {content:""; position:absolute; left:.15rem; top:.95rem; width:5px; height:5px; border-radius:50%; background:var(--cyan);}
.cs-why li:last-child {border-bottom:0;}
.cs-check {display:flex; gap:.7rem; align-items:flex-start; padding:.55rem 0; border-bottom:1px solid #202b38; font-size:.92rem;}
.cs-check .i {flex:0 0 1.5rem; font-size:1rem;} .cs-check .q {color:var(--ink); font-weight:650;} .cs-check .a {color:var(--muted); font-size:.84rem;}
.cs-card {background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:1rem 1.05rem; height:100%;}
.cs-card .big {font-size:1.5rem;} .cs-card h4 {margin:.35rem 0; color:var(--ink);} .cs-card p {font-size:.88rem; margin:0; line-height:1.5;}
.cs-flow {display:flex; gap:.55rem; flex-wrap:wrap; margin:.5rem 0 1rem;}
.cs-flow div {flex:1 1 150px; border:1px solid; border-radius:5px; padding:.6rem .75rem; font-size:.86rem;}
.cs-brow {display:flex; align-items:center; gap:.8rem; margin:.3rem 0; font-size:.86rem;}
.cs-brow .l {flex:0 0 52%; color:#d5dfee;} .cs-brow .t {flex:1; height:7px; background:#1c2a40; border-radius:99px; overflow:hidden;}
.cs-brow .t div {height:100%; background:var(--cyan);} .cs-brow .p {flex:0 0 90px; text-align:right; font-weight:700;}
.cs-tl {border-left:1px solid #314151; margin-left:.45rem; padding-left:1rem;}
.cs-tl .e {margin:.65rem 0; font-size:.86rem; color:#d5dfee;} .cs-tl .ts {color:#8090a2; font-size:.72rem;}

[data-testid="stMetric"] {background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:.9rem 1rem;}
[data-testid="stMetricLabel"] {color:var(--muted) !important; font-size:.8rem;}
[data-testid="stMetricValue"] {color:var(--ink); font-size:1.7rem;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:7px !important; border-color:var(--line) !important; background:var(--panel);}
.stButton > button, .stFormSubmitButton > button {border-radius:5px; font-weight:680; border:1px solid #344354; padding:.48rem .9rem;}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {background:#087e91; border:1px solid #20b3c5; color:#fff;}
.stButton > button:hover, .stFormSubmitButton > button:hover {border-color:var(--cyan); color:#fff;}
[data-testid="stExpander"] {border-radius:6px; border-color:var(--line);}
[data-testid="stDataFrame"] {border:1px solid var(--line); border-radius:6px; overflow:hidden;}
@media (max-width: 800px) {
  .block-container {padding:.9rem 1rem 2rem;}
  .cs-page-head {align-items:flex-start; flex-direction:column;}
  .cs-hero {padding:1.25rem;}
  .cs-hero h1 {font-size:1.5rem;}
  .cs-vhead {font-size:1.45rem;}
  .cs-gauge {width:104px; height:104px; flex-basis:104px;}
  .cs-gauge svg {width:104px; height:104px;}
}
@media (max-width: 520px) {
  .block-container {padding:.65rem .7rem 1.5rem;}
  .cs-title {font-size:1.45rem;}
  .cs-verdict {padding:1rem;}
}
@media (prefers-reduced-motion: reduce) {.cs-gauge .ring {transition:none;}}

/* Premium operations-console layer */
:root {
  --bg:#080d18; --panel:#101827; --panel2:#131e30; --line:#24334a;
  --ink:#f2f6fc; --muted:#9baac0; --cyan:#47d9f2; --violet:#a393ff;
}
.stApp {background:radial-gradient(ellipse at 70% -15%, #16243d 0, #080d18 48%); color:var(--ink);}
.block-container {max-width:1440px; padding:2rem clamp(1.1rem, 3vw, 3rem) 3.5rem;}
[data-testid="stHeader"] {background:transparent;}
[data-testid="stSidebar"] {background:#0b1220; border-right:1px solid #202e43;}
[data-testid="stSidebar"] > div:first-child {padding:1.55rem 1rem;}
.cs-brand {gap:.85rem; padding:.4rem .45rem 1.35rem; margin-bottom:1.2rem;}
.cs-brand .logo {width:44px;height:44px;border-radius:13px;border-color:#31536d;
  background:linear-gradient(145deg,#132b3b,#211d3d);font-size:1.35rem;box-shadow:0 0 25px #31cbe31a;}
.cs-brand b {font-size:1.04rem; letter-spacing:-.02em;}
.cs-brand span {font-size:.67rem; letter-spacing:.11em;}
[data-testid="stSidebar"] [role="radiogroup"] {gap:.35rem;}
[data-testid="stSidebar"] [role="radiogroup"] label {padding:.67rem .72rem;border-radius:9px;color:#a8b6ca;font-weight:560;}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
  background:linear-gradient(90deg,#153148,#172438);border-color:#24516a;color:#ecfbff;
  box-shadow:inset 2px 0 #48d8ed;
}
.cs-page-head {margin:.3rem 0 1.6rem;align-items:center;}
.cs-page-head h1 {font-size:1.8rem !important;font-weight:700;letter-spacing:-.04em;}
.cs-page-head p {font-size:.96rem;max-width:800px;}
.cs-eyebrow {color:#81dff0;letter-spacing:.16em;}
.cs-status {border-radius:999px;padding:.5rem .85rem;letter-spacing:.04em;}
.cs-hero {background:linear-gradient(110deg,#111d2e 0%,#111827 64%,#17182d 100%);
  border:1px solid #293a53;border-left:3px solid var(--cyan);border-radius:16px;
  padding:clamp(1.5rem,3vw,2.4rem);margin-bottom:1.25rem;box-shadow:0 18px 48px #02071144;}
.cs-hero h1 {font-size:clamp(1.8rem,3.8vw,2.8rem) !important;max-width:760px;letter-spacing:-.045em;}
.cs-hero p {max-width:720px;font-size:1rem;line-height:1.7;}
.cs-hero-meta {display:flex;align-items:center;gap:.55rem;color:#a8b6ca;font-size:.78rem;
  letter-spacing:.09em;text-transform:uppercase;margin-bottom:.9rem;}
.cs-live-dot {width:8px;height:8px;border-radius:50%;background:#48d99a;box-shadow:0 0 12px #48d99a88;}
.cs-demo {display:flex;align-items:flex-start;gap:.8rem;padding:.9rem 1rem;margin:0 0 1.4rem;
  border:1px solid #443e70;background:#17172a;border-radius:12px;color:#c7c0ff;line-height:1.5;}
.cs-demo strong {display:block;color:#e3defe;font-size:.83rem;margin-bottom:.12rem;}
.cs-section {margin:1.7rem 0 .85rem;font-size:1.05rem;font-weight:680;letter-spacing:-.015em;}
.cs-card {background:linear-gradient(155deg,#111b2b,#0e1725);border:1px solid #26354a;border-radius:13px;
  padding:1.15rem 1.2rem;box-shadow:0 10px 24px #02071120;}
[data-testid="stMetric"] {min-height:112px;background:linear-gradient(155deg,#111b2b,#0e1725);
  border:1px solid #26354a;border-radius:13px;padding:1.05rem 1.15rem;box-shadow:0 10px 24px #02071120;}
[data-testid="stMetricLabel"] {font-size:.78rem !important;letter-spacing:.035em;}
[data-testid="stMetricValue"] {font-size:1.8rem;font-weight:700;letter-spacing:-.04em;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:13px !important;border-color:#26354a !important;background:#101827;}
.stButton > button,.stFormSubmitButton > button,[data-testid="stDownloadButton"] button {
  min-height:2.7rem;border-radius:9px;font-weight:650;border:1px solid #34455f;
  background:#141f30;color:#dce7f5;transition:background .15s ease,border-color .15s ease;
}
.stButton > button[kind="primary"],.stFormSubmitButton > button[kind="primary"] {
  background:linear-gradient(110deg,#087e95,#176fa1);border-color:#37cbe1;color:#fff;
  box-shadow:0 6px 18px #18b9d526;
}
.stButton > button:hover,.stFormSubmitButton > button:hover,[data-testid="stDownloadButton"] button:hover {
  border-color:#53d9ed;color:#fff;background:#18283b;
}
.stButton > button[kind="primary"]:hover,.stFormSubmitButton > button[kind="primary"]:hover {background:#128aa2;}
[data-testid="stExpander"] {border-radius:11px;border-color:#29384f;background:#0e1725;}
[data-testid="stDataFrame"] {border:1px solid #29384f;border-radius:11px;overflow:hidden;}
.cs-tl {border-left:1px solid #30445e;margin:.5rem 0 .5rem .45rem;padding-left:1.1rem;}
.cs-tl .e {position:relative;margin:0;padding:0 0 1.1rem;font-size:.88rem;color:#d9e3f0;line-height:1.5;}
.cs-tl .e:before {content:"";position:absolute;left:-1.43rem;top:.25rem;width:9px;height:9px;
  border:2px solid #47d9f2;border-radius:50%;background:#101827;box-shadow:0 0 12px #47d9f244;}
.cs-tl .e:last-child {padding-bottom:.2rem;}
.cs-tl .ts {color:#8e9eb5;font-size:.74rem;margin-top:.18rem;}
.cs-tl .event-case {color:#8798b0;font-size:.74rem;margin-top:.15rem;}
.cs-verdict {border-radius:14px;padding:1.45rem 1.6rem;margin:.5rem 0 1rem;}
.cs-vhead {font-size:2rem;letter-spacing:-.035em;}
.cs-vline {line-height:1.55;}
.cs-flow div {border-radius:10px;padding:.8rem .9rem;background:#111b2b;}
.cs-scenario-title {font-size:.86rem;font-weight:700;color:#e8f0fa;margin:.1rem 0 .3rem;}
.cs-scenario-copy {font-size:.8rem;line-height:1.5;color:#9eacc1;min-height:2.4rem;}
.cs-scenario-meta {font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:#8ca0ba;margin-top:.65rem;}
.cs-result-grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:.75rem;margin:.9rem 0;}
.cs-result-item {padding:.85rem 1rem;border:1px solid #29384f;border-radius:11px;background:#0d1624;}
.cs-result-item span {display:block;color:#91a2ba;font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:.35rem;}
.cs-result-item strong {display:block;color:#e7eff9;font-size:.9rem;line-height:1.45;font-weight:600;}
.cs-test-row {display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,2fr) auto;gap:.9rem;align-items:center;
  padding:.85rem 1rem;border:1px solid #26354a;border-radius:10px;background:#101827;margin:.45rem 0;}
.cs-test-name {color:#ecf3fb;font-size:.84rem;font-weight:650;}
.cs-test-description {color:#94a4b9;font-size:.78rem;line-height:1.45;}
.cs-test-status {font-size:.74rem;font-weight:700;white-space:nowrap;}
@media (max-width:850px) {
  .block-container {padding:1rem 1rem 2rem;}
  .cs-page-head {align-items:flex-start;flex-direction:column;}
  .cs-hero h1 {font-size:1.85rem !important;}
  .cs-test-row {grid-template-columns:1fr;gap:.35rem;}
}
@media (max-width:520px) {
  .block-container {padding:.8rem .75rem 1.5rem;}
  .cs-hero {padding:1.2rem;}
  .cs-vhead {font-size:1.5rem;}
}
.cs-dashboard-hero {
  min-height:248px; display:flex; flex-direction:column; justify-content:center;
  padding:clamp(1.7rem,3.8vw,3rem); border:1px solid #2b3c58;
  border-left:3px solid #47d9f2; border-radius:18px;
  background:radial-gradient(ellipse at 92% 10%,#30285666 0,transparent 37%),
             linear-gradient(120deg,#111e30 0%,#101827 62%,#15172b 100%);
  box-shadow:0 20px 55px #02071144;
}
.cs-dashboard-eyebrow {
  color:#83dff0; font-size:.72rem; font-weight:750; letter-spacing:.15em;
  text-transform:uppercase; margin-bottom:.8rem;
}
.cs-dashboard-hero h1 {
  color:#f2f6fc; font-size:clamp(2rem,4vw,3.15rem); font-weight:740;
  line-height:1.08; letter-spacing:-.05em; margin:0 0 .9rem; max-width:790px;
}
.cs-dashboard-hero p {color:#aab8ca; font-size:.98rem; line-height:1.7; max-width:710px; margin:0;}
.cs-dashboard-control {
  min-height:140px; display:flex; flex-direction:column;
  justify-content:center; padding:1.35rem; border:1px solid #2b3b53;
  border-radius:16px; background:linear-gradient(155deg,#121d2d,#101725);
}
.cs-engine-status {
  display:flex; align-items:center; gap:.65rem; color:#dce7f3;
  font-size:.76rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
}
.cs-engine-status .cs-live-dot {flex:0 0 9px;}
.cs-engine-caption {color:#91a2b9; font-size:.8rem; line-height:1.5; margin:.6rem 0 1.2rem;}
.cs-demo {border-color:#443e70;background:linear-gradient(105deg,#17172a,#141827);border-radius:11px;}
.cs-demo-mark {color:#a99bff;font-size:.95rem;line-height:1.4;}
[data-testid="stAppViewContainer"]:has(.cs-dashboard-hero) [data-testid="stMetric"] {
  min-height:116px; padding:1.15rem 1.25rem; border-radius:14px;
  background:linear-gradient(145deg,#121d2c,#0f1725); border-color:#283850;
  box-shadow:0 10px 28px #02071126;
}
[data-testid="stAppViewContainer"]:has(.cs-dashboard-hero) [data-testid="stMetricLabel"] {color:#9baac0 !important;font-size:.76rem !important;}
[data-testid="stAppViewContainer"]:has(.cs-dashboard-hero) [data-testid="stMetricValue"] {color:#edf5fc;font-size:1.85rem;font-weight:720;}
[data-testid="stAppViewContainer"]:has(.cs-dashboard-hero) [data-testid="stVerticalBlockBorderWrapper"]:has(.cs-tl) {
  padding:1rem 1.15rem;border-color:#27374d !important;border-radius:14px;background:#0e1725;
}
.cs-lab-intro {
  margin:.25rem 0 1.15rem; padding:1rem 1.15rem; border:1px solid #293a52;
  border-radius:12px; background:linear-gradient(110deg,#111d2d,#111725);
}
.cs-lab-intro strong {display:block;color:#e8f1fb;font-size:.92rem;margin-bottom:.25rem;}
.cs-lab-intro span {display:block;color:#99a9bf;font-size:.81rem;line-height:1.55;}
.cs-scenario-card,
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-scenario-description) {
  height:100%; min-height:158px; padding:.9rem 1rem 1rem;
  border:1px solid #29384f; border-radius:13px;
  background:linear-gradient(155deg,#121d2c,#0f1725);
  box-shadow:0 10px 24px #02071122;
}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-risk-tag[data-risk="safe"]) {border-top:2px solid #41d695 !important;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-risk-tag[data-risk="suspicious"]) {border-top:2px solid #f2b84b !important;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-risk-tag[data-risk="blocked"]) {border-top:2px solid #ff6677 !important;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-risk-tag[data-risk="custom"]) {border-top:2px solid #a393ff !important;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-scenario-description) .stButton {margin-bottom:.38rem;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-scenario-description) .stButton > button {
  justify-content:flex-start; min-height:2.55rem; padding:.4rem .1rem;
  border:0; background:transparent; color:#edf4fc; font-size:.93rem;
  font-weight:700; text-align:left; box-shadow:none;
}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-scenario-description) .stButton > button:hover {background:transparent;color:#68def0;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.cs-scenario-description) .stButton > button[kind="primary"] {color:#6fe1f2;}
.cs-scenario-description {min-height:2.7rem;color:#a1afc2;font-size:.79rem;line-height:1.5;}
.cs-scenario-tags {display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;margin-top:.7rem;}
.cs-risk-tag,.cs-category-tag {
  display:inline-flex;align-items:center;padding:.22rem .5rem;border-radius:999px;
  font-size:.65rem;font-weight:750;letter-spacing:.065em;text-transform:uppercase;
}
.cs-risk-tag[data-risk="safe"] {color:#66e3aa;border:1px solid #31876588;background:#123026;}
.cs-risk-tag[data-risk="suspicious"] {color:#f4c664;border:1px solid #a17c3688;background:#302819;}
.cs-risk-tag[data-risk="blocked"] {color:#ff8793;border:1px solid #a44c5a88;background:#321b26;}
.cs-risk-tag[data-risk="custom"] {color:#b6a8ff;border:1px solid #695bb688;background:#241f39;}
.cs-category-tag {color:#a7b6cb;border:1px solid #34445a;background:#192335;}
.cs-lab-section {margin:1.45rem 0 .45rem;color:#e8f0fa;font-size:.98rem;font-weight:680;letter-spacing:-.015em;}
.cs-lab-caption {color:#93a3b8;font-size:.79rem;line-height:1.5;margin:0 0 .65rem;}
.cs-lab-result {margin-top:1.35rem;padding-top:1.1rem;border-top:1px solid #27364b;}
.cs-lab-result h3 {font-size:1.05rem !important;color:#eaf2fc;margin:0 0 .65rem;}
.cs-verdict {box-shadow:0 12px 30px #02071124;}
.cs-analysis-verdict {
  position:relative;overflow:hidden;padding:1.65rem 1.8rem;margin:.5rem 0 1.25rem;
  border:1px solid;border-radius:16px;background:#111a28;box-shadow:0 16px 38px #02071133;
}
.cs-analysis-verdict:after {
  content:"";position:absolute;inset:0 auto 0 0;width:4px;background:var(--verdict-color);
  box-shadow:0 0 24px var(--verdict-color);
}
.cs-analysis-kicker {font-size:.7rem;font-weight:750;letter-spacing:.15em;text-transform:uppercase;margin-bottom:.6rem;}
.cs-analysis-title {font-size:clamp(2rem,4vw,3rem);font-weight:780;line-height:1.05;letter-spacing:-.05em;color:#f5f8fc;}
.cs-analysis-definition {font-size:.98rem;line-height:1.55;color:#c2cede;margin-top:.55rem;max-width:760px;}
.cs-analysis-meta {font-size:.75rem;color:#91a1b7;margin-top:.95rem;}
.cs-analysis-section-title {font-size:1rem;font-weight:720;color:#edf3fb;margin:1.4rem 0 .55rem;letter-spacing:-.015em;}
.cs-analysis-copy {color:#bac7d8;font-size:.88rem;line-height:1.6;}
.cs-analysis-reason {padding:.72rem .85rem;margin:.45rem 0;border:1px solid #29384d;border-radius:10px;background:#101827;color:#dce6f2;font-size:.84rem;line-height:1.5;}
.cs-analysis-rule {padding:.75rem .85rem;margin:.45rem 0;border-left:2px solid #a393ff;border-radius:0 9px 9px 0;background:#141829;}
.cs-analysis-rule strong {display:block;color:#e8e4ff;font-size:.84rem;margin-bottom:.2rem;}
.cs-analysis-rule span {display:block;color:#9daac0;font-size:.78rem;line-height:1.5;}
.cs-action-list {padding:.85rem 1rem;border:1px solid #29384d;border-radius:11px;background:#101827;height:100%;}
.cs-action-list h4 {font-size:.79rem !important;text-transform:uppercase;letter-spacing:.09em;color:#9aaac0;margin:.05rem 0 .65rem;}
.cs-action-row {padding:.42rem 0;border-bottom:1px solid #253247;color:#d9e3ef;font-size:.8rem;line-height:1.4;}
.cs-action-row:last-child {border-bottom:0;}
.cs-action-state {font-size:.67rem;font-weight:750;text-transform:uppercase;letter-spacing:.05em;margin-right:.35rem;}
.cs-next-step {padding:.9rem 1rem;border:1px solid #31435b;border-radius:11px;background:linear-gradient(110deg,#121f30,#14182a);color:#d9e5f2;font-size:.85rem;line-height:1.55;}
.cs-next-step strong {color:#eff5fb;}
[data-testid="stAppViewContainer"]:has(.cs-analysis-verdict) [data-testid="stExpander"] {
  margin-top:.8rem;border-radius:11px;border-color:#29384d;background:#0e1725;
}
@media (max-width:900px) {
  .cs-dashboard-hero {min-height:220px;}
}
@media (max-width:700px) {
  .cs-dashboard-hero {min-height:0;}
  .cs-dashboard-hero {padding:1.5rem;}
  .cs-dashboard-hero h1 {font-size:2rem;}
}
</style>
"""


# --------------------------------------------------------------------------- #
# Small UI helpers
# --------------------------------------------------------------------------- #
def esc(x) -> str:
    return _html.escape(str(x), quote=True)


def md_html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def wide(fn, *args, **kwargs):
    """Full-width widgets across Streamlit versions (newer: width='stretch', older: use_container_width)."""
    try:
        return fn(*args, width="stretch", **kwargs)
    except Exception:
        return fn(*args, use_container_width=True, **kwargs)


def pill(status: str) -> str:
    icon, label, color = STATUS_STYLE.get(status, ("•", status, "#9fb0c7"))
    return f'<span class="cs-pill" style="color:{color};border-color:{color}77;background:{color}1a">{icon} {esc(label)}</span>'


def inr(x) -> str:
    return f"₹{float(x):,.0f}"


def when(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return str(iso)


@st.cache_resource
def get_db() -> Database:
    return Database()


MONEY_KEYS = {"refund_amount", "refund_limit", "excess_amount", "total_refunded", "covers_up_to", "requested_amount", "amount"}
COLUMN_NAMES = {"transaction_id": "Transaction", "original_transaction_id": "Order", "account_ref": "Account",
                "employee_id": "Employee", "amount": "Amount", "type": "Type", "status": "Status", "days_ago": "Days ago",
                "case_id": "Case", "decision": "Decision", "outcome": "Outcome", "date": "Date"}


def pretty_key(k: str) -> str:
    return k.replace("_", " ").capitalize()


def fmt_value(k: str, v) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if v is None or v == "":
        return "—"
    if k in MONEY_KEYS and isinstance(v, (int, float)):
        return inr(v)
    if isinstance(v, list):
        return ", ".join(str(i) for i in v) if v else "None"
    return str(v)


def render_evidence(evidence: dict) -> None:
    facts, tables = [], []
    for k, v in evidence.items():
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            tables.append((k, v))
        elif isinstance(v, list) and k == "applicable_rules":
            tables.append((k, [{"Rule": r} for r in v]))
        else:
            facts.append((pretty_key(k), fmt_value(k, v).replace("|", "/")))
    if facts:
        st.markdown("| Fact | Value |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in facts))
    for k, rows in tables:
        df = pd.DataFrame(rows)
        if "amount" in df:
            df["amount"] = df["amount"].map(inr)
        df = df.rename(columns=COLUMN_NAMES)
        wide(st.dataframe, df, hide_index=True)
    if not facts and not tables:
        st.caption("No additional evidence for this step.")


# --------------------------------------------------------------------------- #
# Core actions
# --------------------------------------------------------------------------- #
def run_case(raw: dict) -> None:
    db = get_db()
    try:
        report = investigate_transaction(raw, db.get_history(), POLICY, db.get_approvals(), db.list_memory())
    except InvalidTransaction as exc:
        st.session_state.flash = ("error", f"Could not start the investigation: {exc}")
        return
    if st.session_state.get("narrator") and llm_assist.available():
        report["ai_summary"] = llm_assist.narrate(report)
    db.register_case(report)
    db.save_case(report)
    st.session_state.report = report


def go_to_simulator() -> None:
    st.session_state.page = P_CHECK


def flash() -> None:
    msg = st.session_state.pop("flash", None)
    if msg:
        {"error": st.error, "success": st.success, "warning": st.warning}.get(msg[0], st.info)(msg[1])


# --------------------------------------------------------------------------- #
# Progress steps  (Describe -> Read the result -> Take action)
# --------------------------------------------------------------------------- #
def render_stepper(r: dict | None) -> None:
    if r is None:
        states = ["on", "", ""]
    elif r.get("action_status") == "EXECUTED":
        states = ["done", "done", "done"]
    else:
        states = ["done", "done", "on"]
    labels = ["Describe the refund", "Read the result", "Take action"]
    items = "".join(
        f'<div class="s {st_}"><span class="n">{"✓" if st_ == "done" else i + 1}</span>{lab}</div>'
        for i, (lab, st_) in enumerate(zip(labels, states)))
    md_html(f'<div class="cs-steps">{items}</div>')


# --------------------------------------------------------------------------- #
# Step 2: the result, in plain words
# --------------------------------------------------------------------------- #
def render_verdict(r: dict) -> None:
    decision = r["decision"]
    icon, _, color, _, _ = DECISION_STYLE[decision]
    tx = r["transaction"]
    outcome = {
        "ALLOW": ("SAFE", "Checks passed. This action may proceed.", "#41d695"),
        "ESCALATE": ("NEEDS REVIEW", "A person must review this action before it proceeds.", "#f2b84b"),
        "BLOCK": ("BLOCKED", "A required rule failed. This action cannot proceed.", "#ff6677"),
    }[decision]
    label, summary, color = outcome
    active = [step for step in r["steps"] if not step.get("resolved_by")]
    issue = next((step for step in active if step["status"] == "VIOLATION"), None)
    if issue is None and decision == "ESCALATE":
        issue = next((step for step in active if step["status"] in ("FLAG", "ERROR")), None)
    unsafe_count = 1 if decision == "BLOCK" else 0
    problem = issue["summary"] if issue else "No unsafe action was identified by the completed checks."
    problem_check = issue["title"] if issue else "All required checks passed"
    stopped = [ACTIONS[action]["label"] for action in ACTIONS if action not in r["allowed_actions"]]
    stopped_text = ", ".join(stopped) if stopped else "No actions were stopped."
    prevented = ("Release was blocked." if decision == "BLOCK" else
                 "Release is on hold until reviewed." if decision == "ESCALATE" else
                 "No action was blocked by the checks.")

    st.markdown(
        f'<div class="cs-verdict" style="border-color:{color}99;background:{color}12">'
        f'<div class="cs-vmain"><div class="cs-vlabel" style="color:{color};border-color:{color}88;background:{color}18">{icon} {label}</div>'
        f'<div class="cs-vhead">{label}</div><div class="cs-vline">{esc(summary)}</div>'
        f'<div class="cs-vmeta">Case {esc(r["case_id"])} · Demonstration request {esc(tx["transaction_id"])}</div></div></div>',
        unsafe_allow_html=True,
    )
    a, b = st.columns(2)
    a.metric("Confirmed unsafe actions", unsafe_count,
             help="Review signals are not counted as confirmed unsafe actions.")
    b.metric("Actions stopped", len(stopped))
    md_html(
        '<div class="cs-result-grid">'
        f'<div class="cs-result-item"><span>What happened</span><strong>Refund request for {inr(tx["amount"])} '
        f'from {esc(ROLE_LABEL.get(tx["employee_role"], tx["employee_role"]))}</strong></div>'
        f'<div class="cs-result-item"><span>Policy check</span><strong>{esc(problem_check)}</strong></div>'
        f'<div class="cs-result-item"><span>Why</span><strong>{esc(problem)}</strong></div>'
        f'<div class="cs-result-item"><span>What ChainShield did</span><strong>{esc(prevented)}</strong></div>'
        f'<div class="cs-result-item"><span>Actions stopped by policy</span><strong>{esc(stopped_text)}</strong></div>'
        '<div class="cs-result-item"><span>Dependent actions</span><strong>Not configured in this single-request demonstration.</strong></div>'
        '</div>'
    )


def render_security_analysis_result(report: dict) -> None:
    decision = report["decision"]
    styles = {
        "ALLOW": ("SAFE", "Action satisfies configured policies.", "#41d695"),
        "ESCALATE": ("NEEDS REVIEW", "Suspicious or uncertain activity requires human review.", "#f2b84b"),
        "BLOCK": ("BLOCKED", "A mandatory security policy has been violated.", "#ff6677"),
    }
    label, definition, color = styles[decision]
    transaction = report["transaction"]
    role = ROLE_LABEL.get(transaction.get("employee_role"), "Unknown role")

    md_html(
        f'<section class="cs-analysis-verdict" style="--verdict-color:{color};border-color:{color}88;'
        f'background:linear-gradient(110deg,{color}12,#111a28 58%,#17172a)">'
        f'<div class="cs-analysis-kicker" style="color:{color}">Security decision</div>'
        f'<div class="cs-analysis-title">{label}</div>'
        f'<div class="cs-analysis-definition">{definition}</div>'
        f'<div class="cs-analysis-meta">Policy decision for this refund workflow</div>'
        '</section>'
    )

    st.markdown('<div class="cs-analysis-section-title">1 · What happened?</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="cs-analysis-copy">{esc(role)} submitted a refund request for a pre-execution policy check.</div>',
        unsafe_allow_html=True,
    )

    decision_tools = {
        "verify_employee_role", "check_refund_limit", "check_approval_evidence",
        "detect_duplicate_transaction", "inspect_transaction_history",
    }
    driving_steps = [step for step in report.get("steps", [])
                     if not step.get("resolved_by") and step.get("status") in ("VIOLATION", "FLAG", "ERROR")]
    if not driving_steps and decision == "ALLOW":
        driving_steps = [step for step in report.get("steps", [])
                         if step.get("tool") in decision_tools and
                         (step.get("status") == "PASS" or step.get("resolved_by"))]

    st.markdown('<div class="cs-analysis-section-title">2 · Why did ChainShield decide this?</div>',
                unsafe_allow_html=True)
    if decision == "ALLOW":
        why_summary = "All completed policy checks passed; no unresolved review signal remains."
    elif decision == "ESCALATE":
        signal_titles = list(dict.fromkeys(step["title"] for step in driving_steps))
        why_summary = ("Review signals were found in: " + "; ".join(signal_titles) + "."
                       if signal_titles else "A check returned uncertain or incomplete evidence that needs human review.")
    else:
        violation = next((step for step in driving_steps if step.get("status") == "VIOLATION"), None)
        why_summary = (f"A mandatory rule failed: {violation['title']}."
                       if violation else "A mandatory policy check failed.")
    if decision == "ESCALATE":
        why_summary += " A review signal is not proof of wrongdoing."
    st.markdown(f'<div class="cs-analysis-reason">{esc(why_summary)}</div>', unsafe_allow_html=True)
    if decision == "ESCALATE":
        st.caption("Keep the action on hold until a reviewer checks the evidence.")

    st.markdown('<div class="cs-analysis-section-title">3 · Which security rule was evaluated?</div>',
                unsafe_allow_html=True)
    if driving_steps:
        for step in driving_steps:
            result = "Resolved by approval" if step.get("resolved_by") else step.get("status", "Checked")
            st.markdown(
                f'<div class="cs-analysis-rule"><strong>{esc(step["title"])}'
                f' <span style="display:inline;color:#9e8ef5">· {esc(result)}</span></strong>'
                f'<span>{esc(step.get("why", ""))}</span></div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown('<div class="cs-analysis-rule"><strong>No failed or unresolved security check</strong>'
                    '<span>The completed checks did not identify a policy violation or review signal.</span></div>',
                    unsafe_allow_html=True)

    st.markdown('<div class="cs-analysis-section-title">4 · Which actions are allowed or stopped?</div>',
                unsafe_allow_html=True)
    permitted = set(report.get("allowed_actions", []))
    available_actions = [key for key in ACTIONS if key in permitted]
    stopped_actions = [key for key in ACTIONS if key not in permitted]
    allowed_rows = "".join(
        f'<div class="cs-action-row"><span class="cs-action-state" style="color:#58dba1">Available</span>'
        f'{esc(ACTIONS[key]["label"])}</div>' for key in available_actions
    ) or '<div class="cs-action-row">No actions are available.</div>'
    stopped_rows = "".join(
        f'<div class="cs-action-row"><span class="cs-action-state" style="color:#ff7c89">Stopped by policy</span>'
        f'{esc(ACTIONS[key]["label"])}</div>' for key in stopped_actions
    ) or '<div class="cs-action-row">No actions were stopped by policy.</div>'
    left, right = st.columns(2)
    with left:
        md_html(f'<div class="cs-action-list"><h4>Allowed by this decision</h4>{allowed_rows}</div>')
    with right:
        md_html(f'<div class="cs-action-list"><h4>Stopped by this decision</h4>{stopped_rows}</div>')

    st.markdown('<div class="cs-analysis-section-title">5 · What should the reviewer do next?</div>',
                unsafe_allow_html=True)
    recommendation = esc(report.get("recommended_action", "Review the recorded findings."))
    if decision == "ALLOW":
        next_step = f"Policy checks passed. The recorded recommendation is <strong>{recommendation}</strong>. This result does not mean an action has already run."
    elif decision == "ESCALATE":
        next_step = f"Review the findings before taking action. The recorded recommendation is <strong>{recommendation}</strong>. Suspicious signals are not proof of wrongdoing."
    else:
        next_step = f"Do not release this request or override the mandatory rule. The recorded recommendation is <strong>{recommendation}</strong>."
    if report.get("action_status") == "EXECUTED":
        action_key = report.get("action_taken")
        action_label = ACTIONS.get(action_key, {}).get("label", "An action")
        verification = "verified" if report.get("verification_status") == "VERIFIED" else "not verified"
        next_step += f" A separate recorded action was {esc(action_label)}; its outcome was {verification}."
    md_html(f'<div class="cs-next-step">{next_step}</div>')

    with st.expander("Technical evidence and detailed checks"):
        st.caption("Each finding and its evidence are retained in the saved case record.")
        if report.get("reasons"):
            st.markdown("**Detailed decision reasons**")
            for reason in report["reasons"]:
                st.markdown(f"- {reason}")
        render_step_details(report)
    with st.expander("Raw request values"):
        st.json(transaction, expanded=False)
        st.caption(f"Case ID: {esc(report['case_id'])} · Decision: {esc(decision)}")


def render_voice(r: dict) -> None:
    """Spoken briefing: ElevenLabs when a key is set, otherwise (or on failure) the browser's own voice."""
    text = voice.build_briefing(r)
    key = f"voice_on_{r['case_id']}"
    c1, c2 = st.columns([1, 2])
    if c1.button("🔊 Listen to a short summary", key=f"voice_btn_{r['case_id']}"):
        st.session_state[key] = True
    c2.caption("Read aloud in a natural voice." if voice.available() else "Read aloud with your browser's built-in voice.")
    if st.session_state.get(key):
        audio, msg = (None, "")
        if voice.available():
            with st.spinner("Preparing the audio…"):
                audio, msg = voice.synthesize(text)
        if audio:
            try:
                st.audio(audio, format="audio/mp3", autoplay=True)
            except TypeError:
                st.audio(audio, format="audio/mp3")
            st.caption("The summary text was sent to ElevenLabs to create this audio.")
        else:
            if msg:
                st.warning(f"{msg} Using the browser voice instead.")
            st.components.v1.html(voice.browser_voice_html(text), height=60)
        with st.expander("Read the summary text"):
            st.write(text)
        st.caption("The voice only reads the finished findings. It cannot change the decision.")


# --------------------------------------------------------------------------- #
# Step 3: take action
# --------------------------------------------------------------------------- #
def render_action(r: dict) -> None:
    db = get_db()
    st.markdown("### Next step")
    if r.get("action_status") == "EXECUTED":
        render_outcome(r)
        return

    allowed = r["allowed_actions"]
    rec_key = r.get("recommended_action_key")
    if r.get("last_refusal"):
        st.error(f"Not allowed: {r['last_refusal']}")

    action = st.radio("Choose an action", allowed, key=f"act_{r['case_id']}",
                      index=allowed.index(rec_key) if rec_key in allowed else 0,
                      format_func=lambda a: ACTIONS[a]["label"])

    approver, note = None, ""
    with st.form(f"approve_{r['case_id']}"):
        if needs_approval(r, action):
            st.markdown("**Reviewer approval required**")
            c1, c2 = st.columns(2)
            name = c1.text_input("Reviewer name", placeholder="Name")
            roles = ["finance_manager", "risk_officer", "support_agent", "intern"]
            role = c2.selectbox("Your role", roles, format_func=lambda x: ROLE_LABEL[x],
                                help="Only finance managers and risk officers can approve.")
            note = st.text_area("Reason for approval", placeholder="Briefly explain your decision.")
            approver = {"name": name, "role": role}
        else:
            st.caption("No reviewer needed for this action.")
        fail = False
        with st.expander("Demo option"):
            fail = st.checkbox("Simulate a save failure")
        submitted = wide(st.form_submit_button, "Confirm action", type="primary")
    if submitted:
        updated = execute_action(r, action, db, approver, note, simulate_failure=fail)
        st.session_state.report = updated
        db.save_case(updated)
        st.rerun()
    st.caption("Only simulated records are changed. ChainShield checks the saved result.")


def render_outcome(r: dict) -> None:
    ok = r["verified"]
    if ok:
        st.success("Action completed and verified.")
    else:
        st.error("The change could not be verified. Case remains open for review.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Before", r["before_status"])
    c2.metric("Expected", r["expected_status"])
    c3.metric("Verified", r["after_status"])
    if r.get("approval"):
        a = r["approval"]
        st.caption(f"Approved by {a['name']} ({ROLE_LABEL.get(a['role'], a['role'])}): “{a['note']}”")


# --------------------------------------------------------------------------- #
# Everything else, behind "More details"
# --------------------------------------------------------------------------- #
def render_checklist(r: dict) -> None:
    if r.get("stopped_early"):
        st.warning(r["stopped_early"])
    md_html("".join(
        f'<div class="cs-check"><div class="i">{STATUS_STYLE.get(s["status"], ("•",))[0]}</div>'
        f'<div><div class="q">{esc(s["title"])}</div><div class="a">{esc(s["summary"])}</div></div></div>'
        for s in r["steps"]))
    md_html(" ".join(pill(s) for s in ["PASS", "FLAG", "VIOLATION", "INFO"]))
    st.caption("“Looks suspicious” means a person should look. It is not proof of fraud. “Rule broken” means a mandatory rule was clearly violated.")
    if r["score_breakdown"]:
        st.markdown("**How the risk score was worked out**")
        for b in r["score_breakdown"]:
            tail = "resolved by approval" if b["resolved"] else f"+{b['points']}"
            md_html(f'<div class="cs-brow"><div class="l">{esc(b["label"])}</div>'
                    f'<div class="t"><div style="width:{b["points"]}%"></div></div><div class="p">{tail}</div></div>')
        st.caption(f"Score {r['risk_score']} out of 100. It ranks which cases to look at first. It is not a fraud probability.")


def render_step_details(r: dict) -> None:
    st.caption("Each check, why it was run, and the evidence it found.")
    for s in r["steps"]:
        icon, label, _ = STATUS_STYLE.get(s["status"], ("•", s["status"], ""))
        with st.expander(f"{icon} {s['title']}  ·  {label}", expanded=False):
            st.markdown(f"**Finding:** {s['summary']}")
            st.caption(f"Why it was run: {s['why']}")
            render_evidence(s["evidence"])
    with st.expander("Raw data (for developers)"):
        st.json([{k: v for k, v in s.items() if k not in ("why", "next")} for s in r["steps"]], expanded=False)


def render_more_details(r: dict) -> None:
    with st.expander("Technical evidence", expanded=False):
        t1, t2, t3, t4 = st.tabs(["What was checked", "Evidence", "Connections", "Report & history"])
        with t1:
            render_checklist(r)
        with t2:
            render_step_details(r)
        with t3:
            render_graph(r)
        with t4:
            render_report_tab(r)


def render_result(r: dict) -> None:
    render_verdict(r)
    render_action(r)
    render_more_details(r)
    with st.expander("🔊 Listen to this result"):
        render_voice(r)
    r = render_ask(r)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def page_header(eyebrow: str, title: str, description: str, status: str | None = None) -> None:
    status_html = f'<span class="cs-status">{esc(status)}</span>' if status else ""
    md_html(f'<div class="cs-page-head"><div><div class="cs-eyebrow">{esc(eyebrow)}</div>'
            f'<h1>{esc(title)}</h1><p>{esc(description)}</p></div>{status_html}</div>')


def page_start() -> None:
    db = get_db()
    stats = db.dashboard_stats()
    cases = db.list_cases(500)
    hero, controls = st.columns([3.2, 1.15], gap="medium", vertical_alignment="top")
    with hero:
        md_html(
            '<section class="cs-dashboard-hero">'
            '<div class="cs-dashboard-eyebrow">Autonomous AI · Security operations</div>'
            '<h1>Secure every AI action.<br>Before it executes.</h1>'
            '<p>ChainShield checks agent-generated actions against authorization policies, flags suspicious behavior, '
            'and records evidence before an action can proceed.</p>'
            '</section>'
        )
    with controls:
        md_html(
            '<div class="cs-dashboard-control">'
            '<div class="cs-engine-status"><span class="cs-live-dot"></span> Security engine ready</div>'
            '<div class="cs-engine-caption">Policy checks are active. Decisions are evaluated before execution.</div>'
            '</div>'
        )
        st.button("Analyze Workflow", type="primary", key="dashboard_new_workflow",
                  on_click=go_to_simulator, use_container_width=True)
    md_html(
        '<div class="cs-demo"><span class="cs-demo-mark">◆</span><div>'
        '<strong>Working demonstration · Refund workflows</strong>'
        'ChainShield is a general-purpose security layer for autonomous AI agents. Refund requests are the current '
        'working example in this prototype.</div></div>'
    )
    rate = "—" if stats["workflows_analyzed"] == 0 else f'{stats["safe_rate"]:.0f}%'
    st.markdown('<div class="cs-section">Security overview</div>', unsafe_allow_html=True)
    cols = st.columns(4, gap="medium")
    cols[0].metric("Workflows analyzed", stats["workflows_analyzed"])
    cols[1].metric("Unsafe actions blocked", stats["unsafe_actions_blocked"],
                   help="Recorded action attempts refused by the security gate.")
    cols[2].metric("Policy violations", stats["policy_violations"],
                   help="Recorded checks that found a mandatory rule violation.")
    cols[3].metric("Safe decision rate", rate, help="Share of saved cases classified SAFE.")
    st.markdown('<div class="cs-section">Recent security activity</div>', unsafe_allow_html=True)
    events = db.list_audit_events(limit=5)
    if events:
        case_by_id = {case["case_id"]: case for case in cases}
        with st.container():
            render_activity_feed(events, case_by_id)
    else:
        with st.container():
            st.caption("No security activity has been recorded yet. Analyze a workflow to start the audit trail.")


def decision_text(decision: str) -> str:
    return {"ALLOW": "SAFE", "ESCALATE": "NEEDS REVIEW", "BLOCK": "BLOCKED"}.get(decision, "—")


def render_activity_feed(events: list[dict], case_map: dict, show_case: bool = True) -> None:
    entries = []
    for event in events:
        case = case_map.get(event["case_id"], {})
        label = event["event"].replace("_", " ").capitalize()
        result = decision_text(case.get("decision", ""))
        detail = esc(event.get("detail", ""))
        case_ref = (f'<div class="event-case">{esc(event["case_id"])}'
                    f'{" · " + result if result != "—" else ""}</div>') if show_case else ""
        entries.append(
            f'<div class="e"><b>{esc(label)}</b><br>{detail}'
            f'<div class="ts">{esc(when(event["timestamp"]))}</div>{case_ref}</div>'
        )
    md_html(f'<div class="cs-tl">{"".join(entries)}</div>')


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower())


def choose_scenario(name: str) -> None:
    st.session_state["scenario_choice"] = name


def scenario_risk(expected: str | None) -> tuple[str, str, str]:
    if expected == "ALLOW":
        return "safe", "Safe path", "safe"
    if expected == "ESCALATE":
        return "suspicious", "Suspicious · review", "suspicious"
    if expected == "BLOCK":
        return "blocked", "Policy violation · blocked", "policy-violating"
    return "custom", "Custom · evaluated on analysis", "custom"


def page_investigate() -> None:
    page_header("Workflow lab", "Evaluate an AI-generated action",
                "Test how an AI-agent action is checked against policy before execution.")
    flash()

    r = st.session_state.get("report")

    names = list(SCENARIOS)
    if st.session_state.get("scenario_choice") not in names:
        st.session_state.scenario_choice = names[1]
    scenario = st.session_state["scenario_choice"]
    sc = SCENARIOS[scenario]
    md_html(
        '<div class="cs-lab-intro"><strong>Refund security · working demonstration</strong>'
        '<span>Choose an example to see how ChainShield verifies the request, checks authorization, and decides whether '
        'the action can proceed. The engine evaluates the real policy rules; the cards are not simulated results.</span></div>'
    )
    md_html('<div class="cs-lab-section">Choose a scenario</div>')
    md_html('<div class="cs-lab-caption">The label shows the scenario’s expected policy outcome. Custom requests are classified after analysis.</div>')
    for offset in range(0, len(names), 2):
        card_columns = st.columns(2)
        for column, name in zip(card_columns, names[offset:offset + 2]):
            title, copy, risk = SCENARIO_SUMMARIES[name]
            risk_key, outcome_label, _ = scenario_risk(SCENARIOS[name]["expected"])
            selected_tag = '<span class="cs-category-tag">Selected</span>' if name == scenario else ""
            with column:
                with st.container(border=True):
                    column.button(
                        title, key=f"scenario_pick_{slug(name)}",
                        type="primary" if name == scenario else "secondary",
                        on_click=choose_scenario, args=(name,), use_container_width=True,
                    )
                    md_html(
                        f'<div class="cs-scenario-description">{esc(copy)}</div>'
                        f'<div class="cs-scenario-tags">'
                        f'<span class="cs-risk-tag" data-risk="{risk_key}">{esc(outcome_label)}</span>'
                        f'<span class="cs-category-tag">{esc(risk)}</span>'
                        f'{selected_tag}'
                        f'</div>'
                    )
    with st.expander("Advanced Configuration · transaction and approval fields", expanded=False):
        st.caption("Edit the underlying request fields. These values are passed to the same policy analysis as the example scenarios.")
        d, k = sc["transaction"], slug(scenario)
        with st.form("tx_form"):
            a, b, c = st.columns(3)
            tid = a.text_input("Transaction ID", d["transaction_id"], key=f"tid_{k}", help="The unique ID of this refund request.")
            oid = b.text_input("Original order ID", d["original_transaction_id"], key=f"oid_{k}", help="The order being refunded.")
            acct = c.text_input("Customer account", d["account_ref"], key=f"acct_{k}")
            e, f, g = st.columns(3)
            emp = e.text_input("Employee ID", d["employee_id"], key=f"emp_{k}", help="Who is asking for the refund.")
            roles = ["support_agent", "finance_manager", "intern", "guest", "unknown"]
            role = f.selectbox("Employee role", roles, index=roles.index(d["employee_role"]), key=f"role_{k}",
                               format_func=lambda x: ROLE_LABEL[x])
            amt = g.number_input("Refund amount (₹)", min_value=0.0, value=float(d["amount"]), step=500.0, key=f"amt_{k}",
                                 help=f"Refunds above {inr(POLICY['refund_limit'])} need an approval.")
            apr = st.text_input("Approval reference (optional)", d["approval_ref"], key=f"apr_{k}",
                                help="Try APR-9001 (valid), APR-9002 (only covers ₹25,000) or APR-9003 (revoked).")
            st.caption("Changes apply to this analysis only.")
            if st.form_submit_button("Analyze workflow", type="primary", use_container_width=True):
                run_case({"transaction_id": tid, "original_transaction_id": oid, "account_ref": acct, "employee_id": emp,
                          "employee_role": role, "amount": amt, "approval_ref": apr})
                st.rerun()
    if st.button("Analyze Workflow", type="primary", key="analyze_selected_workflow",
                 use_container_width=True):
        run_case(sc["transaction"])
        st.rerun()

    if r:
        md_html('<div class="cs-lab-result"><h3>Latest security decision</h3></div>')
        render_result(r)



def _queue_question(case_id: str, question: str) -> None:  # button callback
    st.session_state[f"qa_pending_{case_id}"] = question


def render_ask(r: dict) -> dict:
    """Ask the agent about this case by voice or text. Answers come only from the case report.
    Returns the (possibly updated) report, because each question is logged in the audit trail."""
    cid = r["case_id"]
    hist = st.session_state.setdefault(f"qa_hist_{cid}", [])
    just_asked = False
    with st.expander("Ask a question", expanded=False):
        st.caption("Answers use this case’s findings. Questions cannot approve or release refunds.")
        cols = st.columns(3)
        for i, q in enumerate(qa.SUGGESTED):
            cols[i % 3].button(q, key=f"chip_{cid}_{i}", on_click=_queue_question, args=(cid, q))

        pending, via = st.session_state.pop(f"qa_pending_{cid}", None), "suggested question"

        if hasattr(st, "audio_input") and voice.available():
            rec = st.audio_input("🎙️ Record your question", key=f"rec_{cid}")
            if rec is not None:
                data = rec.getvalue()
                digest = hashlib.sha256(data).hexdigest()
                if st.session_state.get(f"qa_lastrec_{cid}") != digest:      # process each recording once
                    st.session_state[f"qa_lastrec_{cid}"] = digest
                    with st.spinner("Transcribing…"):
                        text, msg = voice.transcribe(data)
                    if text:
                        pending, via = text, "voice"
                        st.caption(f"I heard: “{text}”")
                    else:
                        st.warning(f"{msg} You can type your question instead.")
            st.caption("Recording is transcribed by ElevenLabs.")
        elif hasattr(st, "audio_input"):
            st.caption("Voice input needs ElevenLabs. Typing works without it.")
        else:
            st.caption("Microphone input is not available in this Streamlit version.")

        with st.form(f"ask_form_{cid}", clear_on_submit=True):
            typed = st.text_input("Or type a question", placeholder="e.g. Why was this flagged?", max_chars=qa.MAX_QUESTION_CHARS)
            sent = wide(st.form_submit_button, "Ask")
        if sent and typed.strip():
            pending, via = typed, "typed"

        if pending:
            use_llm = llm_assist.answer if (st.session_state.get("narrator") and llm_assist.available()) else None
            res = qa.answer_question(r, pending, llm=use_llm)
            hist.append({**res, "via": via})
            r = qa.log_question(r, res, via)
            st.session_state.report = r
            get_db().save_case(r)
            just_asked = True

        for item in hist:
            with st.chat_message("user"):
                st.write(item["question"] or "(empty)")
            with st.chat_message("assistant"):
                st.write(item["answer"])
                st.caption("Action requests are refused." if item["intent"] == "action" else
                           ("AI phrasing; based on case facts." if item["source"] == "llm" else "Based on case findings."))
        if hist:
            last = hist[-1]["answer"]
            audio = None
            if voice.available():
                audio, _ = voice.synthesize(voice.speakable(last))
            if audio:
                try:
                    st.audio(audio, format="audio/mp3", autoplay=just_asked)
                except TypeError:
                    st.audio(audio, format="audio/mp3")
            else:
                st.components.v1.html(voice.browser_voice_html(voice.speakable(last)), height=60)
    return r


def render_graph(r: dict) -> None:
    g = r["graph"]
    st.markdown(f"**{g['linked_transactions']}** related transaction(s)")
    md_html('<div class="cs-flow">'
            '<div style="border-color:#2dd4bf;color:#2dd4bf">🟩 This request</div>'
            '<div style="border-color:#fbbf24;color:#fbbf24">🟧 Earlier refund on the same order / account</div>'
            '<div style="border-color:#f87171;color:#f87171">🟥 Same transaction ID used before</div>'
            '<div style="border-color:#60a5fa;color:#60a5fa">⬭ Account · ⬡ Order · ▭ Employee</div></div>')
    wide(st.graphviz_chart, graph_to_dot(g))
    st.caption("Connections provide context, not proof of wrongdoing.")


def render_timeline(events: list[dict]) -> None:
    items = "".join(
        f'<div class="e"><b>{EVENT_ICON.get(e["event"], "·")} &nbsp;'
        f'{esc(e["event"].replace("_", " ").capitalize())}</b> — {esc(e["detail"])}'
        f'<div class="ts">{esc(when(e["timestamp"]))}</div>'
        f'<div class="event-case">{esc(e.get("case_id", ""))}</div></div>' for e in events)
    md_html(f'<div class="cs-tl">{items}</div>')


def render_report_tab(r: dict) -> None:
    c1, c2 = st.columns(2)
    c1.download_button("⬇️ Download report (Markdown)", report_to_markdown(r), f"{r['case_id']}.md", "text/markdown")
    c2.download_button("⬇️ Download full case (JSON)", json.dumps(r, indent=2, default=str), f"{r['case_id']}.json", "application/json")
    st.markdown("##### What happened on this case")
    render_timeline(r["audit_events"])
    st.markdown("##### Similar earlier cases")
    mem = next((s for s in r["steps"] if s["tool"] == "find_similar_cases"), None)
    if mem and mem["evidence"].get("similar_cases"):
        wide(st.dataframe, pd.DataFrame(mem["evidence"]["similar_cases"]).rename(columns=COLUMN_NAMES), hide_index=True)
        st.caption("An earlier outcome is background, not evidence about this transaction.")
    else:
        st.caption("No similar earlier cases were looked up or found for this case.")


def selected_report(key: str) -> dict | None:
    db = get_db()
    cases = db.list_cases(500)
    if not cases:
        return None
    active = st.session_state.get("report", {})
    ids = [c["case_id"] for c in cases]
    default = active.get("case_id") if active.get("case_id") in ids else ids[0]
    selected = st.selectbox("Investigation", ids, index=ids.index(default), key=key)
    return db.get_case(selected)


def page_security_analysis() -> None:
    page_header("Security analysis", "Decision and evidence",
                "See the outcome, the rule that shaped it, and which actions remain available.")
    report = selected_report("analysis_case")
    if not report:
        st.info("No saved investigations yet. Run a workflow in Workflow Lab to create an analysis.")
        return

    render_security_analysis_result(report)


def page_dependency_graph() -> None:
    page_header("Dependency graph", "Relationships and action gates",
                "Explore the records this demonstration actually links and the actions permitted by policy.")
    report = selected_report("graph_case")
    if not report:
        st.info("No saved investigations yet. Run a workflow in Workflow Lab to create a relationship graph.")
        return

    if "allow" not in report["allowed_actions"]:
        if report["decision"] == "BLOCK":
            st.error("The release action is blocked. No linked follow-on actions run automatically.")
        else:
            st.warning("The release action is on hold until a reviewer checks the request.")
    else:
        st.success("The release action is available after checks.")
    with st.container(border=True):
        render_graph(report)
    allowed = set(report["allowed_actions"])
    with st.expander("Action permissions"):
        for action, details in ACTIONS.items():
            state = "Available" if action in allowed else "Stopped"
            st.markdown(f"**{state}** · {details['label']}")
    with st.expander("Implemented scope"):
        st.write(
            "This prototype draws relationships to earlier transactions using shared account, order, and employee "
            "records. It evaluates one refund request at a time; chained AI actions and downstream dependency "
            "execution are not implemented."
        )


def page_tests() -> None:
    page_header("Test lab", "Check the examples",
                "Run the existing workflow scenarios and security checks against an isolated test database.")
    if st.button("Run all checks", type="primary"):
        st.session_state.test_rows = testsuite.run_all()
    rows = st.session_state.get("test_rows")
    if not rows:
        with st.container(border=True):
            st.markdown("**Ready to run the test suite**")
            st.caption("Tests use an isolated temporary database. Your saved cases and audit trail are not changed.")
        return
    passed = sum(r["passed"] for r in rows)
    decisions_passed = sum(r["passed"] for r in rows if r["group"] == "Decisions")
    other_count = sum(r["group"] != "Decisions" for r in rows)
    c1, c2, c3 = st.columns(3)
    c1.metric("All checks", f"{passed}/{len(rows)}")
    c2.metric("Workflow examples", f"{decisions_passed}/{len(SCENARIOS)}")
    c3.metric("Additional controls", f"{sum(r['passed'] for r in rows if r['group'] != 'Decisions')}/{other_count}")
    (st.success if passed == len(rows) else st.error)(
        "All checks passed." if passed == len(rows) else "Some checks need attention.")
    scenario_results = {row["name"]: row for row in rows if row["group"] == "Decisions"}
    st.markdown('<div class="cs-section">Workflow scenarios</div>', unsafe_allow_html=True)
    scenario_cards = []
    for name in SCENARIOS:
        result = scenario_results[name]
        short_name, description, category = SCENARIO_SUMMARIES[name]
        expected = decision_text(result["expected"]) if result["expected"] in ("ALLOW", "ESCALATE", "BLOCK") else result["expected"]
        actual = decision_text(result["actual"]) if result["actual"] in ("ALLOW", "ESCALATE", "BLOCK") else result["actual"]
        color, status = ("#49d99b", "PASS") if result["passed"] else ("#ff7180", "FAIL")
        scenario_cards.append(
            f'<div class="cs-test-row"><div><div class="cs-test-name">{esc(short_name)}</div>'
            f'<div class="cs-scenario-meta">{esc(category)} · expected {esc(expected)}</div></div>'
            f'<div class="cs-test-description">{esc(description)}<br>Actual result: {esc(actual)}</div>'
            f'<div class="cs-test-status" style="color:{color}">{status}</div></div>'
        )
    md_html("".join(scenario_cards))
    with st.expander("Additional security checks"):
        other_rows = [row for row in rows if row["group"] != "Decisions"]
        wide(st.dataframe, pd.DataFrame([{
            "Status": "PASS" if row["passed"] else "FAIL",
            "Check": row["name"],
            "Expected": row["expected"],
            "Result": row["actual"],
            "Group": row["group"],
        } for row in other_rows]), hide_index=True)


def page_audit() -> None:
    page_header("Audit trail", "Evidence-backed activity",
                "Search recorded events and export the underlying audit evidence.")
    db = get_db()
    cases = db.list_cases(300)
    events = db.list_audit_events(limit=500)
    if not events:
        st.info("No audit events have been recorded yet. Run a workflow to begin the evidence trail.")
    else:
        case_map = {case["case_id"]: case for case in cases}
        case_ids = ["All cases", *dict.fromkeys(event["case_id"] for event in events)]
        controls = st.columns([2, 1, 1])
        query = controls[0].text_input("Search events", key="audit_search",
                                       placeholder="Case, action, or reason")
        selected_case = controls[1].selectbox("Case", case_ids, key="audit_case_filter")
        decision_filter = controls[2].selectbox(
            "Decision", ["All decisions", "ALLOW", "ESCALATE", "BLOCK"], key="audit_decision_filter"
        )
        filtered = []
        filtered_events = []
        for event in events:
            case = case_map.get(event["case_id"], {})
            if selected_case != "All cases" and event["case_id"] != selected_case:
                continue
            if decision_filter != "All decisions" and case.get("decision") != decision_filter:
                continue
            haystack = " ".join((event["event"], event["detail"], event["case_id"],
                                 case.get("transaction_id", ""), case.get("decision", ""))).lower()
            if query.strip() and query.strip().lower() not in haystack:
                continue
            filtered_events.append(event)
            filtered.append({
                "Timestamp": when(event["timestamp"]),
                "Case": event["case_id"],
                "Activity": event["event"].replace("_", " ").title(),
                "Decision": decision_text(case.get("decision", "")),
                "Reason": event["detail"],
            })
        st.caption(f"{len(filtered)} matching events")
        if filtered:
            audit_df = pd.DataFrame(filtered)
            render_activity_feed(filtered_events, case_map)
            st.download_button("Export CSV", audit_df.to_csv(index=False),
                               "chainshield-audit-events.csv", "text/csv")
        else:
            st.info("No events match these filters.")

        if cases:
            with st.expander("Case details and downloads"):
                pick = st.selectbox("Saved case", [case["case_id"] for case in cases], key="audit_open_case")
                report = db.get_case(pick)
                if report:
                    render_verdict(report)
                    render_timeline(db.list_audit_events(pick))
                    st.download_button("Download report", report_to_markdown(report), f"{pick}.md", key="dl_audit")
                    st.download_button("Download full case", json.dumps(report, indent=2, default=str),
                                       f"{pick}.json", "application/json", key="dl_audit_json")
    with st.expander("Advanced: reset saved cases"):
        st.warning("This deletes saved cases, audit events, and simulated statuses.")
        if st.button("Delete all saved cases"):
            db.clear_cases()
            st.session_state.pop("report", None)
            st.rerun()


def page_settings() -> None:
    page_header("Settings", "Policy and integrations",
                "Configure the rules used by new analyses. Existing investigation records are not changed.")
    current = st.session_state["policy"]
    role_options = list(ROLE_LABEL)
    with st.container(border=True):
        st.markdown("#### Refund demonstration policy")
        st.caption("These controls configure the current refund example. They do not change the underlying security engine.")
        with st.form("policy_settings"):
            left, right = st.columns(2)
            with left:
                refund_limit = st.number_input("Approval required above (₹)", min_value=0.0,
                                               value=float(current["refund_limit"]), step=1000.0)
                repeat_threshold = st.number_input("Refunds before review", min_value=1,
                                                   value=int(current["repeat_refund_threshold"]), step=1)
                allowed_roles = st.multiselect("Who can request refunds", role_options,
                                               default=current["allowed_roles"],
                                               format_func=lambda role: ROLE_LABEL[role])
            with right:
                history_window = st.number_input("Check recent history (days)", min_value=1,
                                                 value=int(current["history_window_days"]), step=1)
                cluster_size = st.number_input("Linked records before review", min_value=1,
                                               value=int(current["cluster_flag_size"]), step=1)
                approver_roles = st.multiselect("Who can approve", role_options,
                                                default=current["approver_roles"],
                                                format_func=lambda role: ROLE_LABEL[role])
            saved = st.form_submit_button("Save policy", type="primary")
    if saved:
        if not allowed_roles or not approver_roles:
            st.error("At least one requester role and one approver role must remain configured.")
        else:
            st.session_state["policy"] = {
                "refund_limit": float(refund_limit),
                "repeat_refund_threshold": int(repeat_threshold),
                "history_window_days": int(history_window),
                "cluster_flag_size": int(cluster_size),
                "allowed_roles": allowed_roles,
                "approver_roles": approver_roles,
            }
            st.success("Policy updated for new investigations.")
            st.rerun()

    with st.container(border=True):
        st.markdown("#### Voice and explanations")
        voice_status = "ElevenLabs voice is available; your key remains private." if voice.available() else \
            "Browser voice fallback is available. ElevenLabs is optional."
        st.markdown(f"**Voice**  ·  {voice_status}")
        narrator_status = "AI wording is available; decisions remain rule-based." if llm_assist.available() else \
            "Rule-based explanations are active. AI wording is optional."
        st.markdown(f"**Explanations**  ·  {narrator_status}")
        if llm_assist.available():
            st.toggle("AI-written explanations", key="narrator",
                      help="Changes wording only. Security decisions stay rule-based.")


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def main() -> None:
    global POLICY
    if "policy" not in st.session_state:
        st.session_state["policy"] = deepcopy(DEFAULT_POLICY)
    if st.session_state.get("page") not in PAGES:
        st.session_state["page"] = P_HOME
    POLICY = deepcopy(st.session_state["policy"])
    md_html(CSS)
    try:
        db = get_db()
        with st.sidebar:
            md_html('<div class="cs-brand"><div class="logo">⬡</div><div><b>ChainShield AI</b><span>Agent action security</span></div></div>')
            st.radio("Navigation", PAGES, key="page", label_visibility="collapsed")
            st.divider()
            st.caption("Policy engine · active")
            st.caption("Prototype example · refunds")

        {
            P_HOME: page_start,
            P_CHECK: page_investigate,
            P_ANALYSIS: page_security_analysis,
            P_GRAPH: page_dependency_graph,
            P_HIST: page_audit,
            P_TEST: page_tests,
            P_SETTINGS: page_settings,
        }[st.session_state.page]()
    except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        st.error(f"SQLite database error: {exc}")
        st.caption("No dashboard metrics or case records are being substituted. Check that the local database file is accessible and restart the app.")


main()
