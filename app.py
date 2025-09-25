import os
import io
import time
import json
import base64
import tempfile
import pathlib
import hashlib
import zipfile
from typing import Dict, List, Tuple, Optional
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

import pandas as pd
import streamlit as st

from PIL import Image, ImageOps, ImageDraw
import requests

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ===============================
# Streamlit config
# ===============================
st.set_page_config(page_title="Medipim Export → Photos (NL/FR)", page_icon="📦", layout="centered")
st.title("Medipim: Login → Export → Download Photos (NL/FR)")

# ---------------- Session state ----------------
if "exports" not in st.session_state:
    st.session_state["exports"] = {}
if "photo_zip" not in st.session_state:
    st.session_state["photo_zip"] = {}
if "missing_lists" not in st.session_state:
    st.session_state["missing_lists"] = {}

# ===============================
# UI — Login & SKUs
# ===============================
with st.form("login_form", clear_on_submit=False):
    st.subheader("Login")
    email = st.text_input("Email", value="", autocomplete="username")
    password = st.text_input("Password", value="", type="password", autocomplete="current-password")

    st.subheader("SKU input")
    sku_text = st.text_area(
        "Paste SKU or CNK codes (separated by spaces, commas, or newlines)",
        height=120,
        placeholder="e.g. BE03678976 or 3678976",
    )
    uploaded_skus = st.file_uploader("Or upload an Excel with a 'sku' column (optional)", type=["xlsx"], key="xls_skus")

    st.subheader("Images to download")
    scope = st.radio("Select images", ["All (NL + FR)", "NL only", "FR only"], index=0, horizontal=True)

    submitted = st.form_submit_button("Download photos")

# Clear cache & data button
clear_clicked = st.button("Clear cache and data", help="Delete temporary files and reset the app state")
if clear_clicked:
    for k in ("exports", "photo_zip", "missing_lists"):
        st.session_state[k] = {}
    removed = 0
    tmp_root = tempfile.gettempdir()
    for name in os.listdir(tmp_root):
        if name.startswith(("medipim_", "chrome-user-")):
            try:
                shutil.rmtree(os.path.join(tmp_root, name), ignore_errors=True)
                removed += 1
            except Exception:
                pass
    try:
        st.cache_data.clear()
    except Exception:
        pass
    try:
        st.cache_resource.clear()
    except Exception:
        pass
    st.success(f"Cache cleared. Removed {removed} temp folder(s) and reset state.")

# ===============================
# Selenium driver + helpers
# ===============================
def make_ctx(download_dir: str):
    from selenium.webdriver.chrome.service import Service
    user_dir = os.path.join(tempfile.gettempdir(), f"chrome-user-{os.getpid()}")
    os.makedirs(user_dir, exist_ok=True)

    def build_options():
        opt = webdriver.ChromeOptions()
        opt.add_argument("--headless=new")
        opt.add_argument("--no-sandbox")
        opt.add_argument("--disable-dev-shm-usage")
        opt.add_argument("--disable-gpu")
        opt.add_argument("--no-zygote")
        opt.add_argument("--window-size=1440,1000")
        opt.add_argument("--remote-debugging-port=0")
        opt.add_argument(f"--user-data-dir={user_dir}")
        opt.add_experimental_option("prefs", {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "download_restrictions": 0,
            "safebrowsing.enabled": True,
            "safebrowsing.disable_download_protection": True,
            "profile.default_content_setting_values.automatic_downloads": 1,
        })
        opt.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
        return opt

    opt = build_options()
    try:
        driver = webdriver.Chrome(options=opt)
    except WebDriverException as e_a:
        st.error("At the moment it is not possible to download the images, please try again later.")
        raise
    wait = WebDriverWait(driver, 40)
    actions = ActionChains(driver)
    return {"driver": driver, "wait": wait, "actions": actions, "download_dir": download_dir}

def handle_cookies(ctx):
    drv = ctx["driver"]
    for xp in [
        "//button[contains(., 'Alles accepteren')]",
        "//button[contains(., 'Ik ga akkoord')]",
        "//button[contains(., 'Accepter') or contains(., 'Tout accepter')]",
        "//button[contains(., 'OK')]",
        "//button[contains(., 'Accept all') or contains(., 'Accept')]",
    ]:
        try:
            btn = WebDriverWait(drv, 3).until(EC.element_to_be_clickable((By.XPATH, xp)))
            drv.execute_script("arguments[0].click();", btn)
            break
        except Exception:
            pass

