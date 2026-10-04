from django.apps import AppConfig


class ReturnablesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "returnables"
    # « Consignes »: the empties handed back to the delivery driver - the
    # word on the link. Not the article category « Consignes » of Produits &
    # charges, nor the Dépenses one: the page's subtitle says which.
    verbose_name = "Consignes"

    def ready(self):
        # pdfminer's decoders, what its interpreter runs, the glyphs a page
        # draws, the codes a font maps and what its parser reads, bounded for
        # the whole process from the start (reading.bound_pdf_decoding,
        # bound_pdf_interpreting, bound_pdf_glyphs, bound_pdf_cmaps,
        # bound_pdf_parsing), not from the first import of
        # the returnables code: a management command reading Achats' PDFs
        # gets the same bounds as the web process.
        from returnables import reading

        reading.bound_pdf_decoding()
        reading.bound_pdf_interpreting()
        reading.bound_pdf_glyphs()
        reading.bound_pdf_cmaps()
        reading.bound_pdf_parsing()
