import os
import re
import html
import json
import streamlit as st
import requests
from openai import OpenAI

# ---------- Config base ----------
st.set_page_config(page_title="PDM • Product Description Builder", page_icon="🧪", layout="centered")

APP_TITLE = st.secrets.get("APP_TITLE", "PDM • Product Description Builder")
APP_FOOTER = st.secrets.get("APP_FOOTER", "")

# Provider e modello (da secrets/env)
PROVIDER = (st.secrets.get("PROVIDER") or os.getenv("LLM_PROVIDER") or "groq").lower()
BASE_URL = st.secrets.get("BASE_URL") or os.getenv("LLM_BASE_URL")
MODEL = st.secrets.get("MODEL") or os.getenv("LLM_MODEL")

def _pick_api_key() -> str:
    return (
        st.secrets.get("API_KEY") or os.getenv("LLM_API_KEY")
        or st.secrets.get("GROQ_API_KEY") or os.getenv("GROQ_API_KEY")
        or st.secrets.get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")
        or st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    )

API_KEY = _pick_api_key()

if not BASE_URL:
    BASE_URL = "https://api.groq.com/openai/v1" if PROVIDER == "groq" else (
        "https://openrouter.ai/api/v1" if PROVIDER == "openrouter" else None
    )

if not MODEL:
    if PROVIDER == "groq":
        MODEL = "llama-3.1-8b-instant"  # rapido e gratuito
    elif PROVIDER == "openrouter":
        MODEL = "meta-llama/llama-3.1-70b-instruct"
    else:
        MODEL = "gpt-4o-mini"

if not API_KEY or not BASE_URL or not MODEL:
    st.error("Config mancante: imposta PROVIDER/BASE_URL/MODEL e API key nei secrets.")
    st.stop()

default_headers = None
if PROVIDER == "openrouter":
    site = st.secrets.get("OPENROUTER_SITE_URL") or os.getenv("OPENROUTER_SITE_URL") or "https://example.com"
    appn = st.secrets.get("OPENROUTER_APP_NAME") or os.getenv("OPENROUTER_APP_NAME") or "PDM Product Description Builder"
    default_headers = {"HTTP-Referer": site, "X-Title": appn}

client = OpenAI(api_key=API_KEY, base_url=BASE_URL, default_headers=default_headers)

# ---------- Helpers ----------
EAN_REGEX = re.compile(r"^[0-9]{8}$|^[0-9]{12,14}$")  # EAN8/UPC/EAN13/GTIN

def valid_ean(code: str) -> bool:
    if not code:
        return False
    c = re.sub(r"[^0-9]", "", code)
    return bool(EAN_REGEX.match(c))

def build_system_prompt():
    return (
        "Sei un assistente di redazione schede prodotto per e-commerce farmaceutico/parafarmaceutico. "
        "Rispetta le normative, evita claim medici non supportati, usa un tono professionale. "
        "Se i dati sono insufficienti, indica esplicitamente 'Dati mancanti' e NON inventare caratteristiche."
    )

BASE_FIELDS = (
    "EAN, nome commerciale, marca, forma, quantità, ingredienti/Principi attivi, indicazioni d'uso, posologia, avvertenze, "
    "modalità di conservazione, contenuto confezione, produttore, paese di origine"
)

# Prompt "strict": zero segnaposto
PROMPT_TEMPLATE = """
Obiettivo: scrivi una descrizione completa e accurata del prodotto per l'e-commerce.

Regole FERREE:
- Non inventare MAI. Se mancano dati, NON usare segnaposto (es. "[inserire...]").
- Se i dati sono insufficienti, rispondi in modo conciso con una sola sezione:
  "Dati mancanti: <elenca i campi assenti tra {base_fields}>"
- Genera la descrizione completa SOLO se sono presenti almeno: (nome commerciale O marca O categoria) E (forma o quantità).

Lingua: {lang}
Tono: {tone}

Se hai abbastanza dati, usa questa struttura:
1) Abstract (1-2 frasi)
2) Descrizione estesa
3) Caratteristiche principali ({bullets})
4) Modalità d'uso e Avvertenze (se applicabili)
5) Contenuto della confezione
6) Specifiche tecniche (se rilevanti)
{meta}

Dati disponibili:
- Codice/EAN: {ean}
- Dettagli dell'utente: "{user_prompt}"
- Campi attesi (se disponibili): {base_fields}

Nota: per farmaci da banco, tono informativo e conforme; per cosmetici/integratori evita claim medici.
""".strip()

# ---------- Web lookup da EAN ----------
# 1) Open Beauty Facts / Open Food Facts (free, senza chiavi)
OBF_API = "https://world.openbeautyfacts.org/api/v0/product/{ean}.json"
OFF_API = "https://world.openfoodfacts.org/api/v0/product/{ean}.json"

