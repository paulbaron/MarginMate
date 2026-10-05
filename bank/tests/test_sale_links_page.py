"""Banque's « Entrées » tab and a credit paying a « facture de vente »
(bank/views.py::bank_home, bank/_sale_links.html, bank:bank_line_action's
`sale_*` actions, bank:sale_document_search).

Each credit says the invoices it pays and what it gives each (the
allocation: oldest first, never beyond what one asks), what it leaves to
none, its suggestion when nothing is linked and nothing reads it, a pick-list
and a search. Every link and unlink goes through `bank:bank_line_action` -
the « Banque » gate - and the debit's own actions refuse a credit.

Every post here is read OFF THE PAGE (staff/tests/page_forms.py), as a
browser sends it. Every customer, payer, number, date and amount is invented.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib import messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bank import income, matching, sale_matching
from bank.models import BankTransaction, IncomePayer
from bank.sale_reconcile import DEBIT_PAYS_NO_SALE, link
from bank.tests.test_recognition_views import text_of
from bank.tests.test_reconcile import statement
from bank.views import (
    CREDIT_PAYS_NO_INVOICE,
    SALE_LINKED_MEANWHILE,
    SALE_UNLINKED,
    UNLINK_CHANGED_MEANWHILE,
)
from recipes.models import SaleDocument, SaleDocumentPayment
from staff.tests.page_forms import as_post, forms_of
from tests.factories import make_credit, make_invoice, make_sale_document
from tests.runner import employee_of_the_test_tenant

D = Decimal
CUSTOMER = "Exemple Événements SARL"
PAYER = "EXEMPLE EVENEMENTS SARL"
ENTRIES = {"vue": "entrees", "du": "2026-06-01", "au": "2026-06-30"}


def page_url() -> str:
    """The tab as the page builds it (`views._page_url`): where its forms come back to."""
    return f"{reverse('bank:bank_home')}?vue=entrees&du=2026-06-01&au=2026-06-30"


def sale(reference, total="500.00", sold_on=date(2026, 6, 1), customer=CUSTOMER, **fields) -> SaleDocument:
    return make_sale_document(reference=reference, customer=customer, sold_on=sold_on, stated_total_ttc=total, **fields)


def received(amount="500.00", day=date(2026, 6, 10), counterparty=PAYER, **fields) -> BankTransaction:
    fields.setdefault("label", f"VIR SEPA RECU /FRM {counterparty or 'INCONNU'} /REF {amount}")
    return make_credit(amount, day, counterparty=counterparty, **fields)


def payout(gross, net, day=date(2026, 6, 10)) -> BankTransaction:
    """A card terminal's payout, read by the seeded till rule."""
    label = f"VIR SEPA RECU /FRM BAR EXEMPLE /RNF TRANSFERT PRESTATAIRE INVENTE TOTAL ENCAISSE {gross} EUROS"
    return make_credit(net, day, counterparty="BAR EXEMPLE", label=label)


def linked_pairs() -> set[tuple[int, int]]:
    return set(SaleDocumentPayment.objects.values_list("transaction_id", "document_id"))


class EntriesPage:
    """The « Entrées » tab as a browser opens it, and one row of it."""

    def page(self, **extra) -> str:
        response = self.client.get(reverse("bank:bank_home"), {**ENTRIES, **extra})
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def row_of(self, html: str, line) -> str:
        start = html.index(f'id="entree-{line.pk}"')
        return html[start : html.index("</tr>", start)]

    def forms_in(self, html: str, line, action: str):
        return [
            form
            for form in forms_of(self.row_of(html, line))
            if form.method == "post" and any(c.name == "action" and c.value == action for c in form.controls)
        ]

    def suggested(self, html: str, line):
        """The row's suggestion forms - those of its pick-list aside."""
        return [
            form
            for form in self.forms_in(html, line, "sale_link")
            if not any(control.tag == "select" for control in form.controls)
        ]

    def post(self, form, **values):
        data = as_post(form.submission())
        for name, value in values.items():
            data[name] = value if isinstance(value, list) else [value]
        return self.client.post(form.action, data)

    def said(self, response) -> list[str]:
        said = [str(message) for message in messages.get_messages(response.wsgi_request)]
        self.client.cookies.pop("messages", None)
        return said


