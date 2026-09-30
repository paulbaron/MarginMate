/* « Consignes » (returnables/templates/returnables/): the pickup form, on a
 * phone, in a cellar, while the driver waits.
 *
 * Everything here is a convenience over a form that works without it: the
 * counts are typed, the photo inputs are the browser's own, and the server
 * checks everything again. What the script adds:
 *
 * - the steppers: − and + beside each count (drawn hidden by the server),
 *   blank taken as 0, kept within 0..9 999, the field's text selected when it
 *   is focused so a new number replaces the old; Enter in a count (the
 *   keypad's key on Android) moves to the next one and never sends the form;
 * - the photos: after each shot the filled input is moved into a hidden box
 *   of the same form and a fresh one takes its place - a camera input holds
 *   ONE photo, and every input named `photos` is posted - with a preview
 *   and « Retirer » for each photo (a multiple input's files are rebuilt
 *   without it), and no more than the pickup may take (its 11th is refused
 *   with a line of text; the server keeps the first ten all the same);
 * - the draft (new pickup only): what was typed, kept in the browser for 12
 *   hours under a key of the tenant (<body data-tenant>, base.html - storage
 *   belongs to the origin, not to the login), offered back into a blank
 *   form - a phone discarding the tab while its camera is open reloads the
 *   page empty. Photos cannot be kept: the notice says how many to take
 *   again, and names a note restored (shown, its folded part opened).
 *   « Effacer » puts back the day and « Repris par » the page offered.
 *   Forgotten once the pickup is saved (`?enregistree=1`);
 * - the stale tab: a tab opened yesterday and shown again today moves its
 *   date's `max` - and its value, when it was still « today » - to today.
 *
 * Nodes are built with createElement and textContent only: a file name or a
 * type name is never read as markup.
 */
