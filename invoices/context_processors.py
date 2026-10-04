def receipt_review_count(request):
    """The nav badge for "Achats": what its "À vérifier" tab holds - the
    tickets still to check, and the documents to fix (undated, or failed to
    import). The same number as the tab, in one query - none on a page of
    Achats, which counted them for that tab already
    (workspace.render_purchases leaves them on the request). Nothing when no
    tenant is bound (multi mode's login, 404 and CSRF pages), nor for an
    employee who does not open « Factures » (accounts/access.py): the
    tickets to check are not his, nor their number. Counted when base.html
    draws it (config.navigation.counted_when_drawn): a job's card polled
    every second paid that scan for nothing."""
    from accounts.access import access_of
    from accounts.tenancy import current_tenant
    from config.navigation import counted_when_drawn

    from . import workspace

    if current_tenant() is None or not access_of(request).allows("invoices"):
        return {}

    def count():
        counts = getattr(request, "invoices_waiting", None) or workspace.waiting_counts()
        return counts["tickets"] + counts["to_fix"]

    return {"receipt_review_count_nav": counted_when_drawn(count)}
