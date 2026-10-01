#!/usr/bin/env python3
"""Deal Sentinel — surveille les anomalies de prix Keepa et des flux RSS.

Fonctionne avec la bibliothèque standard Python. Les alertes partent sur Telegram.
"""
from __future__ import annotations
import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path: Path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def fetch(url: str, *, data=None, headers=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def send_telegram(text: str, env: dict):
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "false"}).encode()
    response = json.loads(fetch(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}))
    if not response.get("ok"):
        raise RuntimeError("Telegram n'a pas accepté le message")


def read_env():
    values = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip().strip('"').strip("'")
    allowed = {"KEEPA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DEALABS_RSS_URL", "DEALABS_RSS_NAME", "RSS_MAX_PRICE_EUR"}
    values.update({k: v for k, v in os.environ.items() if k in allowed})
    return values


def matches(title: str, config: dict) -> bool:
    text = title.casefold()
    excluded = [x.casefold() for x in config.get("exclude_keywords", [])]
    if any(x in text for x in excluded):
        return False
    return any(x.casefold() in text for x in config.get("keywords", []))


def keepa_deals(config: dict, env: dict):
    key = env.get("KEEPA_API_KEY", "").strip()
    if not key:
        print("Keepa ignoré : ajoutez KEEPA_API_KEY dans .env")
        return []
    found = []
    threshold = float(config.get("min_drop_percent", 40))
    # Keepa applique titleSearch avec une logique ET. On fait donc une requête
    # dédiée à chaque famille de produits, au lieu de perdre les offres dans un
    # lot général limité aux 150 premiers résultats.
    for phrase in config.get("keepa_search_phrases", ["iphone", "ps5"]):
        body = {
            "page": 0,
            "domainId": 4,
            "priceTypes": [0],  # Prix Amazon, pas un vendeur tiers inconnu
            "dateRange": 1,
            "isRangeEnabled": True,
            "deltaPercentRange": [10, 100],
            "titleSearch": phrase,
            "sortType": 4,
        }
        query = urllib.parse.urlencode({"key": key})
        payload = json.dumps(body).encode()
        raw = fetch(f"https://api.keepa.com/deal?{query}", data=payload,
                    headers={"Content-Type": "application/json"}, timeout=40)
        response = json.loads(raw)
        deals = response.get("deals", {}).get("dr", [])
        for d in deals:
            title = re.sub(r"<[^>]+>", " ", str(d.get("title", ""))).strip()
            if not title or not matches(title, config):
                continue
            current = d.get("current") or []
            avg_rows = d.get("avg") or []
            if not avg_rows:
                continue
            # Keepa renvoie la série correspondant à la période demandée,
            # puis la moyenne de 90 jours; cette dernière est la rangée finale.
            avg90_row = avg_rows[-1]
            if not avg90_row:
                continue
            # La requête ne demande que le type 0 (prix Amazon).
            if not current or not isinstance(current[0], (int, float)) or current[0] <= 0:
                continue
            if not isinstance(avg90_row[0], (int, float)) or avg90_row[0] <= 0:
                continue
            price, avg90 = current[0] / 100.0, avg90_row[0] / 100.0
            drop = (1 - price / avg90) * 100
            if drop < threshold:
                continue
            asin = str(d.get("asin", ""))
            found.append({
                "key": f"keepa:{asin}", "title": title, "price": price, "reference": avg90,
                "drop": drop, "url": f"https://www.amazon.fr/dp/{asin}", "source": "Amazon France / Keepa",
            })
    return found


def parse_feed(url: str):
    raw = fetch(url, headers={"User-Agent": "DealSentinel/1.0 (RSS reader)"})
    root = ET.fromstring(raw)
    entries = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag not in {"item", "entry"}:
            continue
        fields = {}
        for child in list(el):
            name = child.tag.rsplit("}", 1)[-1].lower()
            fields[name] = (child.text or "").strip()
            if name == "link" and not fields[name]:
                fields[name] = child.attrib.get("href", "")
        title = fields.get("title", "")
        link = fields.get("link", "")
        guid = fields.get("guid") or fields.get("id") or link or title
        entries.append({"title": title, "link": link, "guid": guid})
    return entries


