# Immobilier Monitor

Monitora gli annunci di immobilier.ch per 1207 Genève, filtra le strade configurate e i canoni base inferiori a CHF 2'400, salva i nuovi annunci su Google Sheets e invia una notifica Telegram.

## File da caricare nel repository

- `immobilier_monitor.py`
- `requirements.txt`
- `.gitignore`
- `.github/workflows/immobilier.yml`

Il file scaricato `immobilier.yml` deve quindi essere caricato nel percorso `.github/workflows/immobilier.yml`.

## Secrets GitHub

In `Settings > Secrets and variables > Actions`, creare:

- `GOOGLE_SERVICE_ACCOUNT_JSON`: contenuto completo del JSON del Service Account Google.
- `TELEGRAM_BOT_TOKEN`: token ottenuto da BotFather.
- `TELEGRAM_CHAT_ID`: identificativo della chat Telegram.

## Google Sheets

Il foglio configurato nello script deve essere condiviso come Editor con il valore `client_email` del Service Account.

API da abilitare nel progetto Google Cloud:

- Google Sheets API
- Google Drive API

## Test manuale

Aprire `Actions > Immobilier Monitor > Run workflow`.

## Pianificazione

Il workflow usa:

```yaml
cron: "15 * * * *"
```

quindi viene pianificato al minuto 15 di ogni ora, secondo il fuso orario UTC usato dai workflow pianificati.

## Comportamento

- La deduplicazione usa `listing_code`.
- Una nuova riga viene salvata come `PENDING`.
- Dopo un invio Telegram riuscito diventa `SENT`.
- Se Telegram fallisce, resta `PENDING` e sarà riprovata alla prossima esecuzione.
- Se la prima pagina non contiene card, lo script termina senza modificare il foglio.
