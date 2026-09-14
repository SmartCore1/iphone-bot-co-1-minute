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
import requests
from curl_cffi import requests as curl_requests

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
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
    r"guess\b|ferrari\b|ugreen|baseus|joyroom"
)

# Dodatkowe wykluczenia typowe dla konsol - same gry/pady/piloty, bez
# samej konsoli, nas nie interesuja. Bez koncowego \b przy pad/kontroler/
# pilot, zeby lapac tez polskie odmiany ("pada", "pady", "padow",
# "kontrolera"). Celowo NIE wykluczamy "gier" (liczba mnoga dopelniacza) -
# to psuloby ogloszenia typu "konsola + 20 gier", ktore SA tym czego
# szukamy; wykluczamy tylko wyrazna sprzedaz samej gry/gier w mianowniku.
CONSOLE_EXTRA_EXCLUDE = r"\bgra\b|\bgry\b|\bpad\w*|kontroler|\bpilot"

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
        "exclude": re.compile(
            ACCESSORY_EXCLUDE + "|" + CONSOLE_EXTRA_EXCLUDE, re.IGNORECASE
        ),
    },
    {
        "key": "ps4",
        "label": "PS4",
        "query": "ps4",
        "include": re.compile(r"\bps\s?4\b|playstation\s?4\b", re.IGNORECASE),
        "exclude": re.compile(
            ACCESSORY_EXCLUDE + "|" + CONSOLE_EXTRA_EXCLUDE, re.IGNORECASE
        ),
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
    return True


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

        price_raw = item.get("price")
        price_value = None
        if isinstance(price_raw, dict):
            inner = price_raw.get("value")
            price_value = inner.get("value") if isinstance(inner, dict) else inner

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
    embed = {
        "title": offer["title"][:250],
        "url": offer["url"],
        "description": f"**{price_txt}**\n{offer['label']} - {offer['source']}",
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
    for offer in all_new:
        send_discord(offer)

    save_seen(seen)
    print(f"Gotowe. Wyslano {len(all_new)} nowych ofert.")


if __name__ == "__main__":
    main()
