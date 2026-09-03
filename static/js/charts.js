/* Hover readouts for the two inline SVG charts.
 *
 * Both are rendered server-side (see inventory/views.py and recipes/views.py)
 * and are perfectly readable without this; all it adds is being able to point
 * at something and see its value. That is also the only thing a charting
 * library would have added, which is why there isn't one - Chart.js is ~65KB
 * gzipped plus a CDN dependency, and would have replaced a printable,
 * JS-optional SVG with a canvas.
 *
 * The tooltip follows the cursor and is pointer-events:none, so it can never
 * sit under the pointer and swallow the next hover.
 */
(function () {
    "use strict";

    function showTooltip(chart, tooltip, html, clientX, clientY) {
        var box = chart.getBoundingClientRect();
        tooltip.innerHTML = html;
        tooltip.style.left = (clientX - box.left) + "px";
        tooltip.style.top = (clientY - box.top) + "px";
        tooltip.setAttribute("data-visible", "");
    }

    function hide(chart, tooltip) {
        tooltip.removeAttribute("data-visible");
        chart.removeAttribute("data-hovering");
    }

    function enhanceLine(chart) {
        var svg = chart.querySelector("svg");
        var tooltip = chart.querySelector("[data-chart-tooltip]");
        var points = Array.prototype.slice.call(chart.querySelectorAll(".chart-point"));
        var line = chart.querySelector(".chart-hover-line");
        var dot = chart.querySelector(".chart-hover-dot");
        if (!points.length || !tooltip) return;

        // Whole-chart tracking rather than per-point hover: the dots are 3px
        // across, and hunting for them with a mouse is miserable. Anywhere in
        // the plot snaps to the nearest reading, which is how every chart
        // worth using behaves.
        svg.addEventListener("mousemove", function (event) {
            var box = svg.getBoundingClientRect();
            var viewBox = svg.viewBox.baseVal;
            var x = (event.clientX - box.left) / box.width * viewBox.width;

            var nearest = points[0];
            var best = Infinity;
            points.forEach(function (point) {
                var distance = Math.abs(Number(point.getAttribute("data-x")) - x);
                if (distance < best) { best = distance; nearest = point; }
            });

            var px = Number(nearest.getAttribute("data-x"));
            var py = Number(nearest.getAttribute("data-y"));
            chart.setAttribute("data-hovering", "");
            if (line) { line.setAttribute("x1", px); line.setAttribute("x2", px); }
            if (dot) { dot.setAttribute("cx", px); dot.setAttribute("cy", py); }

            // Anchor the tooltip to the POINT, not the cursor, so it doesn't
            // jitter as the mouse moves within one point's catchment.
            showTooltip(
                chart, tooltip,
                "<strong>" + nearest.getAttribute("data-value") + "</strong><br>" +
                '<span class="muted">' + nearest.getAttribute("data-label") + "</span>",
                box.left + px / viewBox.width * box.width,
                box.top + py / viewBox.height * box.height
            );
        });
        svg.addEventListener("mouseleave", function () { hide(chart, tooltip); });
    }

    function enhancePie(chart) {
        var tooltip = chart.querySelector("[data-chart-tooltip]");
        var slices = Array.prototype.slice.call(chart.querySelectorAll(".chart-slice"));
        var legend = Array.prototype.slice.call(chart.querySelectorAll("[data-legend-for]"));
        if (!slices.length || !tooltip) return;

        function activate(index, clientX, clientY) {
            chart.setAttribute("data-hovering", "");
            slices.forEach(function (slice) {
                slice.toggleAttribute("data-active", slice.getAttribute("data-index") === index);
            });
            legend.forEach(function (item) {
                item.toggleAttribute("data-active", item.getAttribute("data-legend-for") === index);
            });
            var slice = slices.filter(function (s) { return s.getAttribute("data-index") === index; })[0];
            if (!slice) return;
            showTooltip(
                chart, tooltip,
                '<span class="swatch" style="background:' + slice.getAttribute("data-color") + ';"></span>' +
                slice.getAttribute("data-label") + "<br><strong>" +
                slice.getAttribute("data-value") + "</strong>",
                clientX, clientY
            );
        }

        function clear() {
            slices.forEach(function (s) { s.removeAttribute("data-active"); });
            legend.forEach(function (i) { i.removeAttribute("data-active"); });
            hide(chart, tooltip);
        }

        slices.forEach(function (slice) {
            slice.addEventListener("mousemove", function (event) {
                activate(slice.getAttribute("data-index"), event.clientX, event.clientY);
            });
            slice.addEventListener("mouseleave", clear);
        });
        // The legend is easier to hit than a thin slice, so it highlights too.
        legend.forEach(function (item) {
            item.addEventListener("mouseenter", function (event) {
                var box = item.getBoundingClientRect();
                activate(item.getAttribute("data-legend-for"), box.left + box.width / 2, box.top);
            });
            item.addEventListener("mouseleave", clear);
        });
    }

    function init(root) {
        (root || document).querySelectorAll("[data-chart]").forEach(function (chart) {
            if (chart.hasAttribute("data-chart-ready")) return;
            chart.setAttribute("data-chart-ready", "");
            if (chart.getAttribute("data-chart") === "pie") enhancePie(chart);
            else enhanceLine(chart);
        });
    }

    document.addEventListener("DOMContentLoaded", function () { init(document); });
    // Price history is fetched lazily when a stock row is expanded.
    document.addEventListener("htmx:afterSwap", function (event) { init(event.target); });

    window.MarginMateCharts = { init: init };
})();
