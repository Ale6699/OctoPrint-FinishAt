"""OctoPrint-Pianifica: avvia una stampa in modo che finisca a un'ora scelta."""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from typing import Optional

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.filemanager import FileDestinations
from octoprint.util import RepeatedTimer

from . import planning

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None


class PianificaPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
):
    TICK_SECONDS = 15

    def __init__(self):
        self._timer: Optional[RepeatedTimer] = None
        self._lock = threading.RLock()
        self._launching = False
        self._estimate_cache: dict = {}

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_settings_defaults(self):
        return {
            "timezone": "Europe/Rome",
            "margin_percent": 5,  # scarto misurato sulla stima di PrusaSlicer
            "heatup_minutes": 4,  # riscaldamento ugello e piatto
            "connect_timeout_seconds": 180,
            "late_tolerance_minutes": 10,  # oltre questo ritardo non parte più
            "telegram": True,  # avvisi via plugin Telegram, se installato
            # Salvati come stringhe JSON: un dict sotto un default None fa
            # protestare il sistema di impostazioni di OctoPrint.
            "job_json": "",
            "last_json": "",
        }

    def _tz(self):
        name = self._settings.get(["timezone"]) or "Europe/Rome"
        if ZoneInfo is None:
            return dt.timezone.utc
        try:
            return ZoneInfo(name)
        except Exception:
            self._logger.warning("Fuso orario %s non valido, uso Europe/Rome", name)
            return ZoneInfo("Europe/Rome")

    def _now(self) -> dt.datetime:
        return dt.datetime.now(self._tz())

    def _load_json(self, key: str) -> Optional[dict]:
        raw = self._settings.get([key])
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            self._logger.warning("Valore non valido in %s, lo ignoro", key)
            return None
        return value if isinstance(value, dict) else None

    def _save_json(self, key: str, value: Optional[dict]):
        self._settings.set([key], json.dumps(value) if value else "")
        self._settings.save()

    def _get_job(self) -> Optional[dict]:
        return self._load_json("job_json")

    def _set_job(self, job: Optional[dict]):
        self._save_json("job_json", job)

    def _get_last(self) -> Optional[dict]:
        return self._load_json("last_json")

    def _set_last(self, status: str, message: str):
        self._save_json("last_json", {"status": status, "message": message, "ts": time.time()})

    def _drop_old_settings(self):
        """Toglie le chiavi "job"/"last" della versione 0.1.0 (dict sotto default None)."""
        changed = False
        for key in ("job", "last"):
            try:
                if self._settings.get([key]) is not None:
                    self._settings.remove([key])
                    changed = True
            except Exception:
                pass
        if changed:
            self._settings.save()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_after_startup(self):
        self._drop_old_settings()
        job = self._get_job()
        if job:
            self._logger.info(
                "Pianificazione in attesa: %s, partenza %s",
                job.get("path"),
                dt.datetime.fromtimestamp(job["start_ts"], self._tz()).isoformat(),
            )
        self._timer = RepeatedTimer(self.TICK_SECONDS, self._tick, daemon=True)
        self._timer.start()

    def on_shutdown(self):
        if self._timer:
            self._timer.cancel()

    # ------------------------------------------------------------------
    # Assets / templates
    # ------------------------------------------------------------------

    def get_assets(self):
        return {"js": ["js/pianifica.js"], "css": ["css/pianifica.css"]}

    def get_template_configs(self):
        return [{"type": "tab", "name": "Pianifica", "custom_bindings": True}]

    def is_template_autoescaped(self):
        return True

    # ------------------------------------------------------------------
    # Files and estimates
    # ------------------------------------------------------------------

    def _list_gcode_files(self) -> list:
        result = []
        try:
            tree = self._file_manager.list_files(FileDestinations.LOCAL, recursive=True)
        except TypeError:
            tree = self._file_manager.list_files(FileDestinations.LOCAL)
        tree = tree.get(FileDestinations.LOCAL, tree)

        def walk(entries):
            for entry in entries.values():
                if entry.get("type") == "folder":
                    walk(entry.get("children", {}))
                elif entry.get("type") == "machinecode":
                    path = entry.get("path") or entry.get("name")
                    est = self._estimate(path)
                    result.append(
                        {
                            "path": path,
                            "name": entry.get("display") or entry.get("name") or path,
                            "date": entry.get("date"),
                            "estimate_s": est,
                            "estimate_label": planning.format_duration(est) if est else None,
                        }
                    )

        walk(tree)
        result.sort(key=lambda f: f.get("date") or 0, reverse=True)
        return result

    def _estimate(self, path: str) -> Optional[int]:
        """Stima dello slicer; se manca, quella dell'analisi di OctoPrint."""
        try:
            disk = self._file_manager.path_on_disk(FileDestinations.LOCAL, path)
            mtime = os.path.getmtime(disk)
        except Exception:
            return None
        key = (path, mtime)
        if key in self._estimate_cache:
            return self._estimate_cache[key]
        est = None
        try:
            est = planning.estimate_from_file(disk)
        except Exception:
            self._logger.exception("Impossibile leggere la stima da %s", path)
        if not est:
            try:
                meta = self._file_manager.get_metadata(FileDestinations.LOCAL, path) or {}
                est = meta.get("analysis", {}).get("estimatedPrintTime")
                est = int(est) if est else None
            except Exception:
                est = None
        self._estimate_cache[key] = est
        return est

    def _plan(self, path: str, end_value: str) -> planning.Plan:
        if not path or not self._file_manager.file_exists(FileDestinations.LOCAL, path):
            raise planning.PlanError("File non trovato su OctoPrint.")
        now = self._now()
        end = planning.parse_end_time(end_value, now)
        return planning.make_plan(
            self._estimate(path),
            end,
            now,
            margin_percent=float(self._settings.get(["margin_percent"])),
            heatup_minutes=float(self._settings.get(["heatup_minutes"])),
        )

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {
            "preview": ["path", "end"],
            "schedule": ["path", "end", "bed_clear"],
            "cancel": [],
        }

    def on_api_get(self, request):
        if not Permissions.PRINT.can():
            flask.abort(403)
        return flask.jsonify(self._state())

    def on_api_command(self, command, data):
        if not Permissions.PRINT.can():
            flask.abort(403)

        if command == "preview":
            try:
                plan = self._plan(data.get("path"), data.get("end"))
            except planning.PlanError as e:
                return flask.jsonify(error=str(e))
            return flask.jsonify(plan.to_dict(self._now()))

        if command == "schedule":
            if data.get("bed_clear") is not True:
                return flask.make_response(
                    flask.jsonify(error="Conferma che il piatto è libero e pulito."), 400
                )
            with self._lock:
                if self._get_job():
                    return flask.make_response(
                        flask.jsonify(error="C'è già una stampa pianificata: annullala prima."),
                        409,
                    )
                try:
                    plan = self._plan(data.get("path"), data.get("end"))
                except planning.PlanError as e:
                    return flask.make_response(flask.jsonify(error=str(e)), 400)
                job = {
                    "path": data["path"],
                    "start_ts": plan.start.timestamp(),
                    "end_ts": plan.end.timestamp(),
                    "estimate_s": plan.estimate_s,
                    "planned_s": plan.planned_s,
                    "created_ts": time.time(),
                    "user": self._current_user_name(),
                }
                self._set_job(job)
            now = self._now()
            msg = (
                f"Pianificata {job['path']}: parte {planning.day_label(plan.start, now)}, "
                f"finisce {planning.day_label(plan.end, now)}"
            )
            self._logger.info(msg)
            self._notify(msg, "success", telegram=True)
            return flask.jsonify(self._state())

        if command == "cancel":
            with self._lock:
                job = self._get_job()
                if not job:
                    return flask.jsonify(self._state())
                if self._launching:
                    return flask.make_response(
                        flask.jsonify(error="La stampa sta già partendo: fermala da OctoPrint."),
                        409,
                    )
                self._set_job(None)
            msg = f"Pianificazione annullata: {job['path']}"
            self._set_last("cancelled", msg)
            self._logger.info(msg)
            self._notify(msg, "info", telegram=True)
            return flask.jsonify(self._state())

        return flask.make_response(flask.jsonify(error="Comando sconosciuto"), 400)

    def _state(self) -> dict:
        now = self._now()
        job = self._get_job()
        if job:
            job = dict(job)
            job["start_label"] = planning.day_label(
                dt.datetime.fromtimestamp(job["start_ts"], now.tzinfo), now
            )
            job["end_label"] = planning.day_label(
                dt.datetime.fromtimestamp(job["end_ts"], now.tzinfo), now
            )
            job["launching"] = self._launching
        return {
            "files": self._list_gcode_files(),
            "job": job,
            "last": self._get_last(),
            "now_ts": now.timestamp(),
            "timezone": str(now.tzinfo),
            "psucontrol": self._psu() is not None,
        }

    @staticmethod
    def _current_user_name() -> Optional[str]:
        try:
            from flask_login import current_user

            return current_user.get_name()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def _tick(self):
        with self._lock:
            job = self._get_job()
            if not job or self._launching:
                return
            now = time.time()
            if now < job["start_ts"]:
                return
            late = now - job["start_ts"]
            tolerance = float(self._settings.get(["late_tolerance_minutes"])) * 60
            if late > tolerance:
                self._set_job(None)
                self._fail(
                    f"Partenza mancata per {job['path']}: OctoPrint non era attivo all'ora "
                    f"prevista (ritardo {planning.format_duration(late)}). Non la avvio."
                )
                return
            self._launching = True
        threading.Thread(target=self._launch, args=(job,), daemon=True).start()

    def _psu(self) -> Optional[dict]:
        try:
            return self._plugin_manager.get_helpers(
                "psucontrol", "get_psu_state", "turn_psu_on"
            )
        except Exception:
            return None

    def _launch(self, job: dict):
        path = job["path"]
        try:
            self._logger.info("Avvio pianificato di %s", path)
            if self._printer.is_printing() or self._printer.is_paused():
                self._fail(f"Non avvio {path}: la stampante sta già stampando.")
                return
            if not self._file_manager.file_exists(FileDestinations.LOCAL, path):
                self._fail(f"Non avvio {path}: il file non c'è più su OctoPrint.")
                return

            psu = self._psu()
            if psu and not psu["get_psu_state"]():
                self._logger.info("Accendo la stampante con PSU Control")
                psu["turn_psu_on"]()

            timeout = float(self._settings.get(["connect_timeout_seconds"]))
            deadline = time.time() + timeout
            tried_connect_at = None
            while time.time() < deadline:
                if self._printer.is_ready():
                    break
                if self._printer.is_printing() or self._printer.is_paused():
                    self._fail(f"Non avvio {path}: la stampante sta già stampando.")
                    return
                if self._printer.is_closed_or_error() and (
                    tried_connect_at is None or time.time() - tried_connect_at > 60
                ):
                    self._logger.info("Stampante non connessa, provo a connettermi")
                    tried_connect_at = time.time()
                    self._printer.connect()
                time.sleep(2)
            else:
                self._fail(
                    f"Non avvio {path}: la stampante non risponde dopo "
                    f"{planning.format_duration(timeout)}."
                )
                return

            self._printer.select_file(path, False, printAfterSelect=True, user=job.get("user"))
            msg = f"Stampa pianificata partita: {path}"
            self._logger.info(msg)
            self._set_last("started", msg)
            self._notify(msg, "success", telegram=False)  # Telegram avvisa già all'avvio
        except Exception as e:
            self._logger.exception("Errore durante l'avvio pianificato")
            self._fail(f"Errore durante l'avvio di {path}: {e}")
        finally:
            with self._lock:
                self._launching = False
                self._set_job(None)

    def _fail(self, message: str):
        self._logger.warning(message)
        self._set_last("failed", message)
        self._notify(message, "error", telegram=True)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _notify(self, message: str, level: str, telegram: bool):
        try:
            self._plugin_manager.send_plugin_message(
                self._identifier, {"message": message, "level": level}
            )
        except Exception:
            self._logger.exception("Invio del messaggio all'interfaccia fallito")
        if telegram and self._settings.get_boolean(["telegram"]):
            self._telegram(message)

    def _telegram(self, message: str):
        """Usa la notifica "UserNotif" del plugin Telegram, se installato."""
        try:
            info = self._plugin_manager.get_plugin_info("telegram")
            if info and info.enabled and info.implementation:
                info.implementation.on_event("UserNotif", {"UserNotif": f"[Pianifica] {message}"})
        except Exception:
            self._logger.exception("Invio a Telegram fallito")


__plugin_name__ = "Pianifica"
__plugin_pythoncompat__ = ">=3.8,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PianificaPlugin()


__plugin_hooks__ = {}
