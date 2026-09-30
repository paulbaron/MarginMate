/* « Menu »: the topbar's links folded away under 860 px (templates/base.html,
 * static/css/marginmate.css « topbar menu »).
 *
 * The owner, 30/09: « the top menu is too big, maybe do something that can
 * be expanded ». Ten links and their badges took three to five rows of a
 * phone's screen on every page, sticky. Folded, the bar is one row: the
 * brand, the page's section, « Menu ».
 *
 * base.html loads this file in the head WITHOUT defer, and the class it puts
 * on <html> is what folds the links: the stylesheet folds nothing without it.
 * So a page where it did not run keeps every link in sight, as before, and
 * the fold and the button that opens it come and go together.
 *
 * The menu's state is the button's aria-expanded, which the stylesheet reads
 * (the menu, the cross, the veil): one state, never a class and an attribute
 * to keep in step. The listeners are on the document, so a bar htmx draws
 * again (a Back with no history cache swaps the whole body in) needs nothing
 * re-attached, and comes back closed, as the server draws it.
 */
(function () {
    "use strict";

    var root = document.documentElement;
    // Once: run twice, every listener would be there twice, and a tap on
    // « Menu » would open the menu and shut it again.
    if (root.classList.contains("topbar-menu-ready")) return;
    root.classList.add("topbar-menu-ready");

    // marginmate.css's breakpoint for the fold (« topbar menu »).
    var NARROW = "(max-width: 860px)";

    function toggleButton() {
        return document.querySelector("[data-topbar-toggle]");
    }
    function isOpen(toggle) {
        return !!toggle && toggle.getAttribute("aria-expanded") === "true";
    }
    function header(toggle) {
        return toggle ? toggle.closest(".topbar") : null;
    }
    function inTheBar(node) {
        return !!(node && node.closest && node.closest(".topbar"));
    }

    // The veil under the open menu is the bar's own ::after, so a tap on it
    // is a click on the header itself. It closes the menu and reaches nothing
    // else: on a phone, the tap that closes a menu would otherwise land on
    // whatever the page has there (a link, a count's field). The listener
    // sits ON the header while the menu is open, not only on the document:
    // iOS Safari sends no click to a document listener from an element
    // nothing listens on, and Chrome's touch adjustment moves a tap landing
    // on such an element onto the nearest button - « Se déconnecter », the
    // menu's last item.
    function onVeil(event) {
        if (event.target === event.currentTarget) shut(toggleButton());
    }
    // A finger dragged (or a wheel turned) on the veil scrolls nothing: the
    // page moved under the open menu, and on /consignes/, where the bar
    // scrolls with the page, carried the menu off the screen. The stylesheet's
    // touch-action: none is not honoured on a pseudo-element by Chrome. Only
    // on the veil itself (the header as the target): a finger on the menu,
    // which is inside the header, still scrolls the menu.
    function onVeilDrag(event) {
        if (event.target === event.currentTarget) event.preventDefault();
    }
    function open(toggle) {
        toggle.setAttribute("aria-expanded", "true");
        var bar = header(toggle);
        if (!bar) return;
        bar.addEventListener("click", onVeil);
        bar.addEventListener("touchmove", onVeilDrag, { passive: false });
        bar.addEventListener("wheel", onVeilDrag, { passive: false });
    }
    function shut(toggle) {
        if (!isOpen(toggle)) return;
        toggle.setAttribute("aria-expanded", "false");
        var bar = header(toggle);
        if (!bar) return;
        bar.removeEventListener("click", onVeil);
        bar.removeEventListener("touchmove", onVeilDrag);
        bar.removeEventListener("wheel", onVeilDrag);
    }

    document.addEventListener("click", function (event) {
        var toggle = toggleButton();
        if (!toggle || !toggle.contains(event.target)) return;
        if (isOpen(toggle)) shut(toggle);
        else open(toggle);
    });

    // Escape closes it, and gives the focus back to « Menu » when the focus
    // was in the bar (a link of the menu, the button itself).
    document.addEventListener("keydown", function (event) {
        var toggle = toggleButton();
        if (event.key !== "Escape" || !isOpen(toggle)) return;
        var wasInTheBar = inTheBar(document.activeElement);
        shut(toggle);
        if (wasInTheBar) toggle.focus();
    });

    // The focus leaving the bar (Tab past « Se déconnecter ») closes it: a
    // menu left open over the page hides the field being typed in.
    document.addEventListener("focusin", function (event) {
        var toggle = toggleButton();
        if (isOpen(toggle) && !inTheBar(event.target)) shut(toggle);
    });

    // Back to this page from the browser's memory, it is shut. Widened past
    // the breakpoint, the links are the bar's again: a menu left open would
    // come back open when the window narrows.
    window.addEventListener("pageshow", function (event) {
        if (event.persisted) shut(toggleButton());
    });
    if (window.matchMedia) {
        var narrow = window.matchMedia(NARROW);
        var onChange = function () { shut(toggleButton()); };
        if (narrow.addEventListener) narrow.addEventListener("change", onChange);
        else if (narrow.addListener) narrow.addListener(onChange);
    }
})();
