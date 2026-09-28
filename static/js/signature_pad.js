/* A drawn signature, on every form marked data-signature-form: the employee's signing page
 * (staff/templates/staff/sign.html) and the employer's « Contresigner… » on the month's page
 * (staff/templates/staff/_signature_request.html). No dependency: the employee's page is public
 * and its Content-Security-Policy allows this site's own scripts only.
 *
 * A form holds a canvas[data-signature-canvas], the hidden input[data-signature-input] the
 * picture is posted in, and optionally [data-signature-undo], [data-signature-clear] and the
 * sentence [data-signature-missing] shown when it is sent with nothing drawn.
 *
 * Pointer events draw strokes on a canvas - a finger, a pen and a mouse alike - with
 * « Annuler le dernier trait » and « Effacer ». When the form is sent, the drawing goes into
 * the hidden field as a PNG data URL: a transparent background, dark ink, and NOTHING of the
 * strokes themselves. The server keeps a picture, never the pace or the pressure of a hand
 * (a static image is not biometric data - research report, « RGPD »).
 *
 * The exported picture is at most 1200 × 400 pixels, which is what the server accepts
 * (staff.signing.clean_signature_png): the canvas's backing store is its size on screen times
 * the screen's pixel ratio, capped to that. The strokes are kept in fractions of the canvas, so
 * turning the phone redraws them at the new size. The frame itself is watched (ResizeObserver),
 * not only the window: the owner's pad starts folded inside « Contresigner… », and a browser that
 * does not lay out a closed <details> gives it no size until it is opened (Chrome does lay it out).
 *
 * A page drawn back after a refusal (the box not ticked, the timestamp server down) carries the
 * drawing in the hidden field: it is painted back, so he does not sign twice.
 *
 * « Je signe avec des réserves » shows the text box when ticked - or when the box already holds
 * what he wrote. Without JavaScript the box is always there (and the drawing impossible: the page
 * says so in a <noscript>).
 */
(function () {
    "use strict";

    var MAX_WIDTH = 1200;
    var MAX_HEIGHT = 400;
    var INK = "#14206e";
    var LINE_WIDTH = 2.6;   // in CSS pixels
    var PNG_PREFIX = "data:image/png;base64,";

    function pad(form) {
        var canvas = form.querySelector("[data-signature-canvas]");
        var input = form.querySelector("[data-signature-input]");
        var missing = form.querySelector("[data-signature-missing]");
        if (!canvas || !input || !canvas.getContext) return;
        var context = canvas.getContext("2d");
        var strokes = [];       // each an array of [x, y], fractions of the canvas
        var current = null;     // the stroke being drawn
        var background = null;  // the drawing a refused post carried back
        var scale = 1;

        function fit() {
            var box = canvas.getBoundingClientRect();
            if (!box.width || !box.height) return;
            var ratio = window.devicePixelRatio || 1;
            scale = Math.min(ratio, MAX_WIDTH / box.width, MAX_HEIGHT / box.height);
            var width = Math.max(1, Math.floor(box.width * scale));
            var height = Math.max(1, Math.floor(box.height * scale));
            if (canvas.width !== width || canvas.height !== height) {
                canvas.width = width;
                canvas.height = height;
            }
            redraw();
        }

        function at(event) {
            var box = canvas.getBoundingClientRect();
            return [(event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height];
        }

        function paint(stroke) {
            var width = canvas.width;
            var height = canvas.height;
            context.lineWidth = LINE_WIDTH * scale;
            context.lineCap = "round";
            context.lineJoin = "round";
            context.strokeStyle = INK;
            context.fillStyle = INK;
            if (stroke.length === 1) {
                // A tap is a dot.
                context.beginPath();
                context.arc(stroke[0][0] * width, stroke[0][1] * height, context.lineWidth / 2, 0, Math.PI * 2);
                context.fill();
                return;
            }
            // Smoothed: a curve through the middles of the segments.
            context.beginPath();
            context.moveTo(stroke[0][0] * width, stroke[0][1] * height);
            for (var index = 1; index < stroke.length - 1; index++) {
                var point = stroke[index];
                var next = stroke[index + 1];
                context.quadraticCurveTo(
                    point[0] * width, point[1] * height,
                    (point[0] + next[0]) / 2 * width, (point[1] + next[1]) / 2 * height
                );
            }
            var last = stroke[stroke.length - 1];
            context.lineTo(last[0] * width, last[1] * height);
            context.stroke();
        }

        function redraw() {
            context.clearRect(0, 0, canvas.width, canvas.height);
            if (background) context.drawImage(background, 0, 0, canvas.width, canvas.height);
            strokes.forEach(paint);
            if (current) paint(current);
        }

        function empty() {
            return !strokes.length && !background && !current;
        }

        canvas.addEventListener("pointerdown", function (event) {
            if (event.button > 0) return;   // the primary button, finger or pen only
            event.preventDefault();
            if (canvas.setPointerCapture) {
                try { canvas.setPointerCapture(event.pointerId); } catch (error) { /* already gone */ }
            }
            current = [at(event)];
            if (missing) missing.hidden = true;
            redraw();
        });

        canvas.addEventListener("pointermove", function (event) {
            if (!current) return;
            event.preventDefault();
            var events = event.getCoalescedEvents ? event.getCoalescedEvents() : [];
            (events.length ? events : [event]).forEach(function (each) { current.push(at(each)); });
            redraw();
        });

        function finish() {
            if (!current) return;
            strokes.push(current);
            current = null;
            redraw();
        }
        canvas.addEventListener("pointerup", finish);
        canvas.addEventListener("pointercancel", finish);
        canvas.addEventListener("lostpointercapture", finish);

        var undo = form.querySelector("[data-signature-undo]");
        if (undo) {
            undo.addEventListener("click", function () {
                if (strokes.length) {
                    strokes.pop();
                } else {
                    background = null;
                }
                redraw();
            });
        }
        var clear = form.querySelector("[data-signature-clear]");
        if (clear) {
            clear.addEventListener("click", function () {
                strokes = [];
                background = null;
                current = null;
                redraw();
            });
        }

        form.addEventListener("submit", function (event) {
            finish();
            if (empty()) {
                event.preventDefault();
                input.value = "";
                if (missing) missing.hidden = false;
                canvas.scrollIntoView({ block: "center" });
                return;
            }
            input.value = canvas.toDataURL("image/png");
        });

        // The drawing a refused post carried back.
        if (input.value.indexOf(PNG_PREFIX) === 0) {
            var image = new Image();
            image.onload = function () {
                background = image;
                redraw();
            };
            image.src = input.value;
        }

        window.addEventListener("resize", fit);
        // A frame with no size yet (a closed <details>, in a browser that skips laying it out) is
        // fitted when it gets one.
        if (window.ResizeObserver) new ResizeObserver(fit).observe(canvas);
        fit();
    }

    function reservations(form) {
        var toggle = form.querySelector("[data-reservations-toggle]");
        var box = form.querySelector("[data-reservations]");
        if (!toggle || !box) return;
        var text = box.querySelector("textarea");

        function sync() {
            box.hidden = !toggle.checked && !(text && text.value.trim());
        }
        toggle.addEventListener("change", function () {
            sync();
            if (toggle.checked && text) text.focus();
        });
        sync();
    }

    function init() {
        document.querySelectorAll("form[data-signature-form]").forEach(function (form) {
            pad(form);
            reservations(form);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
