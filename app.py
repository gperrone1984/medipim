import streamlit as st
import pandas as pd
import zipfile
import os
import io
import tempfile
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# ================================
# MedipimAPI (embedded)
# ================================
class MedipimAPI:
    # ... [class definition unchanged from previous version] ...
    # (Keep all methods as they are)
    pass

# ================================
# Streamlit UI
# ================================
st.set_page_config(page_title="Medipim Image Downloader", page_icon="📸", layout="wide")
st.title("📸 Medipim Image Downloader")
st.markdown("Upload a list of product IDs and download 1500x1500 images as a ZIP file.")

# [Sidebar code unchanged]

# [File upload and product list code unchanged]

# [Download settings code up to for-loop unchanged]

        total = len(product_ids)
        for i, pid in enumerate(product_ids, start=1):
            progress.progress(i / total)
            status.text(f"Processing {pid} ({i}/{total})")
            try:
                detail_url = api.search_product(str(pid), search_hint=search_hint if search_hint != "auto" else None)
                if not detail_url:
                    failed.append(f"{pid}: Product not found")
                    continue
                img_url = api.get_image_url(detail_url)
                if not img_url:
                    failed.append(f"{pid}: Image not found")
                    continue
                save_path = os.path.join(temp_dir, f"{re.sub(r'[^A-Za-z0-9_-]+', '_', pid)}.jpg")
                if api.download_image(img_url, save_path):
                    downloaded.append((pid, save_path))
                else:
                    failed.append(f"{pid}: Download error")
            except Exception as e:
                failed.append(f"{pid}: {e}")

        if downloaded:
            status.text("Creating ZIP…")
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for pid, path in downloaded:
                    zf.write(path, arcname=f"{re.sub(r'[^A-Za-z0-9_-]+', '_', pid)}.jpg")
            zip_buf.seek(0)

            with results:
                st.success(f"✅ Done! {len(downloaded)} images downloaded")
                if failed:
                    st.warning(f"⚠️ {len(failed)} failed:")
                    for f in failed:
                        st.write(f"• {f}")
                st.download_button(
                    label="📥 Download ZIP",
                    data=zip_buf.getvalue(),
                    file_name=f"medipim_images_{len(downloaded)}_items.zip",
                    mime="application/zip",
                )

            for _, p in downloaded:
                try:
                    os.remove(p)
                except Exception:
                    pass
            try:
                os.rmdir(temp_dir)
            except Exception:
                pass
            status.text("✅ Completed!")
        else:
            st.error("❌ No image was downloaded successfully")
            if failed:
                for f in failed:
                    st.write(f"• {f}")

st.markdown("---")
st.markdown(
    """**Notes:**

- Credentials are never stored.
- Use **🔐 Test Login** in the sidebar to troubleshoot authentication.
- You can change **Base URL** if Medipim uses a different locale (e.g., /nl/).
- If search can't find your product by ID, paste a direct product URL from Medipim.
"""
)
