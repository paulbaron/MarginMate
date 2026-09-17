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
        initShopChoices(root);
    }

    // The new shop's name and header show once "Nouvelle enseigne" is picked.
    function showNewShop(select) {
        var fields = select.parentNode.querySelector("[data-new-shop]");
        if (!fields) return;
        var chosen = select.value === "new";
        fields.hidden = !chosen;
        fields.querySelector("input[name='new_name']").required = chosen;
    }
    document.addEventListener("change", function (event) {
        if (event.target.matches("select[data-shop-select]")) showNewShop(event.target);
    });
    function initShopChoices(root) {
        root.querySelectorAll("select[data-shop-select]").forEach(showNewShop);
    }

    // A form with `data-confirm` asks first. The correction page's forms ask
    // their own question (data-leaves-lines), with the unsaved lines in it.
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches("form[data-confirm]:not([data-leaves-lines])")) return;
        if (!window.confirm(form.getAttribute("data-confirm"))) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }
    }, true);

    // A button with `data-busy-label` says so while its form's request runs
    // (a ticket's OCR is seconds), and can't be pressed a second time.
    document.addEventListener("submit", function (event) {
        var button = event.target.querySelector("button[data-busy-label]");
        if (!button || event.defaultPrevented) return;
        setTimeout(function () {
            button.setAttribute("data-idle-label", button.textContent);
            button.disabled = true;
            button.textContent = button.getAttribute("data-busy-label");
        }, 0);
    });
    // Back to a page the browser kept in memory: the request is long over.
    window.addEventListener("pageshow", function (event) {
        if (!event.persisted) return;
        document.querySelectorAll("button[data-idle-label]").forEach(function (button) {
            button.disabled = false;
            button.textContent = button.getAttribute("data-idle-label");
            button.removeAttribute("data-idle-label");
        });
    });

    document.addEventListener("DOMContentLoaded", function () { init(document); });
    // htmx replaces the whole status card every second while a job runs, so
    // the restored <details> state and the scroll position have to be
    // re-applied to the new markup each time.
    document.addEventListener("htmx:afterSwap", function (event) { init(event.target); });

    window.MarginMateUI = { init: init };
})();

// Bulk selection for deletion (invoice list, receipt queue). The tick boxes
// point at their form with form="..." so a table can stay a table. "Tout
// sélectionner" only ticks what the search filter is showing; anything ticked
// and then filtered out still shows on the confirmation page, which lists
// exactly what will be deleted before anything is.
(function () {
    function setUp(form) {
        var id = form.id;
        var selector = "input[data-bulk-item][form='" + id + "']";
        var all = document.querySelector("input[data-bulk-all][form='" + id + "']");
        var count = form.querySelector("[data-bulk-count]");
        var submit = form.querySelector("[data-bulk-submit]");
        function items() { return Array.prototype.slice.call(document.querySelectorAll(selector)); }
        function refresh() {
            var ticked = items().filter(function (item) { return item.checked; }).length;
            if (submit) submit.disabled = ticked === 0;
            if (count) {
                count.textContent = ticked
                    ? ticked + " sélectionné" + (ticked > 1 ? "s" : "")
                    : (count.getAttribute("data-empty") || "");
            }
        }
        if (all) {
            all.addEventListener("change", function () {
                items().forEach(function (item) {
                    if (item.offsetParent !== null) item.checked = all.checked;
                });
                refresh();
            });
        }
        document.addEventListener("change", function (event) {
            if (event.target.matches(selector)) refresh();
        });
        refresh();
    }
    function init() {
        document.querySelectorAll("form[data-bulk-select]").forEach(setUp);
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
