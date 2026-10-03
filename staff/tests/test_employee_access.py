"""« Personnel » for an employee (accounts/access.py, the owner's choice of
02/10/2026): he fills in the timesheets - the grid of a month, the
typical week - and the signatures stay the employer's. Sending a month for
signature, the employee's link and code, the countersignature, « Corriger
ce mois », the proof and its files: an employee would sign for his
employer, or hand a colleague's link around. So the gate refuses every one
of those routes, and the month draws no « Signature » card for him - hiding
it is never the boundary. Who opens what (« Accès des employés au site »)
is the owner's too.

Every POST is read OFF THE RENDERED PAGE (`page_forms`) through a client
enforcing CSRF, as the other staff pages' tests do: a refused POST carries
his page's valid token, so the 403 is the gate's. The month and the request
are OwnerCase's (staff/tests/test_signature_pages.py): June 2026 saved for
DUPONT Jeanne of BAR EXEMPLE, signed offline. Names and addresses are
INVENTED.
"""

from decimal import Decimal

from django.core import mail
from django.urls import reverse
from django.utils.html import escape

from accounts import access
from staff.models import SignatureEvent, SignatureRequest
from staff.signature_views import DRAWING, HAND_OVER, REASON
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from staff.tests.signing_support import data_url, employer_signature
from staff.tests.test_signature_pages import OwnerCase
from staff.tests.test_views import stored
from tests.runner import employee_of_the_test_tenant

Status = SignatureRequest.Status
#: The employer's signature routes taking a request's version, each refused.
VERSION_ROUTES = (
    "staff:signature_link",
    "staff:signature_code",
    "staff:signature_countersign",
    "staff:signature_cancel",
    "staff:signature_verify",
    "staff:signature_delete",
    "staff:signature_delete_confirm",
)


class MemberCase(OwnerCase):
    """OwnerCase's month, read by an employee given « Personnel »."""

    def setUp(self):
        super().setUp()
        self.client.force_login(
            employee_of_the_test_tenant("adjoint-personnel@example.invalid", ["staff"], name="Adjoint")
        )

    def token(self) -> str:
        """A valid CSRF token of his own page: a crafted POST has one too."""
        return forms_of(self.html(self.get(reverse("staff:home"))))[0].control("csrfmiddlewaretoken").value

    def assertRefused(self, response, *, posted=False):
        """The gate's page (accounts/refused.html): no view ran."""
        self.assertContains(response, "Page non accessible", status_code=403)
        self.assertContains(response, escape(access.REFUSED), status_code=403)
        if posted:
            self.assertContains(response, escape(access.REFUSED_POST), status_code=403)

    def assertNoSignatureCard(self, response):
        content = self.html(response)
        self.assertNotIn('id="signature"', content)
        self.assertNotIn("signature-card", content)
        self.assertNotIn("<h2>Signature</h2>", content)
        for route in ("staff:signature_send", "staff:month_reopen"):
            with self.subTest(route=route):
                self.assertNotIn(self.route(route), content)

    def snapshot(self):
        """Every request of the month with its status and journal."""
        return [
            (request.version, request.status, self.kinds(request))
            for request in SignatureRequest.objects.filter(timesheet=self.timesheet).order_by("version")
        ]


