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

LANG_MAP = {
    "english": "en",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh-tw",
    # Fallback support if raw codes are ever entered
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "zh-tw": "zh-tw",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}


def get_tcgdex_data(set_id: str, lang_label: str, number: str) -> dict:
    """Fetch localized card name, English card name, and set name from TCGdex."""
    lang_code = LANG_MAP.get(lang_label.strip().lower(), "en")
    set_id = set_id.strip().lower()
    number = str(number).strip()

    # 1. Localized card data
    loc_url = f"https://api.tcgdex.net/v2/{lang_code}/sets/{set_id}/{number}"
    loc_resp = requests.get(loc_url, timeout=15)
    localized_name = ""
    en_name = ""
    set_name = ""

    if loc_resp.status_code == 200:
        data = loc_resp.json()
        localized_name = data.get("name", "")
        set_name = data.get("set", {}).get("name", "")

    # 2. Fetch English name if card is non-English (PriceCharting indexes primarily in English)
    if lang_code != "en":
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
        "lang_label": lang_label.strip().title(),
    }


def parse_price(price_str: str) -> float | None:
    """Convert '$124.50' to 124.50."""
    if not price_str:
        return None
    match = re.search(r"[\d,]+\.\d{2}", price_str)
    if match:
        return float(match.group(0).replace(",", ""))
    return None


def extract_prices_from_product_page(soup: BeautifulSoup) -> tuple[str, float | None, float | None, float | None]:
    """Extract Title, Ungraded (#used_price), PSA 9 (#graded_price), and PSA 10 (#manual_only_price)."""
    title_el = soup.select_one("#product_name")
    pc_name = title_el.get_text(strip=True) if title_el else "Unknown"

    ungraded_el = soup.select_one("#used_price .price")
    psa9_el = soup.select_one("#graded_price .price")
    psa10_el = soup.select_one("#manual_only_price .price")

    ungraded = parse_price(ungraded_el.get_text(strip=True)) if ungraded_el else None
    psa9 = parse_price(psa9_el.get_text(strip=True)) if psa9_el else None
    psa10 = parse_price(psa10_el.get_text(strip=True)) if psa10_el else None

    return pc_name, ungraded, psa9, psa10


def scrape_pricecharting(
    card_name: str, set_name: str, number: str, variant: str, lang_label: str
) -> tuple[str, float | None, float | None, float | None]:
    """Search PriceCharting and extract Ungraded, PSA 9, and PSA 10 prices."""
    if not card_name:
        return "Not Found", None, None, None

    variant_clean = "" if variant.strip().lower() in ("normal", "regular", "") else variant.strip()
    lang_hint = "" if lang_label.lower() == "english" else lang_label

    query_parts = [card_name, str(number), "Pokemon", lang_hint, set_name, variant_clean]
    query = " ".join(p for p in query_parts if p)

    search_url = f"https://www.pricecharting.com/search-products?type=prices&q={urllib.parse.quote_plus(query)}"
    resp = requests.get(search_url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}", None, None, None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Case A: Redirected directly to the single card product page
    if soup.select_one("#product_name"):
        return extract_prices_from_product_page(soup)

    # Case B: Search results table (#games_table) -> follow the top match link to get PSA 9 & PSA 10
    first_link = soup.select_one("#games_table tbody tr td.title a")
    if first_link and first_link.get("href"):
        product_url = urllib.parse.urljoin("https://www.pricecharting.com", first_link["href"])
        time.sleep(1.0)
        prod_resp = requests.get(product_url, headers=HEADERS, timeout=15)
        if prod_resp.status_code == 200:
            prod_soup = BeautifulSoup(prod_resp.text, "html.parser")
            return extract_prices_from_product_page(prod_soup)

    return "No Match Found", None, None, None


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
        row += [""] * (10 - len(row))
        set_id, lang, number, variant = (
            row[0].strip(),
            row[1].strip(),
            row[2].strip(),
            row[3].strip(),
        )

        # Skip empty template rows where Set ID or Card Number hasn't been entered yet
        if not set_id or not number:
            continue

        lang = lang or "English"
        variant = variant or "Normal"
        print(f"Row {idx}: Processing {set_id} #{number} ({lang}, {variant})...")

        tcg_info = get_tcgdex_data(set_id, lang, number)
        pc_name, ungraded, psa9, psa10 = scrape_pricecharting(
            tcg_info["en_name"], tcg_info["set_name"], number, variant, lang
        )

        updates.append({
            "range": f"E{idx}:J{idx}",
            "values": [[
                pc_name,
                tcg_info["localized_name"],
                ungraded if ungraded is not None else "",
                psa9 if psa9 is not None else "",
                psa10 if psa10 is not None else "",
                timestamp,
            ]],
        })

        time.sleep(2.0)

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
        print(f"Successfully updated {len(updates)} rows.")


if __name__ == "__main__":
    main()
