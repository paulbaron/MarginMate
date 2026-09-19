"""Every new database is seeded with the suppliers that have a reader of
their own (invoices/0002). They carry their own names - never the name of
the bar that first used the app, which is for any bar or restaurant."""

from django.test import TestCase

from invoices.models import Supplier


class SeededSuppliersTests(TestCase):
    def test_they_are_named_as_they_name_themselves(self):
        self.assertEqual(
            dict(Supplier.objects.filter(code__in=["METRO", "UBA"]).values_list("code", "name")),
            {"METRO": "Metro", "UBA": "UBA"},
        )
