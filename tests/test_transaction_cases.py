"""Every TransactionTestCase of the project restores what the migrations
seeded (`serialized_rollback = True`), so that none of them fires
post_migrate when it flushes the database.

One that does not recreates the content types under new pks when it
flushes, and the next one that does restore its snapshot fails in
setUpClass on « UNIQUE constraint failed: django_content_type.app_label,
django_content_type.model ». In a whole run (`manage.py test`, no tag) that
was every « Données » safety and page test after the browser classes, and
the new « Tout décocher » browser test in the browser suite (verifier,
19/09) - the fast loop and the browser suite apart hid it from each other.
"""

import importlib
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, TransactionTestCase


def test_modules() -> list[str]:
    """Every test module of the project, as the runner discovers them."""
    base = Path(settings.BASE_DIR)
    paths = sorted(base.glob("*/tests/test_*.py")) + sorted(base.glob("tests/test_*.py"))
    return sorted({".".join(path.relative_to(base).with_suffix("").parts) for path in paths})


def transaction_cases() -> list[type]:
    found = []
    for name in test_modules():
        module = importlib.import_module(name)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, TransactionTestCase)
                and not issubclass(value, TestCase)
                and value.__module__ == name
            ):
                found.append(value)
    return found


class SerializedRollbackTests(SimpleTestCase):
    def test_every_transaction_test_case_restores_the_seeded_rows(self):
        cases = transaction_cases()
        # The browser classes of inventory and invoices, and « Données »'s.
        self.assertGreaterEqual(len(cases), 10, [case.__qualname__ for case in cases])
        self.assertEqual(
            [f"{case.__module__}.{case.__qualname__}" for case in cases if not case.serialized_rollback], []
        )
