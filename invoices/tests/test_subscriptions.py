"""One company's two subscriptions filed under one supplier - a box and a
mobile line - and the mobile bills moved to a supplier of their own.

A header only adds documents: giving the box's text to the supplier never
sends the mobile bills away, since they were filed there by the company
number and web site they print, which the supplier learned from them. Moved
one at a time, the first ones teach their new supplier nothing: the siblings
left behind print the same figures, so nothing is the new supplier's alone
until the last one has moved. Moved together (move_documents), they teach
it at once; what both sides print (the customer's own number) still names
neither. Where the two sides print the same company number too (two meters,
two sites), only a header tells them apart.

No page splits a supplier any more (19/09): a source is a supplier of its
own from the start, and a document filed under the wrong one is moved from
its own page.

Data invented; the layouts are the two real subscriptions'.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from inventory.models import Product
from inventory.services import expense_product
from inventory.views import charge_suppliers
from invoices.models import Invoice, Supplier, SupplierChange
from invoices.receipts import (
    UnrecognisedShopError,
    check_header,
    create_shop,
    detect_parser,
    header_choices,
    identifiers_naming,
    move_documents,
    move_to_shop,
    prints_header,
    recognise_shop,
)
from tests.factories import make_invoice, make_invoice_line, make_supplier

D = Decimal
SIREN = "900000019"
BOX_HEADER = "ABONNEMENT BOX EXEMPLE"


def box_text(day):
    return f"""Facture Box
AU COMPTOIR
JEAN EXEMPLE
140 RUE DES LILAS
75010 PARIS
Date de facture : {day:%d/%m/%Y}
{BOX_HEADER}  29,99
Total  29,99
assistance.operateur-exemple.fr"""


def mobile_text(day):
    return f"""SiteInternet:mobile.operateur-exemple.fr
