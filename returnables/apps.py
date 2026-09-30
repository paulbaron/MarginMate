from django.apps import AppConfig


class ReturnablesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "returnables"
    # « Consignes »: the empties handed back to the delivery driver - the
    # word on the link. Not the article category « Consignes » of Produits &
    # charges, nor the Dépenses one: the page's subtitle says which.
    verbose_name = "Consignes"

    def ready(self):
        # pdfminer's decoders bounded for the whole process from the start
        # (reading.bound_pdf_decoding), not from the first import of the
        # returnables code: a management command reading Achats' PDFs gets the
        # same bound as the web process.
        from returnables import reading

        reading.bound_pdf_decoding()
