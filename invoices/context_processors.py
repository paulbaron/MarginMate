def receipt_review_count(request):
    """The nav badge for tickets still waiting to be checked.

    Counts only photographed receipts (those carry `parse_checks`); a
    digital invoice never enters this queue, and including one would make
    the badge a number nothing on the Tickets page accounts for.
    """
    from .receipts import pending_receipts

    return {"receipt_review_count_nav": pending_receipts().count()}