Service abonne au 3244
Forfait Exemple 5G  AU COMPTOIR
N de ligne: 06 12 34 56 78  JEAN EXEMPLE
Date de facture : {day:%d/%m/%Y}
Forfait Exemple 5G  9,99
Total  9,99
SIREN {SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}"""


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def mobile_supplier():
    """Where the mobile bills belong: a supplier of charges of their own."""
    return make_supplier(code="MOBILE_X", name="Mobile Exemple", parser_key="", expenses_only=True)


class Subscriptions(TestCase):
    """An operator filed as one supplier of charges: three box bills
    recognised by the header a person gave, two mobile bills by the figures
    it learned from them. A wholesaler's invoice prints the customer's own
    phone number too."""

    def setUp(self):
        self.today = timezone.localdate()
        self.operator = make_supplier(
            code="OPERATEUR_X",
            name="Operateur Exemple",
            parser_key="",
            expenses_only=True,
            ticket_header=BOX_HEADER,
            ticket_identifiers=[f"siren:{SIREN}", "web:mobile.operateur-exemple.fr"],
        )
        self.poste = expense_product(self.operator)
        self.boxes = [self.bill(box_text, days, "29.99", "24.99") for days in (10, 40, 70)]
        self.mobiles = [self.bill(mobile_text, days, "9.99", "8.33") for days in (20, 50)]
        grossiste = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple", parser_key="")
        make_invoice(supplier=grossiste, source_text="GROSSISTE EXEMPLE\nClient : 06 12 34 56 78\nTOTAL 12,00")

    def bill(self, text_of, days_ago, ttc, ht):
        day = self.today - timedelta(days=days_ago)
        invoice = make_invoice(supplier=self.operator, invoice_date=day, ocr_text=text_of(day))
        make_invoice_line(
            invoice=invoice, product=self.poste, raw_name=self.operator.name, total_ht=ht,
            vat_rate=D("0.20"), printed_ttc=D(ttc),
        )
        return invoice


class MoveTogetherTests(Subscriptions):
    def test_one_move_at_a_time_teaches_nothing_until_the_last(self):
        """The sibling left behind prints the same figures, so they are
        nobody's alone - until it has moved too: « Changer de fournisseur »
        on each of them gets there, the last one teaching."""
        mobile = create_shop("Mobile Exemple", expenses_only=True)
        move_to_shop(self.mobiles[0], mobile)
        mobile.refresh_from_db()
        self.assertEqual(mobile.ticket_identifiers, [])
        move_to_shop(self.mobiles[1], mobile)
        mobile.refresh_from_db()
        # Not the customer's own phone, which the wholesaler's invoice prints.
        self.assertEqual(mobile.ticket_identifiers, [f"siren:{SIREN}", "web:mobile.operateur-exemple.fr"])

    def test_moving_them_together_teaches_the_new_supplier_what_they_share(self):
        mobile = create_shop("Mobile Exemple", expenses_only=True)
        moved = move_documents(self.mobiles, mobile)
        mobile.refresh_from_db()
        self.operator.refresh_from_db()
        self.assertEqual(moved.count, 2)
        self.assertEqual(
            set(Invoice.objects.filter(supplier=mobile)), set(Invoice.objects.filter(pk__in=[m.pk for m in self.mobiles]))
        )
        # Its company number and web site; not the customer's own phone,
        # which the wholesaler's invoice prints too.
        self.assertEqual(mobile.ticket_identifiers, [f"siren:{SIREN}", "web:mobile.operateur-exemple.fr"])
        self.assertEqual((self.operator.ticket_identifiers, self.operator.ticket_header), ([], BOX_HEADER))

    def test_the_next_invoice_of_each_side_lands_there(self):
        mobile = create_shop("Mobile Exemple", expenses_only=True)
        move_documents(self.mobiles, mobile)
        later = self.today + timedelta(days=1)
        self.assertEqual(detect_parser(mobile_text(later)).supplier_code, mobile.code)
        self.assertEqual(detect_parser(box_text(later)).supplier_code, self.operator.code)
        self.assertIsNone(detect_parser("N de ligne: 06 12 34 56 78\nTOTAL 9,99"))

    def test_charges_moved_together_take_the_new_name(self):
        mobile = mobile_supplier()
        move_documents(self.mobiles, mobile)
        for invoice in self.mobiles:
            (line,) = Invoice.objects.get(pk=invoice.pk).lines.all()
            self.assertEqual((line.raw_name, line.printed_ttc, line.total_ht), ("Mobile Exemple", D("9.99"), D("8.33")))
            self.assertTrue(line.product.is_expense)
            self.assertFalse(line.product.needs_review)
            self.assertEqual(line.product.supplier, mobile)
        self.assertEqual(
            sorted((row["supplier"].name, row["documents"], row["total_ttc"]) for row in charge_suppliers()),
            [("Mobile Exemple", 2, D("19.98")), ("Operateur Exemple", 3, D("89.97"))],
        )

    def test_nothing_moves_when_one_document_cannot(self):
        mobile = mobile_supplier()
        make_invoice(supplier=mobile, invoice_number=self.mobiles[1].invoice_number)
        with self.assertRaisesMessage(ValueError, "a déjà un document"):
            move_documents(self.mobiles, mobile)
        self.assertEqual(Invoice.objects.filter(supplier=self.operator).count(), 5)
        self.assertEqual(Invoice.objects.filter(supplier=mobile).count(), 1)

    def test_two_documents_carrying_one_number_do_not_go_together(self):
        """One supplier cannot hold one number twice: moved together, two
        documents printing it - two suppliers' - would make it so. Refused
        before anything moves."""
        other = make_supplier(code="AUTRE_X", name="Autre Exemple", parser_key="", expenses_only=True)
        twin = make_invoice(supplier=other, invoice_number=self.mobiles[0].invoice_number)
        mobile = mobile_supplier()
        with self.assertRaisesMessage(ValueError, f"Deux des documents portent le n° {self.mobiles[0].invoice_number}"):
            move_documents([*self.mobiles, twin], mobile)
        self.assertEqual(Invoice.objects.filter(supplier=self.operator).count(), 5)
        self.assertEqual(Invoice.objects.get(pk=twin.pk).supplier, other)
        self.assertFalse(Invoice.objects.filter(supplier=mobile).exists())

    def test_the_customers_phone_names_nobody_after_the_move(self):
        move_documents(self.mobiles, mobile_supplier())
        self.assertFalse(
            [supplier.name for supplier in Supplier.objects.all() if "tel:0612345678" in supplier.ticket_identifiers]
        )


class TwoMetersTests(TestCase):
    """One energy company, two sites: same company number, same web site on
    every bill. Nothing either side prints is its alone but the site, so the
    header is what tells them apart."""

    def setUp(self):
        self.energy = make_supplier(
            code="ENERGIE_X", name="Energie Exemple", parser_key="", expenses_only=True,
            ticket_identifiers=[f"siren:{SIREN}"],
        )
        self.lilas = [self.bill("SITE LILAS", day) for day in (1, 2)]
        self.roses = [self.bill("SITE ROSES", day) for day in (3, 4)]

    def bill(self, site, day):
        return make_invoice(
            supplier=self.energy,
            ocr_text=f"ENERGIE EXEMPLE\nSIREN {SIREN}\nwww.energie-exemple.fr\n{site}\nLe {day:02d}/05/2026\nTOTAL 80,00",
        )

    def test_they_are_told_apart_by_their_headers_only(self):
        Supplier.objects.filter(pk=self.energy.pk).update(ticket_header="SITE LILAS")
        self.energy.refresh_from_db()
        roses = make_supplier(
            code="ENERGIE_ROSES_X", name="Energie Roses", parser_key="", expenses_only=True, ticket_header="SITE ROSES"
        )
        move_documents(self.roses, roses)
        self.energy.refresh_from_db()
        roses.refresh_from_db()
        self.assertEqual((self.energy.ticket_identifiers, roses.ticket_identifiers), ([], []))
        self.assertEqual(detect_parser("ENERGIE EXEMPLE\nSITE ROSES\nTOTAL 81,00").supplier_code, roses.code)
        self.assertEqual(detect_parser("ENERGIE EXEMPLE\nSITE LILAS\nTOTAL 79,00").supplier_code, self.energy.code)
        self.assertIsNone(detect_parser(f"ENERGIE EXEMPLE\nSIREN {SIREN}\nTOTAL 79,00"))


class WhatThePagesSayTests(Subscriptions):
    def test_saving_a_header_counts_the_documents_that_do_not_print_it(self):
        """What someone giving the box's text expected to see leave - and
        where one that is not the supplier's is moved from."""
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.boxes[0].pk]),
            {"action": "shop_header", "ticket_header": BOX_HEADER},
        )
        said = " ".join(messages_of(response))
        self.assertIn(f"2 des 5 documents Operateur Exemple ne portent pas « {BOX_HEADER} »", said)
        self.assertIn("Si l'un d'eux n'est pas de Operateur Exemple, changez-le de fournisseur depuis sa page.", said)
        self.assertNotIn("sépar", said.lower())

    def test_sources_show_what_names_each_supplier(self):
        whole = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", ticket_header="EAU EXEMPLE")
        for day in (1, 2):
            make_invoice(supplier=whole, ocr_text=f"EAU EXEMPLE\nLe {day:02d}/05/2026\nTOTAL 80,00")
        page = self.client.get(reverse("invoices:supplier_list"))
        self.assertContains(page, "en-tête sur 3 de ses 5 documents · 2 ne le portent pas")
        # Its header on every document: nothing to say.
        self.assertNotContains(page, "en-tête sur 2 de ses 2")
        # A supplier with a reader of its own is listed too, with what names it.
        self.assertContains(page, "Fournisseurs avec leur propre lecteur")


