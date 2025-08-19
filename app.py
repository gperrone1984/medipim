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
    def __init__(self, username: str, password: str, base_url: str = "https://platform.medipim.be/en/", debug: bool = False, cookie_header: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.logged_in = False
        self.debug = debug
        self.last_debug = []
        if cookie_header:
            self.session.headers["Cookie"] = cookie_header

    def _abs(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def _get(self, url: str, **kwargs):
        resp = self.session.get(url, allow_redirects=True, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: dict = None, json: dict = None, **kwargs):
        headers = kwargs.pop("headers", {})
        if json is None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        resp = self.session.post(url, data=data, json=json, allow_redirects=True, timeout=30, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp

    def login(self) -> bool:
        login_url = self._abs("login")
        try:
            resp = self._get(login_url)
        except Exception:
            return False
        try:
            self._post(login_url, data={"_username": self.username, "_password": self.password})
            self.logged_in = True
            return True
        except Exception:
            return False

    def search_product(self, product_id: str) -> str | None:
        if not self.logged_in and not self.login():
            return None
        if isinstance(product_id, str) and product_id.startswith("http"):
            return product_id
        candidates = [
            f"products?search={product_id}",
            f"en/product/{product_id}",
        ]
        for path in candidates:
            try:
                resp = self._get(self._abs(path))
                soup = BeautifulSoup(resp.text, "lxml")
                link = soup.find("a", href=re.compile("/product"))
                if link:
                    return self._abs(link["href"])
            except Exception:
                continue
        return None

    def get_image_url(self, product_detail_url: str) -> str | None:
        if not self.logged_in and not self.login():
            return None
        try:
            resp = self._get(product_detail_url)
            soup = BeautifulSoup(resp.text, "lxml")
            img = soup.find("img", src=re.compile("/media/"))
            if img:
                return self._abs(img["src"])
        except Exception:
            return None
        return None

    def download_image(self, image_url: str, save_path: str) -> bool:
        try:
            resp = self.session.get(image_url, stream=True, timeout=60)
            if resp.status_code == 200:
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(1024 * 64):
                        if chunk:
                            f.write(chunk)
                return True
        except Exception:
            return False
        return False


# ================================
# Streamlit UI
# ================================
st.set_page_config(page_title="Medipim Image Downloader", page_icon="📸", layout="wide")
st.title("📸 Medipim Image Downloader")
st.markdown("Upload a list of product IDs and download 1500x1500 images as a ZIP file.")

st.sidebar.header("Medipim Credentials")
username = st.sidebar.text_input("Username", value="")
password = st.sidebar.text_input("Password", type="password", value="")
base_url = st.sidebar.text_input("Base URL", value="https://platform.medipim.be/en/")

with st.sidebar:
    if st.button("🔐 Test Login", use_container_width=True, disabled=not username or not password):
        api = MedipimAPI(username, password, base_url=base_url)
        ok = api.login()
        if ok:
            st.success("Login successful ✅")
        else:
            st.error("Login failed ❌. Check credentials or try again later.")

col1, col2 = st.columns([1, 1])

with col1:
    st.header("📋 Upload Product List")
    uploaded = st.file_uploader("Upload CSV with product IDs", type=["csv"])
    product_ids: list[str] = []

    if uploaded is not None:
        try:
            df = pd.read_csv(uploaded)
        except Exception:
            uploaded.seek(0)
            df = pd.read_csv(uploaded, sep=";")
        st.write("Preview:")
        st.dataframe(df.head())
        if len(df.columns) > 1:
            id_col = st.selectbox("Choose the column containing product IDs:", df.columns)
            product_ids = df[id_col].dropna().astype(str).str.strip().tolist()
        else:
            product_ids = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
    else:
        st.markdown("**Or paste product IDs / product URLs manually:**")
        manual = st.text_area("Product IDs or product URLs (one per line)", placeholder="4811337\nhttps://platform.medipim.be/en/product/12345\n4811339", height=150)
        if manual:
            product_ids = [x.strip() for x in manual.splitlines() if x.strip()]

with col2:
    st.header("⚙️ Download Settings")
    if product_ids:
        st.success(f"Found {len(product_ids)} product IDs/URLs")
        st.write("Items to process (showing up to 10):")
        for pid in product_ids[:10]:
            st.write(f"• {pid}")
        if len(product_ids) > 10:
            st.write(f"… and {len(product_ids) - 10} more")

    can_run = bool(product_ids and username and password)
    if st.button("🚀 Start Download", disabled=not can_run):
        progress = st.progress(0)
        status = st.empty()
        results = st.container()

        api = MedipimAPI(username, password, base_url=base_url)
        status.text("Logging in…")
        if not api.login():
            st.error("Login error. Verify credentials or try again later.")
            st.stop()
        status.text("Login OK!")

        temp_dir = tempfile.mkdtemp(prefix="medipim_")
        downloaded: list[tuple[str, str]] = []
        failed: list[str] = []

        total = len(product_ids)
        for i, pid in enumerate(product_ids, start=1):
            progress.progress(i / total)
            status.text(f"Processing {pid} ({i}/{total})")
            try:
                detail_url = api.search_product(str(pid))
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
