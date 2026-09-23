"""La marge réelle, sans ce qu'on décoche - in arithmetic.

The owner asked to see the real margin « sans le Matériel », « sans les
charges », **and the global one beside it**. So the report carries both, and
the second is the first with some of the invoiced money set aside:

* **every invoiced euro lands in exactly one place** - a charge's whole
  document on its supplier, a goods line on its article (and so on the
  article's category), a goods line with no article on « à classer », and a
  document with no line at all on its supplier (a charge) or on « à
  classer » (goods);
* **the duty an invoice adds to its lines** (`reconciliation_adjustment`) is
  spread over those lines pro rata to their HT, and a receipt whose total is
  its printed one rather than the sum of its lines keeps the difference on
  its largest place - so that, **with nothing left out, the places add up to
  the global spending to the cent, HT and TTC**. That is the rule every other
  test here leans on;
* **the revenue never moves**: leaving a purchase out takes a cost away, not
  a sale - nothing ties a bag of cement to a till category;
* a key the page does not know - garbled, stale, tampered - is ignored,
  never an error.

Data invented throughout - no real supplier, article or amount.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from common import DateRange
from margins.computation import (
    CHARGES_KEY,
    NO_CATEGORY,
    TO_CLASSIFY_KEY,
    Money,
    article_key,
    category_key,
    margins_for,
    supplier_key,
)
from recipes.models import PosProduct, PosProductDailyQuantity
from tests.factories import make_invoice, make_invoice_line, make_product, make_stock_type, make_supplier

MARCH = DateRange(date(2026, 3, 1), date(2026, 3, 31))


def money(ht, ttc) -> Money:
    return Money(Decimal(ht), Decimal(ttc))


def line(invoice, article, total_ht, vat_rate="0.20", **kwargs):
    """One invoice line on `article` - None is a product no article claims
    yet, which is what « à classer » means."""
    product = make_product(supplier=invoice.supplier, stock_type=article)
    return make_invoice_line(
        invoice=invoice, product=product, total_ht=total_ht, vat_rate=Decimal(vat_rate), **kwargs
    )


def till_takes(day, ttc, ht):
    product = PosProduct.objects.create(name="Pinte du comptoir", category="Bières")
    PosProductDailyQuantity.objects.create(
        product=product, sold_on=day, quantity=20, revenue_ttc=Decimal(ttc), revenue_ht=Decimal(ht), revenue_read=True
    )


class SpendFixture:
    """One March, every shape an invoiced euro can take.

    ====  =======================  ==========================================
    A     goods, three articles     Perceuse 100 · Rhum 50 · Sirop 30 (5,5 %)
    B     goods, duty 7,31 € on top Rhum 60 (droits) · Nappe 40 · à classer 20
    C     a receipt, printed 18,35  Nappe 12,00 TTC · Sirop 6,33 TTC, HT +0,01
    D     goods, NO line            15,00 € of adjustment alone
    E     charge                    Bailleur 500
    F     charge                    Assurance 29,99 (35,988 TTC)
    G     charge, NO line           Assurance 45,00 of adjustment alone
    H     April - outside           Fût consigné 80, Perceuse 999
    ====  =======================  ==========================================

    By hand, B's duty lands 3,655 / 2,4367 / 1,2183 € HT on its three lines:
    rounded one by one that is 7,32 € for 7,31 € of duty, and the missing
    centime comes off the largest place (the rum). Its TTC, 8,772 €, lands
    4,39 / 2,92 / 1,46 - exact. C's printed total is two centimes above its
    printed lines, and those land on its largest place (the tablecloth).

    ==========================  ===========  ===========
    place                       HT           TTC
    ==========================  ===========  ===========
    Charges                     574,99       680,99
      Bailleur Exemple          500,00       600,00
      Assurance Exemple          74,99        80,99
    Matériel                    152,45       182,94
      Perceuse sans fil         100,00       120,00
      Nappe en lin               52,45        62,94
    Spiritueux  (Rhum ambré)    113,65       136,39
    (blank)     (Sirop)          36,00        37,98
    à classer                    36,22        40,46
    ==========================  ===========  ===========
    total                       913,31     1 078,76
    """

    @classmethod
    def setUpTestData(cls):
        goods = make_supplier(name="Grossiste Exemple")
        shop = make_supplier(name="Épicerie du coin")
        cls.landlord = make_supplier(name="Bailleur Exemple", expenses_only=True)
        cls.insurer = make_supplier(name="Assurance Exemple", expenses_only=True)

        cls.drill = make_stock_type(name="Perceuse sans fil", category="Matériel")
        cls.cloth = make_stock_type(name="Nappe en lin", category="Matériel")
        cls.rum = make_stock_type(name="Rhum ambré", category="Spiritueux")
        cls.syrup = make_stock_type(name="Sirop d'églantier", category="")
        cls.keg = make_stock_type(name="Fût consigné", category="Consignes")

        a = make_invoice(supplier=goods, invoice_date=date(2026, 3, 3))
        line(a, cls.drill, "100.00")
        line(a, cls.rum, "50.00")
        line(a, cls.syrup, "30.00", vat_rate="0.055")

        b = make_invoice(supplier=goods, invoice_date=date(2026, 3, 5), reconciliation_adjustment=Decimal("7.31"))
        line(b, cls.rum, "60.00", taxes=Decimal("5.00"))
        line(b, cls.cloth, "40.00")
        line(b, None, "20.00")

        c = make_invoice(
            supplier=shop,
            invoice_date=date(2026, 3, 8),
            reconciliation_adjustment=Decimal("0.01"),
            printed_total_ttc=Decimal("18.35"),
        )
        line(c, cls.cloth, "10.00", printed_ttc=Decimal("12.00"))
        line(c, cls.syrup, "6.00", vat_rate="0.055", printed_ttc=Decimal("6.33"))

        make_invoice(supplier=goods, invoice_date=date(2026, 3, 10), reconciliation_adjustment=Decimal("15.00"))

        line(make_invoice(supplier=cls.landlord, invoice_date=date(2026, 3, 1)), None, "500.00")
        line(make_invoice(supplier=cls.insurer, invoice_date=date(2026, 3, 15)), None, "29.99")
        make_invoice(supplier=cls.insurer, invoice_date=date(2026, 3, 20), reconciliation_adjustment=Decimal("45.00"))

        h = make_invoice(supplier=goods, invoice_date=date(2026, 4, 2))
        line(h, cls.keg, "80.00")
        line(h, cls.drill, "999.00")

        till_takes(date(2026, 3, 12), ttc="1200.00", ht="1000.00")

    def report(self, *left_out):
        return margins_for(MARCH, left_out)

    def group(self, report, key):
        return next(group for group in report.spend_groups if group.key == key)

    def member(self, report, key):
        return next(part for group in report.spend_groups for part in group.members if part.key == key)


class EveryEuroLandsOnceTests(SpendFixture, TestCase):
    def test_the_fixture_is_what_its_docstring_says(self):
        report = self.report()
        self.assertEqual(report.spend_goods, money("338.32", "397.77"))
        self.assertEqual(report.spend_charges, money("574.99", "680.99"))
        self.assertEqual(report.spend, money("913.31", "1078.76"))

    def test_with_nothing_left_out_the_places_add_up_to_the_global_spending_to_the_cent(self):
        """The rule the whole feature stands on: a receipt paid at its
        printed total and an invoice carrying duty are both in this window,
        and neither loses nor gains a centime by being split."""
        report = self.report()
        total = sum((group.money for group in report.spend_groups), start=Money())

        self.assertEqual(total, report.spend)
        for group in report.spend_groups:
            if group.members:
                with self.subTest(group=group.name):
                    self.assertEqual(sum((part.money for part in group.members), start=Money()), group.money)

    def test_with_nothing_left_out_the_second_margin_is_the_global_one(self):
        report = self.report()

        self.assertEqual(report.exclusions, [])
        self.assertEqual(report.spend_left_out, Money())
        self.assertEqual(report.spend_kept, report.spend)
        self.assertEqual(report.kept_margin_ht, report.real_margin_ht)
        self.assertEqual(report.kept_margin_ttc, report.real_margin_ttc)
        self.assertEqual(report.real_margin_ht, Decimal("86.69"))
        self.assertEqual(report.real_margin_ttc, Decimal("121.24"))

    def test_the_charges_are_their_suppliers(self):
        charges = self.group(self.report(), CHARGES_KEY)

        self.assertEqual(charges.name, "Charges")
        self.assertEqual(charges.money, money("574.99", "680.99"))
        self.assertEqual(
            [(part.name, part.money) for part in charges.members],
            [("Bailleur Exemple", money("500.00", "600.00")), ("Assurance Exemple", money("74.99", "80.99"))],
        )

    def test_a_category_is_its_articles(self):
        material = self.group(self.report(), category_key("Matériel"))

        self.assertEqual(material.name, "Matériel")
        self.assertEqual(material.money, money("152.45", "182.94"))
        self.assertEqual(
            [(part.name, part.money) for part in material.members],
            [("Perceuse sans fil", money("100.00", "120.00")), ("Nappe en lin", money("52.45", "62.94"))],
        )

    def test_the_duty_is_spread_over_the_lines_pro_rata_to_their_ht(self):
        """B's 7,31 € of duty: 3,655 € on the rum's 60 €, 2,4367 € on the
        tablecloth's 40 €, 1,2183 € on the unclassified 20 € - and the
        centime rounding each one loses comes off the largest, the rum."""
        report = self.report()

        self.assertEqual(self.member(report, article_key(self.rum.pk)).money, money("113.65", "136.39"))
        self.assertEqual(self.group(report, TO_CLASSIFY_KEY).money, money("36.22", "40.46"))

    def test_a_receipt_paid_at_its_printed_total_keeps_the_difference_on_its_largest_place(self):
        """C printed 18,35 € under lines adding to 18,33 €: what was paid is
        the invoice's TTC, and the two centimes go to the tablecloth."""
        report = self.report()

        self.assertEqual(self.member(report, article_key(self.cloth.pk)).money, money("52.45", "62.94"))
        self.assertEqual(self.group(report, category_key("")).money, money("36.00", "37.98"))

    def test_a_blank_category_is_named_rather_than_dropped(self):
        blank = self.group(self.report(), category_key(""))

        self.assertEqual(blank.name, NO_CATEGORY)
        self.assertEqual([part.name for part in blank.members], ["Sirop d'églantier"])

    def test_an_invoice_with_no_line_stays_whole_on_its_supplier_or_on_a_classer(self):
        report = self.report()

        self.assertEqual(self.member(report, supplier_key(self.insurer.pk)).money, money("74.99", "80.99"))
        # 21,22 € from B's unclassified line and D's 15,00 € with no line.
        self.assertEqual(self.group(report, TO_CLASSIFY_KEY).money.ht, Decimal("36.22"))
        self.assertEqual(self.group(report, TO_CLASSIFY_KEY).members, [])

    def test_the_charges_come_first_and_a_classer_last(self):
        names = [group.name for group in self.report().spend_groups]

        self.assertEqual(names, ["Charges", "Matériel", "Spiritueux", NO_CATEGORY, "Sans article (à classer)"])

    def test_each_place_says_its_share_of_everything_invoiced(self):
        report = self.report()

        self.assertEqual(
            self.group(report, category_key("Matériel")).share.quantize(Decimal("0.01")),
            (Decimal("152.45") / Decimal("913.31") * 100).quantize(Decimal("0.01")),
        )
        self.assertEqual(sum(group.share for group in report.spend_groups).quantize(Decimal("0.01")), Decimal("100.00"))

    def test_an_invoice_outside_the_window_is_in_no_place(self):
        report = self.report()

        self.assertNotIn(category_key("Consignes"), [group.key for group in report.spend_groups])
        self.assertEqual(self.member(report, article_key(self.drill.pk)).money.ht, Decimal("100.00"))


