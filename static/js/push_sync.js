/* Keeps this browser's notifications alive, on every page of a logged-in
   espace (templates/base.html, in the head, deferred).

   It never asks for the permission and never creates a device: « Activer »
   on Données › Notifications does (static/js/notifications.js). Here, once
   the permission is granted, the page tells the server which subscription
   this browser holds now (POST synchroniser, notifications/devices.py sync):
   - « ok »: the device is known and refreshed;
   - « renew »: the browser must subscribe again (no subscription, another
     server key, a dead endpoint) - done here, at most once an hour, with no
     gesture needed once the permission is granted, then synced again;
   - « unknown »: nothing of this login for this browser - nothing to do.
   iOS forgets a subscription without a word and fires no
   pushsubscriptionchange: this sync at each launch of the app is what
   brings it back.

   At most every 12 hours for the same subscription: the moment and a hash
   of the endpoint (never the endpoint itself) are kept under the espace's
   scope - a draft, forgotten at the logout (static/js/ui.js's DRAFTS), so
   the next login syncs at once and the server takes the device back.

   Plain ES5, no markup written. */
(function () {
    "use strict";

    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return;

    var SCRIPT = document.currentScript;
    var SYNC_URL = (SCRIPT && SCRIPT.getAttribute("data-sync-url")) || "/notifications/appareils/synchroniser/";
    var KEY_URL = (SCRIPT && SCRIPT.getAttribute("data-key-url")) || "/notifications/cle/";
    var TENANT_SCOPE = document.body && document.body.getAttribute("data-tenant") ? "espace-" + document.body.getAttribute("data-tenant") + ":" : "";
    var SYNC_KEY = "marginmate:" + TENANT_SCOPE + "push:synchro";
    var RENEW_KEY = "marginmate:" + TENANT_SCOPE + "push:renouvele";
    var SYNC_EVERY = 12 * 3600 * 1000;
    var RENEW_EVERY = 3600 * 1000;

    function readStamp() {
        try {
            var stored = JSON.parse(window.localStorage.getItem(SYNC_KEY) || "null");
            return stored && typeof stored.at === "number" ? stored : null;
        } catch (error) {
            return null;
        }
    }

    function writeStamp(sha) {
        try {
            window.localStorage.setItem(SYNC_KEY, JSON.stringify({ at: Date.now(), sha: sha }));
        } catch (error) {
            // Storage refused (private mode): the next page syncs again.
        }
    }

    function renewedLately() {
        try {
            var at = Number(window.localStorage.getItem(RENEW_KEY) || "0");
            return Date.now() - at < RENEW_EVERY;
        } catch (error) {
            return true;
        }
    }

    function noteRenewal() {
        try {
            window.localStorage.setItem(RENEW_KEY, String(Date.now()));
        } catch (error) {
            // Nothing kept: renewed at most once per page then.
        }
    }

    function hex(buffer) {
        var bytes = new Uint8Array(buffer);
        var out = "";
        for (var index = 0; index < bytes.length; index++) {
            out += (bytes[index] < 16 ? "0" : "") + bytes[index].toString(16);
        }
        return out;
    }

    /* A hash of the endpoint, null for no subscription. */
    function endpointHash(subscription) {
        if (!subscription) return Promise.resolve(null);
        if (!window.crypto || !window.crypto.subtle || !window.TextEncoder) return Promise.resolve("");
        return window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(subscription.endpoint)).then(function (digest) {
            return hex(digest).slice(0, 32);
        }, function () {
            return "";
        });
    }

    function csrfToken() {
        var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function base64url(buffer) {
        if (!buffer) return null;
        var bytes = new Uint8Array(buffer);
        var binary = "";
        for (var index = 0; index < bytes.length; index++) binary += String.fromCharCode(bytes[index]);
        return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }

    function keyBytes(text) {
        var padded = text.replace(/-/g, "+").replace(/_/g, "/");
        while (padded.length % 4) padded += "=";
        var binary = window.atob(padded);
        var bytes = new Uint8Array(binary.length);
        for (var index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
        return bytes;
    }

    /* What the server needs of a subscription; { endpoint: null } for none. */
    function describe(subscription) {
        if (!subscription) return { endpoint: null };
        var json = subscription.toJSON();
        return {
            endpoint: json.endpoint,
            keys: { p256dh: json.keys && json.keys.p256dh, auth: json.keys && json.keys.auth },
            server_key: base64url(subscription.options && subscription.options.applicationServerKey)
        };
    }

    /* The answer as JSON, or a rejection: a login page (a redirect) or an
       error is no answer. */
    function readJson(response) {
        var type = response.headers.get("Content-Type") || "";
        if (!response.ok || response.redirected || type.indexOf("application/json") !== 0) {
            throw new Error("not an answer");
        }
        return response.json();
    }

    function post(subscription) {
        return fetch(SYNC_URL, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
            body: JSON.stringify(describe(subscription))
        }).then(readJson);
    }

    function stampAfter(subscription) {
        return endpointHash(subscription).then(writeStamp);
    }

    function renew(registration, subscription) {
        if (renewedLately()) return null;
        noteRenewal();
        return fetch(KEY_URL, { credentials: "same-origin" }).then(readJson).then(function (answer) {
            var dropped = subscription ? subscription.unsubscribe() : Promise.resolve(true);
            return dropped.then(function () {
                return registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(answer.key) });
            });
        }).then(function (renewed) {
            return post(renewed).then(function (answer) {
                if (answer.state === "ok" || answer.state === "unknown") return stampAfter(renewed);
                return null;
            });
        });
    }

    function synchronise(registration) {
        if (Notification.permission !== "granted") return null;
        var stamp = readStamp();
        return registration.pushManager.getSubscription().then(function (subscription) {
            return endpointHash(subscription).then(function (sha) {
                if (stamp && Date.now() - stamp.at < SYNC_EVERY && stamp.sha === sha) return null;
                return post(subscription).then(function (answer) {
                    if (answer.state === "ok" || answer.state === "unknown") {
                        writeStamp(sha);
                        return null;
                    }
                    if (answer.state === "renew") return renew(registration, subscription);
                    return null;
                });
            });
        });
    }

    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(synchronise).catch(function () {
        // A network error, a refused answer: nothing kept, the next page tries again.
    });
})();
