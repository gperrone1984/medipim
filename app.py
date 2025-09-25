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
from requests.exceptions import RequestException

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
        "Paste SKUs (separated by spaces, commas, or newlines)",
        height=120,
        placeholder="e.g. 4811337 4811352\n4811329, 4811345",
    )
    uploaded_skus = st.file_uploader("Or upload an Excel with a 'sku' column (optional)", type=["xlsx"], key="xls_skus")

    st.subheader("Images to download")
    scope = st.radio("Select images", ["All (NL + FR)", "NL only", "FR only"], index=0, horizontal=True)

    submitted = st.form_submit_button("Download photos")

# Clear cache & data button just under the form
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
        chromebin = "/usr/bin/chromium"
        chromedrv = "/usr/bin/chromedriver"
        if os.path.exists(chromebin) and os.path.exists(chromedrv):
            opt = build_options()
            opt.binary_location = chromebin
            service = Service(chromedrv)
            driver = webdriver.Chrome(service=service, options=opt)
        else:
            raise WebDriverException(
                f"Chrome failed to start: {e_a}. No system Chromium found."
            ) from e_a

    wait = WebDriverWait(driver, 40)
    actions = ActionChains(driver)

    try: driver.execute_cdp_cmd("Network.enable", {})
    except Exception: pass
    try: driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": download_dir})
    except Exception: pass

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
    base = f"https://platform.medipim.be/{'nl/home' if lang=='nl' else 'fr/home'}"
    drv.get(base)
    handle_cookies(ctx)

# ... [tutte le altre funzioni Selenium e di export restano invariate] ...

# ===============================
# SKU parsing (modificata con normalizzazione)
# ===============================
def _normalize_sku(raw: str) -> Optional[str]:
    """
    Normalizza un codice SKU:
    - rimuove lettere e simboli
    - toglie gli zeri iniziali
    - ritorna None se non rimane nulla
    """
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
# (tutto il resto del codice: photo processing, build_zip_for_lang,
# run_exports_with_progress_single_session, main submit flow, downloads)
# ===============================
