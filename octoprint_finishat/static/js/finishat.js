/*
 * OctoPrint-FinishAt: start a print so that it finishes at the time you choose.
 */
$(function () {
    function browserLang() {
        if (typeof LOCALE !== "undefined" && LOCALE) return LOCALE;
        return navigator.language || "en";
    }

    function browserTz() {
        try {
            return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
        } catch (e) {
            return "";
        }
    }

    function whenLabel(ts) {
        if (!ts) return "";
        var d = new Date(ts * 1000);
        var today = new Date();
        today.setHours(0, 0, 0, 0);
        var day0 = new Date(d.getTime());
        day0.setHours(0, 0, 0, 0);
        var diff = Math.round((day0 - today) / 86400000);
        var day;
        if (diff === 0) day = gettext("today");
        else if (diff === 1) day = gettext("tomorrow");
        else day = d.toLocaleDateString(browserLang(), { day: "2-digit", month: "2-digit" });
        var time = d.toLocaleTimeString(browserLang(), { hour: "2-digit", minute: "2-digit" });
        return _.sprintf(gettext("%(day)s at %(time)s"), { day: day, time: time });
    }

    function FinishAtViewModel(parameters) {
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

        self.chooseFileCaption = gettext("Choose a file…");

        self.fileLabel = function (f) {
            return f.estimate_label
                ? f.name + " (" + f.estimate_label + ")"
                : f.name + " " + gettext("(unknown duration)");
        };

        self.jobStartLabel = ko.pureComputed(function () {
            self.nowTs();
            return self.job() ? whenLabel(self.job().start_ts) : "";
        });
        self.jobEndLabel = ko.pureComputed(function () {
            self.nowTs();
            return self.job() ? whenLabel(self.job().end_ts) : "";
        });
        self.previewStartLabel = ko.pureComputed(function () {
            return self.preview() ? whenLabel(self.preview().start_ts) : "";
        });
        self.previewEndLabel = ko.pureComputed(function () {
            return self.preview() ? whenLabel(self.preview().end_ts) : "";
        });

        self.canSchedule = ko.pureComputed(function () {
            return !self.busy() && !!self.preview() && self.bedClear();
        });

        self.countdown = ko.pureComputed(function () {
            var job = self.job();
            if (!job) return "";
            var secs = Math.round(job.start_ts - self.nowTs());
            if (secs <= 0) return gettext("(now)");
            var h = Math.floor(secs / 3600);
            var m = Math.ceil((secs % 3600) / 60);
            if (m === 60) { h += 1; m = 0; }
            if (h && m) return _.sprintf(gettext("(in %(h)d h %(m)d min)"), { h: h, m: m });
            if (h) return _.sprintf(gettext("(in %(h)d h)"), { h: h });
            return _.sprintf(gettext("(in %(m)d min)"), { m: m });
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
            self.nowTs(Date.now() / 1000);
        };

        self.refresh = function () {
            if (!self.loginState.isUser()) return;
            OctoPrint.simpleApiGet("finishat").done(self.applyState);
        };

        function payload(extra) {
            return $.extend({ lang: browserLang(), tz: browserTz() }, extra);
        }

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
                OctoPrint.simpleApiCommand("finishat", "preview", payload({ path: path, end: end }))
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
                        self.error(gettext("Couldn't work out the start time."));
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
            OctoPrint.simpleApiCommand(
                "finishat",
                "schedule",
                payload({ path: self.selected(), end: self.endTime(), bed_clear: self.bedClear() })
            )
                .done(function (r) {
                    self.applyState(r);
                    self.bedClear(false);
                    self.preview(null);
                    self.error("");
                })
                .fail(function (xhr) {
                    self.error(errorText(xhr, gettext("Scheduling failed.")));
                })
                .always(function () { self.busy(false); });
        };

        self.cancel = function () {
            self.busy(true);
            OctoPrint.simpleApiCommand("finishat", "cancel", payload({}))
                .done(self.applyState)
                .fail(function (xhr) {
                    new PNotify({
                        title: "Finish At",
                        // Escaped: the message can contain a file name.
                        text: _.escape(errorText(xhr, gettext("Cancelling failed."))),
                        type: "error"
                    });
                })
                .always(function () { self.busy(false); });
        };

        // Keep the countdown moving.
        setInterval(function () {
            self.nowTs(Date.now() / 1000);
        }, 30000);

        self.onStartupComplete = self.refresh;
        self.onUserLoggedIn = self.refresh;
        self.onTabChange = function (next) {
            if (next === "#tab_plugin_finishat") {
                self.refresh();
                self.requestPreview();
            }
        };
        self.onEventUpdatedFiles = self.refresh;
        self.onEventFileAdded = self.refresh;
        self.onEventFileRemoved = self.refresh;

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "finishat") return;
            if (data.message) {
                new PNotify({
                    title: "Finish At",
                    // Escaped: the message contains the file name.
                    text: _.escape(data.message),
                    type: data.level || "info",
                    hide: data.level !== "error"
                });
            }
            self.refresh();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: FinishAtViewModel,
        dependencies: ["loginStateViewModel"],
        elements: ["#tab_plugin_finishat"]
    });
});
