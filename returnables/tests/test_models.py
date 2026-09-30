"""The returnables tables: what migration 0002 seeds in every database, what
the database refuses on its own, what a deletion keeps (PROTECT) and what
it takes along - the files, only once the deletion commits.

Every name, number and figure is invented (returnables/tests/support.py)."""

import importlib
import re
from datetime import date
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, models, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts import paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices.models import Supplier
from returnables import models as returnables_models
from returnables.models import (
    MAX_COUNT,
    Pickup,
    PickupCount,
    PickupPhoto,
    ReturnableType,
    Slip,
    SlipFormat,
    SlipLine,
    delete_with_files,
    files_of,
    new_reference,
)
from returnables.tests.support import (
    CO2_LINE,
    CRATE_LINE,
    KEG_LINE,
    SEEDED_FORMAT_NAME,
    make_format,
    make_photo,
    make_pickup,
    make_slip,
    make_supplier,
    make_type,
    no_defaults,
    seeded_format,
    tiny_jpeg,
    uba,
)
from tests.runner import test_user as the_owner

SEED_MIGRATION = importlib.import_module("returnables.migrations.0002_seed_defaults")


def app_models():
    return list(apps.get_app_config("returnables").get_models())


def stored(name) -> bool:
    return default_storage.exists(name)


class SeedTests(TestCase):
    """Pinned with literals, like invoices/tests/test_seed_suppliers.py: a
    slip in the migration shows here rather than being copied into it."""

    def test_the_three_usual_types_are_seeded_named_placed_and_active(self):
        self.assertEqual(
            list(ReturnableType.objects.values_list("name", "position", "is_active", "slip_patterns")),
            [
                ("Fûts", 1, True, r"F[ÛU]TS?\b"),
                ("Caisses verre", 2, True, r"CAISSE|CASIER|JUS|SODA|\bEAUX?\b|\d+\s*CL\b"),
                ("Bouteilles CO2", 3, True, r"CO2|\bGAZ\b"),
            ],
        )

    def test_uba_s_slip_format_is_seeded_with_every_pattern(self):
        fmt = SlipFormat.objects.get()
        self.assertEqual(fmt.name, "UBA \N{EM DASH} bon du livreur")
        self.assertEqual((fmt.supplier.code, fmt.is_active), ("UBA", True))
        self.assertEqual(
            {name: getattr(fmt, name) for name in SEED_MIGRATION.FORMAT},
            {
                "sender_pattern": r"mphone@uba\.paris",
                "subject_pattern": r"^\s*Livraison du\b",
                "attachment_pattern": r"(?i)\.pdf$",
                "section_start": r"^REPRISE VIDE\s*$",
                "section_end": r"^FACTURE\(S\)/BL DU JOUR",
                "line_pattern": (
                    r"^(?P<designation>.+?)\s+(?P<quantite>-?\d+)\s+x\s+(?P<prix>-?\d+(?:[.,]\d+)?)\s+=\s+"
                    r"(?P<montant>-?\d+(?:[.,]\d+)?)\s*$"
                ),
                "date_patterns": (
                    r"BL No:\s*\d+\s+du\s+(?P<date>\d{2}/\d{2}/\d{4})" "\n" r"^Le\s+(?P<date>\d{2}/\d{2}/\d{4})"
                ),
                "printed_patterns": r"^Le\s+(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<heure>\d{2}:\d{2}(?::\d{2})?)",
                "number_patterns": r"Ticket No\s*:\s*0*(?P<numero>\d+)",
                "reference_patterns": r"BL No:\s*(?P<reference>\d+)",
                "replaces_pattern": r"ANNULE ET REMPLACE",
                "total_patterns": r"Deconsigne\s*:\s*(?P<total>-?\d+(?:[.,]\d+)?)",
                "remarks_start": r"^ANOMALIES\s*$",
                "remarks_end": r"^Merci de Votre Commande",
            },
        )
        # Every pattern field of the model is seeded: none left to its default.
        pattern_fields = {
            field.name
            for field in SlipFormat._meta.get_fields()
            if field.name.endswith(("_pattern", "_patterns", "_start", "_end"))
        }
        self.assertEqual(pattern_fields, set(SEED_MIGRATION.FORMAT))

    def test_the_seeded_type_patterns_classify_the_invented_lines_of_the_tests(self):
        """What C and D's tests lean on: each invented line is recognised by
        its own type and by no other (plain `re`, small patterns - the guarded
        compiler is returnables/patterns.py's)."""
        regexes = {kind.name: re.compile(kind.slip_patterns, re.IGNORECASE) for kind in ReturnableType.objects.all()}
        for line, expected in ((KEG_LINE, "Fûts"), (CRATE_LINE, "Caisses verre"), (CO2_LINE, "Bouteilles CO2")):
            with self.subTest(line=line[0]):
                self.assertEqual([name for name, compiled in regexes.items() if compiled.search(line[0])], [expected])

    def test_the_migration_depends_on_the_seeded_suppliers_and_reverses_to_nothing(self):
        migration = SEED_MIGRATION.Migration
        self.assertEqual(migration.dependencies, [("returnables", "0001_initial"), ("invoices", "0002_seed_suppliers")])
        (operation,) = migration.operations
        self.assertIs(operation.code, SEED_MIGRATION.seed)
        self.assertIs(operation.reverse_code, SEED_MIGRATION.migrations.RunPython.noop)

    def test_without_uba_only_the_types_are_seeded(self):
        no_defaults()
        Supplier.objects.filter(code="UBA").update(code="UBA-ANCIEN")
        SEED_MIGRATION.seed(apps, None)
        self.assertEqual(ReturnableType.objects.count(), 3)
        self.assertFalse(SlipFormat.objects.exists())

    def test_seeding_again_leaves_what_exists_as_it_is(self):
        keg = ReturnableType.objects.get(position=1)
        keg.slip_patterns = "FUT"
        keg.save()
        fmt = seeded_format()
        fmt.line_pattern = r"^(?P<designation>.+) (?P<quantite>\d+)$"
        fmt.save()
        SEED_MIGRATION.seed(apps, None)
        self.assertEqual(ReturnableType.objects.count(), 3)
        self.assertEqual(ReturnableType.objects.get(pk=keg.pk).slip_patterns, "FUT")
        self.assertEqual(SlipFormat.objects.get().line_pattern, r"^(?P<designation>.+) (?P<quantite>\d+)$")

    def test_no_defaults_empties_the_app(self):
        no_defaults()
        self.assertFalse(ReturnableType.objects.exists())
        self.assertFalse(SlipFormat.objects.exists())
        self.assertTrue(Supplier.objects.filter(code="UBA").exists())


