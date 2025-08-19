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
    def __init__(self, username: str, password: str, base_url: str = "https://platform.medipim.be/en/", debug: bool = False):
        self.session = requests.Session()
        # Be explicit about a browser-like user agent to avoid being blocked by some CDNs/WAFs
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
        self.last_debug = []  # store steps for UI diagnostics

    # --- helpers ---
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

    def _find_login_form(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        form = None
        # try common selectors first
        form = soup.find("form", attrs={"id": re.compile(r"login|signin", re.I)}) or \
               soup.find("form", attrs={"action": re.compile(r"login", re.I)}) or \
               soup.find("form")
        action = None
        inputs = {}
        if form:
            action = form.get("action")
            for i in form.select("input[name]"):
                inputs[i.get("name")] = i.get("value", "")
        # fallback: collect inputs from entire page if no form
        if not inputs:
            for i in soup.select("input[name]"):
                inputs[i.get("name")] = i.get("value", "")
        return action, inputs

    # --- auth ---
    def login(self) -> bool:
        """Robust login that keeps CSRF and tries multiple field name patterns.
        Returns True if we can reach an authenticated page.
        """
        self.last_debug.clear()
        login_url = self._abs("login")

        # 1) fetch login page
        try:
            resp = self._get(login_url)
        except Exception as e:
            self.last_debug.append(f"Failed to GET login page: {e}")
            return False

        action, inputs = self._find_login_form(resp.text)
        action_url = self._abs(action) if action else login_url

        # Prepare several candidate payloads (different field names)
        candidates = []
        # a) Based on discovered inputs
        discovered = inputs.copy()
        # guess keys
        uname_key = next((k for k in discovered.keys() if k.lower() in {"_username", "username", "email", "_email", "login"}), "_username")
        pwd_key = next((k for k in discovered.keys() if k.lower() in {"_password", "password", "pass", "passwd"}), "_password")
        discovered[uname_key] = self.username
        discovered[pwd_key] = self.password
        candidates.append((action_url, discovered))
        
        # b) Common Symfony security keys
        candidates.append((action_url, {"_username": self.username, "_password": self.password, **{k:v for k,v in inputs.items() if k.startswith("_") and k not in {"_username", "_password"}}}))
        # c) email/password
        candidates.append((action_url, {"email": self.username, "password": self.password, **{k:v for k,v in inputs.items() if k not in {"email", "password"}}}))
        # d) username/password
        candidates.append((action_url, {"username": self.username, "password": self.password, **{k:v for k,v in inputs.items() if k not in {"username", "password"}}}))

        success = False
        for post_to, payload in candidates:
            try:
                post_resp = self._post(post_to, data=payload)
            except Exception as e:
                self.last_debug.append(f"POST failed: {e}")
                continue

            # If we were redirected away from /login, that's a strong signal
            redirected_away = ("/login" not in post_resp.url)

            # Probe an authenticated page
            probe_ok = False
            for probe_path in ("home", "products", "account", "profile"):
                try:
                    probe = self._get(self._abs(probe_path))
                    if any(token in probe.text for token in ["Logout", "Sign out", "My account", "Products", "Dashboard"]):
                        probe_ok = True
                        break
                except Exception:
                    continue

            if redirected_away or probe_ok:
                success = True
                break

        self.logged_in = success
        return self.logged_in

    # --- product search ---
    def search_product(self, product_id: str) -> str | None:
        if not self.logged_in and not self.login():
            return None

        candidates = [
            f"products?search=refcode%5B{product_id}%5D",
            f"products?search=refcode[{product_id}]",
            f"products?search={product_id}",
        ]

        for path in candidates:
            try:
                resp = self._get(self._abs(path))
            except Exception:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            # try several link shapes
            links = soup.select('a[href*="/en/product"], a[href*="/product?id="], a[href*="/products/"]')
            for a in links:
                href = a.get("href") or ""
                text = (a.get_text(strip=True) or "")
                if product_id in text or True:
                    return self._abs(href)
        return None

    # --- media lookup ---
    def get_image_url(self, product_detail_url: str, prefer_size: str = "1500x1500") -> str | None:
        if not self.logged_in and not self.login():
            return None

        resp = self._get(product_detail_url)
        soup = BeautifulSoup(resp.text, "lxml")

        # find a media tab/link
        media_link = soup.find('a', href=lambda h: h and 'media' in h.lower())
        if not media_link:
            for el in soup.find_all(string=re.compile(r"Media", re.I)):
                if el and el.parent and el.parent.name == "a" and el.parent.get("href"):
                    media_link = el.parent
                    break

        # open media page if present; otherwise scan current page
        page_html = resp.text
        if media_link and media_link.get("href"):
            media_url = self._abs(media_link["href"])
            media_resp = self._get(media_url)
            page_html = media_resp.text
            soup = BeautifulSoup(page_html, "lxml")

        # Strategy 1: explicit anchors to huge/large
        a_candidates = soup.select('a[href*="/media/huge/"], a[href*="/media/large/"]')
        if a_candidates:
            href = a_candidates[0]["href"]
            return self._abs(href) if href.startswith("/") else href

        # Strategy 2: img tags with data-src/src pointing to assets
        for img in soup.find_all("img"):
            for attr in ("data-src", "src"):
                val = img.get(attr)
                if val and ("/media/huge/" in val or "/media/large/" in val):
                    return self._abs(val) if val.startswith("/") else val

        # Strategy 3: regex scan of page
        huge = re.findall(r"https?://[^\"]+/media/huge/[a-f0-9]+\.jpe?g", page_html)
        if huge:
            return huge[0]
        large = re.findall(r"https?://[^\"]+/media/large/[a-f0-9]+\.jpe?g", page_html)
        if large:
            return large[0]
        return None

    # --- download ---
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

# Sidebar (credentials)
st.sidebar.header("Medipim Credentials")
username = st.sidebar.text_input("Username", value="", placeholder="name.surname@redcare-pharmacy.com")
password = st.sidebar.text_input("Password", type="password", value="")
base_url = st.sidebar.text_input("Base URL", value="https://platform.medipim.be/en/")
show_debug = st.sidebar.toggle("Show login debug", value=False)

with st.sidebar:
    if st.button("🔐 Test Login", use_container_width=True, disabled=not username or not password):
        api = MedipimAPI(username, password, base_url=base_url, debug=True)
        ok = api.login()
        if ok:
            st.success("Login successful ✅")
        else:
            st.error("Login failed ❌. Check credentials or try again later.")
            if api.last_debug:
                st.code("
".join(api.last_debug)[-4000:], language="text")

# Main content
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
            df = pd.read_csv(uploaded, sep=";")  # fallback to semicolon
        st.write("Preview:")
        st.dataframe(df.head())
        if len(df.columns) > 1:
            id_col = st.selectbox("Choose the column containing product IDs:", df.columns)
            product_ids = df[id_col].dropna().astype(str).str.strip().tolist()
        else:
            product_ids = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
    else:
        st.markdown("**Or paste product IDs manually:**")
        manual = st.text_area("Product IDs (one per line)", placeholder="4811337
4811338
4811339", height=150)
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

        api = MedipimAPI(username, password, base_url=base_url, debug=show_debug)
        status.text("Logging in…")
        if not api.login():
            st.error("Login error. Verify credentials or try again later.")
            if show_debug and api.last_debug:
                st.code("
".join(api.last_debug)[-4000:], language="text")
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

            # cleanup
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
    "**Notes:**

"
    "- Credentials are never stored.
"
    "- Use **🔐 Test Login** in the sidebar to troubleshoot authentication.
"
    "- You can change **Base URL** if Medipim uses a different locale (e.g., /nl/)."
)
