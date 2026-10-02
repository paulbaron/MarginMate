def receipt_review_count(request):
    """The nav badge for "Achats": what its "À vérifier" tab holds - the
    tickets still to check, and the documents to fix (undated, or failed to
    import). The same number as the tab, in one query - none on a page of
    Achats, which counted them for that tab already
    (workspace.render_purchases leaves them on the request). Nothing when no
    tenant is bound (multi mode's login, 404 and CSRF pages)."""
    from accounts.tenancy import current_tenant

    from .workspace import waiting_counts

    if current_tenant() is None:
        return {}
    counts = getattr(request, "invoices_waiting", None) or waiting_counts()
    return {"receipt_review_count_nav": counts["tickets"] + counts["to_fix"]}
