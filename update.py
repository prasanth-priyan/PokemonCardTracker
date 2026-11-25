import os
import re
import time
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# --- CONFIGURATION ---
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1HkO6-Nw5ifPlge7SSZf_UtCmuNxwzsgbq2kD4wZ_-MI/edit"
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
START_ROW = 8  # Row where card data starts in the 'Collection' tab

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}


def get_tcgdex_data(set_id: str, lang: str, number: str) -> dict:
    """Fetch localized card name, English card name, and set name from TCGdex."""
    lang = lang.strip().lower()
    set_id = set_id.strip().lower()
    number = str(number).strip()

    # 1. Localized card data
    loc_url = f"https://api.tcgdex.net/v2/{lang}/sets/{set_id}/{number}"
    loc_resp = requests.get(loc_url, timeout=15)
    localized_name = ""
    en_name = ""
    set_name = ""

    if loc_resp.status_code == 200:
        data = loc_resp.json()
        localized_name = data.get("name", "")
        set_name = data.get("set", {}).get("name", "")

    # 2. Fetch English name if card is non-English (PriceCharting indexes primarily in English)
    if lang != "en":
        en_url = f"https://api.tcgdex.net/v2/en/sets/{set_id}/{number}"
        en_resp = requests.get(en_url, timeout=15)
        if en_resp.status_code == 200:
            en_data = en_resp.json()
            en_name = en_data.get("name", "")
            set_name = en_data.get("set", {}).get("name", set_name)
    else:
        en_name = localized_name

    return {
        "localized_name": localized_name,
        "en_name": en_name or localized_name,
        "set_name": set_name,
    }


def scrape_pricecharting(card_name: str, set_name: str, number: str, variant: str) -> tuple[str, float | None]:
    """Search PriceCharting and extract the ungraded market price."""
    if not card_name:
        return "Not Found", None

    # Build search query (e.g., "Charizard ex 199 Pokemon 151 Reverse Holo")
    variant_clean = "" if variant.strip().lower() in ("normal", "regular", "") else variant.strip()
    query_parts = [card_name, str(number), "Pokemon", set_name, variant_clean]
    query = " ".join(p for p in query_parts if p)

    search_url = f"https://www.pricecharting.com/search-products?type=prices&q={urllib.parse.quote_plus(query)}"
    resp = requests.get(search_url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}", None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Case A: Redirected directly to a single product page
    price_el = soup.select_one("#used_price .price")
    title_el = soup.select_one("#product_name")
    if price_el and title_el:
        pc_name = title_el.get_text(strip=True)
        price_val = parse_price(price_el.get_text(strip=True))
        return pc_name, price_val

    # Case B: Search results table (#games_table)
    first_row = soup.select_one("#games_table tbody tr")
    if first_row:
        title_link = first_row.select_one("td.title a")
        used_price_cell = first_row.select_one("td.used_price .price")
        if title_link and used_price_cell:
            pc_name = title_link.get_text(strip=True)
            price_val = parse_price(used_price_cell.get_text(strip=True))
            return pc_name, price_val

    return "No Match Found", None


def parse_price(price_str: str) -> float | None:
    """Convert '$124.50' to 124.50."""
    match = re.search(r"[\d,]+\.\d{2}", price_str)
    if match:
        return float(match.group(0).replace(",", ""))
    return None


def main():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)

    sheet = client.open_by_url(SPREADSHEET_URL).worksheet("Collection")
    all_rows = sheet.get_all_values()

    if len(all_rows) < START_ROW:
        print("No card rows found.")
        return

    updates = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for idx, row in enumerate(all_rows[START_ROW - 1 :], start=START_ROW):
        # Pad row to ensure at least 8 columns
        row += [""] * (8 - len(row))
        set_id, lang, number, variant = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()

        if not set_id or not number:
            continue

        lang = lang or "en"
        print(f"Row {idx}: Processing {set_id} #{number} ({lang}, {variant or 'Normal'})...")

        tcg_info = get_tcgdex_data(set_id, lang, number)
        pc_name, market_price = scrape_pricecharting(
            tcg_info["en_name"], tcg_info["set_name"], number, variant
        )

        price_cell = market_price if market_price is not None else ""
        updates.append({
            "range": f"E{idx}:H{idx}",
            "values": [[pc_name, tcg_info["localized_name"], price_cell, timestamp]],
        })

        # Polite delay to avoid PriceCharting rate limits
        time.sleep(2.0)

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
        print(f"Successfully updated {len(updates)} rows.")


if __name__ == "__main__":
    main()
