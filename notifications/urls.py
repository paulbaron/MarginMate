"""The notifications' addresses, written in full: the app is included once
at the root (config/urls.py), because the service worker must be /sw.js -
served from a folder, its scope could not be the whole site - and the
manifest beside it.

The event's segment is a slug: the registry's keys are slug-safe
(« returnables-comparison »), so the URL sweeps' samples already cover it.
"""

from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("notifications/", views.home, name="home"),
    path("notifications/rappels/", views.reminders, name="reminders"),
    path("notifications/rappels/nuit/", views.night, name="night"),
    path("notifications/rappels/<int:pk>/", views.reminder_edit, name="reminder_edit"),
    path("notifications/rappels/<int:pk>/supprimer/", views.reminder_delete, name="reminder_delete"),
    path("notifications/evenements/", views.events, name="events"),
    path("notifications/evenements/<slug:event>/", views.event_edit, name="event_edit"),
    path("notifications/cle/", views.key, name="key"),
    path("notifications/appareils/inscrire/", views.subscribe, name="subscribe"),
    path("notifications/appareils/synchroniser/", views.sync, name="sync"),
    path("notifications/appareils/<int:pk>/retirer/", views.device_delete, name="device_delete"),
    path("notifications/essai/", views.trial, name="test"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
]
