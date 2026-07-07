import argparse
import base64
import gzip
import json
import os
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone, tzinfo

import requests
from bs4 import BeautifulSoup


DATA_URL = "https://app.dsbcontrol.de/JsonHandler.ashx/GetData"
DEFAULT_CUTOFF_HOUR = 8
DEFAULT_TIMEZONE = "Europe/Berlin"
SCHOOL_TABLEMAPPER = ["class", "lesson", "teacher", "subject", "room", "type", "text"]
SCHOOL_WEEKDAYS = {0, 1, 2, 3, 4}
FIXED_OFFSET_PATTERN = re.compile(r"^UTC(?P<sign>[+-])(?P<hours>\d{1,2})(?::(?P<minutes>\d{2}))?$", re.IGNORECASE)


class BerlinTimezone(tzinfo):
    def tzname(self, dt):
        return "CEST" if self._is_dst(dt) else "CET"

    def utcoffset(self, dt):
        return timedelta(hours=2 if self._is_dst(dt) else 1)

    def dst(self, dt):
        return timedelta(hours=1 if self._is_dst(dt) else 0)

    def fromutc(self, dt):
        standard_time = (dt + timedelta(hours=1)).replace(tzinfo=self)
        if self._is_dst(standard_time):
            return (dt + timedelta(hours=2)).replace(tzinfo=self)
        return standard_time

    def _is_dst(self, dt):
        if dt is None:
            return False
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        year = dt.year
        start = self._last_sunday(year, 3, 2)
        end = self._last_sunday(year, 10, 3)
        return start <= dt < end

    @staticmethod
    def _last_sunday(year, month, hour):
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        last_day = next_month - timedelta(days=1)
        while last_day.weekday() != 6:
            last_day -= timedelta(days=1)
        return datetime.combine(last_day, time(hour=hour))


BERLIN_TZ = BerlinTimezone()


def build_parser():
    parser = argparse.ArgumentParser(description="Fetch DSBMobile substitution plan entries.")
    parser.add_argument("--username", default=os.getenv("DSB_USERNAME"), help="DSB username")
    parser.add_argument("--password", default=os.getenv("DSB_PASSWORD"), help="DSB password")
    parser.add_argument("--class", dest="entry_class", help="Only include entries whose class exactly matches this value")
    parser.add_argument("--type", dest="entry_class_alias", help="Alias for --class")
    parser.add_argument(
        "--timezone",
        default=os.getenv("DSB_TIMEZONE", DEFAULT_TIMEZONE),
        help="Timezone used for day selection. Supports Europe/Berlin and fixed offsets like UTC+2.",
    )
    parser.add_argument(
        "--cutoff-hour",
        type=int,
        default=DEFAULT_CUTOFF_HOUR,
        help="Keep showing the same next school day until this hour on that day, default: 8",
    )
    parser.add_argument("--date", help="Optional explicit target date in YYYY-MM-DD format.")
    return parser


def parse_entry_date(value):
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_cli_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid --date value '{value}'. Use YYYY-MM-DD.") from exc


def resolve_timezone(name):
    normalized = name.strip()
    if normalized.lower() in {"europe/berlin", "berlin", "germany", "de"}:
        return BERLIN_TZ, DEFAULT_TIMEZONE

    match = FIXED_OFFSET_PATTERN.match(normalized)
    if match:
        hours = int(match.group("hours"))
        minutes = int(match.group("minutes") or 0)
        if hours > 23 or minutes > 59:
            raise SystemExit(f"Invalid timezone '{name}'. Use Europe/Berlin or a fixed offset like UTC+2.")
        total_minutes = hours * 60 + minutes
        if match.group("sign") == "-":
            total_minutes *= -1
        tz = timezone(timedelta(minutes=total_minutes))
        label = f"UTC{match.group('sign')}{hours:02d}:{minutes:02d}"
        return tz, label

    raise SystemExit(f"Unknown timezone '{name}'. Use Europe/Berlin or a fixed offset like UTC+2.")


def next_school_day(start_date):
    candidate = start_date + timedelta(days=1)
    while candidate.weekday() not in SCHOOL_WEEKDAYS:
        candidate += timedelta(days=1)
    return candidate


def resolve_target_date(timezone_name, cutoff_hour, explicit_date=None):
    tz, timezone_label = resolve_timezone(timezone_name)
    if explicit_date is not None:
        return explicit_date, None, timezone_label
    now = datetime.now(timezone.utc).astimezone(tz)
    anchor_date = now.date() if now.hour >= cutoff_hour else now.date() - timedelta(days=1)
    return next_school_day(anchor_date), now, timezone_label


def build_request_payload(username, password):
    current_time = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    params = {
        "UserId": username,
        "UserPw": password,
        "AppVersion": "2.5.9",
        "Language": "de",
        "OsVersion": "28 8.0",
        "AppId": str(uuid.uuid4()),
        "Device": "SM-G930F",
        "BundleId": "de.heinekingmedia.dsbmobile",
        "Date": current_time,
        "LastUpdate": current_time,
    }
    params_bytes = json.dumps(params, separators=(",", ":")).encode("utf-8")
    encoded = base64.b64encode(gzip.compress(params_bytes)).decode("utf-8")
    return {"req": {"Data": encoded, "DataType": 1}}


