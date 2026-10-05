"""A « facture de vente »'s page and the « Ventes » tab's card, as a browser
uses them (recipes/views.py, sale_document_form.html,
_sale_documents_card.html).

Every post here is read OFF THE PAGE (staff/tests/page_forms.py) by a
client enforcing CSRF: a field misnamed in the template, a select drawn with
nothing selected, a form posting to the wrong address - a hand-written
request goes on passing and the owner's click does nothing, or the wrong
thing (CLAUDE.md « Formsets: test what the browser actually posts »). A row
the page's script would clone is added the way it clones it: the empty row's
names at the next index, TOTAL_FORMS raised.

Two kinds of document: a typed one (any plain file, a formset of lines,
every figure typed) and an electronic invoice (its figures read-only, its
lines tied in a grid named by line pk). Every refusal in French; a double
post one document; nothing typed lost without a word.

Every name, number, date and amount is invented.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from html import unescape
from unittest import mock

from django import forms
from django.contrib import messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from bank import income
from inventory.models import UnitChoices
from invoices.forms import NUL_REFUSED
from margins.computation import margins_for
from recipes import sale_files
from recipes.forms import (
    ARTICLE_RATE_PRICE,
    CONSUMED_CLEARED,
    CONSUMED_SIGN_TIE,
    CONSUMED_SIGN_TYPED,
    CONSUMED_ZERO,
    COUNTING_HELP,
    FILE_AGAIN,
    FREE_LINE_PRICE,
    HT_ABOVE_TTC,
    HT_WITHOUT_TTC,
    LINE_GONE,
    MIRROR_NOT_TIED,
    NEGATIVE_PRICE,
    NOTHING_SOLD,
    NUMBER_TAKEN,
    PRICE_AMBIGUOUS,
    PRICE_UNREADABLE,
    RATE_REFUSED,
    REBUILT_NOT_TIED,
    TOO_MANY_LINES_TYPED,
    UNKNOWN_CHOICE,
    SaleDocumentForm,
    SaleDocumentLineForm,
    SaleEInvoiceHeaderForm,
    SaleTiesForm,
)
from recipes.menu import SALE_CARD
from recipes.models import LINE_CONSUMED_UNTIED, LINE_NEEDS_A_LABEL, Recipe, SaleDocument, SaleDocumentLine
from recipes.sale_documents import (
    COUNTED_TWICE,
    DELETE_QUESTION,
    DEPOSIT_WITHOUT_FINAL_DAYS,
    NO_FINAL_INVOICE,
    deposit_doubts,
)
from recipes.sale_files import DATE_UNREADABLE, EINVOICE_ON_TYPED_FORM, read_einvoice_upload
from recipes.sale_payments import (
    PAID_SAID,
    PART_PAID_SAID,
    PILL_CREDIT_NOTE,
    PILL_PART_PAID,
    PILL_UNPAID,
    UNPAID_SAID,
)
from recipes.sales import sales_between
from recipes.tests.sale_einvoice_files import (
    CUSTOMER_NAME,
    SALE_CII,
    SALE_CII_CREDIT_NOTE,
    SALE_CII_DELIVERED,
    SALE_CII_DOCTYPE,
    SALE_CII_MINIMUM,
    SALE_CII_MIRROR_LINE,
    SALE_CII_NO_DATE,
    SALE_CII_UTF16,
    SALE_NUMBER,
)
from recipes.tests.test_sale_files import StoredFilesMixin, as_bytes, pdf_bytes, stored_files
from recipes.views import (
    ALREADY_SAVED_CREATE,
    ALREADY_SAVED_UPDATE,
    BANK_PASS_FAILED,
    CREDIT_NOTE_LINES,
    DOCUMENT_GONE,
    LEAVE_WARNING,
    LINE_ADDED,
    LINKED_TO_CREDIT,
    LINKED_WITHOUT_DETAIL,
    NO_CREDIT_PROPOSED,
    NOT_COUNTED_LINES,
    SAVE_BEFORE_PAYMENTS,
)
from staff.tests.page_forms import as_post, forms_of
from tests.factories import (
    make_credit,
    make_recipe,
    make_sale_document,
    make_sale_line,
    make_sale_payment,
    make_stock_type,
)
from tests.runner import employee_of_the_test_tenant
from tests.test_security_headers import EVENT_HANDLER, INLINE_SCRIPT, INLINE_STYLE
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

D = Decimal
#: The customer of SALE_CII as the bank prints a transfer from it.
CUSTOMER_PAYER = "EXEMPLE EVENEMENTS SARL"
CUSTOMER_TRANSFER = f"VIR SEPA RECU /FRM {CUSTOMER_PAYER} /REF {SALE_NUMBER}"
COUNTED, TILL, DEPOSIT = (SaleDocument.Counting.COUNTED, SaleDocument.Counting.TILL, SaleDocument.Counting.DEPOSIT)
WINDOW_QUERY = "du=2026-02-01&au=2026-09-30"
CREATE = "recipes:sale_document_create"


def update_url(document, query="") -> str:
    url = reverse("recipes:sale_document_update", args=[document.pk])
    return f"{url}?{query}" if query else url


def read(xml, name="FV-2026-0101.xml", counting=COUNTED, typed="") -> SaleDocument:
    """An electronic invoice as « Lire la facture » stores it."""
    upload = SimpleUploadedFile(name, as_bytes(xml))
    return read_einvoice_upload(upload, typed_date_text=typed, counting=counting).document


def text_of(html: str) -> str:
    """The page's words, tags and their attributes left out."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class PageMixin(StoredFilesMixin):
    """The pages as a browser opens and posts them."""

    def setUp(self):
        super().setUp()
        #: A client enforcing CSRF, as a browser does - logged in all the same.
        self.browser = self.client_class(enforce_csrf_checks=True)

    def open(self, url) -> str:
        response = self.browser.get(url)
        self.assertEqual(response.status_code, 200, url)
        assertNoUnrenderedTemplateSyntax(self, response, url)
        return response.content.decode()

    def document_form(self, html):
        found = [form for form in forms_of(html) if "sale-document-form" in form.attrs.get("class", "").split()]
        self.assertEqual(len(found), 1, "one form of the document")
        return found[0]

    def send(self, url, *, header=None, rows=None, press=None, files=None, drop=(), follow=False):
        """`url` opened, then its document's form posted as the browser
        sends it: what the page drew, `header` typed over it, `rows`
        ({index: {field: value}}) typed into the rows - those past the rows
        drawn added as the page's script clones the empty row - and `files`."""
        form = self.document_form(self.open(url))
        data = as_post(form.submission(press=press))
        for name, value in (header or {}).items():
            data[name] = [value]
        if rows:
            total = int(data["lines-TOTAL_FORMS"][0])
            for index, fields in rows.items():
                for field, value in fields.items():
                    key = f"lines-{index}-{field}"
                    if field == "DELETE":
                        if value:
                            data[key] = ["on"]
                        else:
                            data.pop(key, None)
                    else:
                        data[key] = [value]
                total = max(total, index + 1)
            data["lines-TOTAL_FORMS"] = [str(total)]
        for name in drop:
            data.pop(name, None)
        return self.browser.post(form.action, {**data, **(files or {})}, follow=follow)

    def said(self, response) -> list[str]:
        """The messages a redirect left - read as the next page reads them."""
        said = [str(message) for message in messages.get_messages(response.wsgi_request)]
        self.browser.cookies.pop("messages", None)
        return said

    def assertRefusedWith(self, response, *sentences):
        self.assertEqual(response.status_code, 200)
        page = text_of(response.content.decode())
        for sentence in sentences:
            self.assertIn(sentence, page)


