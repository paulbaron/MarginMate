"""One module per checkbox, each registering its section with
`@registry.register` when imported.

They are imported by `registry.load_sections()`, on the first question
asked of the registry - not here: every section module imports
`transfer.registry`, which imports `sections.base`, which runs this file
first, so importing them from here would import them half-initialised. And
a module missing (the lanes land one at a time) or broken is skipped there
with a log, so the page still draws the others.
"""


def load() -> None:
    from transfer import registry

    registry.load_sections()
