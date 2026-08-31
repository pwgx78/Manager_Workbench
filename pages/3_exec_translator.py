"""
Module C — The Executive Translator.

Ingests highly technical ME artifacts (root cause analyses, CT scan results,
L3 engineering notes) and SIMULTANEOUSLY reformats them into leadership-ready
"What / So What / Now What" briefs for three distinct audiences. Each brief is
tailored to what its audience cares about, ends with three "Likely Follow-Up
Questions", and can be steered with per-audience prompt instructions. Outlook
can be scrubbed for the subject being summarized to add email context.
"""
from datetime import datetime, timedelta

import streamlit as st
from google.genai import types

import config
import llm_prompts as P
from api_helpers import extract_text_from_file, search_emails_by_subject, clean_html

st.header("🎯 Executive Translator")
st.caption(
    'Translate deep ME engineering content for three leadership audiences at once — '
    'each with its own "What / So What / Now What" brief and likely follow-up questions.'
)

client = st.session_state.get("gemini_client")
if client is None:
    st.error("Gemini client not initialized. Open the app from the main page (app.py).")
    st.stop()

# Leadership audiences translated in parallel. `focus` steers what each brief
# emphasizes and what its follow-up questions probe.
#
# WHO each audience is stays out of source: those are third parties' names, so
# they live in the profile and are set in Settings > Identity. Unset is fine —
# the prompt then targets the role alone.
AUDIENCES = [
    {
        "key": "sr_dir_eng",
        "label": "Senior Director, Engineering",
        "focus": (
            "Engineering rigor and root-cause confidence, cross-program/platform "
            "impact, technical risk, and resourcing. They are technically literate "
            "and want to trust the depth of the analysis."
        ),
    },
    {
        "key": "bu_gm",
        "label": "Business Unit General Manager",
        "focus": (
            "Customer and revenue impact, product-line and P&L risk, time-to-market "
            "and schedule, and competitive exposure. Minimal engineering detail."
        ),
    },
    {
        "key": "svp_emc",
        "label": "SVP, EMC",
        "focus": (
            "Top-level strategic, financial, and reputational risk; whether this is "
            "escalation-worthy; and the single decision or support needed from them. "
            "Wants the headline and the ask, fast."
        ),
    },
]

# Names are profile data, so they are read per run rather than baked in above.
AUDIENCE_NAMES = config.load_audience_names()

# --------------------------------------------------------------------------- #
# Source content
# --------------------------------------------------------------------------- #
technical_text = st.text_area(
    "Paste technical content (RCA, CT scan notes, L3 analysis)",
    height=220,
    key="exec_tech_text",
)
uploaded = st.file_uploader(
    "…or upload an artifact (Word / PDF / PPTX)",
    type=["docx", "pdf", "pptx", "txt", "xlsx", "csv"],
)

# --------------------------------------------------------------------------- #
# Optional: scrub Outlook for related email context
# --------------------------------------------------------------------------- #
with st.expander("📧 Scrub Outlook for related email context (optional)"):
    st.caption(
        "Search your mailbox for the subject being summarized and fold the matching "
        "emails into every translation prompt as extra context."
    )
    scrub_subject = st.text_input("Subject / topic to search for", key="exec_scrub_subject")
    scrub_range = st.date_input(
        "Date range",
        value=(datetime.today() - timedelta(days=30), datetime.today()),
        key="exec_scrub_range",
    )
    if st.button("Scrub Outlook"):
        if not scrub_subject.strip():
            st.warning("Enter a subject or topic to search for.")
        elif not isinstance(scrub_range, (list, tuple)) or len(scrub_range) != 2:
            st.warning("Pick both a start and end date.")
        else:
            try:
                with st.spinner("Searching Outlook..."):
                    msgs = search_emails_by_subject(
                        scrub_subject.strip(), scrub_range[0], scrub_range[1]
                    )
                if not msgs:
                    st.session_state["exec_email_ctx_box"] = ""
                    st.info("No matching emails found.")
                else:
                    block = ""
                    for m in msgs:
                        sender = (
                            m.get("sender", {}).get("emailAddress", {}).get("name", "Unknown")
                        )
                        subj = m.get("subject", "No Subject")
                        date = m.get("receivedDateTime", "")[:10]
                        body = clean_html(m.get("body", {}).get("content", ""))
                        block += (
                            f"FROM: {sender} | {date}\nSUBJECT: {subj}\n"
                            f"CONTENT: {body[:800]}\n---\n"
                        )
                    # Set the widget's value before it is instantiated below.
                    st.session_state["exec_email_ctx_box"] = block
                    st.success(
                        f"Found {len(msgs)} email(s). Review/trim below — this text is "
                        "added to every translation prompt."
                    )
            except Exception as e:
                st.error(f"{e}")
    st.text_area(
        "Scrubbed email context (editable — added to every prompt)",
        key="exec_email_ctx_box",
        height=160,
        placeholder="Run a scrub above, or paste related email context here.",
    )

