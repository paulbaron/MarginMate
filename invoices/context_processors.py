def receipt_review_count(request):
    """The nav badge for "Achats": what its "À vérifier" tab holds - the
    tickets still to check, and the documents to fix (undated, or failed to
    import). The same number as the tab, in one query."""
    from .workspace import waiting_counts

    counts = waiting_counts()
    return {"receipt_review_count_nav": counts["tickets"] + counts["to_fix"]}