def ensure_language(ctx, lang: str):
    drv, wait = ctx["driver"], ctx["wait"]
    try:
        base = f"https://platform.medipim.be/{'nl/home' if lang=='nl' else 'fr/home'}"
        drv.get(base)
        handle_cookies(ctx)
    except Exception:
        st.error("At the moment it is not possible to download the images, please try again later.")
        raise

def open_export_dropdown(ctx):
    drv, wait = ctx["driver"], ctx["wait"]
    try:
        split = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.SplitButton")))
        trigger = split.find_element(By.CSS_SELECTOR, "button.trigger")
        drv.execute_script("arguments[0].click();", trigger); time.sleep(0.25)
        dd = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.Dropdown.open div.dropdown")))
        return dd
    except TimeoutException:
        st.error("At the moment it is not possible to download the images, please try again later.")
        return None

def run_export_and_get_bytes(ctx, lang: str, refs: str) -> bytes | None:
    try:
        ensure_language(ctx, lang)
        if lang == "nl":
            url = f"https://platform.medipim.be/nl/producten?search=refcode[{refs.replace(' ', '%20')}]"
        else:
            url = f"https://platform.medipim.be/fr/produits?search=refcode[{refs.replace(' ', '%20')}]"
        drv, wait = ctx["driver"], ctx["wait"]
        drv.get(url)
        handle_cookies(ctx)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.SplitButton")))
        dd = open_export_dropdown(ctx)
        if dd is None:
            return None
        click_excel_option(ctx, dd)
        select_all_attributes(ctx)
        create_btn = WebDriverWait(drv, 25).until(EC.element_to_be_clickable((By.XPATH,"//button[contains(., 'Create')] | //button[contains(., 'Créer')] | //button[contains(., 'Aanmaken')]")))
        drv.execute_script("arguments[0].click();", create_btn)
        WebDriverWait(drv, 40).until(EC.presence_of_element_located((By.XPATH,"//*[contains(., 'Export')]")))
        dl = wait.until(EC.element_to_be_clickable((By.XPATH,"//button[contains(., 'DOWNLOAD')] | //a[contains(., 'Download')] | //button[contains(., 'Télécharger')]")))
        href = (dl.get_attribute("href") or dl.get_attribute("data-href") or "").strip().lower()
        start = time.time()
        if href and (not href.startswith("javascript")) and (not href.startswith("blob:")):
            drv.get(href)
        else:
            drv.execute_script("arguments[0].click();", dl)
        disk = wait_for_xlsx_on_disk(ctx, start_time=start, timeout=60)
        if disk and disk.exists():
            return disk.read_bytes()
    except Exception:
        st.error("At the moment it is not possible to download the images, please try again later.")
    return None

def do_login(ctx, email_addr: str, pwd: str):
    drv, wait = ctx["driver"], ctx["wait"]
    try:
        drv.get("https://platform.medipim.be/nl/inloggen")
        handle_cookies(ctx)
        email_el = wait.until(EC.presence_of_element_located((By.ID, "form0.email")))
        pwd_el   = wait.until(EC.presence_of_element_located((By.ID, "form0.password")))
        email_el.clear(); email_el.send_keys(email_addr)
        pwd_el.clear();   pwd_el.send_keys(pwd)
        submit = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.SubmitButton")))
        drv.execute_script("arguments[0].click();", submit)
        wait.until(EC.invisibility_of_element_located((By.ID, "form0.email")))
    except Exception:
        st.error("At the moment it is not possible to download the images, please try again later.")
        raise