class NoSplitAnyMoreTests(Subscriptions):
    """The owner removed the split on 19/09: a source is a supplier of its
    own from the start, and a document filed under the wrong one is moved
    from its own page. Nothing links to it, offers it or points to it."""

    def test_its_page_and_its_kind_of_change_are_gone(self):
        response = self.client.get(reverse("invoices:supplier_detail", args=[self.operator.pk]) + "separer/")
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse("invoices:supplier_split", args=[self.operator.pk])
        self.assertNotIn("SPLIT", SupplierChange.Kind.values)

    def test_no_page_offers_it(self):
        """The supplier's page, the Sources tab, a document's page - each
        offered « Séparer des documents… » on these very documents."""
        for url in (
            reverse("invoices:supplier_detail", args=[self.operator.pk]),
            reverse("invoices:invoice_type_list"),
            reverse("invoices:supplier_list"),
            reverse("invoices:receipt_review", args=[self.mobiles[0].pk]),
        ):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertNotIn("sépar", page.content.decode().lower(), url)

    def test_a_supplier_kept_for_its_documents_says_to_move_each_one(self):
        """Said as a verb, not as a button's label: a ticket's page says
        « Changer d'enseigne », an invoice's « Changer de fournisseur »."""
        page = self.client.get(reverse("invoices:supplier_delete", args=[self.operator.pk]))
        shown = " ".join(page.content.decode().split())
        self.assertIn(
            "Changez ses documents de fournisseur depuis la page de chacun et rattachez ses sources de factures à un "
            "autre fournisseur, puis revenez ici.",
            shown,
        )
        self.assertNotIn("sépar", shown.lower())


