#!/usr/bin/env python3
"""Sleduje nové datasety a posílá je na Telegram. Jen standardní knihovna."""
import json, os, re, sys, urllib.parse, urllib.request
from pathlib import Path

STATE = Path(__file__).with_name("seen.json")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Klíčová slova oddělená čárkou; prázdné = posílat vše. Např.: "climate,weather,nlp"
KEYWORDS = [k.strip().lower() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "20"))
UA = {"User-Agent": "dataset-watch/1.0"}


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def clean(text, n=200):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


# --- Zdroje: každý vrací seznam {id, title, url, desc} ----------------------
import datetime as dt
import xml.etree.ElementTree as ET


def get_text(url, accept="*/*"):
    req = urllib.request.Request(url, headers={**UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def rss(url, prefix):
    """Obecný parser RSS 2.0 i Atom."""
    root = ET.fromstring(get_text(url))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        out.append({"id": f"{prefix}:{(it.findtext('guid') or link).strip()}",
                    "title": clean(it.findtext("title"), 150), "url": link,
                    "desc": clean(it.findtext("description"))})
    ns = "{http://www.w3.org/2005/Atom}"
    for e in root.iter(ns + "entry"):
        l = e.find(ns + "link")
        link = l.get("href") if l is not None else ""
        out.append({"id": f"{prefix}:{e.findtext(ns + 'id') or link}",
                    "title": clean(e.findtext(ns + "title"), 150), "url": link,
                    "desc": clean(e.findtext(ns + "summary"))})
    return out


def csu_publikace():
    return rss("https://csu.gov.cz/rss/produkty?kodVlastnostiVystupu=Pub&jazyk=CS", "csu-pub")


def csu_otevrena_data():
    """Kalendář aktualizací otevřených dat ČSÚ (ICS). Oznámí se, až když datum nastane."""
    year = dt.date.today().year
    now = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out = []
    for y in (year, year - 1) if dt.date.today().month == 1 else (year,):
        txt = get_text(f"https://csu.gov.cz/pdi-ws/external/ics/od?rocnik={y}")
        txt = re.sub(r"\r?\n[ \t]", "", txt)  # rozbalení zalomených řádků
        for ev in txt.split("BEGIN:VEVENT")[1:]:
            m = re.search(r"DTSTART[^:]*:(\d{8}T\d{6})", ev)
            code = re.search(r"Kód produktu:\s*([\w-]+)", ev)
            name = re.search(r"Název:\s*(.+?)(?:\\n|\r?\n)", ev)
            url = re.search(r"URL:\s*(\S+?)(?:\\n|\r?\n)", ev)
            if not (m and code and name) or m.group(1) > now:
                continue
            out.append({"id": f"csu-od:{code.group(1)}:{m.group(1)[:8]}", "title": name.group(1).strip(),
                        "url": url.group(1) if url else "https://csu.gov.cz/otevrena_data",
                        "desc": f"Aktualizace otevřených dat ČSÚ ({m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]})"})
    return out


NKOD_QUERY = """
PREFIX dcat: <http://www.w3.org/ns/dcat#>
PREFIX dct: <http://purl.org/dc/terms/>
SELECT ?ds ?title ?issued ?desc WHERE {
  %s
}
ORDER BY DESC(?issued) LIMIT 200
"""
NKOD_BODY = """?ds a dcat:Dataset ; dct:title ?title ; dct:issued ?issued .
  FILTER(lang(?title) = "cs" || lang(?title) = "")
  OPTIONAL { ?ds dct:description ?desc . FILTER(lang(?desc) = "cs" || lang(?desc) = "") }"""


def nkod():
    """Národní katalog otevřených dat (všechny české instituce) přes SPARQL."""
    last = None
    for body in ("GRAPH ?g { %s }" % NKOD_BODY, NKOD_BODY):  # data bývají v pojmenovaných grafech
        q = urllib.parse.urlencode({"query": NKOD_QUERY % body})
        try:
            d = json.loads(get_text(f"https://data.gov.cz/sparql?{q}", "application/sparql-results+json"))
        except Exception as e:
            last = e
            continue
        rows = d["results"]["bindings"]
        if rows:
            return [{"id": f"nkod:{r['ds']['value']}", "title": clean(r["title"]["value"], 150),
                     "url": "https://data.gov.cz/datová-sada?iri=" + urllib.parse.quote(r["ds"]["value"], safe=""),
                     "desc": clean(r.get("desc", {}).get("value"))} for r in rows]
    if last:
        raise last
    return []


SOURCES = {"ČSÚ – otevřená data": csu_otevrena_data,
           "ČSÚ – publikace": csu_publikace,
           "NKOD (data.gov.cz)": nkod}
# Další zdroj = nová funkce výše + řádek v SOURCES.


# --- Telegram ---------------------------------------------------------------
def send(text):
    if not (TOKEN and CHAT_ID):
        print("[dry-run]", text.replace("\n", " | "))
        return
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data), timeout=30).read()


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    first_run = not STATE.exists()
    seen = set(json.loads(STATE.read_text())) if not first_run else set()
    new = []
    for name, fn in SOURCES.items():
        try:
            items = fn()
        except Exception as e:  # jeden spadlý zdroj nesmí shodit ostatní
            print(f"CHYBA {name}: {e}", file=sys.stderr)
            continue
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            blob = f"{it['title']} {it['desc']}".lower()
            if KEYWORDS and not any(k in blob for k in KEYWORDS):
                continue
            new.append((name, it))

    if first_run:
        print(f"První běh: uloženo {len(seen)} položek, nic se neposílá.")
    else:
        for name, it in new[:MAX_PER_RUN]:
            send(f"📦 <b>{esc(it['title'])}</b>\n<i>{name}</i>\n{esc(it['desc'])}\n{it['url']}")
        if len(new) > MAX_PER_RUN:
            send(f"… a dalších {len(new) - MAX_PER_RUN} nových datasetů.")
        print(f"Nových: {len(new)}")

    STATE.write_text(json.dumps(sorted(seen)[-5000:], indent=0))


if __name__ == "__main__":
    main()
