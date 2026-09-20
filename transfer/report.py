"""What an import or a clear did, or will do: one table per run, a row per
entity, and the lists a person reads before confirming.

JSON round-trippable: the preview is kept in the stage's state.json and the
final report in the session, to be shown once after the redirect.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

#: Per list and per section. Beyond it only a count is kept: the page shows
#: 20 anyway, and a replace of 893 invoices must not put 893 lines in a
#: session cookie's database row.
MAX_MESSAGES = 200
#: What the page shows of each list before « … et N autres ».
SHOWN = 20

LISTS = (
    ("conflicts", "Conflits — gardés tels quels"),
    ("skipped", "Ignorés"),
    ("kept", "Gardés — encore utilisés"),
    ("notes", "À savoir"),
)


@dataclass
class Tally:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0


@dataclass
class SectionReport:
    key: str
    label: str
    tallies: dict[str, Tally] = field(default_factory=dict)  # entity label (French plural) → Tally, insertion-ordered
    conflicts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    overflow: dict[str, int] = field(default_factory=dict)  # beyond MAX_MESSAGES = 200 per list

    @classmethod
    def for_key(cls, key: str) -> SectionReport:
        from transfer.registry import INFO

        info = INFO.get(key)
        return cls(key=key, label=info.label if info else key)

    # -- counting ------------------------------------------------------------------
    def _tally(self, what: str) -> Tally:
        if what not in self.tallies:
            self.tallies[what] = Tally()
        return self.tallies[what]

    def created(self, what: str, n: int = 1) -> None:
        self._tally(what).created += n

    def updated(self, what: str, n: int = 1) -> None:
        self._tally(what).updated += n

    def deleted(self, what: str, n: int = 1) -> None:
        self._tally(what).deleted += n

    def unchanged(self, what: str, n: int = 1) -> None:
        self._tally(what).unchanged += n

    # -- saying ---------------------------------------------------------------------
    def _say(self, name: str, text: str) -> None:
        items = getattr(self, name)
        if len(items) < MAX_MESSAGES:
            items.append(text)
        else:
            self.overflow[name] = self.overflow.get(name, 0) + 1

    def conflict(self, text: str) -> None:
        self._say("conflicts", text)

    def skip(self, text: str) -> None:
        self._say("skipped", text)

    def keep(self, text: str) -> None:
        self._say("kept", text)

    def note(self, text: str) -> None:
        self._say("notes", text)

    def note_once(self, text: str) -> None:
        """« champ inconnu ignoré : … » is said once per section and field,
        not once per record."""
        if text not in self.notes:
            self.note(text)

    # -- reading ---------------------------------------------------------------------
    @property
    def changes(self) -> bool:
        """Anything created, updated or deleted."""
        return any(t.created or t.updated or t.deleted for t in self.tallies.values())

    @property
    def destructive(self) -> bool:
        """Anything updated or deleted: what the safety export must hold (§6.5)."""
        return any(t.updated or t.deleted for t in self.tallies.values())

    @property
    def deletes(self) -> bool:
        """Anything deleted - which a « Fusionner » of this section never does
        itself: another section's code did it (the invoices' prune takes the
        bank's payments with it)."""
        return any(t.deleted for t in self.tallies.values())

    @property
    def said(self) -> bool:
        return any(getattr(self, name) for name, _title in LISTS) or bool(self.overflow)

    @property
    def rows(self) -> list[tuple[str, Tally]]:
        return list(self.tallies.items())

    @property
    def display_lists(self) -> list[dict]:
        """Each non-empty list, capped for the page, with how many more."""
        shown = []
        for name, title in LISTS:
            items = getattr(self, name)
            total = len(items) + self.overflow.get(name, 0)
            if total:
                shown.append({"title": title, "items": items[:SHOWN], "more": total - min(len(items), SHOWN), "total": total})
        return shown

    def to_json(self) -> dict:
        return {**self.outcome(), "notes": list(self.notes)}

    def outcome(self) -> dict:
        """What this section's run DOES: how many of each thing, and every
        record it names. Not its `notes`, which also say things about what
        the run leaves alone - « 4 fichiers que plus rien ne cite dans
        media/ restent tels quels » counts stray files, and that count
        changed under the owner between a page's preview and its confirm,
        so every « Effacer » was refused for ever (20/09)."""
        return {
            "key": self.key,
            "label": self.label,
            "tallies": {what: asdict(tally) for what, tally in self.tallies.items()},
            "conflicts": list(self.conflicts),
            "skipped": list(self.skipped),
            "kept": list(self.kept),
            "overflow": dict(self.overflow),
        }

    @classmethod
    def from_json(cls, data) -> SectionReport:
        return cls(
            key=data["key"],
            label=data["label"],
            tallies={what: Tally(**tally) for what, tally in data.get("tallies", {}).items()},
            conflicts=list(data.get("conflicts", [])),
            skipped=list(data.get("skipped", [])),
            kept=list(data.get("kept", [])),
            notes=list(data.get("notes", [])),
            overflow=dict(data.get("overflow", {})),
        )