class MoveOneTests(Subscriptions):
    def test_a_charge_moved_alone_to_another_charge_supplier_takes_its_name(self):
        mobile = make_supplier(code="MOBILE_X", name="Mobile Exemple", parser_key="", expenses_only=True)
        move_to_shop(self.mobiles[0], mobile)
        (line,) = Invoice.objects.get(pk=self.mobiles[0].pk).lines.all()
        self.assertEqual((line.raw_name, line.printed_ttc), ("Mobile Exemple", D("9.99")))

    def test_a_new_supplier_made_from_a_charge_document_is_charges(self):
        url = reverse("invoices:receipt_review", args=[self.mobiles[0].pk])
        page = self.client.get(url)
        self.assertContains(page, 'name="new_expenses" value="1" checked', html=False)
        self.client.post(url, {
            "action": "move_shop", "supplier": "new", "new_name": "Mobile Exemple", "new_header": "",
            "new_expenses": "1",
        })
        mobile = Supplier.objects.get(name="Mobile Exemple")
        self.assertTrue(mobile.expenses_only)
        self.assertFalse(Product.objects.filter(supplier=mobile, is_expense=False).exists())

    def move_first_mobile(self):
        return self.client.post(reverse("invoices:receipt_review", args=[self.mobiles[0].pk]), {
            "action": "move_shop", "supplier": "new", "new_name": "Mobile Exemple",
            "new_header": "Forfait Exemple 5G", "new_expenses": "1",
        })

    def siblings_named(self):
        """What is said of the mobile bill left behind under the operator."""
        return (
            f"« Forfait Exemple 5G » est aussi imprimé sur Operateur Exemple du {self.mobiles[1].invoice_date:%d/%m/%Y} : "
            "si ce sont des documents de Mobile Exemple, changez-les de fournisseur depuis la page de chacun."
        )

    def test_a_header_its_siblings_print_names_them(self):
        """The sibling left behind is named, to be moved from its own page -
        the last one moved teaches the new supplier what they all print."""
        said = " ".join(messages_of(self.move_first_mobile()))
        self.assertIn(self.siblings_named(), said)
        self.assertNotIn("sépar", said.lower())

    def test_a_header_given_from_its_page_names_them_too(self):
        mobile = create_shop("Mobile Exemple", expenses_only=True)
        move_to_shop(self.mobiles[0], mobile)
        response = self.client.post(
            reverse("invoices:receipt_review", args=[self.mobiles[0].pk]),
            {"action": "shop_header", "ticket_header": "Forfait Exemple 5G"},
        )
        mobile.refresh_from_db()
        self.assertEqual(mobile.ticket_header, "Forfait Exemple 5G")
        said = " ".join(messages_of(response))
        self.assertIn(self.siblings_named(), said)
        self.assertNotIn("sépar", said.lower())

    def test_a_header_its_siblings_print_is_refused(self):
        self.mobiles += [self.bill(mobile_text, days, "9.99", "8.33") for days in (80, 110, 140)]
        response = self.move_first_mobile()
        said = " ".join(messages_of(response))
        self.assertIn("est imprimé sur 4 tickets", said)
        self.assertNotIn("sépar", said.lower())
        self.assertEqual(Invoice.objects.get(pk=self.mobiles[0].pk).supplier, self.operator)


