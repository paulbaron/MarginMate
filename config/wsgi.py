"""The WSGI application of every server but the production one.

`manage.py runserver` serves it (settings.WSGI_APPLICATION), and so would
any other WSGI server pointed at config.wsgi. `manage.py serve`, the
production server, builds a handler of its own
(accounts/management/commands/serve.py) and never comes through here.

So a request that came through the Cloudflare Tunnel is REFUSED here: a
503 with one French sentence, nothing internal (review PROD-1). The tunnel
is serve's alone - serve checked the settings, the key, the hosts, HTTPS
and the migrations before it listened, and it answers behind Waitress's
proxy handling (the visitor's own address, https). The tunnel's service
URL is serve's port, 8765, which no development tool uses by default
(runserver's is 8000, the port both .claude/launch.json previews use too);
this is the second lock, for the day a development server stands on the
tunnel's port anyway, or the tunnel is pointed at one. That published
runserver: under DEBUG, Django's technical page for the public Host it did
not list - the settings and request.META, the .env's addresses among
them; without DEBUG, every visitor at 127.0.0.1, one address for the login
limiter to lock out and for the signature proofs to record.

What tells a tunnel's request apart is what Cloudflare's edge adds to every
request it forwards (`TUNNEL_HEADERS`), which cloudflared hands on; a
browser on the PC sends none of them. A visitor cannot open this lock: a
header added is a refusal, never a pass. The console of the server refusing
says so, once.
"""

import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

#: What Cloudflare adds to every request it forwards to the tunnel, as a
#: WSGI environ names it: CF-Ray, CF-Connecting-IP, CF-Visitor, CDN-Loop.
TUNNEL_HEADERS = ("HTTP_CF_RAY", "HTTP_CF_CONNECTING_IP", "HTTP_CF_VISITOR", "HTTP_CDN_LOOP")
#: What a visitor reads: nothing about this machine.
REFUSAL = "MarginMate n'est pas en service pour le moment. Réessayez dans quelques minutes.\n"

logger = logging.getLogger(__name__)
_said = False


def came_through_the_tunnel(environ) -> bool:
    return any(name in environ for name in TUNNEL_HEADERS)


def _say_it_once() -> None:
    """The first refusal of this process, in its console: a published
    development server is a mistake the owner must hear about."""
    global _said
    if _said:
        return
    _said = True
    from accounts.management.commands.serve import DEFAULT_PORT, HOST

    logger.warning(
        "Une requête arrivée par le tunnel Cloudflare a été refusée (503) : ce serveur n'est pas le serveur de "
        "production. Le tunnel doit viser http://%s:%s, où tourne start_production.cmd (« manage.py serve »). "
        "Les suivantes ne sont plus signalées.",
        HOST,
        DEFAULT_PORT,
    )


def refuse_the_tunnel(application):
    """`application`, answering 503 to whatever came through the tunnel
    and passing everything else on untouched."""

    def guarded(environ, start_response):
        if came_through_the_tunnel(environ):
            _say_it_once()
            body = REFUSAL.encode("utf-8")
            start_response(
                "503 Service Unavailable",
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
            return [body]
        return application(environ, start_response)

    guarded.wrapped = application
    return guarded


application = refuse_the_tunnel(get_wsgi_application())
