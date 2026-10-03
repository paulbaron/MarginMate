"""« Consignes » for an employee (accounts/access.py, the owner's choice of
02/10/2026): he counts and photographs the empties taken back, drops and
reads the driver's slips - and the slips' formats and the returnable types
stay the owner's. A format decides which PDF Achats files as a slip and
which mails the slips' gather reads; a type is counted by every pickup.
So the gate refuses their pages and every POST to them, and « Réglages »
is not drawn for him - hiding it is never the boundary.

What the seller's invoice says of a slip (`_invoice_check.html`) names the
invoices it was read on: links into « Factures » for whoever opens it,
their names otherwise.

Every POST is read OFF THE RENDERED PAGE (`staff/tests/page_forms.py`)
through a client enforcing CSRF, as returnables/tests/test_views.py does:
a refused POST carries his page's valid token, so the 403 is the gate's.
Every name, number, date and amount is INVENTED (returnables/tests/support.py).
"""

from datetime import timedelta

from django.urls import reverse
from django.utils.html import escape

from accounts import access
from returnables.models import Pickup, ReturnableType, SlipFormat
from returnables.tests.support import DELIVERY_DAY, KEG_LINE, make_format, make_pickup, make_slip, make_type, uba
from returnables.tests.test_views import HOME, PageTestCase, count_of, photo
from staff.tests.page_forms import forms_of
from tests.factories import make_invoice, make_invoice_line
from tests.runner import employee_of_the_test_tenant

#: The settings' routes: the owner's alone.
SETTINGS = ("returnables:format_list", "returnables:format_create", "returnables:type_list")


class EmployeeCase(PageTestCase):
    def log_in(self, *pages):
        """The client logged in as an employee opening `pages`."""
        self.client.force_login(employee_of_the_test_tenant("consignes@example.invalid", pages, name="Livreur"))

    def token(self) -> str:
        """A valid CSRF token of his own page: a crafted POST has one too."""
        return forms_of(self.html(HOME))[0].control("csrfmiddlewaretoken").value

    def assertRefused(self, response, *, posted=False):
        """The gate's page (accounts/refused.html): no view ran."""
        self.assertContains(response, "Page non accessible", status_code=403)
        self.assertContains(response, escape(access.REFUSED), status_code=403)
        if posted:
            self.assertContains(response, escape(access.REFUSED_POST), status_code=403)

    def refund(self, number, *references):
        """An invented UBA invoice printing `references` and refunding three
        kegs, as InvoiceLineTests builds one."""
        invoice = make_invoice(
            supplier=uba(),
            invoice_date=DELIVERY_DAY + timedelta(days=2),
            invoice_number=number,
            source_text="\n".join(
                ["U.B.A.  EXEMPLE", f"Facture No : {number}", *[f"BL  {ref}  Page  1/1" for ref in references]]
            ),
        )
        make_invoice_line(
            invoice=invoice, raw_name="FÛT INOX 30 L", quantity=-3, total_ht="-90.00", category="Consignes"
        )
        return invoice

    def checked_pages(self):
        """A pickup, its slip and the invoice refunding it: the pages
        drawing the invoice check."""
        pickup = make_pickup(counts={"Fûts": 3})
        slip = make_slip(lines=(KEG_LINE,), references=["800901"])
        invoice = self.refund("F-EX-1", "800901")
        urls = (
            HOME,
            reverse("returnables:pickup_detail", args=[pickup.pk]),
            reverse("returnables:slip_detail", args=[slip.pk]),
        )
        return urls, invoice


