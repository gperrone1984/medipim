import streamlit as st
import pandas as pd
import zipfile
import os
import tempfile
from medipim_api import MedipimAPI
import io

st.set_page_config(
    page_title="Medipim Image Downloader",
    page_icon="📸",
    layout="wide"
)

st.title("📸 Medipim Image Downloader")
st.markdown("Carica una lista di ID prodotto e scarica le immagini 1500x1500 in un file ZIP")

# Sidebar for credentials
st.sidebar.header("Credenziali Medipim")
username = st.sidebar.text_input("Username", value="Donique.May@redcare-pharmacy.com")
password = st.sidebar.text_input("Password", type="password", value="q0gyjs5rbmq")

# Main content
col1, col2 = st.columns([1, 1])

with col1:
    st.header("📋 Carica Lista Prodotti")
    
    # Option 1: Upload CSV file
    uploaded_file = st.file_uploader("Carica file CSV con ID prodotti", type=['csv'])
    
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        st.write("Anteprima del file:")
        st.dataframe(df.head())
        
        # Let user select which column contains product IDs
        if len(df.columns) > 1:
            id_column = st.selectbox("Seleziona la colonna con gli ID prodotto:", df.columns)
            product_ids = df[id_column].tolist()
        else:
            product_ids = df.iloc[:, 0].tolist()
    else:
        # Option 2: Manual input
        st.markdown("**Oppure inserisci manualmente gli ID prodotto:**")
        manual_input = st.text_area(
            "ID Prodotti (uno per riga)",
            placeholder="4811337\n4811338\n4811339",
            height=150
        )
        
        if manual_input:
            product_ids = [id.strip() for id in manual_input.split('\n') if id.strip()]
        else:
            product_ids = []

with col2:
    st.header("⚙️ Configurazione Download")
    
    if product_ids:
        st.success(f"Trovati {len(product_ids)} ID prodotto")
        st.write("ID prodotti da processare:")
        for i, pid in enumerate(product_ids[:10]):  # Show first 10
            st.write(f"• {pid}")
        if len(product_ids) > 10:
            st.write(f"... e altri {len(product_ids) - 10} prodotti")
    
    # Download button
    if st.button("🚀 Avvia Download", disabled=not product_ids or not username or not password):
        if not username or not password:
            st.error("Inserisci username e password")
        elif not product_ids:
            st.error("Inserisci almeno un ID prodotto")
        else:
            # Initialize progress tracking
            progress_bar = st.progress(0)
            status_text = st.empty()
            results_container = st.container()
            
            # Initialize API
            api = MedipimAPI(username, password)
            
            # Login
            status_text.text("Effettuando login...")
            if not api.login():
                st.error("Errore durante il login. Le credenziali potrebbero essere corrette ma la sessione non è stata autenticata. Riprova e, se persiste, aggiorna l'app. ")
                st.stop()
            
            status_text.text("Login effettuato con successo!")
            
            # Create temporary directory for images
            temp_dir = tempfile.mkdtemp()
            downloaded_images = []
            failed_downloads = []
            
            # Process each product ID
            for i, product_id in enumerate(product_ids):
                progress = (i + 1) / len(product_ids)
                progress_bar.progress(progress)
                status_text.text(f"Processando prodotto {product_id} ({i+1}/{len(product_ids)})")
                
                try:
                    # Search for product
                    product_url = api.search_product(product_id)
                    if not product_url:
                        failed_downloads.append(f"{product_id}: Prodotto non trovato")
                        continue
                    
                    # Get image URL
                    image_url = api.get_image_url(product_url)
                    if not image_url:
                        failed_downloads.append(f"{product_id}: Immagine non trovata")
                        continue
                    
                    # Download image
                    image_path = os.path.join(temp_dir, f"{product_id}.jpg")
                    if api.download_image(image_url, image_path):
                        downloaded_images.append((product_id, image_path))
                    else:
                        failed_downloads.append(f"{product_id}: Errore durante il download")
                        
                except Exception as e:
                    failed_downloads.append(f"{product_id}: {str(e)}")
            
            # Create ZIP file
            if downloaded_images:
                status_text.text("Creando file ZIP...")
                zip_buffer = io.BytesIO()
                
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for product_id, image_path in downloaded_images:
                        zip_file.write(image_path, f"{product_id}.jpg")
                
                zip_buffer.seek(0)
                
                # Show results
                with results_container:
                    st.success(f"✅ Download completato! {len(downloaded_images)} immagini scaricate")
                    
                    if failed_downloads:
                        st.warning(f"⚠️ {len(failed_downloads)} download falliti:")
                        for failure in failed_downloads:
                            st.write(f"• {failure}")
                    
                    # Download button for ZIP
                    st.download_button(
                        label="📥 Scarica ZIP con immagini",
                        data=zip_buffer.getvalue(),
                        file_name=f"medipim_images_{len(downloaded_images)}_products.zip",
                        mime="application/zip"
                    )
                
                # Cleanup
                for _, image_path in downloaded_images:
                    try:
                        os.remove(image_path)
                    except:
                        pass
                os.rmdir(temp_dir)
                
                status_text.text("✅ Processo completato!")
            else:
                st.error("❌ Nessuna immagine è stata scaricata con successo")
                if failed_downloads:
                    st.write("Errori:")
                    for failure in failed_downloads:
                        st.write(f"• {failure}")

# Footer
st.markdown("---")
st.markdown("**Nota:** Assicurati di avere le credenziali corrette per accedere alla piattaforma Medipim.")
