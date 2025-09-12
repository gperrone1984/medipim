# app.py
import os
import io
import time
import json
import base64
import tempfile
import pathlib
import pandas as pd
import streamlit as st

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

st.set_page_config(page_title="Medipim Export (NL+FR)", page_icon="📦", layout="centered")
st.title("Medipim Export (NL + FR)")

# ---------------- Session state ----------------
if "exports" not in st.session_state:
    st.session_state["exports"] = {}    # {"nl": bytes, "fr": bytes}
if "last_refs" not in st.session_state:
    st.session_state["last_refs"] = ""

# ---------------- UI ----------------
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
    uploaded = st.file_uploader("Or upload an Excel with a 'sku' column", type=["xlsx"])
    dedup = st.checkbox("Deduplicate SKUs", value=True)

    submitted = st.form_submit_button("Run NL + FR export")

# ---------------- Driver factory (robust for Streamlit Cloud) ----------------
def make_ctx(download_dir: str):
    """Create a headless Chrome session with robust flags and a system fallback."""
    import os, tempfile
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.common.action_chains import ActionChains

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
        driver = webdriver.Chrome(options=opt)  # Selenium Manager
    except WebDriverException as e_a:
        chromebin = "/usr/bin/chromium"
        chromedrv = "/usr/bin/chromedriver"
        if os.path.exists(chromebin) and os.path.exists(chromedrv):
            from selenium.webdriver.chrome.service import Service
            opt = build_options()
            opt.binary_location = chromebin
            service = Service(chromedrv)
            try:
                driver = webdriver.Chrome(service=service, options=opt)
            except WebDriverException as e_b:
                raise WebDriverException(
                    f"Chrome failed to start (system fallback also failed): {e_b}"
                ) from e_b
        else:
            raise WebDriverException(
                f"Chrome failed to start via Selenium Manager: {e_a}. "
                "No system Chromium found for fallback."
            ) from e_a

    wait = WebDriverWait(driver, 40)
    actions = ActionChains(driver)

    try: driver.execute_cdp_cmd("Network.enable", {})
    except Exception: pass
    try: driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": download_dir})
    except Exception: pass

    return {"driver": driver, "wait": wait, "actions": actions, "download_dir": download_dir}

# ---------------- Helpers ----------------
def handle_cookies(ctx):
    drv, wait = ctx["driver"], ctx["wait"]
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

def ensure_language(ctx, lang: str):  # 'nl' or 'fr'
    drv, wait = ctx["driver"], ctx["wait"]
    base = f"https://platform.medipim.be/{'nl/home' if lang=='nl' else 'fr/home'}"
    drv.get(base)
    handle_cookies(ctx)
    try:
        trig_span = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, ".I18nMenu .Dropdown > button.trigger span")))
        current = trig_span.text.strip().lower()
    except TimeoutException:
        current = ""
    if current != lang:
        try:
            trig = drv.find_element(By.CSS_SELECTOR, ".I18nMenu .Dropdown > button.trigger")
            drv.execute_script("arguments[0].click();", trig); time.sleep(0.2)
            if lang == "nl":
                lang_link = wait.until(EC.element_to_be_clickable(
                    (By.XPATH, "//div[contains(@class,'I18nMenu')]//a[contains(@href,'/nl/')]")))
            else:
                lang_link = wait.until(EC.element_to_be_clickable(
                    (By.XPATH, "//div[contains(@class,'I18nMenu')]//a[contains(@href,'/fr/')]")))
            drv.execute_script("arguments[0].click();", lang_link); time.sleep(0.4)
        except TimeoutException:
            pass

def open_export_dropdown(ctx):
    drv, wait = ctx["driver"], ctx["wait"]
    split = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.SplitButton")))
    trigger = split.find_element(By.CSS_SELECTOR, "button.trigger")
    for _ in range(4):
        if trigger.get_attribute("aria-expanded") == "true":
            break
        drv.execute_script("arguments[0].click();", trigger); time.sleep(0.25)
    if trigger.get_attribute("aria-expanded") != "true":
        raise TimeoutException("Export dropdown did not open.")
    dd = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.Dropdown.open div.dropdown")))
    return dd

def click_excel_option(ctx, dropdown):
    actions = ctx["actions"]
    excel_btn = dropdown.find_element(By.CSS_SELECTOR, "div.actions > button:nth-of-type(2)")
    label = excel_btn.text.strip().replace("\n", " ")
    assert ("excel" in label.lower()) or ("xlsx" in label.lower())
    try:
        actions.move_to_element(excel_btn).pause(0.1).click().perform()
    except Exception:
        ctx["driver"].execute_script("arguments[0].click();", excel_btn)