class AMemberTests(MemberCase):
    def test_the_home_draws_no_access_button_and_the_page_is_refused(self):
        response = self.get(reverse("staff:home"))
        self.assertIn("DUPONT Jeanne", self.text(response))
        self.assertNotContains(response, "Accès des employés au site")
        self.assertNotContains(response, reverse("accounts:members"))
        self.assertRefused(self.client.get(reverse("accounts:members")))

    def test_a_saved_month_draws_its_grid_and_no_signature_card(self):
        response = self.page()
        self.assertNoSignatureCard(response)
        # The grid is his.
        form_posting_to(self.html(response), self.url)

    def test_a_month_under_signature_draws_none_of_its_actions(self):
        request, _token = self.create()
        self.employee_signs(request)
        response = self.page()
        self.assertContains(response, "Les jours du mois")
        self.assertNoSignatureCard(response)
        for route in VERSION_ROUTES:
            with self.subTest(route=route):
                self.assertNotContains(response, self.route(route, 1))

    def test_a_locked_month_tells_him_who_reopens_it(self):
        """« Corriger ce mois » is in the Signature section, the owner's:
        not offered to him (review of 02/10/2026)."""
        self.create()
        text = self.text(self.page())
        self.assertIn("Seul votre employeur peut le rouvrir.", text)
        self.assertNotIn("Corriger ce mois", text)

    def test_he_saves_the_month_grid(self):
        form = form_posting_to(self.html(self.page()), self.url)
        answer = self.send(form, values={"heures-2026-06-05": "9"})
        self.assertLandedOn(answer, self.url)
        self.assertEqual(stored(self.person)[5], ("travail", Decimal("9.00"), ""))
        # What OwnerCase saved is kept.
        self.assertEqual(stored(self.person)[2], ("travail", Decimal("9.00"), "inventaire"))

    def test_sending_for_signature_is_refused_and_nothing_is_sent(self):
        response = self.client.post(
            self.route("staff:signature_send"), {"csrfmiddlewaretoken": self.token(), HAND_OVER: "1"}
        )
        self.assertRefused(response, posted=True)
        self.assertFalse(SignatureRequest.objects.exists())
        self.assertEqual(mail.outbox, [])
        # The month is still his to correct.
        form_posting_to(self.html(self.page()), self.url)

    def test_countersigning_is_refused_and_the_request_waits_for_the_employer(self):
        request, _token = self.create()
        self.employee_signs(request)
        before = self.snapshot()
        response = self.client.post(
            self.route("staff:signature_countersign", 1),
            {"csrfmiddlewaretoken": self.token(), DRAWING: data_url(employer_signature())},
        )
        self.assertRefused(response, posted=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.request_().status, Status.EMPLOYEE_SIGNED)
        self.assertFalse(self.request_().events.filter(kind=SignatureEvent.Kind.COUNTERSIGNED).exists())

    def test_reopening_the_month_is_refused_and_the_request_holds(self):
        self.create()
        before = self.snapshot()
        response = self.client.post(
            self.route("staff:month_reopen"), {"csrfmiddlewaretoken": self.token(), REASON: "Erreur de saisie"}
        )
        self.assertRefused(response, posted=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.request_().status, Status.PENDING)
        # Still read-only: the grid is not drawn.
        self.assertContains(self.page(), "Les jours du mois")

    def test_every_other_signature_action_is_refused(self):
        request, _token = self.create()
        self.employee_signs(request)
        before = self.snapshot()
        token = self.token()
        for route in VERSION_ROUTES:
            with self.subTest(route=route):
                self.assertRefused(self.client.get(self.route(route, 1)))
                self.assertRefused(self.client.post(self.route(route, 1), {"csrfmiddlewaretoken": token}), posted=True)
        self.assertRefused(self.client.get(self.route("staff:signature_file", 1, "original")))
        self.assertEqual(self.snapshot(), before)


class TheOwnerTests(OwnerCase):
    """The suite's client: the espace's owner, as before."""

    def test_the_home_draws_the_access_button(self):
        response = self.get(reverse("staff:home"))
        self.assertContains(response, "Accès des employés au site")
        self.assertContains(response, f'href="{reverse("accounts:members")}"')

    def test_a_saved_month_draws_the_signature_card(self):
        response = self.page()
        content = self.html(response)
        self.assertIn('id="signature"', content)
        self.assertIn("<h2>Signature</h2>", content)
        self.assertIn(self.route("staff:signature_send"), content)

    def test_he_sends_the_month_for_signature(self):
        form = self.form(self.page(), "staff:signature_send")
        response = self.client.post(
            form.action.split("#")[0], as_post(form.submission(values={HAND_OVER: "1"})), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request_().status, Status.PENDING)

    def test_his_month_under_signature_draws_its_actions(self):
        request, _token = self.create()
        self.employee_signs(request)
        content = self.html(self.page())
        self.assertIn('id="signature"', content)
        self.assertIn(self.route("staff:signature_countersign", 1), content)
        self.assertIn(self.route("staff:month_reopen"), content)
