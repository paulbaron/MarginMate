/* Photos taken with the phone's camera or picked from its gallery, into a
 * form - shared by « Consignes » (returnables/_pickup_fields.html: a
 * pickup's photos, set up by returnables.js) and Achats' import card
 * (invoices/_receipt_upload_form.html, on « Factures »' import card and on
 * « Ajouter des factures »: « Prendre une photo », set up by
 * receipt_camera.js).
 *
 * Everything here is a convenience over a form that works without it: the
 * inputs are the browser's own, and the server checks everything again.
 * Inside the form's [data-photos] box, what the script adds:
 *
 * - every input of a [data-photo-slot] label is drawn out of sight under its
 *   label, and after each selection the filled input moves into the box's
 *   hidden store ([data-photo-inputs], still inside the form: every input
 *   is posted) while a fresh, empty copy takes its place - a camera input
 *   holds ONE photo, and the second shot would otherwise replace the first;
 * - a preview per photo with « Retirer » (a multiple input's files are
 *   rebuilt without it through DataTransfer; with no DataTransfer, the
 *   photos picked together go together), and "photos-changed" fired on the
 *   form after every change;
 * - the box's limits, said in one sentence in [data-photo-refused]:
 *   data-max-photos, a count (absent: none) - the photos past it are not
 *   added; data-max-bytes, what the WHOLE form would post (every file input
 *   of it, the other choices' files included) - a selection is kept, in
 *   order, while it fits. When both refuse, the count is said. Nothing
 *   kept: the input is emptied;
 * - and with data-max-bytes, while a photo waits, the form's OTHER file
 *   inputs are weighed too: a pick that would take the post past the cap is
 *   emptied and said, and a submit past it is held back and said (a last
 *   line: a selection that changed with no "change" event). A photo lives
 *   in the page alone - a camera shot is usually not in the gallery - and
 *   a post refused before the server sees it (Cloudflare's 100 MB) loses
 *   it. With no photo waiting nothing of it is checked: a refused post then
 *   loses nothing, and a folder imported on the PC goes to the server's own
 *   cap. These two sentences are scrolled into view: the finger is on
 *   another choice, or on « Importer », a screen below the box;
 * - data-photo-rename: each photo kept is named photo-YYYYMMDD-HHMMSS (its
 *   lastModified, local time) and its extension - iOS names every camera
 *   shot « image.jpg », Android a bare number with no extension at all, and
 *   the import lists each file by its name. A name another photo of the box
 *   already has takes -2, -3... before the extension. Where a browser cannot
 *   rebuild an input's files, the photos keep their names, silently.
 *
 * window.MarginMatePhotos.setUp(form) returns a function counting the
 * photos kept (0 when the form has no box): returnables.js keeps it in a
 * pickup's draft; Achats' page changes its button's busy label by it, asks
 * before the page is left and notes for the tab how many wait. A form set
 * up twice is set up once. MarginMatePhotos.weight(bytes) says a weight
 * as the server's sentences do (common.weight).
 *
 * Nodes are built with createElement and textContent only: a file name is
 * never read as markup.
 */