class RecognitionGuardTests(TestCase):
    def test_two_suppliers_headers_on_one_document_name_neither(self):
        """The longest used to win."""
        make_supplier(code="BOX_X", name="Box Exemple", parser_key="", ticket_header="ABONNEMENT BOX")
        make_supplier(code="MOB_X", name="Mobile Exemple", parser_key="", ticket_header="FORFAIT MOBILE EXEMPLE")
        text = "FORFAIT MOBILE EXEMPLE\nDécouvrez aussi notre ABONNEMENT BOX\nTOTAL 9,99"
        parser, _identifiers, conflict = recognise_shop(text)
        self.assertIsNone(parser)
        self.assertIn("Box Exemple et Mobile Exemple", conflict)

    def test_a_header_inside_another_gives_way_to_it(self):
        make_supplier(code="SABAH_X", name="Sabah", parser_key="", ticket_header="SABAH")
        make_supplier(code="EPICERIE_SABAH_X", name="Epicerie Sabah", parser_key="", ticket_header="EPICERIE SABAH")
        self.assertEqual(detect_parser("EPICERIE SABAH\nTOTAL 3,00").supplier_code, "EPICERIE_SABAH_X")

    def test_a_header_another_supplier_has_is_refused(self):
        make_supplier(code="BOX_X", name="Box Exemple", parser_key="", ticket_header="Abonnement Box")
        with self.assertRaisesMessage(ValueError, "est déjà l'en-tête de Box Exemple"):
            check_header("ABONNEMENT  box")

    def test_the_import_says_which_two_disagree(self):
        from invoices.tests.test_receipt_shop_choice import recognised
        from invoices.tests.test_unknown_shops import staged_file
        from unittest import mock

        from invoices.receipts import import_receipt

        make_supplier(code="BOX_X", name="Box Exemple", parser_key="", ticket_header="ABONNEMENT BOX")
        make_supplier(code="MOB_X", name="Mobile Exemple", parser_key="", ticket_identifiers=[f"siren:{SIREN}"])
        text = f"ABONNEMENT BOX\nSIREN {SIREN}\nLE 09/01/2026\nTOTAL 9,99"
        with mock.patch("invoices.receipts.recognise", return_value=recognised(text)):
            with self.assertRaises(UnrecognisedShopError) as raised:
                import_receipt(staged_file(self, "conflit.pdf"))
        self.assertIn("l'en-tête de Box Exemple mais le n° SIREN de Mobile Exemple", str(raised.exception))