class TypedFormTests(PageMixin, TestCase):
    """« + Facture de vente »: create and edit, every figure typed."""

    def setUp(self):
        super().setUp()
        self.mule = make_recipe(name="Mule exemple", selling_price_ttc="8.50")
        self.vodka = make_stock_type(name="Vodka exemple", unit=UnitChoices.LITRE)

    def test_a_document_can_hold_both_kinds_of_line_and_a_free_one(self):
        response = self.send(
            reverse(CREATE),
            header={"reference": "FV-T-1", "customer": "Mariage Exemple"},
            rows={
                0: {"source": f"recipe:{self.mule.pk}", "quantity": "3"},
                1: {"source": f"stock:{self.vodka.pk}", "quantity": "0,7", "unit_price_ttc": "20"},
                2: {"label": "Location de salle", "quantity": "1", "unit_price_ttc": "150"},
            },
        )
        self.assertEqual(response.status_code, 302)
        document = SaleDocument.objects.get()
        self.assertEqual(
            (document.reference, document.customer, document.counting), ("FV-T-1", "Mariage Exemple", COUNTED)
        )
        lines = list(document.lines.order_by("id"))
        self.assertEqual(
            [(line.recipe_id, line.stock_type_id, line.label, line.quantity, line.unit_price_ttc) for line in lines],
            [
                (self.mule.pk, None, "", D("3"), D("8.50")),
                (None, self.vodka.pk, "", D("0.7"), D("20.00")),
                (None, None, "Location de salle", D("1"), D("150.00")),
            ],
        )
        self.assertEqual(document.total_ttc, D("189.50"))

    def test_an_article_with_a_rate_and_its_price(self):
        response = self.send(
            reverse(CREATE),
            rows={
                0: {"source": f"stock:{self.vodka.pk}", "quantity": "2", "unit_price_ttc": "12,50", "vat_percent": "20"}
            },
        )
        self.assertEqual(response.status_code, 302)
        line = SaleDocumentLine.objects.get()
        self.assertEqual((line.unit_price_ttc, line.vat_rate), (D("12.50"), D("0.2000")))

    def test_a_typed_total_with_no_line(self):
        response = self.send(reverse(CREATE), header={"stated_total_ttc": "1 250,00"})
        self.assertEqual(response.status_code, 302)
        document = SaleDocument.objects.get()
        self.assertEqual((document.lines.count(), document.total_ttc), (0, D("1250.00")))

    def test_neither_a_line_nor_a_total_is_refused(self):
        response = self.send(reverse(CREATE))
        self.assertRefusedWith(response, NOTHING_SOLD)
        self.assertFalse(SaleDocument.objects.exists())

    def test_total_ht_is_typed_with_the_ttc_never_alone_nor_above_it(self):
        self.assertRefusedWith(self.send(reverse(CREATE), header={"stated_total_ht": "100"}), HT_WITHOUT_TTC)
        for ttc, ht in (("120", "130"), ("120", "-100"), ("-120", "100")):
            with self.subTest(ttc=ttc, ht=ht):
                response = self.send(reverse(CREATE), header={"stated_total_ttc": ttc, "stated_total_ht": ht})
                self.assertRefusedWith(response, HT_ABOVE_TTC)
        self.assertFalse(SaleDocument.objects.exists())
        response = self.send(reverse(CREATE), header={"stated_total_ttc": "120", "stated_total_ht": "100"})
        self.assertEqual(response.status_code, 302)
        document = SaleDocument.objects.get()
        self.assertEqual((document.stated_total_ttc, document.stated_total_ht), (D("120.00"), D("100.00")))
        self.assertTrue(document.states_its_ht)

    def test_the_counting_radio(self):
        html = self.open(reverse(CREATE))
        form = self.document_form(html)
        radios = [control for control in form.controls if control.name == "counting"]
        self.assertEqual([radio.attrs["value"] for radio in radios], [COUNTED, TILL, DEPOSIT])
        self.assertEqual([("checked" in radio.attrs) for radio in radios], [True, False, False])
        page = text_of(html)
        for sentence in COUNTING_HELP.values():
            self.assertIn(sentence, page)
        response = self.send(reverse(CREATE), header={"counting": TILL, "stated_total_ttc": "80"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SaleDocument.objects.get().counting, TILL)

    def test_the_customers_are_offered(self):
        for day, customer in ((1, "Mariage Exemple"), (2, "Exemple Événements SARL"), (3, "Mariage Exemple"), (4, "")):
            make_sale_document(sold_on=date(2026, 3, day), customer=customer)
        html = self.open(reverse(CREATE))
        self.assertIn('list="known-customers"', html)
        datalist = re.search(r'<datalist id="known-customers">(.*?)</datalist>', html, flags=re.DOTALL)
        self.assertIsNotNone(datalist)
        self.assertEqual(
            re.findall(r'<option value="([^"]*)">', datalist.group(1)), ["Mariage Exemple", "Exemple Événements SARL"]
        )

    def test_a_number_another_document_holds_is_refused(self):
        make_sale_document(reference="FV-77", sold_on=date(2026, 3, 1))
        response = self.send(reverse(CREATE), header={"reference": "fv-77", "stated_total_ttc": "10"})
        self.assertRefusedWith(response, NUMBER_TAKEN.format(number="FV-77", day="01/03/2026"))
        self.assertEqual(SaleDocument.objects.count(), 1)

    def test_two_documents_sharing_a_number_from_before_still_save(self):
        first = make_sale_document(reference="FV-88", stated_total_ttc="10.00")
        make_sale_document(reference="FV-88", stated_total_ttc="20.00")
        response = self.send(update_url(first), header={"note": "corrigée"})
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.note, "corrigée")

    def test_the_date_defaults_to_today(self):
        """A ModelForm seeds self.initial from the instance, so setting
        fields["sold_on"].initial is silently ignored and the box renders
        empty - which is a small thing you notice only by looking."""
        self.assertEqual(SaleDocumentForm()["sold_on"].value(), timezone.localdate())
        self.assertIn(f'value="{timezone.localdate().isoformat()}"', self.open(reverse(CREATE)))

    def test_editing_keeps_the_lines_and_saves_their_changes(self):
        document = make_sale_document(reference="FV-90")
        line = make_sale_line(document, recipe=self.mule, quantity="2", unit_price_ttc="8.50")
        response = self.send(update_url(document), rows={0: {"quantity": "5"}})
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.quantity, D("5"))
        self.assertEqual(document.lines.count(), 1)

    def test_a_saved_rate_is_drawn_as_a_percentage_of_two_decimals(self):
        """Stored 0.0550, drawn « 5.50 » - never the fraction times 100,
        « 5.5000 » - and saved untouched as it was."""
        document = make_sale_document(reference="FV-89")
        line = make_sale_line(document, stock_type=self.vodka, quantity="1", unit_price_ttc="20", vat_rate="0.0550")
        form = self.document_form(self.open(update_url(document)))
        self.assertEqual(form.control("lines-0-vat_percent").value, "5.50")
        self.assertEqual(self.send(update_url(document)).status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.vat_rate, D("0.0550"))

    def test_saved_it_goes_back_to_the_tab_and_says_so_in_the_card(self):
        response = self.send(f"{reverse(CREATE)}?{WINDOW_QUERY}", header={"stated_total_ttc": "80"}, follow=True)
        self.assertEqual(
            response.redirect_chain[-1][0], f"{reverse('recipes:sales_list')}?{WINDOW_QUERY}#factures-vente"
        )
        html = response.content.decode()
        card = html[html.index(f'id="{SALE_CARD}"') :]
        self.assertIn(escape(f"{SaleDocument.objects.get()} enregistrée."), card)

    def test_documents_appear_on_the_sales_page(self):
        document = make_sale_document(reference="T-9")
        make_sale_line(document, recipe=self.mule, quantity="2")
        html = self.open(reverse("recipes:sales_list"))
        self.assertIn("T-9", html)
        self.assertIn("Mule exemple", html)

    def test_a_document_can_be_deleted_from_the_tab(self):
        document = make_sale_document(reference="FV-91")
        make_sale_line(document, recipe=self.mule, quantity="2")
        html = self.open(reverse("recipes:sales_list"))
        delete = reverse("recipes:sale_document_delete", args=[document.pk])
        form = next(form for form in forms_of(html) if form.action.split("?")[0].split("#")[0] == delete)
        self.assertEqual(form.attrs["data-confirm"], DELETE_QUESTION.format(label=document.label))
        response = self.browser.post(form.action, as_post(form.submission()))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SaleDocument.objects.exists())
        self.assertEqual(self.said(response), ["Vente du 05/03/2026 (n° FV-91) supprimée."])

    def test_a_recipe_on_a_document_is_kept_with_a_message(self):
        """A document's line PROTECTs its recipe: deleting the recipe says
        where it is used, as an article does - not a 500."""
        for reference in ("T-1", "T-2"):
            document = make_sale_document(reference=reference)
            make_sale_line(document, recipe=self.mule, quantity="2")
            make_sale_line(document, recipe=self.mule, quantity="1")
        response = self.client.post(reverse("recipes:recipe_delete", kwargs={"pk": self.mule.pk}), follow=True)
        self.assertRedirects(response, reverse("recipes:recipe_detail", kwargs={"pk": self.mule.pk}))
        self.assertContains(response, "utilisée dans 2 document(s) de vente")
        self.assertTrue(Recipe.objects.filter(pk=self.mule.pk).exists())