class LeavingOutTests(SpendFixture, TestCase):
    """What each key takes out - exactly its own places, and never twice."""

    def assertLeftOut(self, report, ht, ttc):
        self.assertEqual(report.spend_left_out, money(ht, ttc))
        self.assertEqual(report.spend_kept, Money(report.spend.ht - Decimal(ht), report.spend.ttc - Decimal(ttc)))
        self.assertEqual(report.kept_margin_ht, report.revenue.ht - report.spend.ht + Decimal(ht))
        self.assertEqual(report.kept_margin_ttc, report.revenue.ttc - report.spend.ttc + Decimal(ttc))

    def test_sans_materiel_takes_exactly_its_lines_and_its_share_of_each_adjustment(self):
        """The drill's 100 €, the tablecloth's 40 € + 10 € and - on top -
        2,44 € of B's duty and 0,01 € of C's HT rounding, plus the two
        centimes of C's printed total."""
        report = self.report(category_key("Matériel"))

        self.assertLeftOut(report, "152.45", "182.94")
        self.assertEqual(report.kept_margin_ht, Decimal("239.14"))
        self.assertEqual(report.kept_margin_ttc, Decimal("304.18"))
        self.assertEqual(report.kept_margin_percent.quantize(Decimal("0.001")), Decimal("23.914"))

    def test_the_global_margin_is_still_there_beside_it(self):
        report = self.report(category_key("Matériel"))

        self.assertEqual(report.spend, money("913.31", "1078.76"))
        self.assertEqual(report.real_margin_ht, Decimal("86.69"))

    def test_sans_charges(self):
        report = self.report(CHARGES_KEY)

        self.assertLeftOut(report, "574.99", "680.99")
        self.assertEqual(report.kept_margin_ht, Decimal("661.68"))
        self.assertEqual(report.spend_kept, report.spend_goods)

    def test_one_charge_supplier(self):
        self.assertLeftOut(self.report(supplier_key(self.insurer.pk)), "74.99", "80.99")

    def test_one_article(self):
        self.assertLeftOut(self.report(article_key(self.rum.pk)), "113.65", "136.39")

    def test_a_classer(self):
        self.assertLeftOut(self.report(TO_CLASSIFY_KEY), "36.22", "40.46")

    def test_the_blank_category(self):
        self.assertLeftOut(self.report(category_key("")), "36.00", "37.98")

    def test_two_at_once(self):
        report = self.report(category_key("Matériel"), CHARGES_KEY)

        self.assertLeftOut(report, "727.44", "863.93")
        self.assertEqual([exclusion.name for exclusion in report.exclusions], ["Matériel", "Charges"])

    def test_a_supplier_inside_the_charges_left_out_is_not_taken_out_twice(self):
        report = self.report(CHARGES_KEY, supplier_key(self.insurer.pk))

        self.assertLeftOut(report, "574.99", "680.99")
        (_, insurer) = report.exclusions
        self.assertEqual(insurer.within, "Charges")

    def test_an_article_inside_its_category_left_out_is_not_taken_out_twice(self):
        report = self.report(article_key(self.drill.pk), category_key("Matériel"))

        self.assertLeftOut(report, "152.45", "182.94")
        self.assertEqual(report.exclusions[0].within, "Matériel")
        self.assertIsNone(report.exclusions[1].within)

    def test_each_exclusion_says_what_it_takes_out_on_its_own(self):
        report = self.report(category_key("Matériel"), article_key(self.rum.pk))

        self.assertEqual(
            [(exclusion.name, exclusion.money) for exclusion in report.exclusions],
            [("Matériel", money("152.45", "182.94")), ("Rhum ambré", money("113.65", "136.39"))],
        )

    def test_a_category_with_nothing_in_the_window_is_named_and_takes_out_nothing(self):
        """Consignes were only bought in April. Left out over March, they
        are the owner's question all the same: named, at 0,00 €, and the
        second margin is the first."""
        report = self.report(category_key("Consignes"))

        self.assertEqual([(exclusion.name, exclusion.money) for exclusion in report.exclusions], [("Consignes", Money())])
        self.assertEqual(report.kept_margin_ht, report.real_margin_ht)
        self.assertEqual(report.kept_margin_ttc, report.real_margin_ttc)

    def test_an_article_with_nothing_in_the_window_is_named_too(self):
        report = self.report(article_key(self.keg.pk))

        self.assertEqual([exclusion.name for exclusion in report.exclusions], ["Fût consigné"])
        self.assertEqual(report.spend_left_out, Money())

    def test_the_revenue_does_not_move(self):
        """A cost is taken out, never a sale: whatever is left out, the till
        took what it took."""
        everything = self.report()
        for keys in (
            (category_key("Matériel"),),
            (CHARGES_KEY,),
            (TO_CLASSIFY_KEY, article_key(self.rum.pk), supplier_key(self.landlord.pk)),
        ):
            with self.subTest(keys=keys):
                report = self.report(*keys)
                self.assertEqual(report.revenue, everything.revenue)
                self.assertEqual(report.revenue, money("1000.00", "1200.00"))

    def test_the_boxes_say_what_is_left_out(self):
        report = self.report(category_key("Matériel"), article_key(self.rum.pk))

        material = self.group(report, category_key("Matériel"))
        self.assertTrue(material.unticked)
        self.assertTrue(all(part.left_out and not part.unticked for part in material.members))
        rum = self.member(report, article_key(self.rum.pk))
        self.assertTrue(rum.unticked and rum.left_out)
        self.assertFalse(self.group(report, CHARGES_KEY).unticked)


