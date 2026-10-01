"""The « Consignes » pages, driven the way the owner drives them.

Every POST is read OFF THE RENDERED PAGE (`staff/tests/page_forms.py`) and
sent through a client that enforces CSRF, as a browser's does: a field
misnamed in a template, a select drawn with nothing selected, a button that
saves where it should test - a request written by hand lets all of them
through. page_forms leaves file inputs out, so the photos and the PDFs are
added to what it reads, as SimpleUploadedFile.

Every name, number, date, amount and count is INVENTED
(returnables/tests/support.py): the owner's real tickets carry his account,
his driver and his deliveries. Photos are a few pixels; a pattern the guard
must refuse never reaches the real `regex.compile` (`RefuseTheFreeze`).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

import regex
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts import paths
from invoices.models import Invoice, ScrapeJob
from returnables import patterns, views
from returnables.forms import COUNT_ERROR, NO_DATE_PATTERN, NO_SUBJECT, NOTHING_LEFT, NOTHING_TO_SAVE
from returnables.models import (
    MAX_PHOTOS,
    Pickup,
    PickupCount,
    PickupPhoto,
    ReturnableType,
    Slip,
    SlipFormat,
    SlipLine,
)
from returnables.tests.support import (
    CO2_LINE,
    CRATE_LINE,
    DELIVERY_DAY,
    KEG_LINE,
    make_format,
    make_photo,
    make_pickup,
    make_slip,
    make_supplier,
    make_type,
    seeded_format,
    seeded_type,
    slip_text,
    tiny_jpeg,
    tiny_pdf,
    uba,
)
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from tests.factories import make_invoice, make_invoice_line

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "returnables"

HOME = "/consignes/"
NBSP = "\N{NO-BREAK SPACE}"
PALLET_LINE = ("PALETTE EXEMPLE", 1, Decimal("12.0000"), Decimal("12.00"))
#: The shapes of the freeze (29/09): refused by the guard, never compiled.
FREEZE_TOKENS = ("65535", "6 5 5 3 5", "{100,}", "{1 0 0")


class RefuseTheFreeze:
    """Stands in for regex.compile while a page is drawn or a form checked:
    compiles as usual, EXCEPT a pattern of the freeze's shapes, which the guard
    must have refused before - that call fails the test."""

    def __init__(self):
        self.real = regex.compile
        self.refused = []

    def __call__(self, pattern, *args, **kwargs):
        if any(token in str(pattern) for token in FREEZE_TOKENS):
            self.refused.append(pattern)
            raise AssertionError(f"regex.compile reached with {pattern!r}")
        return self.real(pattern, *args, **kwargs)


def photo(name="IMG_0001.jpg", **kwargs) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, tiny_jpeg(**kwargs), content_type="image/jpeg")


def not_an_image(name="IMG_0002.HEIC") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"ftypheic - pas une image que Pillow lit", content_type="image/heic")


def media_files(folder="consignes") -> set:
    root = paths.media_root()
    return {path.relative_to(root).as_posix() for path in (root / folder).rglob("*") if path.is_file()}


def count_of(pickup, type_name):
    row = PickupCount.objects.filter(pickup=pickup, returnable_type__name=type_name).first()
    return row.quantity if row else 0


class _Tree(HTMLParser):
    """Which ids enclose each element: enough to say a form is OUTSIDE a
    part of the page."""

    VOID = {
        "input",
        "br",
        "img",
        "meta",
        "link",
        "hr",
        "source",
        "wbr",
        "area",
        "base",
        "col",
        "embed",
        "param",
        "track",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        ancestors = [item for item in self.stack if item]
        self.found.append((tag, attributes, ancestors))
        if tag not in self.VOID:
            self.stack.append(attributes.get("id", ""))

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def enclosing_ids(html: str, tag: str, attribute: str) -> list:
    tree = _Tree()
    tree.feed(html)
    return [ancestors for name, attributes, ancestors in tree.found if name == tag and attribute in attributes]


class PageTestCase(TestCase):
    def setUp(self):
        super().setUp()
        # The suite's client (tests/runner.py): logged in as the tenant's
        # owner, CSRF enforced as a browser's is.
        self.client = self.client_class(enforce_csrf_checks=True)

    def get(self, url, status=200, **extra):
        response = self.client.get(url, **extra)
        self.assertEqual(response.status_code, status, url)
        if status == 200:
            self.assertRendered(response)
        return response

    def html(self, url) -> str:
        return self.get(url).content.decode()

    def text(self, response) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.content.decode())).split())

    def assertRendered(self, response):
        content = response.content.decode()
        for marker in ("{#", "#}", "{%", "%}", "{{"):
            self.assertNotIn(marker, content, f"unrendered template syntax {marker!r}")

    def send(self, form, *, press=None, values=None, files=None, follow=True, **extra):
        """Submit `form` as the browser would - with `files` {name: [uploads]}
        added, which page_forms leaves out."""
        data = as_post(form.submission(press=press, values=values))
        for name, uploads in (files or {}).items():
            data[name] = list(uploads)
        return self.client.post(form.action.split("#")[0], data, follow=follow, **extra)

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def pickup_form(self, url=HOME):
        return form_posting_to(self.html(url), url)

    def count_field(self, type_name) -> str:
        return f"nombre-{seeded_type(type_name).pk}"


# -- /consignes/ --------------------------------------------------------------------------------------------------------


class HomePageTests(PageTestCase):
    def test_the_page_says_what_it_is_for(self):
        response = self.get(HOME)
        self.assertContains(response, "<h1>Consignes</h1>", html=False)
        self.assertContains(response, "Les vides rendus au livreur, comptés ici puis comparés à son bon.")
        self.assertContains(response, 'class="returnables-page returnables-home"')
        self.assertContains(response, "js/returnables.js?v=")

    def test_nothing_yet_says_what_to_do(self):
        # The empty state is one short sentence now; what to do is the
        # « Nouvelle reprise » form drawn right under it.
        response = self.get(HOME)
        self.assertContains(response, "Aucune reprise enregistrée.")
        self.assertContains(response, "<h2>Nouvelle reprise</h2>", html=False)

    def test_the_strip_names_the_latest_pickup(self):
        make_pickup(date=DELIVERY_DAY - timedelta(days=7), counts={"Fûts": 4})
        make_pickup(counts={"Fûts": 15, "Bouteilles CO2": 1}, photos=2)
        text = self.text(self.get(HOME))
        self.assertIn("Dernière reprise : 10/02/2026 · Fûts 15 · Bouteilles CO2 1 · 2 photos en attente du bon", text)

    def test_the_lists_reload_when_a_gather_ends_and_the_form_is_outside_them(self):
        html = self.html(HOME)
        for part in ("returnables-live", "returnables-live-bottom"):
            with self.subTest(part=part):
                opening = re.search(rf'<div id="{part}"[^>]*>', html).group(0)
                self.assertIn('hx-trigger="documents-changed from:body"', opening)
                self.assertIn(f'hx-select="#{part}"', opening)
                self.assertIn('hx-swap="outerHTML"', opening)
                self.assertIn(f'hx-get="{HOME}"', opening)
        (ancestors,) = enclosing_ids(html, "form", "data-pickup-form")
        self.assertNotIn("returnables-live", ancestors)
        self.assertNotIn("returnables-live-bottom", ancestors)

    def test_a_list_shown_whole_reloads_whole(self):
        html = self.html(f"{HOME}?tout=reprises")
        self.assertIn(f'hx-get="{HOME}?tout=reprises"', html)
        self.assertIn(f'hx-get="{HOME}"', self.html(f"{HOME}?tout=%22%3E"))

    def test_the_form_is_a_plain_multipart_post_the_browser_checks(self):
        html = self.html(HOME)
        opening = re.search(r"<form [^>]*data-pickup-form[^>]*>", html).group(0)
        self.assertIn('method="post"', opening)
        self.assertIn('enctype="multipart/form-data"', opening)
        self.assertNotIn("novalidate", html)
        self.assertIn(
            '<button class="btn pickup-submit" type="submit" data-busy-label="Envoi des photos… gardez la page ouverte">'
            "Enregistrer la reprise</button>",
            html,
        )

    def test_two_photo_inputs_one_name(self):
        html = self.html(HOME)
        self.assertIn(
            '<input type="file" name="photos" accept="image/*" capture="environment" data-photo-capture>', html
        )
        self.assertIn('<input type="file" name="photos" accept="image/*" multiple data-photo-gallery>', html)
        self.assertIn("Prendre une photo", html)
        self.assertIn("Choisir des photos", html)
        self.assertIn(f'data-max-photos="{MAX_PHOTOS}"', html)

    def test_the_photos_script_runs_before_the_page_s(self):
        """The photos are static/js/photos.js's since Achats takes them too
        (01/10): returnables.js asks it for them as the page loads. Both are
        deferred, so the order they are written in is the order they run -
        written after, the photos stayed the browser's own and the draft
        counted none."""
        html = self.html(HOME)
        scripts = re.findall(r"<script\b[^>]*>", html)
        (photos,) = [index for index, tag in enumerate(scripts) if "js/photos.js?v=" in tag]
        (page,) = [index for index, tag in enumerate(scripts) if "js/returnables.js?v=" in tag]
        self.assertLess(photos, page)
        self.assertIn(" defer", scripts[photos])
        # Consignes' own box: a count, no byte cap, no renaming - as before.
        box = re.search(r"<fieldset [^>]*data-photos[^>]*>", html).group(0)
        self.assertNotIn("data-max-bytes", box)
        self.assertNotIn("data-photo-rename", box)

    def test_one_count_per_active_type_the_kegs_first_and_bigger(self):
        make_type("Casiers jus", position=4, is_active=False)
        html = self.html(HOME)
        rows = re.findall(r'<div class="count-row([^"]*)" data-count-row data-type-name="([^"]+)">', html)
        self.assertEqual([name for _css, name in rows], ["Fûts", "Caisses verre", "Bouteilles CO2"])
        self.assertEqual(rows[0][0], " is-main")
        self.assertEqual({css for css, _name in rows[1:]}, {""})
        field = re.search(rf'<input[^>]*name="{self.count_field("Fûts")}"[^>]*>', html).group(0)
        for attribute in (
            'type="text"',
            'inputmode="numeric"',
            'pattern="[0-9]*"',
            'maxlength="4"',
            'autocomplete="off"',
            'placeholder="0"',
        ):
            self.assertIn(attribute, field)
        self.assertNotIn("value=", field)
        # The keypad's key says what it does (returnables.js: Enter moves to
        # the next count and never sends the form): « next », then « done »
        # on the last count.
        hints = [
            re.search(rf'<input[^>]*name="{self.count_field(name)}"[^>]*>', html).group(0)
            for name in ("Fûts", "Caisses verre", "Bouteilles CO2")
        ]
        self.assertEqual(
            [re.search(r'enterkeyhint="(\w+)"', hint).group(1) for hint in hints], ["next", "next", "done"]
        )
        for label in ("Un de moins : Fûts", "Un de plus : Fûts"):
            self.assertRegex(
                html, rf'<button type="button" class="stepper-btn" data-step="-?1" aria-label="{label}" hidden>'
            )

    def test_the_date_is_today_in_paris_between_2000_and_today(self):
        with mock.patch("returnables.views.timezone.localdate", return_value=date(2026, 3, 12)):
            html = self.html(HOME)
        field = re.search(r'<input type="date"[^>]*>', html).group(0)
        for attribute in (
            'name="date"',
            'value="2026-03-12"',
            'min="2000-01-01"',
            'max="2026-03-12"',
            'data-today="2026-03-12"',
        ):
            self.assertIn(attribute, field)
        self.assertIn("Reprise du <span data-summary-date>12/03/2026</span>", html)
        for reserved in ('name="du"', 'name="au"', 'name="debut"', 'name="fin"', "date-range"):
            self.assertNotIn(reserved, html)

    def test_taken_back_by_starts_on_the_latest_pickup_s_supplier(self):
        other = make_supplier("Brasserie Exemple")
        make_pickup(supplier=other)
        form = self.pickup_form()
        self.assertEqual(form.control("supplier").value, str(other.pk))

    def test_taken_back_by_starts_on_the_first_format_s_supplier_before_any_pickup(self):
        form = self.pickup_form()
        self.assertEqual(form.control("supplier").value, str(uba().pk))

    def test_suppliers_with_a_format_come_first(self):
        make_supplier("Aaa Premier par le nom")
        options = [value for value, _selected in self.pickup_form().control("supplier").options]
        self.assertEqual(options[0], "")
        self.assertEqual(options[1], str(uba().pk))

    def test_a_pickup_already_saved_that_day_is_offered_to_complete(self):
        with mock.patch("returnables.views.timezone.localdate", return_value=DELIVERY_DAY):
            pickup = make_pickup(counts={"Fûts": 15})
            response = self.get(HOME)
        self.assertIn("Une reprise du 10/02/2026 est déjà enregistrée (Fûts 15) : la compléter", self.text(response))
        self.assertContains(response, f"{reverse('returnables:pickup_detail', args=[pickup.pk])}#modifier")

    def test_the_lists_and_show_all(self):
        for number in range(views.LISTED + 1):
            make_pickup(date=DELIVERY_DAY - timedelta(days=number), counts={"Fûts": 1 + number})
        html = self.html(HOME)
        self.assertEqual(html.count('data-row-href="/consignes/reprises/'), views.LISTED)
        self.assertIn(f"Tout afficher ({views.LISTED + 1} reprises)", html)
        whole = self.html(f"{HOME}?tout=reprises")
        self.assertEqual(whole.count('data-row-href="/consignes/reprises/'), views.LISTED + 1)
        self.assertNotIn("Tout afficher (", whole)

    def test_the_settings_are_plain_links(self):
        html = self.html(HOME)
        self.assertIn(f'<a href="{reverse("returnables:format_list")}">Formats de bons</a>', html)
        self.assertIn(f'<a href="{reverse("returnables:type_list")}">Types de consigne</a>', html)
        self.assertIn('<details class="explainer">', html)


class EveryPageOfTheScriptLoadsThePhotosFirstTests(SimpleTestCase):
    def test_every_template_loading_returnables_js_loads_photos_js_before_it(self):
        """Read on the templates themselves: a page loading returnables.js
        without photos.js before it draws its photo inputs bare."""
        root = Path(__file__).resolve().parents[2]
        paths_found = sorted({*root.glob("templates/**/*.html"), *root.glob("*/templates/**/*.html")})
        loading = []
        for path in paths_found:
            if ".venv" in path.parts:
                continue
            scripts = re.findall(r"<script\b[^>]*>", path.read_text(encoding="utf-8"))
            pages = [index for index, tag in enumerate(scripts) if "js/returnables.js" in tag]
            if not pages:
                continue
            loading.append(path.name)
            with self.subTest(template=path.name):
                photos = [index for index, tag in enumerate(scripts) if "'js/photos.js'" in tag]
                self.assertEqual(len(photos), 1)
                self.assertLess(photos[0], pages[0])
                self.assertIn(" defer", scripts[photos[0]])
        self.assertEqual(sorted(loading), ["home.html", "pickup_detail.html", "slip_detail.html"])


# -- A new pickup --------------------------------------------------------------------------------------------------------


class NewPickupTests(PageTestCase):
    def setUp(self):
        super().setUp()
        self.kegs = self.count_field("Fûts")
        self.co2 = self.count_field("Bouteilles CO2")
        self.crates = self.count_field("Caisses verre")

    def values(self, **counts):
        values = {"date": "2026-02-10"}
        values.update({self.count_field(name): value for name, value in counts.items()})
        return values

    def test_counts_and_photos_are_saved_and_said(self):
        before = media_files()
        response = self.send(
            self.pickup_form(),
            values={"date": "2026-02-10", self.kegs: "15", self.co2: "1", "note": "Un fût cabossé"},
            files={"photos": [photo("IMG_0001.jpg"), photo("IMG_0002.jpg", size=(3, 4))]},
        )
        self.assertEqual(response.redirect_chain[-1], (f"{HOME}?enregistree=1", 302))
        self.assertEqual(
            self.messages_of(response), ["Reprise du 10/02/2026 enregistrée : Fûts 15, Bouteilles CO2 1, 2 photos."]
        )
        pickup = Pickup.objects.get()
        self.assertEqual((pickup.date, pickup.supplier, pickup.note), (DELIVERY_DAY, uba(), "Un fût cabossé"))
        self.assertEqual((count_of(pickup, "Fûts"), count_of(pickup, "Bouteilles CO2")), (15, 1))
        self.assertFalse(PickupCount.objects.filter(pickup=pickup, returnable_type__name="Caisses verre").exists())
        photos = list(pickup.photos.order_by("pk"))
        self.assertEqual(len(photos), 2)
        added = media_files() - before
        self.assertEqual(len(added), 4)
        for number, row in enumerate(photos, start=1):
            with self.subTest(photo=number):
                self.assertRegex(
                    row.image.name, rf"^consignes/photos/\d{{4}}/\d{{2}}/reprise-20260210-{number}(_\w+)?\.jpg$"
                )
                self.assertRegex(row.thumb.name, rf"reprise-20260210-{number}-vignette(_\w+)?\.jpg$")
                self.assertTrue(default_storage.exists(row.image.name))
        self.assertEqual((photos[1].width, photos[1].height), (3, 4))

    def test_the_success_is_said_on_the_page_it_lands_on(self):
        response = self.send(self.pickup_form(), values=self.values(Fûts="3"))
        self.assertContains(response, "Reprise du 10/02/2026 enregistrée : Fûts 3.")
        self.assertIn("Dernière reprise : 10/02/2026 · Fûts 3", self.text(response))
        # At the top of the page, where the redirect lands - not with the slips'.
        html = response.content.decode()
        self.assertLess(html.index("Reprise du 10/02/2026 enregistrée"), html.index("<h1>Consignes</h1>"))

    def test_a_count_left_blank_is_zero_and_zero_is_no_row(self):
        self.send(self.pickup_form(), values=self.values(**{"Fûts": "12", "Caisses verre": "0", "Bouteilles CO2": ""}))
        pickup = Pickup.objects.get()
        self.assertEqual(list(pickup.counts.values_list("returnable_type__name", "quantity")), [("Fûts", 12)])

    def test_photos_alone_are_a_pickup(self):
        self.send(self.pickup_form(), values=self.values(), files={"photos": [photo()]})
        self.assertEqual((Pickup.objects.count(), PickupPhoto.objects.count(), PickupCount.objects.count()), (1, 1, 0))

    def test_nothing_at_all_is_refused_and_writes_nothing(self):
        before = media_files()
        response = self.send(self.pickup_form(), values=self.values())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [])
        self.assertContains(response, NOTHING_TO_SAVE)
        self.assertEqual(Pickup.objects.count(), 0)
        self.assertEqual(media_files(), before)

    def test_a_count_that_is_no_whole_number_from_0_to_9999_is_refused(self):
        for typed in ("1,5", "-1", "10000", "douze", "\N{ARABIC-INDIC DIGIT THREE}", "1 5"):
            with self.subTest(typed=typed):
                before = media_files()
                response = self.send(self.pickup_form(), values=self.values(Fûts=typed), files={"photos": [photo()]})
                self.assertEqual(response.redirect_chain, [])
                self.assertContains(response, COUNT_ERROR)
                self.assertEqual(Pickup.objects.count(), 0)
                self.assertEqual(media_files(), before)

    def test_9999_is_the_most(self):
        self.send(self.pickup_form(), values=self.values(Fûts="9999"))
        self.assertEqual(count_of(Pickup.objects.get(), "Fûts"), 9999)

    def test_a_date_in_the_future_or_before_2000_is_refused(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        for typed in (f"{tomorrow:%Y-%m-%d}", "1999-12-31", "2026-02-30", ""):
            with self.subTest(typed=typed):
                response = self.send(self.pickup_form(), values={"date": typed, self.kegs: "15"})
                self.assertEqual(response.redirect_chain, [])
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'class="pickup-details" open')
                self.assertEqual(Pickup.objects.count(), 0)

    def test_an_unreadable_photo_never_refuses_the_pickup(self):
        response = self.send(
            self.pickup_form(),
            values=self.values(Fûts="15"),
            files={"photos": [photo(), not_an_image("IMG_0002.HEIC")]},
        )
        self.assertEqual(
            self.messages_of(response),
            [
                "Reprise du 10/02/2026 enregistrée : Fûts 15, 1 photo.",
                "Photo non gardée — IMG_0002.HEIC : Cette photo n'est pas dans un format lisible (JPEG, PNG, WebP).",
            ],
        )
        self.assertEqual(PickupPhoto.objects.count(), 1)

    def test_an_unreadable_photo_and_no_count_is_nothing_to_save(self):
        response = self.send(self.pickup_form(), values=self.values(), files={"photos": [not_an_image()]})
        self.assertContains(response, NOTHING_TO_SAVE)
        self.assertContains(response, "Photo non gardée — IMG_0002.HEIC")
        self.assertEqual(Pickup.objects.count(), 0)

    def test_the_eleventh_photo_is_not_kept(self):
        uploads = [photo(f"IMG_{number:04d}.jpg") for number in range(MAX_PHOTOS + 1)]
        response = self.send(self.pickup_form(), values=self.values(), files={"photos": uploads})
        self.assertEqual(PickupPhoto.objects.count(), MAX_PHOTOS)
        self.assertIn("10 photos au plus par reprise : 1 photo en trop n'a pas été gardée.", self.messages_of(response))

    def test_a_failure_while_saving_leaves_no_row_and_no_file(self):
        before = media_files()
        with mock.patch.object(PickupPhoto, "save", side_effect=RuntimeError("disque plein")):
            with self.assertRaises(RuntimeError):
                self.send(self.pickup_form(), values=self.values(Fûts="15"), files={"photos": [photo()]})
        self.assertEqual(Pickup.objects.count(), 0)
        self.assertEqual(media_files(), before)

    def test_photos_taken_another_day_are_said_and_the_date_can_follow_them(self):
        evening_before = datetime(2026, 2, 9, 18, 30)
        response = self.send(
            self.pickup_form(),
            values=self.values(Fûts="15"),
            files={"photos": [photo(taken_at=evening_before), photo("IMG_0002.jpg", taken_at=evening_before)]},
        )
        self.assertIn(
            "Les photos ont été prises le 09/02/2026, la reprise est datée du 10/02/2026.", self.messages_of(response)
        )
        pickup = Pickup.objects.get()
        html = response.content.decode()
        self.assertIn("Dater la reprise du 09/02/2026", html)
        self.assertIn("Photo du 09/02/2026 à 18:30", html)
        button = form_posting_to(html, reverse("returnables:pickup_date", args=[pickup.pk]))
        landed = self.send(button)
        self.assertEqual(landed.redirect_chain[-1], (HOME, 302))
        self.assertEqual(self.messages_of(landed), ["Reprise du 10/02/2026 mise au 09/02/2026."])
        pickup.refresh_from_db()
        self.assertEqual(pickup.date, date(2026, 2, 9))
        self.assertNotIn("Dater la reprise du", landed.content.decode())

    def test_a_photo_without_a_date_says_when_it_was_sent(self):
        make_pickup(photos=1)
        self.assertRegex(self.text(self.get(HOME)), r"Envoyée le \d{2}/\d{2}/\d{4} à \d{2}:\d{2}")

    def test_nothing_about_a_pickup_touches_the_invoices(self):
        self.send(self.pickup_form(), values=self.values(Fûts="15"))
        self.assertEqual(Invoice.objects.count(), 0)


# -- A pickup's page ----------------------------------------------------------------------------------------------------


class PickupPageTests(PageTestCase):
    def url(self, pickup):
        return reverse("returnables:pickup_detail", args=[pickup.pk])

    def test_its_comparison_with_the_day_s_slip(self):
        pickup = make_pickup(counts={"Fûts": 15})
        slip = make_slip(lines=(KEG_LINE,))
        text = self.text(self.get(self.url(pickup)))
        # text() folds the no-break space before « € » (comparison.euros) like any space.
        self.assertIn("Fûts — compté : 15 · sur le bon : 3 → il en manque 12 sur le bon (360,00 €)", text)
        self.assertIn("écart", text)
        self.assertIn(f"Bon du jour : bon n° {slip.number} du 10/02/2026", text)
        self.assertIn("Facture : pas encore reçue", text)

    def test_two_pickups_of_one_day_are_one_side(self):
        first = make_pickup(counts={"Fûts": 10})
        make_pickup(counts={"Fûts": 5})
        make_slip(lines=(("FÛT INOX 30 L", 15, Decimal("30.0000"), Decimal("450.00")),))
        text = self.text(self.get(self.url(first)))
        self.assertIn("Fûts — compté : 15 · sur le bon : 15 ✓", text)
        self.assertIn("2 reprises ce jour-là, additionnées", text)
        self.assertIn("conforme", text)

    def test_editing_reads_the_form_off_the_page(self):
        pickup = make_pickup(counts={"Fûts": 15, "Bouteilles CO2": 1})
        form = self.pickup_form(self.url(pickup))
        self.assertEqual(form.control(self.count_field("Fûts")).value, "15")
        response = self.send(form, values={self.count_field("Fûts"): "12", self.count_field("Bouteilles CO2"): ""})
        self.assertEqual(response.redirect_chain[-1], (self.url(pickup), 302))
        self.assertEqual(self.messages_of(response), ["Reprise du 10/02/2026 modifiée : Fûts 12."])
        self.assertEqual((count_of(pickup, "Fûts"), count_of(pickup, "Bouteilles CO2")), (12, 0))

    def test_a_date_changed_is_said(self):
        pickup = make_pickup()
        response = self.send(self.pickup_form(self.url(pickup)), values={"date": "2026-02-11"})
        self.assertEqual(
            self.messages_of(response), ["Reprise du 10/02/2026 modifiée (datée désormais du 11/02/2026) : Fûts 15."]
        )

    def test_photos_are_added_never_replaced(self):
        pickup = make_pickup(photos=2)
        response = self.send(self.pickup_form(self.url(pickup)), files={"photos": [photo()]})
        self.assertEqual(self.messages_of(response), ["Reprise du 10/02/2026 modifiée : Fûts 15, 1 photo ajoutée."])
        self.assertEqual(pickup.photos.count(), 3)
        self.assertRegex(pickup.photos.order_by("-pk").first().image.name, r"reprise-20260210-3(_\w+)?\.jpg$")

    def test_a_full_pickup_takes_no_more_photos(self):
        pickup = make_pickup(photos=MAX_PHOTOS - 1)
        response = self.send(self.pickup_form(self.url(pickup)), files={"photos": [photo(), photo("IMG_0009.jpg")]})
        self.assertEqual(pickup.photos.count(), MAX_PHOTOS)
        self.assertIn("10 photos au plus par reprise : 1 photo en trop n'a pas été gardée.", self.messages_of(response))
        html = self.html(self.url(pickup))
        self.assertIn("10 photos : retirez-en une pour en ajouter une autre.", html)
        self.assertNotIn('name="photos"', html)

    def test_an_inactive_type_it_counts_stays_on_its_form(self):
        pickup = make_pickup(counts={"Fûts": 15, "Bouteilles CO2": 2})
        ReturnableType.objects.filter(name="Bouteilles CO2").update(is_active=False)
        form = self.pickup_form(self.url(pickup))
        self.assertEqual(form.control(self.count_field("Bouteilles CO2")).value, "2")
        self.send(form)
        self.assertEqual(count_of(pickup, "Bouteilles CO2"), 2)

    def test_editing_it_down_to_nothing_is_refused(self):
        pickup = make_pickup(counts={"Fûts": 15})
        response = self.send(self.pickup_form(self.url(pickup)), values={self.count_field("Fûts"): ""})
        self.assertContains(response, NOTHING_LEFT)
        self.assertEqual(count_of(pickup, "Fûts"), 15)

    def test_a_photo_removed_takes_its_files_once_committed(self):
        pickup = make_pickup(photos=2)
        removed = pickup.photos.order_by("pk").first()
        names = [removed.image.name, removed.thumb.name]
        form = form_posting_to(self.html(self.url(pickup)), reverse("returnables:photo_delete", args=[removed.pk]))
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.send(form)
        self.assertEqual(response.redirect_chain[-1], (self.url(pickup), 302))
        self.assertTrue(all(default_storage.exists(name) for name in names))
        for callback in callbacks:
            callback()
        self.assertFalse(any(default_storage.exists(name) for name in names))
        self.assertEqual(pickup.photos.count(), 1)
        self.assertTrue(self.messages_of(response)[0].startswith("Photo retirée (envoyée le "))

    def test_removing_a_photo_is_asked_twice(self):
        """Right under the thumbnail's link, one mis-tap deleted the photo:
        « Retirer… » opens, then « Retirer » removes - as for the pickup."""
        pickup = make_pickup(photos=1)
        photo_row = pickup.photos.get()
        html = self.html(self.url(pickup))
        self.assertRegex(
            html,
            r'<details class="photo-remove">\s*<summary>Retirer…</summary>\s*'
            rf'<form method="post" action="{reverse("returnables:photo_delete", args=[photo_row.pk])}">',
        )
        # The home card shows the photos without any way to remove them.
        self.assertNotIn(reverse("returnables:photo_delete", args=[photo_row.pk]), self.html(HOME))

    def test_a_pickup_deleted_takes_its_photos_once_committed(self):
        pickup = make_pickup(photos=2)
        names = [name for photo_row in pickup.photos.all() for name in (photo_row.image.name, photo_row.thumb.name)]
        form = form_posting_to(self.html(self.url(pickup)), reverse("returnables:pickup_delete", args=[pickup.pk]))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.send(form)
        self.assertEqual(response.redirect_chain[-1], (HOME, 302))
        self.assertEqual(self.messages_of(response), ["Reprise du 10/02/2026 supprimée, avec ses 2 photos."])
        self.assertFalse(Pickup.objects.exists())
        self.assertFalse(any(default_storage.exists(name) for name in names))

    def test_the_slip_of_a_nearby_day_offers_to_move_the_pickup(self):
        pickup = make_pickup(counts={"Fûts": 3})
        make_slip(delivery_date=DELIVERY_DAY + timedelta(days=1), lines=(KEG_LINE,))
        html = self.html(HOME)
        self.assertIn("Mettre la reprise au 11/02/2026", html)
        button = form_posting_to(html, reverse("returnables:pickup_date", args=[pickup.pk]))
        landed = self.send(button)
        self.assertEqual(self.messages_of(landed), ["Reprise du 10/02/2026 mise au 11/02/2026."])
        pickup.refresh_from_db()
        self.assertEqual(pickup.date, date(2026, 2, 11))
        self.assertIn("Fûts — compté : 3 · sur le bon : 3 ✓", self.text(landed))

    def test_the_date_post_is_checked_like_the_form(self):
        pickup = make_pickup()
        token = forms_of(self.html(self.url(pickup)))[0].control("csrfmiddlewaretoken").value
        url = reverse("returnables:pickup_date", args=[pickup.pk])
        tomorrow = timezone.localdate() + timedelta(days=1)
        for typed in (f"{tomorrow:%Y-%m-%d}", "1999-12-31", "hier", "2026-02-30", ""):
            with self.subTest(typed=typed):
                response = self.client.post(url, {"csrfmiddlewaretoken": token, "date": typed}, follow=True)
                self.assertEqual(response.redirect_chain[-1], (self.url(pickup), 302))
                self.assertIn("La reprise n'a pas été modifiée.", self.messages_of(response)[0])
                pickup.refresh_from_db()
                self.assertEqual(pickup.date, DELIVERY_DAY)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_a_date_button_comes_back_where_it_was_pressed_and_nowhere_else(self):
        pickup = make_pickup()
        token = forms_of(self.html(self.url(pickup)))[0].control("csrfmiddlewaretoken").value
        url = reverse("returnables:pickup_date", args=[pickup.pk])
        for return_to, landing in (
            (HOME, HOME),
            ("https://example.invalid/", self.url(pickup)),
            ("", self.url(pickup)),
        ):
            with self.subTest(return_to=return_to):
                response = self.client.post(
                    url, {"csrfmiddlewaretoken": token, "date": "2026-02-10", "retour": return_to}
                )
                self.assertEqual(response["Location"], landing)


# -- The slips ---------------------------------------------------------------------------------------------------------


class SlipUploadTests(PageTestCase):
    def upload_form(self):
        return form_posting_to(self.html(HOME), reverse("returnables:slip_upload"))

    def pdf(self, name, **text):
        return SimpleUploadedFile(name, tiny_pdf(slip_text(**text)), content_type="application/pdf")

    def test_two_pdfs_then_the_same_two_again(self):
        before = Invoice.objects.count()
        files = lambda: [
            self.pdf("T0001.pdf", number="4101", references=["800101"]),
            self.pdf("T0002.pdf", number="4102", references=["800102"], lines=(CRATE_LINE,)),
        ]
        first = self.send(self.upload_form(), files={"bons": files()})
        self.assertEqual(first.redirect_chain[-1], (f"{HOME}#bons", 302))
        self.assertEqual(
            self.messages_of(first),
            ["2 documents : 2 bons ajoutés, 0 déjà reçu, 0 renvoi d'un bon déjà reçu, 0 refusé."],
        )
        self.assertEqual(sorted(Slip.objects.values_list("number", flat=True)), ["4101", "4102"])
        self.assertEqual(set(Slip.objects.values_list("origin", flat=True)), {Slip.Origin.UPLOAD})
        again = self.send(self.upload_form(), files={"bons": files()})
        self.assertEqual(
            self.messages_of(again),
            ["2 documents : 0 bon ajouté, 2 déjà reçus, 0 renvoi d'un bon déjà reçu, 0 refusé."],
        )
        self.assertEqual(Slip.objects.count(), 2)
        self.assertEqual(Invoice.objects.count(), before)
        self.assertIn("4101", self.text(again))

    def test_a_document_no_format_recognises_offers_a_new_format(self):
        junk = SimpleUploadedFile("facture.pdf", tiny_pdf(["FACTURE EXEMPLE", "TOTAL 12.00"]), "application/pdf")
        response = self.send(self.upload_form(), files={"bons": [junk]})
        (said,) = response.context["messages"]
        self.assertEqual(
            str(said),
            "1 document : 0 bon ajouté, 0 déjà reçu, 0 renvoi d'un bon déjà reçu, 1 refusé. "
            "facture.pdf : Aucun format de bon ne reconnaît ce document.",
        )
        self.assertEqual(said.level_tag, "warning")
        self.assertRegex(
            response.content.decode(),
            r'<li class="message message-warning">[^<]*Aucun format de bon ne reconnaît ce document\. '
            rf'<a href="{reverse("returnables:format_create")}">Nouveau format</a></li>',
        )
        self.assertEqual(Slip.objects.count(), 0)

    def test_what_an_upload_did_is_said_where_the_redirect_lands(self):
        """The redirect lands on #bons, screens under the top of the page:
        the summary, its refusals and « Nouveau format » are drawn there,
        above the upload form - at the top they were out of sight."""
        junk = SimpleUploadedFile("scan.pdf", tiny_pdf(["FACTURE EXEMPLE", "TOTAL 12.00"]), "application/pdf")
        response = self.send(self.upload_form(), files={"bons": [junk]})
        self.assertEqual(response.redirect_chain[-1], (f"{HOME}#bons", 302))
        html = response.content.decode()
        said = "scan.pdf : Aucun format de bon ne reconnaît ce document."
        self.assertEqual(html.count(said), 1)
        section = html.index('<section class="card returnables-upload" id="bons">')
        form = html.index(f'action="{reverse("returnables:slip_upload")}"')
        self.assertLess(section, html.index(said))
        self.assertLess(html.index(said), form)
        self.assertLess(section, html.index(f'<a href="{reverse("returnables:format_create")}">Nouveau format</a>'))
        # The refusals before any file is read land there too.
        empty = self.send(self.upload_form())
        html = empty.content.decode()
        self.assertLess(html.index('id="bons"'), html.index("Aucun fichier choisi : rien n&#x27;a été ajouté."))

    def test_a_chosen_format(self):
        other = make_format("Format essai", section_start="")
        form = self.upload_form()
        self.assertIn(("", False), form.control("format").options)
        self.send(form, values={"format": str(other.pk)}, files={"bons": [self.pdf("T0003.pdf", number="4103")]})
        self.assertEqual(Slip.objects.get().format, other)
        self.assertIn("Format essai (jamais reconnu automatiquement)", self.html(HOME))

    def test_no_file_and_a_format_that_is_no_more(self):
        form = self.upload_form()
        self.assertEqual(self.messages_of(self.send(form)), ["Aucun fichier choisi : rien n'a été ajouté."])
        token = form.control("csrfmiddlewaretoken").value
        for posted in ("abc", "999999", "\N{SUPERSCRIPT TWO}"):
            with self.subTest(format=posted):
                response = self.client.post(
                    reverse("returnables:slip_upload"),
                    {"csrfmiddlewaretoken": token, "format": posted, "bons": [self.pdf("T0004.pdf", number="4104")]},
                    follow=True,
                )
                self.assertEqual(
                    self.messages_of(response),
                    ["Ce format de bon n'existe plus : choisissez-en un dans la liste. Rien n'a été ajouté."],
                )
        self.assertEqual(Slip.objects.count(), 0)


class SlipPageTests(PageTestCase):
    def url(self, slip):
        return reverse("returnables:slip_detail", args=[slip.pk])

    def test_what_the_reading_found(self):
        slip = make_slip(
            lines=(KEG_LINE, CO2_LINE),
            references=["800201"],
            remarks="REPRISE MARCHANDISE",
            checks=[{"label": "Aucune ligne ignorée", "passed": True, "detail": ""}],
        )
        response = self.get(self.url(slip))
        text = self.text(response)
        self.assertIn(f"Bon n° {slip.number} du 10/02/2026", text)
        self.assertIn("FÛT INOX 30 L 3 30.00 € 90.00 € Fûts (motif F[ÛU]TS?\\b", text)
        self.assertIn("BOUTEILLE CO2 10 KG 1 85.00 € 85.00 € Bouteilles CO2", text)
        self.assertIn("800201", text)
        self.assertIn("REPRISE MARCHANDISE", text)
        self.assertIn("Aucune ligne ignorée", text)
        self.assertContains(response, f'<iframe class="slip-pdf" src="{slip.file.url}" title="Le bon en PDF"></iframe>')

    def test_amounts_of_a_thousand_euros_or_more_are_grouped_and_their_sort_keys_are_not(self):
        slip = make_slip(lines=(("FÛT INOX 30 L", 40, Decimal("30.0000"), Decimal("1200.00")),))
        html = self.html(self.url(slip))
        self.assertIn(f"<dd>-1{NBSP}200.00 €</dd>", html)
        self.assertIn(f'<td class="num" data-sort="1200.00">1{NBSP}200.00 €</td>', html)
        self.assertIn('<td class="num" data-sort="30.0000">30.00 €</td>', html)

    def test_a_replaced_slip_says_which_counts(self):
        original = make_slip(references=["800301"])
        replacement = make_slip(references=["800301"], replaces=True)
        text = self.text(self.get(self.url(original)))
        self.assertIn("annulé et remplacé", text)
        self.assertIn(f"Le bon qui compte : Bon n° {replacement.number} du 10/02/2026", text)
        self.assertIn(
            f"Remplace : bon n° {original.number} du 10/02/2026 (annulé et remplacé)",
            self.text(self.get(self.url(replacement))),
        )

    def test_reread_reads_the_stored_text_again(self):
        slip = make_slip(lines=(KEG_LINE,))
        SlipLine.objects.filter(slip=slip).delete()
        Slip.objects.filter(pk=slip.pk).update(read_error="erreur ancienne", checks=[])
        form = form_posting_to(self.html(self.url(slip)), reverse("returnables:slip_reread", args=[slip.pk]))
        response = self.send(form)
        self.assertEqual(
            self.messages_of(response),
            [f"Bon n° {slip.number} du 10/02/2026 relu : 1 ligne lue."],
        )
        slip.refresh_from_db()
        self.assertEqual(slip.read_error, "")
        self.assertEqual(list(slip.lines.values_list("designation", "quantity")), [("FÛT INOX 30 L", 3)])

    def test_classify_as_adds_the_line_s_start_escaped_to_the_type(self):
        slip = make_slip(lines=(("PALETTE (ESSAI) 1.5/2", 1, Decimal("12.0000"), Decimal("12.00")),))
        line = slip.lines.get()
        html = self.html(self.url(slip))
        self.assertIn("sans type de consigne", html)
        crates = seeded_type("Caisses verre")
        form = form_posting_to(html, reverse("returnables:line_classify", args=[line.pk]))
        response = self.send(form, values={"type": str(crates.pk)})
        pattern = r"^PALETTE \(ESSAI\) 1\.5/2"
        self.assertEqual(pattern, "^" + regex.escape("PALETTE (ESSAI) 1.5/2", literal_spaces=True))
        crates.refresh_from_db()
        self.assertEqual(crates.slip_patterns.splitlines()[-1], pattern)
        self.assertEqual(
            self.messages_of(response),
            [f"Motif « {pattern} » ajouté au type « Caisses verre »."],
        )
        self.assertIn("PALETTE (ESSAI) 1.5/2 1 12.00 € 12.00 € Caisses verre", self.text(response))

    def test_classify_as_takes_sixty_characters_at_most(self):
        designation = "PALETTE " + "X" * 70
        slip = make_slip(lines=((designation, 1, None, None),))
        pallets = make_type("Palettes", position=4)
        form = form_posting_to(
            self.html(self.url(slip)), reverse("returnables:line_classify", args=[slip.lines.get().pk])
        )
        self.send(form, values={"type": str(pallets.pk)})
        pallets.refresh_from_db()
        self.assertEqual(pallets.slip_patterns, "^" + regex.escape(designation[:60], literal_spaces=True))

    def test_classify_as_checks_the_whole_field_again(self):
        slip = make_slip(lines=(PALLET_LINE,))
        full = make_type("Plein", position=4, slip_patterns="\n".join(f"^MOTIF{number}" for number in range(50)))
        form = form_posting_to(
            self.html(self.url(slip)), reverse("returnables:line_classify", args=[slip.lines.get().pk])
        )
        response = self.send(form, values={"type": str(full.pk)})
        self.assertEqual(
            self.messages_of(response),
            ["Motifs des bons : 50 motifs au plus, un par ligne (51 ici). Rien n'a été modifié."],
        )
        full.refresh_from_db()
        self.assertEqual(len(full.slip_patterns.splitlines()), 50)

    def test_classify_as_with_no_type_or_a_type_that_is_no_more(self):
        slip = make_slip(lines=(PALLET_LINE,))
        form = form_posting_to(
            self.html(self.url(slip)), reverse("returnables:line_classify", args=[slip.lines.get().pk])
        )
        for posted in ("", "abc", "999999"):
            with self.subTest(type=posted):
                data = as_post(form.submission())
                data["type"] = [posted]
                response = self.client.post(form.action, data, follow=True)
                self.assertEqual(
                    self.messages_of(response), ["Choisissez un type de consigne existant : rien n'a été modifié."]
                )

    def test_classify_as_says_when_a_type_placed_before_already_takes_the_line(self):
        slip = make_slip(lines=(KEG_LINE,))
        later = make_type("Autres", position=9)
        token = forms_of(self.html(self.url(slip)))[0].control("csrfmiddlewaretoken").value
        response = self.client.post(
            reverse("returnables:line_classify", args=[slip.lines.get().pk]),
            {"csrfmiddlewaretoken": token, "type": str(later.pk)},
            follow=True,
        )
        self.assertIn(
            "mais le type « Fûts », placé avant lui, reconnaît déjà cette ligne", self.messages_of(response)[0]
        )

    def test_a_slip_deleted_takes_its_pdf_once_committed(self):
        slip = make_slip()
        name = slip.file.name
        form = form_posting_to(self.html(self.url(slip)), reverse("returnables:slip_delete", args=[slip.pk]))
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.send(form)
        self.assertTrue(default_storage.exists(name))
        for callback in callbacks:
            callback()
        self.assertFalse(default_storage.exists(name))
        self.assertEqual(self.messages_of(response), [f"Bon n° {slip.number} du 10/02/2026 supprimé."])
        self.assertFalse(SlipLine.objects.exists())


class InvoiceLineTests(PageTestCase):
    """What the seller's invoice says of a slip, as the pickup's card (home
    and its page) and the slip's page draw it: « Facture : » then the state -
    never the label word printed before a sentence that says it again
    (« facture facture pas encore reçue »)."""

    def urls(self, pickup, slip):
        return (
            HOME,
            reverse("returnables:pickup_detail", args=[pickup.pk]),
            reverse("returnables:slip_detail", args=[slip.pk]),
        )

    def refund(self, number, *references, lines=(("FÛT INOX 30 L", -3, "-90.00"),)):
        """An invented UBA invoice printing `references`, refunding `lines`
        (raw name, quantity, total)."""
        invoice = make_invoice(
            supplier=uba(),
            invoice_date=DELIVERY_DAY + timedelta(days=2),
            invoice_number=number,
            source_text="\n".join(
                ["U.B.A.  EXEMPLE", f"Facture No : {number}", *[f"BL  {ref}  Page  1/1" for ref in references]]
            ),
        )
        for raw_name, quantity, total in lines:
            make_invoice_line(
                invoice=invoice, raw_name=raw_name, quantity=quantity, total_ht=total, category="Consignes"
            )
        return invoice

    def assertReads(self, url, sentence):
        text = self.text(self.get(url))
        self.assertIn(sentence, text)
        self.assertNotIn("facture facture", text.lower())
        self.assertNotIn("facture annulé", text.lower())

    def test_no_invoice_yet(self):
        pickup = make_pickup(counts={"Fûts": 3})
        slip = make_slip(lines=(KEG_LINE,))
        for url in self.urls(pickup, slip):
            with self.subTest(url=url):
                self.assertReads(url, "Facture : pas encore reçue")

    def test_refunded(self):
        pickup = make_pickup(counts={"Fûts": 3})
        slip = make_slip(lines=(KEG_LINE,), references=["800901"])
        self.refund("F-EX-1", "800901")
        for url in self.urls(pickup, slip):
            with self.subTest(url=url):
                self.assertReads(url, "Facture : remboursé ✓ Facture n° F-EX-1")

    def test_nothing_taken_back_nothing_refunded_is_not_said_refunded(self):
        slip = make_slip(lines=(), references=["800904"])
        self.refund("F-EX-4", "800904", lines=())
        url = reverse("returnables:slip_detail", args=[slip.pk])
        self.assertReads(url, "Facture : rien à rembourser ✓ Facture n° F-EX-4")
        self.assertIn("rien à rembourser ✓", self.text(self.get(HOME)))

    def test_nothing_taken_back_but_other_returnables_refunded_is_to_check(self):
        """The slip's part is empty (corrected to kegs by a replacement not
        received yet, a keg returned full) and the invoice refunds kegs: no
        verdict, and never « rien à rembourser »."""
        slip = make_slip(lines=(), references=["800905"])
        self.refund("F-EX-5", "800905")
        text = self.text(self.get(reverse("returnables:slip_detail", args=[slip.pk])))
        self.assertIn(
            "Facture : à vérifier rien repris sur le bon, mais la facture n° F-EX-5 rembourse d'autres consignes "
            "(non comparées) Facture n° F-EX-5",
            text,
        )
        self.assertIn("Autres avoirs de la facture (non comparés) : FÛT INOX 30 L : -3", text)
        self.assertNotIn("rien à rembourser", text)
        self.assertNotIn("(non comparées) : à vérifier", text)

    def test_several_invoices_says_which_once(self):
        pickup = make_pickup(counts={"Fûts": 3})
        slip = make_slip(lines=(KEG_LINE,), references=["800902"])
        self.refund("F-EX-2", "800902")
        self.refund("F-EX-3", "800902")
        for url in self.urls(pickup, slip):
            with self.subTest(url=url):
                text = self.text(self.get(url))
                self.assertIn("Facture : à vérifier BL n° 800902 sur plusieurs factures (n° F-EX-2, n° F-EX-3)", text)
                self.assertNotIn(
                    "à vérifier BL n° 800902 sur plusieurs factures (n° F-EX-2, n° F-EX-3) : à vérifier", text
                )

    def test_a_replaced_slip(self):
        original = make_slip(references=["800903"])
        make_slip(references=["800903"], replaces=True)
        self.assertReads(reverse("returnables:slip_detail", args=[original.pk]), "Facture : sur le bon qui compte")

    def test_a_state_the_page_has_no_short_word_for_is_never_a_long_pill(self):
        """A new state of invoice_check (added there first) still reads
        « Facture : <its verdict> <its sentence> », its sentence out of the
        pill (a pill never wraps: on a phone it scrolls the page sideways)."""
        from returnables.invoice_check import InvoiceCheck

        check = InvoiceCheck("nouvel_etat", "une phrase assez longue pour ne jamais tenir dans une pastille")
        with mock.patch.dict("returnables.invoice_check.CSS", {"nouvel_etat": "pending"}):
            pill = views.check_pill(check)
        self.assertEqual((pill.text, pill.css), ("à vérifier", "pending"))
        self.assertEqual(pill.detail, "une phrase assez longue pour ne jamais tenir dans une pastille")


# -- Slip formats -------------------------------------------------------------------------------------------------------


class FormatPageTests(PageTestCase):
    SAVE = ("action", "enregistrer")
    TEST = ("action", "tester")

    def url(self, fmt=None):
        return reverse("returnables:format_edit", args=[fmt.pk]) if fmt else reverse("returnables:format_create")

    def form(self, fmt=None, query=""):
        url = self.url(fmt)
        return form_posting_to(self.html(url + query), url)

    def stored_patterns(self, fmt) -> dict:
        fmt.refresh_from_db()
        return {name: getattr(fmt, name) for name in views.SlipFormatForm.Meta.fields if name != "supplier"}

    def test_a_new_format_saved_off_the_page(self):
        supplier = make_supplier("Brasserie Exemple")
        response = self.send(
            self.form(),
            press=self.SAVE,
            values={
                "name": "Brasserie — bon de reprise",
                "supplier": str(supplier.pk),
                "section_start": "^VIDES REPRIS",
                "line_pattern": r"^(?P<designation>.+?)\s+(?P<quantite>\d+)$",
                "date_patterns": r"Livré le (?P<date>\d{2}/\d{2}/\d{4})",
            },
        )
        fmt = SlipFormat.objects.get(name="Brasserie — bon de reprise")
        self.assertEqual(response.redirect_chain[-1], (self.url(fmt), 302))
        self.assertEqual(self.messages_of(response), ["Format « Brasserie — bon de reprise » enregistré."])
        self.assertEqual((fmt.supplier, fmt.attachment_pattern, fmt.sender_pattern), (supplier, r"(?i)\.pdf$", ""))

    def test_saving_a_format_says_its_slips_were_not_read_again_in_french(self):
        fmt = seeded_format()
        make_slip()
        one = self.send(self.form(fmt), press=self.SAVE)
        self.assertEqual(
            self.messages_of(one),
            ["Format « UBA — bon du livreur » enregistré. Son bon n'a pas été relu : utilisez « Relire »."],
        )
        make_slip()
        two = self.send(self.form(fmt), press=self.SAVE)
        self.assertEqual(
            self.messages_of(two),
            ["Format « UBA — bon du livreur » enregistré. Ses 2 bons n'ont pas été relus : utilisez « Relire »."],
        )

    def test_enter_tests_and_never_saves(self):
        form = self.form()
        first = form.buttons()[0]
        self.assertEqual((first.attrs.get("name"), first.attrs.get("value")), ("action", "tester"))
        self.assertIn("formnovalidate", first.attrs)
        response = self.send(
            form,
            values={
                "name": "Jamais enregistré",
                "supplier": str(uba().pk),
                "line_pattern": r"^(?P<designation>.+?)\s+(?P<quantite>\d+)$",
                "date_patterns": r"(?P<date>\d{2}/\d{2}/\d{4})",
                "texte_essai": slip_text(number="4201"),
            },
        )
        self.assertEqual(response.redirect_chain, [])
        self.assertFalse(SlipFormat.objects.filter(name="Jamais enregistré").exists())

    def test_what_a_pattern_must_be(self):
        fmt = seeded_format()
        before = self.stored_patterns(fmt)
        cases = (
            ({"date_patterns": ""}, NO_DATE_PATTERN),
            ({"line_pattern": ""}, "Sans motif de ligne, aucune ligne du bon n'est lue."),
            ({"line_pattern": "^(?P<designation>.+"}, "Motif de ligne : parenthèse non fermée"),
            ({"line_pattern": r"^(?P<designation>.+)$"}, "Motif de ligne : le motif doit contenir"),
            ({"date_patterns": r"^Le (?P<jour>\d+)"}, "Motif de date : le motif doit contenir"),
            ({"total_patterns": "a{e<=1}"}, "Motif de total : "),
            ({"sender_pattern": r"uba\.paris"}, "Le motif d'expéditeur doit désigner une adresse ou un domaine."),
            ({"sender_pattern": ".+@.+"}, "Le motif d'expéditeur doit désigner une adresse ou un domaine."),
            ({"subject_pattern": ""}, NO_SUBJECT),
            ({"section_start": "x" * 301}, "300 caractères au plus"),
        )
        for values, said in cases:
            with self.subTest(values=values):
                response = self.send(self.form(fmt), press=self.SAVE, values=values)
                self.assertEqual(response.redirect_chain, [])
                self.assertIn(said, self.text(response))
                self.assertEqual(self.stored_patterns(fmt), before)

    def test_a_name_is_unique_whatever_its_case_and_accents(self):
        other = make_format("Format essai")
        response = self.send(self.form(other), press=self.SAVE, values={"name": "  uba —  BON du livreur "})
        self.assertIn("Le format « UBA — bon du livreur » existe déjà : choisissez un autre nom.", self.text(response))
        other.refresh_from_db()
        self.assertEqual(other.name, "Format essai")

    def test_the_freeze_s_patterns_are_refused_before_anything_compiles_them(self):
        fmt = seeded_format()
        before = self.stored_patterns(fmt)
        sentinel = RefuseTheFreeze()
        with mock.patch.object(regex, "compile", new=sentinel):
            for pattern in (
                "(?:x{65535}){65535}",
                "(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}",
                "(?:(?:(?:x{100,}){100,}){100,}){100,}",
            ):
                with self.subTest(pattern=pattern):
                    response = self.send(self.form(fmt), press=self.SAVE, values={"line_pattern": pattern})
                    self.assertContains(response, "Motif de ligne : ")
                    self.assertContains(response, "has-error")
        self.assertEqual(sentinel.refused, [])
        self.assertEqual(self.stored_patterns(fmt), before)

    def test_tester_redraws_the_page_with_the_trace_and_saves_nothing(self):
        fmt = seeded_format()
        before = self.stored_patterns(fmt)
        response = self.send(
            self.form(fmt),
            press=self.TEST,
            values={"texte_essai": slip_text(number="4301", references=["800401"]), "section_end": ""},
        )
        self.assertEqual(response.redirect_chain, [])
        text = self.text(response)
        self.assertIn("Testé sur le texte collé ; rien n'est enregistré.", text)
        self.assertIn("REPRISE VIDE début", text)
        self.assertIn("ligne lue", text)
        self.assertIn("1 ligne lue", text)
        self.assertIn("800401", text)
        self.assertEqual(self.stored_patterns(fmt), before)
        self.assertEqual(Slip.objects.count(), 0)

    def test_tester_groups_the_thousands_of_what_it_read_never_of_the_text_tested(self):
        fmt = seeded_format()
        tested = slip_text(number="4305", lines=(("FÛT INOX 30 L", 40, Decimal("30.0000"), Decimal("1200.00")),))
        response = self.send(self.form(fmt), press=self.TEST, values={"texte_essai": tested})
        html = unescape(response.content.decode())
        self.assertIn(f"<dd>-1{NBSP}200.00 €</dd>", html)
        self.assertIn(f'<td class="num">1{NBSP}200.00 €</td>', html)
        self.assertIn(f"lignes : 1{NBSP}200,00 · total imprimé : -1{NBSP}200,00", html)
        self.assertIn(f"quantité 40 · prix 30,00 · montant 1{NBSP}200,00", html)
        # The text tested goes back into its box as it was typed.
        again = form_posting_to(response.content.decode(), self.url(fmt))
        self.assertIn("FÛT INOX 30 L 40 x 30.00 = 1200.00", again.control("texte_essai").value)
        self.assertIn("Deconsigne : -1200.00", again.control("texte_essai").value)

    def test_tester_answers_htmx_in_place_with_the_sources_out_of_band(self):
        fmt = seeded_format()
        before = self.stored_patterns(fmt)
        tested = slip_text(number="4302")
        response = self.send(
            self.form(fmt),
            press=self.TEST,
            values={"texte_essai": tested, "line_pattern": r"^(?P<designation>FÛT.+?)\s+(?P<quantite>\d+)\s+x"},
            HTTP_HX_REQUEST="true",
        )
        html = response.content.decode()
        self.assertNotIn("<html", html)
        self.assertIn('<div id="tester-sources" class="tester-sources" hx-swap-oob="true">', html)
        self.assertIn("Ticket No : 0000004302", unescape(html))
        self.assertIn("ligne lue", html)
        self.assertEqual(self.stored_patterns(fmt), before)

    def test_tester_on_a_pdf_stores_nothing_and_keeps_its_text(self):
        fmt = seeded_format()
        before = (Slip.objects.count(), media_files())
        pdf = SimpleUploadedFile("T0005.pdf", tiny_pdf(slip_text(number="4303")), content_type="application/pdf")
        response = self.send(self.form(fmt), press=self.TEST, files={"pdf_essai": [pdf]})
        self.assertIn("Testé sur le PDF « T0005.pdf »", self.text(response))
        again = form_posting_to(response.content.decode(), self.url(fmt))
        self.assertIn("Ticket No : 0000004303", again.control("texte_essai").value)
        self.assertEqual((Slip.objects.count(), media_files()), before)

    def test_tester_on_a_new_format_comes_first_with_the_pdf(self):
        html = self.html(self.url())
        self.assertLess(html.index('name="pdf_essai"'), html.index('name="name"'))
        html = self.html(self.url(seeded_format()))
        self.assertGreater(html.index('name="pdf_essai"'), html.index('name="name"'))

    def test_tester_on_a_slip_received(self):
        fmt = seeded_format()
        slip = make_slip(number="4304")
        response = self.send(self.form(fmt), press=self.TEST, values={"tester_sur": str(slip.pk)})
        self.assertIn("Testé sur le bon n° 4304 du 10/02/2026", self.text(response))

    def test_tester_with_nothing_or_a_pattern_the_guard_refuses(self):
        fmt = seeded_format()
        self.assertIn("Rien à tester", self.text(self.send(self.form(fmt), press=self.TEST)))
        response = self.send(
            self.form(fmt), press=self.TEST, values={"texte_essai": slip_text(), "line_pattern": "^(?P<designation>.+"}
        )
        self.assertIn("Lecture impossible : Motif de ligne : parenthèse non fermée", self.text(response))

    def test_a_bad_pdf_to_test_is_said(self):
        fmt = seeded_format()
        junk = SimpleUploadedFile("pas-un-pdf.pdf", b"ceci n'est pas un PDF", content_type="application/pdf")
        response = self.send(self.form(fmt), press=self.TEST, files={"pdf_essai": [junk]})
        self.assertIn("pas-un-pdf.pdf : Ce fichier n'est pas un PDF lisible.", self.text(response))

    def test_duplicate_prefills_a_copy_without_its_supplier(self):
        fmt = seeded_format()
        form = self.form(query=f"?depuis={fmt.pk}")
        self.assertEqual(form.control("name").value, "Copie de UBA — bon du livreur")
        self.assertEqual(form.control("line_pattern").value, fmt.line_pattern)
        self.assertEqual(form.control("supplier").value, "")
        self.assertEqual(self.form(query="?depuis=abc").control("name").value, "")

    def test_rereading_the_slips_of_a_format(self):
        slips = [make_slip(), make_slip(lines=(CRATE_LINE,))]
        SlipLine.objects.all().delete()
        fmt = seeded_format()
        html = self.html(self.url(fmt))
        self.assertIn("Relire les 2 bons de ce format", html)
        response = self.send(form_posting_to(html, reverse("returnables:format_reread", args=[fmt.pk])))
        self.assertEqual(self.messages_of(response), ["2 bons relus."])
        self.assertEqual(sorted(SlipLine.objects.values_list("slip_id", flat=True)), sorted(slip.pk for slip in slips))

    def test_reread_stops_when_its_time_is_spent(self):
        make_slip()
        make_slip()
        fmt = seeded_format()
        form = form_posting_to(self.html(self.url(fmt)), reverse("returnables:format_reread", args=[fmt.pk]))
        with mock.patch("returnables.views.slips.reread_format", return_value=(1, 1)):
            response = self.send(form)
        self.assertEqual(self.messages_of(response), ["1 bon relu ; il en reste 1 : relancez « Relire »."])

    def test_a_format_with_slips_is_not_deleted(self):
        fmt = seeded_format()
        make_slip()
        token = forms_of(self.html(self.url(fmt)))[0].control("csrfmiddlewaretoken").value
        response = self.client.post(
            reverse("returnables:format_delete", args=[fmt.pk]), {"csrfmiddlewaretoken": token}, follow=True
        )
        self.assertEqual(
            self.messages_of(response),
            ["Le format « UBA — bon du livreur » a 1 bon : il ne peut pas être supprimé - désactivez-le plutôt."],
        )
        self.assertTrue(SlipFormat.objects.filter(pk=fmt.pk).exists())

    def test_a_format_without_slips_is_deleted(self):
        fmt = make_format("Format essai")
        response = self.send(
            form_posting_to(self.html(self.url(fmt)), reverse("returnables:format_delete", args=[fmt.pk]))
        )
        self.assertEqual(self.messages_of(response), ["Format « Format essai » supprimé."])
        self.assertFalse(SlipFormat.objects.filter(pk=fmt.pk).exists())

    def test_the_pattern_fields_type_like_code(self):
        html = self.html(self.url(seeded_format()))
        field = re.search(r'<input[^>]*name="line_pattern"[^>]*>', html).group(0)
        for attribute in (
            'class="pattern-input"',
            'autocapitalize="off"',
            'autocorrect="off"',
            'spellcheck="false"',
            'autocomplete="off"',
        ):
            self.assertIn(attribute, field)
        self.assertIn("<summary>Réglages avancés</summary>", html)
        self.assertIn("<summary>Écrire un motif</summary>", html)
        self.assertIn("Vide = dépôt à la main seulement.", html)


class TypePageTests(PageTestCase):
    URL = "/consignes/types/"

    def test_one_form_per_type_and_a_new_one(self):
        html = self.html(self.URL)
        for kind in ReturnableType.objects.all():
            with self.subTest(type=kind.name):
                form = form_posting_to(html, reverse("returnables:type_edit", args=[kind.pk]))
                self.assertEqual(form.control(f"type-{kind.pk}-name").value, kind.name)
        self.assertNotIn("TOTAL_FORMS", html)
        form_posting_to(html, self.URL)

    def test_a_refused_form_lands_on_its_own_card(self):
        """A refusal is drawn back at 200 from the top of the page: each form
        carries its card's fragment, so the browser scrolls to the card that
        says why - on a phone the third type's card is screens down."""
        html = self.html(self.URL)
        for kind in ReturnableType.objects.all():
            with self.subTest(type=kind.name):
                url = reverse("returnables:type_edit", args=[kind.pk])
                self.assertEqual(form_posting_to(html, url).action, f"{url}#type-{kind.pk}")
                self.assertIn(f'id="type-{kind.pk}"', html)
        self.assertEqual(form_posting_to(html, self.URL).action, f"{self.URL}#nouveau-type")
        self.assertIn('id="nouveau-type"', html)
        # Drawn back after a refusal, the page still holds the card named.
        crates = seeded_type("Caisses verre")
        url = reverse("returnables:type_edit", args=[crates.pk])
        refused = self.send(form_posting_to(html, url), values={f"type-{crates.pk}-slip_patterns": "F[ÛU"})
        self.assertEqual(refused.status_code, 200)
        self.assertEqual(form_posting_to(refused.content.decode(), url).action, f"{url}#type-{crates.pk}")

    def test_a_new_type(self):
        response = self.send(
            form_posting_to(self.html(self.URL), self.URL),
            values={"nouveau-name": "  Casiers   jus ", "nouveau-slip_patterns": r"\bJUS\b"},
        )
        self.assertEqual(self.messages_of(response), ["Type « Casiers jus » ajouté."])
        kind = ReturnableType.objects.get(name="Casiers jus")
        self.assertEqual((kind.position, kind.is_active), (4, True))

    def test_a_name_is_unique_whatever_its_accents(self):
        response = self.send(form_posting_to(self.html(self.URL), self.URL), values={"nouveau-name": "futs"})
        self.assertContains(response, "Le type « Fûts » existe déjà : choisissez un autre nom.")
        self.assertEqual(ReturnableType.objects.count(), 3)

    def test_a_pattern_edited_is_checked_and_a_refusal_keeps_what_was_typed(self):
        kegs = seeded_type("Fûts")
        url = reverse("returnables:type_edit", args=[kegs.pk])
        response = self.send(
            form_posting_to(self.html(self.URL), url), values={f"type-{kegs.pk}-slip_patterns": "F[ÛU"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [])
        self.assertContains(response, "Motifs des bons : ")
        self.assertContains(response, "F[ÛU</textarea>")
        kegs.refresh_from_db()
        self.assertEqual(kegs.slip_patterns, r"F[ÛU]TS?\b")

    def test_a_pattern_edited_reclassifies_at_once(self):
        slip = make_slip(lines=(PALLET_LINE,))
        crates = seeded_type("Caisses verre")
        url = reverse("returnables:type_edit", args=[crates.pk])
        self.send(
            form_posting_to(self.html(self.URL), url),
            values={f"type-{crates.pk}-slip_patterns": crates.slip_patterns + "\nPALETTE"},
        )
        self.assertIn(
            "PALETTE EXEMPLE 1 12.00 € 12.00 € Caisses verre",
            self.text(self.get(reverse("returnables:slip_detail", args=[slip.pk]))),
        )

    def test_a_type_counted_in_a_pickup_is_not_deleted(self):
        make_pickup(counts={"Fûts": 3})
        kegs = seeded_type("Fûts")
        token = forms_of(self.html(self.URL))[0].control("csrfmiddlewaretoken").value
        response = self.client.post(
            reverse("returnables:type_delete", args=[kegs.pk]), {"csrfmiddlewaretoken": token}, follow=True
        )
        self.assertEqual(
            self.messages_of(response),
            ["Le type « Fûts » est compté dans des reprises : il ne peut pas être supprimé - désactivez-le plutôt."],
        )
        self.assertTrue(ReturnableType.objects.filter(pk=kegs.pk).exists())

    def test_a_type_nobody_counted_is_deleted(self):
        spare = make_type("Palettes", position=5)
        response = self.send(form_posting_to(self.html(self.URL), reverse("returnables:type_delete", args=[spare.pk])))
        self.assertEqual(self.messages_of(response), ["Type « Palettes » supprimé."])

    def test_a_type_switched_off_leaves_the_new_pickup_form(self):
        crates = seeded_type("Caisses verre")
        self.send(
            form_posting_to(self.html(self.URL), reverse("returnables:type_edit", args=[crates.pk])),
            values={f"type-{crates.pk}-is_active": False},
        )
        self.assertNotIn(f"nombre-{crates.pk}", self.html(HOME))


# -- A stored pattern that fails -----------------------------------------------------------------------------------------


class StoredPatternThatFailsTests(PageTestCase):
    """A pattern stored before a stricter rule, or brought by a hand-edited
    archive, is checked again wherever it is used: the page says « motif
    invalide … corrigez-le », never a 500 - and the guard refuses the
    freeze's shape before anything compiles it."""

    def test_every_page_says_it_and_none_fails(self):
        pickup = make_pickup(counts={"Fûts": 3})
        slip = make_slip()
        fmt = seeded_format()
        SlipFormat.objects.filter(pk=fmt.pk).update(line_pattern="^(?P<designation>.+")
        ReturnableType.objects.filter(name="Caisses verre").update(slip_patterns="(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}")
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        sentinel = RefuseTheFreeze()
        with mock.patch.object(regex, "compile", new=sentinel):
            for url in (
                HOME,
                reverse("returnables:pickup_detail", args=[pickup.pk]),
                reverse("returnables:slip_detail", args=[slip.pk]),
                reverse("returnables:format_list"),
                reverse("returnables:format_edit", args=[fmt.pk]),
                reverse("returnables:type_list"),
            ):
                with self.subTest(url=url):
                    self.assertIn("motif invalide", self.text(self.get(url)).lower())
            form = form_posting_to(
                self.html(reverse("returnables:slip_detail", args=[slip.pk])),
                reverse("returnables:slip_reread", args=[slip.pk]),
            )
            response = self.send(form)
            self.assertIn(
                "il n'a pas pu être lu : Motif de ligne : parenthèse non fermée", self.messages_of(response)[0]
            )
        self.assertEqual(sentinel.refused, [])


# -- « Récupérer les bons » -------------------------------------------------------------------------------------------


class GatherTests(PageTestCase):
    """The page asks Achats' gather (invoices:gather) for the slips; it has
    no gather of its own. The mailbox is never reached: the thread is
    patched."""

    def gather_forms(self, html):
        return [form for form in forms_of(html) if form.action == reverse("invoices:gather")]

    def test_a_pickup_waiting_for_its_slip_offers_fetch(self):
        make_pickup()
        html = self.html(HOME)
        forms = self.gather_forms(html)
        self.assertEqual(len(forms), 2)  # the strip's, and « Ajouter des bons »'s
        pairs = forms[0].submission()
        self.assertIn(("sources", f"bons-{seeded_format().pk}"), pairs)
        self.assertIn(("retour", HOME), pairs)
        # No end: a tab opened yesterday would post yesterday and miss
        # today's slip - the gather ends at ITS today (tasks: localdate).
        self.assertNotIn("end_date", [name for name, _value in pairs])
        fmt = seeded_format()
        self.assertIn(
            f"« {fmt.name} » ({fmt.supplier.name}) : mails de {fmt.sender_pattern}", self.text(self.get(HOME))
        )

    def test_no_mail_format_no_fetch(self):
        SlipFormat.objects.update(sender_pattern="")
        make_pickup()
        html = self.html(HOME)
        self.assertEqual(self.gather_forms(html), [])
        self.assertIn("Aucun format de bon n'a de motif d'expéditeur", html)

    def test_a_tenant_without_the_mailbox_says_so(self):
        make_pickup()
        with mock.patch("returnables.views.integrations_allowed", return_value=False):
            response = self.get(HOME)
        self.assertEqual(self.gather_forms(response.content.decode()), [])
        self.assertContains(response, views.slips_refused())
        self.assertIn("Récupérer les bons de consignes depuis la boîte mail", views.slips_refused())

    def test_a_gather_running_is_said_and_shown(self):
        job = ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, progress={"METRO": {"label": "Metro"}})
        make_pickup()
        response = self.get(HOME)
        self.assertContains(response, views.ALREADY_GATHERING)
        self.assertContains(response, 'id="gather-status"')
        self.assertContains(response, f'hx-get="{reverse("invoices:gather_status", args=[job.pk])}"')
        for form in self.gather_forms(response.content.decode()):
            self.assertTrue(all(button.disabled for button in [c for c in form.controls if c.kind == "submit"]))

    def test_another_page_s_gather_is_said_once_where_it_reloads(self):
        """Said inside #returnables-live only: when the gather ends,
        documents-changed reloads that part, and the sentence goes with it."""
        ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, progress={"METRO": {"label": "Metro"}})
        make_pickup()
        html = self.html(HOME)
        self.assertEqual(html.count(views.ALREADY_GATHERING), 1)
        self.assertLess(html.index('<div id="returnables-live"'), html.index(views.ALREADY_GATHERING))
        self.assertLess(html.index(views.ALREADY_GATHERING), html.index('id="new-pickup"'))

    def test_the_page_s_own_gather_is_never_said_to_be_another(self):
        """The owner taps « Récupérer les bons »: the page drawn after it
        shows the gather's card, not « une récupération est déjà en cours » -
        which read as if his tap had been ignored."""
        make_pickup()
        own = ScrapeJob.objects.create(status=ScrapeJob.Status.PENDING, progress={})
        for progress in ({}, {f"bons-{seeded_format().pk}": {"label": "Bons", "found": 0}}):
            with self.subTest(progress=progress):
                ScrapeJob.objects.filter(pk=own.pk).update(progress=progress, status=ScrapeJob.Status.RUNNING)
                response = self.get(HOME)
                self.assertNotContains(response, views.ALREADY_GATHERING)
                self.assertContains(response, 'id="gather-status"')

    def test_a_gather_of_slips_ended_minutes_ago_is_shown_not_an_old_one(self):
        job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.SUCCESS,
            progress={f"bons-{seeded_format().pk}": {"label": "Bons", "found": 2}},
            finished_at=timezone.now() - timedelta(minutes=3),
        )
        shown = self.get(HOME)
        self.assertContains(shown, 'id="gather-status"')
        self.assertContains(shown, "Bons trouvés")  # a gather of slips only (ScrapeJob.slips_only)
        ScrapeJob.objects.filter(pk=job.pk).update(finished_at=timezone.now() - timedelta(minutes=20))
        self.assertNotContains(self.get(HOME), 'id="gather-status"')
        ScrapeJob.objects.create(status=ScrapeJob.Status.SUCCESS, progress={"METRO": {}}, finished_at=timezone.now())
        self.assertNotContains(self.get(HOME), 'id="gather-status"')

    def test_the_post_reaches_the_gather_with_the_slip_sources(self):
        make_pickup()
        form = self.gather_forms(self.html(HOME))[0]
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.send(form, follow=False)
        job = ScrapeJob.objects.get()
        codes = thread.call_args.kwargs["args"][3]
        self.assertEqual(set(codes), {f"bons-{seeded_format().pk}"})
        # No end posted: the task ends the search on the day it runs.
        self.assertIsNone(thread.call_args.kwargs["args"][2])
        self.assertIsNone(job.range_end)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], HOME)


