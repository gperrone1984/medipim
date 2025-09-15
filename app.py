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

# ===============================
# Selenium driver + helpers (unchanged)
# ===============================
# ... keep all selenium helper functions here ...

# ===============================
# SKU parsing (always deduplicated)
# ===============================
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
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

# ===============================
# Photo processing
# ===============================
TYPE_RANK = {
    "photo du produit": 1,
    "productfoto": 1,
    "photo de l'emballage": 2,
    "verpakkingsfoto": 2,
    "photo promotionnelle": 3,
    "sfeerbeeld": 3,
}


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


def _download_image(url: str):
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200 or not r.content:
            return None
        img = Image.open(io.BytesIO(r.content))
        img.load()
        return img
    except Exception:
        return None


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
    img.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()


def _hash_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def build_zip_for_lang(xlsx_bytes: bytes, lang: str, progress: st.progress) -> Tuple[bytes, int, int, List[Dict[str, str]]]:
    products_df, photos_df = _read_book(xlsx_bytes)
    id_cnk = _extract_id_cnk(products_df)
    photos = _extract_photos(photos_df)

    id2cnk: Dict[str, str] = {str(row["ID"]).strip(): str(row["CNK"]).strip() for _, row in id_cnk.iterrows()}

    def _rank_type(t: str) -> int:
        if not isinstance(t, str):
            return 99
        return TYPE_RANK.get(t.strip().lower(), 99)

    photos = photos.dropna(subset=["URL"]).copy()
    photos["rank_type"] = photos["Type"].map(_rank_type)
    photos["rank_photoid"] = pd.to_numeric(photos["Photo ID"], errors="coerce").fillna(10**9).astype(int)
    photos.sort_values(["Product ID", "rank_type", "rank_photoid"], inplace=True)

    zip_buf = io.BytesIO()
    zf = zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED)

    attempted = 0
    saved = 0
    cnk_hashes: Dict[str, set] = {}
    missing: List[Dict[str, str]] = []

    total = len(photos)
    last_update = 0

    for _, r in photos.iterrows():
        attempted += 1
        pid = str(r["Product ID"]).strip()
        url = str(r["URL"]).strip()
        cnk = id2cnk.get(pid)
        if not cnk:
            missing.append({"Product ID": pid, "CNK": None, "URL": url, "Reason": "No CNK"})
            continue

        img = _download_image(url)
        if img is None:
            missing.append({"Product ID": pid, "CNK": cnk, "URL": url, "Reason": "Download failed"})
            continue

        processed = _to_1000_canvas(img)
        jb = _jpeg_bytes(processed)
        h = _hash_bytes(jb)

        if cnk not in cnk_hashes:
            cnk_hashes[cnk] = set()
        if h in cnk_hashes[cnk]:
            continue

        cnk_hashes[cnk].add(h)
        n = len(cnk_hashes[cnk])
        filename = f"BE0{cnk}-{lang}-h{n}.jpg"
        zf.writestr(filename, jb)
        saved += 1

        frac = attempted / max(1, total)
        if frac - last_update >= 0.01:
            progress.progress(min(1.0, frac))
            last_update = frac

    zf.close()
    return zip_buf.getvalue(), attempted, saved, missing

# ===============================
# Orchestrator
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

            results = run_exports(email, password, refs, langs)
            if not results:
                st.stop()

            for lg in langs:
                if lg in results:
                    st.info(f"Processing {lg.upper()} images…")
                    p = st.progress(0.0)
                    z_lg, a_lg, s_lg, miss = build_zip_for_lang(results[lg], lang=lg, progress=p)
                    st.session_state["photo_zip"][lg] = z_lg
                    st.session_state["missing_lists"][lg] = miss
                    st.success(f"{lg.upper()}: saved {s_lg} images.")

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
            miss_df.to_excel(miss_buf, index=False, engine="openpyxl")
            st.download_button(
                "Download missing images list (.xlsx)",
                data=miss_buf.getvalue(),
                file_name=f"{base}_MISSING.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="miss_xlsx",
            )
