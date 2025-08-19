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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
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
            self.last_debug.append("Injected Cookie header for session reuse")

    def _abs(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def _get(self, url: str, **kwargs):
        resp = self.session.get(url, allow_redirects=True, timeout=30, **kwargs)
        if self.debug:
            self.last_debug.append(f"GET {url} -> {resp.status_code} | final: {resp.url}")
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: dict, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        headers.setdefault("Referer", url)
        resp = self.session.post(url, data=data, allow_redirects=True, timeout=30, headers=headers, **kwargs)
        if self.debug:
            self.last_debug.append(f"POST {url} -> {resp.status_code} | final: {resp.url} | sent keys: {list(data.keys())}")
        resp.raise_for_status()
        return resp

    def login(self) -> bool:
        self.last_debug.clear()
        login_url = self._abs("login")
        try:
            resp = self._get(login_url)
        except Exception as e:
            self.last_debug.append(f"Failed to GET login page: {e}")
            return False

        # Try a direct API login endpoint (common in Medipim)
        api_login_url = self._abs("api/login_check")
        try:
            r = self.session.post(api_login_url, json={"username": self.username, "password": self.password}, headers={"Accept": "application/json"}, timeout=30)
            if r.status_code in (200, 204):
                self.logged_in = True
                return True
        except Exception as e:
            self.last_debug.append(f"API login attempt failed: {e}")

        self.logged_in = False
        return False

    def search_product(self, product_id: str) -> str | None:
        if not self.logged_in and not self.login():
            return None

        # Use the API search endpoint if available
        try:
            api_url = self._abs(f"api/products?search={product_id}")
            resp = self._get(api_url, headers={"Accept": "application/json"})
            data = resp.json()
            if isinstance(data, dict) and "items" in data and data["items"]:
                # assume first product
                detail_id = data["items"][0].get("id")
                if detail_id:
                    return self._abs(f"en/product/{detail_id}")
        except Exception as e:
            self.last_debug.append(f"API search failed: {e}")

        return None

    def get_image_url(self, product_detail_url: str, prefer_size: str = "1500x1500") -> str | None:
        if not self.logged_in and not self.login():
            return None
        try:
            # Use API media endpoint if possible
            product_id = product_detail_url.rstrip("/").split("/")[-1]
            media_url = self._abs(f"api/products/{product_id}/media")
            resp = self._get(media_url, headers={"Accept": "application/json"})
            data = resp.json()
            if isinstance(data, list) and data:
                for item in data:
                    if "huge" in item.get("url", "") or "large" in item.get("url", ""):
                        return item["url"]
        except Exception as e:
            self.last_debug.append(f"API media fetch failed: {e}")
        return None

    def download_image(self, image_url: str, save_path: str) -> bool:
        if not self.logged_in and not self.login():
            return False
        try:
            resp = self.session.get(image_url, stream=True, timeout=60)
            if resp.status_code == 200:
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(1024 * 64):
                        if chunk:
                            f.write(chunk)
                return True
        except Exception as e:
            self.last_debug.append(f"Download failed: {e}")
            return False
        return False


# ================================
# Streamlit UI
# ================================
st.set_page_config(page_title="Medipim Image Downloader", page_icon="📸", layout="wide")
st.title("📸 Medipim Image Downloader")
st.markdown("Upload a list of product IDs and download 1500x1500 images as a ZIP file.")

st.sidebar.header("Medipim Credentials")
username = st.sidebar.text_input("Username", value="", placeholder="name.surname@redcare-pharmacy.com")
password = st.sidebar.text_input("Password", type="password", value="")
base_url = st.sidebar.text_input("Base URL", value="https://platform.medipim.be/en/")
show_debug = st.sidebar.toggle("Show login debug", value=False)
cookie_header_opt = st.sidebar.text_input("Cookie header (optional)", value="", placeholder="PHPSESSID=...; other=...")

with st.sidebar:
    if st.button("🔐 Test Login", use_container_width=True, disabled=not username or not password):
        api = MedipimAPI(username, password, base_url=base_url, debug=True, cookie_header=cookie_header_opt or None)
        ok = api.login()
        if ok:
            st.success("Login successful ✅")
        else:
            st.error("Login failed ❌. Check credentials or try again later.")
            if api.last_debug:
                st.code("\n".join(api.last_debug)[-4000:], language="text")

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
        st.markdown("**Or paste product IDs manually:**")
        manual = st.text_area("Product IDs (one per line)", placeholder="4811337\n4811338\n4811339", height=150)
        if manual:
            product_ids = [x.strip() for x in manual.splitlines() if x.strip()]

with col2:
    st.header("⚙️ Download Settings")
    if product_ids:
        st.success(f"Found {len(product_ids)} product IDs")
        st.write("IDs to process (showing up to 10):")
        for pid in product_ids[:10]:
            st.write(f"• {pid}")
        if len(product_ids) > 10:
            st.write(f"… and {len(product_ids) - 10} more")

    can_run = bool(product_ids and username and password)
    if st.button("🚀 Start Download", disabled=not can_run):
        if not username or not password:
            st.error("Please enter username and password")
            st.stop()
        if not product_ids:
            st.error("Please provide at least one product ID")
            st.stop()

        progress = st.progress(0)
        status = st.empty()
        results = st.container()

        api = MedipimAPI(username, password, base_url=base_url, debug=show_debug, cookie_header=cookie_header_opt or None)
        status.text("Logging in…")
        if not api.login():
            st.error("Login error. Verify credentials or try again later.")
            if show_debug and api.last_debug:
                st.code("\n".join(api.last_debug)[-4000:], language="text")
            st.stop()
        status.text("Login OK!")

        temp_dir = tempfile.mkdtemp(prefix="medipim_")
        downloaded: list[tuple[str, str]] = []
        failed: list[str] = []

        total = len(product_ids)
        for i, pid in enumerate(product_ids, start=1):
            progress.progress(i / total)
            status.text(f"Processing product {pid} ({i}/{total})")
            try:
                detail_url = api.search_product(str(pid))
                if not detail_url:
                    failed.append(f"{pid}: Product not found")
                    continue

                img_url = api.get_image_url(detail_url)
                if not img_url:
                    failed.append(f"{pid}: Image not found")
                    continue

                save_path = os.path.join(temp_dir, f"{pid}.jpg")
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
                    zf.write(path, arcname=f"{pid}.jpg")
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
                    file_name=f"medipim_images_{len(downloaded)}_products.zip",
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
    "**Notes:**\n\n"
    "- Credentials are never stored.\n"
    "- Use **🔐 Test Login** in the sidebar to troubleshoot authentication.\n"
    "- You can change **Base URL** if Medipim uses a different locale (e.g., /nl/)."
)
