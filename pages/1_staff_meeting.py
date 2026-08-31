"""
Module A — StaffMeetingBuilder (Meeting Prep & Synthesis).

Ported from the existing baseline; refactored to use the shared api_helpers /
llm_prompts modules and the shared session-state Gemini client.

ARD sub-features preserved:
  - Data Aggregation (tagged Outlook + Excel sender/subject config + uploads)
  - Deduplication (prior-month Confluence notes)
  - Privacy & Tone filtering (systemic prompt rules)
  - External Grounding (ZBRA earnings via Google Search)
"""
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from google.genai import types

import config
import llm_prompts as P
from api_helpers import (
    clean_html,
    fetch_emails_from_graph,
    get_confluence_context,
    extract_text_from_file,
)

st.header("🏗️ Staff Meeting: Info Retrieval & Notes")

client = st.session_state.get("gemini_client")
if client is None:
    st.error("Gemini client not initialized. Open the app from the main page (app.py).")
    st.stop()

with st.sidebar:
    st.subheader("StaffMeetingBuilder 🏗️")
    date_range = st.date_input(
        "1. Search timeframe:",
        value=(datetime.today() - timedelta(days=30), datetime.today()),
    )
    config_file = st.file_uploader("2. Upload Config.xlsx", type=["xlsx"])
    prev_notes_url = st.text_input("3. Prior Confluence URL (Optional)")

    st.markdown("**4. Meeting Agenda Outline**")
    agenda_file = st.file_uploader("Upload Agenda Outline (.txt or .docx)", type=["txt", "docx"])

    st.markdown("**5. Additional Context Files**")
    uploaded_files = st.file_uploader(
        "Upload presentations, reports, or data",
        type=["pdf", "docx", "pptx", "xlsx", "csv", "txt"],
        accept_multiple_files=True,
    )
    st.divider()
    include_earnings = st.checkbox("Include Zebra Financial Update (Web Search)", value=True)

if config_file and len(date_range) == 2:
    start_date, end_date = date_range
    if st.button("Build Staff Meeting Content", type="primary"):
        with st.status("🏗️ Processing...", expanded=True) as status:
            search_df = pd.read_excel(config_file)
            prev_context_text = (
                get_confluence_context(prev_notes_url) if prev_notes_url else "None provided."
            )
            agenda_structure_text = extract_text_from_file(agenda_file) if agenda_file else ""

            st.write("Searching Outlook for sources and 'staff meeting include' categories...")
            try:
                raw_emails = fetch_emails_from_graph(search_df, start_date, end_date)
            except Exception as e:
                st.error(f"{e}")
                raw_emails = []

            if raw_emails:
                verification_list = [
                    {
                        "Date": msg.get("receivedDateTime", "")[:10],
                        "Sender": msg.get("sender", {})
                        .get("emailAddress", {})
                        .get("name", "Unknown"),
                        "Subject": msg.get("subject", "No Subject"),
                        "Tags": ", ".join(msg.get("categories", []) or []),
                    }
                    for msg in raw_emails
                ]
                st.subheader("🔍 Source Verification")
                st.table(pd.DataFrame(verification_list))
            else:
                st.warning("No emails found for the current search criteria.")

            email_summary_block = ""
            for msg in raw_emails:
                sender = msg.get("sender", {}).get("emailAddress", {}).get("name", "Unknown")
                subject = msg.get("subject", "No Subject")
                body = clean_html(msg.get("body", {}).get("content", ""))
                cats = ", ".join(msg.get("categories", []) or [])
                cat_info = f"CATEGORIES: {cats}\n" if cats else ""
                email_summary_block += (
                    f"FROM: {sender}\nSUBJECT: {subject}\n{cat_info}CONTENT: {body[:1000]}\n---\n"
                )

            active_tools = None
            earnings_prompt = ""
            if include_earnings:
                earnings_prompt = P.EARNINGS_PROMPT
                active_tools = P.google_search_tool()

            additional_files_text = ""
            gemini_payload = []
            for f in uploaded_files or []:
                ext = f.name.split(".")[-1].lower()
                if ext == "pdf":
                    gemini_payload.append(
                        types.Part.from_bytes(data=f.read(), mime_type="application/pdf")
                    )
                else:
                    extracted = extract_text_from_file(f)
                    additional_files_text += (
                        f"\n\n--- Content from uploaded file: {f.name} ---\n{extracted[:5000]}"
                    )

            prompt_text = P.build_staff_meeting_prompt(
                prev_context_text,
                email_summary_block,
                additional_files_text,
                agenda_structure_text,
                earnings_prompt,
                team_context=config.team_context_block(),
            )
            gemini_payload.insert(0, prompt_text)

            response_text = P.generate(
                client, gemini_payload, tools=active_tools, temperature=0.3
            )
            status.update(label="Build Complete!", state="complete", expanded=False)

        st.divider()
        st.subheader("Proposed Staff Meeting Updates")
        st.markdown(response_text)
        st.download_button("Download Draft", response_text, file_name="staff_meeting_draft.txt")
else:
    st.info("Upload a Config.xlsx and pick a date range in the sidebar to begin.")
