"""
Bot sprawdzajacy nowe oferty (iPhone 11+, PS3, PS4) na OLX i Vinted
i wysylajacy powiadomienia na Discorda (przez webhook).

Uruchamiany cyklicznie przez GitHub Actions - patrz
.github/workflows/check.yml. Stan "juz widzianych" ofert trzymany jest
w pliku seen_ids.json, ktory workflow commituje z powrotem do repo.

Do OLX uzywamy curl_cffi zamiast zwyklego requests - podszywa sie pod
prawdziwa przegladarke na poziomie polaczenia (TLS), co wystarczylo,
zeby ominac blokady antybotowe OLX (403).

Vinted jest chronione dodatkowo przez DataDome/Cloudflare, ktore
sprawdzaja faktyczne zachowanie przegladarki (wykonanie JS) - samo
podszywanie TLS nie wystarczylo (dalej 404). Dlatego do Vinted uzywamy
prawdziwej, headless'owej przegladarki (Playwright + Chromium), ktora
faktycznie renderuje strone wynikow i czyta oferty z DOM.

Do Discorda zostaje zwykly requests - tam takich blokad nie ma.
"""

import os
import re
import json
import time
import random
import requests
from curl_cffi import requests as curl_requests

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Dashboard SmartCore (zakladka "Okazje"). Opcjonalne - bez tych sekretow bot
# dziala jak wczesniej (tylko Discord). Logowanie tokenem uslugi Cloudflare
# Access (Zero Trust -> Access -> Service credentials).
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://smartcore-panel.pages.dev").rstrip("/")
CF_ACCESS_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID")
CF_ACCESS_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET")
SEEN_FILE = "seen_ids.json"
MAX_SEEN_PER_SOURCE = 800

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Wspolne dla wszystkich profili wykluczenia "samych akcesoriow" -
# ogloszenia z tymi slowami w tytule odpadaja, nawet jesli pasuja do
# wzorca modelu (np. "Etui na iPhone 13" zostanie odrzucone).
ACCESSORY_EXCLUDE = (
    r"etui|case|obudow|pokrowiec|szk[łl]o|szyb[ka]|folia|hartowan|"
    r"silikon|\bżel\b|\bzel\b|ładowark|ladowark|\bkabel\b|słuchawk|"
    r"sluchawk|powerbank|power\s*bank|adapter|rysik|smycz|\bpasek\b|"
    r"uchwyt|stacja\s*dok|zasilacz|osłon|oslon|magsafe|portfel|"
    # znane marki/linie produktow ktore w praktyce ZAWSZE oznaczaja
    # akcesorium (etui/szkło), nawet jesli nie ma slowa "etui" w tytule
    r"spigen|tech-?protect|uniq\b|ringke|nillkin|crong|alogy|3mk|"
    r"mercury\b|forcell|wozinsky|puro\b|esr\b|karl\s*lagerfeld|"
    r"guess\b|ferrari\b|ugreen|baseus|joyroom|mobilfox|\buag\b|otterbox|"
    # pudelka, plecki, zestawy ochronne, naklejki
    r"pude[łl]k|\bbox\b|doboz|krabic|plecki|zestaw\s+(do|ochron)|naklejk|"
    r"sticker|\bskin\b|ochraniacz|protector|bumper|\bcover\b|"
    # Vinted pokazuje ogloszenia z calej Europy - etui w innych jezykach:
    # tok/mobiltok (HU), maskica (HR), kryt/obal/pouzdro (CZ/SK),
    # husa (RO), deklas/dekliukas (LT)
    r"(mobil)?tok\b|tokok|maskic|\bkryt\b|\bobal\b|hus[ăa]|d[eė]kl|pouzdr"
)

# --- Konsole: przepuszczamy TYLKO konsole, nie gry ------------------------
# Gier jest za duzo (i w wielu jezykach), zeby je wszystkie wykluczac, wiec
# dzialamy odwrotnie: ogloszenie PS3/PS4 przechodzi, gdy wyraznie chodzi o
# konsole. Kolejnosc w console_ok():
#   1. CONSOLE_STRONG (konsola, slim, 500GB, CUH-xxxx...) -> konsola, nawet z padami/grami
#   2. CONSOLE_CONTROLLER (pad, DualShock, Victrix...)  -> sam pad, odrzucamy
#   3. "pro" albo sam napis "PS4" / "Sony PlayStation 4"  -> konsola
#   4. wszystko inne (tytul gry, kolekcja, "spel", "jocuri") -> odrzucamy
CONSOLE_STRONG = re.compile(
    r"konsol|console|konzol|\bslim\b|\bfat\b|\d+\s?(gb|tb)\b|cuh-?\d|cech-?\d",
    re.IGNORECASE,
)
CONSOLE_CONTROLLER = re.compile(
    r"\bpad\w*|dualshock|dualsense|kontroler|controller|joysti|gamepad|"
    r"victrix|\bpdp\b|kierownic|\bpilot",
    re.IGNORECASE,
)
CONSOLE_WEAK = re.compile(r"\bpro\b", re.IGNORECASE)
_CONSOLE_NAME = re.compile(r"playstation|ps\s?[34]|sony|[^a-ząćęłńóśźż0-9]", re.IGNORECASE)


