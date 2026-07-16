#!/usr/bin/env python3
"""
Streamlit frontend for run_eval.py evaluation harness.
Run with: streamlit run eval_dashboard.py
"""
import json
import sys
from pathlib import Path
import streamlit as st
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from eval.run_eval import run_eval, render_markdown, judge_all_pairs
from app import config

st.set_page_config(page_title="Eval Dashboard", layout="wide", initial_sidebar_state="expanded")

st.title("🧪 Evaluation Dashboard")

with st.sidebar:
    st.header("Configuration")
    compare_mode = st.checkbox("Compare mock vs openrouter", value=False)

    backend_options = ["mock", "openrouter"]
    if not compare_mode:
        backend = st.selectbox("Backend", backend_options, index=0)
        backends = [backend]
    else:
        backends = backend_options

    run_judge = False
    if compare_mode:
        run_judge = st.checkbox(
            f"Also run LLM-as-judge ({config.OPENROUTER_JUDGE_MODEL})",
            value=False,
            help="Has a separate, stronger model blind-compare mock vs real-LLM replies per ticket. "
                 "Costs one extra API call per ticket, on top of the openrouter run above.",
        )

    run_button = st.button("▶ Run Evaluation", type="primary", use_container_width=True)

if run_button:
    reports = []

    for backend in backends:
        with st.status(f"Running {backend} backend...", expanded=True) as status:
            try:
                st.write(f"Evaluating against {backend}...")
                report = run_eval(backend)
                reports.append(report)
                status.update(label=f"✅ {backend} complete", state="complete")
            except Exception as e:
                st.error(f"Error running {backend}: {str(e)}")
                status.update(label=f"❌ {backend} failed", state="error")

    if reports:
        st.success(f"✅ Evaluation complete! ({len(reports)} backend(s) ran)")
        st.session_state.reports = reports

    st.session_state.judge_verdicts = None
    if run_judge and len(reports) == 2:
        with st.status(f"Running LLM-as-judge ({config.OPENROUTER_JUDGE_MODEL})...", expanded=True) as status:
            try:
                mock_report = next(r for r in reports if r["backend"] == "mock")
                llm_report = next(r for r in reports if r["backend"] == "openrouter")
                verdicts = judge_all_pairs(mock_report, llm_report)
                st.session_state.judge_verdicts = verdicts
                status.update(label="✅ Judge pass complete", state="complete")
            except Exception as e:
                st.error(f"Error running judge pass: {str(e)}")
                status.update(label="❌ Judge pass failed", state="error")

