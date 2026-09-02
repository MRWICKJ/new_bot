import json
import re
from bs4 import BeautifulSoup


def parse_zauba_html(html, cin=None):
    soup = BeautifulSoup(html, "html.parser")

    # 1. Company Master Details
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

    # 2. Directors & Management
    directors = []
    json_ld = soup.find("script", type="application/ld+json")
    if json_ld:
        try:
            data = json.loads(json_ld.string)
            master_data["cin"] = master_data["cin"] or data.get(
                "identifier", {}
            ).get("value")
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

    # 3. Open / Satisfied Charges Extraction
    charges = []
    charges_sec = soup.find("div", id="charges") or soup.find(
        "div", id="charges-details"
    )

    if not charges_sec:
        for table in soup.find_all("table"):
            if "charge id" in table.text.lower():
                charges_sec = table
                break

    if charges_sec:
        table = (
            charges_sec
            if charges_sec.name == "table"
            else charges_sec.find("table")
        )
        if table:
            rows = table.find_all("tr")
            if len(rows) > 1:
                raw_headers = [
                    th.text.strip().lower() for th in rows[0].find_all(["th", "td"])
                ]

                for row in rows[1:]:
                    cols = [td.text.strip() for td in row.find_all("td")]
                    if len(cols) == len(raw_headers):
                        row_dict = dict(zip(raw_headers, cols))

                        charge_id = (
                            row_dict.get("charge id") or row_dict.get("id") or None
                        )
                        creation_date = (
                            row_dict.get("date of creation")
                            or row_dict.get("creation date")
                            or None
                        )
                        modification_date = (
                            row_dict.get("date of modification")
                            or row_dict.get("modification date")
                            or None
                        )
                        closure_date = (
                            row_dict.get("date of satisfaction")
                            or row_dict.get("closure date")
                            or None
                        )
                        amount_str = row_dict.get("amount") or "0"

                        try:
                            amount = float(
                                amount_str.replace(",", "").replace("₹", "").strip()
                            )
                        except ValueError:
                            amount = 0.0

                        charge_holder = (
                            row_dict.get("holder name")
                            or row_dict.get("charge holder")
                            or row_dict.get("bank")
                            or None
                        )
                        status = row_dict.get("status") or (
                            "CLOSED" if closure_date else "OPEN"
                        )

                        charges.append({
                            "cin": cin or master_data.get("cin"),
                            "charge_id": charge_id,
                            "creation_date": creation_date,
                            "modification_date": modification_date,
                            "closure_date": closure_date,
                            "amount": amount,
                            "charge_holder": charge_holder,
                            "status": status,
                        })

    return master_data, directors, charges


def parse_tofler_html(html, cin=None):
    soup = BeautifulSoup(html, "html.parser")

    # 1. Master Info
    master_data = {"cin": cin, "Company Name": None}
    title_elem = soup.find("h1") or soup.find("title")
    if title_elem:
        master_data["Company Name"] = (
            title_elem.text.split(" - ")[0].split(" Overview")[0].strip()
        )

    json_ld = soup.find("script", type="application/ld+json")
    if json_ld and json_ld.string:
        try:
            ld_data = json.loads(json_ld.string)
            if isinstance(ld_data, dict):
                master_data["cin"] = master_data["cin"] or ld_data.get(
                    "identifier"
                )
                master_data["Company Name"] = (
                    master_data["Company Name"] or ld_data.get("name")
                )
        except Exception:
            pass

    for row in soup.find_all(["tr", "div", "p"]):
        text = row.get_text(" ", strip=True)

        if "CIN" in text and not master_data.get("cin"):
            match = re.search(
                r"([A-Z][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6})", text
            )
            if match:
                master_data["cin"] = match.group(1)

        if (
            "Incorporation Date" in text
            and "Incorporation Date" not in master_data
        ):
            master_data["Incorporation Date"] = text.split(":")[-1].strip()

        if "Company Status" in text and "Company Status" not in master_data:
            master_data["Company Status"] = text.split(":")[-1].strip()

    # 2. Financial Metrics
    financials = {}
    financial_keys = [
        "Authorized Capital",
        "Paid-Up Capital",
        "Total Revenue",
        "Net Profit",
        "EBITDA",
        "Networth",
        "Total Borrowings",
        "Total Assets",
    ]

    for elem in soup.find_all(["tr", "div", "li"]):
        text = elem.get_text(" ", strip=True)
        for key in financial_keys:
            if key in text and key not in financials:
                val_match = re.search(
                    r"₹?\s*[\d,]+(?:\.\d+)?\s*(?:Cr|Lakh|%)?", text
                )
                if val_match:
                    financials[key] = val_match.group(0).strip()

    # 3. Directors
    directors = []
    directors_sec = (
        soup.find("div", id="directors")
        or soup.find("section", id="directors")
        or soup.find("table", class_=re.compile("director", re.I))
    )

    if directors_sec:
        rows = directors_sec.find_all(["tr", "li", "div", "p"])
        for row in rows:
            text = row.get_text(" ", strip=True)
            din_match = re.search(r"\b\d{8}\b", text)
            if din_match:
                din = din_match.group(0)
                name = re.sub(r"DIN:\s*\d{8}", "", text).strip()
                directors.append({
                    "cin": master_data.get("cin"),
                    "din": din,
                    "name": name.split("\n")[0].strip(),
                    "designation": "Director",
                })

    return master_data, financials, directors