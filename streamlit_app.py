"""Streamlit web interface for PII Redaction & Anonymization Engine."""

import gc
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import docx
import streamlit as st

from detector import PiiDetector
from redactor import PiiRedactor

# -----------------------------------------------------------------------------
# Page Configuration
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="PII Redaction Engine",
    page_icon="📄",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.title("PII Redaction Engine")
st.write("Upload a Microsoft Word (.docx) document.")

# -----------------------------------------------------------------------------
# Model Initialization
# -----------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading detection models...")
def get_engine():
    """Load detector and redactor instances with cached resources."""
    try:
        import spacy
        spacy.load("en_core_web_sm")
    except Exception:
        try:
            from spacy.cli import download
            download("en_core_web_sm")
        except Exception:
            subprocess.run(
                [sys.executable, "-m", "spacy", "download", "en_core_web_sm"],
                check=True,
            )

    detector = PiiDetector("config.yaml")
    redactor = PiiRedactor("config.yaml")
    return detector, redactor


detector, redactor = get_engine()

# -----------------------------------------------------------------------------
# Document Upload & Action
# -----------------------------------------------------------------------------
with st.container(border=True):
    uploaded_file = st.file_uploader(
        "Upload a Microsoft Word document (.docx)",
        type=["docx"],
    )

    if uploaded_file is not None:
        file_size_kb = uploaded_file.size / 1024
        st.caption(f"Selected: **{uploaded_file.name}** ({file_size_kb:.1f} KB)")
        st.write("")
        button_placeholder = st.empty()
        redact_clicked = button_placeholder.button(
            "Redact Document",
            type="primary",
            use_container_width=True,
            key="redact_btn",
        )
    else:
        redact_clicked = False

# -----------------------------------------------------------------------------
# Processing & Results
# -----------------------------------------------------------------------------
if uploaded_file is not None and redact_clicked:
    # Disable the button immediately while processing runs
    button_placeholder.button(
        "Redact Document",
        type="primary",
        use_container_width=True,
        disabled=True,
        key="redact_btn_disabled",
    )

    with st.spinner("Processing document..."):
        start_time = time.time()

        with tempfile.TemporaryDirectory(prefix="st_pii_") as temp_dir:
            in_path = Path(temp_dir) / uploaded_file.name
            out_path = Path(temp_dir) / f"Redacted_{uploaded_file.name}"

            with open(in_path, "wb") as f_in:
                f_in.write(uploaded_file.getbuffer())

            # 1. Native text redaction across paragraphs and tables
            total_redactions, audits = redactor.redact_document(
                docx_path=in_path,
                output_path=out_path,
                detector=detector,
                config_path="config.yaml",
            )

            # 2. Process embedded images if present
            ocr_count = 0
            try:
                gc.collect()
                from ocr_processor import process_embedded_images
                ocr_doc = docx.Document(str(out_path))
                ocr_count = process_embedded_images(ocr_doc, detector)
                if ocr_count > 0:
                    ocr_doc.save(str(out_path))
                    total_redactions += ocr_count
                gc.collect()
            except Exception:
                pass

            elapsed = time.time() - start_time

            # Read redacted file bytes
            with open(out_path, "rb") as f_out:
                redacted_bytes = f_out.read()

        # Results container
        with st.container(border=True):
            st.subheader("Redaction Results")
            st.success(f"Processing completed in {elapsed:.2f} seconds.")

            col1, col2 = st.columns(2)
            col1.metric("Total Redactions", total_redactions)
            col2.metric("Processing Time", f"{elapsed:.2f} s")

            counts = Counter(audit.category for audit in audits)
            if ocr_count > 0:
                counts["IMAGE_OCR"] = ocr_count

            if counts:
                st.write("")
                st.markdown("**Detected Entity Breakdown**")
                breakdown = [
                    {"Entity Category": category, "Occurrences": count}
                    for category, count in sorted(counts.items(), key=lambda x: -x[1])
                ]
                st.dataframe(breakdown, use_container_width=True, hide_index=True)

            st.write("")
            st.download_button(
                label=f"Download Redacted Document",
                data=redacted_bytes,
                file_name=f"Redacted_{uploaded_file.name}",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary",
                use_container_width=True,
            )

# -----------------------------------------------------------------------------
# Footer
# -----------------------------------------------------------------------------
st.divider()
st.caption("PII Redaction Engine • KSH International Limited RHP")