# ===============================
# SKU parsing (normalizzata)
# ===============================
def _normalize_sku(raw: str) -> Optional[str]:
    """
    Rimuove tutto ciò che non è cifra e toglie gli zeri iniziali.
    'BE03678976' -> '3678976'; '0004811337' -> '4811337'
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    return digits.lstrip("0") or digits  # se tutto zero, torna "0"

def parse_skus(sku_text: str, uploaded_file) -> List[str]:
    skus: List[str] = []
    if sku_text:
        raw = sku_text.replace(",", " ").split()
        skus.extend([x.strip() for x in raw if x.strip()])
    if uploaded_file is not None:
        try:
            df = pd.read_excel(uploaded_file, engine="openpyxl")
            df.columns = [c.lower() for c in df.columns]
            if "sku" in df.columns:
                ex_skus = df["sku"].astype(str).map(lambda x: x.strip()).tolist()
                skus.extend([x for x in ex_skus if x])
        except Exception as e:
            st.error(f"Failed to read uploaded Excel: {e}")
    # normalizza + dedup
    seen, out = set(), []
    for s in skus:
        norm = _normalize_sku(s)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out

# ===============================
# Photo processing — constants
# ===============================
DEDUP_DHASH_THRESHOLD = 3  # Hamming distance per dHash (0..64)
TYPE_RANK = {
    "photo du produit": 1,
    "productfoto": 1,
    "photo de l'emballage": 2,
    "verpakkingsfoto": 2,
    "photo promotionnelle": 3,
    "sfeerbeeld": 3,
}

# ===============================
# Helpers: Excel parse
# ===============================
def _read_book(xlsx_bytes: bytes) -> Tuple[pd.DataFrame, pd.DataFrame]:
    xl = pd.ExcelFile(io.BytesIO(xlsx_bytes))
    products = xl.parse(xl.sheet_names[0])
    try:
        photos = xl.parse("Photos")
    except Exception:
        photos = xl.parse(xl.sheet_names[1]) if len(xl.sheet_names) > 1 else pd.DataFrame()
    return products, photos

def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df

def _extract_id_cnk(products_df: pd.DataFrame) -> pd.DataFrame:
    df = _normalise_columns(products_df)
    cols_lower = {c.lower(): c for c in df.columns}
    id_col = cols_lower.get("id")
    cnk_col = cols_lower.get("cnk code") or cols_lower.get("code cnk")
    if not id_col or not cnk_col:
        raise ValueError("Could not find 'ID' and 'CNK code/code CNK' columns in Products sheet.")
    out = df[[id_col, cnk_col]].rename(columns={id_col: "ID", cnk_col: "CNK"})
    out["ID"] = out["ID"].astype(str).str.strip()
    out["CNK"] = out["CNK"].astype(str).str.replace(" ", "").str.strip()
    return out

def _extract_photos(photos_df: pd.DataFrame) -> pd.DataFrame:
    df = _normalise_columns(photos_df)
    cols_lower = {c.lower(): c for c in df.columns}
    pid_col = cols_lower.get("product id")
    url_col = cols_lower.get("900x900")
    type_col = cols_lower.get("type")
    photoid_col = cols_lower.get("photo id")
    if not pid_col or not url_col:
        raise ValueError("Could not find 'Product ID' and '900x900' columns in Photos sheet.")
    out = df[[pid_col, url_col]].rename(columns={pid_col: "Product ID", url_col: "URL"})
    out["Product ID"] = out["Product ID"].astype(str).str.strip()
    out["Type"] = df[type_col].astype(str).str.strip() if type_col else ""
    out["Photo ID"] = pd.to_numeric(df[photoid_col], errors="coerce") if photoid_col else None
    return out

# ===============================
# Image helpers (cached & parallel)
# ===============================
@st.cache_data(show_spinner=False, ttl=24*3600, max_entries=10000)
def _fetch_url_cached(url: str) -> Optional[bytes]:
    """Scarica e cache-a i bytes dell'immagine per URL (cache 24h)."""
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200 or not r.content:
            return None
        return r.content
    except Exception:
        return None

def _download_many(urls: List[str], progress: Optional[st.progress] = None, max_workers: int = 16) -> Dict[str, Optional[bytes]]:
    """Scarica in parallelo gli URL, usando la cache per ogni URL."""
    results: Dict[str, Optional[bytes]] = {}
    total = len(urls)
    done = 0
    next_update = 0.0

    def task(u):
        return u, _fetch_url_cached(u)

    if total == 0:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(task, u) for u in urls]
        for f in as_completed(futures):
            u, content = f.result()
            results[u] = content
            done += 1
            frac = done / total
            if progress and frac >= next_update:
                progress.progress(min(1.0, frac))
                next_update += 0.05  # update ogni 5%

    if progress:
        progress.progress(1.0)
    return results