class RowsTests(EntriesPage, TestCase):
    def test_a_credit_says_each_invoice_with_its_share_and_what_goes_to_none(self):
        first = sale("FV-1", "600.00", date(2026, 5, 2))
        second = sale("FV-2", "300.00", date(2026, 5, 20))
        line = received("1000.00")
        link(line, [first, second])
        row = text_of(self.row_of(self.page(), line))
        self.assertIn(
            f"Facture de vente n° FV-1 · {CUSTOMER} · 02/05/2026 · 600.00 € pour elle (sur 600.00 € à recevoir)", row
        )
        self.assertIn(
            f"Facture de vente n° FV-2 · {CUSTOMER} · 20/05/2026 · 300.00 € pour elle (sur 300.00 € à recevoir)", row
        )
        self.assertIn(
            "Factures de vente : 900.00 € de cette entrée de 1 000.00 € ; 100.00 € ne vont à aucune facture.",
            row,
        )
        self.assertIn("À la main", row)
        address = reverse("recipes:sale_document_update", args=[first.pk])
        self.assertIn(f'href="{address}"', self.row_of(self.page(), line))
        # One « Délier » a document when it pays several, and « Tout délier ».
        self.assertEqual(len(self.forms_in(self.page(), line, "sale_unlink_document")), 2)
        (whole,) = self.forms_in(self.page(), line, "sale_unlink")
        self.assertEqual(
            sorted(c.value for c in whole.controls if c.name == "shown"), sorted([str(first.pk), str(second.pk)])
        )
        self.assertIn("Tout délier", text_of(self.row_of(self.page(), line)))

    def test_an_invoice_paid_by_another_credit_too(self):
        document = sale("FV-1", "500.00", date(2026, 5, 2))
        deposit = received("200.00", day=date(2026, 6, 3))
        balance = received("300.00", day=date(2026, 6, 12))
        link(deposit, [document])
        link(balance, [document])
        row = text_of(self.row_of(self.page(), balance))
        self.assertIn(f"Aussi réglée par l'entrée du 03/06/2026 ({PAYER}, 200.00 €)", row)
        self.assertIn("Une facture est aussi réglée par une autre entrée.", row)
        self.assertIn("300.00 € pour elle (sur 500.00 € à recevoir)", row)
        # One invoice: « Délier » for the whole line, none per document.
        self.assertEqual(self.forms_in(self.page(), balance, "sale_unlink_document"), [])
        self.assertIn("Délier", text_of(self.row_of(self.page(), balance)))

    def test_the_suggestion_and_its_form(self):
        document = sale("FV-1")
        line = received()
        html = self.page()
        row = self.row_of(html, line)
        self.assertIn(f'class="status-pill status-tier-{matching.SURE}"', row)
        self.assertIn("Montant exact au centime", text_of(row))
        (form,) = self.suggested(html, line)
        self.assertEqual([c.value for c in form.controls if c.name == "document"], [str(document.pk)])
        self.assertEqual(form.control("next").value, page_url())
        # The rules the tiers are decided by, once above the table.
        self.assertEqual(html.count("Comment une entrée est rapprochée d'une facture de vente"), 1)
        for _tier, title, _text in sale_matching.TIER_RULES:
            self.assertIn(title, html)

    def test_a_credit_unlinked_by_hand_is_never_said_sure(self):
        """The pass never comes back to it: « Certaine » - « le rapprochement
        la rattache de lui-même », stated above the table - would be false.
        « Quasi-sûre » with its reason, as the invoice's « Règlement » says."""
        sale("FV-1")
        line = received(settled_by_hand=True)
        row = self.row_of(self.page(), line)
        self.assertIn(f'class="status-pill status-tier-{matching.NEAR_SURE}"', row)
        self.assertNotIn(f"status-tier-{matching.SURE}", row)
        self.assertIn("Entrée déliée à la main : à rattacher par vous.", text_of(row))

    def test_no_suggestion_on_a_credit_a_person_chose(self):
        sale("FV-1")
        line = received(income_source=income.OTHER)
        html = self.page()
        self.assertEqual(self.forms_in(html, line, "sale_link"), [])
        self.assertNotIn("status-tier-", self.row_of(html, line))

    def test_the_pick_list_on_an_unrecognised_row_and_on_a_payout_only_when_asked(self):
        document = sale("FV-1", "980.00")
        line = received("120.00")
        terminal = payout("1000.00", "985.00", day=date(2026, 6, 11))
        html = self.page()
        (pick,) = [
            form for form in self.forms_in(html, line, "sale_link") if any(c.tag == "select" for c in form.controls)
        ]
        self.assertEqual(pick.control("document").options[0], ("", False))
        self.assertIn((str(document.pk), False), pick.control("document").options)
        self.assertEqual(
            [f for f in self.forms_in(html, terminal, "sale_link") if any(c.tag == "select" for c in f.controls)], []
        )
        asked = f"{page_url()}&rattacher={terminal.pk}#entree-{terminal.pk}"
        self.assertIn(f'href="{asked.replace("&", "&amp;")}"', self.row_of(html, terminal))
        html = self.page(rattacher=str(terminal.pk))
        self.assertEqual(
            len([f for f in self.forms_in(html, terminal, "sale_link") if any(c.tag == "select" for c in f.controls)]),
            1,
        )

    def test_the_search_its_fragment_and_its_plain_get(self):
        document = sale("FV-1", "980.00", sold_on=date(2025, 6, 1))
        line = received("120.00")
        html = self.page()
        (search,) = [form for form in forms_of(self.row_of(html, line)) if form.method == "get"]
        self.assertEqual(search.attrs["hx-get"], reverse("bank:sale_document_search", args=[line.pk]))
        self.assertEqual(search.control("ligne").value, str(line.pk))
        fragment = self.client.get(search.attrs["hx-get"], {"recherche": "FV-1", "next": page_url()}).content.decode()
        (found,) = forms_of(fragment)
        self.assertEqual([c.attrs["value"] for c in found.controls if c.name == "document"], [str(document.pk)])
        self.assertEqual(found.control("action").value, "sale_link")
        html = self.page(ligne=str(line.pk), recherche="FV-1")
        self.assertIn(f'value="{document.pk}"', self.row_of(html, line))

    def test_rapprocher_automatiquement_on_a_credit_unlinked_by_hand(self):
        line = received(settled_by_hand=True)
        (form,) = self.forms_in(self.page(), line, "sale_reopen")
        self.assertIn("Rapprocher automatiquement", text_of(self.row_of(self.page(), line)))
        self.assertEqual(form.control("next").value, page_url())

    def test_one_invoice_proposed_on_two_rows_is_said_on_both(self):
        sale("FV-1")
        first = received(day=date(2026, 6, 10))
        second = received(day=date(2026, 6, 12))
        html = self.page()
        self.assertIn("proposée aussi pour l'entrée du 12/06/2026", text_of(self.row_of(html, first)))
        self.assertIn("proposée aussi pour l'entrée du 10/06/2026", text_of(self.row_of(html, second)))

    def test_a_sale_row_says_its_invoice_once(self):
        document = sale("FV-1")
        line = received()
        link(line, [document])
        row = self.row_of(self.page(), line)
        self.assertIn("Facture de vente", text_of(row))
        self.assertNotIn('<span class="read-as">facture de vente n° FV-1</span>', row)
        self.assertEqual(text_of(row).count("n° FV-1"), 1)

    def test_no_a_classer_under_a_credit_a_person_linked(self):
        document = sale("FV-1", counting=SaleDocument.Counting.TILL)
        line = received()
        link(line, [document])
        self.assertNotIn("à classer", text_of(self.row_of(self.page(), line)))
        lone = received("33.00", counterparty="AUTRE PAYEUR")
        self.assertIn("à classer", text_of(self.row_of(self.page(), lone)))


