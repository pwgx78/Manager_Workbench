"""
ui_placeholders.py — shared rendering for not-yet-built modules.

Lets placeholder pages appear in the navigation (so the final information
architecture is visible) while clearly signalling they're under construction.
"""
import streamlit as st


def under_construction(title, focus, planned=None):
    """Render a standard 'under construction' page for a planned module."""
    st.header(title)
    st.info("🚧 Under construction — this module isn't available yet.")
    if focus:
        st.caption(focus)
    if planned:
        st.markdown("**Planned capabilities**")
        for item in planned:
            st.markdown(f"- {item}")
