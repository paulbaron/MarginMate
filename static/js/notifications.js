/* Données › Notifications (notifications/templates/notifications/*.html).

   « Cet appareil » (#cet-appareil): every state is in the page, hidden but
   « JavaScript requis »; this script shows exactly one, in this order:
   1. an iPhone or iPad outside the installed app: how to install it;
   2. the installed app without push: update iOS;
   3. no service worker, push or notifications: this browser cannot;
   4. notifications blocked: where to allow them;
   5. no subscription yet: « Activer » (disabled until the service worker is
      ready, « Rechargez la page. » past 10 s);
   6. a subscription: synced with the server at once - « ok » is « Activées »
      with « Envoyer un essai » and « Désactiver »; « renew » subscribes again
      silently; « unknown » is « À réactiver » with « Activer ».

   « Activer » calls pushManager.subscribe() FIRST in the click handler (iOS
   asks for the permission only from a gesture, and only then), with the
   registration and the key prepared beforehand - the key from the page's
   push-config island. Then POST inscrire, then the state is drawn again.

   On the reminder and alert forms: « Page à ouvrir » shows its path field
   only for « Autre page du site… » (without this script both show).

   Plain ES5, nodes and text only, nothing kept in the browser's storage. */
(function () {
    "use strict";

    var READY_TIMEOUT = 10000;
    // What the answer line says: the server's own French refusal, or one of
    // these - never a rejection's message (« not an answer », « Failed to
    // fetch » are English, and say nothing to the owner).
    var NOT_REGISTERED = "Activé dans le navigateur mais pas enregistré : réessayez.";
    var SUBSCRIBE_FAILED = "Inscription auprès du service de notification impossible : réessayez.";
    var NOT_ALLOWED = "Notifications non autorisées : touchez Activer et acceptez la demande.";

    function csrfToken() {
        var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function readJson(response) {
        var type = response.headers.get("Content-Type") || "";
        if (response.redirected || type.indexOf("application/json") !== 0) throw new Error("not an answer");
        return response.json().then(function (data) {
            return { ok: response.ok, data: data };
        });
    }

    function postJson(url, body) {
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
            body: JSON.stringify(body || {})
        }).then(readJson);
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

    function describe(subscription) {
        if (!subscription) return { endpoint: null };
        var json = subscription.toJSON();
        return {
            endpoint: json.endpoint,
            keys: { p256dh: json.keys && json.keys.p256dh, auth: json.keys && json.keys.auth },
            server_key: base64url(subscription.options && subscription.options.applicationServerKey)
        };
    }

    // -- « Cet appareil » ------------------------------------------------------------------------------------------

    function setUpDevice(card) {
        var island = document.getElementById("push-config");
        if (!island) return;
        var config;
        try {
            config = JSON.parse(island.textContent);
        } catch (error) {
            return;
        }
        var activate = card.querySelector("[data-push-activate]");
        var answer = card.querySelector("[data-push-answer]");
        var stale = card.querySelector("[data-push-stale]");
        var reload = card.querySelector("[data-push-reload]");
        var registration = null;
        var current = null;
        var device = config.device;

        function show(state) {
            var states = card.querySelectorAll("[data-device-state]");
            for (var index = 0; index < states.length; index++) {
                states[index].hidden = states[index].getAttribute("data-device-state") !== state;
            }
        }

        function say(text) {
            answer.textContent = text || "";
            answer.hidden = !text;
        }

        function ready() {
            activate.disabled = false;
            activate.textContent = activate.getAttribute("data-ready-label");
        }

        function drawActivate(isStale) {
            stale.hidden = !isStale;
            show("activate");
        }

        var userAgent = navigator.userAgent || "";
        var ios = /iPad|iPhone|iPod/.test(userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
        var standalone = navigator.standalone === true || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
        var supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

        if (ios && !standalone) return show("ios-install");
        if (ios && !("PushManager" in window)) return show("ios-update");
        if (!supported) return show("unsupported");
        if (Notification.permission === "denied") return show(ios ? "denied-ios" : "denied");

        function synced(subscription) {
            return postJson(config.sync, describe(subscription)).then(function (result) {
                var state = result.data && result.data.state;
                if (result.data && result.data.device) device = result.data.device;
                return state;
            });
        }

        function subscribeAgain(previous) {
            var dropped = previous ? previous.unsubscribe() : Promise.resolve(true);
            return dropped.then(function () {
                return registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(config.key) });
            });
        }

        function drawFromServer(subscription, renewed) {
            return synced(subscription).then(function (state) {
                if (state === "ok") {
                    current = subscription;
                    say("");
                    return show("active");
                }
                if (state === "renew" && !renewed) {
                    return subscribeAgain(subscription).then(function (fresh) {
                        return drawFromServer(fresh, true);
                    });
                }
                current = subscription;
                return drawActivate(true);
            });
        }

        drawActivate(false);
        var waited = window.setTimeout(function () {
            if (!registration) reload.hidden = false;
        }, READY_TIMEOUT);

        navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {
            return null;
        });
        navigator.serviceWorker.ready.then(function (found) {
            window.clearTimeout(waited);
            registration = found;
            reload.hidden = true;
            return registration.pushManager.getSubscription();
        }).then(function (subscription) {
            ready();
            current = subscription;
            if (!subscription || Notification.permission !== "granted") return drawActivate(false);
            return drawFromServer(subscription, false);
        }).catch(function () {
            ready();
            drawActivate(false);
            say(NOT_REGISTERED);
        });

        function register(subscription) {
            var body = describe(subscription);
            return postJson(config.subscribe, body).then(function (result) {
                if (!result.ok || !result.data || !result.data.ok) {
                    var refusal = new Error("refused");
                    refusal.said = result.data && typeof result.data.error === "string" ? result.data.error : "";
                    throw refusal;
                }
                device = result.data.device;
                current = subscription;
                say("");
                show("active");
            });
        }

        activate.addEventListener("click", function () {
            if (!registration) return;
            activate.disabled = true;
            var wanted = keyBytes(config.key);
            var sameKey = current && base64url(current.options && current.options.applicationServerKey) === config.key;
            // subscribe() first, inside the gesture (iOS asks only from one).
            var subscribing = sameKey
                ? Promise.resolve(current)
                : current
                    ? current.unsubscribe().then(function () {
                        return registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: wanted });
                    })
                    : registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: wanted });
            subscribing.then(function (subscription) {
                // Subscribed in the browser: only the inscrire POST failed.
                return register(subscription).catch(function (error) {
                    say((error && error.said) || NOT_REGISTERED);
                });
            }, function () {
                // Nothing subscribed: the prompt dismissed, or the push
                // service refused.
                if (Notification.permission === "denied") return show(ios ? "denied-ios" : "denied");
                say(Notification.permission === "granted" ? SUBSCRIBE_FAILED : NOT_ALLOWED);
                return null;
            }).then(function () {
                activate.disabled = false;
            });
        });

        card.querySelector("[data-push-test]").addEventListener("click", function (event) {
            var button = event.currentTarget;
            button.disabled = true;
            say("Envoi…");
            postJson(config.test, device ? { appareil: device } : {}).then(function (result) {
                say((result.data && (result.data.message || result.data.error)) || "Essai non envoyé.");
            }, function () {
                say("Essai non envoyé : réessayez.");
            }).then(function () {
                button.disabled = false;
            });
        });

        card.querySelector("[data-push-disable]").addEventListener("click", function (event) {
            var button = event.currentTarget;
            button.disabled = true;
            var leaving = current ? current.unsubscribe() : Promise.resolve(true);
            leaving.then(function () {
                if (!device) return null;
                return postJson(config.remove.replace("/0/", "/" + device + "/"), {});
            }).then(function () {
                current = null;
                device = null;
                say("Désactivées sur cet appareil.");
                drawActivate(false);
            }, function () {
                say("Désactivation non enregistrée : réessayez.");
            }).then(function () {
                button.disabled = false;
            });
        });
    }

    // -- « Page à ouvrir » -----------------------------------------------------------------------------------------

    function setUpPageChoices() {
        var selects = document.querySelectorAll("select[data-page-select]");
        for (var index = 0; index < selects.length; index++) {
            (function (select) {
                var path = select.form && select.form.querySelector("[data-page-path]");
                var field = path && path.closest(".form-field");
                if (!field) return;
                function follow() {
                    field.hidden = select.value !== "autre";
                }
                select.addEventListener("change", follow);
                follow();
            })(selects[index]);
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        var card = document.querySelector("[data-push-device]");
        if (card) setUpDevice(card);
        setUpPageChoices();
    });
})();