class ReferenceTests(TestCase):
    def test_sixteen_hex_characters_never_the_same_twice(self):
        references = {new_reference() for _ in range(50)}
        self.assertEqual(len(references), 50)
        for reference in references:
            self.assertRegex(reference, r"\A[0-9a-f]{16}\Z")

    def test_every_pickup_gets_one_and_keeps_it(self):
        pickup = make_pickup()
        self.assertRegex(pickup.reference, r"\A[0-9a-f]{16}\Z")
        before = pickup.reference
        pickup.note = "modifiée"
        pickup.save()
        self.assertEqual(Pickup.objects.get(pk=pickup.pk).reference, before)
        self.assertNotEqual(make_pickup().reference, before)

    def test_it_is_the_natural_key_so_the_database_refuses_it_twice(self):
        first = make_pickup()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Pickup.objects.create(date=date(2026, 2, 11), reference=first.reference)

    def test_it_is_never_a_form_field(self):
        self.assertFalse(Pickup._meta.get_field("reference").editable)


class CountTests(TestCase):
    def setUp(self):
        self.pickup = make_pickup(counts={})
        self.keg = ReturnableType.objects.get(position=1)

    def test_one_count_per_type_and_pickup(self):
        PickupCount.objects.create(pickup=self.pickup, returnable_type=self.keg, quantity=3)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            PickupCount.objects.create(pickup=self.pickup, returnable_type=self.keg, quantity=4)
        other = make_pickup(counts={})
        PickupCount.objects.create(pickup=other, returnable_type=self.keg, quantity=4)

    def test_zero_is_no_row_and_ten_thousand_is_refused_by_the_database(self):
        for quantity in (0, MAX_COUNT + 1):
            with self.subTest(quantity=quantity), transaction.atomic(), self.assertRaises(IntegrityError):
                PickupCount.objects.create(pickup=self.pickup, returnable_type=self.keg, quantity=quantity)
        PickupCount.objects.create(pickup=self.pickup, returnable_type=self.keg, quantity=MAX_COUNT)
        self.assertEqual(MAX_COUNT, 9_999)

    def test_the_validators_speak_french(self):
        for quantity in (0, MAX_COUNT + 1):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError) as refusal:
                PickupCount(pickup=self.pickup, returnable_type=self.keg, quantity=quantity).full_clean()
            self.assertIn("1 à 9 999", " ".join(refusal.exception.message_dict["quantity"]))

    def test_the_factory_leaves_a_zero_out(self):
        pickup = make_pickup(counts={"Caisses verre": 0, "Bouteilles CO2": 2})
        self.assertEqual(list(pickup.counts.values_list("returnable_type__name", "quantity")), [("Bouteilles CO2", 2)])


