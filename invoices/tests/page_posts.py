"""What the correction page posts back, built from what it rendered - every
field the browser sends, the hidden ones included - with changes applied.
Tests that post a hand-written subset test a request no browser makes."""

from datetime import date


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def page_post(response, **changes) -> dict:
    """The page's own form data, as `response` drew it; `changes` are
    posted names (`invoice_date`, `form-0-total_ttc`) with their values."""
    formset = response.context["formset"]
    header = response.context["header_form"]
    data = {
        f"{formset.prefix}-TOTAL_FORMS": str(len(formset.forms)),
        f"{formset.prefix}-INITIAL_FORMS": "0",
        f"{formset.prefix}-MIN_NUM_FORMS": "0",
        f"{formset.prefix}-MAX_NUM_FORMS": "1000",
    }
    for name in header.fields:
        data[name] = _text(header.initial.get(name))
    for index, form in enumerate(formset.forms):
        for name, field in form.fields.items():
            if name == "DELETE":
                continue
            data[f"{formset.prefix}-{index}-{name}"] = _text(form.initial.get(name, field.initial))
    data.update({key: _text(value) for key, value in changes.items()})
    return data
