def receipt_review_count(request):
    """The nav badge for "Achats": what its "À vérifier" tab holds - the
    tickets still to check, and the documents to fix (undated, or failed to
    import). The same number as the tab, in one query. Nothing when no
    espace is bound (multi mode's login, 404 and CSRF pages)."""
    from accounts.tenancy import current_tenant

    from .workspace import waiting_counts

    if current_tenant() is None:
        return {}
    counts = waiting_counts()
    return {"receipt_review_count_nav": counts["tickets"] + counts["to_fix"]}
