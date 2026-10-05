#!/usr/bin/env python3
"""Deal Sentinel — veille multi-boutiques gratuite, neuf et sans conditions.

Bibliothèque standard Python uniquement. --diagnose ne publie aucun message.
Les lecteurs de boutiques utilisent les données Product/Offer schema.org.
Leur fonctionnement sur les sites réels doit être contrôlé dans les diagnostics.
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
from concurrent.futures import ThreadPoolExecutor
from collections import Counter


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
    for text in texts:
        text = normalized(text)
        text = re.sub(r"\bsans (?:abonnement|forfait|engagement|reprise|odr|condition)\b", "", text)
        if CONDITIONAL_PRICE.search(text) or re.search(
            r"\b(?:joyplus|prime|membre|nouveau client|nouveaux clients|etudiant|financement|credit)\b"
            r"|\b(?:code|coupon)\s+(?:promo|promotionnel|obligatoire)\b", text):
            return True
    return False


NON_NEW = re.compile(r"\b(?:recondition\w*|refurbish\w*|renewed|occasion|used|seconde main|"
                     r"comme neuf|open box|deballe\w*|retour client|grade [abc]|endommage\w*)\b")


def target_product(title, config):
    text = normalized(title)
    if NON_NEW.search(text) or conditional_price(text):
        return False
    # Reject accessories and games, even if they mention a target device.
    if re.search(r"\b(?:coque|etui|protection|verre trempe|chargeur|cable|casque|ecouteur|batterie|"
                 r"figurine|sticker|skin|support|station|lecteur|housse)\b", text):
        if not (re.search(r"\bconsole\b", text) and re.search(r"\bavec lecteur\b", text)
                and not re.search(r"\b(?:coque|etui|housse|support|station)\b", text)):
            return False
    iphone = re.search(r"\biphone\s*(\d{1,2})(?:e)?\b", text)
    if iphone or re.search(r"\biphone air\b", text):
        if re.search(r"\b(?:pour|compatible|accessoire)\b", text):
            return False
        return not iphone or int(iphone.group(1)) >= int(config.get("min_iphone_generation", 16))
    if re.search(r"\b(?:ps5|playstation\s*5)\b", text):
        if re.search(r"\b(?:portal|vr2|accessoire|compatible|pour)\b", text):
            return False
        if re.search(r"\bconsole\b", text):
            return True
        return bool(re.fullmatch(r"(?:sony\s+)?(?:playstation\s*5|ps5)"
                                 r"(?:\s+(?:slim|pro|digital|standard|edition|numerique|blanc|noir|go|to|gb|tb|\d+))*", text))
    return False


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
        if any(NON_NEW.search(normalized(t)) for t in descriptions):
            return None
        condition = thread.get("itemCondition") or thread.get("condition")
        if schema_type(condition) not in ("NewCondition", "new", "neuf") and not has_keyword(thread.get("title", ""), "neuf"):
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
    if not target_product(title, config):
        return False
    for word in config.get("exclude_keywords", []):
        # A console bundle legitimately includes a controller.
        if normalized(word) in {"manette", "controller"} and has_keyword(title, "console"):
            continue
        if has_keyword(title, word):
            return False
    return True


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


RETAILERS = {
    "Carrefour": {
        "hosts": {"www.carrefour.fr", "carrefour.fr"}, "path": r"/p/[^/]+-\d+",
        "discover": ["https://www.carrefour.fr/s?q=iphone+17", "https://www.carrefour.fr/s?q=ps5"],
        "products": [
            "https://www.carrefour.fr/p/iphone-17-256-go-noir-mg6j4f-a-apple-0195950643435",
            "https://www.carrefour.fr/p/iphone-17-256-go-brume-mg6l4f-a-apple-0195950643831",
            "https://www.carrefour.fr/p/console-ps5-slim-sony-0711719577171"],
    },
    "E.Leclerc": {
        "hosts": {"www.e.leclerc", "e.leclerc"}, "path": r"/fp/[^/]+-\d+",
        "discover": ["https://www.e.leclerc/cat/iphone"],
        "products": [
            "https://www.e.leclerc/fp/apple-iphone-17-16-cm-6-3-double-sim-ios-26-5g-usb-type-c-256-go-noir-0195950643435",
            "https://www.e.leclerc/fp/apple-iphone-17-16-cm-6-3-double-sim-ios-26-5g-usb-type-c-256-go-lavande-0195950644036",
            "https://www.e.leclerc/fp/console-playstation-5-edition-standard-modele-slim-ps5-0711719021247",
            "https://www.e.leclerc/fp/pack-console-edition-numerique-playstation-5-fortnite-cobalt-star-modele-slim-ps5-0711719593560"],
    },
    "Joybuy": {
        "hosts": {"www.joybuy.fr", "m.joybuy.fr", "joybuy.fr"}, "path": r"/dp/(?:[^/]+/)?\d+",
        "discover": ["https://www.joybuy.fr/cms/iphone17-hp-banner-0912", "https://www.joybuy.fr/cms/consoles-et-jeux-video"],
        "products": [
            "https://www.joybuy.fr/dp/apple-iphone-17-256-go-blanc/10408701",
            "https://m.joybuy.fr/dp/10408705",
            "https://m.joybuy.fr/dp/10444422"],
    },
    "Amazon": {
        "hosts": {"www.amazon.fr", "amazon.fr"}, "path": r"/(?:[^/]+/)?dp/[A-Z0-9]{10}",
        "discover": [],
        "products": ["https://www.amazon.fr/Apple-iPhone-Pro-512-prodigieuse/dp/B0FQH2B7G3"],
    },
}


def retailer_url(url, retailer, *, product=False):
    """Validate hosts and keep offer/variant parameters (never mix SKUs)."""
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in RETAILERS[retailer]["hosts"]:
            return None
        if p.username or p.password or p.port not in (None, 443):
            return None
        if product and not re.fullmatch(RETAILERS[retailer]["path"], p.path.rstrip("/")):
            return None
        query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query)
                                       if k in {"offerId", "ref_sku", "s", "q", "page", "th", "psc"}])
        return urllib.parse.urlunsplit(("https", p.hostname, p.path.rstrip("/"), query, ""))
    except (ValueError, KeyError, TypeError):
        return None


def product_id(url, retailer):
    url = retailer_url(url, retailer, product=True)
    if not url:
        return None
    p = urlsplit(url)
    if retailer == "Amazon":
        identity = p.path.rsplit("/", 1)[-1]
    elif retailer == "Joybuy":
        identity = dict(urllib.parse.parse_qsl(p.query)).get("ref_sku") or p.path.rsplit("/", 1)[-1]
    else:
        identity = p.path.rsplit("-", 1)[-1]
    return identity


def fetch_retail_page(url, retailer):
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not retailer_url(newurl, retailer):
                raise ValueError("Redirection hors de la boutique")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    if not retailer_url(url, retailer):
        raise ValueError("URL boutique invalide")
    req = urllib.request.Request(url, headers={"User-Agent": "DealSentinel/2.0 (personal price monitor)",
                                              "Accept-Language": "fr-FR,fr;q=0.9"})
    with urllib.request.build_opener(SafeRedirect()).open(req, timeout=6) as response:
        raw = response.read(3_000_001)
        if len(raw) > 3_000_000:
            raise ValueError("Page trop volumineuse")
        return raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace"), response.url


class ProductPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.documents, self.links, self.headings = [], [], []
        self.script = None
        self.h1 = False
        self.anchor = None
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self.script = []
        if tag == "h1":
            self.h1 = True
        if tag == "a" and a.get("href"):
            self.anchor = [a["href"], a.get("title", "")]
        if tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href")

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif self.h1:
            self.headings.append(data)
        if self.anchor is not None and self.script is None:
            self.anchor[1] += " " + data

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            try:
                self.documents.append(json.loads("".join(self.script)))
            except ValueError:
                pass
            self.script = None
        if tag == "h1":
            self.h1 = False
        if tag == "a" and self.anchor is not None:
            self.links.append(tuple(self.anchor))
            self.anchor = None


def schema_type(value):
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name")
    return str(value or "").rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def individual_offers(value):
    """An AggregateOffer's lowPrice is not payable; explicit child Offers are."""
    if isinstance(value, list):
        for child in value:
            yield from individual_offers(child)
    elif isinstance(value, dict):
        if schema_type(value.get("@type")) == "AggregateOffer":
            if offer_restriction(value):
                return
            if value.get("itemCondition") and schema_type(value["itemCondition"]) != "NewCondition":
                return
            yield from individual_offers(value.get("offers"))
        else:
            yield value


