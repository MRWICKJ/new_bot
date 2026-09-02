import json
import re
from bs4 import BeautifulSoup

def parse_zauba_html(html, cin=None):
    soup = BeautifulSoup(html, "html.parser")

    master_data = {"cin": cin}
    title_elem = soup.find("h1", id="title")
    master_data["Company Name"] = (
        title_elem.text.strip() if title_elem else None
    )

    basic_info_sec = soup.find("div", id="company-information")
    if basic_info_sec:
        rows = basic_info_sec.find_all("li", class_="row")
        for row in rows:
            label = row.find("span")
            val = row.find("label")
            if label and val:
                master_data[label.text.strip()] = val.text.strip()

    directors = []
    json_ld = soup.find("script", type="application/ld+json")
    if json_ld:
        try:
            data = json.loads(json_ld.string)
            master_data["cin"] = master_data["cin"] or data.get("identifier", {}).get("value")
            master_data["Registered Address"] = data.get("address")
            master_data["Email"] = data.get("email")

            for person in data.get("alumni", []):
                directors.append({
                    "cin": master_data["cin"],
                    "din": person.get("identifier"),
                    "name": person.get("name"),
                    "designation": person.get("jobTitle"),
                })
        except Exception:
            pass

    charges = []
    charges_sec = soup.find("div", id="charges") or soup.find("div", id="charges-details")

    if not charges_sec:
        for table in soup.find_all("table"):
            if "charge id" in table.text.lower():
                charges_sec = table
                break

    if charges_sec:
        table = charges_sec if charges_sec.name == "table" else charges_sec.find("table")
        if table:
            rows = table.find_all("tr")
            if len(rows) > 1:
                raw_headers = [th.text.strip().lower() for th in rows[0].find_all(["th", "td"])]
                for row in rows[1:]:
                    cols = [td.text.strip() for td in row.find_all("td")]
                    if len(cols) == len(raw_headers):
                        row_dict = dict(zip(raw_headers, cols))
                        amount_str = row_dict.get("amount") or "0"
                        try:
                            amount = float(amount_str.replace(",", "").replace("₹", "").strip())
                        except ValueError:
                            amount = 0.0

                        charges.append({
                            "cin": cin or master_data.get("cin"),
                            "charge_id": row_dict.get("charge id") or row_dict.get("id"),
                            "creation_date": row_dict.get("date of creation") or row_dict.get("creation date"),
                            "modification_date": row_dict.get("date of modification") or row_dict.get("modification date"),
                            "closure_date": row_dict.get("date of satisfaction") or row_dict.get("closure date"),
                            "amount": amount,
                            "charge_holder": row_dict.get("holder name") or row_dict.get("charge holder") or row_dict.get("bank"),
                            "status": row_dict.get("status") or ("CLOSED" if row_dict.get("date of satisfaction") else "OPEN"),
                        })

    return master_data, directors, charges


def parse_tofler_html(html, cin=None):
    soup = BeautifulSoup(html, "html.parser")

    master_data = {
        "cin": cin,
        "Company Name": None,
        "Company Status": None,
        "RoC": None,
        "Incorporation Date": None,
        "Authorized Capital": None,
        "Paid-Up Capital": None,
        "Email": None,
        "Registered Address": None
    }

    # Extract structured JSON-LD Metadata
    json_ld = soup.find("script", type="application/ld+json")
    if json_ld and json_ld.string:
        try:
            ld_data = json.loads(json_ld.string)
            if isinstance(ld_data, dict):
                master_data["cin"] = master_data["cin"] or ld_data.get("identifier")
                master_data["Company Name"] = ld_data.get("name")
                master_data["Email"] = ld_data.get("email")

                address = ld_data.get("address")
                if isinstance(address, dict):
                    master_data["Registered Address"] = address.get("streetAddress")
                elif isinstance(address, str):
                    master_data["Registered Address"] = address
        except Exception:
            pass

    if not master_data["Company Name"]:
        title_elem = soup.find("h1") or soup.find("title")
        if title_elem:
            master_data["Company Name"] = title_elem.text.split(" - ")[0].split(" Overview")[0].strip()

    # Extract Key-Value text pairs from elements
    for row in soup.find_all(["tr", "div", "li", "p"]):
        text = row.get_text(" ", strip=True)

        if not master_data["cin"] and "CIN" in text:
            match = re.search(r"([A-Z][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6})", text)
            if match:
                master_data["cin"] = match.group(1)

        if "Incorporation Date" in text and not master_data["Incorporation Date"]:
            master_data["Incorporation Date"] = text.split(":")[-1].strip()

        if "Company Status" in text and not master_data["Company Status"]:
            master_data["Company Status"] = text.split(":")[-1].strip()

        if "RoC" in text and not master_data["RoC"]:
            master_data["RoC"] = text.split(":")[-1].strip()

        if "Authorized Capital" in text and not master_data["Authorized Capital"]:
            val_match = re.search(r"[\d,]+(?:\.\d+)?", text)
            if val_match:
                try:
                    master_data["Authorized Capital"] = float(val_match.group(0).replace(",", ""))
                except ValueError:
                    pass

        if "Paid-Up Capital" in text and not master_data["Paid-Up Capital"]:
            val_match = re.search(r"[\d,]+(?:\.\d+)?", text)
            if val_match:
                try:
                    master_data["Paid-Up Capital"] = float(val_match.group(0).replace(",", ""))
                except ValueError:
                    pass

    # Extract Directors from Tofler
    directors = []
    directors_sec = (
        soup.find("div", id="directors")
        or soup.find("section", id="directors")
        or soup.find("table", class_=re.compile(r"director", re.I))
    )

    if directors_sec:
        rows = directors_sec.find_all(["tr", "li", "div"])
        for row in rows:
            text = row.get_text(" ", strip=True)
            din_match = re.search(r"\b\d{8}\b", text)
            if din_match:
                din = din_match.group(0)
                name = re.sub(r"DIN:\s*\d{8}", "", text).strip().split("\n")[0]
                directors.append({
                    "cin": master_data["cin"],
                    "din": din,
                    "name": name,
                    "designation": "Director",
                    "appointment_date": None,
                    "is_active": True
                })

    # Extract Charges from Tofler
    charges = []
    charges_sec = soup.find("div", id="charges") or soup.find("table", class_=re.compile(r"charge", re.I))
    if charges_sec:
        table = charges_sec if charges_sec.name == "table" else charges_sec.find("table")
        if table:
            rows = table.find_all("tr")
            if len(rows) > 1:
                headers = [th.text.strip().lower() for th in rows[0].find_all(["th", "td"])]
                for row in rows[1:]:
                    cols = [td.text.strip() for td in row.find_all("td")]
                    if len(cols) == len(headers):
                        r_data = dict(zip(headers, cols))
                        amount_str = r_data.get("amount") or r_data.get("charge amount") or "0"
                        try:
                            amount = float(amount_str.replace(",", "").replace("₹", "").strip())
                        except ValueError:
                            amount = 0.0

                        charges.append({
                            "cin": master_data["cin"],
                            "charge_id": r_data.get("charge id") or r_data.get("id"),
                            "creation_date": r_data.get("creation date") or r_data.get("date of creation"),
                            "modification_date": r_data.get("modification date"),
                            "closure_date": r_data.get("satisfaction date") or r_data.get("date of satisfaction"),
                            "amount": amount,
                            "charge_holder": r_data.get("holder name") or r_data.get("bank") or r_data.get("charge holder"),
                            "status": "CLOSED" if r_data.get("satisfaction date") else "OPEN"
                        })

    return master_data, directors, charges