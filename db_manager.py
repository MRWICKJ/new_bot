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
    """Initializes clean structured tables inside all_companies_master.db."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    cursor = conn.cursor()

    cursor.execute('PRAGMA journal_mode=WAL;')

    # 1. Main Structured Company Table
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

    # 2. Directors Table
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

    # 3. Charges Table
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

    # 4. Scrape Source & Audit Logs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scrape_logs (
            cin TEXT PRIMARY KEY,
            primary_source TEXT,
            status TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

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

    cursor.execute('''
        INSERT INTO companies (
            cin, company_name, pincode, post_office_name, district, sub_district,
            status, roc, incorporation_date, authorized_capital, 
            paid_up_capital, email, phone, website, 
            registered_address, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cin) DO UPDATE SET
            company_name=COALESCE(excluded.company_name, companies.company_name),
            pincode=COALESCE(excluded.pincode, companies.pincode),
            post_office_name=COALESCE(excluded.post_office_name, companies.post_office_name),
            district=COALESCE(excluded.district, companies.district),
            sub_district=COALESCE(excluded.sub_district, companies.sub_district),
            status=COALESCE(excluded.status, companies.status),
            roc=COALESCE(excluded.roc, companies.roc),
            incorporation_date=COALESCE(excluded.incorporation_date, companies.incorporation_date),
            authorized_capital=COALESCE(excluded.authorized_capital, companies.authorized_capital),
            paid_up_capital=COALESCE(excluded.paid_up_capital, companies.paid_up_capital),
            email=COALESCE(excluded.email, companies.email),
            phone=COALESCE(excluded.phone, companies.phone),
            website=COALESCE(excluded.website, companies.website),
            registered_address=COALESCE(excluded.registered_address, companies.registered_address),
            source_url=COALESCE(excluded.source_url, companies.source_url)
    ''', (
        data.get("cin"),
        data.get("company_name"),
        data.get("pincode"),
        data.get("post_office_name"),
        data.get("district"),
        data.get("sub_district"),
        basic.get("status") or basic.get("Company Status"),
        basic.get("roc") or basic.get("RoC"),
        basic.get("incorporation_date") or basic.get("Date of Incorporation") or basic.get("Incorporation Date"),
        basic.get("authorized_capital") or basic.get("Authorized Capital"),
        basic.get("paid_up_capital") or basic.get("Paid-Up Capital"),
        basic.get("email") or basic.get("Email"),
        basic.get("phone"),
        basic.get("website"),
        basic.get("registered_address") or basic.get("Registered Address"),
        data.get("source_url")
    ))

    directors = data.get("directors", [])
    if directors:
        cursor.execute('DELETE FROM directors WHERE cin = ?', (data.get("cin"),))
        director_rows = [
            (
                data.get("cin"),
                d.get("din") or d.get("DIN") or None,
                d.get("name") or d.get("Name") or "Unknown",
                d.get("designation") or d.get("Designation") or "Director",
                d.get("appointment_date") or d.get("Appointment Date"),
                d.get("is_active", True)
            )
            for d in directors
        ]
        cursor.executemany('''
            INSERT INTO directors (cin, din, name, designation, appointment_date, is_active)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', director_rows)

    charges = data.get("charges", [])
    if charges:
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
            for c in charges
        ]
        cursor.executemany('''
            INSERT INTO charges (cin, charge_id, creation_date, modification_date, closure_date, amount, charge_holder, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', charge_rows)

    conn.commit()
    conn.close()