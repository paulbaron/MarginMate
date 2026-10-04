from .base import InvoiceParser, ParsedInvoice, ParsedLine
from .cecina import CecinaParser
from .generic_receipt import GenericReceiptParser, TicketShop
from .metro import MetroParser
from .registry import PARSER_REGISTRY, get_parser
from .uba import UBAParser


def is_ticket_shop(supplier) -> bool:
    """Whether `supplier` is a shop whose products are known by what its
    tickets print: a till configured here, or any supplier with no parser of
    its own for invoices - a small shop added from an unrecognised ticket.
    Metro's or UBA's products are named by their documents: a paper ticket of
    theirs is read, but matched strictly and never renames them."""
    from .receipt_base import ReceiptParser

    if ticket_parser_for(supplier.code) is not None:
        return True
    parser = get_parser(supplier.parser_key)
    return parser is None or isinstance(parser, ReceiptParser)


GENERIC_READER_CHOICE = ("", "— Lecteur générique —")


def layout_readers() -> dict:
    """The readers of a PDF layout a source may choose (`InvoiceType.
    parser_key`): the registry but the tills - a till is keyed on its
    supplier's code, its settings are one shop's tickets."""
    from .receipt_base import ReceiptParser

    return {key: parser for key, parser in PARSER_REGISTRY.items() if not isinstance(parser, ReceiptParser)}


def reader_label(key: str) -> str:
    """A reader's name on screen: its `label`, else its key - a reader
    without one, or a key no reader answers to any more."""
    return getattr(get_parser(key), "label", "") or key


def reader_choices(current: str = "") -> list[tuple[str, str]]:
    """The « Lecteur » choices of a source: the generic reader, then the
    layout readers by name - plus `current`, a key saved before (a till's,
    one since removed), so that no saved source becomes invalid."""
    readers = sorted(((key, reader_label(key)) for key in layout_readers()), key=lambda choice: choice[1].casefold())
    if current and current not in dict(readers):
        readers.append((current, reader_label(current)))
    return [GENERIC_READER_CHOICE, *readers]


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
    "GENERIC_READER_CHOICE",
    "PARSER_REGISTRY",
    "CecinaParser",
    "GenericReceiptParser",
    "InvoiceParser",
    "MetroParser",
    "ParsedInvoice",
    "ParsedLine",
    "TicketShop",
    "UBAParser",
    "get_parser",
    "is_ticket_shop",
    "layout_readers",
    "reader_choices",
    "reader_label",
    "ticket_parser_for",
]
