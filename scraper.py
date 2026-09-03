import glob
import os
import re
import time
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_manager import MASTER_DB_PATH, init_db, save_to_master_sqlite
import pandas as pd
from parsor import parse_tofler_html, parse_zauba_html
import requests

BASE_DIR = Path(__file__).parent
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "16"))
FAILED_RETRY_DAYS = int(os.getenv("FAILED_RETRY_DAYS", "7"))
print_lock = threading.Lock()
db_lock = threading.Lock()
claim_lock = threading.Lock()
claimed_cins: set = set()
_thread_local = threading.local()

ZAUBA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101"
        " Firefox/155.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

ZAUBA_COOKIES = {
    "cf_clearance": os.getenv("ZAUBA_CF_CLEARANCE", "vcjt_sU0zH4DLRnwHtaJcPVqH.WjgQE.I.AT6QcmINU-1788257591-1.2.1.1"),
    "ZCSESSID": os.getenv("ZAUBA_ZCSESSID", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1dWlkIjoiIn0"),
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
    "ToflerSession": os.getenv("TOFLER_SESSION", "184216cf8e739cc4f8ae5b1d5ff1aa02"),
    "G_ENABLED_IDPS": "google",
}

if os.getenv("ZAUBA_CF_CLEARANCE") is None:
    print("WARNING: using hardcoded ZAUBA_CF_CLEARANCE; set ZAUBA_CF_CLEARANCE env var.", flush=True)
if os.getenv("TOFLER_SESSION") is None:
    print("WARNING: using hardcoded ToflerSession; set TOFLER_SESSION env var.", flush=True)


def log(msg):
    with print_lock:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(str(msg).encode("ascii", "replace").decode(), flush=True)


def get_session(headers: dict) -> requests.Session:
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        _thread_local.session = sess
    sess.headers.update(headers)
    return sess


def fetch_url(url: str, headers: dict, cookies: dict, timeout=(5, 15), max_retries: int = 3):
    """GET with retry on 429/5xx + backoff. Returns (text_or_None, status_or_None, err_or_None)."""
    sess = get_session(headers)
    last_status, last_err = None, None
    for attempt in range(max_retries):
        try:
            res = sess.get(url, cookies=cookies, timeout=timeout)
            last_status = res.status_code
            if res.status_code == 200:
                return res.text, res.status_code, None
            if res.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {res.status_code} (retryable)"
            else:
                return None, res.status_code, f"HTTP {res.status_code}"
        except Exception as err:
            last_err = str(err)[:200]
        if attempt < max_retries - 1:
            time.sleep(1.5 * (2 ** attempt))
    return None, last_status, last_err


def _norm_col(name: str) -> str:
    return "".join(ch for ch in str(name).strip().lower() if ch.isalnum())


def get_csv_field(row, candidates, default=None):
    """Case/space/punctuation-insensitive column lookup. e.g. 'PIN Code' matches 'pincode'."""
    norm_map = {_norm_col(c): c for c in row.index}
    for cand in candidates:
        key = _norm_col(cand)
        if key in norm_map:
            val = row[norm_map[key]]
            if pd.notna(val) and str(val).strip() not in ("", "nan", "None"):
                return str(val).strip()
    return default


def extract_pincode(row) -> str:
    raw = get_csv_field(row, ["pincode", "pin code", "pin_code", "pin", "zip"], default="000000")
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return digits.zfill(6) if digits else "000000"


def extract_geo(row, fb_district="Unknown", fb_sub="Unknown", fb_office="Unknown"):
    """Prefer CSV columns; fall back to path-derived values. (#7)"""
    district = get_csv_field(row, ["district"], default=fb_district) or fb_district
    sub = get_csv_field(
        row, ["division / sub-division", "division", "sub-division", "subdivision", "sub_district", "subdistrict"],
        default=fb_sub) or fb_sub
    office = get_csv_field(
        row, ["post office / area", "post office", "postoffice", "post_office_name", "office"],
        default=fb_office) or fb_office
    return district, sub, office


def slugify_company(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower())
    return slug.strip("-") or "company"


def fetch_tofler_by_cin(cin: str, company_name: str | None = None):
    """Try Tofler URL candidates. Real pattern is /{slug}/company/{cin};
    the old /company/{cin} 404s. Returns (html_or_None, url_used, err_or_None)."""
    candidates = [f"https://www.tofler.in/company/{cin}"]
    if company_name and not _is_empty_val(company_name):
        slug = slugify_company(company_name)
        candidates.append(f"https://www.tofler.in/{slug}/company/{cin}")
    last_url, last_err, last_status = candidates[-1], None, None
    for url in candidates:
        html, status, err = fetch_url(url, TOFLER_HEADERS, TOFLER_COOKIES)
        last_url, last_status, last_err = url, status, err
        if html:
            if url != candidates[0]:
                log(f"  [Tofler slug URL worked] CIN: {cin}")
            return html, url, None
        log(f"  [Tofler {err or 'no data'}] CIN: {cin} | {url}")
    return None, last_url, last_err or f"HTTP {last_status}"


def _is_empty_val(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() in ("", "Unknown", "None", "-", "null"):
        return True
    return False


def merge_basic_info(primary: dict, fallback: dict) -> dict:
    merged = dict(fallback or {})
    for k, v in (primary or {}).items():
        if not _is_empty_val(v):
            merged[k] = v
    return merged


def merge_directors(a: list, b: list) -> list:
    seen, out = set(), []
    for d in (a or []) + (b or []):
        key = (str(d.get("din") or "").strip() or
               f"name:{str(d.get('name') or '').strip().lower()}")
        if key in (None, "", "name:"):
            continue
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def merge_charges(a: list, b: list) -> list:
    seen, out = set(), []
    for c in (a or []) + (b or []):
        cid = str(c.get("charge_id") or "").strip()
        key = cid if cid else (
            str(c.get("creation_date")), str(c.get("amount")),
            str(c.get("charge_holder")))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def merge_payloads(zauba_payload, tofler_payload):
    """Single merged payload: Zauba wins on conflicts, Tofler fills gaps."""
    if zauba_payload and not tofler_payload:
        return zauba_payload
    if tofler_payload and not zauba_payload:
        return tofler_payload
    if not zauba_payload and not tofler_payload:
        return None
    company_name = (zauba_payload.get("company_name")
                    if not _is_empty_val(zauba_payload.get("company_name"))
                    else tofler_payload.get("company_name"))
    return {
        "cin": zauba_payload.get("cin") or tofler_payload.get("cin"),
        "company_name": company_name,
        "pincode": zauba_payload.get("pincode") or tofler_payload.get("pincode"),
        "post_office_name": zauba_payload.get("post_office_name") or tofler_payload.get("post_office_name"),
        "district": zauba_payload.get("district") or tofler_payload.get("district"),
        "sub_district": zauba_payload.get("sub_district") or tofler_payload.get("sub_district"),
        "basic_info": merge_basic_info(
            zauba_payload.get("basic_info"), tofler_payload.get("basic_info")),
        "directors": merge_directors(
            zauba_payload.get("directors"), tofler_payload.get("directors")),
        "charges": merge_charges(
            zauba_payload.get("charges"), tofler_payload.get("charges")),
        "source_url": zauba_payload.get("source_url") or tofler_payload.get("source_url"),
    }


def try_claim(cin: str) -> bool:
    with claim_lock:
        if cin in claimed_cins:
            return False
        claimed_cins.add(cin)
        return True


def should_skip(cin: str) -> bool:
    """Skip if company saved, or SUCCESS log, or recent FAILED (negative cache)."""
    with db_lock:
        conn = sqlite3.connect(MASTER_DB_PATH, timeout=60.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM companies WHERE cin = ?", (cin,))
            if cur.fetchone():
                return True
            try:
                cur.execute("SELECT status, fetched_at FROM scrape_logs WHERE cin = ?", (cin,))
                row = cur.fetchone()
            except sqlite3.OperationalError:
                return False
            if row:
                status, fetched_at = row
                if status == "SUCCESS":
                    return True
                if status == "FAILED" and fetched_at:
                    try:
                        days = (time.time() - pd.Timestamp(fetched_at).timestamp()) / 86400
                        if days < FAILED_RETRY_DAYS:
                            return True
                    except Exception:
                        return True
            return False
        finally:
            conn.close()


def save_log(cin: str, source: str, status: str, error: str | None = None):
    with db_lock:
        conn = sqlite3.connect(MASTER_DB_PATH, timeout=60.0)
        try:
            try:
                conn.execute(
                    """INSERT INTO scrape_logs (cin, primary_source, status, error, fetched_at)
                       VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(cin) DO UPDATE SET
                         primary_source=excluded.primary_source, status=excluded.status,
                         error=excluded.error, fetched_at=CURRENT_TIMESTAMP""",
                    (cin, source, status, (error or "")[:500]),
                )
            except sqlite3.OperationalError:
                # Old schema without error column (pre-migration fallback)
                conn.execute(
                    "INSERT OR REPLACE INTO scrape_logs (cin, primary_source, status) VALUES (?, ?, ?)",
                    (cin, source, status),
                )
            conn.commit()
        finally:
            conn.close()


def process_company_record(row, district_fb, sub_district_fb, pincode, office_fb):
    cin = get_csv_field(row, ["cin / llpin", "cin", "llpin", "cin/llpin"])
    zauba_url = get_csv_field(row, ["zauba url", "zaubaurl", "url", "link", "zauba"])
    if zauba_url and not zauba_url.startswith("http"):
        zauba_url = None

    if not cin:
        return
    cin = cin.strip()

    if not try_claim(cin):
        return
    if should_skip(cin):
        return

    district, sub_district, office_name = extract_geo(row, district_fb, sub_district_fb, office_fb)

    log(f"Processing CIN: {cin} ...")

    # 1. Fetch & Parse Zauba Corp (with retry)
    zauba_payload = None
    zauba_error = None
    if zauba_url:
        html, status, err = fetch_url(zauba_url, ZAUBA_HEADERS, ZAUBA_COOKIES)
        if html:
            try:
                master, directors, charges = parse_zauba_html(html, cin=cin)
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
                    log(f"  [OK] Parsed Zauba: {cin}")
                else:
                    zauba_error = "parsed but no Company Name"
            except Exception as e:
                zauba_error = str(e)[:200]
                log(f"  [Zauba Parse Error] CIN: {cin} | {e}")
        else:
            zauba_error = err or f"HTTP {status}"
            log(f"  [Zauba {zauba_error}] CIN: {cin}")
    else:
        zauba_error = "no zauba url in csv"

    # 2. Fetch & Parse Tofler (slug URL needs company name)
    tofler_payload = None
    tofler_error = None
    csv_company_name = get_csv_field(row, ["company name", "companyname"], default=None)
    tofler_name_hint = (zauba_payload.get("company_name")
                        if zauba_payload else csv_company_name)
    tofler_html, tofler_url, tofler_err = fetch_tofler_by_cin(cin, tofler_name_hint)
    if tofler_html:
        try:
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
            log(f"  [OK] Parsed Tofler: {cin}")
        except Exception as e:
            tofler_error = f"parse: {e}"[:200]
    else:
        tofler_error = tofler_err

    # 3. Merge payloads (Zauba primary, Tofler fills gaps) + single save.
    merged = merge_payloads(zauba_payload, tofler_payload)
    status_source = (
        "both"
        if (zauba_payload and tofler_payload)
        else ("zaubacorp" if zauba_payload else ("tofler" if tofler_payload else "failed"))
    )
    if merged:
        with db_lock:
            save_to_master_sqlite(merged)
        save_log(cin, status_source, "SUCCESS")
    else:
        error = "; ".join(
            x for x in [f"zauba: {zauba_error}" if zauba_error else None,
                        f"tofler: {tofler_error}" if tofler_error else None] if x
        ) or "both sources failed"
        save_log(cin, "failed", "FAILED", error)


def _rel_key(csv_file: str) -> str:
    try:
        return str(Path(csv_file).relative_to(BASE_DIR))
    except ValueError:
        return str(Path(csv_file).resolve())


def batch_process_all_files(force: bool = False, limit_files=None, limit_rows=None):
    from db_manager import get_file_progress, set_file_progress
    init_db(MASTER_DB_PATH)
    csv_files = []
    for sub in ("output", "others"):
        pattern = str(BASE_DIR / sub / "**" / "*.csv")
        found = glob.glob(pattern, recursive=True)
        if found:
            log(f"Discovered {len(found)} CSV files in {sub}/")
        csv_files.extend(found)
    csv_files.sort()
    if limit_files:
        csv_files = csv_files[:limit_files]
    total_files = len(csv_files)
    print(
        f"Discovered {total_files} CSV files. Processing data into: {MASTER_DB_PATH.resolve()}",
        flush=True,
    )
    if not force:
        log("Resume ON: done files (same mtime+size) + done CINs will be skipped. Use --force to reprocess.")

    skipped_files, queued_files = 0, 0
    fut_to_file: dict = {}
    file_pending: dict = {}
    file_meta: dict = {}
    all_futures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for index, csv_file in enumerate(csv_files, 1):
            parts = Path(csv_file).parts
            district_fb = parts[-3] if len(parts) >= 3 else "Unknown"
            sub_fb = parts[-2] if len(parts) >= 3 else "Unknown"
            office_fb = Path(csv_file).stem
            key = _rel_key(csv_file)

            try:
                st = os.stat(csv_file)
            except OSError as e:
                log(f"Skipping unreadable file {csv_file}: {e}")
                continue

            if not force:
                prev = get_file_progress(MASTER_DB_PATH, key)
                if prev and prev[3] == "done" and prev[0] == st.st_mtime and prev[1] == st.st_size:
                    skipped_files += 1
                    continue

            try:
                df = pd.read_csv(csv_file, dtype=str, keep_default_na=True)
                if df.empty:
                    set_file_progress(MASTER_DB_PATH, key, st.st_mtime, st.st_size, 0, "done")
                    skipped_files += 1
                    continue
                if limit_rows:
                    df = df.head(limit_rows)

                log(
                    f"\n--- Queueing File [{index}/{total_files}]: {csv_file}"
                    f" ({len(df)} records) ---"
                )
                file_meta[key] = (st.st_mtime, st.st_size, len(df))
                file_pending[key] = 0
                queued_files += 1
                for _, row in df.iterrows():
                    pincode = extract_pincode(row)
                    fut = executor.submit(
                        process_company_record,
                        row,
                        district_fb,
                        sub_fb,
                        pincode,
                        office_fb,
                    )
                    fut_to_file[fut] = key
                    file_pending[key] += 1
                    all_futures.append(fut)

            except Exception as e:
                log(f"Error reading CSV {csv_file}: {e}")

        for future in as_completed(all_futures):
            key = fut_to_file.get(future)
            try:
                future.result()
            except Exception as err:
                log(f"Worker task error: {err}")
            if key and key in file_pending:
                file_pending[key] -= 1
                if file_pending[key] <= 0:
                    mtime, size, total = file_meta.get(key, (0, 0, 0))
                    try:
                        # Re-stat in case file changed mid-run
                        st = os.stat(str(BASE_DIR / key) if not os.path.isabs(key) else key)
                        mtime, size = st.st_mtime, st.st_size
                    except OSError:
                        pass
                    set_file_progress(MASTER_DB_PATH, key, mtime, size, total, "done")
                    log(f"  [File done] {key} ({total} rows)")
                    del file_pending[key]

    print(
        f"Done. files_total={total_files} files_queued={queued_files} "
        f"files_skipped_done={skipped_files} db={MASTER_DB_PATH.resolve()}",
        flush=True,
    )

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Scrape Zauba/Tofler into master SQLite DB (resumable).")
    ap.add_argument("--force", action="store_true", help="Ignore resume ledger and reprocess everything")
    ap.add_argument("--limit-files", type=int, default=None, help="Process only first N files (testing)")
    ap.add_argument("--limit-rows", type=int, default=None, help="Process only first N rows per file (testing)")
    args = ap.parse_args()
    batch_process_all_files(force=args.force, limit_files=args.limit_files, limit_rows=args.limit_rows)
