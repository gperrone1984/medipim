import base64
import io
import json
import os
from typing import List, Dict, Optional

import requests
import streamlit as st

# ---------------------------
# App UI (English-only as requested)
# ---------------------------
st.set_page_config(page_title="Medipim 1500×1500 Image Downloader", page_icon="🧰")
st.title("🧰 Medipim 1500×1500 Image Downloader")
st.write("Download packshots/productshots at 1500×1500 (format: 'huge') via the official API.")

with st.expander("🔐 Credentials", expanded=True):
    st.caption(
        "Use an API Key ID and Secret created in Medipim (not your platform password)."
    )
    api_key_id = st.text_input("API Key ID", value=st.secrets.get("MEDIPIM_KEY_ID", ""))
    api_key_secret = st.text_input(
        "API Key Secret", value=st.secrets.get("MEDIPIM_KEY_SECRET", ""), type="password"
    )
    env = st.selectbox(
        "Environment",
        [
            "Production (api.medipim.be)",
            "Sandbox (api.sandbox.medipim.be)",
        ],
        index=0,
    )

API_BASE = "https://api.medipim.be" if env.startswith("Production") else "https://api.sandbox.medipim.be"

# Basic auth header
basic_token = base64.b64encode(f"{api_key_id}:{api_key_secret}".encode()).decode()
AUTH_HEADER = {"Authorization": f"Basic {basic_token}", "Content-Type": "application/json"}

# Always include a descriptive user-agent for image downloads (per docs)
USER_AGENT = "RedcarePDM-ImageDownloader/1.0 (Streamlit)"

st.divider()

# ---------------------------
# Input of identifiers
# ---------------------------
st.subheader("Product identifiers (CNK or EAN/GTIN)")
mode = st.radio("Input mode", ["Paste list", "Upload CSV"], horizontal=True)

codes: List[str] = []
if mode == "Paste list":
    raw = st.text_area(
        "Enter one code per line (CNK 7-digit or EAN/GTIN 8/12/13/14).",
        height=150,
        placeholder="1234567\n5412345678901\n...",
    )
    codes = [c.strip() for c in raw.splitlines() if c.strip()]
elif mode == "Upload CSV":
    f = st.file_uploader("Upload CSV with a single column 'code'", type=["csv"]) 
    if f is not None:
        import pandas as pd
        df = pd.read_csv(f)
        if "code" not in df.columns:
            st.error("CSV must contain a 'code' column.")
        else:
            codes = [str(x).strip() for x in df["code"].tolist() if str(x).strip()]
            st.dataframe(df)

output_dir = st.text_input("Output folder name", value="medipim_images")
filter_photo_type = st.selectbox(
    "Photo type filter (optional)",
    ["Any", "packshot", "productshot", "lifestyle_image", "pillshot"],
)

run = st.button("Start download", type="primary", disabled=not(api_key_id and api_key_secret and len(codes) > 0))

# ---------------------------
# API helpers
# ---------------------------

def _req_json(method: str, path: str, json_body: Optional[Dict] = None, params: Optional[Dict] = None):
    url = f"{API_BASE}{path}"
    r = requests.request(method, url, headers=AUTH_HEADER, json=json_body, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def find_product(code: str) -> Optional[str]:
    """Return Medipim product ID for a CNK/EAN/GTIN code.
    Uses /v4/products/find when possible; falls back to /v4/products/query.
    """
    # 1) Try /v4/products/find with the identifier (docs: any unique identifier allowed)
    # We'll try common keys in order of likelihood.
    keys = ["id", "cnk", "ean", "eanGtin13", "eanGtin14", "eanGtin12", "eanGtin8"]
    for k in keys:
        try:
            data = _req_json("GET", "/v4/products/find", params={k: code})
            if isinstance(data, dict) and data.get("id"):
                return data["id"]
        except requests.HTTPError as e:
            # ignore and try next key
            pass
    # 2) Fallback: query endpoint
    filters = []
    for k in ["cnk", "eanGtin13", "eanGtin14", "eanGtin12", "eanGtin8"]:
        filters.append({k: code})
    body = {
        "filter": {"or": filters},
        "sorting": {"touchedAt": "DESC"},
        "page": {"no": 0, "size": 1},
    }
    try:
        data = _req_json("POST", "/v4/products/query", json_body=body)
        results = data.get("results", [])
        if results:
            return results[0].get("id")
    except requests.HTTPError:
        pass
    return None


def list_media_product(product_id: str) -> List[Dict]:
    """Query media for a given product id. Returns list of media items (photos/frontals)."""
    media_filter = {"product": product_id, "available": True}
    if filter_photo_type != "Any":
        media_filter = {"and": [{"product": product_id}, {"available": True}, {"type": "photo"}, {"photoType": filter_photo_type}]}
    body = {
        "filter": media_filter,
        "sorting": {"touchedAt": "DESC"},
        "page": {"no": 0, "size": 250},
    }
    data = _req_json("POST", "/v4/media/query", json_body=body)
    return data.get("results", [])


def download_url(url: str) -> bytes:
    # Per docs: include a descriptive User-Agent when requesting image binaries
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=120)
    r.raise_for_status()
    return r.content


# ---------------------------
# Run
# ---------------------------
if run:
    os.makedirs(output_dir, exist_ok=True)
    missing: List[str] = []
    downloaded: List[Dict] = []

    progress = st.progress(0)
    for idx, code in enumerate(codes, start=1):
        progress.progress(idx / max(len(codes), 1))
        with st.spinner(f"Processing {code}..."):
            pid = find_product(code)
            if not pid:
                missing.append(code)
                continue
            media_items = list_media_product(pid)
            if not media_items:
                missing.append(code)
                continue
            # Prefer packshot/productshot with 1500×1500 'huge' format
            chosen = None
            for m in media_items:
                if m.get("type") == "photo" and m.get("formats", {}).get("huge"):
                    if filter_photo_type == "Any" or m.get("photoType") == filter_photo_type:
                        chosen = m
                        break
            if not chosen:
                # fallback: any item that has a huge
                for m in media_items:
                    if m.get("formats", {}).get("huge"):
                        chosen = m
                        break
            if not chosen:
                missing.append(code)
                continue
            url = chosen["formats"]["huge"]  # 1500×1500
            try:
                blob = download_url(url)
            except Exception:
                missing.append(code)
                continue
            # Build filename: <code>-<type>-<id>.jpg
            photo_type = chosen.get("photoType", chosen.get("type", "photo"))
            fname = f"{code}-{photo_type}-{chosen.get('id','media')}.jpg"
            path = os.path.join(output_dir, fname)
            with open(path, "wb") as f:
                f.write(blob)
            downloaded.append({"code": code, "file": path, "url": url})

    st.success(f"Downloaded {len(downloaded)} images to '{output_dir}'.")

    if downloaded:
        import pandas as pd
        df = pd.DataFrame(downloaded)
        st.dataframe(df)
        # Zip for download
        import zipfile
        zip_path = f"{output_dir}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for item in downloaded:
                z.write(item["file"], arcname=os.path.basename(item["file"]))
        with open(zip_path, "rb") as f:
            st.download_button(
                label="Download ZIP",
                data=f,
                file_name=os.path.basename(zip_path),
                mime="application/zip",
            )
    if missing:
        st.warning(f"Missing images for {len(missing)} codes.")
        st.text("\n".join(missing))

st.caption("Built for Redcare PDM: uses /v4/products.find|query and /v4/media/query. Images use the 1500×1500 'huge' format with a proper User-Agent.")