class TypedLineRuleTests(PageMixin, TestCase):
    """What a typed line may say - each refusal a sentence of its own, under
    its row."""

    def setUp(self):
        super().setUp()
        self.mule = make_recipe(name="Mule exemple", selling_price_ttc="8.50")
        self.keg = make_stock_type(name="Fût exemple", unit=UnitChoices.LITRE)

    def test_an_article_with_a_rate_and_no_price_is_refused(self):
        """Its cost would come off 0,00 € of revenue (spec §2.6)."""
        response = self.send(
            reverse(CREATE), rows={0: {"source": f"stock:{self.keg.pk}", "quantity": "1", "vat_percent": "20"}}
        )
        self.assertRefusedWith(response, ARTICLE_RATE_PRICE)
        self.assertFalse(SaleDocument.objects.exists())

    def test_an_article_with_no_rate_and_no_price_is_today_s_rule(self):
        response = self.send(reverse(CREATE), rows={0: {"source": f"stock:{self.keg.pk}", "quantity": "1"}})
        self.assertEqual(response.status_code, 302)

    def test_a_tied_line_with_a_negative_price_is_refused_a_free_one_is_a_discount(self):
        response = self.send(
            reverse(CREATE), rows={0: {"source": f"recipe:{self.mule.pk}", "quantity": "1", "unit_price_ttc": "-8"}}
        )
        self.assertRefusedWith(response, NEGATIVE_PRICE)
        response = self.send(
            reverse(CREATE),
            rows={
                0: {"source": f"recipe:{self.mule.pk}", "quantity": "10"},
                1: {"label": "Remise fidélité", "quantity": "1", "unit_price_ttc": "-10"},
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SaleDocument.objects.get().total_ttc, D("75.00"))

    def test_a_free_line_needs_its_price(self):
        self.assertRefusedWith(
            self.send(reverse(CREATE), rows={0: {"label": "Location", "quantity": "1"}}), FREE_LINE_PRICE
        )

    def test_a_line_with_no_source_needs_a_label(self):
        """« Location de salle » is a line tied to nothing: it says what it
        is by its label (was « Choisissez une recette ou un article. »)."""
        self.assertRefusedWith(
            self.send(reverse(CREATE), rows={0: {"quantity": "1", "unit_price_ttc": "100"}}), LINE_NEEDS_A_LABEL
        )
        self.assertFalse(SaleDocument.objects.exists())

    def test_a_consumed_quantity_on_a_free_line_is_refused(self):
        response = self.send(
            reverse(CREATE),
            rows={0: {"label": "Location", "quantity": "1", "unit_price_ttc": "100", "consumed_quantity": "2"}},
        )
        self.assertRefusedWith(response, LINE_CONSUMED_UNTIED)

    def test_a_consumed_quantity_of_the_opposite_sign_is_refused(self):
        response = self.send(
            reverse(CREATE),
            rows={0: {"source": f"stock:{self.keg.pk}", "quantity": "1", "consumed_quantity": "-30"}},
        )
        self.assertRefusedWith(response, CONSUMED_SIGN_TYPED)

    def test_a_rate_outside_0_100_is_refused(self):
        for rate in ("120", "-5", "vingt"):
            with self.subTest(rate=rate):
                response = self.send(
                    reverse(CREATE),
                    rows={0: {"label": "Location", "quantity": "1", "unit_price_ttc": "1", "vat_percent": rate}},
                )
                self.assertRefusedWith(response, RATE_REFUSED)

    def test_a_tie_changed_with_the_consumed_box_as_drawn_clears_it(self):
        """30 typed in litres of a keg is not 30 cocktails: the box comes
        back to the quantity invoiced, and the page says so."""
        document = make_sale_document(reference="FV-92")
        line = make_sale_line(document, stock_type=self.keg, quantity="1", consumed_quantity="30", unit_price_ttc="150")
        response = self.send(update_url(document), rows={0: {"source": f"recipe:{self.mule.pk}"}})
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual((line.recipe_id, line.consumed_quantity), (self.mule.pk, None))
        self.assertIn(CONSUMED_CLEARED.format(label="Mule exemple"), self.said(response))

    def test_a_tie_changed_with_a_new_consumed_quantity_keeps_it(self):
        document = make_sale_document(reference="FV-93")
        line = make_sale_line(document, stock_type=self.keg, quantity="1", consumed_quantity="30", unit_price_ttc="150")
        other = make_stock_type(name="Fût exemple 20 L", unit=UnitChoices.LITRE)
        response = self.send(update_url(document), rows={0: {"source": f"stock:{other.pk}", "consumed_quantity": "20"}})
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual((line.stock_type_id, line.consumed_quantity), (other.pk, D("20")))


class KeptWhenAbsentTests(PageMixin, TestCase):
    """« Missing is not blank » (CLAUDE.md): a page left open from before
    the sales invoices, posted after them, blanks nothing."""

    def test_a_post_without_the_new_fields_keeps_them(self):
        document = make_sale_document(
            reference="FV-94",
            customer="Mariage Exemple",
            counting=TILL,
            stated_total_ttc="300.00",
            stated_total_ht="250.00",
            prepaid_ttc="100.00",
        )
        response = self.send(
            update_url(document),
            header={"note": "réglée"},
            drop=("customer", "counting", "stated_total_ttc", "stated_total_ht", "prepaid_ttc", "jeton"),
        )
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        self.assertEqual(
            (document.note, document.customer, document.counting),
            ("réglée", "Mariage Exemple", TILL),
        )
        self.assertEqual(
            (document.stated_total_ttc, document.stated_total_ht, document.prepaid_ttc),
            (D("300.00"), D("250.00"), D("100.00")),
        )

    def test_a_new_document_posted_without_them_is_counted_and_states_nothing(self):
        response = self.send(
            reverse(CREATE),
            rows={0: {"label": "Location", "quantity": "1", "unit_price_ttc": "100"}},
            drop=("customer", "counting", "stated_total_ttc", "stated_total_ht", "prepaid_ttc"),
        )
        self.assertEqual(response.status_code, 302)
        document = SaleDocument.objects.get()
        self.assertEqual((document.counting, document.customer, document.stated_total_ttc), (COUNTED, "", None))


class FrenchErrorsTests(PageMixin, TestCase):
    """LANGUAGE_CODE is en-us: every field says its refusals in French."""

    ENGLISH = ("This field", "Select a valid", "Ensure this value", "Enter a", "Null characters")

    def test_every_text_field_says_a_nul_in_french(self):
        """Every field Django checks for a NUL (its CharFields), on every
        form of the pages - a text field added later included. A figure
        (recipes.forms.TypedAmountField) is no text: a NUL in it is
        « illisible », in French."""
        document = make_sale_document(reference="FV-95", einvoice_format="CII")
        line = make_sale_line(document, label="Formule exemple", quantity="1", total_ht="10.00", vat_rate="0.20")
        texts = {}
        for form in (
            SaleDocumentForm(),
            SaleEInvoiceHeaderForm(instance=document),
            SaleDocumentLineForm(source_choices=[("", "—")]),
            SaleTiesForm(document, [line]),
        ):
            for name, field in form.fields.items():
                if isinstance(field, forms.CharField):
                    texts[f"{type(form).__name__}.{name}"] = field
        self.assertEqual(
            sorted(texts),
            [
                "SaleDocumentForm.customer",
                "SaleDocumentForm.note",
                "SaleDocumentForm.reference",
                "SaleDocumentLineForm.label",
                "SaleEInvoiceHeaderForm.note",
            ],
        )
        for name, field in texts.items():
            with self.subTest(field=name):
                self.assertEqual(field.error_messages["null_characters_not_allowed"], NUL_REFUSED)

    def test_a_line_deleted_in_another_tab(self):
        document = make_sale_document(reference="FV-96")
        line = make_sale_line(document, label="Location", unit_price_ttc="100")
        html = self.open(update_url(document))
        form = self.document_form(html)
        line.delete()
        response = self.browser.post(form.action, as_post(form.submission()))
        self.assertRefusedWith(response, LINE_GONE)

    def test_nothing_is_refused_in_english(self):
        recipe = make_recipe(name="Mule exemple", selling_price_ttc="8.50")
        response = self.send(
            reverse(CREATE),
            header={
                "sold_on": "",
                "reference": "x" * 101,
                "customer": "Mariage\x00Exemple",
                "counting": "gratuit",
                "note": "n" * 256,
                "stated_total_ttc": "beaucoup",
            },
            rows={
                0: {"source": "recipe:999999", "quantity": "deux", "label": "l" * 256},
                1: {"source": f"recipe:{recipe.pk}", "quantity": "", "unit_price_ttc": "1,234,5"},
            },
        )
        self.assertEqual(response.status_code, 200)
        page = text_of(response.content.decode())
        for words in self.ENGLISH:
            with self.subTest(words=words):
                self.assertNotIn(words, page)
        for sentence in ("Saisissez la date de la vente.", "100 caractères au plus.", NUL_REFUSED):
            self.assertIn(sentence, page)


class MaxLinesTests(PageMixin, TestCase):
    def test_more_than_500_typed_rows_are_refused_in_french(self):
        rows = {index: {"label": f"Ligne {index}", "quantity": "1", "unit_price_ttc": "1"} for index in range(501)}
        response = self.send(reverse(CREATE), rows=rows)
        self.assertRefusedWith(response, TOO_MANY_LINES_TYPED)
        self.assertFalse(SaleDocument.objects.exists())


class NonContiguousTests(PageMixin, TestCase):
    """A row removed in the browser leaves a gap in the posted indices: 0, 1
    and 3 with TOTAL_FORMS 4 (CLAUDE.md « Formsets: test what the browser
    actually posts »)."""

    def test_indices_with_a_gap_a_blank_row_and_a_row_taken_out(self):
        document = make_sale_document(reference="FV-97")
        kept = make_sale_line(document, label="Location", unit_price_ttc="100")
        removed = make_sale_line(document, label="Service", unit_price_ttc="20")
        url = update_url(document)
        form = self.document_form(self.open(url))
        data = as_post(form.submission())
        data["lines-1-DELETE"] = ["on"]
        data["lines-TOTAL_FORMS"] = ["4"]
        # Row 2 was added and removed in the browser: nothing of it is sent.
        # Row 3 is new; a fifth row is never sent at all.
        data.update({"lines-3-label": ["Vestiaire"], "lines-3-quantity": ["1"], "lines-3-unit_price_ttc": ["5"]})
        response = self.browser.post(form.action, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(sorted(document.lines.values_list("label", flat=True)), ["Location", "Vestiaire"])
        self.assertTrue(SaleDocumentLine.objects.filter(pk=kept.pk).exists())
        self.assertFalse(SaleDocumentLine.objects.filter(pk=removed.pk).exists())

    def test_a_blank_row_left_as_drawn_is_no_line(self):
        response = self.send(reverse(CREATE), header={"stated_total_ttc": "40"}, rows={2: {}})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SaleDocument.objects.get().lines.count(), 0)


class PriceErrorTests(PageMixin, TestCase):
    """A price typed as a person types it - « 12,50 » under en-us - or said
    why not, never dropped in silence (spec §9.2)."""

    def price(self, typed):
        return self.send(reverse(CREATE), rows={0: {"label": "Location", "quantity": "1", "unit_price_ttc": typed}})

    def test_a_comma_is_a_decimal_point(self):
        self.assertEqual(self.price("12,50").status_code, 302)
        self.assertEqual(SaleDocumentLine.objects.get().unit_price_ttc, D("12.50"))

    def test_three_decimals_or_nine_digits_are_refused(self):
        for typed in ("12,555", "123456789"):
            with self.subTest(typed=typed):
                self.assertRefusedWith(self.price(typed), PRICE_UNREADABLE)
        self.assertFalse(SaleDocument.objects.exists())

    def test_a_thousand_or_one_and_a_half_is_asked_again(self):
        self.assertRefusedWith(self.price("1,500"), PRICE_AMBIGUOUS)
        self.assertEqual(self.price("1 500").status_code, 302)
        self.assertEqual(SaleDocumentLine.objects.get().unit_price_ttc, D("1500.00"))


class SpareRowTests(PageMixin, TestCase):
    def test_a_saved_document_reopens_with_its_lines_only(self):
        document = make_sale_document(reference="FV-98")
        make_sale_line(document, label="Location", unit_price_ttc="100")
        make_sale_line(document, label="Service", unit_price_ttc="20")
        form = self.document_form(self.open(update_url(document)))
        self.assertEqual(form.control("lines-TOTAL_FORMS").value, "2")
        self.assertEqual(form.control("lines-INITIAL_FORMS").value, "2")

    def test_a_new_document_opens_with_one_row(self):
        form = self.document_form(self.open(reverse(CREATE)))
        self.assertEqual(form.control("lines-TOTAL_FORMS").value, "1")


class KeptPreparationTests(PageMixin, TestCase):
    """A preparation is not offered on a sale document - but a line written
    before its recipe's price was cleared still opens and saves (spec §9.1:
    `keep=` never reached the formset)."""

    def test_a_line_on_a_recipe_turned_preparation_opens_and_saves_untouched(self):
        syrup = make_recipe(name="Sirop maison exemple", selling_price_ttc="5.00")
        document = make_sale_document(reference="FV-99")
        line = make_sale_line(document, recipe=syrup, quantity="2", unit_price_ttc="5.00")
        Recipe.objects.filter(pk=syrup.pk).update(selling_price_ttc=None)
        html = self.open(update_url(document))
        form = self.document_form(html)
        self.assertEqual(form.control("lines-0-source").value, f"recipe:{syrup.pk}")
        response = self.browser.post(form.action, as_post(form.submission()))
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.recipe_id, syrup.pk)

    def test_it_is_still_not_offered_on_another_document(self):
        syrup = make_recipe(name="Sirop maison exemple", selling_price_ttc=None)
        form = self.document_form(self.open(reverse(CREATE)))
        options = [value for value, _selected in form.control("lines-0-source").options]
        self.assertNotIn(f"recipe:{syrup.pk}", options)


