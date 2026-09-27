"""JobSync — Streamlit entrypoint.

Phase 1 foundation: local account creation/sign-in, a per-account profile,
and empty-state shells for the Queue and History views that later phases
will populate. See services/auth.py and services/storage.py for the backing
logic.
"""

from __future__ import annotations

import streamlit as st

from services import agent, auth, storage

st.set_page_config(page_title="JobSync", page_icon="\U0001F9ED", layout="wide")

EXPERIENCE_LEVELS = ["Entry level", "Mid level", "Senior", "Lead / Staff", "Manager+"]

# Builds on the accent/background tokens already set in .streamlit/config.toml
# (dark background, red accent) with a small CSS variable block for surfaces
# this page needs that Streamlit's theme config doesn't cover directly.
CSS = """
<style>
:root {
    --jobsync-accent: #ef4444;
    --jobsync-bg: #050505;
    --jobsync-surface: #111418;
    --jobsync-surface-border: #23272e;
    --jobsync-text: #f5f7fa;
    --jobsync-text-muted: #9aa2ad;
}

.jobsync-card {
    background: var(--jobsync-surface);
    border: 1px solid var(--jobsync-surface-border);
    border-radius: 12px;
    padding: 1.5rem 1.75rem;
    margin-bottom: 1rem;
}

.jobsync-empty-state {
    background: var(--jobsync-surface);
    border: 1px dashed var(--jobsync-surface-border);
    border-radius: 12px;
    padding: 2.5rem;
    text-align: center;
    color: var(--jobsync-text-muted);
}

.jobsync-subtitle {
    color: var(--jobsync-text-muted);
    font-size: 0.95rem;
    margin-top: -0.5rem;
    margin-bottom: 1.5rem;
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def render_auth_page() -> None:
    st.title("JobSync")
    st.markdown(
        '<p class="jobsync-subtitle">Sign in or create an account to set up your profile.</p>',
        unsafe_allow_html=True,
    )

    tab_signin, tab_create = st.tabs(["Sign in", "Create account"])

    with tab_signin:
        with st.form("signin_form"):
            email = st.text_input("Email", key="signin_email")
            password = st.text_input("Password", type="password", key="signin_password")
            submitted = st.form_submit_button("Sign in", use_container_width=True)
        if submitted:
            if auth.authenticate(email, password):
                auth.login(email)
                st.rerun()
            else:
                st.error("Incorrect email or password.")

    with tab_create:
        with st.form("create_account_form"):
            email = st.text_input("Email", key="create_email")
            password = st.text_input("Password", type="password", key="create_password")
            confirm = st.text_input(
                "Confirm password", type="password", key="create_confirm"
            )
            submitted = st.form_submit_button("Create account", use_container_width=True)
        if submitted:
            if password != confirm:
                st.error("Passwords do not match.")
            else:
                try:
                    auth.create_account(email, password)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    auth.login(email)
                    st.success("Account created.")
                    st.rerun()


def render_profile_page(email: str) -> None:
    st.title("Profile")
    st.markdown(
        '<p class="jobsync-subtitle">This stands in for your base CV for now — '
        "the agent will match jobs against it once search is built.</p>",
        unsafe_allow_html=True,
    )

    profile = storage.get_profile(email)
    with st.form("profile_form"):
        target_role = st.text_input("Target role", value=profile.get("target_role", ""))
        location = st.text_input("Location", value=profile.get("location", ""))
        current_level = profile.get("experience_level", "")
        level_index = (
            EXPERIENCE_LEVELS.index(current_level)
            if current_level in EXPERIENCE_LEVELS
            else 0
        )
        experience_level = st.selectbox(
            "Experience level", EXPERIENCE_LEVELS, index=level_index
        )
        background = st.text_area(
            "Background",
            value=profile.get("background", ""),
            height=260,
            placeholder=(
                "Paste or describe your work history, skills, and what you're "
                "looking for. This is used to tailor generated applications."
            ),
        )
        submitted = st.form_submit_button("Save profile", use_container_width=True)

    if submitted:
        storage.save_profile(
            email,
            {
                "target_role": target_role.strip(),
                "location": location.strip(),
                "experience_level": experience_level,
                "background": background.strip(),
            },
        )
        st.success("Profile saved.")


def render_queue_page(email: str) -> None:
    st.title("Queue")
    st.markdown(
        '<p class="jobsync-subtitle">Postings the agent matched against your '
        "profile. Dismiss only for now — review, tailoring, and applying "
        "are next.</p>",
        unsafe_allow_html=True,
    )

    if st.button("Run agent now", type="primary"):
        with st.spinner("Fetching postings and matching against your profile..."):
            summary = agent.run_agent_once(email)
        st.success(
            f"Fetched {summary['fetched']} postings, "
            f"{summary['matched']} matched your profile, "
            f"{summary['added']} new added to the queue."
        )

    queue = storage.get_queue(email)
    if not queue:
        st.markdown(
            '<div class="jobsync-empty-state">'
            "No staged matches yet — run the agent to search."
            "</div>",
            unsafe_allow_html=True,
        )
        return

    for entry in sorted(queue, key=lambda e: e.get("score", 0), reverse=True):
        with st.container():
            st.markdown('<div class="jobsync-card">', unsafe_allow_html=True)
            col_info, col_action = st.columns([5, 1])
            with col_info:
                st.markdown(f"**{entry.get('title', 'Untitled role')}**")
                st.markdown(
                    f"{entry.get('company', 'Unknown company')} — "
                    f"{entry.get('location', 'Location unknown')}"
                )
                st.markdown(f"Match score: {entry.get('score', 0)}/100")
                url = entry.get("url")
                if url:
                    st.markdown(f"[View posting]({url})")
            with col_action:
                if st.button("Dismiss", key=f"dismiss_{url}"):
                    storage.remove_from_queue(email, url)
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)


def render_history_page() -> None:
    st.title("History")
    st.markdown(
        '<div class="jobsync-empty-state">'
        "No applications submitted yet."
        "</div>",
        unsafe_allow_html=True,
    )
    # TODO(phase 3): record submissions here when a browser-apply window closes.


def main() -> None:
    user = auth.current_user()
    if not user:
        render_auth_page()
        return

    st.sidebar.markdown(f"Signed in as **{user}**")
    if st.sidebar.button("Log out", use_container_width=True):
        auth.logout()
        st.rerun()
    st.sidebar.divider()

    page = st.sidebar.radio("Navigate", ["Profile", "Queue", "History"])

    if page == "Profile":
        render_profile_page(user)
    elif page == "Queue":
        render_queue_page(user)
    else:
        render_history_page()


if __name__ == "__main__":
    main()