st.divider()


def _assemble_source():
    """Read the current source widgets into (combined_text, pdf_bytes). PDFs are
    sent to Gemini as a Part; every other format is extracted to text."""
    combined_text = technical_text or ""
    pdf_bytes = None
    if uploaded is not None:
        ext = uploaded.name.split(".")[-1].lower()
        if ext == "pdf":
            pdf_bytes = uploaded.getvalue()
        else:
            combined_text += f"\n\n--- {uploaded.name} ---\n{extract_text_from_file(uploaded)}"
    return combined_text, pdf_bytes


def _translate(audience, combined_text, pdf_bytes, email_context, extra):
    contents = [
        P.build_exec_translator_prompt(
            combined_text,
            audience["label"],
            AUDIENCE_NAMES.get(audience["key"], ""),
            audience["focus"],
            extra_instructions=extra,
            email_context=email_context,
            team_context=config.team_context_block(),
        )
    ]
    if pdf_bytes is not None:
        contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
    return P.generate(client, contents)


# --------------------------------------------------------------------------- #
# Translate all three audiences at once
# --------------------------------------------------------------------------- #
if st.button("Translate for all audiences", type="primary"):
    combined_text, pdf_bytes = _assemble_source()
    if not combined_text.strip() and pdf_bytes is None:
        st.error("Provide some technical text or upload a file first.")
    else:
        email_context = st.session_state.get("exec_email_ctx_box", "")
        results = st.session_state.setdefault("exec_results", {})
        with st.spinner("Translating for all three audiences..."):
            for a in AUDIENCES:
                extra = st.session_state.get(f"exec_extra_{a['key']}", "")
                results[a["key"]] = _translate(
                    a, combined_text, pdf_bytes, email_context, extra
                )
        st.session_state["exec_results"] = results

# --------------------------------------------------------------------------- #
# Per-audience segments — extra-instruction field sits above each one
# --------------------------------------------------------------------------- #
results = st.session_state.get("exec_results", {})
for a in AUDIENCES:
    st.divider()
    st.subheader(a["label"])
    _who = AUDIENCE_NAMES.get(a["key"], "")
    if _who:
        st.caption(f"Audience: {_who}")
    extra = st.text_input(
        "Additional instructions for this translation (optional)",
        key=f"exec_extra_{a['key']}",
        placeholder='e.g. "limit translation to 3 sentences"',
    )
    if st.button(f"Translate / re-translate — {a['label']}", key=f"exec_btn_{a['key']}"):
        combined_text, pdf_bytes = _assemble_source()
        if not combined_text.strip() and pdf_bytes is None:
            st.error("Provide some technical text or upload a file first.")
        else:
            email_context = st.session_state.get("exec_email_ctx_box", "")
            with st.spinner(f"Translating for {a['label']}..."):
                out = _translate(a, combined_text, pdf_bytes, email_context, extra)
            st.session_state.setdefault("exec_results", {})[a["key"]] = out
            results = st.session_state["exec_results"]

    if results.get(a["key"]):
        st.markdown(results[a["key"]])
        st.download_button(
            "Download brief",
            results[a["key"]],
            file_name=f"exec_brief_{a['key']}.md",
            key=f"exec_dl_{a['key']}",
        )
