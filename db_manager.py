import sqlite3
import re
from pathlib import Path

MASTER_DB_PATH = Path(__file__).parent / "all_companies_master.db"

def sanitize_name(name: str) -> str:
    if not name:
        return "Unknown"
    clean = re.sub(r'[^\w\.-]', '_', str(name).strip())
    return re.sub(r'_+', '_', clean)

def init_db(db_path: Path = MASTER_DB_PATH):
    """Initializes tables inside all_companies_master.db."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    cursor = conn.cursor()

    # Enable Write-Ahead Logging for higher concurrent read/write throughput
    cursor.execute('PRAGMA journal_mode=WAL;')

    # 1. Main Company Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            cin TEXT PRIMARY KEY,
            company_name TEXT NOT NULL,
            pincode TEXT NOT NULL,
            post_office_name TEXT,
            district TEXT,
            sub_district TEXT,
            status TEXT,
            roc TEXT,
            incorporation_date TEXT,
            authorized_capital REAL,
            paid_up_capital REAL,
            email TEXT,
            phone TEXT,
            website TEXT,
            registered_address TEXT,
            source_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 2. Directors Table (din set to TEXT so missing DIN values do not fail)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS directors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cin TEXT NOT NULL,
            din TEXT,
            name TEXT NOT NULL,
            designation TEXT,
            appointment_date TEXT,
            is_active BOOLEAN DEFAULT 1,
            FOREIGN KEY (cin) REFERENCES companies (cin) ON DELETE CASCADE
        )
    ''')

    # 3. Company Charges Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cin TEXT NOT NULL,
            charge_id TEXT,
            creation_date TEXT,
            modification_date TEXT,
            closure_date TEXT,
            amount REAL,
            charge_holder TEXT,
            status TEXT,
            FOREIGN KEY (cin) REFERENCES companies (cin) ON DELETE CASCADE
        )
    ''')

    # 4. Tofler Raw Storage Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tofler_raw (
            cin TEXT PRIMARY KEY,
            company_name TEXT,
            html_content TEXT,
            source_url TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cin) REFERENCES companies (cin) ON DELETE CASCADE
        )
    ''')

    # 5. Scrape Source & Audit Logs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scrape_logs (
            cin TEXT PRIMARY KEY,
            primary_source TEXT,
            status TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Fast Lookups
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_pincode ON companies(pincode)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_directors_din ON directors(din)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_charges_cin ON charges(cin)')

    conn.commit()
    conn.close()

def save_to_master_sqlite(data: dict):
    init_db(MASTER_DB_PATH)

    conn = sqlite3.connect(MASTER_DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    basic = data.get("basic_info", {})

    # Company Master update logic...
    cursor.execute('''
        INSERT INTO companies (
            cin, company_name, pincode, post_office_name, district, sub_district,
            status, roc, incorporation_date, authorized_capital, 
            paid_up_capital, email, phone, website, 
            registered_address, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cin) DO UPDATE SET
            company_name=excluded.company_name,
            pincode=excluded.pincode,
            post_office_name=excluded.post_office_name,
            district=excluded.district,
            sub_district=excluded.sub_district,
            status=excluded.status,
            roc=excluded.roc,
            incorporation_date=excluded.incorporation_date,
            authorized_capital=excluded.authorized_capital,
            paid_up_capital=excluded.paid_up_capital,
            email=excluded.email,
            phone=excluded.phone,
            website=excluded.website,
            registered_address=excluded.registered_address,
            source_url=excluded.source_url
    ''', (
        data.get("cin"),
        data.get("company_name"),
        data.get("pincode"),
        data.get("post_office_name"),
        data.get("district"),
        data.get("sub_district"),
        basic.get("status") or basic.get("Company Status"),
        basic.get("roc") or basic.get("RoC"),
        basic.get("incorporation_date") or basic.get("Date of Incorporation"),
        basic.get("authorized_capital"),
        basic.get("paid_up_capital"),
        basic.get("email") or basic.get("Email"),
        basic.get("phone"),
        basic.get("website"),
        basic.get("registered_address") or basic.get("Registered Address"),
        data.get("source_url")
    ))

    # Update Directors
    cursor.execute('DELETE FROM directors WHERE cin = ?', (data.get("cin"),))
    director_rows = [
        (data.get("cin"), d.get("din") or d.get("DIN") or None, d.get("name") or d.get("Name") or "Unknown", d.get("designation") or d.get("Designation"), d.get("appointment_date"), d.get("is_active", True))
        for d in data.get("directors", [])
    ]
    if director_rows:
        cursor.executemany('''
            INSERT INTO directors (cin, din, name, designation, appointment_date, is_active)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', director_rows)

    # Update Charges Status
    cursor.execute('DELETE FROM charges WHERE cin = ?', (data.get("cin"),))
    charge_rows = [
        (
            data.get("cin"),
            c.get("charge_id"),
            c.get("creation_date"),
            c.get("modification_date"),
            c.get("closure_date"),
            c.get("amount"),
            c.get("charge_holder"),
            c.get("status")
        )
        for c in data.get("charges", [])
    ]
    if charge_rows:
        cursor.executemany('''
            INSERT INTO charges (cin, charge_id, creation_date, modification_date, closure_date, amount, charge_holder, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', charge_rows)

    conn.commit()
    conn.close()