"""Build the Italian catalog (messages.po + messages.mo) for Finish At.

Extracts every translatable string from the Python, Jinja and JS sources,
checks that each one has an Italian translation below, then writes
``octoprint_finishat/translations/it/LC_MESSAGES/messages.po`` and compiles it
with ``msgfmt``.

    python3 translations_src/build_it.py
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "octoprint_finishat"
OUT = PKG / "translations" / "it" / "LC_MESSAGES"

IT = {
    # planning.py
    "today": "oggi",
    "tomorrow": "domani",
    "{day} at {time}": "{day} alle {time}",
    "Invalid date: {value}": "Data non valida: {value}",
    "Enter the finish time as HH:MM, for example 16:00.": "Scrivi l'ora di fine nel formato HH:MM, per esempio 16:00.",
    "Invalid time: {value}": "Ora non valida: {value}",
    "Couldn't find the estimated print time in this file.": "Non trovo il tempo stimato in questo file.",
    "Too late: the print takes about {duration}. Starting now it would finish {when}.":
        "Troppo tardi: la stampa dura circa {duration}. Partendo adesso finirebbe {when}.",
    # __init__.py
    "File not found on OctoPrint.": "File non trovato su OctoPrint.",
    "Confirm that the bed is clear and clean.": "Conferma che il piatto è libero e pulito.",
    "A print is already scheduled: cancel it first.": "C'è già una stampa pianificata: annullala prima.",
    "Scheduled {file}: starts {start}, finishes {end}": "Pianificata {file}: parte {start}, finisce {end}",
    "The print is already starting: stop it from OctoPrint.": "La stampa sta già partendo: fermala da OctoPrint.",
    "Schedule cancelled: {file}": "Pianificazione annullata: {file}",
    "Unknown command": "Comando sconosciuto",
    "Missed start for {file}: OctoPrint was not running at the planned time ({delay} late). Not starting it.":
        "Partenza mancata per {file}: OctoPrint non era attivo all'ora prevista (ritardo {delay}). Non la avvio.",
    "Not starting {file}: the printer is already printing.": "Non avvio {file}: la stampante sta già stampando.",
    "Not starting {file}: the file is no longer on OctoPrint.": "Non avvio {file}: il file non c'è più su OctoPrint.",
    "Not starting {file}: the printer did not respond within {duration}.":
        "Non avvio {file}: la stampante non risponde dopo {duration}.",
    "Scheduled print started: {file}": "Stampa pianificata partita: {file}",
    "Error while starting {file}: {error}": "Errore durante l'avvio di {file}: {error}",
    # finishat_tab.jinja2
    "Scheduled print": "Stampa pianificata",
    "Starts": "Parte",
    "finishes": "finisce",
    "Starting: powering on the printer and connecting…": "Sta partendo: accendo la stampante e mi connetto…",
    "Cancel schedule": "Annulla pianificazione",
    "Schedule a print": "Pianifica una stampa",
    "Pick the file and the time you want it to finish: I'll work out when to start it.":
        "Scegli il file e l'ora a cui vuoi che finisca: calcolo io quando farla partire.",
    "File": "File",
    "Finish at": "Deve finire alle",
    "if that time has already passed, tomorrow": "se l'ora è già passata, intendo domani",
    "and finishes": "e finisce",
    "Slicer estimate": "Stima dello slicer",
    "with margin and heat-up": "con margine e riscaldamento",
    "The bed is clear and clean and the filament is loaded": "Il piatto è libero e pulito e il filamento è caricato",
    "Schedule": "Pianifica",
    "PSU Control not found: the printer must already be on and connected.":
        "PSU Control non trovato: la stampante deve essere già accesa e connessa.",
    "Last result:": "Ultimo esito:",
    # finishat_settings.jinja2
    "Margin on the slicer estimate": "Margine sulla stima dello slicer",
    "Added to the estimate, so the print finishes a little early rather than late.":
        "Si aggiunge alla stima, così la stampa finisce un po' prima invece che in ritardo.",
    "Heat-up time": "Tempo di riscaldamento",
    "Printer connection timeout": "Attesa massima per la connessione",
    "How long to wait for the printer to connect after powering it on.":
        "Quanto aspettare che la stampante si connetta dopo averla accesa.",
    "Late start tolerance": "Tolleranza sul ritardo",
    "If OctoPrint was down at the planned start and comes back later than this, the print does not start.":
        "Se all'ora di partenza OctoPrint era spento e riparte più tardi di così, la stampa non parte.",
    "Send notifications through the Telegram plugin": "Invia gli avvisi tramite il plugin Telegram",
    "They arrive as the Telegram plugin's \"User Notification\" message, which must be enabled for your chat.":
        "Arrivano come messaggio \"User Notification\" del plugin Telegram, che deve essere attivo per la tua chat.",
    "Times are read in your browser's time zone when you schedule a print.":
        "Quando pianifichi una stampa, gli orari sono letti nel fuso orario del tuo browser.",
    # finishat.js
    "%(day)s at %(time)s": "%(day)s alle %(time)s",
    "Choose a file…": "Scegli un file…",
    "(unknown duration)": "(durata sconosciuta)",
    "(now)": "(adesso)",
    "(in %(h)d h %(m)d min)": "(tra %(h)d h %(m)d min)",
    "(in %(h)d h)": "(tra %(h)d h)",
    "(in %(m)d min)": "(tra %(m)d min)",
    "Couldn't work out the start time.": "Non riesco a calcolare l'ora di partenza.",
    "Scheduling failed.": "Pianificazione non riuscita.",
    "Cancelling failed.": "Annullamento non riuscito.",
}


def python_strings(path: Path) -> set:
    """Literal first arguments of _(...) calls, implicit concatenation included."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def _unescape(s: str) -> str:
    return s.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")


