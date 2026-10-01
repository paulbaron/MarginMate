"""An identifier typed on a supplier's page, rather than learned.

Until now a supplier could only be given what its OWN documents already
printed (« Retenir »), or have one taken away (« Retirer »). On 24/09 one
supplier had learned another's SIREN, its phone and its web site: the owner
removed them one at a time, then had to wait for the rightful supplier's
own documents to print them before it could claim them back. Nothing on
either page said the figures were held elsewhere - and a figure two
suppliers hold names NEITHER, so the rightful supplier's invoices were
recognised by nobody, in silence.

Data invented throughout; the SIRENs carry real check digits, as printed
ones do.
"""

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from invoices.models import Supplier, SupplierChange
from invoices.receipts import filing_rules, identifier_report, still_naming
from tests.factories import make_invoice, make_supplier

# Both pass the Luhn check.
SIREN = "900000019"
OTHER_SIREN = "552100554"


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class TypingTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        self.other = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple", parser_key="")
        self.supplier_page = reverse("invoices:supplier_detail", args=[self.shop.pk])
        self.url = reverse("invoices:supplier_identifiers", args=[self.shop.pk])

    def add(self, value, supplier=None, action="ajouter"):
        url = reverse("invoices:supplier_identifiers", args=[(supplier or self.shop).pk])
        return self.client.post(url, {"action": action, "valeur": value}, follow=True)

    def reload(self):
        self.shop.refresh_from_db()
        self.other.refresh_from_db()

    def test_a_typed_siren_names_the_supplier(self):
        page = self.add("900 000 019")
        self.reload()
        self.assertEqual(self.shop.ticket_identifiers, [f"siren:{SIREN}"])
        self.assertIn("n° SIREN 900 000 019 reconnaît désormais Epicerie Exemple.", " ".join(messages_of(page)))

    def test_a_phone_and_a_site_are_typed_the_same_way(self):
        self.add("01 23 45 67 89")
        self.add("brico-exemple.fr")
        self.reload()
        self.assertEqual(self.shop.ticket_identifiers, ["tel:0123456789", "web:brico-exemple.fr"])

    def test_what_was_typed_is_recorded_as_typed(self):
        self.add(SIREN)
        self.reload()
        self.assertEqual(self.shop.typed_identifiers, [f"siren:{SIREN}"])

    def test_a_figure_that_is_no_identifier_says_which_digit_is_wrong(self):
        page = self.add("900000018")
        self.reload()
        self.assertEqual(self.shop.ticket_identifiers, [])
        self.assertIn("clé de contrôle", " ".join(messages_of(page)))

    def test_typing_one_it_already_holds_says_so_and_changes_nothing(self):
        self.add(SIREN)
        before = SupplierChange.objects.count()
        page = self.add(SIREN)
        self.assertIn("reconnaît déjà", " ".join(messages_of(page)))
        self.assertEqual(SupplierChange.objects.count(), before)

    def test_typing_one_that_was_set_aside_points_at_the_way_back(self):
        self.shop.refused_identifiers = [f"siren:{SIREN}"]
        self.shop.save()
        page = self.add(SIREN)
        self.reload()
        self.assertEqual(self.shop.ticket_identifiers, [])
        self.assertIn("Ne plus l'écarter", " ".join(messages_of(page)))

    def test_the_action_is_recorded_under_the_button_that_was_clicked(self):
        self.add(SIREN)
        change = SupplierChange.objects.get(supplier=self.shop, kind=SupplierChange.Kind.IDENTIFIERS)
        self.assertTrue(change.by_person)
        self.assertIn("Ajouter", change.cause)

    def test_a_get_changes_nothing(self):
        self.client.get(self.url, {"action": "ajouter", "valeur": SIREN})
        self.reload()
        self.assertEqual(self.shop.ticket_identifiers, [])