class ActionTests(EntriesPage, TestCase):
    def test_a_suggestion_accepted_as_drawn(self):
        document = sale("FV-1")
        line = received()
        (form,) = self.suggested(self.page(), line)
        response = self.post(form)
        self.assertEqual(response["Location"], page_url())
        self.assertEqual(self.said(response), [f"Entrée rattachée à la facture de vente {document.label}."])
        payment = SaleDocumentPayment.objects.get()
        self.assertEqual(
            (payment.transaction_id, payment.document_id, payment.method), (line.pk, document.pk, "MANUAL")
        )

    def test_the_pick_list_s_blank_choice_is_said(self):
        sale("FV-1", "980.00")
        line = received("120.00")
        (pick,) = [
            form
            for form in self.forms_in(self.page(), line, "sale_link")
            if any(c.tag == "select" for c in form.controls)
        ]
        response = self.post(pick)
        self.assertEqual(self.said(response), ["Choisissez la facture de vente que cette entrée a réglée."])
        response = self.post(pick, document="²")
        self.assertEqual(self.said(response), ["Choisissez la facture de vente que cette entrée a réglée."])
        self.assertFalse(SaleDocumentPayment.objects.exists())

    def test_two_documents_found_and_ticked(self):
        first = sale("FV-1", "980.00", sold_on=date(2025, 6, 1))
        second = sale("FV-2", "20.00", sold_on=date(2025, 6, 2))
        line = received("1000.00")
        fragment = self.client.get(
            reverse("bank:sale_document_search", args=[line.pk]), {"recherche": "FV", "next": page_url()}
        ).content.decode()
        (form,) = forms_of(fragment)
        response = self.post(form, document=[str(first.pk), str(second.pk)])
        self.assertEqual(linked_pairs(), {(line.pk, first.pk), (line.pk, second.pk)})
        self.assertEqual(
            self.said(response), [f"Entrée rattachée aux factures de vente {first.label}, {second.label}."]
        )

    def test_the_line_s_own_choice_is_said_after_the_link(self):
        document = sale("FV-1")
        line = received(income_source=income.OTHER)
        response = self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]), {"action": "sale_link", "document": document.pk}
        )
        said = self.said(response)
        self.assertEqual(len(said), 1)
        self.assertTrue(
            said[0].endswith("choisissez « Automatique » sur « Entrées d'argent » pour la compter en facture de vente.")
        )

    def test_one_document_off_its_credit(self):
        first, second = sale("FV-1", "300.00"), sale("FV-2", "200.00")
        line = received()
        link(line, [first, second])
        forms = self.forms_in(self.page(), line, "sale_unlink_document")
        form = next(f for f in forms if f.control("document").value == str(first.pk))
        response = self.post(form)
        self.assertEqual(self.said(response), [f"Facture de vente {first.label} détachée de cette entrée."])
        self.assertEqual(linked_pairs(), {(line.pk, second.pk)})
        # The same form again: a page left open.
        response = self.post(form)
        self.assertEqual(self.said(response), [f"Cette entrée ne règle pas la facture de vente {first.label}."])
        response = self.post(form, document="abc")
        self.assertEqual(self.said(response), ["Choisissez la facture de vente à détacher de cette entrée."])

    def test_tout_delier_takes_off_what_its_row_showed_or_nothing(self):
        first, second = sale("FV-1", "300.00"), sale("FV-2", "200.00")
        line = received()
        link(line, [first])
        (form,) = self.forms_in(self.page(), line, "sale_unlink")
        link(line, [second])
        response = self.post(form)
        self.assertEqual(self.said(response), [UNLINK_CHANGED_MEANWHILE])
        self.assertEqual(len(linked_pairs()), 2)
        (form,) = self.forms_in(self.page(), line, "sale_unlink")
        response = self.post(form)
        self.assertEqual(self.said(response), [SALE_UNLINKED])
        self.assertEqual(linked_pairs(), set())
        line.refresh_from_db()
        self.assertTrue(line.settled_by_hand)

    def test_rapprocher_automatiquement_links_it_again(self):
        document = sale("FV-1")
        line = received(settled_by_hand=True)
        (form,) = self.forms_in(self.page(), line, "sale_reopen")
        response = self.post(form)
        self.assertEqual(
            self.said(response), ["Entrée rendue au rapprochement automatique, et rattachée à sa facture de vente."]
        )
        self.assertEqual(linked_pairs(), {(line.pk, document.pk)})

    def test_rapprocher_automatiquement_on_a_line_linked_meanwhile(self):
        document = sale("FV-1", "900.00")
        line = received(settled_by_hand=True)
        (form,) = self.forms_in(self.page(), line, "sale_reopen")
        link(line, [document])
        response = self.post(form)
        self.assertEqual(self.said(response), [SALE_LINKED_MEANWHILE])
        line.refresh_from_db()
        self.assertTrue(line.settled_by_hand)

    def test_the_place_a_message_belongs_to(self):
        document = sale("FV-1")
        line = received()
        response = self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]),
            {"action": "sale_link", "document": document.pk, "lieu": "reglements"},
        )
        (said,) = list(messages.get_messages(response.wsgi_request))
        self.assertIn("reglements", said.extra_tags.split())
        self.client.cookies.pop("messages", None)
        response = self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]),
            {"action": "sale_unlink_document", "document": document.pk, "lieu": "ailleurs"},
        )
        (said,) = list(messages.get_messages(response.wsgi_request))
        self.assertFalse(said.extra_tags)

    def test_a_debit_pays_no_sale(self):
        document = sale("FV-1")
        debit = make_credit("-500.00", date(2026, 6, 10), label="PRLV SEPA EXEMPLE", kind=BankTransaction.Kind.DEBIT)
        for action, data in (
            ("sale_link", {"document": document.pk}),
            ("sale_unlink_document", {"document": document.pk}),
            ("sale_unlink", {"shown": document.pk}),
            ("sale_reopen", {}),
        ):
            with self.subTest(action=action):
                response = self.client.post(
                    reverse("bank:bank_line_action", args=[debit.pk]), {"action": action, **data}
                )
                self.assertEqual(self.said(response), [DEBIT_PAYS_NO_SALE])
        debit.refresh_from_db()
        self.assertFalse(debit.settled_by_hand)
        self.assertFalse(SaleDocumentPayment.objects.exists())

    def test_the_debit_s_actions_refuse_a_credit(self):
        """No form of theirs is drawn on a credit: a stale or crafted POST -
        and `reopen` would have handed back a credit a person unlinked from a
        sale, `no_invoice` frozen it."""
        line = received(settled_by_hand=True)
        invoice = make_invoice()
        before = BankTransaction.objects.filter(pk=line.pk).values().get()
        for action, data in (
            ("no_invoice", {}),
            ("reopen", {}),
            ("unlink", {"shown": invoice.pk}),
            ("unlink_invoice", {"invoice": invoice.pk}),
            ("link", {"invoice": invoice.pk}),
        ):
            with self.subTest(action=action):
                response = self.client.post(
                    reverse("bank:bank_line_action", args=[line.pk]), {"action": action, **data}
                )
                self.assertEqual(self.said(response), [CREDIT_PAYS_NO_INVOICE])
        self.assertEqual(BankTransaction.objects.filter(pk=line.pk).values().get(), before)