class DoublePostTests(PageMixin, TestCase):
    """The one-time `jeton` (spec §5.6): a page posted twice - a double tap,
    a phone resending after a slow answer - is one document."""

    def post_twice(self, url, **kwargs):
        form = self.document_form(self.open(url))
        data = as_post(form.submission())
        for name, value in kwargs.get("header", {}).items():
            data[name] = [value]
        for name, value in kwargs.get("extra", {}).items():
            data[name] = [value]
        first = self.browser.post(form.action, data)
        second = self.browser.post(form.action, data)
        return first, second

    def test_a_new_document_posted_twice(self):
        first, second = self.post_twice(reverse(CREATE), header={"stated_total_ttc": "80"})
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        document = SaleDocument.objects.get()
        self.assertEqual(second["Location"], update_url(document))
        self.assertIn(ALREADY_SAVED_CREATE, self.said(second))

    def test_another_document_typed_on_the_same_page_is_another_one(self):
        form = self.document_form(self.open(reverse(CREATE)))
        data = as_post(form.submission())
        data["stated_total_ttc"] = ["80"]
        self.browser.post(form.action, data)
        data["stated_total_ttc"] = ["90"]
        self.browser.post(form.action, data)
        self.assertEqual(SaleDocument.objects.count(), 2)

    def test_an_update_posted_twice_adds_its_line_once(self):
        document = make_sale_document(reference="FV-100")
        make_sale_line(document, label="Location", unit_price_ttc="100")
        first, second = self.post_twice(
            update_url(document),
            extra={
                "lines-TOTAL_FORMS": "2",
                "lines-1-label": "Service",
                "lines-1-quantity": "1",
                "lines-1-unit_price_ttc": "20",
            },
        )
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(document.lines.count(), 2)
        self.assertIn(ALREADY_SAVED_UPDATE, self.said(second))

    def test_the_read_card_posted_twice(self):
        html = self.open(reverse("recipes:sales_list"))
        form = next(form for form in forms_of(html) if "sale-einvoice-form" in form.attrs.get("class", ""))
        data = as_post(form.submission())
        upload = as_bytes(SALE_CII)
        first = self.browser.post(form.action, {**data, "fichier": SimpleUploadedFile("FV-2026-0101.xml", upload)})
        second = self.browser.post(form.action, {**data, "fichier": SimpleUploadedFile("FV-2026-0101.xml", upload)})
        document = SaleDocument.objects.get()
        self.assertEqual(first["Location"], update_url(document))
        self.assertEqual(second["Location"], update_url(document))
        self.assertIn(ALREADY_SAVED_CREATE, self.said(second))