def console_ok(title):
    if CONSOLE_STRONG.search(title):
        return True
    if CONSOLE_CONTROLLER.search(title):
        return False
    # Po wycieciu "PlayStation 4"/"PS4"/"Sony" i znakow nic nie zostaje = sama konsola.
    return bool(CONSOLE_WEAK.search(title)) or len(_CONSOLE_NAME.sub("", title)) <= 3

# Minimalna cena, zeby ogloszenie trafilo do zakladki "Okazje" w panelu
# (Discord dostaje wszystko jak wczesniej). Ponizej tego to prawie zawsze
# etui, pudelko albo gra.
DASHBOARD_MIN_PRICE = {"iPhone": 300, "PS4": 150, "PS3": 80}

PROFILES = [
    {
        "key": "iphone",
        "label": "iPhone",
        "query": "iphone",
        # Model 11 i nowszy (12,13...19, plus 20+ na przyszlosc). Warianty
        # "iPhone 13 Pro Max" / "iphone14 pro" tez lapie.
        "include": re.compile(r"iphone\s*(1[1-9]|[2-9]\d)", re.IGNORECASE),
        "exclude": re.compile(ACCESSORY_EXCLUDE, re.IGNORECASE),
    },
    {
        "key": "ps3",
        "label": "PS3",
        "query": "ps3",
        "include": re.compile(r"\bps\s?3\b|playstation\s?3\b", re.IGNORECASE),
        "exclude": re.compile(ACCESSORY_EXCLUDE, re.IGNORECASE),
        "console_only": True,
    },
    {
        "key": "ps4",
        "label": "PS4",
        "query": "ps4",
        "include": re.compile(r"\bps\s?4\b|playstation\s?4\b", re.IGNORECASE),
        "exclude": re.compile(ACCESSORY_EXCLUDE, re.IGNORECASE),
        "console_only": True,
    },
]


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"olx": [], "vinted": []}