(function () {
    "use strict";

    var MEGABYTE = 1024 * 1024;
    // What a name's extension looks like; anything else is no extension.
    var EXTENSION = /\.[A-Za-z0-9]{1,5}$/;
    // A photo without one (Android's bare number) takes its type's.
    var EXTENSION_OF_TYPE = { "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp" };
    // A form set up twice would add every photo twice.
    var SET_UP = typeof WeakMap === "function" ? new WeakMap() : null;

    // -------------------------------------------------------------- helpers

    function fire(element, name) {
        element.dispatchEvent(new Event(name, { bubbles: true }));
    }

    function two(n) { return (n < 10 ? "0" : "") + n; }

    // common.weight, to the letter: « 90 Mo », « 612,5 Mo », « 300 Ko » - the
    // way the server's own sentences say a weight. The tenths are rounded
    // half to even, as Python's format does, and exactly: a whole number of
    // bytes divided by 2^20 is exact in binary, so a tie is a true tie.
    function weight(size) {
        size = Math.max(Math.floor(Number(size) || 0), 0);
        if (size < MEGABYTE) return (size ? Math.max(Math.ceil(size / 1024), 1) : 0) + " Ko";
        var tenths = size * 10 / MEGABYTE;
        var rounded = Math.floor(tenths);
        var rest = tenths - rounded;
        if (rest > 0.5 || (rest === 0.5 && rounded % 2 === 1)) rounded += 1;
        var units = Math.floor(rounded / 10);
        var tenth = rounded % 10;
        return (tenth ? units + "," + tenth : String(units)) + " Mo";
    }

    function countRefusal(max, refused) {
        return max + " photos au plus : " + refused + (refused > 1 ? " photos n'ont pas été ajoutées." : " photo n'a pas été ajoutée.");
    }

    function bytesRefusal(maxBytes, refused) {
        var why = "l'envoi dépasserait " + weight(maxBytes) + ". Importez d'abord ce qui est déjà choisi, puis ";
        return refused > 1
            ? refused + " photos non ajoutées : " + why + "reprenez-les."
            : "Photo non ajoutée : " + why + "reprenez-la.";
    }

    // A pick in another file input of the form, past the cap while a photo
    // waits: the input is emptied, its files are still on the device.
    function filesRefusal(maxBytes, refused) {
        var why = "l'envoi dépasserait " + weight(maxBytes) + ". Importez d'abord ce qui est déjà choisi, puis ";
        return refused > 1
            ? refused + " fichiers non ajoutés : " + why + "choisissez-les à nouveau."
            : "Fichier non ajouté : " + why + "choisissez-le à nouveau.";
    }

    // A submit past the cap while a photo waits: nothing is sent.
    function heldRefusal(maxBytes) {
        return "Envoi arrêté : il dépasserait " + weight(maxBytes)
            + ", et les photos prises seraient perdues. Retirez des photos ou choisissez moins de fichiers,"
            + " puis « Importer » à nouveau.";
    }

    // What the form would post: every file of every file input it sends (a
    // disabled or nameless input sends nothing).
    function formBytes(form) {
        var total = 0;
        Array.prototype.forEach.call(form.elements, function (element) {
            if (element.type !== "file" || !element.name || element.disabled) return;
            Array.prototype.forEach.call(element.files || [], function (file) { total += file.size || 0; });
        });
        return total;
    }

    // The first of `files` that fit under `maxBytes` beside what the form
    // already posts - `selected` (the input's whole selection) is counted in
    // the form's files, so it is taken out first.
    function fitting(files, selected, form, maxBytes) {
        var posted = formBytes(form);
        selected.forEach(function (file) { posted -= file.size || 0; });
        var kept = [];
        for (var index = 0; index < files.length; index += 1) {
            var size = files[index].size || 0;
            if (posted + size > maxBytes) break;
            posted += size;
            kept.push(files[index]);
        }
        return kept;
    }

    function stamp(file) {
        var moment = new Date(typeof file.lastModified === "number" ? file.lastModified : Date.now());
        if (isNaN(moment.getTime())) moment = new Date();
        return "photo-" + moment.getFullYear() + two(moment.getMonth() + 1) + two(moment.getDate())
            + "-" + two(moment.getHours()) + two(moment.getMinutes()) + two(moment.getSeconds());
    }

    function extensionOf(file) {
        var found = EXTENSION.exec(file.name || "");
        if (found) return found[0].toLowerCase();
        return EXTENSION_OF_TYPE[String(file.type || "").toLowerCase()] || "";
    }

    // `files` under the names they are given, or null when none changed or a
    // File cannot be made here. `taken`: the names the box's photos have.
    function renamed(files, taken) {
        try {
            var used = Object.create(null);
            taken.forEach(function (name) { used[name] = true; });
            var changed = false;
            var named = files.map(function (file) {
                var extension = extensionOf(file);
                if (!extension) return file;
                var base = stamp(file);
                var name = base + extension;
                for (var number = 2; used[name]; number += 1) name = base + "-" + number + extension;
                used[name] = true;
                if (name === file.name) return file;
                changed = true;
                return new File([file], name, { type: file.type, lastModified: file.lastModified });
            });
            return changed ? named : null;
        } catch (error) {
            return null;
        }
    }

    // An input's files replaced by `files`; false where the browser cannot.
    function rebuild(input, files) {
        try {
            var transfer = new DataTransfer();
            files.forEach(function (file) { transfer.items.add(file); });
            input.files = transfer.files;
            return true;
        } catch (error) {
            return false;
        }
    }

    // ---------------------------------------------------------------- setUp

    function setUp(form) {
        var box = form ? form.querySelector("[data-photos]") : null;
        if (!box) return function () { return 0; };
        if (SET_UP && SET_UP.has(box)) return SET_UP.get(box);
        var counted = box.hasAttribute("data-max-photos");
        var max = parseInt(box.getAttribute("data-max-photos"), 10) || 0;
        var maxBytes = parseInt(box.getAttribute("data-max-bytes"), 10);
        if (!(maxBytes > 0)) maxBytes = null;
        var rename = box.hasAttribute("data-photo-rename");
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

        // Said where the eye is not: brought into view.
        function sayInView(text) {
            say(text);
            try { refusal.scrollIntoView({ block: "nearest" }); } catch (error) { /* said all the same */ }
        }

        function changed() {
            fire(form, "photos-changed");
        }

        function forget(entry) {
            try { URL.revokeObjectURL(entry.url); } catch (error) { /* already gone */ }
            if (entry.item && entry.item.parentNode) entry.item.parentNode.removeChild(entry.item);
        }

        function remove(entry) {
            forget(entry);
            entries = entries.filter(function (other) { return other !== entry; });
            var siblings = entries.filter(function (other) { return other.input === entry.input; });
            if (siblings.length && !rebuild(entry.input, siblings.map(function (other) { return other.file; }))) {
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
            var kept = counted ? files.slice(0, Math.max(0, max - entries.length)) : files;
            var overCount = kept.length < files.length;
            if (maxBytes !== null) kept = fitting(kept, files, form, maxBytes);
            var refused = files.length - kept.length;
            say(refused ? (overCount ? countRefusal(max, refused) : bytesRefusal(maxBytes, refused)) : "");
            if (!kept.length) {
                input.value = "";
                changed();
                return;
            }
            var named = rename ? renamed(kept, entries.map(function (entry) { return entry.file.name; })) : null;
            if (named && rebuild(input, named)) {
                kept = named;
            } else if (refused && !rebuild(input, kept)) {
                // No DataTransfer here: part of a selection cannot be posted,
                // so none of it is.
                input.value = "";
                changed();
                return;
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

        if (maxBytes !== null) {
            // The form's other file inputs (Achats' « Des fichiers », « Un
            // dossier entier »): weighed only while a photo waits. Picked
            // after the shots, they used to be counted at the next shot
            // alone, and « Importer » sent past the cap.
            form.addEventListener("change", function (event) {
                var input = event.target;
                if (!input || input.type !== "file") return;
                if (input.matches("[data-photo-slot] input[type=file]") || store.contains(input)) return;
                if (!entries.length) return;
                var picked = (input.files || []).length;
                if (formBytes(form) <= maxBytes) {
                    say("");
                    return;
                }
                if (!picked) return;
                input.value = "";
                sayInView(filesRefusal(maxBytes, picked));
            });
            // The last line, before ui.js's busy label (a document listener,
            // which leaves a submit held back alone): nothing past the cap is
            // sent while a photo waits.
            form.addEventListener("submit", function (event) {
                if (!entries.length || formBytes(form) <= maxBytes) return;
                event.preventDefault();
                sayInView(heldRefusal(maxBytes));
            });
        }

        var count = function () { return entries.length; };
        if (SET_UP) SET_UP.set(box, count);
        return count;
    }

    // `weight` is common.weight's twin: given out so a browser test holds the
    // two to the same words (invoices/tests/test_receipt_camera_browser.py).
    window.MarginMatePhotos = { setUp: setUp, weight: weight };
})();
