# LuxmedSniper-web

Fork [pawliczka/LuxmedSniper](https://github.com/pawliczka/LuxmedSniper) przerobiony
na webową usługę do samodzielnego hostowania (Proxmox LXC): FastAPI + HTMX GUI,
powiadomienia push na iOS (Pushover domyślnie lub self-hosted ntfy), hasło Luxmed
szyfrowane w aplikacji (Fernet + master key trzymany tylko w RAM).

```
[iPhone: apka Pushover] ◄──push──── [Pushover / ntfy]
                                          ▲
[przeglądarka w LAN] ──HTTP──► [LXC: sniper-app] ──┘
                               FastAPI + HTMX
                               APScheduler (polling Luxmed)
                               SQLite (monitory, stan, szyfrogram hasła)
```

## Funkcje

- **GUI w przeglądarce** — monitory wyklikujesz z kaskadowych dropdownów
  (miasto → usługa → placówka/lekarz) zamiast wpisywać ID typu `1*7409*-1*-1`
- **Push na iOS** — domyślnie [Pushover](https://pushover.net) (jednorazowo ~$5,
  działa wszędzie od ręki); alternatywnie self-hosted [ntfy](https://ntfy.sh)
  z relayem APNs, gdy chcesz trzymać treść u siebie. Dostawcę wybierasz w GUI
- **Sekrety**: hasło Luxmed w bazie tylko jako szyfrogram Fernet; klucz
  wyprowadzany z master key (scrypt) i trzymany wyłącznie w pamięci — po
  restarcie odblokowujesz w GUI albo przez plik klucza z hosta Proxmoxa
- **Ochrona przed banem Luxmedu**: minimalny interwał (domyślnie 30 min, dolna
  granica 5 min), losowy jitter, dzienny budżet zapytań jako realny ogranicznik
  tempa, auto-pauza po nieudanych logowaniach
- Historia znalezionych terminów + deduplikacja powiadomień w SQLite

## Szybki start (lokalnie)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```

GUI: <http://localhost:8080>. Przy pierwszym uruchomieniu ustawiasz hasło GUI
i master key, potem w **Ustawieniach** podajesz dane Luxmed (zapis = szyfrowanie)
oraz User Key + API Token z [pushover.net](https://pushover.net).

Konfiguracja przez zmienne środowiskowe (wszystkie opcjonalne):

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `SNIPER_DATA_DIR` | `./data` | katalog z bazą SQLite |
| `SNIPER_HOST` / `SNIPER_PORT` | `0.0.0.0` / `8080` | adres serwera |
| `SNIPER_MASTER_KEY_FILE` | — | plik z master key → auto-unlock po starcie |

## Deployment na Proxmoxie

Kompletna instrukcja (LXC unprivileged, systemd, ntfy z `upstream-base-url`,
bind-mount klucza z hosta, backupy): **[deploy/PROXMOX.md](deploy/PROXMOX.md)**.

## Stary interfejs CLI

Oryginalny skrypt `luxmed_sniper.py` (YAML + pushover/telegram/…) nadal działa —
patrz historia README w repo upstream.

# Warning

Please be advised that running too many queries against LuxMed API may result in
locking your LuxMed account. Breaching the 'fair use policy' for the first time
locks the account temporarily for 1 day. Breaching it again locks it indefinitely
and manual intervention with "Patient Portal Support" is required to unlock it.

Aplikacja ma wbudowane limity (interwał regulowalny od 5 min, dzienny budżet
zapytań, auto-pauzę po błędach logowania). Im krótszy interwał, tym większe
ryzyko blokady konta — 30 min to bezpieczny default.
