/* « Format du relevé » (bank/_statement_format_fields.html): what a CSV alone
   needs - its separator, dates, decimals, columns and account pattern - is
   shown for a CSV and hidden for a file that says where each datum is (OFX,
   CAMT.053). Disabled too, as the server draws it for the kind it shows: a
   disabled field is never sent, and an empty required one of the hidden part
   would stop the browser sending the form, silently. Each section names the
   kinds it is for in `data-file-types`. Writes no markup. */
(function () {
    function show(select) {
        var form = select.form;
        if (!form) return;
        form.querySelectorAll("[data-file-types]").forEach(function (section) {
            var kinds = section.getAttribute("data-file-types").split(/\s+/);
            section.hidden = section.disabled = kinds.indexOf(select.value) === -1;
        });
    }
    document.addEventListener("change", function (event) {
        if (event.target.matches("select[name=file_type]")) show(event.target);
    });
    function init() {
        document.querySelectorAll("select[name=file_type]").forEach(show);
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
