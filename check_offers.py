"""
Bot sprawdzajacy nowe oferty (iPhone 11+, PS3, PS4) na OLX i Vinted
i wysylajacy powiadomienia na Discorda (przez webhook).

Uruchamiany cyklicznie przez GitHub Actions - patrz
.github/workflows/check.yml. Stan "juz widzianych" ofert trzymany jest
w pliku seen_ids.json, ktory workflow commituje z powrotem do repo.
"""

import os
import re
import json
import time
import requests

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
SEEN_FILE = "seen_ids.json"
MAX_SEEN_PER_SOURCE = 800

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Wspolne dla wszystkich profili wykluczenia "samych akcesoriow" -
# ogloszenia z tymi slowami w tytule odpadaja, nawet jesli pasuja do
# wzorca modelu (np. "Etui na iPhone 13" zostanie odrzucone).
ACCESSORY_EXCLUDE = (
    r"etui|case|obudow|pokrowiec|szk[łl]o|szyb[ka]|folia|hartowan|"
    r"silikon|\bżel\b|\bzel\b|ładowark|ladowark|\bkabel\b|słuchawk|"
    r"sluchawk|powerbank|power\s*bank|adapter|rysik|smycz|\bpasek\b|"
    r"uchwyt|stacja\s*dok|zasilacz"
)

# Dodatkowe wykluczenia typowe dla konsol - same gry/pady/piloty, bez
# samej konsoli, nas nie interesuja.
CONSOLE_EXTRA_EXCLUDE = r"\bgra\b|\bgry\b|\bpad\b|pady\b|kontroler|\bpilot\b"

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
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
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


def fetch_vinted(profile):
    """Nowe oferty z Vinted (najnowsze pierwsze) pasujace do profilu."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        # Vinted wymaga wczesniejszego wejscia na strone glowna, zeby zalozyc
        # sesje/ciasteczka - bez tego API zwraca 401.
        session.get("https://www.vinted.pl/", timeout=20)
        resp = session.get(
            "https://www.vinted.pl/api/v2/catalog/items",
            params={
                "search_text": profile["query"],
                "order": "newest_first",
                "per_page": 40,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json().get("items", [])
    except Exception as e:
        print(f"[Vinted/{profile['key']}] Blad pobierania: {e}")
        return []

    results = []
    for item in data:
        title = item.get("title", "")
        if not passes_filters(title, profile):
            continue

        price_obj = item.get("price") or item.get("total_item_price") or {}
        price_value = price_obj.get("amount") if isinstance(price_obj, dict) else None

        photo = item.get("photo") or {}
        image = photo.get("url") if isinstance(photo, dict) else None

        results.append(
            {
                "id": f"vinted_{item.get('id')}",
                "title": title,
                "price": price_value,
                "url": item.get("url"),
                "image": image,
                "source": "Vinted",
                "label": profile["label"],
                "created": str(item.get("created_at_ts") or ""),
            }
        )
    return results


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

    for source_key, fetch_fn in (("olx", fetch_olx), ("vinted", fetch_vinted)):
        seen_ids = set(seen.get(source_key, []))
        seen.setdefault(source_key, [])

        for profile in PROFILES:
            offers = fetch_fn(profile)
            new_offers = [o for o in offers if o["id"] not in seen_ids]
            print(
                f"[{source_key}/{profile['key']}] pobrano {len(offers)}, "
                f"nowych {len(new_offers)}"
            )
            for o in new_offers:
                seen[source_key].append(o["id"])
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
