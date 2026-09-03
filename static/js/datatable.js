/* Search and per-column sorting for any table, with no dependencies.
 *
 *     <table data-table data-table-label="factures"> ... </table>
 *
 * gives that table a search box, a live "12 / 261" count, and a sort button in
 * every header. Opt a column out with `data-no-sort` on its <th>.
 *
 * Sorting is by the cell's own text, which is what the reader sees, with two
 * escape hatches for when that isn't sortable:
 *
 *   - `data-sort="..."` on a <td> is used verbatim (numeric if it parses).
 *     That's how a date renders as "31/03/2026" but sorts as 2026-03-31.
 *   - Otherwise the text is parsed as a French-formatted number - "1 234,56 €",
 *     "12,5 %", "x4.5", "−3" - and falls back to a locale-aware string compare.
 *
 * Rows that belong to the row above (an expanded detail panel, a "valued in X"
 * note) carry `data-child-row` and travel with their parent through both
 * sorting and filtering, so a table can still have that structure.
 */
(function () {
    "use strict";

    var COLLATOR = new Intl.Collator("fr", { sensitivity: "base", numeric: true });

    /** Text of a cell, ignoring anything marked as not part of the value. */
    function cellText(cell) {
        if (!cell) return "";
        var explicit = cell.getAttribute("data-sort");
        if (explicit !== null) return explicit.trim();
        return (cell.textContent || "").replace(/\s+/g, " ").trim();
    }

    /**
     * A number if the text is one, otherwise null.
     *
     * Deliberately strict about what counts: "70CL" must NOT parse as 70, or a
     * column of product names sorts by a size buried in the middle of them.
     * Currency, percent, the "x" of a price factor and a leading sign are
     * stripped because those are formatting, not content.
     */
    function asNumber(text) {
        if (!text) return null;
        var cleaned = text
            .replace(/[  \s]/g, "")   // ordinary, non-breaking and narrow spaces
            .replace(/[€%]/g, "")
            .replace(/^x/i, "")
            .replace(/^[−–—]/, "-")             // real minus signs, not hyphens
            .replace(",", ".");
        if (cleaned === "" || cleaned === "-") return null;
        if (!/^-?\d+(\.\d+)?$/.test(cleaned)) return null;
        var value = parseFloat(cleaned);
        return isNaN(value) ? null : value;
    }

    /** dd/mm/yyyy (the only date format this app renders) as a sortable number. */
    function asDate(text) {
        var match = /^(\d{2})\/(\d{2})\/(\d{4})/.exec(text);
        if (!match) return null;
        return Number(match[3] + match[2] + match[1]);
    }

    function compare(a, b) {
        var da = asDate(a), db = asDate(b);
        if (da !== null && db !== null) return da - db;

        var na = asNumber(a), nb = asNumber(b);
        if (na !== null && nb !== null) return na - nb;
        // Blanks and dashes sort last whichever direction you're going: an
        // empty cell is "no value", not "the smallest value".
        var ea = a === "" || a === "—" || a === "-";
        var eb = b === "" || b === "—" || b === "-";
        if (ea !== eb) return ea ? 1 : -1;
        return COLLATOR.compare(a, b);
    }

    /** Rows grouped with the child rows that belong to them. */
    function rowGroups(tbody) {
        var groups = [];
        Array.prototype.forEach.call(tbody.rows, function (row) {
            if (row.hasAttribute("data-child-row") && groups.length) {
                groups[groups.length - 1].children.push(row);
            } else {
                groups.push({ row: row, children: [] });
            }
        });
        return groups;
    }

    function enhance(table) {
        var tbody = table.tBodies[0];
        var head = table.tHead;
        if (!tbody || !head || !head.rows.length) return;

        var headerRow = head.rows[head.rows.length - 1];
        var groups = rowGroups(tbody);
        var label = table.getAttribute("data-table-label") || "lignes";
        // `data-table-sort-only` is for tables that already have their own
        // search - the stock list's is server-backed and fuzzy, and a second
        // box filtering the same rows differently would be worse than none.
        var toolbar = table.hasAttribute("data-table-sort-only") ? null : buildToolbar(table, label);

        function visibleCount() {
            return groups.filter(function (g) { return !g.row.classList.contains("search-hidden"); }).length;
        }

        function filter(query) {
            var needle = query.trim().toLowerCase();
            groups.forEach(function (group) {
                // Search the whole row, children included: an expanded panel's
                // contents are part of what that row is about.
                var haystack = [group.row].concat(group.children)
                    .map(function (r) { return (r.textContent || "").toLowerCase(); })
                    .join(" ");
                var hidden = needle !== "" && haystack.indexOf(needle) === -1;
                group.row.classList.toggle("search-hidden", hidden);
                group.children.forEach(function (child) {
                    // A child that was collapsed stays collapsed - only the
                    // search's own hiding is ours to undo.
                    child.classList.toggle("search-hidden", hidden);
                });
            });
            if (toolbar) toolbar.setCount(visibleCount(), groups.length, needle !== "");
        }

        function sortBy(index, direction) {
            var decorated = groups.map(function (group, position) {
                return { group: group, key: cellText(group.row.cells[index]), position: position };
            });
            decorated.sort(function (a, b) {
                var result = compare(a.key, b.key);
                // Ties keep the order the server sent, which is usually
                // meaningful (newest first, or alphabetical).
                return (result !== 0 ? result : a.position - b.position) * direction;
            });
            var fragment = document.createDocumentFragment();
            decorated.forEach(function (entry) {
                fragment.appendChild(entry.group.row);
                entry.group.children.forEach(function (child) { fragment.appendChild(child); });
            });
            tbody.appendChild(fragment);
        }

        Array.prototype.forEach.call(headerRow.cells, function (th, index) {
            if (th.hasAttribute("data-no-sort") || !(th.textContent || "").trim()) return;
            th.classList.add("sortable");
            th.setAttribute("aria-sort", "none");
            var button = document.createElement("button");
            button.type = "button";
            button.innerHTML = th.innerHTML;
            button.title = "Trier par " + (th.textContent || "").trim();
            th.textContent = "";
            th.appendChild(button);

            button.addEventListener("click", function () {
                var current = th.getAttribute("aria-sort");
                var next = current === "ascending" ? "descending" : "ascending";
                applySort(index, next === "ascending" ? 1 : -1);
                saveSort(table, index, next === "ascending" ? 1 : -1);
            });
        });

        function applySort(index, direction) {
            Array.prototype.forEach.call(headerRow.cells, function (other) {
                if (other.hasAttribute("aria-sort")) other.setAttribute("aria-sort", "none");
            });
            var th = headerRow.cells[index];
            if (!th || !th.hasAttribute("aria-sort")) return;
            th.setAttribute("aria-sort", direction === 1 ? "ascending" : "descending");
            sortBy(index, direction);
        }

        var saved = readSort(table);
        if (saved) applySort(saved.index, saved.direction);

        if (toolbar) {
            toolbar.input.addEventListener("input", function () { filter(toolbar.input.value); });
            // ui.js restores the box's TEXT while the toolbar is being built,
            // which is before this listener exists - so a restored filter
            // would show its own search term over a completely unfiltered
            // table. Apply whatever it put there.
            filter(toolbar.input.value);
        }
    }

    /** Stable per-table key: its id when it has one, else its position. */
    function tableKey(table) {
        if (table.id) return table.id;
        var all = Array.prototype.slice.call(document.querySelectorAll("table[data-table]"));
        return "t" + all.indexOf(table);
    }

    function sortKeyFor(table) { return "mm:" + location.pathname + ":table-sort:" + tableKey(table); }

    function saveSort(table, index, direction) {
        try { sessionStorage.setItem(sortKeyFor(table), index + ":" + direction); } catch (e) { /* blocked */ }
    }

    function readSort(table) {
        var raw;
        try { raw = sessionStorage.getItem(sortKeyFor(table)); } catch (e) { return null; }
        if (!raw) return null;
        var parts = raw.split(":");
        return { index: Number(parts[0]), direction: Number(parts[1]) };
    }

    function buildToolbar(table, label) {
        var toolbar = document.createElement("div");
        toolbar.className = "table-toolbar";

        var search = document.createElement("div");
        search.className = "table-search";
        var input = document.createElement("input");
        input.type = "search";
        input.setAttribute("autocomplete", "off");
        input.setAttribute("aria-label", "Rechercher dans les " + label);
        // ui.js restores this on reload. Session-scoped, so a filter typed
        // today doesn't quietly hide rows next week - see its header.
        input.setAttribute("data-persist", "table-search:" + tableKey(table));
        input.placeholder = "Rechercher…";
        search.appendChild(input);

        var count = document.createElement("span");
        count.className = "table-count";
        count.setAttribute("aria-live", "polite");

        toolbar.appendChild(search);
        toolbar.appendChild(count);

        var anchor = table.closest(".table-wrap") || table;
        anchor.parentNode.insertBefore(toolbar, anchor);

        // This input is created here, AFTER ui.js has already swept the page
        // for [data-persist] elements - so it has to hand itself over, or the
        // search box is the one thing on the page that forgets itself.
        if (window.MarginMateUI) window.MarginMateUI.init(toolbar);

        return {
            input: input,
            setCount: function (shown, total, filtering) {
                count.textContent = filtering
                    ? shown + " / " + total + " " + label
                    : total + " " + label;
            },
        };
    }

    function init(root) {
        (root || document).querySelectorAll("table[data-table]").forEach(function (table) {
            if (table.hasAttribute("data-table-ready")) return;
            table.setAttribute("data-table-ready", "");
            enhance(table);
        });
    }

    document.addEventListener("DOMContentLoaded", function () { init(document); });
    // Tables that arrive later (an htmx swap, an expanded panel) get the same
    // treatment without the page needing to know about them.
    document.body && document.addEventListener("htmx:afterSwap", function (event) { init(event.target); });

    window.MarginMateTable = { init: init };
})();
