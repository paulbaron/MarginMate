/* Moves the browser's storage keys of a session opened before 29/09/2026
 * under the tenant's new scope. base.html loads it for such a session only
 * (accounts.middleware.LEGACY_STORAGE_SESSION_KEY), first thing in the body
 * and not deferred: it runs before any script of the page reads a key.
 *
 * Until then every key carried the tenant's id - « marginmate:espace-3:… »,
 * « mm:espace-3:… » - which told any bar how many tenants were opened before
 * its own (security audit LB-6). The keys now carry an opaque scope
 * (<body data-tenant>, accounts.tenancy.storage_scope). A session opened
 * before is still given its own old id (<body data-tenant-legacy>) - its
 * pages showed it anyway - and this moves THAT tenant's keys only, once: an
 * unsaved stock count, a « Consignes » draft, ticked boxes, opened rows, a
 * table's sort. Another tenant's keys on the same device stay where they
 * are (no page can tell whose they are). A key already written under the
 * new scope wins over the old one. A session opened since is never given
 * the id, and never loads this.
 *
 * Can be deleted two weeks after the deployment (Django's session age):
 * no session from before is left by then.
 */
(function () {
    "use strict";

    var body = document.body;
    var scope = body ? body.getAttribute("data-tenant") : "";
    var legacy = body ? body.getAttribute("data-tenant-legacy") : "";
    if (!scope || !/^[0-9]+$/.test(legacy || "")) return;

    var TENANT_SCOPE = "espace-" + scope + ":";
    var LEGACY_TENANT_SCOPE = "espace-" + legacy + ":";
    // The application's two prefixes: "mm:" (ui.js's data-persist,
    // datatable.js's sort), "marginmate:" (the pages' own keys).
    var OLD_KEY = new RegExp("^(marginmate|mm):" + LEGACY_TENANT_SCOPE + "([\\s\\S]*)$");

    function oldKey(app, rest) { return app + ":" + LEGACY_TENANT_SCOPE + rest; }
    function newKey(app, rest) { return app + ":" + TENANT_SCOPE + rest; }

    function moveIn(area) {
        var found = [];
        for (var index = 0; index < area.length; index++) {
            var match = OLD_KEY.exec(area.key(index) || "");
            if (match) found.push(match);
        }
        found.forEach(function (match) {
            var app = match[1];
            var rest = match[2];
            var value = area.getItem(oldKey(app, rest));
            if (value !== null && area.getItem(newKey(app, rest)) === null) area.setItem(newKey(app, rest), value);
            area.removeItem(oldKey(app, rest));
        });
    }

    // Storage can throw outright (private mode, blocked site data): nothing
    // was kept there to move.
    try { moveIn(window.localStorage); } catch (error) { /* blocked */ }
    try { moveIn(window.sessionStorage); } catch (error) { /* blocked */ }
})();
