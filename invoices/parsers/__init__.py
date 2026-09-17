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


def is_ticket_shop(supplier) -> bool:
    """Whether `supplier` is a shop whose products are known by what its
    tickets print: a till configured here, or any supplier with no parser of
    its own for invoices - a small shop added from an unrecognised ticket.
    Metro's or UBA's products are named by their documents: a paper ticket of
    theirs is read, but matched strictly and never renames them."""
    from .receipt_base import ReceiptParser

    if ticket_parser_for(supplier.code) is not None:
        return True
    if supplier.parser_key == LLM_PARSER_KEY:
        return False
    parser = get_parser(supplier.parser_key)
    return parser is None or isinstance(parser, ReceiptParser)


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
    "is_ticket_shop",
    "ticket_parser_for",
]
