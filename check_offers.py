"""
Bot sprawdzajacy nowe oferty iPhone (model 11+) na OLX i Vinted
i wysylajacy powiadomienia na Discorda (przez webhook).

Uruchamiany cyklicznie (np. co 10 min) przez GitHub Actions - patrz
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
MAX_SEEN_PER_SOURCE = 500

# Lapiemy iPhone 11 i nowsze (11, 12, 13 ... a takze przyszle 20+, na zapas).
# Warianty typu "iPhone 13 Pro Max" czy "iphone14promax" tez pasuja.
MODEL_PATTERN = re.compile(r"iphone\s*(1[1-9]|[2-9]\d)", re.IGNORECASE)

# Fraza wyszukiwania - szeroka, filtrowanie modelu robimy sami po tytule,
# zeby nie ominac ogloszen z nietypowym zapisem.
SEARCH_QUERY = "iphone"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


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


def matches_model(title):
    return bool(MODEL_PATTERN.search(title or ""))


def fetch_olx():
    """Nowe oferty z OLX pasujace do wzorca modelu."""
    url = "https://www.olx.pl/api/v1/offers/"
    params = {
        "offset": 0,
        "limit": 40,
        "query": SEARCH_QUERY,
        "sort_by": "created_at:desc",
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        print(f"[OLX] Blad pobierania: {e}")
        return []

    results = []
    for item in data:
        title = item.get("title", "")
        if not matches_model(title):
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
            }
        )
    return results


def fetch_vinted():
    """Nowe oferty z Vinted pasujace do wzorca modelu."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        # Vinted wymaga wczesniejszego wejscia na strone glowna, zeby zalozyc
        # sesje/ciasteczka - bez tego API zwraca 401.
        session.get("https://www.vinted.pl/", timeout=20)
        resp = session.get(
            "https://www.vinted.pl/api/v2/catalog/items",
            params={
                "search_text": SEARCH_QUERY,
                "order": "newest_first",
                "per_page": 40,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json().get("items", [])
    except Exception as e:
        print(f"[Vinted] Blad pobierania: {e}")
        return []

    results = []
    for item in data:
        title = item.get("title", "")
        if not matches_model(title):
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
        "description": f"**{price_txt}**\nZrodlo: {offer['source']}",
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
        offers = fetch_fn()
        seen_ids = set(seen.get(source_key, []))
        new_offers = [o for o in offers if o["id"] not in seen_ids]
        print(f"[{source_key}] pobrano {len(offers)}, nowych {len(new_offers)}")
        seen.setdefault(source_key, [])
        for o in new_offers:
            seen[source_key].append(o["id"])
        all_new.extend(new_offers)

    # Wysylamy od najstarszej do najnowszej, zeby kolejnosc na Discordzie
    # byla chronologiczna.
    for offer in reversed(all_new):
        send_discord(offer)

    save_seen(seen)
    print(f"Gotowe. Wyslano {len(all_new)} nowych ofert.")


if __name__ == "__main__":
    main()
