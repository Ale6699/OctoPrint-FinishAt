"""OctoPrint-FinishAt: start a print so that it finishes at the time you choose."""

from __future__ import annotations

import datetime as dt
import gettext
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
    try:
        from backports.zoneinfo import ZoneInfo  # installed as a dependency on 3.7/3.8
    except ImportError:
        ZoneInfo = None

_TRANSLATIONS_DIR = os.path.join(os.path.dirname(__file__), "translations")


def _normalize_lang(lang: Optional[str]) -> str:
    """"it-IT" / "it_IT" / "it" -> "it"; anything empty -> "en"."""
    if not lang:
        return "en"
    return lang.replace("-", "_").split("_")[0].lower() or "en"


class FinishAtPlugin(
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
        self._translators: dict = {}

    # ------------------------------------------------------------------
    # Translations (also used outside requests, e.g. for Telegram)
    # ------------------------------------------------------------------

    def _translator(self, lang: Optional[str]):
        lang = _normalize_lang(lang)
        if lang not in self._translators:
            t = gettext.translation(
                "messages", localedir=_TRANSLATIONS_DIR, languages=[lang], fallback=True
            )
            self._translators[lang] = t.gettext
        return self._translators[lang]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_settings_defaults(self):
        return {
            "margin_percent": 5,  # added to the slicer's estimate
            "heatup_minutes": 4,  # nozzle and bed heat-up
            "connect_timeout_seconds": 180,
            "late_tolerance_minutes": 10,  # later than this, the print doesn't start
            "telegram": True,  # notifications through the Telegram plugin, if installed
            # Stored as JSON strings: OctoPrint's settings system dislikes
            # arbitrary dicts under a None default.
            "job_json": "",
            "last_json": "",
        }

    def get_settings_restricted_paths(self):
        return {"never": [["job_json"], ["last_json"]]}

    def _zone(self, name: Optional[str]):
        """The browser's time zone sent with the request, or else the server's."""
        if name and ZoneInfo is not None:
            try:
                return ZoneInfo(name)
            except Exception:
                self._logger.warning("Unknown time zone %r, using the server's", name)
        return dt.datetime.now().astimezone().tzinfo

    def _load_json(self, key: str) -> Optional[dict]:
        raw = self._settings.get([key])
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            self._logger.warning("Invalid value in %s, ignoring it", key)
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_after_startup(self):
        job = self._get_job()
        if job:
            self._logger.info(
                "Scheduled print waiting: %s, start at %s",
                job.get("path"),
                dt.datetime.fromtimestamp(job["start_ts"], self._zone(job.get("tz"))).isoformat(),
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
        return {"js": ["js/finishat.js"], "css": ["css/finishat.css"]}

    def get_template_configs(self):
        return [
            {"type": "tab", "name": "Finish At", "custom_bindings": True},
            {"type": "settings", "name": "Finish At", "custom_bindings": False},
        ]

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
        """The slicer's estimate; failing that, OctoPrint's own analysis."""
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
            self._logger.exception("Couldn't read the estimate from %s", path)
        if not est:
            try:
                meta = self._file_manager.get_metadata(FileDestinations.LOCAL, path) or {}
                est = meta.get("analysis", {}).get("estimatedPrintTime")
                est = int(est) if est else None
            except Exception:
                est = None
        self._estimate_cache[key] = est
        return est

    def _plan(self, path: str, end_value: str, tz, _) -> planning.Plan:
        if not path or not self._file_manager.file_exists(FileDestinations.LOCAL, path):
            raise planning.PlanError(_("File not found on OctoPrint."))
        now = dt.datetime.now(tz)
        end = planning.parse_end_time(end_value, now, _)
        return planning.make_plan(
            self._estimate(path),
            end,
            now,
            margin_percent=float(self._settings.get(["margin_percent"])),
            heatup_minutes=float(self._settings.get(["heatup_minutes"])),
            _=_,
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

    @staticmethod
    def _error(message: str, status: int):
        return flask.make_response(flask.jsonify(error=message), status)

    def on_api_command(self, command, data):
        if not Permissions.PRINT.can():
            flask.abort(403)
        lang = data.get("lang")
        _ = self._translator(lang)
        tz = self._zone(data.get("tz"))

        if command == "preview":
            try:
                plan = self._plan(data.get("path"), data.get("end"), tz, _)
            except planning.PlanError as e:
                return flask.jsonify(error=str(e))
            return flask.jsonify(plan.to_dict())

        if command == "schedule":
            if data.get("bed_clear") is not True:
                return self._error(_("Confirm that the bed is clear and clean."), 400)
            with self._lock:
                if self._get_job():
                    return self._error(_("A print is already scheduled: cancel it first."), 409)
                try:
                    plan = self._plan(data.get("path"), data.get("end"), tz, _)
                except planning.PlanError as e:
                    return self._error(str(e), 400)
                job = {
                    "path": data["path"],
                    "start_ts": plan.start.timestamp(),
                    "end_ts": plan.end.timestamp(),
                    "estimate_s": plan.estimate_s,
                    "planned_s": plan.planned_s,
                    "created_ts": time.time(),
                    "user": self._current_user_name(),
                    "tz": getattr(tz, "key", None),
                    "lang": _normalize_lang(lang),
                }
                self._set_job(job)
            now = dt.datetime.now(tz)
            msg = _("Scheduled {file}: starts {start}, finishes {end}").format(
                file=job["path"],
                start=planning.day_label(plan.start, now, _),
                end=planning.day_label(plan.end, now, _),
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
                    return self._error(
                        _("The print is already starting: stop it from OctoPrint."), 409
                    )
                self._set_job(None)
            msg = _("Schedule cancelled: {file}").format(file=job["path"])
            self._set_last("cancelled", msg)
            self._logger.info(msg)
            self._notify(msg, "info", telegram=True)
            return flask.jsonify(self._state())

        return self._error(_("Unknown command"), 400)

    def _state(self) -> dict:
        job = self._get_job()
        if job:
            job = dict(job)
            job["launching"] = self._launching
        return {
            "files": self._list_gcode_files(),
            "job": job,
            "last": self._get_last(),
            "now_ts": time.time(),
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
                _ = self._translator(job.get("lang"))
                self._fail(
                    _(
                        "Missed start for {file}: OctoPrint was not running at the planned "
                        "time ({delay} late). Not starting it."
                    ).format(file=job["path"], delay=planning.format_duration(late))
                )
                return
            self._launching = True
        threading.Thread(target=self._launch, args=(job,), daemon=True).start()

    def _psu(self) -> Optional[dict]:
        try:
            return self._plugin_manager.get_helpers("psucontrol", "get_psu_state", "turn_psu_on")
        except Exception:
            return None

    def _launch(self, job: dict):
        path = job["path"]
        _ = self._translator(job.get("lang"))
        try:
            self._logger.info("Scheduled start of %s", path)
            if self._printer.is_printing() or self._printer.is_paused():
                self._fail(_("Not starting {file}: the printer is already printing.").format(file=path))
                return
            if not self._file_manager.file_exists(FileDestinations.LOCAL, path):
                self._fail(_("Not starting {file}: the file is no longer on OctoPrint.").format(file=path))
                return

            psu = self._psu()
            if psu and not psu["get_psu_state"]():
                self._logger.info("Powering on the printer through PSU Control")
                psu["turn_psu_on"]()

            timeout = float(self._settings.get(["connect_timeout_seconds"]))
            deadline = time.time() + timeout
            tried_connect_at = None
            while time.time() < deadline:
                if self._printer.is_ready():
                    break
                if self._printer.is_printing() or self._printer.is_paused():
                    self._fail(
                        _("Not starting {file}: the printer is already printing.").format(file=path)
                    )
                    return
                if self._printer.is_closed_or_error() and (
                    tried_connect_at is None or time.time() - tried_connect_at > 60
                ):
                    self._logger.info("Printer not connected, trying to connect")
                    tried_connect_at = time.time()
                    self._printer.connect()
                time.sleep(2)
            else:
                self._fail(
                    _("Not starting {file}: the printer did not respond within {duration}.").format(
                        file=path, duration=planning.format_duration(timeout)
                    )
                )
                return

            self._printer.select_file(path, False, printAfterSelect=True, user=job.get("user"))
            msg = _("Scheduled print started: {file}").format(file=path)
            self._logger.info(msg)
            self._set_last("started", msg)
            self._notify(msg, "success", telegram=False)  # Telegram already announces print starts
        except Exception as e:
            self._logger.exception("Error during the scheduled start")
            self._fail(_("Error while starting {file}: {error}").format(file=path, error=e))
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
            self._logger.exception("Couldn't send the message to the UI")
        if telegram and self._settings.get_boolean(["telegram"]):
            self._telegram(message)

    def _telegram(self, message: str):
        """Use the Telegram plugin's "User Notification" message, if installed."""
        try:
            info = self._plugin_manager.get_plugin_info("telegram")
            if info and info.enabled and info.implementation:
                info.implementation.on_event("UserNotif", {"UserNotif": f"[Finish At] {message}"})
        except Exception:
            self._logger.exception("Couldn't send the Telegram notification")

    # ------------------------------------------------------------------
    # Software update
    # ------------------------------------------------------------------

    def get_update_information(self):
        return {
            "finishat": {
                "displayName": "Finish At",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "Ale6699",
                "repo": "OctoPrint-FinishAt",
                "current": self._plugin_version,
                "pip": "https://github.com/Ale6699/OctoPrint-FinishAt/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "Finish At"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = FinishAtPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