class ConsignesOnlyTests(EmployeeCase):
    """Given « Consignes » and nothing else."""

    def setUp(self):
        super().setUp()
        self.log_in("returnables")

    def test_the_page_draws_no_settings(self):
        html = self.html(HOME)
        self.assertIn("<h1>Consignes</h1>", html)
        self.assertNotIn("Réglages", html)
        for route in SETTINGS:
            with self.subTest(route=route):
                self.assertNotIn(reverse(route), html)

    def test_the_settings_pages_are_refused(self):
        fmt = make_format("Format essai")
        for url in (*(reverse(route) for route in SETTINGS), reverse("returnables:format_edit", args=[fmt.pk])):
            with self.subTest(url=url):
                self.assertRefused(self.client.get(url))

    def test_deleting_a_format_or_a_type_is_refused_and_nothing_goes(self):
        fmt = make_format("Format essai")
        spare = make_type("Palettes", position=5)
        token = self.token()
        for url in (
            reverse("returnables:format_delete", args=[fmt.pk]),
            reverse("returnables:type_delete", args=[spare.pk]),
        ):
            with self.subTest(url=url):
                self.assertRefused(self.client.post(url, {"csrfmiddlewaretoken": token}), posted=True)
        self.assertTrue(SlipFormat.objects.filter(pk=fmt.pk).exists())
        self.assertTrue(ReturnableType.objects.filter(pk=spare.pk).exists())

    def test_adding_a_type_is_refused_and_none_is_added(self):
        before = ReturnableType.objects.count()
        response = self.client.post(
            reverse("returnables:type_list"),
            {"csrfmiddlewaretoken": self.token(), "nouveau-name": "Palettes", "nouveau-position": "5"},
        )
        self.assertRefused(response, posted=True)
        self.assertEqual(ReturnableType.objects.count(), before)

    def test_he_saves_a_pickup_and_sees_its_photo(self):
        response = self.send(
            self.pickup_form(),
            values={"date": "2026-02-10", self.count_field("Fûts"): "4"},
            files={"photos": [photo("IMG_0001.jpg")]},
        )
        self.assertEqual(response.redirect_chain[-1], (f"{HOME}?enregistree=1", 302))
        self.assertEqual(self.messages_of(response), ["Reprise du 10/02/2026 enregistrée : Fûts 4, 1 photo."])
        pickup = Pickup.objects.get()
        self.assertEqual(count_of(pickup, "Fûts"), 4)
        # Its photo is a file of « Consignes »: his to open.
        self.assertEqual(self.client.get(pickup.photos.get().image.url).status_code, 200)

    def test_the_invoice_is_named_never_linked(self):
        urls, invoice = self.checked_pages()
        link = reverse("invoices:invoice_detail", args=[invoice.pk])
        for url in urls:
            with self.subTest(url=url):
                response = self.get(url)
                self.assertIn("Facture : remboursé ✓ Facture n° F-EX-1", self.text(response))
                self.assertNotContains(response, link)

    def test_classifying_a_slip_s_line_is_refused_and_no_motif_is_written(self):
        """« Classer comme » writes the chosen type's motifs, which every
        pickup and slip comparison reads: the types are the owner's - the
        form is not drawn for him, and a crafted post is refused (review of
        02/10/2026)."""
        slip = make_slip(lines=(("PALETTE EXEMPLE", 1, None, None),))
        line = slip.lines.get()
        kind = ReturnableType.objects.order_by("position", "pk").first()
        before = kind.slip_patterns
        html = self.html(reverse("returnables:slip_detail", args=[slip.pk]))
        self.assertIn("sans type de consigne", html)
        self.assertNotIn(reverse("returnables:line_classify", args=[line.pk]), html)
        self.assertNotIn("Classer comme", html)
        response = self.client.post(
            reverse("returnables:line_classify", args=[line.pk]),
            {"csrfmiddlewaretoken": self.token(), "type": str(kind.pk)},
        )
        self.assertRefused(response, posted=True)
        kind.refresh_from_db()
        self.assertEqual(kind.slip_patterns, before)

    def test_a_slip_not_read_sends_him_to_his_employer_not_to_the_format(self):
        slip = make_slip(lines=(), read_error="Aucune ligne reconnue.")
        text = self.text(self.get(reverse("returnables:slip_detail", args=[slip.pk])))
        self.assertIn("Prévenez votre employeur : son format est à corriger.", text)
        self.assertNotIn("Corrigez le format", text)

    def test_the_slip_names_its_format_without_a_link(self):
        slip = make_slip()
        response = self.get(reverse("returnables:slip_detail", args=[slip.pk]))
        self.assertIn(f"Lu avec le format « {slip.format.name} »", self.text(response))
        self.assertNotContains(response, reverse("returnables:format_edit", args=[slip.format.pk]))


class ConsignesAndFacturesTests(EmployeeCase):
    """Given « Factures : tout consulter et corriger » too."""

    def setUp(self):
        super().setUp()
        self.log_in("returnables", "invoices")

    def test_the_invoice_is_a_link(self):
        urls, invoice = self.checked_pages()
        link = f'<a href="{reverse("invoices:invoice_detail", args=[invoice.pk])}">Facture n° F-EX-1</a>'
        for url in urls:
            with self.subTest(url=url):
                self.assertContains(self.get(url), link, html=False)

    def test_the_settings_stay_the_owner_s(self):
        self.assertNotIn("Réglages", self.html(HOME))
        self.assertRefused(self.client.get(reverse("returnables:format_list")))


class TheOwnerTests(EmployeeCase):
    """The suite's client: the espace's owner, as before."""

    def test_the_settings_are_drawn_and_open(self):
        html = self.html(HOME)
        self.assertIn("Réglages", html)
        self.assertIn(f'<a href="{reverse("returnables:format_list")}">Formats de bons</a>', html)
        self.assertIn(f'<a href="{reverse("returnables:type_list")}">Types de consigne</a>', html)
        for route in SETTINGS:
            with self.subTest(route=route):
                self.get(reverse(route))

    def test_the_invoice_is_a_link(self):
        urls, invoice = self.checked_pages()
        link = f'<a href="{reverse("invoices:invoice_detail", args=[invoice.pk])}">Facture n° F-EX-1</a>'
        for url in urls:
            with self.subTest(url=url):
                self.assertContains(self.get(url), link, html=False)

    def test_he_deletes_a_type_nobody_counted(self):
        spare = make_type("Palettes", position=5)
        response = self.client.post(
            reverse("returnables:type_delete", args=[spare.pk]), {"csrfmiddlewaretoken": self.token()}, follow=True
        )
        self.assertEqual(self.messages_of(response), ["Type « Palettes » supprimé."])
        self.assertFalse(ReturnableType.objects.filter(pk=spare.pk).exists())
