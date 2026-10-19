import asyncio
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin

import gspread
import pandas as pd
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Secret/variabile mancante: {name}")
    return value


BASE_URL = "https://www.immobilier.ch"
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1n88ZGWqbUXK4H6fAC3nrc8onqq5a07Vbfb_D4cu58aQ/edit?gid=71426295#gid=71426295"
WORKSHEET_NAME = "Annunci"
MAX_PRICE_CHF = 2400
MAX_PAGES = 10
WAIT_AFTER_LOAD_MS = 5000

STREET_KEYWORDS = {
    "Rue du Lac": "LAC",
    "Rue Muzy": "MUZY",
    "Rue du 31-Décembre": "DECEMBRE",
    "Rue des Barques": "BARQUES",
    "Rue de la Scie": "SCIE",
    "Place Comoletti": "COMOLETTI",
    "Quai Gustave-Ador": "ADOR",
    "Rue des Pierres-du-Niton": "NITON",
    "Rue Cécile-Béler-Butticaz": "BUTTICAZ",
    "Rue du Simplon": "SIMPLON",
    "Ruelle des Marins": "MARINS",
    "Rue du Roveray": "ROVERAY",
    "Rue des Eaux-Vives": "EAUX VIVES",
}

SHEET_COLUMNS = [
    "listing_code",
    "matched_street",
    "matched_keyword",
    "address",
    "price",
    "monthly_price_chf",
    "charges_chf",
    "total_monthly_chf",
    "rooms",
    "property_type",
    "source_page",
    "position_on_page",
    "url",
    "notification_status",
    "inserted_at",
    "notification_sent_at",
]





def build_search_url(page_number):
    return (
        "https://www.immobilier.ch/fr/carte/louer/"
        "appartement-maison/geneve/geneve-1207/"
        f"page-{page_number}"
        "?t=rent&c=1%3B2&p=c10384&z=10384&nb=false&gr=1"
    )