class OrderTests(TestCase):
    def test_types_by_position_then_by_age(self):
        no_defaults()
        late = make_type("Tonnelets", position=2)
        first = make_type("Fûts", position=1)
        twin = make_type("Palettes", position=2)
        self.assertEqual(list(ReturnableType.objects.all()), [first, late, twin])


class ProtectTests(TestCase):
    """A type counts were made with, a format slips were read by, a supplier
    a format or a pickup names: kept, the page says « désactivez-le »."""

    def test_a_type_with_counts_is_kept(self):
        kind = make_type("Tonnelets")
        make_pickup(counts={kind: 2})
        with self.assertRaises(ProtectedError):
            kind.delete()
        unused = make_type("Palettes")
        unused.delete()
        self.assertFalse(ReturnableType.objects.filter(name="Palettes").exists())

    def test_a_format_with_slips_is_kept(self):
        fmt = make_format()
        make_slip(fmt)
        with self.assertRaises(ProtectedError):
            fmt.delete()
        unused = make_format()
        unused_pk = unused.pk  # delete() sets it to None
        unused.delete()
        self.assertFalse(SlipFormat.objects.filter(pk=unused_pk).exists())
        self.assertTrue(SlipFormat.objects.filter(pk=fmt.pk).exists())

    def test_a_supplier_held_by_a_format_or_a_pickup_is_kept(self):
        by_format = make_supplier()
        make_format(supplier=by_format)
        by_pickup = make_supplier()
        make_pickup(supplier=by_pickup)
        for supplier in (by_format, by_pickup):
            with self.subTest(supplier=supplier.name), self.assertRaises(ProtectedError):
                supplier.delete()

    def test_a_pickup_needs_no_supplier(self):
        pickup = make_pickup(supplier=None)
        self.assertIsNone(Pickup.objects.get(pk=pickup.pk).supplier)

    def test_a_pickup_takes_its_counts_and_photos_a_slip_its_lines(self):
        pickup = make_pickup(counts={"Fûts": 3, "Bouteilles CO2": 1}, photos=2)
        slip = make_slip(lines=[KEG_LINE, CO2_LINE])
        with self.captureOnCommitCallbacks(execute=True):
            delete_with_files(pickup)
            delete_with_files(slip)
        self.assertFalse(PickupCount.objects.exists())
        self.assertFalse(PickupPhoto.objects.exists())
        self.assertFalse(SlipLine.objects.exists())
        self.assertEqual(ReturnableType.objects.count(), 3)


