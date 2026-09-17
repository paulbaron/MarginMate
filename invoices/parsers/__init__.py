from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .cecina import CecinaParser
from .depoivre import DepoivreParser
from .generic_receipt import GenericReceiptParser, TicketShop
from .llm_fallback import LLMFallbackParser
from .metro import MetroParser
from .ploufils import PlouFilsParser
from .registry import PARSER_REGISTRY, get_parser
from .uba import UBAParser

# The fallback "supplier" for documents nobody has a parser for. Its reader is
# a language model, which nothing is sent to by default and a ticket never.
LLM_PARSER_KEY = "LLM"


def ticket_parser_for(supplier_code: str):
    """The photographed-ticket reader for a supplier's tills, or None - Metro
    and UBA send digital invoices and have none."""
    from .receipt_base import ReceiptParser

    return next(
        (
            parser
            for parser in PARSER_REGISTRY.values()
            if isinstance(parser, ReceiptParser) and parser.supplier_code == supplier_code
        ),
        None,
    )

__all__ = [
    "LLM_PARSER_KEY",
    "PARSER_REGISTRY",
    "CecinaParser",
    "DepoivreParser",
    "GenericReceiptParser",
    "InvoiceParser",
    "LLMFallbackParser",
    "MetroParser",
    "ParsedInvoice",
    "ParsedLine",
    "PlouFilsParser",
    "TicketShop",
    "UBAParser",
    "get_parser",
    "ticket_parser_for",
]