# 2) Fallback HTML "a firma" (aggiungi siti qui se vuoi ampliare)
CANDIDATE_URLS = [
    # Esempi di e-commerce dove spesso l'EAN compare nella pagina (personalizza liberamente)
    "https://www.tuttofarma.it/search?controller=search&s={ean}",
    "https://www.topfarmacia.it/ricerca?controller=search&s={ean}",
    "https://www.amicafarmacia.com/catalogsearch/result/?q={ean}",
    "https://www.farmaciaigea.com/ricerca?controller=search&s={ean}",
    "https://www.amazon.it/s?k={ean}",
]

def _http_get(url: str, timeout=8):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        if r.ok:
            return r
    except Exception:
        pass
    return None

def _clean_text(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def _pick_qty(name: str) -> str:
    if not name:
        return ""
    m = re.search(r"(\d{1,4}\s?(?:ml|mL|ML|g|G|kg|KG|capsule|compresse|p(?:z|z\.)?))\b", name)
    return m.group(1).replace("ML","ml").replace("KG","kg") if m else ""

def _pick_form(name: str) -> str:
    if not name: return ""
    lowers = name.lower()
    for k in ["olio", "crema", "gel", "spray", "lozione", "shampoo", "balsamo", "capsule", "compresse", "gocce"]:
        if k in lowers:
            return k
    return ""

def _dedupe_words(s: str) -> str:
    seen, out = set(), []
    for w in s.split():
        if w.lower() not in seen:
            out.append(w); seen.add(w.lower())
    return " ".join(out)

def fetch_from_open_dbs(ean: str):
    """Prova Open Beauty Facts e Open Food Facts."""
    sources = []
    data = {}

    for base in (OBF_API, OFF_API):
        url = base.format(ean=ean)
        r = _http_get(url)
        if not r:
            continue
        try:
            j = r.json()
        except Exception:
            continue
        if j.get("status") != 1:
            continue
        p = j.get("product", {})
        # Campi utili (best-effort, variano per dataset)
        name = p.get("product_name") or p.get("generic_name") or ""
        brand = ""
        if isinstance(p.get("brands_tags"), list) and p["brands_tags"]:
            brand = p["brands_tags"][0].replace("-", " ").title()
        elif p.get("brands"):
            brand = p["brands"].split(",")[0].strip()
        qty = p.get("quantity") or _pick_qty(name)
        form = _pick_form(name)
        ingred = p.get("ingredients_text") or p.get("ingredients_text_it") or ""

        # Se non c'è nulla di utile, salta
        if not any([name, brand, qty, form, ingred]):
            continue

        data.update({
            "name": _clean_text(name),
            "brand": _clean_text(brand),
            "quantity": _clean_text(qty),
            "form": _clean_text(form),
            "ingredients": _clean_text(ingred),
            "category": "cosmetico" if base == OBF_API else "",
        })
        sources.append(url)

        # Se abbiamo nome+brand, ci basta
        if data.get("name") and data.get("brand"):
            break

    return data, sources

def fetch_from_html(ean: str, limit_pages=3):
    """Fallback HTML: prova alcune ricerche e scrapa i primi titoli utili."""
    data = {}
    sources = []
    for u in CANDIDATE_URLS:
        url = u.format(ean=ean)
        r = _http_get(url)
        if not r:
            continue
        txt = r.text
        if str(ean) not in txt:
            continue

        # prova a prendere un titolo plausibile di prodotto
        m = re.search(r"<h1[^>]*>([^<]{10,200})</h1>", txt, re.I|re.S)
        title = _clean_text(m.group(1)) if m else ""
        if not title:
            # fallback su <title>
            m2 = re.search(r"<title>([^<]{10,200})</title>", txt, re.I|re.S)
            title = _clean_text(m2.group(1)) if m2 else ""

        if title:
            data.setdefault("name", title)
            data.setdefault("quantity", _pick_qty(title))
            data.setdefault("form", _pick_form(title))

        # brand grezzo (da breadcrumb/meta)
        m3 = re.search(r'(?:Brand|Marca)\s*[:\-]\s*([A-Za-z0-9 \-\’\']{2,40})', txt, re.I)
        if m3:
            data.setdefault("brand", _clean_text(m3.group(1)))

        # ingredienti
        m4 = re.search(r"(?:INGREDIENTI|INCI|Ingredients)\s*[:\-]?\s*</?\w*>\s*([^<]{10,800})", txt, re.I)
        if m4:
            data.setdefault("ingredients", _clean_text(m4.group(1)))

        sources.append(url)
        if len(sources) >= limit_pages:
            break
    return data, sources

def fetch_product_seed_by_ean(ean: str):
    combined = {}
    srcs = []

    d1, s1 = fetch_from_open_dbs(ean)
    if d1:
        combined.update({k:v for k,v in d1.items() if v})
        srcs.extend(s1)

    # se mancano ancora nome/brand, prova HTML
    if not (combined.get("name") and combined.get("brand")):
        d2, s2 = fetch_from_html(ean)
        if d2:
            # non sovrascrivere campi già buoni
            for k, v in d2.items():
                combined.setdefault(k, v)
            srcs.extend(s2)

    # normalizzazioni
    if "name" in combined:
        combined["name"] = _dedupe_words(combined["name"])
    if not combined.get("category") and combined.get("form"):
        combined["category"] = "cosmetico" if combined["form"] in ["olio","crema","gel","spray","lozione","shampoo","balsamo"] else ""

    return combined, srcs

# ---------- LLM call ----------
def call_llm(ean: str, user_prompt: str, tone: str, lang: str, include_bullets: bool, include_meta: bool):
    sys_prompt = build_system_prompt()
    prompt = PROMPT_TEMPLATE.format(
        ean=ean.strip(),
        user_prompt=(user_prompt or "").strip(),
        tone=tone,
        lang=lang,
        bullets="• 5-8 punti" if include_bullets else "(omessi)",
        meta="7) Meta description (max 160 caratteri)." if include_meta else "",
        base_fields=BASE_FIELDS,
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
        )
        try:
            return resp.choices[0].message.content
        except Exception:
            return resp.choices[0].message["content"]
    except Exception as e:
        st.error(f"Errore chiamando il provider ({PROVIDER}): {e}")
        return ""

# ---------- UI ----------
st.title("🧪 " + APP_TITLE)
st.write("Inserisci un codice/EAN e (facoltativo) un prompt guida. L'app può cercare dati in internet a partire dall'EAN.")

with st.form("desc_form"):
    ean = st.text_input("Codice/EAN", placeholder="Es. 4012345678901", max_chars=32)
    tone = st.selectbox("Tono", ["Neutro", "Clinico", "Marketing", "SEO"], index=0)
    lang = st.selectbox("Lingua output", ["Italiano", "Tedesco", "Francese", "Inglese"], index=0)
    enrich_web = st.checkbox("Arricchisci da web (EAN lookup)", value=True)
    user_prompt = st.text_area(
        "Prompt guida (facoltativo)",
        placeholder=(
            "Es.: Descrivi ingredienti, forma, indicazioni d'uso, posologia, avvertenze, "
            "benefici, contenuto confezione. Bullet point e abstract di 160 caratteri."
        ),
        height=120,
    )
    col1, col2 = st.columns([1, 1])
    with col1:
        include_bullets = st.checkbox("Bullet points", value=True)
    with col2:
        include_meta = st.checkbox("Meta/SEO snippet", value=True)

    submitted = st.form_submit_button("Genera descrizione")

if submitted:
    if not ean or not valid_ean(ean):
        st.warning("Inserisci un EAN/GTIN valido (8/12/13/14 cifre).")
        st.stop()

    seed, sources = ({}, [])
    if enrich_web:
        with st.spinner("Cerco dati pubblici dal codice EAN..."):
            seed, sources = fetch_product_seed_by_ean(ean)

    # costruisci prompt effettivo
    auto_prompt = ""
    if seed:
        parts = []
        if seed.get("category"): parts.append(f"Categoria: {seed['category']}.")
        if seed.get("brand"):    parts.append(f"Marca: {seed['brand']}.")
        if seed.get("name"):     parts.append(f"Nome: {seed['name']}.")
        if seed.get("form"):     parts.append(f"Forma: {seed['form']}.")
        if seed.get("quantity"): parts.append(f"Quantità: {seed['quantity']}.")
        if seed.get("ingredients"):
            # accorcia ingredienti lunghissimi
            ingred = seed["ingredients"]
            if len(ingred) > 800:
                ingred = ingred[:800] + "..."
            parts.append(f"Ingredienti (parziali): {ingred}")
        auto_prompt = " ".join(parts)

    effective_user_prompt = (user_prompt or auto_prompt).strip()

    if not effective_user_prompt:
        st.info("Non ho trovato informazioni online per questo EAN. "
                "La risposta mostrerà solo 'Dati mancanti' (nessun segnaposto).")

    result = call_llm(ean, effective_user_prompt, tone, lang, include_bullets, include_meta)

    st.subheader("Risultato")
    if result:
        st.write(result)
        st.download_button(
            label="Scarica come .txt",
            file_name=f"descrizione_{re.sub(r'[^0-9A-Za-z_-]', '_', ean)}.txt",
            mime="text/plain",
            data=result,
        )
    else:
        st.info("Nessun testo generato.")

    if sources:
        st.caption("Fonti trovate:")
        for u in sources:
            st.caption(f"• {u}")

st.divider()
if APP_FOOTER:
    st.caption(APP_FOOTER)