class FileFieldTests(TestCase):
    def test_every_file_is_on_the_tenant_storage_under_the_returnables_folder_at_most_100_characters(self):
        fields = [
            field for model in app_models() for field in model._meta.get_fields() if isinstance(field, models.FileField)
        ]
        self.assertEqual(
            sorted(f"{field.model.__name__}.{field.name}" for field in fields),
            ["PickupPhoto.image", "PickupPhoto.thumb", "Slip.file"],
        )
        for field in fields:
            with self.subTest(field=field.name):
                self.assertEqual(field.max_length, 100)
                self.assertIs(field.storage, default_storage)
                self.assertNotIn("storage", field.deconstruct()[3])
                self.assertTrue(field.upload_to.startswith("consignes/"), field.upload_to)

    def test_files_land_in_the_tenant_s_media_and_are_served_behind_the_login(self):
        photo = make_photo(make_pickup())
        slip = make_slip()
        for field in (photo.image, photo.thumb, slip.file):
            with self.subTest(name=field.name):
                self.assertTrue(field.name.startswith("consignes/"), field.name)
                self.assertTrue((Path(paths.media_root()) / field.name).is_file())
                self.assertEqual(field.url, reverse("accounts:media", args=[field.name]))

    def test_the_longest_name_a_slip_gets_fits_twice_over(self):
        """bon-<20 characters>.pdf, and the same again: the storage adds its
        random suffix and the name still fits the column."""
        names = []
        for _ in range(2):
            slip = Slip(format=seeded_format(), origin=Slip.Origin.UPLOAD, sha256=new_reference() * 4)
            slip.file.save("bon-" + "9" * 20 + ".pdf", ContentFile(b"%PDF-1.4"), save=False)
            slip.save()
            names.append(slip.file.name)
        self.assertNotEqual(names[0], names[1])
        for name in names:
            self.assertLessEqual(len(name), 100, name)
            self.assertTrue(name.startswith("consignes/bons/"), name)


class DeletionTests(TestCase):
    """Rows now, files once the deletion commits (invoices/deletion.py)."""

    def test_a_pickup_s_photos_and_thumbnails_go_after_the_commit_only(self):
        pickup = make_pickup(photos=2)
        pickup_pk = pickup.pk  # delete() sets it to None
        names = [name for _storage, name in files_of(pickup)]
        self.assertEqual(len(names), 4)
        with self.captureOnCommitCallbacks() as callbacks:
            delete_with_files(pickup)
        self.assertFalse(Pickup.objects.filter(pk=pickup_pk).exists())
        self.assertFalse(PickupPhoto.objects.filter(pickup_id=pickup_pk).exists())
        self.assertTrue(all(stored(name) for name in names), "deleted before the commit")
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertFalse(any(stored(name) for name in names))

    def test_a_deletion_rolled_back_keeps_rows_and_files(self):
        pickup = make_pickup(photos=1)
        slip = make_slip()
        names = [name for obj in (pickup, slip) for _storage, name in files_of(obj)]
        # delete() sets the instances' pk to None: kept apart.
        pickup_pk, slip_pk = pickup.pk, slip.pk
        with (
            self.captureOnCommitCallbacks(execute=True) as callbacks,
            self.assertRaises(RuntimeError),
            transaction.atomic(),
        ):
            delete_with_files(pickup)
            delete_with_files(slip)
            raise RuntimeError("après la suppression")
        self.assertEqual(callbacks, [])
        self.assertTrue(Pickup.objects.filter(pk=pickup_pk).exists())
        self.assertTrue(Slip.objects.filter(pk=slip_pk).exists())
        self.assertEqual(PickupPhoto.objects.filter(pickup_id=pickup_pk).count(), 1)
        self.assertTrue(all(stored(name) for name in names))

    def test_one_photo_goes_with_its_two_files_and_nothing_else(self):
        pickup = make_pickup()
        kept, removed = make_photo(pickup), make_photo(pickup)
        gone = [removed.image.name, removed.thumb.name]
        with self.captureOnCommitCallbacks(execute=True):
            delete_with_files(removed)
        self.assertEqual(list(pickup.photos.all()), [kept])
        self.assertFalse(any(stored(name) for name in gone))
        self.assertTrue(stored(kept.image.name) and stored(kept.thumb.name))

    def test_a_slip_goes_with_its_pdf_and_its_lines(self):
        slip = make_slip(lines=[KEG_LINE, CRATE_LINE])
        slip_pk, name = slip.pk, slip.file.name
        other = make_slip()
        with self.captureOnCommitCallbacks(execute=True):
            delete_with_files(slip)
        self.assertFalse(stored(name))
        self.assertFalse(Slip.objects.filter(pk=slip_pk).exists())
        self.assertFalse(SlipLine.objects.filter(slip_id=slip_pk).exists())
        self.assertTrue(stored(other.file.name))
        self.assertEqual(other.lines.count(), 1)

    def test_a_file_that_cannot_be_deleted_is_left_the_others_go(self):
        pickup = make_pickup(photos=2)
        pickup_pk = pickup.pk  # delete() sets it to None
        names = [name for _storage, name in files_of(pickup)]
        real_delete = default_storage.delete
        attempts = []

        def locked_first(name):
            attempts.append(name)
            if len(attempts) == 1:
                raise PermissionError("tenu par un autre programme")
            real_delete(name)

        with (
            mock.patch.object(default_storage, "delete", side_effect=locked_first),
            self.captureOnCommitCallbacks(execute=True),
        ):
            delete_with_files(pickup)
        self.assertEqual(attempts, names)
        self.assertFalse(Pickup.objects.filter(pk=pickup_pk).exists())
        self.assertTrue(stored(names[0]))
        self.assertFalse(any(stored(name) for name in names[1:]))

    def test_a_row_in_use_is_refused_and_nothing_is_scheduled(self):
        kind = make_type("Tonnelets")
        make_pickup(counts={kind: 1}, photos=1)
        with self.captureOnCommitCallbacks() as callbacks, self.assertRaises(ProtectedError):
            delete_with_files(kind)
        self.assertEqual(callbacks, [])

    def test_every_file_field_of_the_app_is_collected(self):
        """A FileField added to a model and forgotten here would leave its
        files behind every deletion: listed by introspection, not by hand."""
        pickup = make_pickup(photos=1)
        photo = pickup.photos.get()
        slip = make_slip()
        held = {PickupPhoto: photo, Slip: slip}
        for model in app_models():
            file_fields = [field.name for field in model._meta.get_fields() if isinstance(field, models.FileField)]
            if not file_fields:
                continue
            with self.subTest(model=model.__name__):
                obj = held[model]
                self.assertEqual(
                    sorted(name for _storage, name in files_of(obj)),
                    sorted(getattr(obj, field).name for field in file_fields),
                )
        self.assertEqual(
            sorted(name for _storage, name in files_of(pickup)), sorted([photo.image.name, photo.thumb.name])
        )
        for obj in (ReturnableType.objects.first(), seeded_format(), pickup.counts.first(), slip.lines.first()):
            self.assertEqual(files_of(obj), [])


