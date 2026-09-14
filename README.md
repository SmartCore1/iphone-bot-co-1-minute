# Alert bot (OLX + Vinted -> Discord)

Sprawdza cyklicznie nowe ogloszenia z OLX i Vinted w trzech kategoriach:
**iPhone 11+**, **PS3** i **PS4**, odrzuca ogloszenia z samymi
akcesoriami (etui, szkla, kable, ladowarki, pady, gry itp.) i wysyla
reszte na kanal Discord jako powiadomienie, posortowane od
najstarszej do najnowszej. Dziala w calosci na GitHub Actions - nie
trzeba niczego trzymac wlaczonego.

## Konfiguracja (raz)

1. **Utworz webhook na Discordzie**: Ustawienia kanalu -> Integracje ->
   Webhooki -> Nowy webhook -> skopiuj URL.
2. **Zaloz nowe repozytorium na GitHubie** (moze byc prywatne) i wrzuc
   do niego te pliki (przez `git push` albo przycisk "Add file -> Upload
   files" w interfejsie GitHuba - zachowaj strukture folderow, w tym
   `.github/workflows/check.yml`).
3. **Dodaj sekret**: w repo -> Settings -> Secrets and variables ->
   Actions -> New repository secret -> nazwa `DISCORD_WEBHOOK_URL`,
   wartosc: URL webhooka z kroku 1.
4. **Wlacz Actions**, jesli GitHub o to zapyta (zakladka "Actions" w
   repo), i przy pierwszym razie zatwierdz uruchomienie workflow.
5. **Test**: zakladka Actions -> "Sprawdz oferty iPhone" -> "Run
   workflow" - odpali sprawdzenie od razu, bez czekania na harmonogram.
   Sprawdz logi kroku "Sprawdz nowe oferty..." - powinny pokazac ile
   ofert pobrano i ile bylo nowych.

Od tej pory bot sam sprawdza oferty co 10 minut i wysyla nowe na
Discorda.

## Co warto wiedziec

- **Pierwsze uruchomienie wyslie sporo ofert na raz** (wszystko co
  aktualnie pasuje i jeszcze nie jest w `seen_ids.json`). Kolejne
  uruchomienia beda juz wysylac tylko naprawde nowe ogloszenia.
- **Kategorie/filtry** sa w pliku `check_offers.py` w liscie `PROFILES` -
  kazda pozycja to osobna kategoria (iphone / ps3 / ps4) z wlasnym
  wzorcem "include" (co ma pasowac w tytule) i "exclude" (czego nie
  chcemy - akcesoria, gry, pady). Chcesz dodac kolejna kategorie (np.
  PS5, iPad) - dopisz kolejny slownik do tej listy na wzor istniejacych.
- **Dlaczego filtrowanie po slowach kluczowych, a nie po kategorii OLX
  "elektronika/telefony/iphone"?** Nie mialem z tego srodowiska dostepu
  sieciowego do olx.pl, wiec nie moglem sprawdzic na zywo, jaki
  dokladnie `category_id` odpowiada tej kategorii w wewnetrznym API -
  wpisanie zlego ID dawaloby zero wynikow bez ostrzezenia. Filtrowanie
  po tytule (wzorzec + lista wykluczen) daje ten sam efekt (lapie tylko
  telefony/konsole, odrzuca akcesoria) i dziala niezaleznie od struktury
  kategorii OLX. Jesli chcesz to mimo wszystko zawezic dodatkowo po
  kategorii: w przegladarce wejdz na `olx.pl/elektronika/telefony/iphone/`,
  otworz Narzedzia deweloperskie (F12) -> zakladka Network/Siec -> odswiez
  strone -> znajdz zapytanie do `api/v1/offers` -> w jego parametrach
  bedzie widoczny `category_id` - podeslij mi go, dodam jako dodatkowy
  filtr w kodzie.
- **Bez filtra ceny** - zgodnie z tym, co ustalilismy, bot wysyla
  wszystko niezalezno od kwoty, ty sam oceniasz oferty.
- OLX i Vinted moga w dowolnym momencie zmienic swoje wewnetrzne API
  albo dolozyc zabezpieczenia antybotowe - nie moglem tego przetestowac
  na zywo z tego srodowiska (brak dostepu sieciowego do tych domen), wiec
  jesli po uruchomieniu logi pokaza blad (np. 403/401) albo 0 wynikow,
  daj znac z tresci bledu z logu - dostosujemy naglowki/endpoint.
- **Wazne przy odstepie co 1 minute - limit minut GitHub Actions.**
  Kazde uruchomienie jest liczone jako co najmniej 1 minuta zuzytego
  czasu Actions. Co minute = ok. 1440 uruchomien/dobe = ok. 1440 minut
  zuzycia dziennie.
  - Jesli repo jest **publiczne** - Actions jest bez limitu, zupelnie
    za darmo, niezaleznie od czestotliwosci.
  - Jesli repo jest **prywatne** - darmowy limit to 2000 minut/miesiac.
    Przy odstepie co 1 minute zuzyjesz go w ok. **1,5 dnia**, a potem
    GitHub albo zacznie naliczac oplaty (jesli masz podpieta karte),
    albo wstrzyma workflow.
  - Rozwiazanie: albo ustaw repo jako **publiczne** (kod bota i tak nie
    zawiera nic wrazliwego - webhook jest w sekrecie, niewidocznym
    nawet w publicznym repo), albo zostan przy wiekszym odstepie (co
    2-5 minut), jesli repo ma zostac prywatne.
  - Dodatkowo: GitHub automatycznie wylacza harmonogramowe workflow po
    **60 dniach bez zadnej aktywnosci w repo** (commitow) - jesli kiedys
    przestanie dzialac bez wyraznego powodu, to najczestsza przyczyna,
    wystarczy wtedy recznie odpalic "Run workflow" zeby wznowic.

- Sciagajac dane w ten sposob dzialasz poza oficjalnym API tych
  serwisow (nie ma formalnej zgody w regulaminie) - to typowe podejscie
  dla osobistego uzytku (dokladnie tak dzialaja komercyjne serwisy typu
  Webmon/LZX), ale warto o tym pamietac przy duzej czestotliwosci
  zapytan - 10 minut to bezpieczny, "grzeczny" odstep.
