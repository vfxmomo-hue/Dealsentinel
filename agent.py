#!/usr/bin/env python3
"""Deal Sentinel — surveille les anomalies de prix Keepa et des flux RSS.

Fonctionne avec la bibliothèque standard Python. Les alertes partent sur Telegram.
"""
from __future__ import annotations
import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
import unicodedata
from urllib.parse import urlsplit


def plain_text(value):
    return unescape(re.sub(r"<[^>]*>", " ", str(value or "")))


def normalized(value):
    text = unicodedata.normalize("NFKD", plain_text(value).casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def has_keyword(text, keyword):
    return re.search(r"(?<!\w)" + re.escape(normalized(keyword)) + r"(?!\w)", normalized(text)) is not None


CONDITIONAL_PRICE = re.compile(
    r"\b(?:cagnott\w*|cash\s*back|odr|rembours\w*|reprise|abonnement|forfait|"
    r"fidelit\w*|adherent\w*|infinity|parrain\w*|location|mensualit\w*)\b|\bclub\s*\+"
    r"|\bbon(?:s)?\s+d['’ ]achat\b|\bcarte\s+cadeau\b"
    r"|(?:€|eur)\s*/\s*(?:mois|month)\b"
)


def conditional_price(*texts):
    # Dealabs may display a net price after deferred rewards. Without an
    # independently verified upfront amount, these offers must be skipped.
    return any(CONDITIONAL_PRICE.search(normalized(t)) for t in texts)


def euro_amount(value):
    if isinstance(value, bool) or value is None:
        return None
    raw = str(value).strip().replace("\u00a0", " ").replace("\u202f", " ")
    if re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+(?:,\d{1,2})?", raw):
        raw = raw.replace(" ", "").replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+(?:[,.]\d{1,2})?", raw):
        raw = raw.replace(",", ".")
    else:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return float(amount) if amount.is_finite() and 1 <= amount <= 20000 else None


def dealabs_id(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"dealabs.com", "www.dealabs.com"}:
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    match = re.fullmatch(r"/bons-plans/[^/]+-(\d+)/?", parsed.path)
    return match.group(1) if match else None


class DealabsData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.threads = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs).get("data-vue3")
        if not data:
            return
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict) and isinstance(payload.get("props"), dict):
            thread = payload["props"].get("thread")
            if isinstance(thread, dict):
                self.threads.append(thread)


def verified_dealabs_price(page, item):
    """Only read props.thread.price belonging to this exact deal and title.

    No fallback to description numbers, JSON recommendations, reference prices,
    discounts, cashback, delivery amounts, or the title is allowed.
    """
    thread_id = dealabs_id(item.get("link", ""))
    if thread_id is None:
        return None
    parser = DealabsData()
    parser.feed(page)
    prices = set()
    for thread in parser.threads:
        if str(thread.get("threadId")) != thread_id:
            continue
        if normalized(thread.get("title")) != normalized(item.get("title")):
            return None
        descriptions = [item.get("description", ""), thread.get("description", ""),
                        thread.get("content", ""), thread.get("descriptionHtml", "")]
        if conditional_price(thread.get("title", ""), *descriptions):
            return None
        if thread.get("isExpired") or thread.get("isDeleted") or thread.get("status") in ("expired", "deleted"):
            return None
        if thread.get("discountType") is not None and thread.get("discountType") != "":
            return None
        if thread.get("currency", "EUR") not in ("EUR", "€"):
            return None
        price = euro_amount(thread.get("price"))
        if price is None:
            return None
        prices.add(price)
    return prices.pop() if len(prices) == 1 else None


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
    if any(has_keyword(title, x) for x in config.get("exclude_keywords", [])):
        return False
    return any(has_keyword(title, x) for x in config.get("keywords", []))


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
            ceiling = price_limit_for_title(title, config)
            if ceiling is not None and price > ceiling:
                continue
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
            fields[name] = "".join(child.itertext()).strip()
            if name == "link" and not fields[name]:
                fields[name] = child.attrib.get("href", "")
        title = fields.get("title", "")
        link = fields.get("link", "")
        guid = fields.get("guid") or fields.get("id") or link or title
        description = "\n".join(fields.get(k, "") for k in ("description", "summary", "content", "encoded"))
        entries.append({"title": plain_text(title).strip(), "link": link, "guid": guid,
                        "description": description})
    return entries


def price_limit_for_title(title: str, config: dict, fallback=None):
    """Return the category-specific ceiling matching this offer title."""
    limits = config.get("price_limits_by_keyword", {})
    matched = [float(limit) for keyword, limit in limits.items() if has_keyword(title, keyword)]
    if matched:
        return min(matched)
    return fallback


def rss_candidates(config: dict, env: dict):
    found = []
    # Keep page verification within the three-minute Actions job, including
    # when a feed has many offers or Dealabs is unavailable.
    deadline = time.monotonic() + 80
    verified = {}
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
                ceiling = price_limit_for_title(item["title"], config, feed.get("max_price_eur"))
                if ceiling is None:
                    continue
                if conditional_price(item["title"], item.get("description", "")):
                    print("Offre ignorée : prix soumis à avantage différé ou condition.")
                    continue
                if dealabs_id(item["link"]) is None:
                    print("Offre ignorée : aucune source de prix vérifiable prise en charge.")
                    continue
                if time.monotonic() >= deadline:
                    print("Budget de vérification RSS atteint ; reprise au prochain passage.")
                    return found
                cache_key = (item["link"], item["title"], item.get("description", ""))
                if cache_key not in verified:
                    try:
                        page = fetch(item["link"], headers={"User-Agent": "DealSentinel/1.1 (price verification)"},
                                     timeout=min(8, max(1, deadline - time.monotonic())))
                        verified[cache_key] = verified_dealabs_price(page.decode("utf-8"), item)
                    except (OSError, ValueError, UnicodeError):
                        verified[cache_key] = None
                price = verified[cache_key]
                if price is None:
                    print("Offre ignorée : prix du produit non vérifié.")
                    continue
                # Un flux RSS n'apporte pas à lui seul un historique fiable.
                # On alerte seulement si le prix est inférieur au seuil configuré.
                if price > float(ceiling):
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
        detail = f"Prix de l'article déclaré par Dealabs : {d['price']:.2f} €\nSeuil personnalisé atteint (hors livraison)"
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
