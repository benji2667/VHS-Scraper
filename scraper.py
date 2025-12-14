import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple

import requests
import pdfplumber
from bs4 import BeautifulSoup


SEARCH_URL = (
    "https://www.vhsit.berlin.de/vhskurse/BusinessPages/CourseSearch.aspx"
    "?direkt=1&begonnen=0&beendet=0&stichw=Goldschmieden%7CSchmuck"
)

DISTRICT_RE = re.compile(r"Friedrichshai\s*n-Kreuzberg", re.IGNORECASE)
STATE_PATH = "state.json"
PDF_PATH = "kursliste.pdf"


# Kursnummern in deinem PDF sehen so aus: FK2.604-A, FK2.664-C etc.
COURSE_ID_RE = re.compile(r"\b(FK\d\.\d{3}(?:-[A-Z])?)\b")

@dataclass
class Course:
    course_id: str
    district: str
    title: str
    raw: str  # kompletter Textblock zur Sicherheit


def load_state(path: str) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, courses: Dict[str, Course]) -> None:
    out = {cid: asdict(c) for cid, c in courses.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def extract_hidden_fields(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    fields = {}
    for inp in soup.select("input[type=hidden][name]"):
        name = inp.get("name")
        value = inp.get("value", "")
        fields[name] = value
    return fields


def download_pdf_via_webforms(session: requests.Session) -> bytes:
    """
    WebForms-Flow:
      1) GET SEARCH_URL -> landet auf CourseList.aspx (oder bleibt dort)
      2) Hidden fields aus HTML ziehen
      3) POST mit hidden fields + Submit-Button-Feld -> PDF
    """
    # Step 1: GET initial search/result page
    r1 = session.get(SEARCH_URL, timeout=30)
    r1.raise_for_status()

    # In manchen WebForms-Flows führt SEARCH_URL direkt auf die Ergebnisliste,
    # manchmal folgt ein Redirect. requests folgt Redirects automatisch.
    html = r1.text
    post_url = r1.url  # tatsächliche Zielseite (z.B. .../CourseList.aspx)

    hidden = extract_hidden_fields(html)

    # WebForms: PDF-Button ist oft ein <input type="submit" name="...btnPDFTop" value="Trefferliste als PDF">
    # In deinem Network-Log war der POST auf CourseList.aspx.
    # Entscheidend: zusätzlich zum ViewState muss das Submit-Feld gesetzt sein.
    payload = dict(hidden)

    # Häufig funktionieren beide Varianten; wir setzen beides, ohne etwas kaputtzumachen:
    # 1) klassischer Submit-Name
    payload["ctl00$Content$btnPDFTop"] = "Trefferliste als PDF"
    # 2) falls __EVENTTARGET benutzt wird, ebenfalls setzen (wenn vorhanden)
    if "__EVENTTARGET" in payload:
        payload["__EVENTTARGET"] = "ctl00$Content$btnPDFTop"
    if "__EVENTARGUMENT" in payload:
        payload["__EVENTARGUMENT"] = ""

    r2 = session.post(
        post_url,
        data=payload,
        timeout=60,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": post_url,
        },
    )

    # Debug-Ausgaben (landen in GitHub Actions Logs)
    print("POST status:", r2.status_code)
    print("POST final URL:", r2.url)
    print("Resp Content-Type:", r2.headers.get("Content-Type"))
    print("Resp Content-Disposition:", r2.headers.get("Content-Disposition"))

    r2.raise_for_status()

    ctype = (r2.headers.get("Content-Type") or "").lower()
    disp = (r2.headers.get("Content-Disposition") or "").lower()

    if "pdf" not in ctype and "attachment" not in disp:
        # speichere Response zum Debuggen
        with open("debug_response.html", "wb") as f:
            f.write(r2.content)
        snippet = r2.text[:800].replace("\n", " ")
        raise RuntimeError(
            "Erwartete PDF-Response, bekam vermutlich HTML. "
            f"Content-Type={ctype}, Content-Disposition={disp}. "
            f"Snippet: {snippet}"
        )

    return r2.content



def pdf_to_courses(pdf_bytes: bytes) -> Dict[str, Course]:
    """
    Robust gegen leichte Layout-Änderungen: wir arbeiten textbasiert, nicht über Tabellenzellen.
    Wir splitten den Gesamttext in Blöcke je Kursnummer.
    Dann filtern wir nach Bezirks-String.
    """
    with open(PDF_PATH, "wb") as f:
        f.write(pdf_bytes)

    full_text = []
    with pdfplumber.open(PDF_PATH) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                full_text.append(txt)

    text = "\n".join(full_text)

    # Blöcke nach Kursnummern
    # Wir finden alle Kurs-IDs + Startpositionen
    matches = list(COURSE_ID_RE.finditer(text))
    courses: Dict[str, Course] = {}

    for i, m in enumerate(matches):
        cid = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()

        # Titel-Heuristik:
        # In vielen Exporten steht nach der Kursnummer in derselben Zeile oder kurz danach der Titel.
        # Wir nehmen: erste Zeile ohne Kursnummer und ohne Bezirk als "title candidate".
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        title = ""

        # Entferne Kursnummer aus erster Zeile
        for ln in lines[:6]:  # nur früh suchen
            if DISTRICT_RE in ln:
                continue
            if COURSE_ID_RE.search(ln):
                # Kursnummer-Zeile -> Rest nach ID als Titelanteil
                rest = COURSE_ID_RE.sub("", ln).strip(" -–—\t")
                if rest and rest.lower() != cid.lower():
                    title = rest
                    break
                continue
            # ansonsten erster sinnvolle Kandidat
            if len(ln) >= 6:
                title = ln
                break

        courses[cid] = Course(
            course_id=cid,
            district=DISTRICT_RE,
            title=title,
            raw=block,
        )

    return courses


def diff_courses(prev: Dict[str, dict], curr: Dict[str, Course]) -> Tuple[List[Course], List[Course]]:
    prev_ids = set(prev.keys())
    curr_ids = set(curr.keys())

    new_ids = sorted(curr_ids - prev_ids)
    removed_ids = sorted(prev_ids - curr_ids)

    new_courses = [curr[cid] for cid in new_ids]
    removed_courses = [Course(**prev[cid]) for cid in removed_ids] if removed_ids else []
    return new_courses, removed_courses


def main() -> None:
    prev_state = load_state(STATE_PATH)

    with requests.Session() as s:
        s.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; vhs-goldschmiede-bot/1.0)",
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        pdf_bytes = download_pdf_via_webforms(s)

    curr_courses = pdf_to_courses(pdf_bytes)
    new_courses, removed_courses = diff_courses(prev_state, curr_courses)

    # Ausgabe für Actions-Logs
    print("DEBUG: Gefundene Kurs-IDs:", sorted(curr_courses.keys()))
    print(f"Gefunden (Bezirk={DISTRICT_RE}): {len(curr_courses)} Kurse")
    print(f"Neu seit letztem Lauf: {len(new_courses)}")
    if new_courses:
        print("\nNEUE KURSE:")
        for c in new_courses:
            print(f"- {c.course_id} | {c.title}".strip())
    if removed_courses:
        print(f"\nEntfernt seit letztem Lauf: {len(removed_courses)}")

    # State aktualisieren
    save_state(STATE_PATH, curr_courses)

    # GitHub Actions Output: Flag setzen, ob neu
    # (neue Output-Syntax)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"has_new={'true' if new_courses else 'false'}\n")


if __name__ == "__main__":
    main()
