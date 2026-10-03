/* MarginMate's service worker, served at /sw.js (notifications/views.py)
   so that its scope is the whole site. It only shows the notifications and
   opens their page: no fetch handler, no cache - the site works exactly as
   without it.

   A push carries the Declarative Web Push shape the server writes
   (notifications/webpush.py payload_for): { web_push, notification: {
   title, body, navigate, tag, lang, dir }, mutable }. Safari 18.4+ shows it
   itself and never wakes this script; the other browsers hand it here. A
   plain { title, body, url } is read too. Every push shows a notification,
   whatever it holds - a push shown nothing makes iOS take the permission
   back - and nothing correctness-critical lives here.

   Plain ES5, no markup written anywhere. */
(function () {
    "use strict";

    var ICON = "/static/icons/icon-192.png";
    var BADGE = "/static/icons/badge-96.png";
    var FALLBACK_TITLE = "MarginMate";

    self.addEventListener("install", function () {
        self.skipWaiting();
    });

    self.addEventListener("activate", function (event) {
        event.waitUntil(self.clients.claim());
    });

    function text(value) {
        return typeof value === "string" ? value : "";
    }

    function readPush(event) {
        var shown = { title: FALLBACK_TITLE, body: "", tag: "", lang: "fr-FR", url: "/" };
        try {
            var data = event.data ? event.data.json() : null;
            var notification = data && typeof data.notification === "object" && data.notification ? data.notification : data;
            if (notification && typeof notification === "object") {
                shown.title = text(notification.title) || FALLBACK_TITLE;
                shown.body = text(notification.body);
                shown.tag = text(notification.tag);
                shown.lang = text(notification.lang) || shown.lang;
                shown.url = text(notification.navigate) || text(notification.url) || "/";
            }
        } catch (error) {
            // Unreadable: « MarginMate » all the same.
        }
        return shown;
    }

    self.addEventListener("push", function (event) {
        var shown = readPush(event);
        var options = {
            body: shown.body,
            lang: shown.lang,
            icon: ICON,
            badge: BADGE,
            data: { url: shown.url }
        };
        if (shown.tag) options.tag = shown.tag;
        event.waitUntil(self.registration.showNotification(shown.title, options));
    });

    /* The page to open, on this site whatever the notification says: its
       path, query and fragment put on this origin. */
    function targetOf(notification) {
        var wanted = notification && notification.data && notification.data.url ? String(notification.data.url) : "/";
        try {
            var parsed = new URL(wanted, self.location.origin);
            return new URL(parsed.pathname + parsed.search + parsed.hash, self.location.origin).href;
        } catch (error) {
            return new URL("/", self.location.origin).href;
        }
    }

    self.addEventListener("notificationclick", function (event) {
        event.notification.close();
        var target = targetOf(event.notification);
        var opened = false;
        function openOnce() {
            if (opened) return null;
            opened = true;
            return self.clients.openWindow(target);
        }
        event.waitUntil(
            self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (windows) {
                var index;
                for (index = 0; index < windows.length; index++) {
                    if (windows[index].url === target && "focus" in windows[index]) return windows[index].focus();
                }
                for (index = 0; index < windows.length; index++) {
                    if ("focus" in windows[index]) {
                        var chosen = windows[index];
                        return chosen.focus().then(function (focused) {
                            var client = focused || chosen;
                            try {
                                return client.navigate(target).then(function (navigated) {
                                    return navigated || openOnce();
                                }, openOnce);
                            } catch (error) {
                                return openOnce();
                            }
                        }, openOnce);
                    }
                }
                return openOnce();
            })
        );
    });

    /* The browser replaced the subscription: subscribe again with the same
       options. The next page's sync (static/js/push_sync.js) records the new
       one through the device its cookie names. */
    self.addEventListener("pushsubscriptionchange", function (event) {
        if (!event.oldSubscription || !event.oldSubscription.options) return;
        event.waitUntil(self.registration.pushManager.subscribe(event.oldSubscription.options));
    });
})();
