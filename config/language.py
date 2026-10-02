"""`ActiveLanguageMiddleware`: every request runs with its language ACTIVE -
the language it already had, so not a character of any page changes.

LANGUAGE_CODE is en-us and nothing activates a language per request (no
LocaleMiddleware: the pages are French by their own words, the numbers and
dates by their own filters). With no language active, Django's
`get_language()` answers LANGUAGE_CODE only after a failed lookup in the
thread's translation slot - an exception caught each time - and a page
asks it once for every number it prints, every date and every `{% url %}`
(three times there): thousands of times on a big page. Active, it is
answered from the slot, in about half the time (2.5 µs against 4.3 µs,
measured 01/10/2026). The same translation object either
way (`translation(LANGUAGE_CODE)` is what Django falls back to), and
`translation.override` - the French messages of accounts.pages and
transfer - nests and restores as before (it already left the language
active behind it).
"""

from django.utils import translation


class ActiveLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with translation.override(translation.get_language()):
            return self.get_response(request)