class KeysTests(SpendFixture, TestCase):
    """What a key is, read off a query string anyone can type."""

    def test_a_key_the_page_does_not_know_is_ignored_never_an_error(self):
        garbled = (
            "",
            "rien",
            "article:",
            "article:abc",
            "article:²",
            "article:-3",
            "article:99999",
            "article:1234567890123456789012",
            "fournisseur:x",
            "fournisseur:99999",
            "categorie:Inexistante",
            "Charges",
            "charges:",
        )
        for key in garbled:
            with self.subTest(key=key):
                report = self.report(key)
                self.assertEqual(report.exclusions, [])
                self.assertEqual(report.kept_margin_ht, report.real_margin_ht)

    def test_a_supplier_of_goods_is_no_charge_to_leave_out(self):
        goods = make_supplier(name="Autre grossiste")

        self.assertEqual(self.report(supplier_key(goods.pk)).exclusions, [])

    def test_the_same_key_twice_is_one_exclusion(self):
        report = self.report(CHARGES_KEY, CHARGES_KEY)

        self.assertEqual(len(report.exclusions), 1)
        self.assertEqual(report.spend_left_out.ht, Decimal("574.99"))

    def test_an_id_with_leading_zeros_is_the_same_article(self):
        report = self.report(f"article:000{self.rum.pk}")

        self.assertEqual([exclusion.key for exclusion in report.exclusions], [article_key(self.rum.pk)])

    def test_a_category_is_its_name_exactly_accents_spaces_and_all(self):
        stool = make_stock_type(name="Tabouret haut", category="Mobilier de salle")
        invoice = make_invoice(invoice_date=date(2026, 3, 9))
        line(invoice, stool, "45.00")

        report = self.report(category_key("Mobilier de salle"))

        self.assertEqual(report.spend_left_out.ht, Decimal("45.00"))
        self.assertEqual(self.report("categorie:Mobilier").exclusions, [])


