/* The login page (accounts/templates/accounts/login.html): a fragment of
   the page that sent here - a notification opening « /consignes/#new-pickup »
   on a phone whose session ended - never reaches the server, but the
   browser keeps it on this page's address. Put back on the hidden `next`,
   it survives the login: the redirect lands on the reprise's form, not on
   the top of Consignes.

   Plain ES5, nothing written but the field's value. */
(function () {
    "use strict";

    document.addEventListener("DOMContentLoaded", function () {
        var hash = window.location.hash;
        if (!hash || hash.length < 2) return;
        var fields = document.querySelectorAll("form input[type=hidden][name=next]");
        for (var index = 0; index < fields.length; index++) {
            var next = fields[index].value;
            if (next && next.indexOf("#") === -1) fields[index].value = next + hash;
        }
    });
})();