@dataclass
class RunReport:
    mode: str                                  # "import" | "clear"
    preview: bool
    sections: list[SectionReport]
    rebuilt: dict[str, int]                    # "mouvements de stock", "statuts de factures", "ventes par recette", "produits caisse"
    safety: dict[str, str] | None = None       # {"database": path, "archive": path or ""}
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)  # about the run as a whole

    def affected(self) -> set[str]:
        """Keys whose report is destructive (§6.5), whichever section's code
        did it: clearing invoices deletes the bank's payments, and says so
        in the bank's report."""
        return {section.key for section in self.sections if section.destructive}

    def section(self, key: str) -> SectionReport | None:
        return next((section for section in self.sections if section.key == key), None)

    def outcome(self) -> dict:
        """What the run does, without when or how long: a preview and its
        confirm on the same input have the same outcome. Notes are left out
        (SectionReport.outcome says why): a run is what it changes, and a
        note that counts what it leaves alone made every confirm refuse."""
        return {
            "mode": self.mode,
            "sections": [section.outcome() for section in self.sections],
            "rebuilt": dict(self.rebuilt),
        }

    def same_outcome(self, other: RunReport) -> bool:
        """Whether two runs do the same, compared as JSON: the preview a
        confirm is held to was read back from the stage's state.json or the
        session."""
        return json.loads(json.dumps(self.outcome())) == json.loads(json.dumps(other.outcome()))

    @property
    def fingerprint(self) -> str:
        """A sha256 of the outcome, which a confirm form posts back to name
        the preview it shows (views.SHOWN_PREVIEW): with the stage open in two
        tabs, the preview stored last may be another tab's. Two previews
        announcing the same run have the same one. Keys sorted as
        same_outcome compares them; ASCII JSON, so no text makes it fail."""
        return hashlib.sha256(json.dumps(self.outcome(), sort_keys=True).encode("ascii")).hexdigest()

    @property
    def duration_text(self) -> str:
        """« 10,3 » - with a comma, as the page writes « 422,2 Mo »."""
        return f"{self.duration_s:.1f}".replace(".", ",")

    def to_json(self) -> dict:
        # Its sections' notes too (SectionReport.to_json): kept and shown,
        # only left out of what a preview and its confirm are held to.
        return {
            **self.outcome(),
            "sections": [section.to_json() for section in self.sections],
            "notes": list(self.notes),
            "preview": self.preview,
            "safety": self.safety,
            "duration_s": self.duration_s,
        }

    @classmethod
    def from_json(cls, data) -> RunReport:
        return cls(
            mode=data["mode"],
            preview=bool(data.get("preview")),
            sections=[SectionReport.from_json(section) for section in data.get("sections", [])],
            rebuilt=dict(data.get("rebuilt", {})),
            safety=data.get("safety"),
            duration_s=float(data.get("duration_s", 0.0)),
            notes=list(data.get("notes", [])),
        )
