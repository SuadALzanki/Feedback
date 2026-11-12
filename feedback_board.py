import base64
import csv
import html
import io
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit_autorefresh import st_autorefresh
from better_profanity import profanity

# ---------- CONFIG ----------
st.set_page_config(page_title="AUM Certificate Feedback Wall", layout="wide")

DB_PATH = Path(__file__).parent / "feedback.db"   # local fallback only
DISPLAY_REFRESH_MS = 7000  # how often the booth screen advances (7 seconds)
KUWAIT_TZ = ZoneInfo("Asia/Kuwait")
SHEET_HEADERS = ["id", "certificate_name", "student_name", "feedback_text", "created_at"]

# Put your logo file next to this script with this exact filename.
LOGO_PATH = Path(__file__).parent / "aum_logo.png"

profanity.load_censor_words()

# Hide the little link icon Streamlit adds next to headings (it pushes titles off-centre)
st.markdown(
    "<style>[data-testid='stHeaderActionElements'], h1 a, h2 a {display:none !important;}</style>",
    unsafe_allow_html=True,
)


def _secret(key, default=None):
    """Read a Streamlit secret without crashing when no secrets are set."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


# Permanent storage = Google Sheet (set in Streamlit Secrets).
# Without those secrets the app falls back to a local SQLite file, which is
# fine for testing on your laptop but is WIPED on Streamlit Cloud restarts.
USE_SHEETS = bool(_secret("gcp_service_account")) and bool(_secret("SHEET_URL"))


# ---------- LOGO ----------
def render_logo(width=260):
    """Shows the university logo, perfectly centered, if the file has been
    placed next to this script. Silently skips it otherwise, so the app
    never crashes just because the logo hasn't been added yet.

    Embedded as a base64 <img> inside a centered flex div instead of
    st.image()/st.columns(), since Streamlit's column layout does not
    reliably center images within a column."""
    if LOGO_PATH.exists():
        b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode()
        st.markdown(
            f"""
            <div style="display:flex; justify-content:center; margin-bottom:8px;">
                <img src="data:image/png;base64,{b64}" style="width:{width}px; max-width:90%; height:auto; transform:translateX(-5%);">
            </div>
            """,
            unsafe_allow_html=True,
        )


# ---------- STORAGE: GOOGLE SHEETS (permanent) ----------
@st.cache_resource(show_spinner=False)
def get_worksheet():
    import gspread

    gc = gspread.service_account_from_dict(dict(_secret("gcp_service_account")))
    ws = gc.open_by_url(_secret("SHEET_URL")).sheet1
    if not ws.row_values(1):
        ws.append_row(SHEET_HEADERS)
    return ws


# ---------- STORAGE: SQLITE (local testing only) ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            certificate_name TEXT NOT NULL,
            student_name TEXT,
            feedback_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


# ---------- DATA FUNCTIONS (same interface for both storages) ----------
def add_feedback(certificate_name, student_name, feedback_text):
    cert = certificate_name.strip()
    name = (student_name or "").strip()
    text = feedback_text.strip()
    created_at = datetime.now(KUWAIT_TZ).strftime("%Y-%m-%d %H:%M:%S")

    if USE_SHEETS:
        # RAW = store exactly as typed (stops students injecting sheet formulas)
        get_worksheet().append_row(
            [uuid.uuid4().hex[:10], cert, name, text, created_at],
            value_input_option="RAW",
        )
    else:
        conn = get_conn()
        conn.execute(
            "INSERT INTO feedback (certificate_name, student_name, feedback_text, created_at) "
            "VALUES (?, ?, ?, ?)",
            (cert, name, text, created_at),
        )
        conn.commit()
        conn.close()
    get_all_feedback.clear()


@st.cache_data(ttl=5, show_spinner=False)
def get_all_feedback():
    """Returns a list of (id, certificate, name, feedback, created_at), oldest first."""
    if USE_SHEETS:
        rows = get_worksheet().get_all_values()[1:]  # skip header
        rows = [tuple((r + [""] * 5)[:5]) for r in rows]
        return [r for r in rows if r[1].strip() and r[3].strip()]

    conn = get_conn()
    rows = conn.execute(
        "SELECT id, certificate_name, student_name, feedback_text, created_at "
        "FROM feedback ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [(str(r[0]),) + tuple(r[1:]) for r in rows]


def delete_feedback(feedback_id):
    if USE_SHEETS:
        ws = get_worksheet()
        ids = ws.col_values(1)
        if feedback_id in ids:
            ws.delete_rows(ids.index(feedback_id) + 1)
    else:
        conn = get_conn()
        conn.execute("DELETE FROM feedback WHERE id = ?", (int(feedback_id),))
        conn.commit()
        conn.close()
    get_all_feedback.clear()


def feedback_csv(entries):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Certificate", "Student", "Feedback", "Submitted"])
    for _, cert, name, text, created_at in entries:
        writer.writerow([cert, name or "Anonymous Student", text, created_at])
    return buf.getvalue().encode("utf-8-sig")  # utf-8-sig so Excel shows Arabic correctly


# ---------- CONTENT FILTER ----------
def is_clean(text):
    return not profanity.contains_profanity(text or "")


# ---------- SUBMIT VIEW (what the QR code opens) ----------
def render_submit():
    render_logo(width=520)

    st.markdown(
        """
        <div style="text-align:center; padding: 10px 0 24px 0;">
            <h1 style="color:#b3423e; margin-bottom:4px;">Share Your Certificate Story</h1>
            <p style="color:#555; font-size:1.05rem;">
                Tell us about a Coursera certificate or guided project you completed.
                Your feedback may appear on our booth screen!
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("feedback_form", clear_on_submit=True):
        certificate_name = st.text_input(
            "Certificate or Guided Project name",
            placeholder="e.g. Google Data Analytics Professional Certificate",
        )
        student_name = st.text_input(
            "Your name (optional)",
            placeholder="Leave blank to stay anonymous",
        )
        feedback_text = st.text_area(
            "Your feedback",
            placeholder="What did you learn? How did it help you?",
            height=140,
        )
        submitted = st.form_submit_button("Submit Feedback", use_container_width=True)

    if submitted:
        if not certificate_name.strip() or not feedback_text.strip():
            st.error("Please fill in the certificate name and your feedback.")
        elif not (
            is_clean(certificate_name) and is_clean(feedback_text) and is_clean(student_name)
        ):
            st.error(
                "Please keep your feedback respectful and appropriate, then try again."
            )
        else:
            try:
                add_feedback(certificate_name, student_name, feedback_text)
                st.success("Thank you! Your feedback will appear on the screen shortly. 🎉")
            except Exception:
                st.error("Sorry, we couldn't save your feedback. Please try again in a moment.")