# Display results if available
if "reports" in st.session_state and st.session_state.reports:
    reports = st.session_state.reports

    # Scorecard
    st.header("📊 Scorecard")
    scorecard_data = []
    for r in reports:
        scorecard_data.append({
            "Backend": r["backend"],
            "Model": r["model"],
            "Auto-resolved %": r["pct_auto_resolved"],
            "Trace Layer %": r["layer_pass_rate"]["trace"],
            "Tool-call Layer %": r["layer_pass_rate"]["tool_call"],
            "Output Layer %": r["layer_pass_rate"]["output"],
            "Overall %": r["overall_pass_rate"],
        })

    df_scorecard = pd.DataFrame(scorecard_data)
    st.dataframe(df_scorecard, use_container_width=True, hide_index=True)

    # Comparison insights
    if len(reports) > 1:
        st.divider()
        col1, col2, col3 = st.columns(3)
        with col1:
            improvement = reports[-1]["overall_pass_rate"] - reports[0]["overall_pass_rate"]
            st.metric("Overall Improvement", f"{improvement:+.1f}%")
        with col2:
            st.metric("Mock Auto-resolve %", f"{reports[0]['pct_auto_resolved']}%")
        with col3:
            st.metric("LLM Auto-resolve %", f"{reports[-1]['pct_auto_resolved']}%")

    # Layer breakdown
    st.header("📈 Layer Pass Rates")
    cols = st.columns(len(reports), gap="large")

    for idx, r in enumerate(reports):
        with cols[idx]:
            st.subheader(r["backend"].title())
            st.metric("Trace", f"{r['layer_pass_rate']['trace']}%")
            st.metric("Tool-call", f"{r['layer_pass_rate']['tool_call']}%")
            st.metric("Output", f"{r['layer_pass_rate']['output']}%")

    # LLM-as-judge results
    if st.session_state.get("judge_verdicts"):
        verdicts = st.session_state.judge_verdicts
        st.header(f"⚖️ LLM-as-Judge ({config.OPENROUTER_JUDGE_MODEL})")
        st.caption("Blinded pairwise comparison -- the judge model isn't told which reply came from mock vs the real LLM.")

        wins = {"mock": 0, "real_llm": 0, "tie": 0}
        for v in verdicts:
            if "winner" in v:
                wins[v["winner"]] = wins.get(v["winner"], 0) + 1

        jcol1, jcol2, jcol3 = st.columns(3)
        jcol1.metric("Real LLM preferred", wins.get("real_llm", 0))
        jcol2.metric("Mock preferred", wins.get("mock", 0))
        jcol3.metric("Tie", wins.get("tie", 0))

        judge_data = []
        for v in verdicts:
            if "error" in v:
                judge_data.append({"ID": v["id"], "Category": v["category"], "Winner": "ERROR", "Mock score": "-", "Real-LLM score": "-", "Reasoning": v["error"]})
            else:
                judge_data.append({
                    "ID": v["id"], "Category": v["category"], "Winner": v["winner"],
                    "Mock score": v["mock_score"], "Real-LLM score": v["real_llm_score"],
                    "Reasoning": v["reasoning"],
                })
        st.dataframe(pd.DataFrame(judge_data), use_container_width=True, hide_index=True)

    # Adversarial cases
    st.header("⚠️ Adversarial / Guardrail Cases")
    for r in reports:
        adv = [t for t in r["ticket_results"] if t["category"] == "adversarial"]
        with st.expander(f"**{r['backend'].upper()}** — {len(adv)} adversarial test(s)", expanded=True):
            for t in adv:
                status_badge = "✅ PASS" if t["all_pass"] else "❌ FAIL"
                st.markdown(f"#### {t['id']} {status_badge}")
                st.markdown(f"**Ticket:** {t['text']}")
                st.markdown(f"**Routed to:** `{t['actual_route']}`")

                check_data = []
                for c in t["checks"]:
                    check_data.append({
                        "Type": c["type"],
                        "Status": "✅" if c["pass"] else "❌",
                        "Layer": c["layer"],
                        "Detail": c["detail"],
                    })
                df_checks = pd.DataFrame(check_data)
                st.dataframe(df_checks, use_container_width=True, hide_index=True)
                st.divider()

    # Per-ticket detail
    st.header("📋 Per-Ticket Results")
    for r in reports:
        with st.expander(f"**{r['backend'].upper()}** — {r['total_tickets']} tickets", expanded=False):
            ticket_data = []
            for t in r["ticket_results"]:
                lp = t["layer_pass"]
                ticket_data.append({
                    "ID": t["id"],
                    "Category": t["category"],
                    "Route": t["actual_route"],
                    "Trace": "✅" if lp["trace"] else "❌",
                    "Tool-call": "✅" if lp["tool_call"] else "❌",
                    "Output": "✅" if lp["output"] else "❌",
                    "Overall": "✅" if t["all_pass"] else "❌",
                })
            df_tickets = pd.DataFrame(ticket_data)
            st.dataframe(df_tickets, use_container_width=True, hide_index=True)

    # Download results
    st.divider()
    st.header("💾 Export Results")
    col1, col2 = st.columns(2)

    with col1:
        json_data = json.dumps(reports, indent=2)
        st.download_button(
            label="📥 Download JSON Report",
            data=json_data,
            file_name=f"eval_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json",
        )

    with col2:
        md_data = render_markdown(reports)
        st.download_button(
            label="📥 Download Markdown Report",
            data=md_data,
            file_name=f"eval_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
            mime="text/markdown",
        )
else:
    st.info("👈 Configure settings in the sidebar and click **Run Evaluation** to start")