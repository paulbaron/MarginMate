// « Personnel »: conveniences over pages that work without them.
//
// * The month's grid. Choosing an absence (« Congés payés », « Arrêt
//   maladie »…) empties and disables the day's hours: an absence is 0 h,
//   which the server enforces whatever it is sent - and a disabled field is
//   not sent at all, which the server reads as « nothing said » (a day that
//   does not work then has no hours). Back to « Travail », the field comes
//   back holding the typical week's hours. Hours typed on a « Repos » day
//   switch it to « Travail », which is what the server reads them as.
// * Its figures follow the typing: each week's total, the month's, the
//   three cards above the grid (hours worked, the difference, the absences)
//   and the orange mark of a day that is not the typical week - by the
//   rules of staff/timesheet.py (only « Travail » hours count; an
//   absence's typical hours are set aside from the difference,
//   MonthSummary.difference). Drawn by the server, they did not move: a
//   week corrected read « 36 h » beside the rows just changed (review,
//   28/09). The server's figures stay the reference: the page draws them
//   again once saved, and until then [data-unsaved] says these are not.
// * Nothing typed is lost without a word. While the grid holds changes not
//   saved, leaving asks first: the browser's own prompt for any way out,
//   and the page's sentence (data-leaves-grid) for its links and the three
//   forms beside the grid, which reload the month from the database - and
//   for the PDF, which prints the month as SAVED: a sheet handed over to be
//   signed without the corrections just typed (review, 28/09).
// * The typical week's seven fields: their total, as they are typed.
//
// Hours are added up in hundredths, as integers: the same figures the
// server keeps as Decimal, with no 22,999999 on the way.
(function () {
    "use strict";

    var DECIMAL = /^(?:([0-9]+)(?:[.,]([0-9]*))?|[.,]([0-9]+))$/;
    var CLOCK = /^([0-9]+)(?:[hH]([0-9]{2})?|:([0-9]{2}))$/;
    // « 1 5 » is refused, not read as 15 (timesheet._SPLIT_DIGITS).
    var SPLIT_DIGITS = /[0-9]\s+[0-9]/;
    var MINUS = "−";   // timesheet.MINUS
    var WORK = "travail";
    var REST = "repos";

    // Hours as typed -> hundredths of an hour, or null when the server would
    // refuse them (timesheet.parse_hours): garbage, over 24 h, more than two
    // decimals, minutes that do not come to a hundredth (7h20), two figures
    // a space apart.
    function hundredths(text) {
        if (SPLIT_DIGITS.test(text || "")) return null;
        var compact = (text || "").replace(/\s+/g, "");
        if (!compact) return 0;
        var value = null;
        var match = DECIMAL.exec(compact);
        if (match) {
            var whole = match[1] || "0";
            var fraction = match[1] !== undefined ? (match[2] || "") : match[3];
            fraction = fraction.replace(/0+$/, "");
            if (fraction.length > 2) return null;
            value = parseInt(whole, 10) * 100 + parseInt((fraction + "00").slice(0, 2), 10);
        } else {
            match = CLOCK.exec(compact);
            if (!match) return null;
            var minutes = parseInt(match[2] || match[3] || "0", 10);
            if (minutes > 59 || (minutes * 100) % 60 !== 0) return null;
            value = parseInt(match[1], 10) * 100 + (minutes * 100) / 60;
        }
        return value > 2400 ? null : value;
    }

    // « 36 », « 7,5 », « 7,25 » - timesheet.format_hours.
    function written(value) {
        var whole = Math.floor(value / 100);
        var rest = value % 100;
        if (!rest) return String(whole);
        var fraction = (rest < 10 ? "0" : "") + rest;
        return whole + "," + fraction.replace(/0$/, "");
    }

    // « +3 h », « −8 h », « 0 h » - timesheet.format_hours_difference.
    function signed(value) {
        if (value === 0) return "0 h";
        return (value > 0 ? "+" : MINUS) + written(Math.abs(value)) + " h";
    }

    function dayCount(count) {
        return count === 1 ? "1 jour" : count + " jours";
    }

    function weekTotals(root) {
        root.querySelectorAll("[data-week]").forEach(function (week) {
            var output = week.querySelector("[data-week-total]");
            if (!output) return;
            function update() {
                var total = 0;
                var fields = week.querySelectorAll("input[data-week-hours]");
                for (var i = 0; i < fields.length; i++) {
                    var value = hundredths(fields[i].value);
                    if (value === null) {
                        output.textContent = "—";
                        return;
                    }
                    total += value;
                }
                output.textContent = written(total);
            }
            week.addEventListener("input", update);
            update();
        });
    }

    function grid(root) {
        var form = root.querySelector("form[data-timesheet-form]");
        var table = root.querySelector("[data-timesheet-grid]");
        if (!form || !table) return;
        var absences = (table.getAttribute("data-absence-kinds") || "").split(/\s+/);
        // The kinds' labels and order, from the page's own select: the
        // absences card lists them in that order, as the server does.
        var labels = {};
        var order = [];
        var first = table.querySelector("select[data-kind]");
        if (first) {
            Array.prototype.forEach.call(first.options, function (option) {
                labels[option.value] = option.textContent;
                order.push(option.value);
            });
        }

        function settle(select, chosenNow) {
            var row = select.closest("tr");
            var hours = row && row.querySelector("input[data-hours]");
            if (!hours) return;
            if (absences.indexOf(select.value) !== -1) {
                hours.value = "";
                hours.disabled = true;
                return;
            }
            var wasDisabled = hours.disabled;
            hours.disabled = false;
            if (!chosenNow) return;
            if (select.value === REST) {
                hours.value = "";
            } else if (select.value === WORK && (wasDisabled || !hours.value.trim())) {
                hours.value = row.getAttribute("data-typical") || "";
            }
        }

        function live(name, text) {
            document.querySelectorAll('[data-live="' + name + '"]').forEach(function (element) {
                element.textContent = text;
            });
        }

        function absencesCard(counts) {
            var card = document.querySelector('[data-live="absences"]');
            var label = card && card.querySelector(".stat-label");
            if (!label) return;
            while (label.nextSibling) card.removeChild(label.nextSibling);
            var kinds = order.filter(function (kind) { return counts[kind]; });
            if (!kinds.length) {
                var none = document.createElement("div");
                none.className = "stat-value";
                none.textContent = "aucune";
                card.appendChild(none);
                return;
            }
            var list = document.createElement("ul");
            list.className = "absence-counts";
            kinds.forEach(function (kind) {
                var item = document.createElement("li");
                item.appendChild(document.createTextNode(labels[kind] + " : "));
                var count = document.createElement("strong");
                count.textContent = dayCount(counts[kind]);
                item.appendChild(count);
                list.appendChild(item);
            });
            card.appendChild(list);
        }

        // The figures, from the grid as it stands (timesheet.MonthSheet).
        function recompute() {
            var worked = 0;
            var planned = 0;
            var absent = 0;
            var daysWorked = 0;
            var unreadable = false;
            var counts = {};
            table.querySelectorAll("tbody").forEach(function (tbody) {
                var week = 0;
                var weekUnreadable = false;
                tbody.querySelectorAll("tr.day-row").forEach(function (row) {
                    var kind = row.querySelector("select[data-kind]").value;
                    var typical = hundredths(row.getAttribute("data-typical")) || 0;
                    var hours = kind === WORK ? hundredths(row.querySelector("input[data-hours]").value) : 0;
                    planned += typical;
                    if (absences.indexOf(kind) !== -1) {
                        absent += typical;
                        counts[kind] = (counts[kind] || 0) + 1;
                    }
                    if (hours === null) {
                        weekUnreadable = true;
                    } else if (kind === WORK) {
                        week += hours;
                        if (hours > 0) daysWorked += 1;
                    }
                    // MonthDay.differs: another kind, or other hours.
                    var plannedKind = typical > 0 ? WORK : REST;
                    var differs = kind !== plannedKind || hours === null || (kind === WORK && hours !== typical);
                    row.classList.toggle("is-changed", differs);
                    if (differs) {
                        row.setAttribute("title", "Différent de la semaine type");
                    } else {
                        row.removeAttribute("title");
                    }
                });
                var total = tbody.querySelector('[data-live="week"]');
                // A figure the server would refuse makes no total: one that
                // skipped it would be a wrong one.
                if (total) total.textContent = weekUnreadable ? "— h" : written(week) + " h";
                if (weekUnreadable) unreadable = true;
                worked += week;
            });
            var difference = unreadable ? "—" : signed(worked - (planned - absent));
            var label = absent ? "Écart hors absences" : "Écart";
            live("worked", unreadable ? "— h" : written(worked) + " h");
            live("days-worked", daysWorked === 1 ? "1 jour travaillé" : daysWorked + " jours travaillés");
            live("difference", (absent ? "dont absences : " + written(absent) + " h · " : "") + label + " : " + difference);
            live(
                "month-note",
                "semaine type : " + written(planned) + " h" +
                    (absent ? ", dont absences " + written(absent) + " h" : "") +
                    " (" + label.toLowerCase() + " " + difference + ")"
            );
            absencesCard(counts);
        }

        // Changes not saved: the form as it posts now against as it was
        // drawn - typed and typed back is no change. A page drawn back after
        // a refused save holds nothing saved yet, from the start.
        var startsUnsaved = form.hasAttribute("data-unsaved-on-load");
        var initial = null;
        var unsaved = false;
        var leaving = false;

        function posted() {
            return new URLSearchParams(new FormData(form)).toString();
        }

        function markUnsaved() {
            unsaved = startsUnsaved || posted() !== initial;
            document.querySelectorAll("[data-unsaved]").forEach(function (element) {
                element.hidden = !unsaved;
            });
            var stats = document.querySelector("[data-month-stats]");
            if (stats) stats.classList.toggle("is-unsaved", unsaved);
        }

        function changed() {
            recompute();
            markUnsaved();
        }

        table.querySelectorAll("select[data-kind]").forEach(function (select) { settle(select, false); });
        initial = posted();
        markUnsaved();

        form.addEventListener("change", function (event) {
            if (event.target.matches("select[data-kind]")) settle(event.target, true);
            changed();
        });
        form.addEventListener("input", function (event) {
            if (event.target.matches("input[data-hours]")) {
                var select = event.target.closest("tr").querySelector("select[data-kind]");
                if (select && select.value === REST && event.target.value.trim()) select.value = WORK;
            }
            changed();
        });
        form.addEventListener("submit", function () { leaving = true; });

        window.addEventListener("beforeunload", function (event) {
            if (!unsaved || leaving) return;
            event.preventDefault();
            event.returnValue = "";
        });

        document.querySelectorAll("[data-leaves-grid]").forEach(function (element) {
            element.addEventListener(element.tagName === "FORM" ? "submit" : "click", function (event) {
                if (!unsaved) return;
                if (!window.confirm(element.getAttribute("data-leaves-grid"))) {
                    event.preventDefault();
                    return;
                }
                // Asked once: the browser's own prompt is not asked again.
                leaving = true;
                // A download leaves the page where it is, still unsaved.
                if (element.hasAttribute("data-download")) {
                    setTimeout(function () { leaving = false; }, 1000);
                }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        weekTotals(document);
        grid(document);
    });
})();