def fetch_api_data(session, username, password):
    response = session.post(DATA_URL, json=build_request_payload(username, password), timeout=15)
    response.raise_for_status()
    payload = response.json()
    compressed = payload["d"]
    data = json.loads(gzip.decompress(base64.b64decode(compressed)))
    if data.get("Resultcode") != 0:
        raise RuntimeError(data.get("ResultStatusInfo", "Unknown DSBMobile error"))
    return data


def extract_detail_urls(data):
    detail_urls = []
    menu_items = data.get("ResultMenuItems") or []
    if not menu_items:
        return detail_urls

    for page in menu_items[0].get("Childs", []):
        root = page.get("Root") or {}
        for child in root.get("Childs", []):
            child_nodes = child.get("Childs")
            if isinstance(child_nodes, list):
                for sub_child in child_nodes:
                    detail = sub_child.get("Detail")
                    if detail:
                        detail_urls.append(detail)
            elif isinstance(child_nodes, dict):
                detail = child_nodes.get("Detail")
                if detail:
                    detail_urls.append(detail)
    return detail_urls


def extract_updated(headers, index):
    if index >= len(headers):
        return "---"
    spans = headers[index].find_all("span")
    if not spans:
        return "---"
    sibling = spans[-1].next_sibling
    if not isinstance(sibling, str):
        return "---"
    parts = sibling.split("Stand: ", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return sibling.strip() or "---"


def extract_title_parts(titles, index):
    if index >= len(titles):
        return "---", "---"
    title = titles[index].strip()
    if not title:
        return "---", "---"
    parts = title.split(" ", 1)
    date_value = parts[0]
    day_text = parts[1] if len(parts) > 1 else "---"
    day_value = day_text.split(", ", 1)[0].replace(",", "").strip() or "---"
    return date_value, day_value


def extract_class_values(cells):
    raw_value = cells[0].get_text(strip=True) if cells else ""
    if not raw_value:
        return ["---"]
    return [part.strip() for part in raw_value.split(",") if part.strip()] or ["---"]


def fetch_timetable(session, url):
    response = session.get(url, timeout=15)
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")
    tables = soup.find_all("table", {"class": "mon_list"})
    headers = soup.find_all("table", {"class": "mon_head"})
    titles = [title.get_text(" ", strip=True) for title in soup.find_all("div", {"class": "mon_title"})]
    results = []

    for index, table in enumerate(tables):
        updated = extract_updated(headers, index)
        date_value, day_value = extract_title_parts(titles, index)
        rows = table.find_all("tr")[1:]

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            class_values = extract_class_values(cells)
            values = [cell.get_text(strip=True) or "---" for cell in cells]
            for class_value in class_values:
                entry = {
                    "date": date_value,
                    "day": day_value,
                    "updated": updated,
                }
                for column_index, key in enumerate(SCHOOL_TABLEMAPPER):
                    if column_index < len(values):
                        entry[key] = values[column_index]
                    else:
                        entry[key] = "---"
                entry["class"] = class_value if values[0] != "---" else "---"
                results.append(entry)
    return results


def normalize_entry(entry):
    return {
        "date": entry.get("date"),
        "day": entry.get("day"),
        "updated": entry.get("updated"),
        "class": entry.get("class"),
        "lesson": entry.get("lesson"),
        "teacher": entry.get("teacher"),
        "new_teacher": entry.get("subject"),
        "room": entry.get("room"),
        "type": entry.get("type"),
        "text": entry.get("text", "---"),
    }


def filter_entries(entries, entry_class=None, target_date=None):
    filtered = []
    for group in entries:
        matches = []
        for entry in group:
            normalized = normalize_entry(entry)

            cls = (normalized.get("class") or "").lower()

            if entry_class:
                ec = entry_class.lower()
                if not all(char in cls for char in ec):
                    continue

            if target_date and parse_entry_date(normalized.get("date")) != target_date:
                continue

            matches.append(normalized)

        if matches:
            filtered.append(matches)

    return filtered



def fetch_entries(username, password):
    session = requests.Session()
    data = fetch_api_data(session, username, password)
    detail_urls = extract_detail_urls(data)
    outputs = []
    for url in detail_urls:
        if url.endswith(".htm") and not url.endswith(".html") and not url.endswith("news.htm"):
            outputs.append(fetch_timetable(session, url))
    return outputs


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error("username and password are required")

    entry_class = args.entry_class or args.entry_class_alias

    target_date, now, timezone_label = resolve_target_date(
        args.timezone, args.cutoff_hour, args.date
    )

    entries = fetch_entries(args.username, args.password)
    entries = filter_entries(entries, entry_class, target_date)

    output = {
        "timezone": timezone_label,
        "target_date": target_date.isoformat(),
        "current_time": now.isoformat() if now else None,
        "filters": {
            "class": entry_class,
            "cutoff_hour": args.cutoff_hour,
        },
        "entries": entries,
    }

    # PRINT für Home Assistant
    print(json.dumps(output, ensure_ascii=False))

    # SAVE FILE
    output_path = "/config/dsb_output.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)


if __name__ == "__main__":
    main()