class HeldByAnotherTests(TestCase):
    """The 24/09 case, in arithmetic: one supplier holding another's SIREN."""

    def setUp(self):
        self.mine = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple", parser_key="")
        self.squatter = make_supplier(
            code="SUPERETTE_X", name="Superette Exemple", parser_key="", ticket_identifiers=[f"siren:{SIREN}"]
        )
        self.supplier_page = reverse("invoices:supplier_detail", args=[self.mine.pk])

    def add(self, action="ajouter", value=SIREN):
        return self.client.post(
            reverse("invoices:supplier_identifiers", args=[self.mine.pk]),
            {"action": action, "valeur": value},
            follow=True,
        )

    def reload(self):
        self.mine.refresh_from_db()
        self.squatter.refresh_from_db()

    def test_typing_it_says_who_holds_it_and_takes_it_from_nobody(self):
        page = self.add()
        self.reload()
        self.assertEqual(self.mine.ticket_identifiers, [])
        self.assertEqual(self.squatter.ticket_identifiers, [f"siren:{SIREN}"])
        said = " ".join(messages_of(page))
        self.assertIn("reconnaît déjà Superette Exemple", said)
        # Why it is not simply added: said on the page the message lands on,
        # beside « Le déplacer ici ».
        self.assertContains(page, "Retenu par deux fournisseurs, il n'en reconnaîtrait aucun")

    def test_the_page_comes_back_offering_to_move_it(self):
        page = self.add()
        offer = page.context["move_offer"]
        self.assertEqual(offer["identifier"], f"siren:{SIREN}")
        self.assertEqual(offer["holder"].pk, self.squatter.pk)
        self.assertContains(page, "Le déplacer ici")

    def test_moving_it_takes_it_off_the_other_and_gives_it_here(self):
        self.add(action="deplacer")
        self.reload()
        self.assertEqual(self.mine.ticket_identifiers, [f"siren:{SIREN}"])
        self.assertEqual(self.squatter.ticket_identifiers, [])
        self.assertEqual(self.mine.typed_identifiers, [f"siren:{SIREN}"])

    def test_a_move_is_one_operation_in_both_histories(self):
        self.add(action="deplacer")
        changes = SupplierChange.objects.filter(kind=SupplierChange.Kind.IDENTIFIERS)
        self.assertEqual(changes.count(), 2)
        operations = {change.operation for change in changes}
        self.assertEqual(len(operations), 1)
        self.assertIsNotNone(operations.pop())
        self.assertEqual({change.supplier_id for change in changes}, {self.mine.pk, self.squatter.pk})
        for change in changes:
            self.assertEqual(change.data["moved"]["identifier"], f"siren:{SIREN}")

    def test_the_move_is_undone_from_either_side_and_undoes_both(self):
        self.add(action="deplacer")
        for side in (self.mine, self.squatter):
            with self.subTest(side=side.name):
                change = (
                    SupplierChange.objects.filter(
                        supplier=side, kind=SupplierChange.Kind.IDENTIFIERS, undone_at__isnull=True
                    )
                    .exclude(data__undoes__isnull=False)
                    .first()
                )
                self.assertIsNotNone(change)
                self.assertIn("Rendre cet identifiant", self._undo_label(side, change))
        # Undoing from the side that lost it gives it back, both sides at once.
        change = SupplierChange.objects.get(supplier=self.squatter, data__moved__isnull=False, undone_at__isnull=True)
        self.client.post(reverse("invoices:supplier_change_undo", args=[self.squatter.pk, change.pk]), follow=True)
        self.reload()
        self.assertEqual(self.squatter.ticket_identifiers, [f"siren:{SIREN}"])
        self.assertEqual(self.mine.ticket_identifiers, [])

    def _undo_label(self, supplier, change):
        page = self.client.get(reverse("invoices:supplier_detail", args=[supplier.pk]))
        for shown in page.context["changes"]:
            if shown.pk == change.pk:
                return shown.undo_label
        return ""

    def test_a_move_never_leaves_it_on_both(self):
        """The state the whole feature exists to avoid: held twice, it names
        nobody, and every document printing it goes unrecognised in silence."""
        self.add(action="deplacer")
        self.reload()
        holders = [
            supplier.name
            for supplier in Supplier.objects.all()
            if f"siren:{SIREN}" in (supplier.ticket_identifiers or [])
        ]
        self.assertEqual(holders, ["Grossiste Exemple"])


class NeverUnlearnedTests(TestCase):
    """A typed figure is a statement about a supplier, not a reading of its
    documents: learning may forget what learning found, never this."""

    def test_still_naming_keeps_what_was_typed(self):
        known = {f"siren:{SIREN}"}
        # Its own documents do not print it, and another's does: both
        # reasons the learning would drop it for.
        kept = still_naming(known, ["TOTAL 3,00"], [f"SIREN {SIREN}"], typed=known)
        self.assertEqual(kept, known)

    def test_still_naming_drops_a_learned_one_for_the_same_reason(self):
        known = {f"siren:{SIREN}"}
        self.assertEqual(still_naming(known, ["TOTAL 3,00"], []), set())

    def test_a_typed_figure_survives_an_import_that_rechecks(self):
        from invoices.receipts import _recheck

        shop = make_supplier(code="EPICERIE_Y", name="Epicerie Exemple", parser_key="")
        make_invoice(supplier=shop, ocr_text="EPICERIE EXEMPLE\nTOTAL 3,00")
        self.client.post(
            reverse("invoices:supplier_identifiers", args=[shop.pk]),
            {"action": "ajouter", "valeur": SIREN},
        )
        shop.refresh_from_db()
        _recheck(shop)
        shop.refresh_from_db()
        self.assertEqual(shop.ticket_identifiers, [f"siren:{SIREN}"])

    def test_removing_it_by_hand_takes_it_out_of_the_typed_list_too(self):
        shop = make_supplier(code="EPICERIE_Z", name="Epicerie Exemple", parser_key="")
        url = reverse("invoices:supplier_identifiers", args=[shop.pk])
        self.client.post(url, {"action": "ajouter", "valeur": SIREN})
        self.client.post(url, {"action": "retirer", "identifier": f"siren:{SIREN}"})
        shop.refresh_from_db()
        self.assertEqual(shop.ticket_identifiers, [])
        self.assertEqual(shop.typed_identifiers, [])

    def test_the_page_says_which_are_typed(self):
        shop = make_supplier(code="EPICERIE_W", name="Epicerie Exemple", parser_key="")
        self.client.post(
            reverse("invoices:supplier_identifiers", args=[shop.pk]),
            {"action": "ajouter", "valeur": SIREN},
        )
        page = self.client.get(reverse("invoices:supplier_detail", args=[shop.pk]))
        self.assertContains(page, "saisi à la main")
        self.assertTrue(identifier_report(Supplier.objects.get(pk=shop.pk))["known"][0]["typed"])


