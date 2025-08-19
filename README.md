# Medipim Image Downloader

Un'applicazione Streamlit per scaricare automaticamente immagini di prodotti dalla piattaforma Medipim in formato 1500x1500 e comprimerle in un file ZIP.

## Caratteristiche

- **Caricamento lista prodotti**: Carica un file CSV o inserisci manualmente gli ID prodotto
- **Login automatico**: Utilizza le credenziali Medipim per accedere alla piattaforma
- **Download automatico**: Cerca e scarica automaticamente le immagini 1500x1500
- **Compressione ZIP**: Crea un file ZIP con tutte le immagini scaricate
- **Monitoraggio progresso**: Barra di progresso e stato in tempo reale
- **Gestione errori**: Rapporto dettagliato dei download riusciti e falliti

## Installazione

1. Assicurati di avere Python 3.7+ installato
2. Installa le dipendenze:
   ```bash
   pip install -r requirements.txt
   ```

## Utilizzo

1. Avvia l'applicazione:
   ```bash
   streamlit run app.py
   ```

2. Inserisci le tue credenziali Medipim nella barra laterale

3. Carica la lista di ID prodotto:
   - **Opzione 1**: Carica un file CSV con una colonna contenente gli ID prodotto
   - **Opzione 2**: Inserisci manualmente gli ID prodotto (uno per riga)

4. Clicca su "🚀 Avvia Download" per iniziare il processo

5. Attendi il completamento e scarica il file ZIP con le immagini

## Formato file CSV

Il file CSV deve contenere almeno una colonna con gli ID prodotto. Esempio:

```csv
product_id
4811337
4811338
4811339
```

Se il file ha più colonne, l'applicazione ti permetterà di selezionare quale colonna contiene gli ID prodotto.

## Credenziali

L'applicazione richiede credenziali valide per accedere alla piattaforma Medipim:
- **Username**: Il tuo indirizzo email Medipim
- **Password**: La tua password Medipim

## Risoluzione problemi

### Login fallito
- Verifica che username e password siano corretti
- Assicurati che l'account abbia accesso alla piattaforma Medipim

### Prodotto non trovato
- Verifica che l'ID prodotto sia corretto
- Alcuni prodotti potrebbero non essere disponibili o non avere immagini

### Immagine non trovata
- Il prodotto esiste ma non ha immagini disponibili in formato 1500x1500
- Controlla manualmente sulla piattaforma Medipim

## Struttura del progetto

```
streamlit_medipim_app/
├── app.py              # Applicazione Streamlit principale
├── medipim_api.py      # Modulo per interagire con l'API Medipim
├── requirements.txt    # Dipendenze Python
├── test_products.csv   # File CSV di esempio
└── README.md          # Questo file
```

## Dipendenze

- `streamlit`: Framework per l'interfaccia web
- `requests`: Per le richieste HTTP
- `beautifulsoup4`: Per il parsing HTML
- `pandas`: Per la gestione dei file CSV

## Note tecniche

- L'applicazione utilizza web scraping per interagire con la piattaforma Medipim
- Le immagini vengono scaricate temporaneamente e poi compresse in un file ZIP
- I file temporanei vengono automaticamente eliminati dopo la creazione del ZIP
- L'applicazione gestisce automaticamente il login e mantiene la sessione attiva

## Limitazioni

- Dipende dalla struttura HTML della piattaforma Medipim
- Richiede credenziali valide per funzionare
- La velocità di download dipende dalla connessione internet e dalla risposta del server Medipim

