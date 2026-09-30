"""The request's log is an append-only hash chain
(`signature_requests.log_event`, `verify_event_chain`): each event's hash
covers the one before it and the event's own content, so an event edited,
removed or cut off the end after the fact no longer adds up."""

import datetime as dt
from datetime import date
from itertools import pairwise

from staff import signature_requests as requests_
from staff.models import Establishment, SignatureEvent, SignatureRequest
from staff.tests.signing_support import SigningTestMixin
from staff.tests.support import employee
from staff.timesheet import save_month
from tests.support import NoNetworkTestCase

JUNE = date(2026, 6, 1)
NOW = dt.datetime(2026, 7, 2, 8, 0, tzinfo=dt.UTC)
Kind = SignatureEvent.Kind


class EventChainTests(SigningTestMixin, NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE")
        self.person = employee()
        save_month(self.person, JUNE, [])
        self.request, _token = requests_.create_request(self.person, JUNE, now=NOW, ip="203.0.113.7")
        for minute, kind in ((1, Kind.LINK_OPENED), (2, Kind.CODE_GIVEN), (3, Kind.CODE_FAILED)):
            requests_.log_event(
                self.request,
                kind,
                at=NOW + dt.timedelta(minutes=minute),
                ip="203.0.113.8",
                user_agent="Essai/1.0",
                detail={"essai": minute},
            )
        self.request.refresh_from_db()

    def events(self):
        return list(self.request.events.order_by("id"))

    def test_an_untouched_chain_verifies(self):
        check = requests_.verify_event_chain(self.request)
        self.assertTrue(check.ok, check.message)
        self.assertEqual(check.count, 4)
        self.assertIn("intègre", check.message)
        events = self.events()
        self.assertEqual(events[0].previous_hash, "0" * 64)
        for before, after in pairwise(events):
            self.assertEqual(after.previous_hash, before.hash)
        self.assertEqual(self.request.last_event_hash, events[-1].hash)

    def test_every_field_of_an_event_is_covered(self):
        target = self.events()[2]
        changes = {
            "kind": Kind.CODE_VERIFIED,
            "at": target.at + dt.timedelta(seconds=1),
            "ip": "203.0.113.99",
            "user_agent": "Autre/2.0",
            "detail": {"essai": 99},
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                original = getattr(target, field)
                SignatureEvent.objects.filter(pk=target.pk).update(**{field: value})
                check = requests_.verify_event_chain(self.request)
                self.assertFalse(check.ok)
                self.assertEqual(check.broken_at, 3)
                self.assertIn("événement n° 3", check.message)
                SignatureEvent.objects.filter(pk=target.pk).update(**{field: original})
                self.assertTrue(requests_.verify_event_chain(self.request).ok)

    def test_an_event_removed_from_the_middle_is_seen(self):
        SignatureEvent.objects.filter(pk=self.events()[1].pk).delete()
        check = requests_.verify_event_chain(self.request)
        self.assertFalse(check.ok)

    def test_an_event_cut_off_the_end_is_seen(self):
        SignatureEvent.objects.filter(pk=self.events()[-1].pk).delete()
        check = requests_.verify_event_chain(self.request)
        self.assertFalse(check.ok)
        self.assertIn("dernier", check.message)

    def test_a_forged_rehash_of_the_whole_chain_still_misses_the_request_row(self):
        """Rewriting an event and every hash after it makes the chain add up
        again - but not the head the request row holds."""
        events = self.events()
        previous = events[1].hash
        for event in events[2:]:
            detail = {"essai": 0} if event.pk == events[2].pk else event.detail
            forged = requests_.event_hash(
                self.request.uuid, previous, event.at, event.kind, event.ip, event.user_agent, detail
            )
            SignatureEvent.objects.filter(pk=event.pk).update(detail=detail, previous_hash=previous, hash=forged)
            previous = forged
        check = requests_.verify_event_chain(self.request)
        self.assertFalse(check.ok)
        self.assertIn("dernier", check.message)

    def test_an_event_is_never_saved_twice(self):
        event = self.events()[0]
        event.detail = {"change": True}
        with self.assertRaises(ValueError):
            event.save()

    def test_what_is_stored_is_what_was_hashed(self):
        """An IPv6 written two ways, a user agent past the column, a detail
        holding a date: all normalised BEFORE hashing, so reading them back
        gives the same hash."""
        requests_.log_event(
            self.request,
            Kind.LINK_OPENED,
            ip="2001:0DB8:0000:0000:0000:0000:0000:0001",
            user_agent="A" * 400,
            detail={"quand": dt.date(2026, 7, 2), "n": 3, "rien": None},
        )
        requests_.log_event(self.request, Kind.LINK_OPENED, ip="pas une adresse", user_agent="")
        event, bad_ip = self.events()[-2:]
        self.assertEqual(event.ip, "2001:db8::1")
        self.assertEqual(len(event.user_agent), 255)
        self.assertEqual(event.detail, {"quand": "2026-07-02", "n": 3, "rien": None})
        self.assertIsNone(bad_ip.ip)
        self.request.refresh_from_db()
        self.assertTrue(requests_.verify_event_chain(self.request).ok)

    def test_each_request_has_its_own_chain(self):
        other_person = employee(last_name="Martin", first_name="Paul")
        save_month(other_person, JUNE, [])
        other, _token = requests_.create_request(other_person, JUNE, now=NOW)
        self.assertEqual(other.events.first().previous_hash, "0" * 64)
        SignatureEvent.objects.filter(pk=self.events()[1].pk).update(user_agent="changé")
        self.assertFalse(requests_.verify_event_chain(self.request).ok)
        self.assertTrue(requests_.verify_event_chain(other).ok)

    def test_events_read_as_french_sentences(self):
        lines = [requests_.describe_event(event) for event in self.events()]
        self.assertEqual(lines[0].title, "Demande créée, document figé")
        self.assertIn("203.0.113.7", lines[0].where)
        self.assertEqual(lines[0].when, "02/07/2026 à 10:00:00")
        self.assertTrue(all(line.title for line in lines))


class ClientAddressTests(SigningTestMixin, NoNetworkTestCase):
    """What REMOTE_ADDR says is hashed as the column stores it (review,
    28/09): a server listening on [::] reports an IPv4 client as
    « ::ffff:203.0.113.7 »; hashed in Python's form « ::ffff:cb00:7107 »
    and stored in Django's, an untouched journal read « Journal altéré »
    in the proof file and on the owner's page."""

    def test_every_way_of_writing_an_address_keeps_the_chain_whole(self):
        from django.test import Client
        from django.urls import reverse

        Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE")
        person = employee()
        save_month(person, JUNE, [])
        request, token = requests_.create_request(person, JUNE)
        url = reverse("staff:sign", args=[token])
        for remote in ("::ffff:203.0.113.7", "::ffff:cb00:7107", "fe80::1%eth0", "2001:DB8::1", "203.0.113.7"):
            with self.subTest(remote=remote):
                self.assertEqual(Client(REMOTE_ADDR=remote).get(url).status_code, 200)
                event = request.events.order_by("-id").first()
                self.assertEqual(event.kind, Kind.LINK_OPENED)
                self.assertEqual(event.ip, requests_._clean_ip(remote))
                request.refresh_from_db()
                check = requests_.verify_event_chain(request)
                self.assertTrue(check.ok, check.message)
        self.assertEqual(requests_._clean_ip("::ffff:cb00:7107"), "::ffff:203.0.113.7")
        self.assertEqual(requests_._clean_ip("fe80::1%eth0"), "fe80::1")
        self.assertIsNone(requests_._clean_ip("pas une adresse"))


class EventOrderTests(NoNetworkTestCase):
    def test_no_request_no_chain_to_check(self):
        request = SignatureRequest(uuid="11111111-2222-4333-8444-555555555555")
        check = requests_.verify_event_chain(request)
        self.assertTrue(check.ok)
        self.assertEqual(check.count, 0)