class HeaderChoiceTests(TestCase):
    def test_a_long_line_is_cut_at_a_word_and_still_found_on_its_document(self):
        """Cut inside a word ("...depuisu"), the chip offered from a document
        no longer matched that very document."""
        text = "Serviceabonne au3244(appelgratuitdepuisuneligneExemple)\nForfait Exemple 5G\nTOTAL 9,99"
        choices = header_choices(text)
        self.assertTrue(choices)
        for choice in choices:
            self.assertTrue(prints_header(text, choice), choice)

    def test_a_line_carrying_the_documents_own_number_is_not_offered(self):
        """The next bill prints another number: as a header it would match
        this one document and no other."""
        text = "Facture no 2500000001 du 19 janvier\nForfait Exemple 5G\n12 RUE DES LILAS\nTOTAL 9,99"
        self.assertEqual(header_choices(text), ["Forfait Exemple 5G", "12 RUE DES LILAS"])

    def test_one_word_longer_than_a_header_is_not_offered(self):
        text = "Serviceabonne´au3244(appelgratuitdepuisuneligneExemple)\nForfait Exemple 5G\nTOTAL 9,99"
        self.assertEqual(header_choices(text), ["Forfait Exemple 5G"])

    def test_the_review_page_offers_no_header_the_save_would_refuse(self):
        """The customer's own street is printed on every supplier's
        documents: offered as a chip, it was one click from a refusal."""
        street = "140 RUE DES LILAS"
        for code in ("UN_X", "DEUX_X"):
            other = make_supplier(code=code, name=code, parser_key="")
            make_invoice(supplier=other, ocr_text=f"AUTRE\n{street}\nTOTAL 1,00")
        shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        ticket = make_invoice(
            supplier=shop, ocr_text=f"EPICERIE EXEMPLE\n{street}\nTOTAL 2,00",
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        )
        page = self.client.get(reverse("invoices:receipt_review", args=[ticket.pk]))
        self.assertIn("EPICERIE EXEMPLE", page.context["header_choices"])
        self.assertNotIn(street, page.context["header_choices"])


class IdentifiersNamingTests(TestCase):
    """The learning arithmetic, with no database."""

    PHONE = "tel:0123456789"

    def texts(self, printing, total):
        return ["TEL 01 23 45 67 89"] * printing + ["RIEN"] * (total - printing)

    def test_a_quarter_of_its_documents_is_enough(self):
        self.assertEqual(identifiers_naming(self.texts(1, 4), [], {self.PHONE}), {self.PHONE})

    def test_less_is_not(self):
        self.assertEqual(identifiers_naming(self.texts(1, 5), [], {self.PHONE}), set())

    def test_printed_by_another_suppliers_document_it_names_nobody(self):
        self.assertEqual(identifiers_naming(self.texts(4, 4), ["Client TEL 01 23 45 67 89"], {self.PHONE}), set())

    def test_no_documents_or_no_candidates_name_nothing(self):
        self.assertEqual(identifiers_naming([], [], {self.PHONE}), set())
        self.assertEqual(identifiers_naming(self.texts(4, 4), [], set()), set())


class WhatAMoveTeachesTests(Subscriptions):
    """A move teaches the new supplier what the documents print, corrects
    whoever else knew it, and counts the charge lines renamed."""

    def test_a_third_supplier_sharing_what_they_print_is_corrected(self):
        """The wholesaler and the operator had both learned the customer's
        number, so it named neither; the operator forgetting it left the
        wholesaler its only owner."""
        grossiste = Supplier.objects.get(code="GROSSISTE_X")
        Supplier.objects.filter(pk=grossiste.pk).update(ticket_identifiers=["tel:0612345678"])
        Supplier.objects.filter(pk=self.operator.pk).update(
            ticket_identifiers=[f"siren:{SIREN}", "tel:0612345678", "web:mobile.operateur-exemple.fr"]
        )
        move_documents(self.mobiles, mobile_supplier())
        grossiste.refresh_from_db()
        self.assertEqual(grossiste.ticket_identifiers, [])
        self.assertIsNone(detect_parser("Client : 06 12 34 56 78\nTOTAL 3,00"))

    def test_what_both_sides_print_is_not_learned(self):
        """Both sides print the company number: the mobile side keeps only
        its web site, and a web site alone names no one."""
        for box in self.boxes:
            box.ocr_text += f"\nSIREN {SIREN}"
            box.save(update_fields=["ocr_text"])
        mobile = mobile_supplier()
        move_documents(self.mobiles, mobile)
        mobile.refresh_from_db()
        self.assertEqual(mobile.ticket_identifiers, ["web:mobile.operateur-exemple.fr"])
        self.assertIsNone(detect_parser(mobile_text(self.today + timedelta(days=1))))

    def test_the_charge_lines_renamed_are_counted(self):
        self.assertEqual(move_documents(self.mobiles, mobile_supplier()).renamed, 2)