def select_all_attributes(ctx):
    """Click 'Select all' in any of the supported languages before creating the export."""
    drv = ctx["driver"]
    try:
        all_attr = WebDriverWait(drv, 8).until(
            EC.element_to_be_clickable((By.XPATH,
                "//a[contains(., 'Alles selecteren')] | //button[contains(., 'Alles selecteren')] | "
                "//a[contains(., 'Sélectionner tout') or contains(., 'Selectionner tout')] | "
                "//button[contains(., 'Sélectionner tout') or contains(., 'Selectionner tout')] | "
                "//button[contains(., 'Select all')] | //a[contains(., 'Select all')]"
            ))
        )
        drv.execute_script("arguments[0].click();", all_attr)
    except TimeoutException:
        # Sometimes the attribute screen isn't shown; that's fine.
        pass

def wait_for_xlsx_on_disk(ctx, start_time: float, timeout=60) -> pathlib.Path | None:
    download_dir = ctx["download_dir"]
    end = time.time() + timeout
    margin = 2.0
    while time.time() < end:
        files = [
            (f, os.path.getmtime(os.path.join(download_dir, f)))
            for f in os.listdir(download_dir)
            if f.lower().endswith(".xlsx")
        ]
        fresh = [f for f, m in files if m >= (start_time - margin)]
        if fresh:
            fresh.sort(key=lambda f: os.path.getmtime(os.path.join(download_dir, f)), reverse=True)
            return pathlib.Path(os.path.join(download_dir, fresh[0]))
        time.sleep(0.5)
    return None

def try_save_xlsx_from_perflog(ctx, timeout=12) -> bytes | None:
    drv = ctx["driver"]
    deadline = time.time() + timeout
    seen = set()
    try:
        drv.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    while time.time() < deadline:
        try:
            logs = drv.get_log('performance')
        except Exception:
            logs = []
        for entry in logs:
            try:
                payload = json.loads(entry.get('message', '{}'))
                m = payload.get("message", {})
            except Exception:
                continue
            if m.get("method") != "Network.responseReceived":
                continue
            params = m.get("params", {})
            resp = params.get("response", {})
            req_id = params.get("requestId")
            if not req_id or req_id in seen:
                continue
            seen.add(req_id)
            mime = (resp.get("mimeType") or "").lower()
            url  = (resp.get("url") or "").lower()
            if ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in mime) or url.endswith(".xlsx"):
                try:
                    body = drv.execute_cdp_cmd('Network.getResponseBody', {'requestId': req_id})
                    data = body.get('body', '')
                    raw = base64.b64decode(data) if body.get('base64Encoded') else data.encode('utf-8', 'ignore')
                    return raw
                except Exception:
                    pass
        time.sleep(0.4)
    return None

def run_export_and_get_bytes(ctx, lang: str, refs: str) -> bytes | None:
    """Trigger Excel export (with ALL attributes) and return XLSX bytes (disk or CDP fallback)."""
    ensure_language(ctx, lang)
    if lang == "nl":
        url = f"https://platform.medipim.be/nl/producten?search=refcode[{refs.replace(' ', '%20')}]"
    else:
        url = f"https://platform.medipim.be/fr/produits?search=refcode[{refs.replace(' ', '%20')}]"

    drv, wait = ctx["driver"], ctx["wait"]
    drv.get(url)
    handle_cookies(ctx)

    # Open Export and choose Excel
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.SplitButton")))
    dd = open_export_dropdown(ctx)
    click_excel_option(ctx, dd)

    # >>> IMPORTANT: select ALL attributes <<<
    select_all_attributes(ctx)

    # Create export (AANMAKEN / Créer / Create)
    try:
        create_btn = WebDriverWait(drv, 25).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[contains(., 'AANMAKEN')] | //button[contains(., 'Aanmaken')] | "
                "//button[contains(., 'Create')] | "
                "//button[contains(., 'Créer') or contains(., 'Creer')]"
            ))
        )
        drv.execute_script("arguments[0].click();", create_btn)
    except TimeoutException:
        pass

    # Wait for "ready" (best-effort)
    try:
        WebDriverWait(drv, 40).until(
            EC.presence_of_element_located((By.XPATH,
                "//*[contains(., 'Export is klaar') or contains(., 'Export gereed') or "
                "contains(., 'Export ready') or contains(., 'Export prêt') or contains(., 'Export est prêt')]"
            ))
        )
    except TimeoutException:
        pass

    # Click Download (or direct href)
    dl = wait.until(
        EC.element_to_be_clickable((By.XPATH,
            "//button[contains(., 'DOWNLOAD')] | //a[contains(., 'DOWNLOAD')] | "
            "//button[contains(., 'Download')] | //a[contains(., 'Download')] | "
            "//button[contains(., 'Télécharger') or contains(., 'Telecharger')] | "
            "//a[contains(., 'Télécharger') or contains(., 'Telecharger')]"
        ))
    )
    href = (dl.get_attribute("href") or dl.get_attribute("data-href") or "").strip().lower()
    start = time.time()
    if href and (not href.startswith("javascript")) and (not href.startswith("blob:")):
        drv.get(href)
    else:
        drv.execute_script("arguments[0].click();", dl)

    # Disk first
    disk = wait_for_xlsx_on_disk(ctx, start_time=start, timeout=60)
    if disk and disk.exists():
        return disk.read_bytes()

    # CDP fallback
    return try_save_xlsx_from_perflog(ctx, timeout=12)