class AddRowWithoutScriptTests(PageMixin, TestCase):
    """« + Ajouter une ligne » is a submit button: without JavaScript the
    page comes back with one more row, nothing saved, nothing refused."""

    def test_one_more_row_nothing_saved_and_what_was_typed_kept(self):
        response = self.send(
            reverse(CREATE),
            press=("ajouter_ligne", "1"),
            header={"customer": "Mariage Exemple"},
            rows={0: {"label": "Location de salle", "quantity": "1"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SaleDocument.objects.exists())
        html = response.content.decode()
        form = self.document_form(html)
        self.assertEqual(form.control("lines-TOTAL_FORMS").value, "2")
        self.assertEqual(form.control("lines-0-label").value, "Location de salle")
        self.assertEqual(form.control("lines-1-label").value, "")
        self.assertEqual(form.control("customer").value, "Mariage Exemple")
        self.assertIn(LINE_ADDED, text_of(html))

    def test_no_error_is_drawn_and_the_file_is_asked_again(self):
        response = self.send(
            reverse(CREATE),
            press=("ajouter_ligne", "1"),
            header={"sold_on": ""},
            rows={0: {"label": "Location", "quantity": "deux"}},
            files={"source_file": SimpleUploadedFile("scan.pdf", b"%PDF-1.4 scan")},
        )
        html = response.content.decode()
        self.assertNotIn("field-error", html)
        self.assertNotIn("has-error", html)
        self.assertNotIn("Saisissez la date", html)
        self.assertIn(FILE_AGAIN, text_of(html))
        self.assertEqual(self.new_files(), set())


class EnterSavesTests(PageMixin, TestCase):
    """Enter in any field presses the form's FIRST submit button: a hidden
    « Enregistrer », never « + Ajouter une ligne »."""

    def test_the_first_button_is_a_hidden_enregistrer(self):
        for url in (reverse(CREATE), update_url(read(SALE_CII))):
            with self.subTest(url=url):
                form = self.document_form(self.open(url))
                first = form.buttons()[0]
                self.assertNotIn("name", first.attrs)
                self.assertIn("visually-hidden", first.attrs.get("class", ""))
                self.assertEqual(first.attrs.get("tabindex"), "-1")

    def test_the_hidden_button_goes_busy_like_the_visible_one(self):
        """A second Enter while the save runs presses the hidden button: it
        carries `data-default-submit`, which ui.js disables as it disables
        the visible « Enregistrer » (`data-busy-label`) - else two Enters
        made two documents."""
        for url in (reverse(CREATE), update_url(read(SALE_CII))):
            with self.subTest(url=url):
                form = self.document_form(self.open(url))
                first = form.buttons()[0]
                self.assertIn("data-default-submit", first.attrs)
                self.assertNotIn("data-busy-label", first.attrs)
                self.assertTrue(any("data-busy-label" in button.attrs for button in form.buttons()[1:]))


class UnsavedFlagTests(PageMixin, TestCase):
    """A page drawn in answer to a POST holds what was typed and not saved -
    a successful save always redirects -, so it says so (`data-unsaved` on
    the document's form) and static/js/sale_document.js asks before any
    form leaves it, from the start: « Rattacher » right after a refused save
    threw the typed values away in silence."""

    def test_a_page_opened_is_not_unsaved(self):
        for url in (reverse(CREATE), update_url(read(SALE_CII))):
            with self.subTest(url=url):
                self.assertNotIn("data-unsaved", self.document_form(self.open(url)).attrs)

    def test_a_refused_typed_save_is_drawn_unsaved(self):
        response = self.send(reverse(CREATE))
        self.assertRefusedWith(response, NOTHING_SOLD)
        self.assertIn("data-unsaved", self.document_form(response.content.decode()).attrs)

    def test_a_refused_electronic_invoice_save_is_drawn_unsaved(self):
        document = read(SALE_CII)
        first = document.lines.order_by("id").first()
        response = self.send(update_url(document), header={f"consomme-{first.pk}": "-3"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("data-unsaved", self.document_form(response.content.decode()).attrs)


class FileUploadTests(PageMixin, TestCase):
    """A plain file kept with its typed invoice (spec §4): stored as received
    under `ventes/`, never an electronic invoice - « Lire la facture » reads
    those."""

    def test_the_form_says_multipart(self):
        form = self.document_form(self.open(reverse(CREATE)))
        self.assertEqual(form.attrs.get("enctype"), "multipart/form-data")
        self.assertIn('type="file" name="source_file"', self.open(reverse(CREATE)))

    def test_a_plain_pdf_and_a_photo_are_stored(self):
        for name, data in (("scan.pdf", pdf_bytes()), ("photo.JPG", b"\xff\xd8\xff photo exemple")):
            with self.subTest(name=name):
                response = self.send(
                    reverse(CREATE),
                    header={"stated_total_ttc": "80"},
                    files={"source_file": SimpleUploadedFile(name, data)},
                )
                self.assertEqual(response.status_code, 302)
                document = SaleDocument.objects.latest("pk")
                self.assertTrue(document.source_file.name.startswith("ventes/"))
                self.assertTrue(document.source_file.name.endswith(name[name.rindex(".") :].lower()))
                self.assertEqual(document.source_file.open("rb").read(), data)
                document.source_file.close()

    def test_an_electronic_invoice_is_refused_here(self):
        response = self.send(
            reverse(CREATE),
            header={"stated_total_ttc": "230.52"},
            files={"source_file": SimpleUploadedFile("FV-2026-0101.xml", as_bytes(SALE_CII))},
        )
        self.assertRefusedWith(response, EINVOICE_ON_TYPED_FORM.format(name="FV-2026-0101.xml"))
        self.assertFalse(SaleDocument.objects.exists())
        self.assertEqual(self.new_files(), set())

    def test_an_xml_in_an_encoding_expat_cannot_use_is_refused_on_its_field(self):
        """« UCS2 » (unknown) or « utf_16 » (multi-byte): einvoice's refusal
        on the file's field, the page drawn again - never a 500."""
        for name in ("UCS2", "utf_16", "bogus"):
            with self.subTest(encoding=name):
                xml = SALE_CII.replace('encoding="UTF-8"', f'encoding="{name}"', 1)
                response = self.send(
                    reverse(CREATE),
                    header={"stated_total_ttc": "80"},
                    files={"source_file": SimpleUploadedFile("piece.xml", as_bytes(xml))},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors.get("source_file"))
                self.assertFalse(SaleDocument.objects.exists())
                self.assertEqual(self.new_files(), set())

    def test_any_other_refusal_asks_for_the_file_again(self):
        response = self.send(reverse(CREATE), files={"source_file": SimpleUploadedFile("scan.pdf", pdf_bytes())})
        self.assertRefusedWith(response, NOTHING_SOLD, FILE_AGAIN)
        self.assertEqual(self.new_files(), set())

    def test_retirer_le_fichier(self):
        self.send(
            reverse(CREATE),
            header={"stated_total_ttc": "80"},
            files={"source_file": SimpleUploadedFile("scan.pdf", pdf_bytes())},
        )
        document = SaleDocument.objects.get()
        name = document.source_file.name
        html = self.open(update_url(document))
        self.assertIn('name="retirer_fichier"', html)
        file_url = reverse("recipes:sale_document_file", args=[document.pk])
        self.assertIn(f'href="{file_url}"', html)
        self.assertIn(f'href="{file_url}?telecharger=1"', html)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.send(update_url(document), header={"retirer_fichier": "on"})
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        self.assertFalse(document.source_file)
        self.assertNotIn(name, stored_files())
        self.assertNotIn('name="retirer_fichier"', self.open(update_url(document)))


class EInvoicePageTests(PageMixin, TestCase):
    """An electronic invoice's page: its figures as stated, read-only; its
    lines tied in a grid named by line pk - never a formset, so no index can
    move a value onto another line."""

    def setUp(self):
        super().setUp()
        self.cocktail = make_recipe(name="Formule cocktail", selling_price_ttc="9.00")

    def lines(self, document):
        return list(document.lines.order_by("id"))

    def test_the_corrected_invoice_s_link_keeps_the_tab_s_parameters(self):
        """« Facture corrigée » is an address of the tab like any other: the
        period and the search ride along, as on every other link."""
        corrected = make_sale_document(reference="FV-900", stated_total_ttc="50.00")
        document = read(SALE_CII)
        SaleDocument.objects.filter(pk=document.pk).update(einvoice_preceding_number="FV-900")
        query = "du=2026-09-01&au=2026-09-30&facture=FV"
        html = self.open(update_url(document, query))
        target = reverse("recipes:sale_document_update", args=[corrected.pk])
        links = [link for link in re.findall(r'href="([^"]*)"', html) if link.split("?")[0] == target]
        self.assertEqual([unescape(link) for link in links], [f"{target}?{query}"])

    def test_its_figures_are_said_never_typed(self):
        document = read(SALE_CII)
        html = self.open(update_url(document))
        page = text_of(html)
        for words in ("Ce que dit la facture", SALE_NUMBER, CUSTOMER_NAME, "Bar des tests", "230.52 €", "194.20 €"):
            self.assertIn(words, page)
        form = self.document_form(html)
        for name in ("reference", "customer", "stated_total_ttc", "source_file", "lines-TOTAL_FORMS"):
            self.assertNotIn(name, form.names)
        self.assertEqual(
            sorted(set(form.names) - {"csrfmiddlewaretoken", "jeton"}),
            sorted(
                ["sold_on", "counting", "note"]
                + [f"{kind}-{line.pk}" for line in self.lines(document) for kind in ("lien", "consomme")]
            ),
        )
        self.assertIn("Le fichier est la facture : il ne se remplace pas.", page)

    def test_the_checks_fold_open_when_one_fails(self):
        passing = self.open(update_url(read(SALE_CII)))
        self.assertIn('<details class="sale-einvoice-checks">', passing)
        failing = self.open(update_url(read(SALE_CII_MIRROR_LINE, "FV-2026-0113.xml")))
        self.assertIn('<details class="sale-einvoice-checks" open>', failing)
        self.assertIn("en échec", text_of(failing))

    def test_a_proposal_accepted_as_drawn_is_saved(self):
        document = read(SALE_CII)
        first, second = self.lines(document)
        form = self.document_form(self.open(update_url(document)))
        self.assertEqual(form.control(f"lien-{first.pk}").value, f"recipe:{self.cocktail.pk}")
        self.assertEqual(form.control(f"lien-{second.pk}").value, "")
        response = self.browser.post(form.action, as_post(form.submission()))
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.recipe_id, self.cocktail.pk)

    def test_only_what_changed_is_saved_and_a_tie_not_posted_is_kept(self):
        document = read(SALE_CII)
        _first, second = self.lines(document)
        keg = make_stock_type(name="Planche exemple", unit=UnitChoices.UNIT)
        self.send(update_url(document), header={f"lien-{second.pk}": f"stock:{keg.pk}", f"consomme-{second.pk}": "1,5"})
        second.refresh_from_db()
        self.assertEqual((second.stock_type_id, second.consumed_quantity), (keg.pk, D("1.5")))
        # A post that does not mention the second line keeps its tie.
        response = self.send(update_url(document), drop=(f"lien-{second.pk}", f"consomme-{second.pk}"))
        self.assertEqual(response.status_code, 302)
        second.refresh_from_db()
        self.assertEqual((second.stock_type_id, second.consumed_quantity), (keg.pk, D("1.5")))

    def test_saved_it_comes_back_to_its_own_page_with_the_window(self):
        document = read(SALE_CII)
        response = self.send(update_url(document, WINDOW_QUERY))
        self.assertEqual(response["Location"], f"{update_url(document, WINDOW_QUERY)}#reglements")
        said = self.said(response)
        self.assertIn(f"{document} enregistrée.", said)
        self.assertIn("1 ligne reliée à une recette ou un article.", said)

    def test_a_rebuilt_line_and_a_mirror_line_are_drawn_without_a_select(self):
        for xml, name, refusal in (
            (SALE_CII_MINIMUM, "FV-2026-0103.xml", REBUILT_NOT_TIED),
            (SALE_CII_MIRROR_LINE, "FV-2026-0113.xml", MIRROR_NOT_TIED),
        ):
            with self.subTest(name=name):
                document = read(xml, name)
                blocked = [line for line in self.lines(document) if line.rebuilt or line.quantity < 0]
                self.assertTrue(blocked)
                form = self.document_form(self.open(update_url(document)))
                for line in blocked:
                    self.assertNotIn(f"lien-{line.pk}", form.names)
                    self.assertNotIn(f"consomme-{line.pk}", form.names)
                data = as_post(form.submission())
                data[f"lien-{blocked[0].pk}"] = [f"recipe:{self.cocktail.pk}"]
                response = self.browser.post(form.action, data)
                self.assertRefusedWith(response, refusal)
                self.assertFalse(SaleDocumentLine.objects.filter(pk=blocked[0].pk, recipe__isnull=False).exists())

    def test_zero_consumed_is_accepted_and_said(self):
        document = read(SALE_CII)
        first, _second = self.lines(document)
        self.send(update_url(document), header={f"consomme-{first.pk}": "0"})
        first.refresh_from_db()
        self.assertEqual(first.consumed_quantity, D("0"))
        self.assertIn(CONSUMED_ZERO, text_of(self.open(update_url(document))))

    def test_a_consumed_quantity_against_the_line_or_on_nothing_is_refused(self):
        document = read(SALE_CII)
        first, second = self.lines(document)
        response = self.send(update_url(document), header={f"consomme-{first.pk}": "-2"})
        self.assertRefusedWith(response, CONSUMED_SIGN_TIE)
        response = self.send(update_url(document), header={f"lien-{second.pk}": "", f"consomme-{second.pk}": "3"})
        self.assertRefusedWith(response, LINE_CONSUMED_UNTIED)

    def test_the_consumption_doubt_is_said(self):
        document = read(SALE_CII)
        first, _second = self.lines(document)
        # 169,00 € HT for 2 cocktails at 7,50 € HT: 84,50 € a serving, eleven times the menu.
        self.send(update_url(document), header={f"lien-{first.pk}": f"recipe:{self.cocktail.pk}"})
        self.assertIn(
            "Quantité consommée ? 84,50 € HT pour 1 portion (carte : 7,50 € HT).",
            text_of(self.open(update_url(document))),
        )

    def test_a_document_counting_in_nothing_says_its_ties_change_no_figure(self):
        for counting in (TILL, DEPOSIT):
            with self.subTest(counting=counting):
                document = read(SALE_CII, counting=counting)
                self.assertIn(NOT_COUNTED_LINES, text_of(self.open(update_url(document))))
                document.delete()
        self.assertNotIn(NOT_COUNTED_LINES, text_of(self.open(update_url(read(SALE_CII)))))

    def test_a_credit_note_says_what_tying_means_and_proposes_nothing(self):
        make_sale_document(reference=SALE_NUMBER, sold_on=date(2026, 9, 3))
        document = read(SALE_CII_CREDIT_NOTE, "AV-2026-0011.xml")
        html = self.open(update_url(document))
        self.assertIn(CREDIT_NOTE_LINES, text_of(html))
        form = self.document_form(html)
        self.assertEqual([form.control(f"lien-{line.pk}").value for line in self.lines(document)], ["", ""])

    def test_the_date_of_sale_is_editable_and_the_invoice_s_dates_are_said(self):
        document = read(SALE_CII_DELIVERED, "FV-2026-0107.xml")
        page = text_of(self.open(update_url(document)))
        self.assertIn("Date de la facture électronique : 03/09/2026 · livraison : 31/08/2026", page)
        response = self.send(update_url(document), header={"sold_on": "2026-09-01"})
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        self.assertEqual((document.sold_on, document.einvoice_issued_on), (date(2026, 9, 1), date(2026, 9, 3)))

    def test_the_counting_is_changed_on_the_page(self):
        document = read(SALE_CII)
        self.send(update_url(document), header={"counting": TILL})
        document.refresh_from_db()
        self.assertEqual(document.counting, TILL)


class ReadCard(PageMixin):
    """The « Ventes » tab's read card, opened and posted as a browser does."""

    def card_form(self, query=""):
        url = reverse("recipes:sales_list") + (f"?{query}" if query else "")
        html = self.open(url)
        found = [form for form in forms_of(html) if "sale-einvoice-form" in form.attrs.get("class", "").split()]
        self.assertEqual(len(found), 1)
        return found[0], html

    def post_card(self, xml, name="FV-2026-0101.xml", *, compte=None, date_typed=None, query=""):
        form, _html = self.card_form(query)
        data = as_post(form.submission())
        if compte is not None:
            data["compte"] = [compte]
        if date_typed is not None:
            data["date"] = [date_typed]
        return self.browser.post(form.action, {**data, "fichier": SimpleUploadedFile(name, as_bytes(xml))})

    def card_text(self, response) -> str:
        html = self.browser.get(response["Location"]).content.decode()
        start = html.index(f'id="{SALE_CARD}"')
        return text_of(html[start : html.index("</form>", start)])


class ReadCardTests(ReadCard, TestCase):
    """« Lire la facture », the « Ventes » tab's card: the file, how it
    counts - chosen BEFORE the document exists - and a date if the invoice
    gives none."""

    def test_the_card_as_drawn(self):
        form, html = self.card_form()
        self.assertEqual(form.attrs.get("enctype"), "multipart/form-data")
        self.assertEqual(form.action, reverse("recipes:sale_document_read"))
        self.assertIn('type="file" name="fichier" accept=".pdf,.xml" required', html)
        radios = [control for control in form.controls if control.name == "compte"]
        self.assertEqual([radio.attrs["value"] for radio in radios], [COUNTED, TILL, DEPOSIT])
        self.assertEqual([("checked" in radio.attrs) for radio in radios], [True, False, False])
        self.assertTrue(all("required" in radio.attrs for radio in radios))
        self.assertEqual(form.control("date").attrs.get("type"), "date")

    def test_a_till_invoice_read_counts_nothing(self):
        """Chosen on the card: neither Marges nor the stock count it - its
        lines tied or not - and the credit that paid it, linked by the pass
        all the same, reads as the till took it: never « Facture de vente »."""
        credit = make_credit("230.52", date(2026, 9, 10), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)
        response = self.post_card(SALE_CII, compte=TILL)
        document = SaleDocument.objects.get()
        self.assertEqual(response["Location"], update_url(document))
        self.assertEqual(document.counting, TILL)
        first = document.lines.order_by("id").first()
        cocktail = make_recipe(name="Mojito exemple", selling_price_ttc="9.00")
        self.send(update_url(document), header={f"lien-{first.pk}": f"recipe:{cocktail.pk}"})
        self.assertEqual(SaleDocumentLine.objects.get(pk=first.pk).recipe_id, cocktail.pk)
        self.assertEqual(sales_between(date(2026, 9, 1), date(2026, 9, 30)), {})
        from common import DateRange

        report = margins_for(DateRange(date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(report.revenue_documents.ttc, D("0"))
        self.assertEqual(report.documents_set_aside, 1)
        self.assertEqual(list(document.bank_payments.values_list("transaction_id", flat=True)), [credit.pk])
        received = income.income_for(DateRange(date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual([entry.line.pk for entry in received.others], [credit.pk])
        self.assertEqual(received.source_count(income.SALE), 0)

    def test_success_lands_on_the_document_with_the_window(self):
        response = self.post_card(SALE_CII, query=WINDOW_QUERY)
        document = SaleDocument.objects.get()
        self.assertEqual(response["Location"], update_url(document, WINDOW_QUERY))
        said = self.said(response)
        self.assertTrue(said[0].startswith(f"Facture électronique (CII) n° {SALE_NUMBER} du 03/09/2026 ajoutée"), said)

    def test_einvoice_s_refusals_are_said_in_the_card(self):
        for xml, words in (
            (SALE_CII_UTF16, "n'est pas encodée en UTF-8"),
            (SALE_CII_DOCTYPE, "déclare un DOCTYPE ou une entité XML"),
        ):
            with self.subTest(words=words):
                response = self.post_card(xml, query=WINDOW_QUERY)
                self.assertEqual(response["Location"], f"{reverse('recipes:sales_list')}?{WINDOW_QUERY}#{SALE_CARD}")
                self.assertIn(words, self.card_text(response))
        self.assertFalse(SaleDocument.objects.exists())

    def test_a_date_typed_that_is_no_date(self):
        response = self.post_card(SALE_CII_NO_DATE, date_typed="2026-02-30")
        self.assertIn(DATE_UNREADABLE, self.card_text(response))
        self.assertNotIn("date=", response["Location"])

    def test_a_readable_typed_date_comes_back_in_the_box(self):
        response = self.post_card(SALE_CII_NO_DATE, date_typed="1999-12-31")
        self.assertIn("date=1999-12-31", response["Location"])
        html = self.browser.get(response["Location"]).content.decode()
        form = next(form for form in forms_of(html) if "sale-einvoice-form" in form.attrs.get("class", ""))
        self.assertEqual(form.control("date").value, "1999-12-31")
        card = html[html.index(f'id="{SALE_CARD}"') :]
        self.assertIn("Date impossible", text_of(card[: card.index("</form>")]))

    def test_the_counting_chosen_comes_back_after_a_refusal(self):
        """« Déjà comptée par la caisse » chosen, the file refused: the card
        is drawn again with that choice still made - the next file chosen
        and sent without looking again must not count by default."""
        response = self.post_card(SALE_CII_UTF16, compte=TILL)
        self.assertIn(f"compte={TILL}", response["Location"])
        html = self.browser.get(response["Location"]).content.decode()
        form = next(form for form in forms_of(html) if "sale-einvoice-form" in form.attrs.get("class", ""))
        radios = {
            control.attrs["value"]: "checked" in control.attrs for control in form.controls if control.name == "compte"
        }
        self.assertEqual(radios, {COUNTED: False, TILL: True, DEPOSIT: False})
        self.assertNotIn("compte=", self.document_links_of(html))

    def document_links_of(self, html) -> str:
        """Every address the redrawn tab gives its other links: the choice is
        given back once, to the card, never carried on."""
        return " ".join(re.findall(r'href="([^"]*)"', html))

    def test_a_counting_nobody_offers_comes_back_as_the_default(self):
        response = self.post_card(SALE_CII_UTF16, compte="xyz")
        self.assertNotIn("compte=", response["Location"])
        html = self.browser.get(f"{reverse('recipes:sales_list')}?compte=xyz").content.decode()
        form = next(form for form in forms_of(html) if "sale-einvoice-form" in form.attrs.get("class", ""))
        radios = {
            control.attrs["value"]: "checked" in control.attrs for control in form.controls if control.name == "compte"
        }
        self.assertEqual(radios, {COUNTED: True, TILL: False, DEPOSIT: False})

    def test_a_get_goes_back_to_the_tab(self):
        response = self.client.get(f"{reverse('recipes:sale_document_read')}?{WINDOW_QUERY}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('recipes:sales_list')}?{WINDOW_QUERY}#{SALE_CARD}")


class LeavesLinesTests(PageMixin, TestCase):
    """Nothing typed is lost without a word: the forms beside the document's
    own carry `data-leaves-lines`, which static/js/sale_document.js asks
    before they leave the page."""

    def test_the_head_s_delete_asks_both_questions(self):
        document = make_sale_document(reference="FV-101", stated_total_ttc="10.00")
        html = self.open(update_url(document))
        delete = reverse("recipes:sale_document_delete", args=[document.pk])
        form = next(form for form in forms_of(html) if form.action.split("?")[0] == delete)
        self.assertEqual(form.attrs["data-leaves-lines"], LEAVE_WARNING)
        self.assertEqual(form.attrs["data-confirm"], DELETE_QUESTION.format(label=document.label))
        self.assertIn('src="/static/js/sale_document.js?v=', html)

    def test_every_form_of_reglement_asks_too(self):
        """« Délier », « Rattacher » and « Chercher une entrée » leave the
        page: each asks before what was typed is lost."""
        document = make_sale_document(reference="FV-111", customer=CUSTOMER_NAME, stated_total_ttc="400.00")
        make_sale_payment(document, make_credit("100.00", date(2026, 3, 9), counterparty=CUSTOMER_PAYER))
        make_credit("300.00", date(2026, 3, 12), counterparty=CUSTOMER_PAYER)
        html = self.open(update_url(document))
        section = html[html.index('id="reglements"') :]
        found = forms_of(section[: section.index("</section>")])
        self.assertEqual(
            sorted(next(c.value for c in form.controls if c.name in ("action", "entree")) for form in found),
            ["", "sale_link", "sale_unlink_document"],
        )
        for form in found:
            with self.subTest(form=form.action):
                self.assertEqual(form.attrs.get("data-leaves-lines"), LEAVE_WARNING)

    def test_a_reader_without_script_is_told_to_save_first(self):
        document = make_sale_document(reference="FV-112", stated_total_ttc="10.00")
        html = self.open(update_url(document))
        sentence = f'<p class="muted small no-js-only">{escape(SAVE_BEFORE_PAYMENTS)}</p>'
        self.assertIn(sentence, html)
        self.assertLess(html.index(sentence), html.index('id="reglements"'))
        self.assertNotIn("no-js-only", self.open(reverse(CREATE)))


class StaleTests(PageMixin, TestCase):
    """A document gone - a double tap, another tab - is said, never a 404
    (CLAUDE.md « … n'existe plus. »)."""

    def test_a_save_of_a_document_deleted_meanwhile(self):
        document = make_sale_document(reference="FV-102", stated_total_ttc="10.00")
        form = self.document_form(self.open(update_url(document, WINDOW_QUERY)))
        document.delete()
        response = self.browser.post(form.action, as_post(form.submission()))
        self.assertEqual(response["Location"], f"{reverse('recipes:sales_list')}?{WINDOW_QUERY}#{SALE_CARD}")
        self.assertEqual(self.said(response), [DOCUMENT_GONE])

    def test_a_save_whose_document_goes_while_it_runs(self):
        """Deleted in another tab after the page loaded it, before the save
        wrote it: said the same way, nothing written back."""
        document = make_sale_document(reference="FV-104", stated_total_ttc="10.00")
        form = self.document_form(self.open(update_url(document, WINDOW_QUERY)))
        real_save_typed = sale_files.save_typed

        def deleted_meanwhile(*args, **kwargs):
            SaleDocument.objects.filter(pk=document.pk).delete()
            return real_save_typed(*args, **kwargs)

        with mock.patch("recipes.views.save_typed", side_effect=deleted_meanwhile):
            response = self.browser.post(form.action, as_post(form.submission()))
        self.assertEqual(response["Location"], f"{reverse('recipes:sales_list')}?{WINDOW_QUERY}#{SALE_CARD}")
        self.assertEqual(self.said(response), [DOCUMENT_GONE])
        self.assertFalse(SaleDocument.objects.filter(pk=document.pk).exists())

    def test_deleting_twice_says_it_is_gone(self):
        document = make_sale_document(reference="FV-103")
        url = reverse("recipes:sale_document_delete", args=[document.pk])
        self.assertEqual(self.client.post(url).status_code, 302)
        # The first « supprimée » read, as the page it lands on reads it.
        self.client.cookies.pop("messages", None)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual([str(message) for message in messages.get_messages(response.wsgi_request)], [DOCUMENT_GONE])

    def test_a_page_never_drawn_is_a_404(self):
        self.assertEqual(self.client.get(reverse("recipes:sale_document_update", args=[987654])).status_code, 404)


class TemplatesTests(TestCase):
    """No inline code (spec §5.8, §9.5): the policy's own patterns
    (tests/test_security_headers.py) over the sales templates."""

    TEMPLATES = (
        "recipes/templates/recipes/_tab_sales.html",
        "recipes/templates/recipes/_sale_documents_card.html",
        "recipes/templates/recipes/sale_document_form.html",
        "recipes/templates/recipes/_sale_line_row.html",
        "recipes/templates/recipes/_sale_payments.html",
        "bank/templates/bank/_sale_links.html",
        "bank/templates/bank/_found_sale_documents.html",
    )

    def test_the_sales_templates_hold_no_inline_code(self):
        from django.conf import settings

        for relative in self.TEMPLATES:
            source = (settings.BASE_DIR / relative).read_text(encoding="utf-8")
            for pattern in (INLINE_SCRIPT, EVENT_HANDLER, INLINE_STYLE):
                with self.subTest(template=relative, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(source))


COUNTING_FIELDSET = re.compile(r'<fieldset class="sale-counting[^"]*">(.*?)</fieldset>', flags=re.DOTALL)


class CountingChoiceTests(PageMixin, TestCase):
    """« Compte » is one group of three choices, drawn as one - a fieldset
    and its legend, as on the read card and the house's supplier_create.html.
    Through _form_fields.html it was a label naming no field (`for=""`) over
    three radios and their sentences squeezed into one column of the grid."""

    def group_of(self, html) -> str:
        """What the page's « Compte » fieldset holds."""
        fieldset = COUNTING_FIELDSET.search(html)
        self.assertIsNotNone(fieldset)
        return fieldset.group(1) if fieldset else ""

    def assertDrawnAsAGroup(self, html):
        group = self.group_of(html)
        self.assertRegex(group, r"<legend[^>]*>\s*Compte\s*</legend>")
        radios = re.findall(r'<input type="radio" name="counting"[^>]*id="(id_counting_\d)"', group)
        self.assertEqual(radios, ["id_counting_0", "id_counting_1", "id_counting_2"])
        for radio in radios:
            self.assertIn(f'for="{radio}"', group)
        self.assertNotIn('for=""', html)

    def test_on_every_page_of_a_document(self):
        typed = make_sale_document(reference="FV-201")
        for url in (reverse(CREATE), update_url(typed), update_url(read(SALE_CII))):
            with self.subTest(url=url):
                self.assertDrawnAsAGroup(self.open(url))

    def test_a_choice_nobody_offered_is_refused_in_the_group(self):
        response = self.send(reverse(CREATE), header={"counting": "gratuit"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(UNKNOWN_CHOICE, unescape(self.group_of(response.content.decode())))
        self.assertFalse(SaleDocument.objects.exists())


class MessagesPlacementTests(PageMixin, TestCase):
    """The card's messages are said in the card on « Ventes » (where its
    redirects land), and at the top on the tabs that draw no card."""

    def test_in_the_card_on_ventes_at_the_top_on_recettes(self):
        document = make_sale_document(reference="FV-104")
        delete = reverse("recipes:sale_document_delete", args=[document.pk])
        self.client.post(delete)
        html = self.client.get(reverse("recipes:sales_list")).content.decode()
        sentence = "Vente du 05/03/2026 (n° FV-104) supprimée."
        card = html.index(f'id="{SALE_CARD}"')
        self.assertGreater(html.index(sentence), card)
        self.assertEqual(html.count(sentence), 1)

        document = make_sale_document(reference="FV-105")
        self.client.post(reverse("recipes:sale_document_delete", args=[document.pk]))
        html = self.client.get(reverse("recipes:recipe_list")).content.decode()
        sentence = "Vente du 05/03/2026 (n° FV-105) supprimée."
        self.assertLess(html.index(sentence), html.index('id="workspace"'))

    def test_a_message_of_no_place_stays_at_the_top_of_ventes(self):
        recipe = make_recipe(name="Mule exemple")
        self.client.post(reverse("recipes:sales_list"), {"recipe": recipe.pk, "sold_on": "2026-03-05", "quantity": "3"})
        html = self.client.get(reverse("recipes:sales_list")).content.decode()
        self.assertLess(html.index("3 × Mule exemple le 05/03/2026."), html.index(f'id="{SALE_CARD}"'))


class DoubtsTests(PageMixin, TestCase):
    """The two doubts a deposit invoice raises (spec §2.5), said on the
    documents' pages, never repaired: a deposit counted that its final
    invoice deducts too - the same sale twice -, and a « Acompte » with no
    final invoice months on."""

    def test_a_deposit_counted_and_deducted_is_said_on_both_pages(self):
        deposit = make_sale_document(
            reference="AC-7", sold_on=date(2026, 2, 10), customer="Mariage Exemple", stated_total_ttc="500.00"
        )
        final = make_sale_document(
            reference="FV-8",
            sold_on=date(2026, 3, 20),
            customer="MARIAGE  exemple",
            stated_total_ttc="2000.00",
            prepaid_ttc="500.00",
        )
        sentence = COUNTED_TWICE.format(deposit="n° AC-7 du 10/02/2026", final="n° FV-8 du 20/03/2026")
        for document in (deposit, final):
            with self.subTest(reference=document.reference):
                self.assertIn(sentence, text_of(self.open(update_url(document))))
        SaleDocument.objects.filter(pk=deposit.pk).update(counting=DEPOSIT)
        for document in (deposit, final):
            with self.subTest(reference=document.reference, marked=True):
                self.assertNotIn("compte deux fois", text_of(self.open(update_url(document))))

    def test_a_deposit_with_no_final_invoice_after_90_days(self):
        today = timezone.localdate()
        old = make_sale_document(
            reference="AC-8",
            sold_on=today - timedelta(days=DEPOSIT_WITHOUT_FINAL_DAYS + 1),
            customer="Exemple Événements SARL",
            counting=DEPOSIT,
            stated_total_ttc="300.00",
        )
        recent = make_sale_document(
            reference="AC-9",
            sold_on=today - timedelta(days=DEPOSIT_WITHOUT_FINAL_DAYS - 1),
            customer="Exemple Événements SARL",
            counting=DEPOSIT,
            stated_total_ttc="300.00",
        )
        self.assertIn(NO_FINAL_INVOICE, text_of(self.open(update_url(old))))
        self.assertNotIn(NO_FINAL_INVOICE, text_of(self.open(update_url(recent))))
        self.assertEqual(deposit_doubts([old, recent]), {old.pk: NO_FINAL_INVOICE})
        # Its final invoice, later, stating what was already paid: nothing to say.
        make_sale_document(
            reference="FV-10",
            sold_on=today - timedelta(days=10),
            customer="EXEMPLE EVENEMENTS SARL",
            stated_total_ttc="1000.00",
            prepaid_ttc="300.00",
        )
        self.assertEqual(deposit_doubts([old]), {})
        self.assertNotIn(NO_FINAL_INVOICE, text_of(self.open(update_url(old))))

    def test_a_deposit_naming_no_customer_has_no_final_invoice_to_find(self):
        old = make_sale_document(
            reference="AC-11", sold_on=date(2025, 1, 10), counting=DEPOSIT, stated_total_ttc="100.00"
        )
        make_sale_document(
            reference="FV-12", sold_on=date(2025, 3, 10), stated_total_ttc="400.00", prepaid_ttc="100.00"
        )
        self.assertEqual(deposit_doubts([old]), {old.pk: NO_FINAL_INVOICE})

    def test_one_query_and_only_for_an_old_deposit(self):
        documents = [make_sale_document(reference=f"FV-{number}") for number in range(3)]
        with self.assertNumQueries(0):
            deposit_doubts(documents)
        documents.append(make_sale_document(reference="AC-12", sold_on=date(2025, 1, 10), counting=DEPOSIT))
        with self.assertNumQueries(1):
            deposit_doubts(documents)


class TabTests(PageMixin, TestCase):
    """The card's list: what each row says of its document."""

    def test_each_kind_of_document_is_said(self):
        today = timezone.localdate()
        old_deposit = make_sale_document(
            reference="AC-1", customer="Mariage Exemple", counting=DEPOSIT, sold_on=today - timedelta(days=120)
        )
        make_sale_line(old_deposit, label="Acompte", unit_price_ttc="300")
        read(SALE_CII, counting=TILL)
        credit = make_sale_document(reference="AV-1", stated_total_ttc="-20.00")
        html = self.open(reverse("recipes:sales_list"))
        card = text_of(html[html.index(f'id="{SALE_CARD}"') :])
        for words in (
            "Facture électronique",
            "Déjà comptée par la caisse",
            "Acompte",
            "Sans facture finale",
            "Avoir",
            "Fichier",
        ):
            self.assertIn(words, card)
        self.assertIn(
            reverse("recipes:sale_document_file", args=[SaleDocument.objects.get(reference=SALE_NUMBER).pk]), html
        )
        self.assertIn(update_url(credit), html)

    def test_three_lines_then_how_many_more(self):
        document = make_sale_document(reference="FV-106")
        for number in range(5):
            make_sale_line(document, label=f"Ligne exemple {number}", unit_price_ttc="1")
        card = text_of(self.open(reverse("recipes:sales_list")))
        self.assertIn("Ligne exemple 2", card)
        self.assertNotIn("Ligne exemple 3", card)
        self.assertIn("… et 2 autres", card)

    def test_the_same_number_of_queries_for_2_and_20_documents(self):
        def count():
            from django.db import connection
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as queries:
                self.client.get(reverse("recipes:sales_list"))
            return len(queries)

        recipe = make_recipe(name="Mule exemple")
        for number in range(2):
            make_sale_line(make_sale_document(reference=f"A-{number}"), recipe=recipe, quantity="1")
        two = count()
        for number in range(18):
            make_sale_line(make_sale_document(reference=f"B-{number}"), recipe=recipe, quantity="1")
        self.assertEqual(count(), two)

    def test_a_document_listed_runs_no_query_of_its_own(self):
        with mock.patch("recipes.models.SaleDocument.total_ttc", new_callable=mock.PropertyMock) as total:
            make_sale_document(reference="FV-107")
            self.open(reverse("recipes:sales_list"))
        total.assert_not_called()

    def test_what_each_invoice_received(self):
        """The « Règlement » column: the pill and the amounts the allocation
        gives THAT invoice - never a bank date, a payer or another invoice's
        money."""
        paid = make_sale_document(reference="FV-120", sold_on=date(2026, 3, 1), stated_total_ttc="300.00")
        part = make_sale_document(reference="FV-121", sold_on=date(2026, 3, 2), stated_total_ttc="500.00")
        make_sale_document(reference="FV-122", stated_total_ttc="80.00")
        make_sale_document(reference="AV-123", stated_total_ttc="-20.00")
        deposit = make_credit("400.00", date(2026, 3, 20), counterparty="BANQUE EXEMPLE REMISE")
        for document in (paid, part):
            make_sale_payment(document, deposit)
        html = self.open(reverse("recipes:sales_list"))
        cells = [
            text_of(cell)
            for cell in re.findall(r'<td[^>]*data-label="Règlement"[^>]*>(.*?)</td>', html, flags=re.DOTALL)
        ]
        # The most recent first: the two of 05/03/2026, then the 2nd, then the 1st.
        self.assertEqual(
            sorted(cells),
            sorted([PILL_CREDIT_NOTE, PILL_UNPAID, f"{PILL_PART_PAID} 100.00 € reçus", "Réglée 300.00 € reçus"]),
        )
        self.assertNotIn("BANQUE EXEMPLE", html)
        self.assertNotIn("20/03/2026", html)


class PaymentSectionTests(PageMixin, TestCase):
    """« Règlement » (`id="reglements"`, recipes/_sale_payments.html): what the
    bank has paid of the invoice - and, with « Banque », the credits, their
    shares, what could pay it, and a search. Its forms post to Banque's own
    action, the gate of « Banque »."""

    def section(self, html) -> str:
        start = html.index('id="reglements"')
        return html[start : html.index("</section>", start)]

    def test_the_state_comes_from_the_allocation(self):
        document = make_sale_document(reference="FV-301", customer=CUSTOMER_NAME, stated_total_ttc="300.00")
        self.assertIn(UNPAID_SAID.format(due="300.00"), text_of(self.section(self.open(update_url(document)))))
        cheques = make_credit("1200.00", date(2026, 3, 10), label="REMISE CHEQUES EXEMPLE", bank_type="REMISE CHEQUE")
        make_sale_payment(document, cheques)
        self.assertIn(PAID_SAID.format(paid="300.00"), text_of(self.section(self.open(update_url(document)))))

    def test_with_banque_each_credit_and_its_share(self):
        first = make_sale_document(
            reference="FV-302", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 1), stated_total_ttc="300.00"
        )
        second = make_sale_document(reference="FV-303", sold_on=date(2026, 3, 4), stated_total_ttc="600.00")
        credit = make_credit("500.00", date(2026, 3, 10), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)
        make_sale_payment(first, credit)
        make_sale_payment(second, credit)
        section = self.section(self.open(update_url(second, WINDOW_QUERY)))
        said = text_of(section)
        self.assertIn(PART_PAID_SAID.format(paid="200.00", to_pay="600.00"), said)
        self.assertIn("200.00 € sur une entrée de 500.00 €", said)
        self.assertIn("aussi pour la facture de vente n° FV-302", said)
        self.assertIn(CUSTOMER_PAYER, said)
        self.assertIn("À la main", said)
        (form,) = [form for form in forms_of(section) if form.method == "post"]
        self.assertEqual(form.action, reverse("bank:bank_line_action", args=[credit.pk]))
        self.assertEqual(
            {name: form.control(name).value for name in ("action", "document", "next", "lieu")},
            {
                "action": "sale_unlink_document",
                "document": str(second.pk),
                "next": f"{update_url(second, WINDOW_QUERY)}#reglements",
                "lieu": "reglements",
            },
        )
        response = self.browser.post(form.action, as_post(form.submission()))
        self.assertEqual(response["Location"], f"{update_url(second, WINDOW_QUERY)}#reglements")
        self.assertEqual(list(second.bank_payments.all()), [])

    def test_what_could_pay_it_with_one_form_each(self):
        first = make_sale_document(
            reference="FV-304", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 1), stated_total_ttc="100.00"
        )
        second = make_sale_document(
            reference="FV-305", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 3), stated_total_ttc="150.00"
        )
        self.assertIn(NO_CREDIT_PROPOSED, text_of(self.section(self.open(update_url(first)))))
        credit = make_credit("250.00", date(2026, 3, 9), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)
        section = self.section(self.open(update_url(first)))
        self.assertIn(f"Entrée du 09/03/2026 · {CUSTOMER_PAYER} · 250.00 €", text_of(section))
        self.assertIn('class="status-pill status-tier-sure"', section)
        (form,) = [form for form in forms_of(section) if form.method == "post"]
        self.assertEqual(form.action, reverse("bank:bank_line_action", args=[credit.pk]))
        # A sum of two invoices posts both: linked alone, the second would be left out in silence.
        self.assertEqual(
            [control.value for control in form.controls if control.name == "document"], [str(first.pk), str(second.pk)]
        )
        response = self.browser.post(form.action, as_post(form.submission()), follow=True)
        self.assertEqual(
            set(SaleDocument.objects.filter(bank_payments__transaction=credit).values_list("pk", flat=True)),
            {first.pk, second.pk},
        )
        # Said in « Règlement », where the redirect lands - not at the top.
        html = response.content.decode()
        sentence = "Entrée rattachée aux factures de vente"
        self.assertIn(sentence, text_of(self.section(html)))
        self.assertEqual(html.count(sentence), 1)

    def test_a_sum_names_every_invoice_it_posts(self):
        """The reason of a sum names no invoice: the form says which it links,
        each with what it still asks, as Banque's own suggestion does."""
        first = make_sale_document(
            reference="FV-311", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 1), stated_total_ttc="200.00"
        )
        second = make_sale_document(
            reference="FV-312", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 3), stated_total_ttc="300.00"
        )
        make_credit("500.00", date(2026, 3, 9), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)
        section = self.section(self.open(update_url(first)))
        (form,) = [form for form in forms_of(section) if form.method == "post"]
        self.assertEqual(
            [control.value for control in form.controls if control.name == "document"], [str(first.pk), str(second.pk)]
        )
        self.assertIn(f"{first.label} · 200.00 € + {second.label} · 300.00 €", text_of(section))

    def test_two_sums_holding_it_are_two_forms_each_naming_its_other_invoice(self):
        """Two sums of one customer's invoices give the amount, both holding
        this one: each is offered, never the first alone in silence."""
        mine = make_sale_document(
            reference="FV-313", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 1), stated_total_ttc="100.00"
        )
        one = make_sale_document(
            reference="FV-314", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 2), stated_total_ttc="150.00"
        )
        other = make_sale_document(
            reference="FV-315", customer=CUSTOMER_NAME, sold_on=date(2026, 3, 3), stated_total_ttc="150.00"
        )
        make_credit("250.00", date(2026, 3, 9), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)
        section = self.section(self.open(update_url(mine)))
        posted = [
            [control.value for control in form.controls if control.name == "document"]
            for form in forms_of(section)
            if form.method == "post"
        ]
        self.assertEqual(sorted(posted), sorted([[str(mine.pk), str(one.pk)], [str(mine.pk), str(other.pk)]]))
        said = text_of(section)
        self.assertIn(f"{mine.label} · 100.00 € + {one.label} · 150.00 €", said)
        self.assertIn(f"{mine.label} · 100.00 € + {other.label} · 150.00 €", said)

    def test_the_credit_search(self):
        document = make_sale_document(reference="FV-306", customer=CUSTOMER_NAME, stated_total_ttc="90.00")
        other = make_sale_document(reference="FV-307", stated_total_ttc="40.00")
        here = make_credit("90.00", date(2026, 3, 6), counterparty="PAYEUR EXEMPLE")
        elsewhere = make_credit("40.00", date(2026, 3, 7), counterparty="PAYEUR EXEMPLE")
        chosen = make_credit("15.00", date(2026, 3, 8), counterparty="PAYEUR EXEMPLE", income_source="credit")
        make_sale_payment(document, here)
        make_sale_payment(other, elsewhere)
        section = self.section(self.open(update_url(document)))
        (search,) = [form for form in forms_of(section) if form.method == "get"]
        self.assertEqual(search.action, f"{reverse('recipes:sale_document_update', args=[document.pk])}#reglements")
        response = self.browser.get(
            search.action.split("#")[0], as_post(search.submission(values={"entree": "payeur"}))
        )
        section = self.section(response.content.decode())
        said = text_of(section)
        self.assertIn("déjà rattachée ici", said)
        self.assertIn("rattachée aussi à la facture de vente n° FV-307", said)
        self.assertIn("choisie « Avoir » en caisse", said)
        # « Rattacher » on the two found elsewhere; « Délier » on the one here.
        linking = {
            form.action
            for form in forms_of(section)
            if form.method == "post" and form.control("action").value == "sale_link"
        }
        self.assertEqual(linking, {reverse("bank:bank_line_action", args=[line.pk]) for line in (elsewhere, chosen)})

    def test_without_banque_the_state_only(self):
        document = make_sale_document(reference="FV-308", customer=CUSTOMER_NAME, stated_total_ttc="300.00")
        credit = make_credit("120.00", date(2026, 3, 10), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)
        make_sale_payment(document, credit)
        make_credit("180.00", date(2026, 3, 12), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)
        self.browser.force_login(employee_of_the_test_tenant("ventes@example.invalid", ["recipes"]))
        response = self.browser.get(f"{update_url(document)}?entree={CUSTOMER_PAYER}")
        self.assertEqual(response.status_code, 200)
        section = self.section(response.content.decode())
        self.assertIn(PART_PAID_SAID.format(paid="120.00", to_pay="300.00"), text_of(section))
        self.assertEqual(forms_of(section), [])
        for hidden in (CUSTOMER_PAYER, "10/03/2026", "12/03/2026", reverse("bank:bank_line_action", args=[credit.pk])):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, section)


class AccessMessagesTests(ReadCard, TestCase):
    """What « Lire la facture » says of the automatic bank pass, worded from
    who reads it: « Recettes & ventes » alone sees no bank date, amount or
    payer."""

    def setUp(self):
        super().setUp()
        self.credit = make_credit("230.52", date(2026, 9, 10), counterparty=CUSTOMER_PAYER, label=CUSTOMER_TRANSFER)

    def test_with_banque(self):
        said = self.said(self.post_card(SALE_CII))
        self.assertIn(LINKED_TO_CREDIT.format(day="10/09/2026", amount="230.52"), said)

    def test_without_banque(self):
        self.browser.force_login(employee_of_the_test_tenant("ventes@example.invalid", ["recipes"]))
        said = self.said(self.post_card(SALE_CII))
        self.assertIn(LINKED_WITHOUT_DETAIL, said)
        for message in said:
            with self.subTest(message=message):
                self.assertNotIn("230.52", message)
                self.assertNotIn("10/09/2026", message)
        self.assertEqual(SaleDocument.objects.get().bank_payments.count(), 1)

    def test_a_pass_that_fails_is_said_and_the_invoice_kept(self):
        from django.db import OperationalError

        with (
            mock.patch("bank.sale_reconcile.reconcile_sales", side_effect=OperationalError("database is locked")),
            self.assertLogs("recipes.sale_files", "ERROR"),
        ):
            response = self.post_card(SALE_CII)
        self.assertIn(BANK_PASS_FAILED, self.said(response))
        self.assertTrue(SaleDocument.objects.exists())