def price_from_text(text: str):
    # Accepte par exemple 499,99 €, 499.99 EUR ou 1 099 €.
    matches_found = re.findall(r"(?<!\d)(\d{1,4}(?:[ .\u202f]\d{3})*(?:[,.]\d{1,2})?)\s*(?:€|eur(?![a-z]))", text, re.I)
    if not matches_found:
        return None
    raw = matches_found[-1].replace("\u202f", "").replace(" ", "")
    if "," in raw and "." in raw:
        # Le dernier séparateur est la décimale; l'autre sépare les milliers.
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        grouping = "." if decimal == "," else ","
        raw = raw.replace(grouping, "").replace(decimal, ".")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "." in raw and len(raw.rsplit(".", 1)[-1]) == 3:
        raw = raw.replace(".", "")
    try:
        val = float(raw)
        return val if 1 <= val <= 20000 else None
    except ValueError:
        return None


def rss_candidates(config: dict, env: dict):
    found = []
    feeds = list(config.get("rss_feeds", []))
    # En hébergement GitHub Actions public, placer le flux Dealabs en secret
    # évite d'exposer son URL personnalisée dans le dépôt public.
    rss_url = env.get("DEALABS_RSS_URL", "").strip()
    if rss_url:
        feeds.append({
            "name": env.get("DEALABS_RSS_NAME", "Flux Dealabs"),
            "url": rss_url,
            "max_price_eur": env.get("RSS_MAX_PRICE_EUR", "") or None,
        })
    for feed in feeds:
        name, url = feed.get("name", "Flux RSS"), feed.get("url", "").strip()
        if not url:
            continue
        try:
            for item in parse_feed(url):
                if not matches(item["title"], config):
                    continue
                price = price_from_text(item["title"])
                if price is None:
                    continue
                # Un flux RSS n'apporte pas à lui seul un historique fiable.
                # On alerte seulement si le prix est inférieur au seuil configuré.
                ceiling = feed.get("max_price_eur")
                if ceiling is None or price > float(ceiling):
                    continue
                found.append({"key": f"rss:{item['guid']}", "title": item["title"], "price": price,
                              "reference": None, "drop": None, "url": item["link"], "source": name})
        except Exception as exc:
            print(f"Flux {name} inaccessible : {exc}")
    return found


def should_alert(deal: dict, state: dict, config: dict):
    old = state.get(deal["key"])
    now = time.time()
    if not old:
        return True
    old_price, sent = float(old.get("price", 0)), float(old.get("sent_at", 0))
    # Nouvel avis si le prix a baissé d'au moins 5 %, ou rappel après 24 h.
    return deal["price"] <= old_price * 0.95 or now - sent >= 24 * 3600


def format_alert(d: dict):
    if d.get("reference"):
        detail = f"Prix actuel : {d['price']:.2f} €\nMoyenne Keepa sur 90 jours : {d['reference']:.2f} €\nÉcart : −{d['drop']:.0f} %"
        label = "🚨 Grosse anomalie de prix à vérifier"
    else:
        detail = f"Prix repéré : {d['price']:.2f} €\nSeuil personnalisé atteint"
        label = "🔎 Offre repérée dans un flux"
    return f"{label}\n\n{d['title']}\n{detail}\nSource : {d['source']}\n{d['url']}\n\nVérifiez le modèle, l'état, le vendeur, les frais de livraison et le prix final avant achat."


def check_once(config, env, state):
    candidates = []
    try:
        candidates.extend(keepa_deals(config, env))
    except Exception as exc:
        print(f"Keepa inaccessible : {exc}")
    candidates.extend(rss_candidates(config, env))
    sent = 0
    for deal in candidates:
        if not should_alert(deal, state, config):
            continue
        try:
            send_telegram(format_alert(deal), env)
            state[deal["key"]] = {"price": deal["price"], "sent_at": time.time()}
            print(f"Alerte envoyée : {deal['title']} ({deal['price']:.2f} €)")
            sent += 1
        except Exception as exc:
            print(f"Échec envoi Telegram : {exc}")
    save_json(STATE_PATH, state)
    print(f"Vérification terminée : {len(candidates)} offre(s) candidate(s), {sent} alerte(s).")


def main():
    config = load_json(CONFIG_PATH, {})
    env = read_env()
    state = load_json(STATE_PATH, {})
    if "--test" in sys.argv:
        send_telegram("✅ Deal Sentinel est connecté. Les alertes arriveront ici.", env)
        print("Message de test envoyé.")
        return
    if "--once" in sys.argv:
        check_once(config, env, state)
        return
    interval = max(300, int(config.get("check_interval_seconds", 300)))
    print(f"Deal Sentinel actif — vérification toutes les {interval // 60} minutes. Ctrl+C pour arrêter.")
    while True:
        started = time.time()
        check_once(config, env, state)
        time.sleep(max(5, interval - (time.time() - started)))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent arrêté.")