def _to_1000_canvas(img: Image.Image) -> Image.Image:
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    img = ImageOps.contain(img, (1000, 1000))
    canvas = Image.new("RGB", (1000, 1000), (255, 255, 255))
    x = (1000 - img.width) // 2
    y = (1000 - img.height) // 2
    canvas.paste(img, (x, y))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(940, 940), (999, 999)], fill=(255, 255, 255))
    return canvas

def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()

def _dhash(image: Image.Image, hash_size: int = 8) -> int:
    """Perceptual difference hash (dHash)."""
    img = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = list(img.getdata())
    w = hash_size + 1
    bits = []
    for row in range(hash_size):
        row_start = row * w
        for col in range(hash_size):
            left = pixels[row_start + col]
            right = pixels[row_start + col + 1]
            bits.append(1 if left > right else 0)
    val = 0
    for b in bits:
        val = (val << 1) | b
    return val

def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()

def _hash_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()

def _process_one(url: str, content: Optional[bytes]) -> Tuple[str, Optional[Tuple[bytes, int, str]]]:
    """Elabora un'immagine (bytes → canvas 1000 → jpeg → dhash/md5)."""
    if content is None:
        return url, None
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
        processed = _to_1000_canvas(img)
        dh = _dhash(processed, hash_size=8)
        jb = _jpeg_bytes(processed)
        md5 = _hash_bytes(jb)
        return url, (jb, dh, md5)
    except Exception:
        return url, None

def _process_many(urls: List[str], contents: Dict[str, Optional[bytes]], progress: Optional[st.progress] = None, max_workers: int = 16) -> Dict[str, Optional[Tuple[bytes, int, str]]]:
    """Elabora in parallelo i contenuti scaricati."""
    results: Dict[str, Optional[Tuple[bytes, int, str]]] = {}
    total = len(urls)
    done = 0
    next_update = 0.0

    if total == 0:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process_one, u, contents.get(u)) for u in urls]
        for f in as_completed(futures):
            u, triple = f.result()
            results[u] = triple
            done += 1
            frac = done / total
            if progress and frac >= next_update:
                progress.progress(min(1.0, frac))
                next_update += 0.05

    if progress:
        progress.progress(1.0)
    return results

# ===============================
# Build ZIP (parallelo + dedup)
# ===============================
class ScaledProgress:
    """Proxy around una progress bar unica, con finestra [start,end]."""
    def __init__(self, widget, start: float, end: float):
        self.widget = widget
        self.start = float(start)
        self.end = float(end)
    def progress(self, frac: float):
        frac = max(0.0, min(1.0, float(frac)))
        val = self.start + (self.end - self.start) * frac
        self.widget.progress(min(1.0, max(0.0, val)))

