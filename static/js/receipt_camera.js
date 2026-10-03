/* « Prendre une photo » on the receipt import form
 * (invoices/_receipt_upload_form.html, shared by « Factures » - purchases.html,
 * through its import card _import_card.html - and « Ajouter des factures » -
 * invoice_add.html, directly): the shots kept and previewed in the form by
 * static/js/photos.js, loaded deferred just before this file, so both run
 * before DOMContentLoaded and photos.js is there first. A shot lives in the
 * page alone (a camera input usually puts it in no gallery), so:
 * - while a photo waits, « Importer » says what Consignes says as its photos
 *   go up: a phone sending them over a slow network takes a while;
 * - leaving the page with a photo waiting asks first (the browser's own
 *   prompt, as the timesheet's grid does) - a document's supplier link, the
 *   topbar, a chip all navigate; a tab switch is an htmx swap and keeps them;
 * - how many wait is noted for this tab (sessionStorage: another tab of
 *   Achats has photos of its own), and a page drawn again with none says
 *   they were lost - Android may drop the tab while its camera app is in
 *   front, and the page then comes back empty, with nothing saying so.
 *   Consignes' draft says the same (« N photos à reprendre »).
 * Every lookup guarded: no form, no photos.js, nothing to do.
 */
(function () {
    "use strict";

    var PHOTOS_BUSY = "Envoi des photos… gardez la page ouverte";
    var TENANT_SCOPE = document.body.getAttribute("data-tenant") ? "espace-" + document.body.getAttribute("data-tenant") + ":" : "";
    var PENDING_KEY = "marginmate:" + TENANT_SCOPE + "achats:pending-photos";
    // A note older than this says nothing (Consignes' draft keeps 12 h).
    var PENDING_FOR = 12 * 60 * 60 * 1000;

    function lostSentence(lost) {
        return lost > 1
            ? lost + " photos prises n'ont pas été importées : la page a été quittée ou rechargée avant « Importer ». Reprenez-les."
            : "1 photo prise n'a pas été importée : la page a été quittée ou rechargée avant « Importer ». Reprenez-la.";
    }

    function setUpCamera() {
        var form = document.querySelector("form[data-receipt-upload]");
        if (!form || !window.MarginMatePhotos) return;
        var count = window.MarginMatePhotos.setUp(form);
        var button = form.querySelector("button[data-busy-label]");
        var plain = button ? button.getAttribute("data-busy-label") : null;
        var refusal = form.querySelector("[data-photo-refused]");
        var leaving = false;

        function remember() {
            try {
                if (count() > 0) sessionStorage.setItem(PENDING_KEY, JSON.stringify({ count: count(), at: Date.now() }));
                else sessionStorage.removeItem(PENDING_KEY);
            } catch (error) { /* not noted */ }
        }

        function forget() {
            try { sessionStorage.removeItem(PENDING_KEY); } catch (error) { /* nothing noted */ }
        }

        // Said once, on a box drawn empty.
        function sayLost() {
            var note = null;
            try { note = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null"); } catch (error) { note = null; }
            forget();
            if (!note || !refusal || count() > 0) return;
            var lost = parseInt(note.count, 10) || 0;
            if (lost < 1 || !(Date.now() - Number(note.at) < PENDING_FOR)) return;
            refusal.textContent = lostSentence(lost);
            refusal.hidden = false;
        }

        form.addEventListener("photos-changed", function () {
            if (button) button.setAttribute("data-busy-label", count() > 0 ? PHOTOS_BUSY : plain);
            remember();
        });
        // After photos.js's own listener, set up above: a post it held back
        // (past what one post may carry) leaves nothing.
        form.addEventListener("submit", function (event) {
            if (event.defaultPrevented) return;
            leaving = true;
            forget();
        });
        window.addEventListener("beforeunload", function (event) {
            if (leaving || count() === 0) return;
            event.preventDefault();
            event.returnValue = "";
        });
        // Back to a page the browser kept in memory, its photos with it.
        window.addEventListener("pageshow", function (event) {
            if (!event.persisted) return;
            leaving = false;
            remember();
        });
        sayLost();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setUpCamera);
    else setUpCamera();
})();