class DutyOverReturnedDepositsTests(TestCase):
    """A deposit given back is a negative line. Spread pro rata to a signed
    HT, the duty on the beer would put a negative share on the returned keg
    and more than the whole duty on the beer; it goes to what was
    delivered."""

    def test_a_returned_deposit_takes_no_share_of_the_duty(self):
        beer = make_stock_type(name="Bière Zéphyr", category="Bières")
        deposit = make_stock_type(name="Consigne fût", category="Consignes")
        invoice = make_invoice(invoice_date=date(2026, 3, 4), reconciliation_adjustment=Decimal("6.00"))
        line(invoice, beer, "90.00", taxes=Decimal("4.00"))
        line(invoice, deposit, "-30.00")

        report = margins_for(MARCH)

        groups = {group.key: group.money for group in report.spend_groups}
        self.assertEqual(groups[category_key("Bières")], money("96.00", "115.20"))
        self.assertEqual(groups[category_key("Consignes")], money("-30.00", "-36.00"))
        self.assertEqual(sum(groups.values(), start=Money()), report.spend)

    def test_lines_that_cancel_out_put_the_whole_adjustment_on_the_largest(self):
        beer = make_stock_type(name="Bière brune", category="Bières")
        invoice = make_invoice(invoice_date=date(2026, 3, 4), reconciliation_adjustment=Decimal("2.00"))
        line(invoice, beer, "0.00")
        line(invoice, None, "0.00")

        report = margins_for(MARCH)

        self.assertEqual(sum((group.money for group in report.spend_groups), start=Money()), report.spend)
        self.assertEqual(report.spend.ht, Decimal("2.00"))


