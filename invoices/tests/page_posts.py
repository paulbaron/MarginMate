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


def page_post(response, with_vat=False, **changes) -> dict:
    """The page's own form data, as `response` drew it; `changes` are
    posted names (`invoice_date`, `form-0-total_ttc`, `tva-0-rate`) with
    their values.

    `with_vat` posts the VAT table block too, as the browser does. Left out,
    the stored table stays as it was (a page cached before the block
    existed) - which is how a table drawn as its own form refused ("5.500"
    for 5,5 %, on 361 documents) went unseen by every test that saved."""
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
    if with_vat:
        data.update(vat_table_post(response))
    data.update({key: _text(value) for key, value in changes.items()})
    return data


def vat_table_post(response) -> dict:
    """The VAT table block as the page drew it: its management form and
    each row's value as its input holds it (BoundField.value(), the same
    text the widget renders and the browser sends back)."""
    vat_form = response.context["vat_form"]
    management = vat_form.management_form
    data = {management.add_prefix(name): _text(management[name].value()) for name in management.fields}
    for form in vat_form.forms:
        for name in form.fields:
            data[form.add_prefix(name)] = _text(form[name].value())
    return data