class ALearnedCompanyNumberBlockingTests(TestCase):
    def test_filing_a_document_by_hand_corrects_a_supplier_that_learned_it_alone(self):
        """A rent supplier had learned the customer's company number while
        it was the only one printing it: every bill of the water company,
        header and all, was then refused - until a person filed one."""
        loyer = make_supplier(
            code="LOYER_X", name="Loyer Exemple", parser_key="", ticket_identifiers=[f"siren:{SIREN}"]
        )
        make_invoice(supplier=loyer, ocr_text=f"LOYER\nClient SIREN {SIREN}\nTOTAL 800,00")
        eau = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", ticket_header="EAU EXEMPLE")
        bill = f"EAU EXEMPLE\nClient SIREN {SIREN}\nTOTAL 80,00"
        self.assertIsNone(detect_parser(bill))
        from invoices.receipts import learn_identifiers

        make_invoice(supplier=eau, ocr_text=bill)
        learn_identifiers(eau, bill)
        loyer.refresh_from_db()
        self.assertEqual(loyer.ticket_identifiers, [])
        self.assertEqual(detect_parser(bill.replace("80,00", "81,00")).supplier_code, "EAU_X")


class MovingKeepsWhatTheLinesDoNotSayTests(Subscriptions):
    def test_a_charge_whose_total_was_never_read_stays_to_be_checked(self):
        from invoices.importing import UNREAD_CHARGE

        unread = self.mobiles[0]
        Invoice.objects.filter(pk=unread.pk).update(
            printed_total_ttc=None, status=Invoice.Status.NEEDS_REVIEW, error_message=UNREAD_CHARGE
        )
        mobile = make_supplier(code="MOBILE_X", name="Mobile Exemple", parser_key="", expenses_only=True)
        move_to_shop(Invoice.objects.get(pk=unread.pk), mobile)
        moved = Invoice.objects.get(pk=unread.pk)
        self.assertEqual((moved.status, moved.error_message), (Invoice.Status.NEEDS_REVIEW, UNREAD_CHARGE))

    def test_a_document_read_as_goods_moved_into_charges_is_read_again_as_one(self):
        """A rent statement read as a ticket - previous balance, direct
        debit, rent - kept its three lines as postes: three times what it
        charges."""
        from invoices.tests.test_expense_suppliers import STATEMENT

        agence = make_supplier(code="AGENCE_X", name="Agence Exemple", parser_key="")
        statement = make_invoice(
            supplier=agence, ocr_text=STATEMENT, invoice_date=self.today - timedelta(days=5),
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": False, "detail": ""}],
        )
        for name in ("SOLDE PRECEDENT", "PRELEVEMENT", "LOYER"):
            make_invoice_line(invoice=statement, raw_name=name, total_ht="830.00", vat_rate=D("0"), printed_ttc=D("830.00"))
        response = self.client.post(reverse("invoices:receipt_review", args=[statement.pk]), {
            "action": "move_shop", "supplier": "new", "new_name": "Bailleur Exemple", "new_header": "",
            "new_expenses": "1",
        })
        self.assertEqual(response.status_code, 302)
        (row,) = [row for row in charge_suppliers() if row["supplier"].name == "Bailleur Exemple"]
        self.assertEqual((row["documents"], row["total_ttc"]), (1, D("820.00")))

    def test_a_classified_line_keeps_its_stock_item(self):
        """Re-resolved as a new product at the new shop, the purchase left
        the stock ledger without a word."""
        from inventory.models import StockMovement
        from inventory.services import create_stock_movement_for_line
        from tests.factories import make_product, make_stock_type

        shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        vodka = make_stock_type(name="Vodka")
        product = make_product(supplier=shop, raw_name="VODKA 70CL", stock_type=vodka, stock_equivalent="0.7")
        tickets = []
        for day in (1, 2):
            ticket = make_invoice(supplier=shop, ocr_text=f"EPICERIE\nVODKA 70CL  15,00\nLe {day:02d}/05/2026")
            create_stock_movement_for_line(make_invoice_line(invoice=ticket, product=product, total_ht="12.50"))
            tickets.append(ticket)
        deux = create_shop("Epicerie Deux")
        moved = move_documents([tickets[1]], deux)
        (line,) = Invoice.objects.get(pk=tickets[1].pk).lines.all()
        self.assertEqual((line.product.supplier, line.product.stock_type), (deux, vodka))
        self.assertEqual(StockMovement.objects.filter(stock_type=vodka).count(), 2)
        self.assertEqual(moved.reclassified, 1)


