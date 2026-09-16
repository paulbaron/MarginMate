from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .cecina import CecinaParser
from .depoivre import DepoivreParser
from .franprix import FranprixParser
from .llm_fallback import LLMFallbackParser
from .metro import MetroParser
from .monoprix import MonoprixParser
from .ploufils import PlouFilsParser
from .registry import PARSER_REGISTRY, get_parser
from .sabbh import SabbhParser
from .uba import UBAParser
from .wingseng import WingSengParser

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
    "InvoiceParser",
    "ParsedInvoice",
    "ParsedLine",
    "PARSER_REGISTRY",
    "get_parser",
    "LLM_PARSER_KEY",
    "ticket_parser_for",
    "MetroParser",
    "UBAParser",
    "CecinaParser",
    "PlouFilsParser",
    "DepoivreParser",
    "LLMFallbackParser",
    "FranprixParser",
    "MonoprixParser",
    "SabbhParser",
    "WingSengParser",
]