def do_login(ctx, email_addr: str, pwd: str):
    drv, wait = ctx["driver"], ctx["wait"]
    drv.get("https://platform.medipim.be/nl/inloggen")
    handle_cookies(ctx)
    try:
        email_el = wait.until(EC.presence_of_element_located((By.ID, "form0.email")))
        pwd_el   = wait.until(EC.presence_of_element_located((By.ID, "form0.password")))
        email_el.clear(); email_el.send_keys(email_addr)
        pwd_el.clear();   pwd_el.send_keys(pwd)
        submit = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.SubmitButton")))
        drv.execute_script("arguments[0].click();", submit)
        wait.until(EC.invisibility_of_element_located((By.ID, "form0.email")))
    except TimeoutException:
        pass  # likely already logged in

def run_both_exports(email: str, password: str, refs: str):
    """Run NL and FR export in two isolated sessions. Return dict with bytes."""
    results = {}
    for lang in ("nl", "fr"):
        with st.spinner(f"Running {lang.upper()} export..."):
            tmpdir = tempfile.mkdtemp(prefix=f"medipim_{lang}_")
            ctx = make_ctx(tmpdir)
            try:
                do_login(ctx, email, password)
                data = run_export_and_get_bytes(ctx, lang, refs)
                if data:
                    results[lang] = data
                else:
                    st.error(f"{lang.upper()} export failed: no XLSX found.")
            finally:
                try:
                    ctx["driver"].quit()
                except Exception:
                    pass
    return results

# ---------------- SKU parsing ----------------
def parse_skus(sku_text: str, uploaded_file, dedup_on: bool) -> list[str]:
    """Merge SKUs from textarea and optional Excel file (column 'sku')."""
    skus = []

    if sku_text:
        raw = sku_text.replace(",", " ").split()
        skus.extend([x.strip() for x in raw if x.strip()])

    if uploaded_file is not None:
        try:
            df = pd.read_excel(uploaded_file, engine="openpyxl")
            df.columns = [c.lower() for c in df.columns]
            if "sku" not in df.columns:
                st.error("The uploaded Excel must contain a 'sku' column.")
                return []
            ex_skus = df["sku"].astype(str).map(lambda x: x.strip()).tolist()
            skus.extend([x for x in ex_skus if x])
        except Exception as e:
            st.error(f"Failed to read Excel: {e}")
            return []

    if dedup_on:
        seen, out = set(), []
        for s in skus:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out
    return skus

# ---------------- Action ----------------
if submitted:
    # Reset previous exports so buttons don't show stale files
    st.session_state["exports"] = {}
    st.session_state["last_refs"] = ""

    if not email or not password:
        st.error("Please enter your email and password.")
    else:
        skus = parse_skus(sku_text, uploaded, dedup_on=dedup)
        if not skus:
            st.error("Please provide at least one SKU (textarea or Excel).")
        else:
            refs = " ".join(skus)
            st.session_state["last_refs"] = refs
            results = run_both_exports(email, password, refs)
            # Persist results so download buttons survive reruns
            if results:
                st.session_state["exports"] = results

# ---------------- Download buttons (persist across reruns) ----------------
if st.session_state["exports"]:
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = f"medipim_export_{ts}"
    col1, col2 = st.columns(2)
    if "nl" in st.session_state["exports"]:
        with col1:
            st.success("NL export ready")
            st.download_button(
                "Download NL (.xlsx)",
                data=io.BytesIO(st.session_state["exports"]["nl"]),
                file_name=f"{base}-nl.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_nl",  # unique key so clicking NL doesn't affect FR button identity
            )
    if "fr" in st.session_state["exports"]:
        with col2:
            st.success("FR export ready")
            st.download_button(
                "Download FR (.xlsx)",
                data=io.BytesIO(st.session_state["exports"]["fr"]),
                file_name=f"{base}-fr.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_fr",
            )
