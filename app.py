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

    st.subheader("SKU or CNK codes")
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
    except WebDriverException:
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
    drv = ctx["driver"]
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

def click_excel_option(ctx, dropdown):
    actions = ctx["actions"]
    excel_btn = dropdown.find_element(By.CSS_SELECTOR, "div.actions > button:nth-of-type(2)")
    try:
        actions.move_to_element(excel_btn).pause(0.1).click().perform()
    except Exception:
        ctx["driver"].execute_script("arguments[0].click();", excel_btn)

def select_all_attributes(ctx):
    drv = ctx["driver"]
    try:
        all_attr = WebDriverWait(drv, 8).until(
            EC.element_to_be_clickable((By.XPATH,
                "//a[contains(., 'Alles selecteren')] | //button[contains(., 'Select all')]"
            ))
        )
        drv.execute_script("arguments[0].click();", all_attr)
    except TimeoutException:
        pass

def wait_for_xlsx_on_disk(ctx, start_time: float, timeout=60) -> pathlib.Path | None:
    download_dir = ctx["download_dir"]
    end = time.time() + timeout
    while time.time() < end:
        files = [f for f in os.listdir(download_dir) if f.lower().endswith(".xlsx")]
        if files:
            return pathlib.Path(os.path.join(download_dir, files[0]))
        time.sleep(0.5)
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
        create_btn = WebDriverWait(drv, 25).until(EC.element_to_be_clickable((By.XPATH,
            "//button[contains(., 'Create')] | //button[contains(., 'Créer')] | //button[contains(., 'Aanmaken')]"
        )))
        drv.execute_script("arguments[0].click();", create_btn)
        WebDriverWait(drv, 40).until(EC.presence_of_element_located((By.XPATH,"//*[contains(., 'Export')]")))
        dl = wait.until(EC.element_to_be_clickable((By.XPATH,
            "//button[contains(., 'DOWNLOAD')] | //a[contains(., 'Download')] | //button[contains(., 'Télécharger')]"
        )))
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
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    return digits.lstrip("0") or digits

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
    seen, out = set(), []
    for s in skus:
        norm = _normalize_sku(s)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out

# ===============================
# Image helpers
# ===============================
DEDUP_DHASH_THRESHOLD = 3

def _fetch_url_cached(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200 or not r.content:
            return None
        return r.content
    except Exception:
        return None

def _download_many(urls: List[str], progress: Optional[st.progress] = None, max_workers: int = 16) -> Dict[str, Optional[bytes]]:
    results: Dict[str, Optional[bytes]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_url_cached, u) for u in urls]
        for u, f in zip(urls, futures):
            results[u] = f.result()
    return results

def _to_1000_canvas(img: Image.Image) -> Image.Image:
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = ImageOps.contain(img, (1000, 1000))
    canvas = Image.new("RGB", (1000, 1000), (255, 255, 255))
    x = (1000 - img.width) // 2
    y = (1000 - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas

def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()

def _dhash(image: Image.Image, hash_size: int = 8) -> int:
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

# ===============================
# Build ZIP (semplificato)
# ===============================
def build_zip_for_lang(xlsx_bytes: bytes, lang: str, progress: st.progress):
    xl = pd.ExcelFile(io.BytesIO(xlsx_bytes))
    products = xl.parse(xl.sheet_names[0])
    photos = xl.parse("Photos") if "Photos" in xl.sheet_names else xl.parse(xl.sheet_names[1])
    ids = products.iloc[:,0].astype(str).tolist()
    urls = photos.iloc[:,1].astype(str).tolist()

    contents = _download_many(urls)
    zip_buf = io.BytesIO()
    zf = zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED)

    hashes = set()
    saved, attempted = 0, 0
    for pid, url in zip(ids, urls):
        attempted += 1
        content = contents.get(url)
        if not content: continue
        try:
            img = Image.open(io.BytesIO(content))
            img.load()
            processed = _to_1000_canvas(img)
            jb = _jpeg_bytes(processed)
            md5 = _hash_bytes(jb)
            if md5 in hashes: continue
            hashes.add(md5)
            zf.writestr(f"{pid}-{lang}.jpg", jb)
            saved += 1
        except Exception:
            continue
    zf.close()
    return zip_buf.getvalue(), attempted, saved, []

# ===============================
# Orchestrator
# ===============================
def run_exports_with_progress_single_session(email: str, password: str, refs: str, langs: List[str], prog_widget, start: float, end: float):
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
                st.error("At the moment it is not possible to download the images, please try again later.")
            prog_widget.progress(start + step * (i + 1))
    finally:
        try: ctx["driver"].quit()
        except Exception: pass
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception: pass
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
            st.error("Please provide at least one SKU or CNK code.")
        else:
            refs = " ".join(skus)
            langs = ["nl"] if scope == "NL only" else ["fr"] if scope == "FR only" else ["nl", "fr"]
            main_prog = st.progress(0.0)
            export_end = 0.5 if len(langs) == 1 else 0.6
            results = run_exports_with_progress_single_session(email, password, refs, langs, main_prog, 0.0, export_end)
            if not results:
                st.stop()
            proc_start = export_end
            per_lang = (1.0 - proc_start) / max(1, len(langs))
            for i, lg in enumerate(langs):
                if lg in results:
                    st.info(f"Processing {lg.upper()} images…")
                    z_lg, a_lg, s_lg, miss = build_zip_for_lang(results[lg], lg, main_prog)
                    st.session_state["photo_zip"][lg] = z_lg
                    st.session_state["missing_lists"][lg] = miss
                    st.success(f"{lg.upper()}: saved {s_lg} images.")
            main_prog.progress(1.0)

# ===============================
# Downloads
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