class ModelShapeTests(SimpleTestCase):
    def test_money_is_decimal_never_float(self):
        for model in app_models():
            for field in model._meta.get_fields():
                with self.subTest(field=f"{model.__name__}.{field.name}"):
                    self.assertNotIsInstance(field, models.FloatField)
        self.assertIsInstance(Slip._meta.get_field("printed_total"), models.DecimalField)
        places = {
            name: (field.max_digits, field.decimal_places)
            for name, field in (
                ("unit_amount", SlipLine._meta.get_field("unit_amount")),
                ("amount", SlipLine._meta.get_field("amount")),
                ("printed_total", Slip._meta.get_field("printed_total")),
            )
        }
        self.assertEqual(places, {"unit_amount": (12, 4), "amount": (12, 2), "printed_total": (12, 2)})

    def test_the_columns_the_readers_cut_their_text_to(self):
        widths = {
            ("ReturnableType", "name"): 60,
            ("SlipFormat", "name"): 80,
            ("SlipFormat", "sender_pattern"): 300,
            ("SlipFormat", "subject_pattern"): 300,
            ("SlipFormat", "attachment_pattern"): 200,
            ("SlipFormat", "line_pattern"): 500,
            ("Slip", "sha256"): 64,
            ("Slip", "original_name"): 200,
            ("Slip", "mail_sender"): 300,
            ("Slip", "mail_subject"): 300,
            ("Slip", "number"): 40,
            ("Slip", "read_error"): 300,
            ("SlipLine", "designation"): 200,
            ("Pickup", "reference"): 16,
        }
        for (model, field), width in widths.items():
            with self.subTest(field=f"{model}.{field}"):
                self.assertEqual(apps.get_model("returnables", model)._meta.get_field(field).max_length, width)

    def test_the_words_the_admin_and_the_messages_use(self):
        self.assertEqual(apps.get_app_config("returnables").verbose_name, "Consignes")
        self.assertEqual(ReturnableType._meta.verbose_name, "type de consigne")
        self.assertEqual(SlipFormat._meta.verbose_name_plural, "formats de bons")
        self.assertEqual(Pickup._meta.verbose_name, "reprise")
        self.assertEqual(Slip._meta.verbose_name, "bon")
        self.assertEqual(dict(Slip.Origin.choices), {"MAIL": "Reçu par mail", "UPLOAD": "Déposé à la main"})

    def test_a_new_format_fetches_pdf_attachments_and_nothing_by_mail_until_told(self):
        fmt = SlipFormat()
        self.assertEqual((fmt.sender_pattern, fmt.attachment_pattern, fmt.is_active), ("", r"(?i)\.pdf$", True))

    def test_the_readings_lists_start_empty_and_apart(self):
        first, second = Slip(), Slip()
        first.references.append("123456")
        self.assertEqual((second.references, second.checks), ([], []))

    def test_the_names_read_on_screen(self):
        self.assertEqual(str(Slip(number="1234", delivery_date=date(2026, 2, 10))), "Bon n° 1234 du 10/02/2026")
        self.assertEqual(str(Slip()), "Bon sans numéro")
        self.assertEqual(str(Pickup(date=date(2026, 2, 10))), "Reprise du 10/02/2026")
        self.assertEqual(str(ReturnableType(name="Fûts")), "Fûts")


