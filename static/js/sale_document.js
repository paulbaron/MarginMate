// « Facture de vente »'s page (recipes/templates/recipes/sale_document_form.html):
// conveniences over a page that works without them.
//
// * « + Ajouter une ligne » clones the empty row (<template id="empty-sale-line">,
//   « __prefix__ » its index) at the next index - TOTAL_FORMS, then one more -
//   and never renumbers a row: a gap in the indices is what the formset is
//   tested for (recipes/tests/test_sale_document_pages.py::NonContiguousTests).
//   Without this script the button posts and the server draws one more row.
// * Nothing typed is lost without a word: once a field of the document's
//   form has changed, every form carrying `data-leaves-lines` - the head's
//   « Supprimer », and those of « Règlement » - asks that sentence first,
//   then its own `data-confirm`. ui.js leaves such a form to this script, so
//   each question is asked once. A page drawn from a refused save (or the
//   no-JS « + Ajouter une ligne ») carries `data-unsaved` on the document's
//   form and starts out edited: everything on it is typed and unsaved.
// * What is written for a reader without JavaScript (.no-js-only) is hidden.
//
// Nodes and text only: a row is cloned from its <template>, never written as
// markup (tests/test_ui.py::SaleDocumentScriptTests).
(function () {
    "use strict";

    var PREFIX = /__prefix__/g;
    var NUMBERED = ["name", "id", "for"];
    var edited = false;

    function renumber(element, index) {
        for (var i = 0; i < NUMBERED.length; i++) {
            var value = element.getAttribute(NUMBERED[i]);
            if (value && value.indexOf("__prefix__") !== -1) {
                element.setAttribute(NUMBERED[i], value.replace(PREFIX, String(index)));
            }
        }
    }

    function addRow(event) {
        var template = document.getElementById("empty-sale-line");
        var rows = document.getElementById("sale-line-rows");
        var total = document.querySelector("input[name='lines-TOTAL_FORMS']");
        // A browser with no <template> posts the button: the server adds the row.
        if (!template || !template.content || !rows || !total) return;
        var index = parseInt(total.value, 10);
        if (isNaN(index)) return;
        event.preventDefault();
        var row = document.importNode(template.content, true);
        var first = row.firstElementChild;
        var elements = row.querySelectorAll("[name], [id], [for]");
        for (var i = 0; i < elements.length; i++) renumber(elements[i], index);
        total.value = String(index + 1);
        rows.appendChild(row);
        edited = true;
        var label = first && first.querySelector("input[name$='-label']");
        if (label) label.focus();
    }

    function askBeforeLeaving(form) {
        form.addEventListener("submit", function (event) {
            if (edited && !window.confirm(form.getAttribute("data-leaves-lines"))) {
                event.preventDefault();
                return;
            }
            var question = form.getAttribute("data-confirm");
            if (question && !window.confirm(question)) event.preventDefault();
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        var form = document.querySelector("form.sale-document-form");
        if (form) {
            edited = form.hasAttribute("data-unsaved");
            form.addEventListener("input", function () { edited = true; });
            form.addEventListener("change", function () { edited = true; });
        }
        var buttons = document.querySelectorAll("button[name='ajouter_ligne']");
        for (var i = 0; i < buttons.length; i++) buttons[i].addEventListener("click", addRow);
        var leaving = document.querySelectorAll("form[data-leaves-lines]");
        for (var j = 0; j < leaving.length; j++) askBeforeLeaving(leaving[j]);
        var notes = document.querySelectorAll(".no-js-only");
        for (var k = 0; k < notes.length; k++) notes[k].hidden = true;
    });
})();