# -- Queries, addresses, templates, words ------------------------------------------------------------------------------


class QueryCountTests(PageTestCase):
    """Each page reads a fixed number of queries however many pickups,
    photos, slips and lines it shows (CLAUDE.md « N+1s hide in per-object
    properties »). Compared, never pinned: the topbar's badges are other
    apps' queries."""

    def count(self, url):
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(queries)

    def many(self):
        for number in range(1, 7):
            day = DELIVERY_DAY - timedelta(days=4 * number)
            make_pickup(date=day, counts={"Fûts": number, "Bouteilles CO2": 1}, photos=2)
            make_slip(delivery_date=day, lines=(KEG_LINE, CO2_LINE, CRATE_LINE))
        make_slip(references=["800501"])
        make_slip(references=["800501"], replaces=True)

    def test_the_home_page(self):
        make_pickup(photos=1)
        make_slip()
        one = self.count(HOME)
        self.many()
        self.assertEqual(self.count(HOME), one)

    def test_a_pickup_s_page(self):
        pickup = make_pickup(photos=1)
        make_slip()
        url = reverse("returnables:pickup_detail", args=[pickup.pk])
        one = self.count(url)
        for _ in range(4):
            make_photo(pickup)
        make_pickup(counts={"Fûts": 2, "Caisses verre": 4})
        make_slip(lines=(KEG_LINE, CRATE_LINE))
        self.many()
        self.assertEqual(self.count(url), one)

    def test_a_slip_s_page(self):
        slip = make_slip()
        # One pickup near it from the start: none at all would skip the
        # prefetch of their counts, a query no list adds.
        make_pickup()
        url = reverse("returnables:slip_detail", args=[slip.pk])
        one = self.count(url)
        for number in range(8):
            SlipLine.objects.create(slip=slip, position=number + 2, designation=f"CAISSE EXEMPLE {number}", quantity=1)
        self.many()
        self.assertEqual(self.count(url), one)

    def test_the_settings(self):
        urls = (
            reverse("returnables:format_list"),
            reverse("returnables:type_list"),
            reverse("returnables:format_edit", args=[seeded_format().pk]),
        )
        few = [self.count(url) for url in urls]
        for number in range(4):
            make_format(f"Format essai {number}")
            make_type(f"Type essai {number}")
        self.many()
        self.assertEqual([self.count(url) for url in urls], few)


