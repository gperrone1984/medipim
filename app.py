import os
import re
import streamlit as st
from openai import OpenAI

# ---------- Config base ----------
st.set_page_config(page_title="PDM • Product Description Builder", page_icon="🧪", layout="centered")

APP_TITLE = st.secrets.get("APP_TITLE", "PDM • Product Description Builder")
APP_FOOTER = st.secrets.get("APP_FOOTER", "")
MODEL = st.secrets.get("OPENAI_MODEL", "gpt-4o-mini")

# API key
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    st.error("Missing OPENAI_API_KEY. Add it to .streamlit/secrets.toml or env vars.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Helpers ----------
EAN_REGEX = re.compile(r"^[0-9]{8}$|^[0-9]{12,14}$")  # EAN8/UPC/EAN13/GTIN


def valid_ean(code: str) -> bool:
    if not code:
        return False
    c = re.sub(r"[^0-9]", "", code)
    return bool(EAN_REGEX.match(c))


# ---------- Prompting ----------
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

PROMPT_TEMPLATE = """
Obiettivo: scrivi una descrizione completa e accurata del prodotto per l'e-commerce.

Requisiti:
- Lingua: {lang}
- Tono: {tone}
- Non inventare dati. Se mancano, aggiungi una sezione "Dati mancanti".
- Struttura:
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

Nota: se il prodotto è un farmaco da banco, mantieni un tono informativo e conforme; per cosmetici/integratori evita claim medici.
""".strip()


def call_openai(ean: str, user_prompt: str, tone: str, lang: str, include_bullets: bool, include_meta: bool):
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
        # Endpoint compatibile e semplice da debuggare
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
        )
        # SDK v1.x restituisce oggetti con attributi; compatibilità dict[]
        try:
            return resp.choices[0].message.content
        except Exception:
            return resp.choices[0].message["content"]
    except Exception as e:
        st.error(f"Errore chiamando OpenAI: {e}")
        return ""


# ---------- UI ----------
st.title("🧪 " + APP_TITLE)
st.write("Inserisci un codice/EAN e (facoltativo) un prompt guida. Il modello genererà una descrizione completa.")

with st.form("desc_form"):
    ean = st.text_input("Codice/EAN", placeholder="Es. 4012345678901", max_chars=32)
    tone = st.selectbox("Tono", ["Neutro", "Clinico", "Marketing", "SEO"], index=0)
    lang = st.selectbox("Lingua output", ["Italiano", "Tedesco", "Francese", "Inglese"], index=0)
    user_prompt = st.text_area(
        "Prompt guida (facoltativo)",
        placeholder=(
            "Es.: Descrivi ingredienti, forma farmaceutica, indicazioni d'uso, posologia, avvertenze, "
            "benefici, contenuto confezione. Inserisci bullet point e un abstract di 160 caratteri."
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

    result = call_openai(ean, user_prompt, tone, lang, include_bullets, include_meta)

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

st.divider()
if APP_FOOTER:
    st.caption(APP_FOOTER)
