/* Shared behaviour: remembering UI state across reloads, and the job console.
 *
 * ---------------------------------------------------------------- state
 *
 * Anything with `data-persist="key"` keeps its state when the page reloads.
 * Works on checkboxes, radios, text inputs, selects and <details>.
 *
 * Two stores, because "remember this" means two different things:
 *
 *   sessionStorage  transient VIEW state - a table's search text and sort.
 *                   Right for a reload or a form round trip, wrong for
 *                   next week: nobody wants to open the invoices page and
 *                   silently see 7 of 261 rows because of something they
 *                   typed in another session.
 *   localStorage    deliberate PREFERENCES - which sources are ticked,
 *                   whether the log is expanded, which categories are open.
 *                   Those are decisions, and re-making them every visit is
 *                   the annoyance. Opt in with `data-persist-durable`.
 *
 * Keys are scoped by pathname, so two pages can both have a "search" without
 * one clobbering the other.
 */
(function () {
    "use strict";

    function store(element) {
        return element.hasAttribute("data-persist-durable") ? localStorage : sessionStorage;
    }

    function keyFor(element) {
        return "mm:" + location.pathname + ":" + element.getAttribute("data-persist");
    }

    /** Storage can throw outright in private mode, and a remembered filter is
     *  never worth taking the page down for. */
    function safeGet(element) {
        try { return store(element).getItem(keyFor(element)); } catch (e) { return null; }
    }
    function safeSet(element, value) {
        try { store(element).setItem(keyFor(element), value); } catch (e) { /* full or blocked */ }
    }

    function restore(element) {
        var saved = safeGet(element);
        if (saved === null) return;
        if (element.tagName === "DETAILS") {
            element.open = saved === "1";
        } else if (element.type === "checkbox" || element.type === "radio") {
            element.checked = saved === "1";
        } else {
            element.value = saved;
        }
        // Let whatever listens to this field react to the restored value -
        // a restored search box has to actually filter, not just look filled.
        element.dispatchEvent(new Event("change", { bubbles: true }));
        if (element.tagName === "INPUT" && element.type !== "checkbox" && element.type !== "radio") {
            element.dispatchEvent(new Event("input", { bubbles: true }));
        }
    }

    function watch(element) {
        if (element.hasAttribute("data-persist-ready")) return;
        element.setAttribute("data-persist-ready", "");
        restore(element);
        var event = element.tagName === "DETAILS" ? "toggle" : "change";
        element.addEventListener(event, function () {
            if (element.tagName === "DETAILS") return safeSet(element, element.open ? "1" : "0");
            if (element.type === "checkbox" || element.type === "radio") {
                return safeSet(element, element.checked ? "1" : "0");
            }
            safeSet(element, element.value);
        });
        if (element.tagName === "INPUT" && element.type === "search") {
            element.addEventListener("input", function () { safeSet(element, element.value); });
        }
    }

    function initPersistence(root) {
        (root || document).querySelectorAll("[data-persist]").forEach(watch);
    }

    // ------------------------------------------------------------ console

    function initConsoles(root) {
        (root || document).querySelectorAll("[data-job-console]").forEach(function (console_) {
            var box = console_.querySelector("[data-log-scroll]");
            // A running job's newest line is the one worth seeing, and it's at
            // the bottom.
            if (box && console_.hasAttribute("data-running")) box.scrollTop = box.scrollHeight;

            var copy = console_.querySelector("[data-copy-log]");
            if (copy && !copy.hasAttribute("data-ready")) {
                copy.setAttribute("data-ready", "");
                copy.addEventListener("click", function () {
                    var text = box ? box.textContent : "";
                    var done = function () {
                        var original = copy.textContent;
                        copy.textContent = "Copié ✓";
                        setTimeout(function () { copy.textContent = original; }, 1500);
                    };
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(text).then(done, function () {});
                    }
                });
            }
        });
    }

    // ------------------------------------------------------- bulk select

    /** Row checkboxes with a bar that appears once something is ticked.
     *
     * The bar only exists while there's a selection, so it never sits there
     * as another thing to read - and its count is the reassurance you need
     * before pressing something destructive on a hundred rows at once. */
    function initBulk(root) {
        (root || document).querySelectorAll("[data-bulk-bar]").forEach(function (bar) {
            if (bar.hasAttribute("data-bulk-ready")) return;
            bar.setAttribute("data-bulk-ready", "");

            var scope = bar.parentElement;
            var items = function () {
                return Array.prototype.slice.call(scope.querySelectorAll("[data-bulk-item]"));
            };
            var countEl = bar.querySelector("[data-bulk-count]");
            var all = scope.querySelector("[data-bulk-all]");

            function refresh() {
                // Only rows the search left visible can be selected - acting
                // on a row you can't see is exactly the kind of surprise a
                // bulk action must never spring.
                var checked = items().filter(function (item) {
                    var row = item.closest("tr");
                    if (row && row.classList.contains("search-hidden")) {
                        item.checked = false;
                        return false;
                    }
                    return item.checked;
                });
                bar.hidden = checked.length === 0;
                if (countEl) countEl.textContent = checked.length;
                if (all) {
                    var visible = items().filter(function (i) {
                        var row = i.closest("tr");
                        return !row || !row.classList.contains("search-hidden");
                    });
                    all.checked = visible.length > 0 && checked.length === visible.length;
                    all.indeterminate = checked.length > 0 && checked.length < visible.length;
                }
            }

            scope.addEventListener("change", function (event) {
                if (event.target === all) {
                    items().forEach(function (item) {
                        var row = item.closest("tr");
                        if (row && row.classList.contains("search-hidden")) return;
                        item.checked = all.checked;
                    });
                }
                if (event.target.hasAttribute && (event.target.hasAttribute("data-bulk-item") || event.target === all)) {
                    refresh();
                }
            });
            scope.addEventListener("input", refresh);  // the table search
            var clear = bar.querySelector("[data-bulk-clear]");
            if (clear) {
                clear.addEventListener("click", function () {
                    items().forEach(function (item) { item.checked = false; });
                    refresh();
                });
            }
            refresh();
        });
    }

    function init(root) {
        initPersistence(root);
        initConsoles(root);
        initBulk(root);
    }

    document.addEventListener("DOMContentLoaded", function () { init(document); });
    // htmx replaces the whole status card every second while a job runs, so
    // the restored <details> state and the scroll position have to be
    // re-applied to the new markup each time.
    document.addEventListener("htmx:afterSwap", function (event) { init(event.target); });

    window.MarginMateUI = { init: init };
})();
