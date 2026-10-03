def receipt_review_count(request):
    """The nav badge for "Achats": what its "À vérifier" tab holds - the
    tickets still to check, and the documents to fix (undated, or failed to
    import). The same number as the tab, in one query - none on a page of
    Achats, which counted them for that tab already
    (workspace.render_purchases leaves them on the request). Nothing when no
    tenant is bound (multi mode's login, 404 and CSRF pages), nor for an
    employee who does not open « Factures » (accounts/access.py): the
    tickets to check are not his, nor their number."""
    from accounts.access import access_of
    from accounts.tenancy import current_tenant

    from .workspace import waiting_counts

    if current_tenant() is None or not access_of(request).allows("invoices"):
        return {}
    counts = getattr(request, "invoices_waiting", None) or waiting_counts()
    return {"receipt_review_count_nav": counts["tickets"] + counts["to_fix"]}