def build_zip_for_lang(xlsx_bytes: bytes, lang: str, progress: ScaledProgress) -> Tuple[bytes, int, int, List[Dict[str, str]]]:
    """
    Pipeline:
      1) Parse/sort
      2) Download parallelo (cache)
      3) Processing parallelo (canvas+hash)
      4) Dedup per CNK
      5) Scrittura ZIP
    """
    products_df, photos_df = _read_book(xlsx_bytes)
    id_cnk = _extract_id_cnk(products_df)
    photos_raw = _extract_photos(photos_df)

    id2cnk: Dict[str, str] = {str(row["ID"]).strip(): str(row["CNK"]).strip() for _, row in id_cnk.iterrows()}

    try:
        all_pids_set = set(photos_raw["Product ID"].astype(str).str.strip())
    except Exception:
        all_pids_set = set()

    def _rank_type(t: str) -> int:
        if not isinstance(t, str):
            return 99
        return TYPE_RANK.get(t.strip().lower(), 99)

    photos = photos_raw.dropna(subset=["URL"]).copy()
    photos["rank_type"] = photos["Type"].map(_rank_type)
    photos["rank_photoid"] = pd.to_numeric(photos["Photo ID"], errors="coerce").fillna(10**9).astype(int)
    photos.sort_values(["Product ID", "rank_type", "rank_photoid"], inplace=True)

    # record ordinati
    records = []
    for _, r in photos.iterrows():
        pid = str(r["Product ID"]).strip()
        url = str(r["URL"]).strip()
        cnk = id2cnk.get(pid)
        records.append({"pid": pid, "cnk": cnk, "url": url})

    # Download parallelo (0→40%)
    dl_prog = ScaledProgress(progress.widget, progress.start, progress.start + (progress.end - progress.start) * 0.40)
    url_list = [rec["url"] for rec in records]
    url_contents = _download_many(url_list, progress=dl_prog, max_workers=16)

    # Processing parallelo (40→85%)
    pr_prog = ScaledProgress(progress.widget, progress.start + (progress.end - progress.start) * 0.40, progress.start + (progress.end - progress.start) * 0.85)
    processed_map = _process_many(url_list, url_contents, progress=pr_prog, max_workers=16)

    # Dedup + ZIP (85→100%)
    zip_prog = ScaledProgress(progress.widget, progress.start + (progress.end - progress.start) * 0.85, progress.end)
    zip_buf = io.BytesIO()
    zf = zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED)

    attempted = 0
    saved = 0
    cnk_hashes: Dict[str, set] = {}
    cnk_phashes: Dict[str, List[int]] = {}
    missing: List[Dict[str, str]] = []

    total = len(records)
    done = 0
    next_update = 0.0

    for rec in records:
        attempted += 1
        pid = rec["pid"]
        cnk = rec["cnk"]
        url = rec["url"]

        if not cnk:
            missing.append({"Product ID": pid, "CNK": None, "URL": url, "Reason": "No CNK"})
            done += 1
            frac = done / max(1, total)
            if frac >= next_update:
                zip_prog.progress(frac); next_update += 0.05
            continue

        triple = processed_map.get(url)
        if not triple:
            reason = "Download failed" if url_contents.get(url) is None else "Processing failed"
            missing.append({"Product ID": pid, "CNK": cnk, "URL": url, "Reason": reason})
            done += 1
            frac = done / max(1, total)
            if frac >= next_update:
                zip_prog.progress(frac); next_update += 0.05
            continue

        jb, dh, md5 = triple

        if cnk not in cnk_hashes:
            cnk_hashes[cnk] = set()
        if cnk not in cnk_phashes:
            cnk_phashes[cnk] = []

        if md5 in cnk_hashes[cnk]:
            done += 1
            frac = done / max(1, total)
            if frac >= next_update:
                zip_prog.progress(frac); next_update += 0.05
            continue
        if any(_hamming(dh, existing) <= DEDUP_DHASH_THRESHOLD for existing in cnk_phashes[cnk]):
            done += 1
            frac = done / max(1, total)
            if frac >= next_update:
                zip_prog.progress(frac); next_update += 0.05
            continue

        cnk_hashes[cnk].add(md5)
        cnk_phashes[cnk].append(dh)
        n = len(cnk_hashes[cnk])
        filename = f"BE0{cnk}-{lang}-h{n}.jpg"
        zf.writestr(filename, jb)
        saved += 1

        done += 1
        frac = done / max(1, total)
        if frac >= next_update:
            zip_prog.progress(frac); next_update += 0.05

    # prodotti senza righe "Photos"
    for pid, cnk in id_cnk.values:
        pid = str(pid)
        cnk = str(cnk)
        if pid not in all_pids_set:
            missing.append({"Product ID": pid, "CNK": cnk, "URL": None, "Reason": "No photos in export"})

    zf.close()
    zip_prog.progress(1.0)
    return zip_buf.getvalue(), attempted, saved, missing

# ===============================
# Orchestrator — single session for NL/FR
# ===============================
def run_exports_with_progress_single_session(email: str, password: str, refs: str, langs: List[str], prog_widget, start: float, end: float):
    """
    Una sola sessione Chrome: login una volta, poi export per le lingue richieste
    """
    results = {}
    tmpdir = tempfile.mkdtemp(prefix="medipim_all_")
    ctx = make_ctx(tmpdir)
    try:
        do_login(ctx, email, password)
        step = (end - start) / max(1, len(langs))
        for i, lang in enumerate(langs):
            prog_widget.progress(start + step * i)
            data = run_export_and_get_bytes(ctx, lang, refs)
            if data:
                results[lang] = data
            else:
                st.error(f"{lang.upper()} export failed: no XLSX found.")
            prog_widget.progress(start + step * (i + 1))
    finally:
        try:
            ctx["driver"].quit()
        except Exception:
            pass
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return results