class FilingRulesTests(TestCase):
    """« Ce qui range un document ici » - the order the application really
    asks, said on the page rather than left to be guessed."""

    def test_a_header_and_its_identifiers_are_listed_in_the_order_asked(self):
        shop = make_supplier(
            code="EPICERIE_R",
            name="Epicerie Exemple",
            parser_key="",
            ticket_header="EPICERIE EXEMPLE",
            ticket_identifiers=[f"siren:{SIREN}"],
        )
        kinds = [rule["kind"] for rule in filing_rules(shop)]
        self.assertEqual(kinds, ["einvoice", "header", "guard", "identifier"])

    def test_a_source_beats_what_a_document_prints_and_says_so(self):
        from invoices.models import InvoiceType

        shop = make_supplier(code="EAU_R", name="Eau Exemple", parser_key="", expenses_only=True)
        InvoiceType.objects.create(name="Espace client Eau", supplier=shop)
        rules = filing_rules(shop)
        self.assertEqual(rules[0]["kind"], "source")
        self.assertIn("quoi qu'il imprime", rules[0]["detail"])

    def test_the_guard_that_overrules_a_header_is_said(self):
        shop = make_supplier(code="EPICERIE_G", name="Epicerie Exemple", parser_key="", ticket_header="EPICERIE")
        guard = next(rule for rule in filing_rules(shop) if rule["kind"] == "guard")
        self.assertIn("n° SIREN qu'un autre", guard["detail"])

    def test_a_supplier_nothing_recognises_says_that_too(self):
        shop = make_supplier(code="NEUF_R", name="Neuf Exemple", parser_key="")
        kinds = [rule["kind"] for rule in filing_rules(shop)]
        self.assertIn("nothing", kinds)

    def test_a_till_needs_neither_header_nor_identifier(self):
        shop = make_supplier(code="CAISSE_R", name="Caisse Exemple", parser_key="")
        rules = filing_rules(shop, is_till=True)
        self.assertEqual([rule["kind"] for rule in rules], ["till"])

    def test_the_page_draws_them(self):
        shop = make_supplier(
            code="EPICERIE_P", name="Epicerie Exemple", parser_key="", ticket_header="EPICERIE EXEMPLE"
        )
        page = self.client.get(reverse("invoices:supplier_detail", args=[shop.pk]))
        self.assertContains(page, "Ce qui range un document ici")
        self.assertContains(page, "Son en-tête « EPICERIE EXEMPLE »")


