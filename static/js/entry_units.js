/* One entry's units in its <select>: the stock take's rows and the shopping
 * list's add form. The choices are inventory/entries.py's, shipped by the page
 * in a json_script island ({name: {kind, unit_choices: [[value, label]],
 * default_unit}}).
 *
 * - lookup(data, typed, aliases): the entry `typed` names - its spaces around
 *   taken off, as the save takes them off - when it is one of the island's
 *   own names; else, only when `aliases` is handed in (the shopping list's
 *   second island, {exact: {name: island name}, folded: {key: island
 *   name}}: names its server resolves forgivingly, each kept only when it
 *   names that very entry), the island name an alias gives it - exactly,
 *   then folded (case, accents and spacing aside, entries.same_name's
 *   fold); null otherwise. The stock take hands no aliases: its server is
 *   exact, and so is its lookup.
 * - fill(select, entry, useDefault): the select offers the entry's units and
 *   takes its default_unit - or keeps its value, when useDefault is false
 *   and that value is still offered (a saved line's unit as the page opens).
 *   With no entry (a name nobody knows, a free text), the options the server
 *   drew are put back, as the first call on that select found them (value,
 *   text, selected): never the previous entry's units under a name that has
 *   none.
 * - A select[data-entry-units][data-entry-field] wires itself, for a page
 *   with no script of its own: it reads the island whose id
 *   data-entry-units names - and the aliases' whose id data-entry-aliases
 *   names, when it says one -, once, and follows the input and change
 *   events of its form's field named by data-entry-field. A name that still
 *   names the entry on screen leaves the unit chosen as it is.
 *
 * window.MarginMateEntryUnits = { lookup, fill }. Nodes and text only - a
 * product's name and a label are never read as markup - and nothing is kept
 * beyond the page.
 */
(function () {
    "use strict";

    // The options each select was drawn with, kept by fill() the first time
    // it meets the select, and whether they are on screen now.
    var drawn = new WeakMap();
    // The selects already following their field.
    var wired = new WeakMap();
    // Each island, read once: {id: data or null}.
    var islands = {};
    // The accents NFD splits off a letter (U+0300 to U+036F).
    var COMBINING_MARKS = new RegExp("[" + String.fromCharCode(0x0300) + "-" + String.fromCharCode(0x036f) + "]", "g");

    function owns(object, key) {
        return !!object && typeof object === "object" && Object.prototype.hasOwnProperty.call(object, key);
    }

    // A name as entries.same_name reads it: case, accents and spacing aside.
    function fold(text) {
        return text.toLowerCase().normalize("NFD").replace(COMBINING_MARKS, "").replace(/\s+/g, " ").trim();
    }

    function lookup(data, typed, aliases) {
        if (!data || typeof typed !== "string") return null;
        var name = typed.trim();
        if (!name) return null;
        if (Object.prototype.hasOwnProperty.call(data, name)) return data[name] || null;
        if (!aliases) return null;
        var found = null;
        if (owns(aliases.exact, name)) {
            found = aliases.exact[name];
        } else if (owns(aliases.folded, fold(name))) {
            found = aliases.folded[fold(name)];
        }
        return typeof found === "string" && owns(data, found) ? data[found] || null : null;
    }

    function remember(select) {
        var state = drawn.get(select);
        if (state) return state;
        var options = [];
        for (var index = 0; index < select.options.length; index++) {
            var option = select.options[index];
            options.push({
                value: option.value,
                text: option.textContent,
                selected: option.selected,
                defaultSelected: option.defaultSelected
            });
        }
        state = { options: options, replaced: false };
        drawn.set(select, state);
        return state;
    }

    function empty(select) {
        while (select.firstChild) select.removeChild(select.firstChild);
    }

    function addOption(select, value, text) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        select.appendChild(option);
        return option;
    }

    function choicesOf(entry) {
        var choices = entry ? entry.unit_choices : null;
        return Array.isArray(choices) && choices.length ? choices : null;
    }

    function offers(choices, value) {
        for (var index = 0; index < choices.length; index++) {
            if (String(choices[index][0]) === value) return true;
        }
        return false;
    }

    function putBack(select, state) {
        empty(select);
        state.options.forEach(function (kept) {
            var option = addOption(select, kept.value, kept.text);
            option.defaultSelected = kept.defaultSelected;
            option.selected = kept.selected;
        });
        state.replaced = false;
    }

    function fill(select, entry, useDefault) {
        if (!select) return;
        var state = remember(select);
        var choices = choicesOf(entry);
        if (!choices) {
            if (state.replaced) putBack(select, state);
            return;
        }
        var current = select.value;
        empty(select);
        choices.forEach(function (choice) {
            addOption(select, String(choice[0]), String(choice[1]));
        });
        state.replaced = true;
        var wanted = !useDefault && offers(choices, current) ? current : String(entry.default_unit);
        select.value = offers(choices, wanted) ? wanted : String(choices[0][0]);
    }

    function island(id) {
        if (Object.prototype.hasOwnProperty.call(islands, id)) return islands[id];
        var node = id ? document.getElementById(id) : null;
        var data = null;
        if (node) {
            try {
                data = JSON.parse(node.textContent);
            } catch (error) {
                data = null;  // a page whose island is broken offers its server's options
            }
        }
        islands[id] = data;
        return data;
    }

    function wire(select) {
        if (wired.has(select)) return;
        var name = select.getAttribute("data-entry-field");
        var field = select.form && name ? select.form.elements.namedItem(name) : null;
        if (!field || typeof field.value !== "string" || !field.addEventListener) return;
        wired.set(select, true);
        var data = island(select.getAttribute("data-entry-units"));
        // The names its server also accepts, when the select names them.
        var aliasesId = select.getAttribute("data-entry-aliases");
        var aliases = aliasesId ? island(aliasesId) : null;
        // The entry the select shows now. The field's change fires again as
        // it loses the focus - often after a unit was chosen - and the same
        // name must not take that choice back to its default.
        var shown = null;
        var follow = function () {
            var entry = lookup(data, field.value, aliases);
            if (entry && entry === shown) return;
            shown = entry;
            fill(select, entry, true);
        };
        field.addEventListener("input", follow);
        field.addEventListener("change", follow);
        // A name already there (the browser gave the form back): its units,
        // the select's own value kept when it is one of them.
        if (field.value.trim()) {
            shown = lookup(data, field.value, aliases);
            fill(select, shown, false);
        }
    }

    function wireAll() {
        var selects = document.querySelectorAll("select[data-entry-units][data-entry-field]");
        for (var index = 0; index < selects.length; index++) wire(selects[index]);
    }

    window.MarginMateEntryUnits = { lookup: lookup, fill: fill };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", wireAll);
    } else {
        wireAll();
    }
})();
