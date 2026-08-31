PARSER_REGISTRY = {}


def register(parser_cls):
    PARSER_REGISTRY[parser_cls.supplier_code] = parser_cls()
    return parser_cls


def get_parser(key: str):
    return PARSER_REGISTRY.get(key)
