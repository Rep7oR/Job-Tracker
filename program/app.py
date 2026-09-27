"""JobSync — Streamlit entrypoint.

Phase 1 foundation: local account creation/sign-in, a per-account profile,
and empty-state shells for the Queue and History views that later phases
will populate. See services/auth.py and services/storage.py for the backing
logic.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import streamlit as st

from services import agent, apply_session, auth, generator, pdf_compiler, storage

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


def _pdf_iframe(pdf_path) -> None:
    """Render a PDF inline via a base64 data-URI iframe (no external viewer)."""
    try:
        data = pdf_path.read_bytes()
    except OSError as exc:
        st.warning(f"Could not read generated PDF: {exc}")
        return
    b64 = base64.b64encode(data).decode("ascii")
    st.markdown(
        f'<iframe src="data:application/pdf;base64,{b64}" '
        'width="100%" height="600" style="border:1px solid #23272e;'
        'border-radius:8px;"></iframe>',
        unsafe_allow_html=True,
    )


def _record_closed_apply_sessions(email: str) -> bool:
    """Check every apply session tied to this account; for each newly-closed
    one, record it into History and remove the corresponding queue entry.
    Returns True if anything changed (so the caller can rerun)."""
    changed = False
    sessions = apply_session.get_sessions_for_email(email)
    recorded = st.session_state.setdefault("_recorded_apply_sessions", set())

    for session_id, session in sessions.items():
        if session.get("status") != "closed" or session_id in recorded:
            continue
        recorded.add(session_id)
        url = session.get("url")
        queue = storage.get_queue(email)
        entry = next((e for e in queue if e.get("url") == url), None)
        if entry is not None:
            prepared = st.session_state.get("_prepared_applications", {}).get(url, {})
            storage.add_to_history(
                email,
                {
                    "title": entry.get("title", ""),
                    "company": entry.get("company", ""),
                    "url": url,
                    "status": "Applied",
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                    "cv_pdf": str(prepared.get("cv_pdf", "")) if prepared.get("cv_pdf") else "",
                    "cover_letter_pdf": (
                        str(prepared.get("cover_letter_pdf", ""))
                        if prepared.get("cover_letter_pdf")
                        else ""
                    ),
                },
            )
            storage.remove_from_queue(email, url)
            changed = True
    return changed


def _render_review_panel(email: str, entry: dict) -> None:
    """The generation -> compile -> preview -> browser-apply flow for one
    queue entry, shown inline once "Prepare application" has been clicked."""
    url = entry.get("url")
    prepared_store = st.session_state.setdefault("_prepared_applications", {})
    prepared = prepared_store.get(url)

    if prepared is None:
        with st.spinner("Drafting tailored CV and cover letter..."):
            profile = {**storage.get_profile(email), "email": email}
            content = generator.generate_application(profile, entry)
        with st.spinner("Compiling to PDF..."):
            compiled = pdf_compiler.compile_application(
                email, url, content["cv_tex"], content["cover_letter_tex"]
            )
        prepared = {**content, **compiled}
        prepared_store[url] = prepared

    source_label = "AI-drafted" if prepared["source"] == "llm" else "template fill from your profile text"
    st.caption(f"Content source: {source_label}")

    if prepared.get("errors"):
        for err in prepared["errors"]:
            st.error(err)
        st.info(
            "PDF compilation needs Tectonic installed on this machine. "
            "The LaTeX source below is still ready to compile elsewhere."
        )

    tab_cv, tab_cover = st.tabs(["CV", "Cover letter"])
    with tab_cv:
        edited_cv = st.text_area(
            "CV LaTeX source", value=prepared["cv_tex"], height=240, key=f"cv_tex_{url}"
        )
        prepared["cv_tex"] = edited_cv
        if prepared.get("cv_pdf"):
            _pdf_iframe(prepared["cv_pdf"])
    with tab_cover:
        edited_cover = st.text_area(
            "Cover letter LaTeX source",
            value=prepared["cover_letter_tex"],
            height=240,
            key=f"cover_tex_{url}",
        )
        prepared["cover_letter_tex"] = edited_cover
        if prepared.get("cover_letter_pdf"):
            _pdf_iframe(prepared["cover_letter_pdf"])

    if st.button("Recompile", key=f"recompile_{url}"):
        with st.spinner("Compiling to PDF..."):
            compiled = pdf_compiler.compile_application(
                email, url, prepared["cv_tex"], prepared["cover_letter_tex"]
            )
        prepared.update(compiled)
        st.rerun()

    st.divider()
    existing_session_id = st.session_state.get("_apply_session_by_url", {}).get(url)
    session = apply_session.get_session(existing_session_id) if existing_session_id else None

    if session and session.get("status") in ("opening", "open"):
        st.info("Browser window open — submit the application there, then close it.")
    elif session and session.get("status") == "closed":
        st.success("Browser window closed. This entry will move to History shortly.")
    else:
        if st.button("Open application in browser", type="primary", key=f"open_browser_{url}"):
            session_id = apply_session.open_application(email, url)
            st.session_state.setdefault("_apply_session_by_url", {})[url] = session_id
            st.rerun()


def render_queue_page(email: str) -> None:
    st.title("Queue")
    st.markdown(
        '<p class="jobsync-subtitle">Postings the agent matched against your '
        "profile. Prepare an application to generate a tailored CV and cover "
        "letter, then open the posting in a real browser window to submit it "
        "yourself.</p>",
        unsafe_allow_html=True,
    )

    if hasattr(st, "fragment"):

        @st.fragment(run_every="3s")
        def _poll_apply_sessions() -> None:
            if _record_closed_apply_sessions(email):
                st.rerun()

        _poll_apply_sessions()
    elif _record_closed_apply_sessions(email):
        st.rerun()

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

    reviewing = st.session_state.get("_reviewing_url")

    for entry in sorted(queue, key=lambda e: e.get("score", 0), reverse=True):
        url = entry.get("url")
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
                if url:
                    st.markdown(f"[View posting]({url})")
            with col_action:
                if st.button("Dismiss", key=f"dismiss_{url}"):
                    storage.remove_from_queue(email, url)
                    st.rerun()
                label = "Close review" if reviewing == url else "Prepare application"
                if st.button(label, key=f"prepare_{url}"):
                    st.session_state["_reviewing_url"] = None if reviewing == url else url
                    st.rerun()

            if reviewing == url:
                st.divider()
                _render_review_panel(email, entry)

            st.markdown("</div>", unsafe_allow_html=True)


def render_history_page(email: str) -> None:
    st.title("History")
    history = storage.get_history(email)
    if not history:
        st.markdown(
            '<div class="jobsync-empty-state">'
            "No applications submitted yet."
            "</div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        '<p class="jobsync-subtitle">Applications recorded once their browser '
        "window was closed.</p>",
        unsafe_allow_html=True,
    )
    for entry in sorted(history, key=lambda e: e.get("applied_at", ""), reverse=True):
        with st.container():
            st.markdown('<div class="jobsync-card">', unsafe_allow_html=True)
            st.markdown(f"**{entry.get('title', 'Untitled role')}**")
            st.markdown(f"{entry.get('company', 'Unknown company')}")
            st.markdown(f"Status: {entry.get('status', 'Applied')}")
            applied_at = entry.get("applied_at", "")
            if applied_at:
                st.markdown(f"Applied: {applied_at}")
            if entry.get("url"):
                st.markdown(f"[View posting]({entry['url']})")
            if entry.get("cv_pdf"):
                st.markdown(f"CV: `{entry['cv_pdf']}`")
            if entry.get("cover_letter_pdf"):
                st.markdown(f"Cover letter: `{entry['cover_letter_pdf']}`")
            st.markdown("</div>", unsafe_allow_html=True)


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
        render_history_page(user)


if __name__ == "__main__":
    main()