class OverAllTimeTests(TestCase):
    """With no window an undated invoice IS part of the spending
    (`undated_in_spend`), so it has a place like any other."""

    def test_an_undated_invoice_has_a_place_when_it_is_in_the_spending(self):
        from invoices.models import Invoice

        invoice = Invoice.objects.create(supplier=make_supplier(name="Grossiste sans date"), invoice_date=None)
        line(invoice, None, "40.00")

        report = margins_for(DateRange())

        self.assertEqual(report.spend.ht, Decimal("40.00"))
        self.assertEqual(sum((group.money for group in report.spend_groups), start=Money()), report.spend)


class OneReadingOfTheInvoicesTests(SpendFixture, TestCase):
    """Per LINE now, over every invoice of the window: one prefetch, never a
    query per invoice or per line - and three kinds of key cost a query
    each, whatever their number."""

    def test_more_invoices_and_more_keys_cost_no_more_queries(self):
        with CaptureQueriesContext(connection) as small:
            margins_for(MARCH, [category_key("Matériel"), article_key(self.rum.pk), supplier_key(self.insurer.pk)])

        for index in range(6):
            article = make_stock_type(name=f"Article {index}", category=f"Catégorie {index}")
            invoice = make_invoice(invoice_date=date(2026, 3, 20), reconciliation_adjustment=Decimal("1.00"))
            line(invoice, article, "10.00")
            line(invoice, None, "5.00")
            charge = make_supplier(name=f"Charge {index}", expenses_only=True)
            line(make_invoice(supplier=charge, invoice_date=date(2026, 3, 21)), None, "12.00")
        keys = [category_key(f"Catégorie {index}") for index in range(6)]
        keys += [article_key(self.drill.pk), article_key(self.keg.pk), supplier_key(self.landlord.pk)]
        with CaptureQueriesContext(connection) as large:
            margins_for(MARCH, keys)

        self.assertEqual(len(large), len(small), "une requête par facture ou par ligne s'est glissée")