# ===============================
# Main flow
# ===============================
if submitted:
    st.session_state["exports"] = {}
    st.session_state["photo_zip"] = {}
    st.session_state["missing_lists"] = {}

    if not email or not password:
        st.error("Please enter your email and password.")
    else:
        skus = parse_skus(sku_text, uploaded_skus)
        if not skus:
            st.error("Please provide at least one SKU (textarea or Excel).")
        else:
            refs = " ".join(skus)
            if scope == "NL only":
                langs = ["nl"]
            elif scope == "FR only":
                langs = ["fr"]
            else:
                langs = ["nl", "fr"]

            # progress globale
            main_prog = st.progress(0.0)
            # Phase 1: exports (0.0 → 0.5 se una lingua, 0.0 → 0.6 se due)
            export_end = 0.5 if len(langs) == 1 else 0.6
            results = run_exports_with_progress_single_session(email, password, refs, langs, main_prog, 0.0, export_end)
            if not results:
                st.stop()

            # Phase 2: processing per lingua
            proc_start = export_end
            proc_end = 1.0
            per_lang = (proc_end - proc_start) / max(1, len(langs))

            for i, lg in enumerate(langs):
                if lg in results:
                    st.info(f"Processing {lg.upper()} images…")
                    scaled = ScaledProgress(main_prog, proc_start + per_lang * i, proc_start + per_lang * (i + 1))
                    z_lg, a_lg, s_lg, miss = build_zip_for_lang(results[lg], lang=lg, progress=scaled)
                    st.session_state["photo_zip"][lg] = z_lg
                    st.session_state["missing_lists"][lg] = miss
                    st.success(f"{lg.upper()}: saved {s_lg} images.")
            main_prog.progress(1.0)

            # merge ZIP se NL+FR
            if scope == "All (NL + FR)" and ("nl" in st.session_state["photo_zip"] or "fr" in st.session_state["photo_zip"]):
                combo = io.BytesIO()
                with zipfile.ZipFile(combo, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
                    for lg in ("nl", "fr"):
                        if lg in st.session_state["photo_zip"]:
                            with zipfile.ZipFile(io.BytesIO(st.session_state["photo_zip"][lg])) as zlg:
                                for name in zlg.namelist():
                                    z.writestr(name, zlg.read(name))
                st.session_state["photo_zip"]["all"] = combo.getvalue()

# ===============================
# Downloads (ZIP and missing list)
# ===============================
if st.session_state["photo_zip"]:
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = f"medipim_photos_{ts}"

    st.markdown("### Downloads")
    if "all" in st.session_state["photo_zip"]:
        st.download_button(
            "Download ALL photos (ZIP)",
            data=io.BytesIO(st.session_state["photo_zip"]["all"]),
            file_name=f"{base}_ALL.zip",
            mime="application/zip",
            key="zip_all",
        )
    if "nl" in st.session_state["photo_zip"] and "all" not in st.session_state["photo_zip"]:
        st.download_button(
            "Download NL photos (ZIP)",
            data=io.BytesIO(st.session_state["photo_zip"]["nl"]),
            file_name=f"{base}_NL.zip",
            mime="application/zip",
            key="zip_nl",
        )
    if "fr" in st.session_state["photo_zip"] and "all" not in st.session_state["photo_zip"]:
        st.download_button(
            "Download FR photos (ZIP)",
            data=io.BytesIO(st.session_state["photo_zip"]["fr"]),
            file_name=f"{base}_FR.zip",
            mime="application/zip",
            key="zip_fr",
        )

    if st.session_state["missing_lists"]:
        miss_all = []
        for lg, miss in st.session_state["missing_lists"].items():
            for row in miss:
                row["Lang"] = lg.upper()
                miss_all.append(row)
        if miss_all:
            miss_df = pd.DataFrame(miss_all)
            miss_buf = io.BytesIO()
            with pd.ExcelWriter(miss_buf, engine="openpyxl") as writer:
                miss_df.to_excel(writer, index=False)
            st.download_button(
                "Download missing images list (.xlsx)",
                data=miss_buf.getvalue(),
                file_name=f"{base}_MISSING.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="miss_xlsx",
            )