class FilingReportTests(TestCase):
    """« Rangé ici parce que… » on a document.

    Most documents have nothing stored about why they are where they are: a
    document filed by its header records no check, no field and no history,
    and a digital invoice cannot be given a check at all. So the block is
    worked out from the text the document kept, and states facts - what it
    prints, and who retains each figure - never a second verdict about the
    order, which is said once on the supplier's page.
    """

    def setUp(self):
        self.shop = make_supplier(
            code="EPICERIE_F", name="Epicerie Exemple", parser_key="", ticket_header="EPICERIE EXEMPLE"
        )
        self.other = make_supplier(code="GROS_F", name="Grossiste Exemple", parser_key="")

    def report(self, **kwargs):
        from invoices.receipts import filing_report

        return filing_report(make_invoice(supplier=self.shop, **kwargs))

    def test_a_document_printing_its_header_says_so(self):
        found = self.report(ocr_text="EPICERIE EXEMPLE\nTOTAL 3,00")
        self.assertEqual([row["kind"] for row in found["names_it"]], ["header"])
        self.assertIn("EPICERIE EXEMPLE", found["names_it"][0]["label"])

    def test_a_document_printing_a_figure_it_retains_says_so(self):
        self.shop.ticket_identifiers = [f"siren:{SIREN}"]
        self.shop.save()
        found = self.report(ocr_text=f"UN MAGASIN\nSIREN {SIREN}\nTOTAL 3,00")
        self.assertEqual([row["kind"] for row in found["names_it"]], ["identifier"])
        self.assertEqual(found["names_another"], [])

    def test_a_figure_another_supplier_retains_is_the_alarm(self):
        self.other.ticket_identifiers = [f"siren:{SIREN}"]
        self.other.save()
        found = self.report(ocr_text=f"EPICERIE EXEMPLE\nSIREN {SIREN}")
        self.assertEqual(len(found["names_another"]), 1)
        row = found["names_another"][0]
        self.assertEqual([holder.name for holder in row["holders"]], ["Grossiste Exemple"])
        self.assertFalse(row["shared"])

    def test_a_figure_both_retain_names_neither_and_says_it(self):
        """The state that made one supplier's invoices unrecognised, in silence: another had learned its figures."""
        self.shop.ticket_identifiers = [f"siren:{SIREN}"]
        self.shop.save()
        self.other.ticket_identifiers = [f"siren:{SIREN}"]
        self.other.save()
        found = self.report(ocr_text=f"EPICERIE EXEMPLE\nSIREN {SIREN}")
        self.assertTrue(found["names_another"][0]["shared"])
        self.assertEqual(found["names_it"][0]["kind"], "header")

    def test_a_figure_nobody_retains_is_said_apart(self):
        found = self.report(ocr_text="EPICERIE EXEMPLE\nTEL 01 23 45 67 89")
        self.assertEqual([row["identifier"] for row in found["unknown"]], ["tel:0123456789"])

    def test_a_document_naming_nobody_is_said_rather_than_left_blank(self):
        found = self.report(ocr_text="UN TICKET\nTOTAL 3,00")
        self.assertTrue(found["text"])
        self.assertEqual(found["names_it"], [])

    def test_a_document_with_no_text_asks_nothing(self):
        found = self.report()
        self.assertFalse(found["text"])
        self.assertEqual((found["names_it"], found["names_another"]), ([], []))

    def test_it_costs_the_same_whatever_the_number_of_documents(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from invoices.receipts import filing_report

        invoice = make_invoice(supplier=self.shop, ocr_text=f"EPICERIE EXEMPLE\nSIREN {SIREN}")
        with CaptureQueriesContext(connection) as first:
            filing_report(invoice)
        for index in range(12):
            make_invoice(supplier=self.other, ocr_text=f"AUTRE {index}\nSIREN {SIREN}")
        with CaptureQueriesContext(connection) as again:
            filing_report(invoice)
        self.assertEqual(len(again), len(first))

    def test_the_document_page_says_why_it_is_there(self):
        invoice = make_invoice(supplier=self.shop, ocr_text="EPICERIE EXEMPLE\nTOTAL 3,00")
        page = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(page, "Rangé chez Epicerie Exemple")
        self.assertContains(page, "son en-tête « EPICERIE EXEMPLE »")

    def test_the_correction_page_says_it_too_above_the_move_form(self):
        self.other.ticket_identifiers = [f"siren:{SIREN}"]
        self.other.save()
        invoice = make_invoice(
            supplier=self.shop,
            ocr_text=f"EPICERIE EXEMPLE\nSIREN {SIREN}",
            parse_checks=[{"label": "Lecture automatique", "passed": True, "detail": ""}],
        )
        page = self.client.get(reverse("invoices:receipt_review", args=[invoice.pk]))
        self.assertContains(page, "qui reconnaît")
        self.assertContains(page, "Grossiste Exemple")


class WordingTests(TestCase):
    """Two sentences the real data caught, each read off the page."""

    def test_a_siren_keeps_its_capitals_in_a_title(self):
        shop = make_supplier(code="MAJ_X", name="Maj Exemple", parser_key="", ticket_identifiers=[f"siren:{SIREN}"])
        titles = [rule["title"] for rule in filing_rules(shop) if rule["kind"] == "identifier"]
        # str.capitalize() made « n° SIREN 900 000 019 » read « N° siren … ».
        self.assertEqual(titles, ["N° SIREN 900 000 019"])

    def test_the_guard_does_not_speak_of_a_header_a_supplier_has_not_got(self):
        without = make_supplier(code="SANS_X", name="Sans Exemple", parser_key="")
        guard = next(rule for rule in filing_rules(without) if rule["kind"] == "guard")
        self.assertNotIn("son en-tête", guard["detail"])
        withheader = make_supplier(code="AVEC_X", name="Avec Exemple", parser_key="", ticket_header="AVEC EXEMPLE")
        guard = next(rule for rule in filing_rules(withheader) if rule["kind"] == "guard")
        self.assertIn("son en-tête", guard["detail"])
