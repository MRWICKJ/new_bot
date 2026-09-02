import glob
import os
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_manager import MASTER_DB_PATH, init_db, save_to_master_sqlite
import pandas as pd
from parsor import parse_tofler_html, parse_zauba_html
import requests

MAX_WORKERS = 4
print_lock = threading.Lock()
db_lock = threading.Lock()

ZAUBA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101"
        " Firefox/155.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

ZAUBA_COOKIES = {
    "cf_clearance": (
        "vcjt_sU0zH4DLRnwHtaJcPVqH.WjgQE.I.AT6QcmINU-1788257591-1.2.1.1"
    ),
    "ZCSESSID": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1dWlkIjoiIn0",
}

TOFLER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101"
        " Firefox/156.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tofler.in/",
    "Connection": "keep-alive",
}

TOFLER_COOKIES = {
    "ToflerSession": "184216cf8e739cc4f8ae5b1d5ff1aa02",
    "G_ENABLED_IDPS": "google",
}

def log(msg):
    with print_lock:
        print(msg, flush=True)

def fetch_tofler_by_cin(cin: str):
    url = f"https://www.tofler.in/company/{cin}"
    try:
        response = requests.get(
            url, headers=TOFLER_HEADERS, cookies=TOFLER_COOKIES, timeout=(5, 8)
        )
        if response.status_code == 200:
            return response.text, url
        else:
            log(f"  [Tofler HTTP {response.status_code}] CIN: {cin}")
    except Exception as err:
        log(f"  [Tofler Timeout/Error] CIN: {cin} | {err}")
    return None, url

def process_company_record(row, district, sub_district, pincode, office_name):
    cin = None
    zauba_url = None

    for col_name, val in row.items():
        col_clean = str(col_name).strip().lower()
        if pd.notna(val):
            val_str = str(val).strip()
            if not cin and ("cin" in col_clean or "llpin" in col_clean):
                cin = val_str
            elif not zauba_url and (
                "url" in col_clean or "link" in col_clean or "zauba" in col_clean
            ):
                if val_str.startswith("http"):
                    zauba_url = val_str

    if not cin:
        return

    with db_lock:
        conn = sqlite3.connect(MASTER_DB_PATH, timeout=60.0)
        cursor = conn.cursor()
        cursor.execute("SELECT cin FROM companies WHERE cin = ?", (cin,))
        exists = cursor.fetchone()
        conn.close()

    if exists:
        return

    log(f"Processing CIN: {cin} ...")

    # 1. Fetch & Parse Zauba Corp
    zauba_payload = None
    if zauba_url:
        try:
            res = requests.get(
                zauba_url, headers=ZAUBA_HEADERS, cookies=ZAUBA_COOKIES, timeout=(5, 8)
            )
            if res.status_code == 200:
                master, directors, charges = parse_zauba_html(res.text, cin=cin)
                if master and master.get("Company Name"):
                    zauba_payload = {
                        "cin": cin,
                        "company_name": master.get("Company Name"),
                        "pincode": pincode,
                        "post_office_name": office_name,
                        "district": district,
                        "sub_district": sub_district,
                        "basic_info": master,
                        "directors": directors,
                        "charges": charges,
                        "source_url": zauba_url,
                    }
                    log(f"  ✓ Parsed Zauba: {cin}")
        except Exception as e:
            log(f"  [Zauba Error] CIN: {cin} | {e}")

    # 2. Fetch & Parse Tofler
    tofler_payload = None
    tofler_html, tofler_url = fetch_tofler_by_cin(cin)
    if tofler_html:
        tofler_master, tofler_directors, tofler_charges = parse_tofler_html(
            tofler_html, cin=cin
        )
        tofler_payload = {
            "cin": cin,
            "company_name": tofler_master.get("Company Name") or "Unknown",
            "pincode": pincode,
            "post_office_name": office_name,
            "district": district,
            "sub_district": sub_district,
            "basic_info": tofler_master,
            "directors": tofler_directors,
            "charges": tofler_charges,
            "source_url": tofler_url,
        }
        log(f"  ✓ Parsed Tofler: {cin}")

    # 3. Save Structured Data ONLY
    with db_lock:
        if zauba_payload:
            save_to_master_sqlite(zauba_payload)
        if tofler_payload:
            save_to_master_sqlite(tofler_payload)

        conn = sqlite3.connect(MASTER_DB_PATH, timeout=60.0)

        status_source = (
            "both"
            if (zauba_payload and tofler_payload)
            else ("zaubacorp" if zauba_payload else ("tofler" if tofler_payload else "failed"))
        )
        conn.execute(
            "INSERT OR REPLACE INTO scrape_logs (cin, primary_source, status) VALUES (?, ?, ?)",
            (cin, status_source, "SUCCESS" if status_source != "failed" else "FAILED"),
        )

        conn.commit()
        conn.close()

def batch_process_all_files():
    init_db(MASTER_DB_PATH)
    csv_files = glob.glob(os.path.join("output", "**", "*.csv"), recursive=True)
    total_files = len(csv_files)
    print(
        f"Discovered {total_files} CSV files. Processing data into: {MASTER_DB_PATH.resolve()}",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for index, csv_file in enumerate(csv_files, 1):
            parts = Path(csv_file).parts
            district = parts[-3] if len(parts) >= 3 else "Unknown"
            sub_district = parts[-2] if len(parts) >= 3 else "Unknown"
            office_name = Path(csv_file).stem

            try:
                df = pd.read_csv(csv_file)
                if df.empty:
                    continue

                log(
                    f"\n--- Processing File [{index}/{total_files}]: {csv_file}"
                    f" ({len(df)} records) ---"
                )
                futures = []

                for _, row in df.iterrows():
                    pincode = str(row.get("pincode", "000000"))
                    futures.append(
                        executor.submit(
                            process_company_record,
                            row,
                            district,
                            sub_district,
                            pincode,
                            office_name,
                        )
                    )

                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as err:
                        log(f"Worker task error: {err}")

            except Exception as e:
                log(f"Error reading CSV {csv_file}: {e}")

if __name__ == "__main__":
    batch_process_all_files()