class EvidencePage(HTMLParser):
    """Collect public visible price/condition blocks, never scripts or inputs."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.blocks, self.titles = [], [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        a = dict(attrs)
        hidden = tag in {"script", "style", "noscript"}
        if hidden:
            self.hidden += 1
        label = " ".join(str(a.get(k, "")) for k in ("id", "class", "itemprop", "data-testid"))[:250]
        selected = tag in {"h1", "title"} or bool(re.search(
            r"price|prix|condition|offer|seller|merchant|buybox|availability|stock|sold|neuf|productTitle", label, re.I))
        self.stack.append({"tag": tag, "label": label, "text": [], "length": 0,
                           "selected": selected, "hidden": hidden})

    def handle_data(self, data):
        if self.hidden:
            return
        for frame in self.stack:
            if frame["selected"] and frame["length"] < 1600:
                frame["text"].append(data[:1600-frame["length"]])
                frame["length"] += len(data)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        index = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i]["tag"] == tag), None)
        if index is None:
            return
        for frame in self.stack[index:]:
            if frame["hidden"]:
                self.hidden = max(0, self.hidden - 1)
            text = " ".join(" ".join(frame["text"]).split())
            if frame["tag"] in {"h1", "title"} and text:
                self.titles.append(text[:300])
            if frame["selected"] and text and len(self.blocks) < 60:
                self.blocks.append({"tag": frame["tag"], "selector": frame["label"], "text": text[:1200]})
        del self.stack[index:]


def public_price_fields(value, depth=0):
    """Whitelist product fields so diagnostics cannot include arbitrary tokens."""
    allowed = {"@type", "@id", "name", "sku", "gtin", "gtin13", "itemCondition", "availability",
               "price", "priceCurrency", "lowPrice", "highPrice", "offers", "seller", "priceSpecification",
               "validForMemberTier", "eligibleCustomerType", "validThrough", "priceValidUntil",
               "businessFunction", "value", "propertyID", "additionalProperty"}
    if depth > 7:
        return "[limite]"
    if isinstance(value, dict):
        return {k: public_price_fields(v, depth+1) for k, v in value.items() if k in allowed}
    if isinstance(value, list):
        return [public_price_fields(v, depth+1) for v in value[:12]]
    if isinstance(value, str):
        # @id often contains a public product URL: omit tracking query strings.
        if value.startswith(("https://", "http://")):
            p = urlsplit(value)
            return urllib.parse.urlunsplit((p.scheme, p.hostname or "", p.path, "", p.fragment))[:400]
        return value[:400]
    return value


def emit_source_evidence(retailer, url, page="", *, status=None, reason=None):
    parser = ProductPage()
    parser.feed(page)
    visible = EvidencePage()
    visible.feed(page)
    parts = urlsplit(url)
    evidence = {"retailer": retailer, "path": parts.path, "http_status": status,
                "reason": reason, "titles": visible.titles[:3],
                "products": [public_price_fields(p) for doc in parser.documents for p in products_in(doc)][:4],
                "blocks": visible.blocks[:40]}
    # Keep valid JSON even on large pages, with a bounded per-page log entry.
    encoded = json.dumps(evidence, ensure_ascii=False)
    while len(encoded) > 16000 and evidence["blocks"]:
        evidence["blocks"].pop()
        encoded = json.dumps(evidence, ensure_ascii=False)
    while len(encoded) > 24000 and evidence["products"]:
        evidence["products"].pop()
        encoded = json.dumps(evidence, ensure_ascii=False)
    print("SOURCE_EVIDENCE " + encoded, flush=True)


def products_in(document):
    if isinstance(document, list):
        for entry in document:
            yield from products_in(entry)
    elif isinstance(document, dict):
        types = document.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if "Product" in [schema_type(t) for t in types]:
            yield document
        # Graphs and variants are explicit structures; recommendations are not
        # recursively treated as the product belonging to this page.
        for field in ("@graph", "hasVariant"):
            yield from products_in(document.get(field))


def offer_restriction(offer):
    if not isinstance(offer, dict):
        return True
    if conditional_price(offer.get("name"), offer.get("description")):
        return True
    if NON_NEW.search(normalized(offer.get("name"))) or NON_NEW.search(normalized(offer.get("description"))):
        return True
    if any(offer.get(k) for k in ("validForMemberTier", "eligibleCustomerType", "eligibleDuration",
                                 "billingDuration", "billingIncrement", "leaseLength", "eligibleTransactionVolume")):
        return True
    if offer.get("availableAtOrFrom") or offer.get("ineligibleRegion"):
        return True
    region = offer.get("eligibleRegion")
    if region is not None and region not in ("FR", "France"):
        return True
    if offer.get("businessFunction") and schema_type(offer["businessFunction"]) != "Sell":
        return True
    quantity = offer.get("eligibleQuantity")
    if quantity is not None and quantity not in ({"value": 1}, {"minValue": 1, "maxValue": 1}):
        return True
    specs = offer.get("priceSpecification", [])
    specs = specs if isinstance(specs, list) else [specs]
    for spec in specs:
        if not isinstance(spec, dict) or any(spec.get(k) for k in (
            "validForMemberTier", "eligibleCustomerType", "billingDuration", "billingIncrement",
            "priceType", "unitCode", "referenceQuantity")):
            return True
        if conditional_price(spec.get("name"), spec.get("description")):
            return True
        if "price" in spec and euro_amount(spec["price"]) != euro_amount(offer.get("price")):
            return True
    expiry = offer.get("priceValidUntil") or offer.get("validThrough")
    if expiry:
        try:
            if datetime.fromisoformat(str(expiry).replace("Z", "+00:00")).date() < datetime.now(timezone.utc).date():
                return True
        except ValueError:
            return True
    return False


def retailer_candidates(page, url, retailer, config):
    parser = ProductPage()
    parser.feed(page)
    identity = product_id(url, retailer)
    rejected = Counter()
    found = []
    products = [p for doc in parser.documents for p in products_in(doc)]
    if not products:
        return [], Counter({"donnees_produit_absentes": 1})
    for product in products:
        title = plain_text(product.get("name", "")).strip()
        if not matches(title, config):
            rejected["modele_etat_ou_condition_exclus"] += 1
            continue
        product_url = urllib.parse.urljoin(url, str(product.get("url") or product.get("@id") or ""))
        # A URL or a matching main heading is required to ignore recommendation
        # blocks and prove this price belongs to the product being monitored.
        if product.get("url") or product.get("@id"):
            if product_id(product_url, retailer) != identity:
                rejected["identite_produit_incoherente"] += 1
                continue
        elif normalized(" ".join(parser.headings)) != normalized(title):
            rejected["identite_produit_non_confirmee"] += 1
            continue
        if NON_NEW.search(normalized(product.get("description", ""))) or conditional_price(product.get("description", "")):
            rejected["description_sous_conditions"] += 1
            continue
        offers = list(individual_offers(product.get("offers", [])))
        if not offers:
            rejected["offre_agregee_ou_incomplete"] += 1
        for offer in offers:
            if not isinstance(offer, dict) or schema_type(offer.get("@type")) != "Offer":
                rejected["offre_agregee_ou_incomplete"] += 1
                continue
            if offer_restriction(offer):
                rejected["prix_sous_conditions"] += 1
                continue
            condition = offer.get("itemCondition", product.get("itemCondition"))
            if schema_type(condition) != "NewCondition":
                rejected["etat_neuf_non_confirme"] += 1
                continue
            if schema_type(offer.get("availability")) != "InStock":
                rejected["stock_non_confirme"] += 1
                continue
            price = euro_amount(offer.get("price"))
            if offer.get("priceCurrency") != "EUR" or price is None:
                rejected["prix_eur_non_confirme"] += 1
                continue
            ceiling = price_limit_for_title(title, config)
            if ceiling is None or price > ceiling:
                rejected["au_dessus_du_seuil"] += 1
                continue
            seller = offer.get("seller")
            seller = seller.get("name", "") if isinstance(seller, dict) else seller
            if not isinstance(seller, str) or not seller.strip():
                rejected["vendeur_non_confirme"] += 1
                continue
            offer_url = urllib.parse.urljoin(url, str(offer.get("url") or product_url))
            if product_id(offer_url, retailer) != identity:
                rejected["lien_offre_incoherent"] += 1
                continue
            offer_url = retailer_url(offer_url, retailer, product=True)
            found.append({"key": f"direct:{retailer}:{identity}:{seller}:{offer_url}",
                          "title": title, "price": price, "reference": None, "drop": None,
                          "url": offer_url, "source": retailer, "seller": seller,
                          "condition": "Neuf", "direct": True})
    # Different prices for the very same seller/offer are ambiguous, not a
    # reason to pick whichever number is lowest.
    by_key = {}
    conflicts = set()
    for deal in found:
        if deal["key"] in by_key and by_key[deal["key"]]["price"] != deal["price"]:
            conflicts.add(deal["key"])
        by_key[deal["key"]] = deal
    rejected["prix_contradictoires"] += len(conflicts)
    return [d for k, d in by_key.items() if k not in conflicts], rejected


def scan_retailer(retailer, config, cursor=0):
    source = RETAILERS[retailer]
    deadline = time.monotonic() + 55
    counts = Counter()
    evidence_remaining = 2 if config.get("source_evidence", True) else 0
    queue = list(source["products"])
    queue.extend(config.get("retailer_product_urls", {}).get(retailer, []))
    for url in source["discover"]:
        try:
            page, final_url = fetch_retail_page(url, retailer)
            parser = ProductPage()
            parser.feed(page)
            for href, text in parser.links:
                product_url = retailer_url(urllib.parse.urljoin(final_url, href), retailer, product=True)
                label = text + " " + urllib.parse.unquote(href).replace("-", " ")
                if product_url and target_product(label, config):
                    queue.append(product_url)
            counts["pages_decouverte_lues"] += 1
        except Exception as exc:
            counts["erreurs_acces"] += 1
            status = getattr(exc, "code", None)
            print(f"[{retailer}] Découverte inaccessible ({type(exc).__name__}, HTTP {status}).")
    queue = list(dict.fromkeys(u for u in queue if retailer_url(u, retailer, product=True)))
    if queue:
        offset = cursor % len(queue)
        queue = queue[offset:] + queue[:offset]
    found = []
    checked = 0
    for url in queue[:8]:
        if time.monotonic() >= deadline:
            counts["temps_limite"] += 1
            break
        checked += 1
        try:
            page, final_url = fetch_retail_page(url, retailer)
            if product_id(final_url, retailer) != product_id(url, retailer):
                counts["redirection_non_produit"] += 1
                if evidence_remaining:
                    emit_source_evidence(retailer, final_url, page, status=200, reason="redirection_non_produit")
                    evidence_remaining -= 1
                continue
            deals, reasons = retailer_candidates(page, final_url, retailer, config)
            if evidence_remaining:
                emit_source_evidence(retailer, final_url, page, status=200, reason=dict(reasons))
                evidence_remaining -= 1
            found.extend(deals)
            counts.update(reasons)
            counts["fiches_lues"] += 1
        except Exception as exc:
            counts["erreurs_acces"] += 1
            status = getattr(exc, "code", None)
            print(f"[{retailer}] Fiche inaccessible ({type(exc).__name__}, HTTP {status}).")
            if evidence_remaining:
                body = ""
                if isinstance(exc, urllib.error.HTTPError):
                    try:
                        body = exc.read(100000).decode("utf-8", errors="replace")
                    except OSError:
                        pass
                emit_source_evidence(retailer, url, body, status=status, reason=type(exc).__name__)
                evidence_remaining -= 1
    counts["offres_retenues"] = len(found)
    print(f"[{retailer}] Diagnostic : {json.dumps(dict(counts), ensure_ascii=False)}")
    return found, cursor + checked


def collect_candidates(config, env, state):
    cursors = state.get("_retailer_cursors", {})
    if not isinstance(cursors, dict):
        cursors = {}
    enabled = [s for s in config.get("retailers", list(RETAILERS)) if s in RETAILERS]
    found = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        rss_job = pool.submit(rss_candidates, config, env)
        jobs = [(name, pool.submit(scan_retailer, name, config, int(cursors.get(name, 0)))) for name in enabled]
        for name, job in jobs:
            try:
                deals, cursor = job.result()
                found.extend(deals)
                cursors[name] = cursor
            except Exception as exc:
                print(f"[{name}] Vérification interrompue ({type(exc).__name__}).")
        found.extend(rss_job.result())
    state["_retailer_cursors"] = cursors
    # The free mode makes no paid Keepa API requests. Explicit opt-in only.
    if config.get("enable_keepa", False):
        print("Keepa désactivé en mode strict : état neuf et conditions non vérifiés par ce lecteur.")
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
    if d.get("direct"):
        detail = (f"Prix article : {d['price']:.2f} €\nÉtat déclaré : neuf — En stock\n"
                  f"Vendeur : {d['seller']}\nPrix public, sans avantage différé déduit\n"
                  "Seuil atteint hors livraison ; frais à vérifier")
        label = "🔎 Offre repérée chez un marchand"
    elif d.get("reference"):
        detail = f"Prix actuel : {d['price']:.2f} €\nMoyenne Keepa sur 90 jours : {d['reference']:.2f} €\nÉcart : −{d['drop']:.0f} %"
        label = "🚨 Grosse anomalie de prix à vérifier"
    else:
        detail = f"Prix de l'article déclaré par Dealabs : {d['price']:.2f} €\nSeuil personnalisé atteint (hors livraison)"
        label = "🔎 Offre repérée dans un flux"
    return f"{label}\n\n{d['title']}\n{detail}\nSource : {d['source']}\n{d['url']}\n\nVérifiez le modèle, l'état, le vendeur, les frais de livraison et le prix final avant achat."


def check_once(config, env, state):
    candidates = collect_candidates(config, env, state)
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
    if "--diagnose" in sys.argv:
        candidates = collect_candidates(config, env, {})
        print(f"Diagnostic terminé : {len(candidates)} offre(s). Aucun message Telegram envoyé.")
        return
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