class HeaderGuessTests(TestCase):
    def test_a_long_first_line_is_cut_at_a_word(self):
        from invoices.receipts import header_guess

        text = "EPICERIE FINE ET PRODUITS DU TERROIR DE LA VALLEE DE CHEVREUSE\nTOTAL 3,00"
        guess = header_guess(text)
        self.assertTrue(guess)
        self.assertTrue(prints_header(text, guess), guess)


class CorpusTests(TestCase):
    def test_it_is_read_once_while_no_document_changes(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from invoices.receipts import document_corpus

        make_invoice(ocr_text="EPICERIE EXEMPLE\nTOTAL 2,00")
        first = document_corpus()
        with CaptureQueriesContext(connection) as queries:
            again = document_corpus()
        # Kept, not read again: one small aggregate asks whether anything
        # changed, and no document's text is fetched.
        self.assertIs(again, first)
        self.assertEqual(len(queries.captured_queries), 1)
        self.assertNotIn('"ocr_text" FROM', queries.captured_queries[0]["sql"])
        make_invoice(ocr_text="AUTRE EXEMPLE\nTOTAL 3,00")
        self.assertEqual(len(document_corpus()), 2)


class ChargesPageAfterReviewTests(TestCase):
    def setUp(self):
        self.water = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)
        self.poste = expense_product(self.water)

    def bill(self, day, ttc):
        invoice = make_invoice(supplier=self.water, invoice_date=day)
        make_invoice_line(
            invoice=invoice, product=self.poste, raw_name=self.water.name, total_ht=ttc, vat_rate=D("0"),
            printed_ttc=D(ttc),
        )
        return invoice

    def test_the_first_stock_take_is_a_window_from_the_beginning(self):
        """Its window has no start, and a None in the filter was a 500."""
        from tests.factories import make_stock_take

        self.bill(timezone.localdate() - timedelta(days=3), "80.00")
        take = make_stock_take()
        page = self.client.get(reverse("inventory:stock_list") + f"?inventaire={take.pk}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual([row["documents"] for row in page.context["charge_suppliers"]], [1])

    def test_the_curve_adds_up_what_one_day_charged(self):
        day = timezone.localdate() - timedelta(days=3)
        self.bill(day, "100.00")
        self.bill(day, "300.00")
        self.bill(day - timedelta(days=30), "150.00")
        curve = self.client.get(reverse("inventory:charge_supplier_history", args=[self.water.pk]))
        self.assertIn("400.00 €", curve.context["chart_svg"])

    def test_a_document_with_nothing_read_is_listed(self):
        empty = make_invoice(supplier=self.water, invoice_date=timezone.localdate())
        self.bill(timezone.localdate() - timedelta(days=3), "80.00")
        panel = self.client.get(reverse("inventory:charge_supplier_documents", args=[self.water.pk]))
        self.assertIn(empty, [row["invoice"] for row in panel.context["rows"]])
        self.assertContains(panel, reverse("invoices:invoice_edit_lines", args=[empty.pk]))