def normalize_text(value):
    if value is None:
        return ""
    value = str(value).upper().strip()
    value = value.replace("’", "'").replace("`", "'")
    value = value.replace("–", "-").replace("—", "-").replace("‑", "-")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[-,;:']", " ", value)
    value = re.sub(r"[^A-Z0-9 ]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


NORMALIZED_KEYWORDS = {
    street: normalize_text(keyword) for street, keyword in STREET_KEYWORDS.items()
}


def find_matching_street(address):
    normalized = normalize_text(address)
    if not normalized:
        return None
    padded = f" {normalized} "
    for street, keyword in NORMALIZED_KEYWORDS.items():
        if f" {keyword} " in padded:
            return street
    return None


def find_matching_keyword(address):
    normalized = normalize_text(address)
    if not normalized:
        return None
    padded = f" {normalized} "
    for keyword in NORMALIZED_KEYWORDS.values():
        if f" {keyword} " in padded:
            return keyword
    return None


def extract_price_number(price_text):
    if not price_text:
        return None
    match = re.search(r"CHF\s*([\d'’\s]+)", price_text, flags=re.IGNORECASE)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    return int(digits) if digits else None


def extract_charges(price_text):
    if not price_text:
        return None
    match = re.search(r"\+\s*([\d'’\s]+).*?charges", price_text, flags=re.IGNORECASE)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    return int(digits) if digits else None


def extract_rooms(card):
    room_icon = card.select_one(".characteristic-list .icon-plan")
    if room_icon:
        match = re.search(r"(\d+(?:[.,]\d+)?)", room_icon.get("title", ""))
        if match:
            return float(match.group(1).replace(",", "."))
    for item in card.select(".filter-item-characteristic li"):
        match = re.search(r"\d+(?:[.,]\d+)?", item.get_text(" ", strip=True))
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def parse_cards(html, page_number):
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('a[id^="link-result-item-"]')
    records = []

    for position, card in enumerate(cards, start=1):
        listing_code = card.get("id", "").replace("link-result-item-", "").strip()
        if not listing_code:
            continue

        content = card.select_one("div.filter-item-content")
        if content is None:
            continue

        price_tag = content.select_one("strong.title")
        type_tag = content.select_one("p.object-type")
        address_tags = content.select("p:not(.object-type)")

        price_text = price_tag.get_text(" ", strip=True) if price_tag else None
        property_type = type_tag.get_text(" ", strip=True) if type_tag else None
        address = address_tags[-1].get_text(" ", strip=True) if address_tags else None
        monthly_price = extract_price_number(price_text)
        charges = extract_charges(price_text)
        total = monthly_price + charges if monthly_price is not None and charges is not None else monthly_price

        records.append({
            "listing_code": listing_code,
            "matched_street": find_matching_street(address),
            "matched_keyword": find_matching_keyword(address),
            "address": address,
            "price": price_text,
            "monthly_price_chf": monthly_price,
            "charges_chf": charges,
            "total_monthly_chf": total,
            "rooms": extract_rooms(card),
            "property_type": property_type,
            "source_page": page_number,
            "position_on_page": position,
            "url": urljoin(BASE_URL, card.get("href", "")),
        })
    return records


async def scrape_all_pages():
    all_records = []
    previous_page_ids = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="fr-CH",
        )
        page = await context.new_page()

        for page_number in range(1, MAX_PAGES + 1):
            search_url = build_search_url(page_number)
            print(f"Caricamento pagina {page_number}...")
            response = await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

            try:
                await page.wait_for_selector('a[id^="link-result-item-"]', timeout=30000)
            except Exception:
                html = await page.content()
                waf = "x-amzn-waf" in html.lower() or "challenge" in html.lower()
                if page_number == 1:
                    await browser.close()
                    raise RuntimeError(
                        "Nessuna card trovata sulla prima pagina. "
                        + ("Possibile challenge WAF." if waf else "Struttura della pagina cambiata o pagina non caricata.")
                    )
                print(f"Pagina {page_number} senza card: fine paginazione.")
                break

            await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
            html = await page.content()
            page_records = parse_cards(html, page_number)
            status = response.status if response else None
            print(f"Pagina {page_number}: HTTP {status}, {len(page_records)} card")

            if not page_records:
                if page_number == 1:
                    await browser.close()
                    raise RuntimeError("HTML ricevuto, ma nessuna card è stata interpretata.")
                break

            current_ids = {r["listing_code"] for r in page_records}
            if previous_page_ids is not None and current_ids == previous_page_ids:
                print("Pagina duplicata: fine paginazione.")
                break

            previous_page_ids = current_ids
            all_records.extend(page_records)

        await browser.close()

    return all_records

    

def google_client_from_secret():
    info = json.loads(require_env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    credentials = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(credentials)


def clean_value(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return value


def get_worksheet(client):
    spreadsheet = client.open_by_url(GOOGLE_SHEET_URL)
    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME, rows=2000, cols=len(SHEET_COLUMNS)
        )
    fix_or_create_header(worksheet)
    return worksheet


def fix_or_create_header(worksheet):
    all_values = worksheet.get_all_values()
    if not all_values:
        worksheet.append_row(SHEET_COLUMNS, value_input_option="RAW")
        print("Intestazione creata.")
        return

    first_row = [str(v).strip() for v in all_values[0]]
    if "listing_code" not in first_row:
        worksheet.insert_row(SHEET_COLUMNS, index=1, value_input_option="RAW")
        print("Intestazione inserita sopra i dati esistenti.")
        return

    updated = first_row.copy()
    for column in SHEET_COLUMNS:
        if column not in updated:
            updated.append(column)
    if updated != first_row:
        worksheet.update(range_name="A1", values=[updated])
        print("Intestazione aggiornata.")


def read_sheet_dataframe(worksheet):
    records = worksheet.get_all_records()
    return pd.DataFrame(records)


def append_new_rows(worksheet, df_new):
    if df_new.empty:
        return
    inserted_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for _, listing in df_new.iterrows():
        row_data = listing.to_dict()
        row_data.update({
            "notification_status": "PENDING",
            "inserted_at": inserted_at,
            "notification_sent_at": "",
        })
        rows.append([clean_value(row_data.get(column)) for column in SHEET_COLUMNS])
    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Inserite {len(rows)} nuove righe nel Google Sheet.")


def update_notification_status(worksheet, listing_code, status):
    values = worksheet.get_all_values()
    if not values:
        return
    header = [str(v).strip() for v in values[0]]
    code_col = header.index("listing_code") + 1
    status_col = header.index("notification_status") + 1
    sent_col = header.index("notification_sent_at") + 1

    for row_number, row in enumerate(values[1:], start=2):
        code = str(row[code_col - 1]).strip() if len(row) >= code_col else ""
        if code == str(listing_code).strip():
            sent_at = datetime.now(timezone.utc).isoformat() if status == "SENT" else ""
            worksheet.batch_update(
                [
                    {
                        "range": gspread.utils.rowcol_to_a1(row_number, status_col),
                        "values": [[status]],
                    },
                    {
                        "range": gspread.utils.rowcol_to_a1(row_number, sent_col),
                        "values": [[sent_at]],
                    },
                ],
                value_input_option="RAW",
            )
            return
    raise RuntimeError(f"Codice {listing_code} non trovato nel foglio.")


def telegram_message(listing):
    charges = clean_value(listing.get("charges_chf"))
    charges_line = f"\nSpese: CHF {charges}" if charges != "" else ""
    rooms = clean_value(listing.get("rooms"))
    rooms_line = f"\nLocali: {rooms:g}" if isinstance(rooms, (int, float)) else (f"\nLocali: {rooms}" if rooms != "" else "")
    return (
        "🏠 Nuovo annuncio immobiliare\n\n"
        f"📍 {clean_value(listing.get('address'))}\n"
        f"💰 {clean_value(listing.get('price'))}"
        f"{charges_line}"
        f"{rooms_line}\n"
        f"Tipo: {clean_value(listing.get('property_type'))}\n"
        f"Codice: {clean_value(listing.get('listing_code'))}\n\n"
        f"{clean_value(listing.get('url'))}"
    )


def send_telegram_message(text):
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Errore Telegram non specificato"))
    return payload


def pending_rows(worksheet):
    df = read_sheet_dataframe(worksheet)
    if df.empty or "listing_code" not in df.columns:
        return pd.DataFrame()
    if "notification_status" not in df.columns:
        df["notification_status"] = ""
    status = df["notification_status"].fillna("").astype(str).str.strip().str.upper()
    pending = df[status != "SENT"].copy()
    return pending.drop_duplicates(subset=["listing_code"], keep="first").reset_index(drop=True)


async def main():
    records = await scrape_all_pages()
    df_all = pd.DataFrame(records)
    if df_all.empty:
        raise RuntimeError("Nessun annuncio estratto: nessun aggiornamento eseguito.")

    df_all["listing_code"] = df_all["listing_code"].astype(str).str.strip()
    df_all = df_all.drop_duplicates(subset=["listing_code"], keep="first").reset_index(drop=True)

    df_filtered = df_all[
        df_all["matched_street"].notna()
        & df_all["monthly_price_chf"].notna()
        & (df_all["monthly_price_chf"] < MAX_PRICE_CHF)
    ].copy()
    df_filtered = df_filtered.sort_values(
        ["monthly_price_chf", "matched_street", "address"]
    ).reset_index(drop=True)

    print(f"Annunci unici estratti: {len(df_all)}")
    print(f"Annunci conformi ai filtri: {len(df_filtered)}")

    client = google_client_from_secret()
    worksheet = get_worksheet(client)
    df_existing = read_sheet_dataframe(worksheet)

    if df_existing.empty or "listing_code" not in df_existing.columns:
        existing_codes = set()
    else:
        existing_codes = set(
            df_existing["listing_code"].astype(str).str.strip().replace("", pd.NA).dropna()
        )

    df_new = df_filtered[~df_filtered["listing_code"].isin(existing_codes)].copy()
    append_new_rows(worksheet, df_new)
    print(f"Nuovi annunci: {len(df_new)}")

    pending = pending_rows(worksheet)
    print(f"Notifiche in attesa: {len(pending)}")

    sent = 0
    failed = 0
    for _, listing in pending.iterrows():
        code = str(listing.get("listing_code", "")).strip()
        try:
            send_telegram_message(telegram_message(listing))
            update_notification_status(worksheet, code, "SENT")
            sent += 1
            print(f"Telegram inviato per {code}")
            time.sleep(1)
        except Exception as exc:
            failed += 1
            print(f"Telegram fallito per {code}: {exc}")

    print("\nRiepilogo")
    print(f"Estratti: {len(df_all)}")
    print(f"Conformi: {len(df_filtered)}")
    print(f"Nuovi: {len(df_new)}")
    print(f"Telegram inviati: {sent}")
    print(f"Telegram falliti: {failed}")

    if failed:
        raise RuntimeError(f"{failed} notifiche Telegram non inviate; resteranno PENDING.")


if __name__ == "__main__":
    asyncio.run(main())