# ---------- DISPLAY VIEW (the booth screen, styled like branded slides) ----------
# Brand-derived gradients: AUM red/coral and Coursera-style periwinkle blue,
# alternated so slides feel on-brand instead of a random rainbow.
GRADIENTS = [
    "linear-gradient(135deg,#b3423e,#e08b86)",   # deep red -> coral
    "linear-gradient(135deg,#3f5a99,#9fb3dd)",   # deep blue -> periwinkle
    "linear-gradient(135deg,#b3423e,#5b7fc7)",   # red -> blue (both brand colors)
    "linear-gradient(135deg,#8f3532,#b3423e)",   # maroon -> red
    "linear-gradient(135deg,#2f477a,#8fa8d9)",   # navy -> soft blue
]


def render_display():
    st_autorefresh(interval=DISPLAY_REFRESH_MS, key="display_autorefresh")

    render_logo(width=600)

    st.markdown(
        """
        <div style="text-align:center; padding: 6px 0 4px 0;">
            <h1 style="color:#b3423e; font-size:2.4rem; margin-bottom:0;">Student Voices</h1>
            <p style="color:#666; font-size:1.1rem; margin-top:4px;">
                What our students are saying about Coursera certificates &amp; guided projects
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        entries = get_all_feedback()
    except Exception:
        entries = []
    n = len(entries)

    if n == 0:
        st.markdown(
            """
            <div style="text-align:center; margin-top:80px;">
                <h2 style="color:#888;">Waiting for the first feedback...</h2>
                <p style="color:#aaa;">Scan the QR code to share yours!</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    if "fb_index" not in st.session_state:
        st.session_state.fb_index = 0
        st.session_state.fb_last_count = 0

    if n > st.session_state.fb_last_count:
        # a new submission arrived since the last refresh: jump to show it
        st.session_state.fb_index = n - 1
    else:
        st.session_state.fb_index = (st.session_state.fb_index + 1) % n
    st.session_state.fb_last_count = n

    idx = st.session_state.fb_index
    _, cert, name, text, _created_at = entries[idx]
    display_name = name.strip() if name and name.strip() else "Anonymous Student"
    bg = GRADIENTS[idx % len(GRADIENTS)]

    # Escape user-submitted text before it goes into raw HTML below, so a
    # submission can never inject scripts/tags onto the public booth screen.
    cert = html.escape(cert)
    text = html.escape(text)
    display_name = html.escape(display_name)

    st.markdown(
        f"""
        <div style="
            background:{bg};
            border-radius:24px;
            padding:50px 60px;
            margin: 20px auto;
            max-width: 900px;
            box-shadow: 0 20px 45px rgba(0,0,0,0.25);
            animation: fadeIn 0.8s ease-in;
        ">
            <div style="
                display:inline-block;
                background:rgba(255,255,255,0.18);
                color:white;
                padding:8px 20px;
                border-radius:999px;
                font-weight:600;
                font-size:1.1rem;
                margin-bottom: 24px;
            ">🏆 {cert}</div>
            <p style="
                color:white;
                font-size:1.9rem;
                line-height:1.5;
                font-style:italic;
                margin: 10px 0 30px 0;
            ">&ldquo;{text}&rdquo;</p>
            <p style="
                color:rgba(255,255,255,0.9);
                font-size:1.3rem;
                font-weight:700;
                text-align:right;
                margin:0;
            ">&mdash; {display_name}</p>
        </div>
        <style>
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(12px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    if n <= 15:
        dots = "".join("●" if i == idx else "○" for i in range(n))
        st.markdown(
            f"<p style='text-align:center; color:#999; letter-spacing:6px; font-size:1.2rem;'>{dots}</p>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<p style='text-align:center; color:#999; font-size:1.1rem;'>Feedback {idx + 1} of {n}</p>",
            unsafe_allow_html=True,
        )


# ---------- ADMIN VIEW (moderation, delete inappropriate entries) ----------
def render_admin():
    st.title("🔒 Feedback Moderation")

    # Set a real ADMIN_PASSWORD in the app's Streamlit Cloud "Secrets".
    admin_password = _secret("ADMIN_PASSWORD", "changeme123")

    if not st.session_state.get("admin_authed", False):
        pw = st.text_input("Enter admin password", type="password")
        if st.button("Log in"):
            if pw == admin_password:
                st.session_state.admin_authed = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        return

    st.success("Logged in as moderator.")
    if USE_SHEETS:
        st.caption("✅ Storage: Google Sheet (permanent)")
    else:
        st.warning(
            "⚠️ Storage: local file only. Feedback will be LOST when the app restarts. "
            "Add the Google Sheet secrets to make it permanent."
        )

    entries = get_all_feedback()
    st.caption(f"{len(entries)} total feedback entries")

    if not entries:
        st.info("No feedback submitted yet.")
        return

    st.download_button(
        "⬇️ Download all feedback (CSV / Excel)",
        data=feedback_csv(entries),
        file_name=f"aum_feedback_{datetime.now(KUWAIT_TZ):%Y-%m-%d}.csv",
        mime="text/csv",
    )

    for fid, cert, name, text, created_at in reversed(entries):
        display_name = name.strip() if name and name.strip() else "Anonymous Student"
        with st.container(border=True):
            col1, col2 = st.columns([5, 1])
            with col1:
                st.markdown(f"**{cert}** — *{display_name}*")
                st.write(text)
                st.caption(created_at)
            with col2:
                if st.button("Delete", key=f"del_{fid}"):
                    delete_feedback(fid)
                    st.rerun()

    if st.button("Log out"):
        st.session_state.admin_authed = False
        st.rerun()


# ---------- ROUTE ----------
view = st.query_params.get("view", "submit")
if view == "display":
    render_display()
elif view == "admin":
    render_admin()
else:
    render_submit()
