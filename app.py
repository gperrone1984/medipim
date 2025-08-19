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
    def __init__(self, username: str, password: str, base_url: str = "https://platform.medipim.be/en/"):
        self.session = requests.Session()
        # Be explicit about a browser-y user agent to avoid being blocked by some CDNs/WAFs
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.logged_in = False

    # --- helpers ---
    def _abs(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def _get(self, url: str, **kwargs):
        resp = self.session.get(url, allow_redirects=True, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: dict, **kwargs):
        resp = self.session.post(url, data=data, allow_redirects=True, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    # --- auth ---
    def login(self) -> bool:
        """Robust login that reads the form, keeps CSRF, and detects success via redirects or dashboard markers."""
        login_url = self._abs("login")

        # 1) fetch login page
        resp = self._get(login_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        # 2) discover form and inputs
        form = soup.find("form")
        action = self._abs(form.get("action") if form and form.get("action") else login_url)
        inputs = {i.get("name"): i.get("value", "") for i in soup.select("input[name]")}

        # best-effort mapping for username/password fields
        # common: _username, email, username ; _password, password
        uname_key = next((k for k in inputs.keys() if k in {"_username", "username", "email", "_email"}), "_username")
        pwd_key = next((k for k in inputs.keys() if k in {"_password", "password"}), "_password")
        inputs[uname_key] = self.username
        inputs[pwd_key] = self.password

        # Some Symfony apps expect this header
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        # 3) submit
        post_resp = self._post(action, data=inputs, headers=headers)

        # 4) determine success
        # Successful login usually redirects away from /login
        redirected_away_from_login = all("/login" not in h.headers.get("Location", "") for h in post_resp.history) and \
                                      "/login" not in post_resp.url

        # Also try to fetch home or products page and look for navbar items that indicate an authenticated session
        dashboard_ok = False
        try:
            home = self._get(self._abs("home"))
            if any(token in home.text for token in ["Logout", "Sign out", "My account", "Products", "Dashboard"]):
                dashboard_ok = True
        except Exception:
            # not fatal
            pass

        self.logged_in = bool(redirected_away_from_login or dashboard_ok)
        return self.logged_in

    # --- product search ---
    def search_product(self, product_id: str) -> str | None:
        if not self.logged_in and not self.login():
            return None

        # Try a few search patterns, falling back to a generic search term
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

            soup = BeautifulSoup(resp.text, "html.parser")

            # look for product detail links; support multiple URL shapes
            links = soup.select('a[href*="/en/product"], a[href*="/product?id="], a[href*="/products/"]')
            for a in links:
                href = a.get("href") or ""
                text = (a.get_text(strip=True) or "")
                # take the first match; optionally verify id appears in link text or near it
                if product_id in text or True:
                    return self._abs(href)
        return None

    # --- media lookup ---
    def get_image_url(self, product_detail_url: str, prefer_size: str = "1500x1500") -> str | None:
        if not self.logged_in and not self.login():
            return None

        # visit product page
        resp = self._get(product_detail_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        # find a media tab/link
        media_link = soup.find('a', href=lambda h: h and 'media' in h.lower())
        if not media_link:
            # heuristic: find any anchor mentioning Media
            for el in soup.find_all(text=re.compile(r"Media", re.I)):
                if el.parent.name == "a" and el.parent.get("href"):
                    media_link = el.parent
                    break

        # open media page if present; otherwise scan current page
        page_html = resp.text
        if media_link and media_link.get("href"):
            media_url = self._abs(media_link["href"])
            media_resp = self._get(media_url)
            page_html = media_resp.text
            soup = BeautifulSoup(page_html, "html.parser")

        # Strategy 1: explicit anchors to huge/large
        a_candidates = soup.select('a[href*="/media/huge/"], a[href*="/media/large/"]')
        if a_candidates:
            return self._abs(a_candidates[0]["href"]) if a_candidates[0]["href"].startswith("/") else a_candidates[0]["href"]

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
        except Exception:
            return False
        return False


# ================================
# Streamlit UI
# ================================
st.set_page_config(page_title="Medipim Image Downloader", page_icon="📸", layout="wide")
st.title("📸 Medipim Image Downloader")
st.markdown("Carica una lista di ID prodotto e scarica le immagini 1500x1500 in un file ZIP")

# Sidebar (credentials)
st.sidebar.header("Credenziali Medipim")
username = st.sidebar.text_input("Username", value="", placeholder="nome.cognome@redcare-pharmacy.com")
password = st.sidebar.text_input("Password", type="password", value="")

with st.sidebar:
    if st.button("🔐 Test Login", use_container_width=True, disabled=not username or not password):
        api = MedipimAPI(username, password)
        ok = api.login()
        st.success("Login effettuato ✅" if ok else "Login fallito ❌")

# Main content
col1, col2 = st.columns([1, 1])

with col1:
    st.header("📋 Carica Lista Prodotti")
    uploaded = st.file_uploader("Carica file CSV con ID prodotti", type=["csv"])
    product_ids: list[str] = []

    if uploaded is not None:
        try:
            df = pd.read_csv(uploaded)
        except Exception:
            uploaded.seek(0)
            df = pd.read_csv(uploaded, sep=";")  # fallback to semicolon
        st.write("Anteprima del file:")
        st.dataframe(df.head())
        if len(df.columns) > 1:
            id_col = st.selectbox("Seleziona la colonna con gli ID prodotto:", df.columns)
            product_ids = df[id_col].dropna().astype(str).str.strip().tolist()
        else:
            product_ids = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
    else:
        st.markdown("**Oppure inserisci manualmente gli ID prodotto:**")
        manual = st.text_area("ID Prodotti (uno per riga)", placeholder="4811337\n4811338\n4811339", height=150)
        if manual:
            product_ids = [x.strip() for x in manual.splitlines() if x.strip()]

with col2:
    st.header("⚙️ Configurazione Download")
    if product_ids:
        st.success(f"Trovati {len(product_ids)} ID prodotto")
        st.write("ID prodotti da processare (max 10 mostrati):")
        for pid in product_ids[:10]:
            st.write(f"• {pid}")
        if len(product_ids) > 10:
            st.write(f"… e altri {len(product_ids) - 10} prodotti")

    can_run = bool(product_ids and username and password)
    if st.button("🚀 Avvia Download", disabled=not can_run):
        if not username or not password:
            st.error("Inserisci username e password")
            st.stop()
        if not product_ids:
            st.error("Inserisci almeno un ID prodotto")
            st.stop()

        progress = st.progress(0)
        status = st.empty()
        results = st.container()

        api = MedipimAPI(username, password)
        status.text("Effettuando login…")
        if not api.login():
            st.error("Errore durante il login. Verifica le credenziali oppure riprova più tardi.")
            st.stop()
        status.text("Login effettuato con successo!")

        temp_dir = tempfile.mkdtemp(prefix="medipim_")
        downloaded: list[tuple[str, str]] = []
        failed: list[str] = []

        total = len(product_ids)
        for i, pid in enumerate(product_ids, start=1):
            progress.progress(i / total)
            status.text(f"Processando prodotto {pid} ({i}/{total})")
            try:
                detail_url = api.search_product(str(pid))
                if not detail_url:
                    failed.append(f"{pid}: Prodotto non trovato")
                    continue

                img_url = api.get_image_url(detail_url)
                if not img_url:
                    failed.append(f"{pid}: Immagine non trovata")
                    continue

                save_path = os.path.join(temp_dir, f"{pid}.jpg")
                if api.download_image(img_url, save_path):
                    downloaded.append((pid, save_path))
                else:
                    failed.append(f"{pid}: Errore durante il download")
            except Exception as e:
                failed.append(f"{pid}: {e}")

        if downloaded:
            status.text("Creando file ZIP…")
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for pid, path in downloaded:
                    zf.write(path, arcname=f"{pid}.jpg")
            zip_buf.seek(0)

            with results:
                st.success(f"✅ Download completato! {len(downloaded)} immagini scaricate")
                if failed:
                    st.warning(f"⚠️ {len(failed)} download falliti:")
                    for f in failed:
                        st.write(f"• {f}")
                st.download_button(
                    label="📥 Scarica ZIP con immagini",
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
            status.text("✅ Processo completato!")
        else:
            st.error("❌ Nessuna immagine è stata scaricata con successo")
            if failed:
                for f in failed:
                    st.write(f"• {f}")

st.markdown("---")
st.markdown("**Nota:** Nessuna credenziale è salvata. Il tasto *Test Login* aiuta a diagnosticare eventuali problemi di autenticazione.")