class NotFoundTests(PageTestCase):
    """A bad address is a 404 - a stale bookmark, a photo removed in another
    tab - never a 500, on GET and on POST."""

    ROUTES = (
        "pickup_detail",
        "pickup_delete",
        "pickup_date",
        "photo_delete",
        "slip_detail",
        "slip_delete",
        "slip_reread",
        "line_classify",
        "format_edit",
        "format_delete",
        "format_reread",
        "type_edit",
        "type_delete",
    )

    def test_an_unknown_row(self):
        token = forms_of(self.html(HOME))[0].control("csrfmiddlewaretoken").value
        for name in self.ROUTES:
            for pk in (999999, 99999999999999999999999):
                with self.subTest(route=name, pk=pk):
                    url = reverse(f"returnables:{name}", args=[pk])
                    self.assertEqual(self.client.get(url).status_code, 404)
                    self.assertEqual(self.client.post(url, {"csrfmiddlewaretoken": token}).status_code, 404)

    def test_an_address_that_is_no_number(self):
        for url in ("/consignes/reprises/abc/", "/consignes/bons/-1/", "/consignes/formats/1.5/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)


class TemplateTests(TestCase):
    def test_every_template_of_the_app_loads(self):
        """tests/test_ui.py's syntax check walks a fixed list of apps; this
        one also pins which templates the app has, so a new one is a
        decision."""
        names = sorted(path.name for path in TEMPLATES.glob("*.html"))
        self.assertEqual(
            names,
            [
                "_day.html",  # a pickup and its comparison: the home card, the pickup's page
                "_format_test.html",  # « Tester »'s answer (htmx, or inside the page)
                "_format_tester.html",  # « Tester » inside the format's form
                "_gather_form.html",  # « Récupérer les bons »: a post to Achats' gather
                "_invoice_check.html",  # what the seller's invoice says of a slip
                "_pickup_fields.html",  # a pickup's fields: new and edit
                "_tester_sources.html",  # PDF, slip received, pasted text (swapped out of band)
                "format_form.html",
                "format_list.html",
                "home.html",
                "pickup_detail.html",
                "slip_detail.html",
                "type_list.html",
            ],
        )
        for name in names:
            with self.subTest(template=name):
                get_template(f"returnables/{name}")


class WordsTests(TestCase):
    """The words on screen (CLAUDE.md « The words on screen »): a seller's
    document is a « bon », never a « ticket » - the app's ticket is a till
    receipt; a regex is a « motif »; trying one is « Tester »."""

    FORBIDDEN = ("ticket", "le bon compte", "type de stock", "récupérées", "recherche en cours", "essayer")

    def test_the_templates(self):
        for path in TEMPLATES.glob("*.html"):
            text = path.read_text(encoding="utf-8").lower()
            for word in self.FORBIDDEN:
                with self.subTest(template=path.name, word=word):
                    self.assertNotIn(word, text)

    def test_the_pages(self):
        pickup = make_pickup(photos=1)
        slip = make_slip()
        for url in (
            HOME,
            reverse("returnables:pickup_detail", args=[pickup.pk]),
            reverse("returnables:slip_detail", args=[slip.pk]),
            reverse("returnables:format_list"),
            reverse("returnables:type_list"),
        ):
            page = re.sub(r"<[^>]+>", " ", self.client.get(url).content.decode()).lower()
            page = page[page.index("consignes") :]
            for word in ("le bon compte", "type de stock", "récupérées", "recherche en cours"):
                with self.subTest(url=url, word=word):
                    self.assertNotIn(word, page)
