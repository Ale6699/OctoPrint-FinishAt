# OctoPrint-Pianifica

Plugin per [OctoPrint](https://octoprint.org) che avvia una stampa **in modo che finisca all'ora che scegli**.

Scegli il file, scrivi per esempio "16:00", e il plugin calcola quando farla partire:

- legge la **stima dello slicer** dal G-code (PrusaSlicer, Cura); se manca usa l'analisi di OctoPrint;
- aggiunge un **margine** (5% di default) e il tempo di **riscaldamento** (4 minuti di default);
- all'ora di partenza **accende la stampante** con [PSU Control](https://plugins.octoprint.org/plugins/psucontrol/), se installato, aspetta che si connetta e **avvia la stampa**.

## Sicurezza

- Per pianificare devi confermare che **il piatto è libero e pulito**.
- Se all'ora di partenza la stampante sta già stampando, non risponde o il file non c'è più, **non parte** e lo segnala.
- Se OctoPrint era spento all'ora prevista e riparte più di 10 minuti dopo, la stampa **non parte** in ritardo.
- La pianificazione è salvata nelle impostazioni e sopravvive ai riavvii.

## Avvisi

Gli esiti compaiono nell'interfaccia di OctoPrint. Se è installato il plugin
[Telegram](https://github.com/jacopotediosi/OctoPrint-Telegram), arrivano anche lì, tramite la notifica
"User Notification": deve essere attiva per la tua chat.

## Impostazioni

Per ora si cambiano in `config.yaml`, sotto `plugins.pianifica`:

| Chiave | Default | Significato |
|---|---|---|
| `timezone` | `Europe/Rome` | Fuso orario per interpretare l'ora di fine |
| `margin_percent` | `5` | Margine aggiunto alla stima dello slicer |
| `heatup_minutes` | `4` | Minuti di riscaldamento |
| `connect_timeout_seconds` | `180` | Attesa massima per la connessione alla stampante |
| `late_tolerance_minutes` | `10` | Oltre questo ritardo la stampa non parte |
| `telegram` | `true` | Avvisi via plugin Telegram |

## Installazione

Da OctoPrint: Impostazioni → Plugin Manager → Get More → "...from URL", con l'archivio zip del repository.

Con pip, nello stesso ambiente di OctoPrint:

```bash
pip install https://github.com/<utente>/OctoPrint-Pianifica/archive/main.zip
```

## Sviluppo

I calcoli degli orari sono in `octoprint_pianifica/planning.py` e si provano senza OctoPrint:

```bash
python3 -m unittest discover -s tests
```

## Licenza

AGPL-3.0-or-later, come OctoPrint.