class AdminTests(TestCase):
    def test_every_model_is_registered(self):
        for model in app_models():
            with self.subTest(model=model.__name__):
                self.assertTrue(admin.site.is_registered(model))

    def superuser(self):
        """The test tenant's owner, made a superuser: in multi mode the admin
        is theirs alone (accounts/admin_site.py)."""
        owner = the_owner()
        type(owner).objects.filter(pk=owner.pk).update(is_staff=True, is_superuser=True)

    def test_every_list_and_change_page_draws_for_a_superuser(self):
        self.superuser()
        pickup = make_pickup(counts={"Fûts": 2}, photos=1)
        slip = make_slip(lines=[KEG_LINE])
        rows = {
            Pickup: pickup,
            PickupPhoto: pickup.photos.get(),
            PickupCount: pickup.counts.get(),
            Slip: slip,
            SlipLine: slip.lines.get(),
            SlipFormat: seeded_format(),
            ReturnableType: ReturnableType.objects.first(),
        }
        for model in app_models():
            name = model._meta.model_name
            with self.subTest(model=name):
                self.assertEqual(self.client.get(reverse(f"admin:returnables_{name}_changelist")).status_code, 200)
                page = self.client.get(reverse(f"admin:returnables_{name}_change", args=[rows[model].pk]))
                self.assertEqual(page.status_code, 200)
        self.assertContains(
            self.client.get(reverse("admin:returnables_pickup_change", args=[pickup.pk])), pickup.reference
        )

    def test_deleting_from_the_admin_takes_the_files_along_on_commit(self):
        self.superuser()
        pickup = make_pickup(photos=1)
        names = [name for _storage, name in files_of(pickup)]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("admin:returnables_pickup_delete", args=[pickup.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Pickup.objects.filter(pk=pickup.pk).exists())
        self.assertFalse(any(stored(name) for name in names))


class FactoryTests(TestCase):
    """The factories other packages' tests are built on."""

    def test_a_slip_carries_a_real_pdf_its_reading_and_its_lines(self):
        slip = make_slip(lines=[KEG_LINE, CO2_LINE], number="4321", references=["900123"])
        self.assertTrue(slip.file.read(8).startswith(b"%PDF-"))
        slip.file.close()
        self.assertEqual(len(slip.sha256), 64)
        self.assertEqual(
            list(slip.lines.values_list("position", "designation", "quantity", "unit_amount", "amount")),
            [(1, *KEG_LINE), (2, *CO2_LINE)],
        )
        self.assertEqual((slip.number, slip.references), ("4321", ["900123"]))
        self.assertEqual(slip.printed_total, -(KEG_LINE[3] + CO2_LINE[3]))
        self.assertIn("Ticket No : 0000004321", slip.text)
        self.assertIn("BL No: 900123 du 10/02/2026", slip.text)
        self.assertIn(f"{KEG_LINE[0]} 3 x 30.00 = 90.00", slip.text)
        self.assertNotEqual(make_slip().sha256, make_slip().sha256)

    def test_an_empty_slip_prints_a_zero_refund(self):
        slip = make_slip(lines=[])
        self.assertIn("Deconsigne : 0.00", slip.text)
        self.assertEqual(str(slip.printed_total), "0")

    def test_tiny_jpeg_is_a_few_pixels_with_the_exif_asked_for(self):
        from datetime import datetime, timedelta, timezone
        from io import BytesIO

        from PIL import Image

        moment = datetime(2026, 2, 10, 7, 45, tzinfo=timezone(timedelta(hours=1)))
        with Image.open(BytesIO(tiny_jpeg(exif_orientation=6, taken_at=moment))) as image:
            self.assertEqual(image.size, (4, 3))
            exif = image.getexif()
            self.assertEqual(exif.get(0x0112), 6)
            self.assertEqual(exif.get_ifd(0x8769), {0x9003: "2026:02:10 07:45:00", 0x9011: "+01:00"})
        with Image.open(BytesIO(tiny_jpeg(taken_at="0000:00:00 00:00:00"))) as image:
            self.assertEqual(image.getexif().get_ifd(0x8769), {0x9003: "0000:00:00 00:00:00"})
        with Image.open(BytesIO(tiny_jpeg())) as image:
            self.assertEqual(dict(image.getexif()), {})

    def test_a_pickup_with_counts_and_photos(self):
        pickup = make_pickup(counts={"Fûts": 15, "Bouteilles CO2": 1}, photos=2)
        self.assertEqual(pickup.supplier, uba())
        self.assertEqual(
            list(pickup.counts.values_list("returnable_type__name", "quantity")), [("Fûts", 15), ("Bouteilles CO2", 1)]
        )
        # The storage adds a random suffix where a name is taken (the test
        # media folder is shared by the run).
        folder = r"\Aconsignes/photos/\d{4}/\d{2}/"
        for number, photo in enumerate(pickup.photos.all(), start=1):
            self.assertRegex(photo.image.name, rf"{folder}reprise-20260210-{number}(_\w+)?\.jpg\Z")
            self.assertRegex(photo.thumb.name, rf"{folder}reprise-20260210-{number}-vignette(_\w+)?\.jpg\Z")

    def test_no_defaults_then_a_format_of_one_s_own(self):
        no_defaults()
        fmt = make_format("Bon de test", supplier=make_supplier(name="Brasserie Exemple"))
        self.assertEqual(fmt.section_start, r"^REPRISE VIDE\s*$")
        self.assertEqual(list(SlipFormat.objects.values_list("name", flat=True)), ["Bon de test"])
        self.assertFalse(SlipFormat.objects.filter(name=SEEDED_FORMAT_NAME).exists())


class ModuleTests(SimpleTestCase):
    def test_the_reference_default_is_a_module_function_the_migration_can_name(self):
        self.assertIs(Pickup._meta.get_field("reference").default, returnables_models.new_reference)


class TenantTests(TwoTenantsTestCase):
    """Real tenants (accounts.provisioning): each is copied from the
    migrated _template, and keeps its files in its own media."""

    def test_every_new_tenant_is_given_the_seeds(self):
        for bar in (self.bar_a, self.bar_b):
            with self.subTest(bar=bar.name), bound_tenant(bar):
                self.assertEqual(
                    list(ReturnableType.objects.values_list("name", flat=True)),
                    ["Fûts", "Caisses verre", "Bouteilles CO2"],
                )
                self.assertEqual(list(SlipFormat.objects.values_list("supplier__code", flat=True)), ["UBA"])

    def test_a_deletion_in_one_tenant_leaves_the_other_s_file_of_the_same_name(self):
        photos = {}
        for bar, size in ((self.bar_a, (4, 3)), (self.bar_b, (5, 3))):
            with bound_tenant(bar):
                photos[bar.pk] = make_photo(make_pickup(), size=size)
        name = photos[self.bar_a.pk].image.name
        # Same day, same number, each tenant its own folder: the same name.
        self.assertEqual(photos[self.bar_b.pk].image.name, name)
        on_disk = {bar.pk: paths.tenant_dir(bar) / "media" / name for bar in (self.bar_a, self.bar_b)}
        kept = on_disk[self.bar_b.pk].read_bytes()
        self.assertNotEqual(on_disk[self.bar_a.pk].read_bytes(), kept)
        with bound_tenant(self.bar_a):
            delete_with_files(Pickup.objects.get())
        self.assertFalse(on_disk[self.bar_a.pk].exists())
        self.assertEqual(on_disk[self.bar_b.pk].read_bytes(), kept)
        with bound_tenant(self.bar_b):
            self.assertEqual(PickupPhoto.objects.count(), 1)