class AccessTests(EntriesPage, TestCase):
    def test_banque_alone_names_the_invoices_never_their_page(self):
        document = sale("FV-1")
        line = received()
        link(line, [document])
        unlinked = received("77.00", counterparty="AUTRE PAYEUR")
        self.client.force_login(employee_of_the_test_tenant("banque@example.invalid", ["bank"]))
        html = self.page()
        self.assertIn("n° FV-1", text_of(self.row_of(html, line)))
        self.assertNotIn(reverse("recipes:sale_document_update", args=[document.pk]), html)
        fragment = self.client.get(
            reverse("bank:sale_document_search", args=[unlinked.pk]), {"recherche": "FV-1"}
        ).content.decode()
        self.assertIn("n° FV-1", fragment)
        self.assertNotIn(reverse("recipes:sale_document_update", args=[document.pk]), fragment)

    def test_recettes_alone_cannot_link(self):
        document = sale("FV-1")
        line = received()
        self.client.force_login(employee_of_the_test_tenant("ventes@example.invalid", ["recipes"]))
        response = self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]), {"action": "sale_link", "document": document.pk}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SaleDocumentPayment.objects.exists())


class MessagesTests(EntriesPage, TestCase):
    def test_the_import_counts_the_credits_it_linked(self):
        sale("FV-1", sold_on=date(2026, 7, 1))
        row = f"09/07/2026;VIREMENT RECU;VIR SEPA RECU;VIR SEPA RECU /FRM {PAYER} /REF FV-1;09/07/2026;500,00"
        response = self.client.post(
            reverse("bank:bank_home"), {"files": [SimpleUploadedFile("juillet.csv", statement(row))]}
        )
        (said,) = self.said(response)
        self.assertTrue(said.endswith(" à leur facture, et 1 entrée(s) à une facture de vente."), said)

    def test_rapprocher_automatiquement_counts_them(self):
        sale("FV-1")
        received()
        response = self.client.post(reverse("bank:bank_reconcile"))
        self.assertEqual(self.said(response), ["1 entrée(s) rattachée(s) automatiquement à une facture de vente."])