def save_seen(seen):
    for key in seen:
        seen[key] = seen[key][-MAX_SEEN_PER_SOURCE:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def passes_filters(title, profile):
    title = title or ""
    if not profile["include"].search(title):
        return False
    if profile["exclude"].search(title):
        return False
    if profile.get("console_only") and not console_ok(title):
        return False
    return True


def olx_price(item):
    """
    Cena z ogloszenia OLX. API trzyma ja w params: {"key": "price",
    "value": {"value": 2300, "label": "2 300 zł"}} - nie w polu "price".
    Zwraca liczbe albo None (np. "Zamienię" / "Oddam za darmo").
    """
    for p in item.get("params") or []:
        if p.get("key") != "price":
            continue
        v = p.get("value") or {}
        if isinstance(v.get("value"), (int, float)):
            return v["value"]
        digits = re.sub(r"[^\d,]", "", str(v.get("label") or "")).replace(",", ".")
        try:
            return float(digits) if digits else None
        except ValueError:
            return None
    # Zapas na wypadek zmiany API: stary format {"price": {"value": {...}}}
    price_raw = item.get("price")
    if isinstance(price_raw, dict):
        inner = price_raw.get("value")
        return inner.get("value") if isinstance(inner, dict) else inner
    return None


def fetch_olx(profile):
    """Nowe oferty z OLX (najnowsze pierwsze) pasujace do profilu."""
    url = "https://www.olx.pl/api/v1/offers/"
    params = {
        "offset": 0,
        "limit": 40,
        "query": profile["query"],
        "sort_by": "created_at:desc",
    }
    try:
        resp = curl_requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=20,
            impersonate="chrome124",
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        print(f"[OLX/{profile['key']}] Blad pobierania: {e}")
        return []

    results = []
    for item in data:
        title = item.get("title", "")
        if not passes_filters(title, profile):
            continue

        price_value = olx_price(item)

        photos = item.get("photos") or []
        image = photos[0].get("link") if photos else None
        if image:
            image = image.replace("{width}", "512").replace("{height}", "512")

        results.append(
            {
                "id": f"olx_{item.get('id')}",
                "title": title,
                "price": price_value,
                "url": item.get("url"),
                "image": image,
                "source": "OLX",
                "label": profile["label"],
                "created": item.get("created_time") or "",
            }
        )
    return results


# JS wykonywane w kontekscie strony Vinted - wyciaga liste ogloszen z
# faktycznie wyrenderowanej strony (ustalone na zywo w prawdziwej
# przegladarce, wiec te selektory sa sprawdzone, nie zgadywane).
_VINTED_EXTRACT_JS = """
() => {
    const links = Array.from(document.querySelectorAll('a[data-testid$="--overlay-link"]'));
    return links.map(a => {
        const m = a.getAttribute('data-testid').match(/product-item-id-(\\d+)--overlay-link/);
        const id = m ? m[1] : null;
        const img = id ? document.querySelector(`[data-testid="product-item-id-${id}--image--img"]`) : null;
        return {
            id: id,
            title_attr: a.getAttribute('title') || '',
            href: a.href,
            img: img ? img.src : null,
        };
    });
}
"""


def fetch_vinted_all(profiles):
    """
    Pobiera oferty z Vinted dla WSZYSTKICH profili naraz, uzywajac
    prawdziwej (headless) przegladarki Playwright zamiast zwyklych
    zapytan HTTP.

    Vinted jest chronione przez DataDome/Cloudflare, ktore analizuja
    faktyczne zachowanie przegladarki (wykonanie JS, odcisk
    przegladarki) - zwykle zapytania HTTP (nawet z podszytym TLS przez
    curl_cffi) sa blokowane (403/404). Prawdziwa przegladarka renderuje
    strone normalnie, wiec czytamy dane bezposrednio z DOM zamiast z
    wewnetrznego API.

    Zwraca slownik {profile_key: [oferty...]}.
    """
    from playwright.sync_api import sync_playwright

    out = {p["key"]: [] for p in profiles}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="pl-PL",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()

            for profile in profiles:
                url = (
                    "https://www.vinted.pl/catalog"
                    f"?search_text={profile['query']}&order=newest_first"
                )
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_selector(
                        'a[data-testid$="--overlay-link"]', timeout=15000
                    )
                    items = page.evaluate(_VINTED_EXTRACT_JS)
                except Exception as e:
                    print(f"[Vinted/{profile['key']}] Blad pobierania: {e}")
                    continue

                results = []
                for it in items:
                    title_attr = it.get("title_attr") or ""
                    # Tytul to czesc przed ", Marka:" - reszta to marka/model/
                    # stan/cena doklejone przez Vinted do atrybutu title.
                    title = title_attr.split(", Marka:")[0].strip() or title_attr
                    if not passes_filters(title, profile) and not passes_filters(
                        title_attr, profile
                    ):
                        continue

                    price_match = re.search(r"([\d]+[.,][\d]+)\s*zł", title_attr)
                    price_value = (
                        price_match.group(1).replace(",", ".") if price_match else None
                    )

                    results.append(
                        {
                            "id": f"vinted_{it.get('id')}",
                            "title": title,
                            "price": price_value,
                            "url": it.get("href"),
                            "image": it.get("img"),
                            "source": "Vinted",
                            "label": profile["label"],
                            # Vinted nie daje tu gotowego timestampu - lista
                            # jest juz posortowana najnowsze->najstarsze,
                            # wiec przyblizamy malejacym czasem wzgledem "teraz".
                            "created": str(int(time.time()) - len(results)),
                        }
                    )
                out[profile["key"]] = results
                print(f"[Vinted/{profile['key']}] pobrano surowo {len(items)} kart")

            browser.close()
    except Exception as e:
        print(f"[Vinted] Blad ogolny przegladarki: {e}")

    return out


def send_discord(offer):
    if not DISCORD_WEBHOOK_URL:
        print("Brak DISCORD_WEBHOOK_URL - pomijam wysylke:", offer["title"])
        return

    price_txt = f"{offer['price']} zl" if offer["price"] else "brak ceny w ogloszeniu"
    description = f"**{price_txt}**\n{offer['label']} - {offer['source']}"
    if offer.get("in_dashboard"):
        description += f"\n[📥 W panelu: Okazje]({DASHBOARD_URL}/#okazje)"
    embed = {
        "title": offer["title"][:250],
        "url": offer["url"],
        "description": description,
        "color": 5793266,
    }
    if offer.get("image"):
        embed["thumbnail"] = {"url": offer["image"]}

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        if r.status_code >= 300:
            print(f"Discord error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Blad wysylki do Discorda: {e}")
    time.sleep(1)  # zapas na limit webhookow Discorda


def _to_float(value):
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except ValueError:
        return None


def send_dashboard(offers):
    """
    Wysyla nowe oferty do zakladki "Okazje" w panelu SmartCore (POST /api/deals).
    Do panelu ida tylko oferty od minimalnej ceny (DASHBOARD_MIN_PRICE) -
    telefony i konsole, bez etui/gier za kilkadziesiat zlotych.
    Zwraca zbior id wyslanych ofert (pusty, gdy brak sekretow albo blad).
    """
    def price_ok(o):
        price = _to_float(o.get("price"))
        return price is None or price >= DASHBOARD_MIN_PRICE.get(o.get("label"), 0)

    offers = [o for o in offers if price_ok(o)]
    if not offers:
        return set()
    if not (CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET):
        print("Brak CF_ACCESS_CLIENT_ID/SECRET - pomijam wysylke do panelu")
        return set()
    payload = {
        "deals": [
            {
                "id": o["id"],
                "source": o["source"],
                "label": o["label"],
                "title": o["title"][:300],
                "url": o["url"],
                "image": o.get("image"),
                "price": _to_float(o.get("price")),
            }
            for o in offers
        ]
    }
    headers = {
        "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
        "Content-Type": "application/json",
    }
    try:
        # Paczkami po 50 (API przyjmuje max 200 naraz; mniejsze paczki = szybsza odpowiedz).
        for i in range(0, len(payload["deals"]), 50):
            chunk = {"deals": payload["deals"][i:i + 50]}
            r = requests.post(f"{DASHBOARD_URL}/api/deals", json=chunk, headers=headers, timeout=60, allow_redirects=False)
            if r.status_code >= 300:
                print(f"Panel error {r.status_code}: {r.text[:300]}")
                return set()
        print(f"Panel: zapisano {len(offers)} okazji")
        return {o["id"] for o in offers}
    except Exception as e:
        print(f"Blad wysylki do panelu: {e}")
        return set()


# --- Sprzatanie "Okazji": ogloszenia, ktore zniknely (sprzedane/usuniete) -----

# Sprawdzamy tylko okazje w tych statusach. "dogadane" pomijamy celowo -
# zniknięcie ogloszenia moze oznaczac, ze to wlasnie Ty je kupujesz.
CLEANUP_STATUSES = "nowa,wyslano,negocjuje,uwaga,do_negocjacji"
VINTED_CHECKS_PER_RUN = 12  # strona Vinted wymaga przegladarki - sprawdzamy po troche
GONE_REASON = "Ogłoszenie niedostępne (sprzedane, zarezerwowane lub usunięte) – zdjęte z Okazji automatycznie."


def _dashboard_headers():
    return {
        "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
        "Content-Type": "application/json",
    }


def fetch_open_deals():
    r = requests.get(
        f"{DASHBOARD_URL}/api/deals",
        params={"status": CLEANUP_STATUSES, "limit": 500},
        headers=_dashboard_headers(),
        timeout=20,
        allow_redirects=False,
    )
    if r.status_code != 200:
        print(f"Panel (lista okazji) error {r.status_code}: {r.text[:200]}")
        return []
    return r.json().get("deals", [])


def olx_is_gone(native_id):
    """True = ogloszenia juz nie ma, False = jest, None = nie wiadomo (blad)."""
    try:
        resp = curl_requests.get(
            f"https://www.olx.pl/api/v1/offers/{native_id}/",
            headers=HEADERS,
            timeout=20,
            impersonate="chrome124",
        )
    except Exception as e:
        print(f"[OLX/sprawdzanie {native_id}] blad: {e}")
        return None
    if resp.status_code in (404, 410):
        return True
    if resp.status_code != 200:
        return None
    try:
        status = (resp.json().get("data") or {}).get("status")
    except Exception:
        return None
    return status is not None and status != "active"


def vinted_gone_ids(deals):
    """
    Sprawdza strony przedmiotow Vinted w prawdziwej przegladarce.
    Dostepny przedmiot ma przycisk "Kup teraz" albo "Zaproponuj cene";
    sprzedany/zarezerwowany ich nie ma, usuniety daje 404.
    """
    from playwright.sync_api import sync_playwright

    gone = set()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(
                user_agent=HEADERS["User-Agent"], locale="pl-PL",
                viewport={"width": 1366, "height": 900},
            ).new_page()
            for d in deals:
                url = d.get("url") or f"https://www.vinted.pl/items/{d['id'].split('_', 1)[1]}"
                try:
                    resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    if resp is not None and resp.status in (404, 410):
                        gone.add(d["id"])
                        continue
                    # Czekamy na cene - jesli jej nie ma, strona sie nie zaladowala
                    # (np. blokada) i NIE uznajemy ogloszenia za zniknięte.
                    page.wait_for_selector('[data-testid="item-price"]', timeout=12000)
                    available = page.query_selector(
                        '[data-testid="item-buy-button"], [data-testid="item-buyer-offer-button"]'
                    )
                    if not available:
                        gone.add(d["id"])
                except Exception as e:
                    print(f"[Vinted/sprawdzanie {d['id']}] pomijam: {e}")
            browser.close()
    except Exception as e:
        print(f"[Vinted/sprawdzanie] blad przegladarki: {e}")
    return gone


def cleanup_unavailable():
    """Zdejmuje z Okazji ogloszenia, ktorych juz nie ma na OLX/Vinted."""
    if not (CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET):
        return
    try:
        deals = fetch_open_deals()
        gone = set()
        for d in deals:
            if d["id"].startswith("olx_") and olx_is_gone(d["id"].split("_", 1)[1]):
                gone.add(d["id"])
        vinted = [d for d in deals if d["id"].startswith("vinted_")]
        random.shuffle(vinted)
        gone |= vinted_gone_ids(vinted[:VINTED_CHECKS_PER_RUN])
        if gone:
            body = {"deals": [{"id": i, "status": "zakonczone", "reason": GONE_REASON} for i in gone]}
            r = requests.post(f"{DASHBOARD_URL}/api/deals", json=body, headers=_dashboard_headers(), timeout=60, allow_redirects=False)
            print(f"Panel: zdjeto {len(gone)} niedostepnych okazji (HTTP {r.status_code})")
        else:
            print(f"Panel: sprawdzono {len(deals)} okazji - wszystkie dostepne")
    except Exception as e:
        print(f"Sprzatanie okazji nie powiodlo sie: {e}")


def main():
    seen = load_seen()
    all_new = []

    # OLX - osobne zapytanie HTTP (curl_cffi) na kazdy profil.
    seen_ids = set(seen.get("olx", []))
    seen.setdefault("olx", [])
    for profile in PROFILES:
        offers = fetch_olx(profile)
        new_offers = [o for o in offers if o["id"] not in seen_ids]
        print(f"[olx/{profile['key']}] pobrano {len(offers)}, nowych {len(new_offers)}")
        for o in new_offers:
            seen["olx"].append(o["id"])
            seen_ids.add(o["id"])
        all_new.extend(new_offers)

    # Vinted - jedna przegladarka Playwright obslugujaca wszystkie profile.
    seen_ids = set(seen.get("vinted", []))
    seen.setdefault("vinted", [])
    vinted_by_profile = fetch_vinted_all(PROFILES)
    for profile in PROFILES:
        offers = vinted_by_profile.get(profile["key"], [])
        new_offers = [o for o in offers if o["id"] not in seen_ids]
        print(
            f"[vinted/{profile['key']}] pobrano {len(offers)}, "
            f"nowych {len(new_offers)}"
        )
        for o in new_offers:
            seen["vinted"].append(o["id"])
            seen_ids.add(o["id"])
        all_new.extend(new_offers)

    # Wysylamy od najstarszej do najnowszej, zeby kolejnosc wiadomosci na
    # Discordzie byla chronologiczna (najnowsza oferta na koncu/na dole).
    all_new.sort(key=lambda o: o.get("created") or "")
    # Najpierw do panelu (zeby wiadomosc na Discordzie mogla linkowac do "Okazji").
    sent_ids = send_dashboard(all_new)
    for offer in all_new:
        offer["in_dashboard"] = offer["id"] in sent_ids
    for offer in all_new:
        send_discord(offer)

    save_seen(seen)
    cleanup_unavailable()
    print(f"Gotowe. Wyslano {len(all_new)} nowych ofert.")


if __name__ == "__main__":
    main()
