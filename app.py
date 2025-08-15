import os
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

# ---------- UI ----------
st.title("🧪 " + APP_TITLE)
st.write("Inserisci un codice prodotto / EAN e (opzionale) un prompt guida. Il modello genererà una descrizione completa e coerente.")

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
    col1, col2 = st.columns([1,1])
    with col1:
        include_bullets = st.checkbox("Bullet points", value=True)
    with col2:
        include_meta = st.checkbox("Meta/SEO snippet", value=True)

    submitted = st.form_submit_button("Genera descrizione")

# ---------- Prompt template ----------
def build_system_prompt():
    return (
        "Sei un assistente per la redazione di schede prodotto farmaceutiche/parafarmaceutiche. "
        "Rispetta le normative, evita claim medici non supportati, usa un tono professionale. "
        "Se i dati sono insufficienti, chiedi esplicitamente maggiori informazioni e NON inventare caratteristiche."
    )

BASE_FIELDS = (
    "EAN, nome commerciale, marca, forma, quantità, ingredienti/Principi attivi, indicazioni d'uso, posologia, avvertenze, "+
    "modalità di conservazione, contenuto confezione, produttore, paese di origine"
)

PROMPT_TEMPLATE = """
Obiettivo: scrivi una descrizione completa e accurata del prodotto per l'e-commerce.

Requisiti:
- Lingua: {lang}
- Tono: {tone}
- Non inventare dati. Se mancano, segnala con una sezione "Dati mancanti".
- Struttura:
  1) Abstract (1-2 frasi).
  2) Descrizione estesa.
  3) Caratteristiche principali ({bullets}).
  4) Modalità d'uso e avvertenze (se applicabili).
  5) Contenuto della confezione.
  6) Specifiche tecniche (se rilevanti).
  {meta}

Dati disponibili:
- Codice/EAN: {ean}
- Altri dettagli forniti dall'utente: "{user_prompt}"
- Campi attesi (se disponibili): {base_fields}

Nota: se il prodotto è un farmaco da banco, mantieni un tono informativo e conforme. Se è cosmetico/integrazione, evita claim medici.
""".strip()

# ---------- Call OpenAI (Responses API) ----------
def call_openai(ean: str, user_prompt: str, tone: str, lang: str):
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

    # Streaming token-by-token
    with st.status("Chiedo al modello..."):
        stream = client.responses.stream(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        stream.until_done()
        full = stream.get_final_response()
        # Il testo generato può trovarsi in diversi punti a seconda della versione SDK
        text = ""
        try:
            # SDK >= 1.0 Responses API
            text = full.output_text
        except Exception:
            # Fallback generico
            text = str(full)
        return text

# ---------- Run ----------
if submitted:
    if not ean:
        st.warning("Inserisci un codice/EAN prima di procedere.")
        st.stop()

    result = call_openai(ean, user_prompt, tone, lang)

    st.subheader("Risultato")
    st.write(result)

    # Download
    st.download_button(
        label="Scarica come .txt",
        file_name=f"descrizione_{ean}.txt",
        mime="text/plain",
        data=result or "",
    )

st.divider()
if APP_FOOTER:
    st.caption(APP_FOOTER)
