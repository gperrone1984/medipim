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

    def _post(self, url: str, data: dict = None, json: dict = None, **kwargs):
        headers = kwargs.pop("headers", {})
        if json is None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        headers.setdefault("Referer", url)
        resp = self.session.post(url, data=data, json=json, allow_redirects=True, timeout=30, headers=headers, **kwargs)
        if self.debug:
            sent_keys = list((data or json or {}).keys())
            self.last_debug.append(f"POST {url} -> {resp.status_code} | final: {resp.url} | sent keys: {sent_keys}")
        resp.raise_for_status()
        return resp

    def _find_login_form(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", attrs={"id": re.compile(r"login|signin", re.I)}) or \
               soup.find("form", attrs={"action": re.compile(r"login", re.I)}) or \
               soup.find("form")
        action = None
        inputs = {}
        if form:
            action = form.get("action")
            for i in form.select("input[name]"):
                inputs[i.get("name")] = i.get("value", "")
        meta_token = soup.find("meta", attrs={"name": re.compile(r"csrf", re.I)})
        if meta_token and meta_token.get("content"):
            inputs.setdefault("_csrf_token", meta_token["content"])
        if not inputs:
            for i in soup.select("input[name]"):
                inputs[i.get("name")] = i.get("value", "")
        return action, inputs

    def login(self) -> bool:
        self.last_debug.clear()
        login_url = self._abs("login")
        try:
            self._get(login_url)
        except Exception as e:
            self.last_debug.append(f"Failed to GET login page: {e}")

        api_candidates = [
            (self._abs("api/login_check"), {"username": self.username, "password": self.password}),
            (self._abs("login_check"), {"_username": self.username, "_password": self.password}),
            (self._abs("en/login_check"), {"_username": self.username, "_password": self.password}),
            (self._abs("authenticate"), {"username": self.username, "password": self.password}),
        ]
        for url, payload in api_candidates:
            try:
                r = self._post(url, json=payload, headers={"Accept": "application/json"})
                if r.status_code in (200, 204):
                    self.logged_in = True
                    return True
            except Exception as e:
                self.last_debug.append(f"API login attempt to {url} failed: {e}")

        try:
            resp = self._get(login_url)
            action, inputs = self._find_login_form(resp.text)
            action_url = self._abs(action) if action else login_url
            discovered = inputs.copy()
            uname_key = next((k for k in discovered.keys() if k.lower() in {"_username", "username", "email", "_email", "login"}), "_username")
            pwd_key = next((k for k in discovered.keys() if k.lower() in {"_password", "password", "pass", "passwd"}), "_password")
            discovered[uname_key] = self.username
            discovered[pwd_key] = self.password
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if discovered.get("_csrf_token"):
                headers["X-CSRF-TOKEN"] = discovered["_csrf_token"]
            self._post(action_url, data=discovered, headers=headers)
            for probe_path in ("home", "products", "account", "profile"):
                try:
                    probe = self._get(self._abs(probe_path))
                    if any(token in probe.text for token in ["Logout", "Sign out", "My account", "Products", "Dashboard"]):
                        self.logged_in = True
                        return True
                except Exception:
                    continue
        except Exception as e:
            self.last_debug.append(f"Form login failed: {e}")

        self.logged_in = False
        return False

    def search_product(self, product_id: str, search_hint: str | None = None) -> str | None:
        if not self.logged_in and not self.login():
            return None
        if isinstance(product_id, str) and product_id.startswith("http"):
            return product_id
        try:
            api_url = self._abs(f"api/products?search={product_id}")
            resp = self._get(api_url, headers={"Accept": "application/json"})
            data = resp.json()
            if isinstance(data, dict) and data.get("items"):
                detail_id = data["items"][0].get("id")
                if detail_id:
                    return self._abs(f"en/product/{detail_id}")
        except Exception as e:
            self.last_debug.append(f"API search failed: {e}")
        fields = [search_hint] if search_hint else ["refcode", "ean", "gtin", "sku", "id"]
        candidates = []
        for field in fields:
            candidates.append(f"products?search%5B{field}%5D={product_id}")
            candidates.append(f"products?search={field}%5B{product_id}%5D")
            candidates.append(f"products?search={product_id}")
        direct_candidates = [
            f"en/products/{product_id}", f"en/product/{product_id}", f"products/{product_id}", f"product/{product_id}"
        ]
        for path in candidates:
            try:
                resp = self._get(self._abs(path))
            except Exception:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            links = soup.select('a[href*="/en/product"], a[href*="/product?id="], a[href*="/products/"]')
            for a in links:
                href = a.get("href") or ""
                if href:
                    return self._abs(href)
            card = soup.find(attrs={"data-product-id": True})
            if card and card.get("href"):
                return self._abs(card.get("href"))
        for path in direct_candidates:
            try:
                resp = self._get(self._abs(path))
                if any(k in resp.text for k in ["Media", "Specifications", "Images", "Downloads", "Product"]):
                    return resp.url
            except Exception:
                continue
        return None

    def get_image_url(self, product_detail_url: str, prefer_size: str = "1500x1500") -> str | None:
        if not self.logged_in and not self.login():
            return None
        try:
            product_id = product_detail_url.rstrip("/").split("/")[-1]
            media_url = self._abs(f"api/products/{product_id}/media")
            resp = self._get(media_url, headers={"Accept": "application/json"})
            data = resp.json()
            if isinstance(data, list) and data:
                for item in data:
                    url = item.get("url", "")
                    if "/media/huge/" in url or "/media/large/" in url:
                        return url
                return data[0].get("url")
        except Exception as e:
            self.last_debug.append(f"API media fetch failed: {e}")
        try:
            resp = self._get(product_detail_url)
            soup = BeautifulSoup(resp.text, "lxml")
            media_link = soup.find('a', href=lambda h: h and 'media' in h.lower())
            page_html = resp.text
            if media_link and media_link.get("href"):
                media_url = self._abs(media_link["href"])
                mresp = self._get(media_url)
                page_html = mresp.text
                soup = BeautifulSoup(page_html, "lxml")
            a_candidates = soup.select('a[href*="/media/huge/"], a[href*="/media/large/"]')
            if a_candidates:
                href = a_candidates[0]["href"]
                return self._abs(href) if href.startswith("/") else href
            for img in soup.find_all("img"):
                for attr in ("data-src", "src"):
                    val = img.get(attr)
                    if val and ("/media/huge/" in val or "/media/large/" in val):
                        return self._abs(val) if val.startswith("/") else val
            huge = re.findall(r"https?://[^\"]+/media/huge/[a-f0-9]+\.jpe?g", page_html)
            if huge:
                return huge[0]
            large = re.findall(r"https?://[^\"]+/media/large/[a-f0-9]+\.jpe?g", page_html)
            if large:
                return large[0]
        except Exception as e:
            self.last_debug.append(f"HTML media parse failed: {e}")
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
search_hint = st.sidebar.selectbox("Search field (hint)", ["auto", "refcode", "ean", "gtin", "sku", "id"], index=0)

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
        if not username or not password:
            st.error("Please enter username and password")
            st.stop()
        if not product_ids:
            st.error("Please provide at least one product ID or URL")
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
