// « Données »: the picker ticks what a section needs, as the server does.
//
// The server is authoritative (transfer/registry.py): a selection posted
// without its closure is refused and sent back ticked. This only mirrors it,
// so the page reads right while boxes are clicked. Each box carries
// data-forced-by, every section that ticks it when ticked - transitive
// already - so there is no graph here to drift from the Python one.
//
// A forced box stays ENABLED (a disabled input is not posted, and the server
// wants the whole closure): it is marked aria-disabled and a click on it is
// undone. The person's own tick is remembered, so a box no longer forced goes
// back to what they had chosen.
(function () {
    "use strict";

    function setup(form) {
        var picker = form.querySelector("[data-picker]");
        if (!picker) return;
        var mode = picker.getAttribute("data-mode");
        var boxes = Array.prototype.slice.call(picker.querySelectorAll('input[type="checkbox"][data-section]'));
        var byKey = {};
        var own = {};
        boxes.forEach(function (box) {
            var key = box.getAttribute("data-section");
            byKey[key] = box;
            own[key] = box.checked && !box.classList.contains("is-forced");
        });

        function forcersOf(box) {
            try {
                return JSON.parse(box.getAttribute("data-forced-by") || "{}");
            } catch (error) {
                return {};
            }
        }

        function refresh() {
            boxes.forEach(function (box) {
                var key = box.getAttribute("data-section");
                var row = box.closest("[data-row]");
                if (box.disabled) return;
                var forcers = forcersOf(box);
                var names = [];
                Object.keys(forcers).forEach(function (other) {
                    if (own[other] && byKey[other] && !byKey[other].disabled) names.push(forcers[other]);
                });
                var forced = names.length > 0;
                box.checked = forced || own[key];
                box.classList.toggle("is-forced", forced);
                if (forced) box.setAttribute("aria-disabled", "true");
                else box.removeAttribute("aria-disabled");
                var note = row && row.querySelector("[data-forced-note]");
                if (note) {
                    note.textContent = forced ? (mode === "clear" ? "effacé avec " : "nécessaire pour ") + names.join(", ") : "";
                    note.hidden = !forced;
                }
                var strategy = row && row.querySelector("[data-strategy-for]");
                if (strategy) strategy.hidden = !box.checked;
            });
            form.querySelectorAll("[data-shown-with]").forEach(function (element) {
                var box = byKey[element.getAttribute("data-shown-with")];
                element.hidden = !(box && box.checked);
            });
        }

        // After a preview, any change makes it the preview of something else:
        // the confirm is hidden until the next one (the server refuses it too).
        function stale() {
            if (!form.hasAttribute("data-previewed")) return;
            form.querySelectorAll("[data-after-preview]").forEach(function (element) { element.hidden = true; });
            form.querySelectorAll("[data-stale-note]").forEach(function (element) { element.hidden = false; });
        }

        picker.addEventListener("click", function (event) {
            var box = event.target.closest('input[type="checkbox"][data-section]');
            if (box && box.classList.contains("is-forced")) event.preventDefault();
        });

        picker.addEventListener("change", function (event) {
            var target = event.target;
            if (target.matches('input[type="checkbox"][data-section]')) {
                own[target.getAttribute("data-section")] = target.checked;
                refresh();
            }
            stale();
        });

        form.querySelectorAll("[data-js-only]").forEach(function (element) { element.hidden = false; });

        form.querySelectorAll("[data-tick-all]").forEach(function (button) {
            button.addEventListener("click", function () {
                boxes.forEach(function (box) {
                    if (!box.disabled) own[box.getAttribute("data-section")] = true;
                });
                refresh();
                stale();
            });
        });

        // A full archive starts all ticked; one part out of it meant
        // unticking eight boxes in dependency order, a forced one ignoring
        // the click. Every own tick goes, so nothing is forced any more.
        form.querySelectorAll("[data-untick-all]").forEach(function (button) {
            button.addEventListener("click", function () {
                boxes.forEach(function (box) {
                    own[box.getAttribute("data-section")] = false;
                });
                refresh();
                stale();
            });
        });

        form.querySelectorAll("[data-all-strategy]").forEach(function (button) {
            button.addEventListener("click", function () {
                var value = button.getAttribute("data-all-strategy");
                var group = button.closest("fieldset") || form;
                group.querySelectorAll('input[type="radio"][value="' + value + '"]').forEach(function (radio) {
                    radio.checked = true;
                });
                stale();
            });
        });

        refresh();
    }

    document.querySelectorAll("[data-transfer-form]").forEach(setup);
})();