def call_strings(path: Path, func: str) -> set:
    text = path.read_text()
    found = set()
    for q in ("'", '"'):
        pattern = re.escape(func) + r"\(\s*" + q + r"((?:\\.|[^" + q + r"\\])*)" + q + r"\s*\)"
        for m in re.finditer(pattern, text):
            found.add(_unescape(m.group(1)))
    return found


def po_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def main() -> int:
    used = set()
    for py in (PKG / "__init__.py", PKG / "planning.py"):
        used |= python_strings(py)
    for tpl in (PKG / "templates").glob("*.jinja2"):
        used |= call_strings(tpl, "_")
    used |= call_strings(PKG / "static" / "js" / "finishat.js", "gettext")

    missing = sorted(used - IT.keys())
    unused = sorted(IT.keys() - used)
    if missing:
        print("Missing Italian translations:")
        for s in missing:
            print("  ", repr(s))
    if unused:
        print("Translations no longer used:")
        for s in unused:
            print("  ", repr(s))
    if missing:
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Italian translations for OctoPrint-FinishAt.",
        "msgid \"\"",
        "msgstr \"\"",
        "\"Project-Id-Version: OctoPrint-FinishAt\\n\"",
        "\"PO-Revision-Date: 2026-11-22 00:00+0000\\n\"",
        "\"Last-Translator: Ale6699\\n\"",
        "\"Language-Team: Italian\\n\"",
        "\"Language: it\\n\"",
        "\"MIME-Version: 1.0\\n\"",
        "\"Content-Type: text/plain; charset=utf-8\\n\"",
        "\"Content-Transfer-Encoding: 8bit\\n\"",
        "\"Plural-Forms: nplurals=2; plural=(n != 1);\\n\"",
        "",
    ]
    for msgid in sorted(used):
        if "%(" in msgid:
            lines.append("#, python-format")
        lines.append(f'msgid "{po_escape(msgid)}"')
        lines.append(f'msgstr "{po_escape(IT[msgid])}"')
        lines.append("")
    po = OUT / "messages.po"
    po.write_text("\n".join(lines), encoding="utf-8")
    subprocess.run(["msgfmt", "--check", "-o", str(OUT / "messages.mo"), str(po)], check=True)
    print(f"{len(used)} strings written to {po.relative_to(ROOT)} and compiled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
