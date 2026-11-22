/*
 * OctoPrint-Pianifica: pannello per avviare una stampa in modo che finisca a un'ora scelta.
 */
$(function () {
    function PianificaViewModel(parameters) {
        var self = this;
        self.loginState = parameters[0];

        self.files = ko.observableArray([]);
        self.selected = ko.observable(undefined);
        self.endTime = ko.observable("16:00");
        self.bedClear = ko.observable(false);
        self.preview = ko.observable(null);
        self.error = ko.observable("");
        self.job = ko.observable(null);
        self.last = ko.observable(null);
        self.psucontrol = ko.observable(true);
        self.busy = ko.observable(false);
        self.nowTs = ko.observable(Date.now() / 1000);

        self.fileLabel = function (f) {
            return f.estimate_label ? f.name + " (" + f.estimate_label + ")" : f.name + " (durata sconosciuta)";
        };

        self.canSchedule = ko.pureComputed(function () {
            return !self.busy() && !!self.preview() && self.bedClear();
        });

        self.countdown = ko.pureComputed(function () {
            var job = self.job();
            if (!job) return "";
            var secs = Math.round(job.start_ts - self.nowTs());
            if (secs <= 0) return "(adesso)";
            var h = Math.floor(secs / 3600);
            var m = Math.ceil((secs % 3600) / 60);
            if (m === 60) { h += 1; m = 0; }
            if (h && m) return "(tra " + h + " h " + m + " min)";
            if (h) return "(tra " + h + " h)";
            return "(tra " + m + " min)";
        });

        self.lastClass = ko.pureComputed(function () {
            var last = self.last();
            if (!last) return "";
            return { started: "text-success", failed: "text-error", cancelled: "muted" }[last.status] || "";
        });

        self.applyState = function (r) {
            self.files(r.files || []);
            self.job(r.job || null);
            self.last(r.last || null);
            self.psucontrol(!!r.psucontrol);
            if (r.now_ts) self.nowTs(r.now_ts);
        };

        self.refresh = function () {
            if (!self.loginState.isUser()) return;
            OctoPrint.simpleApiGet("pianifica").done(self.applyState);
        };

        var previewTimer = null;
        self.requestPreview = function () {
            clearTimeout(previewTimer);
            previewTimer = setTimeout(function () {
                var path = self.selected();
                var end = self.endTime();
                if (!path || !end) {
                    self.preview(null);
                    self.error("");
                    return;
                }
                OctoPrint.simpleApiCommand("pianifica", "preview", { path: path, end: end })
                    .done(function (r) {
                        if (r.error) {
                            self.preview(null);
                            self.error(r.error);
                        } else {
                            self.preview(r);
                            self.error("");
                        }
                    })
                    .fail(function () {
                        self.preview(null);
                        self.error("Errore nel calcolo dell'orario.");
                    });
            }, 250);
        };
        self.selected.subscribe(self.requestPreview);
        self.endTime.subscribe(self.requestPreview);

        function errorText(xhr, fallback) {
            return (xhr && xhr.responseJSON && xhr.responseJSON.error) || fallback;
        }

        self.schedule = function () {
            if (!self.canSchedule()) return;
            self.busy(true);
            OctoPrint.simpleApiCommand("pianifica", "schedule", {
                path: self.selected(),
                end: self.endTime(),
                bed_clear: self.bedClear()
            })
                .done(function (r) {
                    self.applyState(r);
                    self.bedClear(false);
                    self.preview(null);
                    self.error("");
                })
                .fail(function (xhr) {
                    self.error(errorText(xhr, "Pianificazione non riuscita."));
                })
                .always(function () { self.busy(false); });
        };

        self.cancel = function () {
            self.busy(true);
            OctoPrint.simpleApiCommand("pianifica", "cancel", {})
                .done(self.applyState)
                .fail(function (xhr) {
                    new PNotify({ title: "Pianifica", text: errorText(xhr, "Annullamento non riuscito."), type: "error" });
                })
                .always(function () { self.busy(false); });
        };

        // Aggiorna il conto alla rovescia e, ogni tanto, l'anteprima.
        setInterval(function () {
            self.nowTs(self.nowTs() + 30);
        }, 30000);

        self.onStartupComplete = self.refresh;
        self.onUserLoggedIn = self.refresh;
        self.onTabChange = function (next) {
            if (next === "#tab_plugin_pianifica") {
                self.refresh();
                self.requestPreview();
            }
        };
        self.onEventUpdatedFiles = self.refresh;
        self.onEventFileAdded = self.refresh;
        self.onEventFileRemoved = self.refresh;

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "pianifica") return;
            if (data.message) {
                new PNotify({
                    title: "Pianifica",
                    text: data.message,
                    type: data.level || "info",
                    hide: data.level !== "error"
                });
            }
            self.refresh();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PianificaViewModel,
        dependencies: ["loginStateViewModel"],
        elements: ["#tab_plugin_pianifica"]
    });
});
