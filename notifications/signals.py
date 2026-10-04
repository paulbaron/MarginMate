"""Two moments a browser must stop receiving a login's notifications.

**Another login on a browser that receives notifications.** Permission to
notify belongs to the browser (the site's origin), not to the login: once
someone enabled notifications on a shared PC or tablet, whoever logs in
next would otherwise go on receiving the first login's alerts - or have the
page's sync attach that subscription to the newcomer. So a login whose
browser carries the `marginmate_push` cookie of ANOTHER login's device
deletes that device (notifications/devices.py). The cookie stays; the next
sync answers « unknown » and the newcomer taps « Activer » if he wants
them. A missing table never stops a login.

**A membership moved to another espace or given to another login** (the
admin lets its espace and its login be edited, the shell too): its devices
are deleted before the row is saved. A device serves one login of ONE
espace - kept, the old bar's shared PC would receive the new bar's
reminders and alerts at once, before anyone opens a page there (the
middleware's forced logout comes only then), and a leaver's phone would go
on receiving the bar's alerts meant for the login now holding the row,
with nothing on that phone able to stop them (its cookie names the leaver,
who has no membership there any more). The login taps « Activer » again. A
`QuerySet.update` of the tenant or the user bypasses this, as it bypasses
every save.
"""

import logging

from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import pre_save
from django.dispatch import receiver

from accounts.models import Membership, PushDevice

logger = logging.getLogger(__name__)


@receiver(user_logged_in, dispatch_uid="notifications.forget_another_login_s_device")
def forget_another_login_s_device(sender, request=None, user=None, **kwargs):
    if request is None or user is None:
        return
    from django.db import DatabaseError

    from . import devices

    try:
        devices.forget_another_login_s_device(request, user)
    except DatabaseError:
        logger.warning("Notifications : l'appareil d'une autre connexion n'a pas pu être retiré", exc_info=True)


@receiver(pre_save, sender=Membership, dispatch_uid="notifications.forget_a_moved_membership_s_devices")
def forget_a_moved_membership_s_devices(sender, instance, raw=False, using=None, **kwargs):
    if raw or instance.pk is None:
        return
    stored = Membership.objects.using(using).filter(pk=instance.pk).values_list("tenant_id", "user_id").first()
    if stored is not None and stored != (instance.tenant_id, instance.user_id):
        PushDevice.objects.using(using).filter(membership_id=instance.pk).delete()
