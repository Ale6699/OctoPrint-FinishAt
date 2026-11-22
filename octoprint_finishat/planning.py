"""Pure scheduling maths, with no OctoPrint dependency.

Kept apart from ``__init__.py`` so it can be tested without OctoPrint.
Functions that produce user-facing text take a ``_`` translator
(``gettext``-style); it defaults to returning the English text unchanged.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Callable, Optional

Translator = Callable[[str], str]


def _identity(text: str) -> str:
    return text


# PrusaSlicer:  "; estimated printing time (normal mode) = 1h 2m 3s"
# Others:       "; estimated printing time = 1d 2h 3m 4s"
_ESTIMATE_RE = re.compile(
    r"^;\s*estimated printing time(?: \(normal mode\))?\s*=\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Cura: ";TIME:3723"
_CURA_TIME_RE = re.compile(r"^;TIME:(\d+)\s*$", re.MULTILINE)

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> Optional[int]:
    """Turn "1d 2h 3m 4s" (or any part of it) into seconds."""
    parts = re.findall(r"(\d+)\s*([dhms])\b", text.lower())
    if not parts:
        return None
    return sum(int(n) * _UNITS[u] for n, u in parts)


def estimate_from_gcode_text(text: str) -> Optional[int]:
    """Find the slicer's print time estimate in G-code text, in seconds."""
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
    """Read only the start and end of the file: the estimate is in one of them."""
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


def format_duration(secs: float) -> str:
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{max(m, 1)} min"


def day_label(when: dt.datetime, now: dt.datetime, _: Translator = _identity) -> str:
    delta = (when.date() - now.date()).days
    if delta == 0:
        day = _("today")
    elif delta == 1:
        day = _("tomorrow")
    else:
        day = when.strftime("%d/%m")
    return _("{day} at {time}").format(day=day, time=when.strftime("%H:%M"))


class PlanError(ValueError):
    """An error whose message is shown to the user as is."""


@dataclass
class Plan:
    start: dt.datetime
    end: dt.datetime
    estimate_s: int
    planned_s: int

    def to_dict(self) -> dict:
        return {
            "start_ts": self.start.timestamp(),
            "end_ts": self.end.timestamp(),
            "estimate_s": self.estimate_s,
            "estimate_label": format_duration(self.estimate_s),
            "planned_s": self.planned_s,
            "planned_label": format_duration(self.planned_s),
        }


def parse_end_time(value: str, now: dt.datetime, _: Translator = _identity) -> dt.datetime:
    """"16:00" -> next occurrence (today if still ahead, otherwise tomorrow).

    Also accepts "YYYY-MM-DDTHH:MM" for a specific date.
    """
    value = (value or "").strip()
    tz = now.tzinfo
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})", value)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return dt.datetime(y, mo, d, h, mi, tzinfo=tz)
        except ValueError as e:
            raise PlanError(_("Invalid date: {value}").format(value=value)) from e
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value)
    if not m:
        raise PlanError(_("Enter the finish time as HH:MM, for example 16:00."))
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise PlanError(_("Invalid time: {value}").format(value=value))
    end = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if end <= now:
        end += dt.timedelta(days=1)
    return end


def make_plan(
    estimate_s: Optional[int],
    end: dt.datetime,
    now: dt.datetime,
    margin_percent: float = 5.0,
    heatup_minutes: float = 4.0,
    min_lead_seconds: int = 60,
    _: Translator = _identity,
) -> Plan:
    """Work out when to start so that the print finishes at ``end``."""
    if not estimate_s or estimate_s <= 0:
        raise PlanError(_("Couldn't find the estimated print time in this file."))
    planned = int(round(estimate_s * (1 + margin_percent / 100.0) + heatup_minutes * 60))
    # Work in UTC: subtracting in local time is "wall clock" arithmetic and
    # would be an hour off on the nights the clocks change.
    start = (end.astimezone(dt.timezone.utc) - dt.timedelta(seconds=planned)).astimezone(end.tzinfo)
    if start < now + dt.timedelta(seconds=min_lead_seconds):
        earliest = now + dt.timedelta(seconds=planned + min_lead_seconds)
        raise PlanError(
            _("Too late: the print takes about {duration}. Starting now it would finish {when}.").format(
                duration=format_duration(planned), when=day_label(earliest, now, _)
            )
        )
    return Plan(start=start, end=end, estimate_s=int(estimate_s), planned_s=planned)
