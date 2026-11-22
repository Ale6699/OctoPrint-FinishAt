"""Calcoli puri per la pianificazione: nessuna dipendenza da OctoPrint.

Tenuti separati da ``__init__.py`` così si possono provare senza OctoPrint.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Optional

# PrusaSlicer:  "; estimated printing time (normal mode) = 1h 2m 3s"
# Cura/altri:   "; estimated printing time = 1d 2h 3m 4s"
_ESTIMATE_RE = re.compile(
    r"^;\s*estimated printing time(?: \(normal mode\))?\s*=\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Cura: ";TIME:3723"
_CURA_TIME_RE = re.compile(r"^;TIME:(\d+)\s*$", re.MULTILINE)

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> Optional[int]:
    """Converte "1d 2h 3m 4s" (anche parziale) in secondi."""
    parts = re.findall(r"(\d+)\s*([dhms])\b", text.lower())
    if not parts:
        return None
    return sum(int(n) * _UNITS[u] for n, u in parts)


def estimate_from_gcode_text(text: str) -> Optional[int]:
    """Cerca nel testo del G-code la stima dello slicer, in secondi."""
    m = _ESTIMATE_RE.search(text)
    if m:
        secs = parse_duration(m.group(1))
        if secs:
            return secs
    m = _CURA_TIME_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def estimate_from_file(path: str, chunk: int = 256 * 1024) -> Optional[int]:
    """Legge solo inizio e fine del file: la stima sta in uno dei due."""
    with open(path, "rb") as f:
        head = f.read(chunk)
        f.seek(0, 2)
        size = f.tell()
        tail = b""
        if size > chunk:
            f.seek(max(size - chunk, 0))
            tail = f.read()
    for blob in (tail, head):
        secs = estimate_from_gcode_text(blob.decode("utf-8", errors="replace"))
        if secs:
            return secs
    return None


def format_duration(secs: int) -> str:
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{max(m, 1)} min"


def day_label(when: dt.datetime, now: dt.datetime) -> str:
    delta = (when.date() - now.date()).days
    if delta == 0:
        day = "oggi"
    elif delta == 1:
        day = "domani"
    else:
        day = when.strftime("%d/%m")
    return f"{day} alle {when.strftime('%H:%M')}"


class PlanError(ValueError):
    """Errore da mostrare all'utente così com'è."""


@dataclass
class Plan:
    start: dt.datetime
    end: dt.datetime
    estimate_s: int
    planned_s: int

    def to_dict(self, now: dt.datetime) -> dict:
        return {
            "start_ts": self.start.timestamp(),
            "end_ts": self.end.timestamp(),
            "start_label": day_label(self.start, now),
            "end_label": day_label(self.end, now),
            "estimate_s": self.estimate_s,
            "estimate_label": format_duration(self.estimate_s),
            "planned_s": self.planned_s,
            "planned_label": format_duration(self.planned_s),
        }


def parse_end_time(value: str, now: dt.datetime) -> dt.datetime:
    """"16:00" -> prossima occorrenza (oggi se non è passata, altrimenti domani).

    Accetta anche "YYYY-MM-DDTHH:MM" per una data precisa.
    """
    value = (value or "").strip()
    tz = now.tzinfo
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})", value)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return dt.datetime(y, mo, d, h, mi, tzinfo=tz)
        except ValueError as e:
            raise PlanError(f"Data non valida: {value}") from e
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value)
    if not m:
        raise PlanError("Scrivi l'ora di fine nel formato HH:MM, per esempio 16:00.")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise PlanError(f"Ora non valida: {value}")
    end = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if end <= now:
        end += dt.timedelta(days=1)
    return end


def make_plan(
    estimate_s: int,
    end: dt.datetime,
    now: dt.datetime,
    margin_percent: float = 5.0,
    heatup_minutes: float = 4.0,
    min_lead_seconds: int = 60,
) -> Plan:
    """Calcola l'ora di partenza per finire a ``end``."""
    if not estimate_s or estimate_s <= 0:
        raise PlanError("Non trovo il tempo stimato in questo file.")
    planned = int(round(estimate_s * (1 + margin_percent / 100.0) + heatup_minutes * 60))
    # Calcolo in UTC: con il fuso locale la sottrazione sarebbe "da orologio"
    # e sbaglierebbe di un'ora nelle notti del cambio ora.
    start = (end.astimezone(dt.timezone.utc) - dt.timedelta(seconds=planned)).astimezone(end.tzinfo)
    if start < now + dt.timedelta(seconds=min_lead_seconds):
        earliest = now + dt.timedelta(seconds=planned + min_lead_seconds)
        raise PlanError(
            f"Troppo tardi: la stampa dura circa {format_duration(planned)}. "
            f"Partendo adesso finirebbe {day_label(earliest, now)}."
        )
    return Plan(start=start, end=end, estimate_s=int(estimate_s), planned_s=planned)
