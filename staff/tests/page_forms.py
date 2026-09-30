"""The forms of a rendered page, submitted the way a browser submits them.

A request written by hand tests the view, not the page: a day's field
misnamed in the template, a `<select>` drawn with nothing selected, a CSRF
token missing - the hand-written request goes on passing and the owner's
click does nothing, or the wrong thing (CLAUDE.md « Formsets: test what the
browser actually posts »). So the staff pages' tests read every request OFF
THE RENDERED HTML, following a browser's rules:

* a control belongs to the `<form>` around it (these pages use no `form=`
  attribute, and `forms_of` refuses one rather than guess);
* a disabled control, an unticked box and a button not pressed send nothing;
* a `<select>` sends its selected option - the first one when none is
  marked - and a changed value must be one of its options, since a browser
  can send nothing else; a `<textarea>` sends its text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

SUBMIT_TYPES = ("submit", "image")
IGNORED_TYPES = ("button", "reset", "file")


@dataclass
class Control:
    tag: str
    attrs: dict
    options: list = field(default_factory=list)  # (value, selected) for a <select>
    text: str = ""  # a <textarea>'s content

    @property
    def name(self) -> str | None:
        return self.attrs.get("name")

    @property
    def kind(self) -> str:
        if self.tag == "button":
            return self.attrs.get("type", "submit")
        if self.tag == "input":
            return self.attrs.get("type", "text")
        return self.tag

    @property
    def disabled(self) -> bool:
        return "disabled" in self.attrs

    @property
    def value(self) -> str:
        if self.tag == "select":
            for value, selected in self.options:
                if selected:
                    return value
            return self.options[0][0] if self.options else ""
        if self.tag == "textarea":
            # The HTML parser drops the one newline that follows <textarea>.
            return self.text.removeprefix("\n")
        return self.attrs.get("value", "on" if self.kind in ("checkbox", "radio") else "")


@dataclass
class Form:
    attrs: dict
    controls: list = field(default_factory=list)

    @property
    def action(self) -> str:
        return self.attrs.get("action", "")

    @property
    def method(self) -> str:
        return self.attrs.get("method", "get").lower()

    def control(self, name: str) -> Control:
        found = [control for control in self.controls if control.name == name]
        assert len(found) == 1, f"{len(found)} controls named {name!r} in the form posting to {self.action!r}"
        return found[0]

    @property
    def names(self) -> list[str]:
        return [control.name for control in self.controls if control.name]

    def buttons(self) -> list[Control]:
        return [control for control in self.controls if control.kind in SUBMIT_TYPES and not control.disabled]

    def submission(self, *, press: tuple[str, str] | None = None, values: dict | None = None) -> list[tuple[str, str]]:
        """What the browser sends when the button `press` (its name and
        value) is clicked - the form's first button when None - with the
        fields named in `values` changed first."""
        values = dict(values or {})
        missing = set(values) - set(self.names)
        assert not missing, f"no such field in the form posting to {self.action!r}: {sorted(missing)}"
        buttons = self.buttons()
        assert buttons, f"no enabled button in the form posting to {self.action!r}"
        if press is None:
            clicked = buttons[0]
        else:
            matching = [button for button in buttons if (button.name, button.attrs.get("value", "")) == press]
            assert matching, f"no enabled button {press!r} in the form posting to {self.action!r}"
            clicked = matching[0]
        pairs = []
        for control in self.controls:
            if control.disabled or control.kind in IGNORED_TYPES:
                continue
            if control.kind in SUBMIT_TYPES:
                if control is clicked and control.name:
                    pairs.append((control.name, control.attrs.get("value", "")))
                continue
            if not control.name:
                continue
            if control.kind in ("checkbox", "radio"):
                checked = values.pop(control.name, "checked" in control.attrs)
                if checked:
                    pairs.append((control.name, control.value))
                continue
            value = values.get(control.name, control.value)
            if control.tag == "select":
                offered = [option for option, _selected in control.options]
                assert value in offered, f"{control.name!r} offers no option {value!r}: {offered}"
            pairs.append((control.name, value))
        return pairs


def as_post(pairs) -> dict[str, list[str]]:
    data: dict[str, list[str]] = {}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    return data


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._form: Form | None = None
        self._select: Control | None = None
        self._option: list | None = None  # [value or None, selected, text]
        self._textarea: Control | None = None

    def handle_starttag(self, tag, attrs):
        attributes = {name: ("" if value is None else value) for name, value in attrs}
        if tag == "form":
            self._form = Form(attributes)
            self.forms.append(self._form)
            return
        if tag == "option" and self._select is not None:
            self._close_option()
            self._option = [attributes.get("value"), "selected" in attributes, ""]
            return
        if tag not in ("input", "button", "select", "textarea"):
            return
        assert "form" not in attributes, "a control tied to a form by its `form` attribute: teach page_forms"
        control = Control(tag, attributes)
        if self._form is not None:
            self._form.controls.append(control)
        if tag == "select":
            self._select = control
        elif tag == "textarea":
            self._textarea = control

    def handle_data(self, data):
        if self._option is not None:
            self._option[2] += data
        elif self._textarea is not None:
            self._textarea.text += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "option":
            self._close_option()
        elif tag == "select":
            self._close_option()
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def _close_option(self):
        if self._option is not None and self._select is not None:
            value, selected, text = self._option
            self._select.options.append((text.strip() if value is None else value, selected))
        self._option = None


def forms_of(html: str) -> list[Form]:
    parser = _Forms()
    parser.feed(html)
    parser.close()
    return parser.forms


#: The topbar's « Se déconnecter » (templates/base.html): on every page of a
#: logged-in owner, and none of the page's own forms.
LOGOUT_ACTION = "/deconnexion/"


def page_forms_of(html: str) -> list[Form]:
    """The page's own forms: `forms_of` without the topbar's logout form -
    which must be there, once, on every page an owner is logged in to."""
    forms = forms_of(html)
    logout = [form for form in forms if form.action == LOGOUT_ACTION]
    assert len(logout) == 1, f"{len(logout)} logout forms in the topbar"
    return [form for form in forms if form.action != LOGOUT_ACTION]


def form_posting_to(html: str, action: str, *, method: str = "post", holding: tuple[str, str] | None = None) -> Form:
    """The one form of the page sending to `action` (its fragment aside) -
    and, when two do, the one `holding` a control of that name and value
    (the list page's two forms both post to it, each saying which it is)."""
    found = [form for form in forms_of(html) if form.method == method and form.action.split("#")[0] == action]
    if holding is not None:
        found = [
            form
            for form in found
            if any((control.name, control.attrs.get("value", "")) == holding for control in form.controls)
        ]
    assert len(found) == 1, f"{len(found)} {method.upper()} forms sending to {action!r} holding {holding!r}"
    return found[0]