(function () {
    "use strict";

    var MAX_COUNT = 9999;
    var DRAFT_MAX_AGE_MS = 12 * 60 * 60 * 1000;
    var TENANT_SCOPE = document.body.getAttribute("data-tenant") ? "espace-" + document.body.getAttribute("data-tenant") + ":" : "";
    var DRAFT_KEY = "marginmate:" + TENANT_SCOPE + "consignes:brouillon";

    // -------------------------------------------------------------- helpers

    function isoDay(moment) {
        function two(n) { return (n < 10 ? "0" : "") + n; }
        return moment.getFullYear() + "-" + two(moment.getMonth() + 1) + "-" + two(moment.getDate());
    }

    function frenchDay(iso) {
        var parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
        return parts ? parts[3] + "/" + parts[2] + "/" + parts[1] : "";
    }

    function wholeNumber(text) {
        var value = String(text || "").trim();
        return /^[0-9]+$/.test(value) ? parseInt(value, 10) : 0;
    }

    function fire(element, name) {
        element.dispatchEvent(new Event(name, { bubbles: true }));
    }

    // ------------------------------------------------------------- steppers

    document.addEventListener("click", function (event) {
        var button = event.target.closest ? event.target.closest(".stepper-btn") : null;
        if (!button) return;
        event.preventDefault();
        var input = button.parentNode.querySelector("input");
        if (!input) return;
        var step = parseInt(button.getAttribute("data-step"), 10) || 0;
        var next = Math.min(MAX_COUNT, Math.max(0, wholeNumber(input.value) + step));
        input.value = String(next);
        fire(input, "input");
    });

    document.addEventListener("focusin", function (event) {
        var input = event.target;
        if (!input.matches || !input.matches(".count-input")) return;
        setTimeout(function () {
            try { input.select(); } catch (error) { /* a field that cannot select: nothing to do */ }
        }, 0);
    });

    // The keypad's key (« next », the last count's « done ») reaches the page
    // as an Enter on Android, and Enter in a field sends its form: the
    // pickup was saved with the kegs typed and nothing else. It moves to the
    // next count instead; on the last one it closes the keypad. Cancelling
    // keydown cancels the keypress, and with it the implicit submission.
    document.addEventListener("keydown", function (event) {
        var input = event.target;
        if (!(event.key === "Enter" || event.keyCode === 13)) return;
        if (!input.matches || !input.matches(".count-input") || !input.form) return;
        event.preventDefault();
        var fields = Array.prototype.slice.call(input.form.querySelectorAll(".count-input"));
        var next = fields[fields.indexOf(input) + 1];
        if (next) next.focus();
        else input.blur();
    });

    // --------------------------------------------------------------- photos

    function setUpPhotos(form) {
        var box = form.querySelector("[data-photos]");
        if (!box) return function () { return 0; };
        var max = parseInt(box.getAttribute("data-max-photos"), 10) || 0;
        var store = box.querySelector("[data-photo-inputs]");
        var list = box.querySelector("[data-photo-previews]");
        var refusal = box.querySelector("[data-photo-refused]");
        var entries = [];

        box.querySelectorAll("[data-photo-slot] input[type=file]").forEach(function (input) {
            input.classList.add("visually-hidden");
        });

        function say(text) {
            refusal.textContent = text;
            refusal.hidden = !text;
        }

        function changed() {
            fire(form, "photos-changed");
        }

        function forget(entry) {
            try { URL.revokeObjectURL(entry.url); } catch (error) { /* already gone */ }
            if (entry.item && entry.item.parentNode) entry.item.parentNode.removeChild(entry.item);
        }

        function rebuild(input, kept) {
            try {
                var transfer = new DataTransfer();
                kept.forEach(function (other) { transfer.items.add(other.file); });
                input.files = transfer.files;
                return true;
            } catch (error) {
                return false;
            }
        }

        function remove(entry) {
            forget(entry);
            entries = entries.filter(function (other) { return other !== entry; });
            var siblings = entries.filter(function (other) { return other.input === entry.input; });
            if (siblings.length && !rebuild(entry.input, siblings)) {
                // No DataTransfer here: the photos picked with it go together.
                siblings.forEach(forget);
                entries = entries.filter(function (other) { return other.input !== entry.input; });
                siblings = [];
            }
            if (!siblings.length && entry.input.parentNode) entry.input.parentNode.removeChild(entry.input);
            say("");
            changed();
        }

        function preview(entry) {
            var item = document.createElement("li");
            item.className = "photo-preview";
            var image = document.createElement("img");
            entry.url = URL.createObjectURL(entry.file);
            image.src = entry.url;
            image.alt = entry.file.name;
            var name = document.createElement("span");
            name.className = "small muted";
            name.textContent = entry.file.name;
            var button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-small btn-secondary";
            button.textContent = "Retirer";
            button.addEventListener("click", function () { remove(entry); });
            item.appendChild(image);
            item.appendChild(name);
            item.appendChild(button);
            list.appendChild(item);
            entry.item = item;
        }

        box.addEventListener("change", function (event) {
            var input = event.target;
            if (!input.matches || !input.matches("[data-photo-slot] input[type=file]")) return;
            var files = Array.prototype.slice.call(input.files || []);
            if (!files.length) return;
            var room = Math.max(0, max - entries.length);
            var kept = files.slice(0, room);
            if (kept.length < files.length) {
                var refused = files.length - kept.length;
                say(max + " photos au plus : " + refused + (refused > 1 ? " photos n'ont pas été ajoutées." : " photo n'a pas été ajoutée."));
                if (!kept.length || !rebuild(input, kept.map(function (file) { return { file: file }; }))) {
                    input.value = "";
                    changed();
                    return;
                }
            } else {
                say("");
            }
            // The filled input keeps its photos in the hidden box; a fresh one,
            // empty, takes its place under the same label.
            var fresh = input.cloneNode(false);
            fresh.value = "";
            input.parentNode.insertBefore(fresh, input);
            store.appendChild(input);
            kept.forEach(function (file) {
                var entry = { input: input, file: file };
                entries.push(entry);
                preview(entry);
            });
            changed();
        });

        return function () { return entries.length; };
    }

    // ----------------------------------------------------- the folded line

    function setUpSummary(form) {
        var dateInput = form.querySelector("input[data-today]");
        var supplier = form.querySelector("select[name=supplier]");
        var shownDate = form.querySelector("[data-summary-date]");
        var shownSupplier = form.querySelector("[data-summary-supplier]");
        function update() {
            if (dateInput && shownDate && frenchDay(dateInput.value)) shownDate.textContent = frenchDay(dateInput.value);
            if (supplier && shownSupplier) {
                var option = supplier.options[supplier.selectedIndex];
                shownSupplier.textContent = option && option.value ? option.textContent.trim() : "non précisé";
            }
        }
        if (dateInput) dateInput.addEventListener("change", update);
        if (supplier) supplier.addEventListener("change", update);
        return update;
    }

    // ------------------------------------------------------------ stale tab

    function setUpToday(form, update, moveValue) {
        var dateInput = form.querySelector("input[data-today]");
        if (!dateInput) return;
        function check() {
            var was = dateInput.getAttribute("data-today");
            var today = isoDay(new Date());
            if (!was || today === was) return;
            dateInput.max = today;
            if (moveValue && dateInput.value === was) {
                dateInput.value = today;
                fire(dateInput, "input");
            }
            dateInput.setAttribute("data-today", today);
            update();
        }
        window.addEventListener("pageshow", check);
        document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "visible") check();
        });
        check();
    }

    // ---------------------------------------------------------------- draft

    function readDraft() {
        try {
            var raw = localStorage.getItem(DRAFT_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (error) {
            return null;
        }
    }

    function writeDraft(draft) {
        try { localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); } catch (error) { /* storage refused */ }
    }

    function dropDraft() {
        try { localStorage.removeItem(DRAFT_KEY); } catch (error) { /* storage refused */ }
    }

    function setUpDraft(form, photoCount, update) {
        var dateInput = form.querySelector("input[data-today]");
        var supplier = form.querySelector("select[name=supplier]");
        var note = form.querySelector("textarea[name=note]");
        var notice = form.querySelector("[data-draft-notice]");
        var countInputs = Array.prototype.slice.call(form.querySelectorAll("[data-count-row] input"));
        var flag = form.getAttribute("data-saved-flag") || "enregistree";

        function collect() {
            var counts = {};
            countInputs.forEach(function (input) {
                if (wholeNumber(input.value)) counts[input.name] = String(wholeNumber(input.value));
            });
            return {
                saved_at: Date.now(),
                date: dateInput ? dateInput.value : "",
                supplier: supplier ? supplier.value : "",
                counts: counts,
                note: note ? note.value : "",
                photos: photoCount(),
            };
        }

        function holdsSomething(draft) {
            return !!draft && (Object.keys(draft.counts || {}).length > 0 || !!(draft.note || "").trim() || draft.photos > 0);
        }

        function isBlank() {
            return countInputs.every(function (input) { return !wholeNumber(input.value); })
                && !(note && note.value.trim()) && photoCount() === 0;
        }

        function save() {
            var draft = collect();
            if (holdsSomething(draft)) writeDraft(draft);
            else dropDraft();
        }

        // Saved: the draft is the pickup now. The flag leaves the address,
        // so a reload does not forget a draft typed since.
        var query = new URLSearchParams(window.location.search);
        if (query.has(flag)) {
            dropDraft();
            query.delete(flag);
            var rest = query.toString();
            try {
                window.history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : "") + window.location.hash);
            } catch (error) { /* an old browser keeps the flag: harmless */ }
        } else {
            offer();
        }

        form.addEventListener("input", save);
        form.addEventListener("change", save);
        form.addEventListener("photos-changed", save);

        function offer() {
            var draft = readDraft();
            if (!draft) return;
            if (!(Date.now() - (draft.saved_at || 0) < DRAFT_MAX_AGE_MS) || !holdsSomething(draft)) {
                dropDraft();
                return;
            }
            if (!isBlank() || !notice) return;
            var said = [];
            countInputs.forEach(function (input) {
                var value = (draft.counts || {})[input.name];
                if (!value) return;
                input.value = value;
                var row = input.closest("[data-count-row]");
                said.push((row ? row.getAttribute("data-type-name") : input.name) + " " + value);
            });
            // A note restored is said, and shown: it sits in the folded part,
            // and would otherwise be saved with the next pickup unseen.
            if (note && (draft.note || "").trim()) {
                note.value = draft.note;
                said.push("une note");
                var details = form.querySelector(".pickup-details");
                if (details) details.open = true;
            }
            if (dateInput && draft.date && draft.date >= (dateInput.min || "") && draft.date <= (dateInput.max || draft.date)) {
                dateInput.value = draft.date;
            }
            if (supplier && Array.prototype.some.call(supplier.options, function (option) { return option.value === draft.supplier; })) {
                supplier.value = draft.supplier;
            }
            if (draft.photos > 0) said.push(draft.photos + (draft.photos > 1 ? " photos à reprendre" : " photo à reprendre"));
            update();
            notice.textContent = "";
            notice.appendChild(document.createTextNode(
                said.length ? "Comptage non envoyé retrouvé (" + said.join(" ; ") + ") — " : "Comptage non envoyé retrouvé — "
            ));
            var clear = document.createElement("button");
            clear.type = "button";
            clear.className = "link-button";
            clear.textContent = "Effacer";
            clear.addEventListener("click", function () {
                countInputs.forEach(function (input) { input.value = ""; });
                if (note) note.value = "";
                // What the draft moved goes back too: its day (yesterday
                // evening's count) and « Repris par » - else today's pickup
                // was saved dated the day before.
                if (dateInput && dateInput.getAttribute("data-today")) dateInput.value = dateInput.getAttribute("data-today");
                if (supplier) {
                    var offered = Array.prototype.filter.call(supplier.options, function (option) { return option.defaultSelected; })[0];
                    supplier.value = offered ? offered.value : "";
                }
                update();
                dropDraft();
                notice.hidden = true;
            });
            notice.appendChild(clear);
            notice.hidden = false;
        }
    }

    // ----------------------------------------------------------------- init

    function init() {
        document.querySelectorAll(".stepper-btn[hidden]").forEach(function (button) { button.hidden = false; });
        document.querySelectorAll("form[data-pickup-form]").forEach(function (form) {
            var photoCount = setUpPhotos(form);
            var update = setUpSummary(form);
            var drafted = form.hasAttribute("data-draft");
            setUpToday(form, update, drafted);
            if (drafted) setUpDraft(form, photoCount, update);
        });
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
