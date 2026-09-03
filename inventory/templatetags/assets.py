"""`{% asset %}` - like `{% static %}`, but the browser notices changes.

Editing marginmate.css or datatable.js and seeing absolutely nothing happen
is a genuinely nasty way to lose half an hour: the page keeps using a cached
copy, so the change looks like it didn't work rather than like it didn't
load. It has already cost that twice here.

Appending the file's modification time makes each edit a new URL, so the
browser fetches it. In production it's equally correct - a deploy changes the
mtime, so nobody is served yesterday's stylesheet against today's markup.

Lives in `inventory` only because a template tag has to live in some app;
nothing about it is inventory-specific.
"""

import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def asset(path: str) -> str:
    url = static(path)
    try:
        absolute = finders.find(path)
        if absolute:
            return f"{url}?v={int(os.path.getmtime(absolute))}"
    except (OSError, ValueError):
        pass
    # Missing file, or a storage backend with no path on disk: still give a
    # usable URL rather than breaking the page over a cache hint.
    return url