#: The tables a credit's sale links are read from.
SALE_TABLES = re.compile(
    r'"(recipes_saledocument|recipes_saledocumentline|recipes_saledocumentpayment|staff_establishment)"'
)


class QueriesTests(EntriesPage, TestCase):
    """The sale links, the documents near the credits shown and the bar's own
    names: six queries, only when a credit is shown."""

    def sale_queries(self, **params) -> int:
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("bank:bank_home"), {**ENTRIES, **params})
        return sum(1 for query in queries.captured_queries if SALE_TABLES.search(query["sql"]))

    def test_six_with_a_credit_shown_none_otherwise(self):
        for number in range(3):
            document = sale(f"FV-{number}", seller_name="Bar des tests")
            link(received(f"{number + 1}00.00", day=date(2026, 6, 10 + number)), [document])
        received("42.00", counterparty="AUTRE PAYEUR")
        IncomePayer.objects.create(key="AUTRE PAYEUR", source=income.OTHER)
        self.assertEqual(self.sale_queries(), 6)
        self.assertEqual(self.sale_queries(vue="toutes"), 0)
        self.assertEqual(self.sale_queries(du="2026-08-01", au="2026-08-31"), 0)

    def test_the_till_rules_read_each_credit_once(self):
        """What each credit is, read once (`income.entry_for`): the sales
        rows ask that reading, never the rules again - a rule's time is
        counted over the reading (recognition.RULE_SECONDS), and run twice
        over the tab it would be found « trop lent » twice as soon."""
        sale("FV-1")
        received()
        payout("1000.00", "985.00")
        with mock.patch("bank.income.automatic_source", wraps=income.automatic_source) as asked:
            self.page()
        self.assertEqual(asked.call_count, 2)
