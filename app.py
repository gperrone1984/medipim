import os
import io
import time
import json
import base64
import tempfile
import pathlib
import hashlib
import zipfile
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

from PIL import Image, ImageOps, ImageDraw
import requests

# ---------------- Streamlit config ----------------
st.set_page_config(page_title="Medipim Photo Downloader (NL/FR)", page_icon="📦", layout="centered")
st.title("Medipim Photo Downloader (NL + FR)")

# ---------------- Session state ----------------
if "photo_outputs" not in st.session_state:
    st.session_state["photo_outputs"] = {}

# ---------------- UI ----------------
st.subheader("Upload Excel Base")
uploaded = st.file_uploader("Upload NL or FR Excel export (with Products + Photos sheets)", type=["xlsx"])

st.subheader("Options")
lang_choice = st.radio("Language of Excel", ["NL", "FR"], index=0, horizontal=True)
type_options = [
    "Product photo",
    "Packaging photo",
    "Promotional photo",
    "Atmosphere photo"
]
selected_types = st.multiselect("Select which types of photos to download", type_options, default=type_options)
go = st.button("Download Photos")

# ---------------- Photo processing helpers ----------------
TYPE_RANK = {
    "photo du produit": "Product photo",
    "productfoto": "Product photo",
    "photo de l'emballage": "Packaging photo",
    "verpakkingsfoto": "Packaging photo",
    "photo promotionnelle": "Promotional photo",
    "sfeerbeeld": "Atmosphere photo",
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
    out["Type"] = df[type_col] if type_col else ""
    out["Photo ID"] = pd.to_numeric(df[photoid_col], errors="coerce") if photoid_col else None
    return out


def _download_image(url: str) -> Image.Image | None:
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
    # white square 60x60 bottom-right
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(940, 940), (999, 999)], fill=(255, 255, 255))
    return canvas


def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()


def _hash_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def build_zip(xlsx_bytes: bytes, lang: str, allowed_types: List[str], progress: st.progress) -> Tuple[bytes, int, int]:
    products_df, photos_df = _read_book(xlsx_bytes)
    id_cnk = _extract_id_cnk(products_df)
    photos = _extract_photos(photos_df)

    # Product ID and ID are treated as strings
    id2cnk = {str(row["ID"]).strip(): str(row["CNK"]).strip() for _, row in id_cnk.iterrows()}

    # Priorities: Type then Photo ID
    photos = photos.dropna(subset=["URL"]).copy()
    photos["rank_type"] = photos["Type"].map(
        lambda t: TYPE_RANK.get(str(t).strip().lower(), "")
    ).map(lambda t: type_options.index(t) + 1 if t in type_options else 99)
    photos["rank_photoid"] = pd.to_numeric(photos["Photo ID"], errors="coerce").fillna(10**9).astype(int)
    photos.sort_values(["Product ID", "rank_type", "rank_photoid"], inplace=True)

    zip_buf = io.BytesIO()
    zf = zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED)

    attempted, saved = 0, 0
    cnk_hashes: Dict[str, set] = {}
    cnk_counts: Dict[str, int] = {}

    total = len(photos)
    last_update = 0

    for _, r in photos.iterrows():
        attempted += 1
        pid = str(r["Product ID"]).strip()
        url = str(r["URL"]).strip()
        cnk = id2cnk.get(pid)
        if not cnk:
            continue

        t_label = TYPE_RANK.get(str(r["Type"]).strip().lower(), None)
        if t_label not in allowed_types:
            continue

        img = _download_image(url)
        if img is None:
            continue

        processed = _to_1000_canvas(img)
        jb = _jpeg_bytes(processed)
        h = _hash_bytes(jb)

        # Deduplicate within same CNK by image hash
        if cnk not in cnk_hashes:
            cnk_hashes[cnk] = set()
        if h in cnk_hashes[cnk]:
            continue
        cnk_hashes[cnk].add(h)

        cnk_counts[cnk] = cnk_counts.get(cnk, 0) + 1
        n = cnk_counts[cnk]

        filename = f"BE0{cnk}-{lang.lower()}-h{n}.jpg"
        zf.writestr(filename, jb)
        saved += 1

        frac = attempted / max(1, total)
        if frac - last_update >= 0.01:
            progress.progress(min(1.0, frac))
            last_update = frac

    zf.close()
    return zip_buf.getvalue(), attempted, saved

# ---------------- Action ----------------
if go and uploaded:
    st.info("Processing images…")
    progress = st.progress(0.0)
    try:
        zbytes, attempted, saved = build_zip(uploaded.read(), lang_choice, selected_types, progress=progress)
        st.success(f"{lang_choice}: saved {saved} / attempted {attempted} images.")
        st.download_button(
            "Download photos (ZIP)",
            data=io.BytesIO(zbytes),
            file_name=f"medipim_photos_{lang_choice}_{time.strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
        )
    except Exception as e:
        st.error(f"Processing failed: {e}")
