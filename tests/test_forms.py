"""Forms tests: field snapshots, identity matching, presets, Fill&Sign.

Everything runs headless against AcroForm PDFs generated on the fly with
PyMuPDF (``page.add_widget``); no user file is ever read or written. The
matching heuristic is pinned exactly: which fields are proposed, with which
key, value, score and source, and, just as important, which fields are NOT
(checkbox / radio / signature widgets, birth or expiry dates, fax numbers,
and a bare "name" field on a page that already has split first/last fields).
As in test_pdfedit, the fixture bytes on disk must never change.
"""
from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

from figureviewer import docs as D
from figureviewer import forms as F
from figureviewer import pdfedit as E


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path: Path) -> D.Entry:
    stat = path.stat()
    return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)


def _add_widget(page, name: str, ftype, rect, *, label: str = "",
                max_len: int = 0) -> None:
    """Create one widget on ``page``; the caller holds the page proxy."""
    widget = D.fitz.Widget()
    widget.field_name = name
    widget.field_type = ftype
    widget.rect = D.fitz.Rect(*rect)
    if label:
        widget.field_label = label
    if max_len:
        widget.text_maxlen = max_len
    page.add_widget(widget)


def _fake_field(name: str, ftype: str, *, page: int = 0,
                options: tuple[str, ...] = ()) -> F.FieldInfo:
    """A synthetic FieldInfo for widget kinds PyMuPDF cannot create
    (radio buttons raise on add_widget); match_identity is a pure function
    over FieldInfo records, so this is a faithful test input."""
    return F.FieldInfo(page=page, xref=9999, name=name, ftype=ftype,
                       rect_pts=(72.0, 72.0, 200.0, 92.0), value="",
                       options=options)


#: The preset used by every matching test, in the full stored-record shape.
PRESET = {
    "id": "abc123", "label": "Test identity",
    "fields": {
        "first_name": "Ny", "last_name": "Andrianarivelo",
        "full_name": "Ny Andrianarivelo", "email": "ny@example.org",
        "phone": "+33 6 12 34 56 78", "company": "CNRS",
    },
}


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for form tests")
class FormModelTests(unittest.TestCase):
    """has_form / fields snapshots and the pinned matching heuristic."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "form.pdf"
        text = D.fitz.PDF_WIDGET_TYPE_TEXT
        doc = D.fitz.open()
        # Page 0: a classic split-name form plus the traps (birth date,
        # checkbox with an email-ish name, a redundant full-name line).
        page = doc.new_page(width=595, height=842)
        _add_widget(page, "first_name", text, (72, 100, 250, 120))
        _add_widget(page, "last_name", text, (72, 140, 250, 160))
        _add_widget(page, "email", text, (72, 180, 250, 200), max_len=40)
        _add_widget(page, "applicant_name", text, (72, 220, 250, 240))
        _add_widget(page, "birth_date", text, (72, 260, 250, 280))
        _add_widget(page, "date_signed", text, (72, 340, 250, 360))
        _add_widget(page, "agree_email", D.fitz.PDF_WIDGET_TYPE_CHECKBOX,
                    (72, 300, 90, 318))
        # Page 1: no split fields, so full_name is legitimate here; a fax
        # trap; a nameless field whose only hint is the printed label beside
        # it; a tooltip-only field; a signature that must never be proposed.
        page2 = doc.new_page(width=595, height=842)
        _add_widget(page2, "name", text, (200, 100, 400, 120))
        _add_widget(page2, "fax_number", text, (200, 140, 400, 160))
        page2.insert_text((140, 214), "Phone:", fontsize=10)
        _add_widget(page2, "Text7", text, (200, 200, 400, 220))
        _add_widget(page2, "Text9", text, (200, 240, 400, 260),
                    label="Company name")
        _add_widget(page2, "name_signature", D.fitz.PDF_WIDGET_TYPE_SIGNATURE,
                    (200, 300, 400, 340))
        doc.save(str(self.pdf))
        doc.close()
        self.source_hash = _sha(self.pdf)
        self.session = E.EditSession(_entry(self.pdf))
        self.addCleanup(self.session.close)

    def _assert_source_untouched(self) -> None:
        self.assertEqual(_sha(self.pdf), self.source_hash,
                         "the raw file on disk must never change")

    # -- field model --------------------------------------------------------
    def test_has_form_detects_acroforms_and_rejects_plain_pdfs(self) -> None:
        self.assertTrue(F.has_form(self.session))
        plain = self.root / "plain.pdf"
        doc = D.fitz.open()
        doc.new_page(width=595, height=842).insert_text((72, 100), "no form")
        doc.save(str(plain))
        doc.close()
        plain_session = E.EditSession(_entry(plain))
        self.addCleanup(plain_session.close)
        self.assertFalse(F.has_form(plain_session))
        # The raw fitz document path of _resolve must agree with the session.
        raw = D.fitz.open(str(self.pdf))
        try:
            self.assertTrue(F.has_form(raw))
        finally:
            raw.close()
        self._assert_source_untouched()

    def test_fields_snapshots_every_widget_in_page_order(self) -> None:
        snapshot = F.fields(self.session)
        self.assertEqual(
            [(f.name, f.ftype, f.page) for f in snapshot],
            [("first_name", "text", 0), ("last_name", "text", 0),
             ("email", "text", 0), ("applicant_name", "text", 0),
             ("birth_date", "text", 0), ("date_signed", "text", 0),
             ("agree_email", "checkbox", 0),
             ("name", "text", 1), ("fax_number", "text", 1),
             ("Text7", "text", 1), ("Text9", "text", 1),
             ("name_signature", "signature", 1)])
        by_name = {f.name: f for f in snapshot}
        self.assertTrue(all(f.xref > 0 for f in snapshot))
        self.assertEqual(by_name["email"].max_len, 40)
        self.assertIsNone(by_name["first_name"].max_len)
        self.assertEqual(by_name["Text9"].tooltip, "Company name")
        self.assertEqual(by_name["first_name"].value, "")
        self._assert_source_untouched()

    # -- matching heuristic (pinned exactly) --------------------------------
    def test_match_identity_pins_the_exact_proposal_set(self) -> None:
        proposals = F.match_identity(F.fields(self.session), PRESET,
                                     self.session)
        got = {p.field.name: (p.key, p.value, p.score, p.source)
               for p in proposals}
        today = date.today().strftime("%Y-%m-%d")
        self.assertEqual(got, {
            "first_name": ("first_name", "Ny", 2, "name"),
            "last_name": ("last_name", "Andrianarivelo", 2, "name"),
            "email": ("email", "ny@example.org", 2, "name"),
            "date_signed": ("date_today", today, 2, "name"),
            "name": ("full_name", "Ny Andrianarivelo", 2, "name"),
            "Text7": ("phone", "+33 6 12 34 56 78", 2, "nearby"),
            "Text9": ("company", "CNRS", 2, "tooltip"),
        })
        # The traps, spelled out: the page-0 "name" line yields to the split
        # first/last pair, birth dates never receive today's date, fax is not
        # a phone, and checkbox / signature widgets are never proposed.
        for absent in ("applicant_name", "birth_date", "agree_email",
                       "fax_number", "name_signature"):
            self.assertNotIn(absent, got)
        self._assert_source_untouched()

    def test_full_name_does_not_steal_from_split_fields_per_page(self) -> None:
        proposals = F.match_identity(F.fields(self.session), PRESET,
                                     self.session)
        full_name_pages = [p.field.page for p in proposals
                           if p.key == "full_name"]
        self.assertEqual(full_name_pages, [1],
                         "full_name must be dropped on the split page only")

    def test_checkbox_radio_and_signature_are_never_proposed(self) -> None:
        synthetic = [
            _fake_field("phone_choice", "radio"),
            _fake_field("applicant_signature", "signature"),
            _fake_field("email_optin", "checkbox"),
        ]
        self.assertEqual(F.match_identity(synthetic, PRESET), [])

    def test_exact_token_score_beats_substring_score(self) -> None:
        # "emailing_city": email only matches by substring (1) while city
        # matches an exact token (2); city must win although email comes
        # first in FIELD_TOKEN_MAP.
        self.assertEqual(F._best_key(F._tokens("emailing_city")), ("city", 2))
        self.assertEqual(F._best_key(F._tokens("Phone")), ("phone", 2))
        self.assertEqual(F._best_key(F._tokens("telephone")), ("phone", 1))
        # And through the public path: a substring-only field is score 1.
        proposals = F.match_identity([_fake_field("telephone", "text")],
                                     PRESET)
        self.assertEqual([(p.key, p.score) for p in proposals],
                         [("phone", 1)])

    def test_nearby_label_reads_text_left_of_and_above_the_widget(self) -> None:
        text7 = next(f for f in F.fields(self.session) if f.name == "Text7")
        self.assertEqual(F.nearby_label(self.session, text7), "Phone:")

    def test_choice_widgets_only_accept_actual_options(self) -> None:
        combo = _fake_field("country", "combo",
                            options=("France", "Germany"))
        hit = F.match_identity([combo], {"country": "france"})
        self.assertEqual([(p.key, p.value) for p in hit],
                         [("country", "France")],
                         "the canonical export value must be proposed")
        miss = F.match_identity([combo], {"country": "Spain"})
        self.assertEqual(miss, [])

    # -- writing ------------------------------------------------------------
    def test_apply_proposals_is_one_undo_entry_that_round_trips(self) -> None:
        proposals = F.match_identity(F.fields(self.session), PRESET,
                                     self.session)
        count = F.apply_proposals(self.session, proposals)
        self.assertEqual(count, 7)
        self.assertEqual(len(self.session._history.undo), 1,
                         "auto-fill must collapse into one history entry")
        self.assertEqual(self.session.undo_label, "Auto-fill (7 fields)")
        filled = {f.name: f.value for f in F.fields(self.session)}
        self.assertEqual(filled["first_name"], "Ny")
        self.assertEqual(filled["name"], "Ny Andrianarivelo")
        self.assertEqual(filled["Text7"], "+33 6 12 34 56 78")
        self.session.undo()
        self.assertFalse(self.session.can_undo)
        cleared = {f.name: f.value for f in F.fields(self.session)
                   if f.ftype == "text"}
        self.assertEqual(set(cleared.values()), {""},
                         "one undo must clear every auto-filled field")
        self.session.redo()
        refilled = {f.name: f.value for f in F.fields(self.session)}
        self.assertEqual(refilled["last_name"], "Andrianarivelo")
        self.assertEqual(F.apply_proposals(self.session, []), 0)
        self.assertEqual(len(self.session._history.undo), 1,
                         "an empty apply must not push a history entry")
        self._assert_source_untouched()

    def test_set_value_fill_survives_save_bytes_and_reopen(self) -> None:
        proposals = F.match_identity(F.fields(self.session), PRESET,
                                     self.session)
        F.apply_proposals(self.session, proposals)
        email = next(f for f in F.fields(self.session) if f.name == "email")
        F.set_value(self.session, email, "override@example.org")
        self.assertEqual(self.session.undo_label, "Fill email")
        reopened = D.fitz.open(stream=self.session.save_bytes(),
                               filetype="pdf")
        try:
            saved = {}
            for page in reopened:      # the proxy is held per iteration
                for widget in page.widgets():
                    saved[widget.field_name] = widget.field_value
        finally:
            reopened.close()
        self.assertEqual(saved["email"], "override@example.org")
        self.assertEqual(saved["first_name"], "Ny")
        self.assertEqual(saved["name"], "Ny Andrianarivelo")
        self.assertEqual(saved["Text7"], "+33 6 12 34 56 78")
        self.assertEqual(saved["applicant_name"], "")
        self.assertEqual(saved["birth_date"], "")
        self._assert_source_untouched()


class IdentityStoreTests(unittest.TestCase):
    """Preset CRUD with plain dependencies; every mutation persists at once."""

    def setUp(self) -> None:
        self.saves: list[dict] = []
        self.settings: dict = {"identity_presets": "corrupt",
                               "active_identity": 7}
        self.store = F.IdentityStore(
            self.settings, lambda s: self.saves.append(copy.deepcopy(s)))

    def test_construction_sanitises_without_saving(self) -> None:
        self.assertEqual(self.settings["identity_presets"], [])
        self.assertEqual(self.settings["active_identity"], "")
        self.assertEqual(self.saves, [],
                         "sanitising hand-edited JSON is not a user mutation")

    def test_add_persists_immediately_and_activates_the_preset(self) -> None:
        preset_id = self.store.add("Work", {"first_name": "Ny", "zip": 75005})
        self.assertEqual(len(self.saves), 1)
        self.assertEqual(self.settings["active_identity"], preset_id)
        stored = self.saves[0]["identity_presets"][0]
        self.assertEqual(stored["label"], "Work")
        self.assertEqual(stored["fields"], {"first_name": "Ny",
                                            "zip": "75005"},
                         "values are coerced to strings for stable JSON")
        active = self.store.active()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], preset_id)

    def test_update_and_set_active_persist_and_refuse_unknown_ids(self) -> None:
        preset_id = self.store.add("Work", {"email": "a@b.c"})
        self.assertTrue(self.store.update(preset_id, label="Perso"))
        self.assertEqual(len(self.saves), 2)
        self.assertEqual(self.store.get(preset_id)["label"], "Perso")
        self.assertFalse(self.store.update("missing", label="x"))
        self.assertFalse(self.store.set_active("missing"))
        self.assertEqual(len(self.saves), 2,
                         "refused mutations must not trigger a save")
        self.assertTrue(self.store.set_active(""))
        self.assertIsNone(self.store.active())
        self.assertTrue(self.store.set_active(preset_id))
        self.assertEqual(len(self.saves), 4)

    def test_remove_clears_the_active_selection_and_persists(self) -> None:
        keep = self.store.add("Keep", {})
        gone = self.store.add("Gone", {})
        self.assertEqual(self.settings["active_identity"], gone)
        self.assertTrue(self.store.remove(gone))
        self.assertEqual(self.settings["active_identity"], "")
        self.assertEqual([p["id"] for p in self.store.presets()], [keep])
        self.assertFalse(self.store.remove(gone))
        self.assertEqual(len(self.saves), 3)

    def test_reads_hand_out_copies_never_live_records(self) -> None:
        preset_id = self.store.add("Work", {"city": "Paris"})
        leaked = self.store.get(preset_id)
        leaked["fields"]["city"] = "Bordeaux"
        leaked["label"] = "tampered"
        self.assertEqual(self.store.get(preset_id)["fields"]["city"], "Paris")
        self.assertEqual(self.store.presets()[0]["label"], "Work")


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is required for Fill&Sign tests")
class FillSignTests(unittest.TestCase):
    """Free placement on flat PDFs: borderless text and ink check glyphs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pdf = self.root / "flat.pdf"
        doc = D.fitz.open()
        doc.new_page(width=595, height=842).insert_text(
            (72, 100), "A flat scan-like page with no form fields")
        doc.save(str(self.pdf))
        doc.close()
        self.source_hash = _sha(self.pdf)
        self.session = E.EditSession(_entry(self.pdf))
        self.addCleanup(self.session.close)

    def _assert_source_untouched(self) -> None:
        self.assertEqual(_sha(self.pdf), self.source_hash,
                         "the raw file on disk must never change")

    def test_place_text_drops_an_undoable_freetext_at_the_point(self) -> None:
        uid = F.place_text(self.session, 0, (100.0, 600.0),
                           "Ny Andrianarivelo", size=12.0)
        annots = self.session.annots_on(0)
        self.assertEqual([(a.kind, a.uid) for a in annots],
                         [(E.TYPE_FREETEXT, uid)])
        rect = annots[0].rect
        self.assertAlmostEqual(rect[0], 100.0, delta=2.0)
        self.assertAlmostEqual(rect[1], 600.0, delta=2.0)
        self.assertGreater(rect[2] - rect[0], 12.0,
                           "the rect must be sized to the text, never zero")
        self.session.undo()
        self.assertEqual(self.session.annots_on(0), [])
        self._assert_source_untouched()

    def test_add_check_draws_a_centred_ink_glyph_per_kind(self) -> None:
        centre = (300.0, 400.0)
        size = 14.0
        for kind in sorted(F.CHECK_GLYPHS):
            with self.subTest(kind=kind):
                uid = F.add_check(self.session, 0, centre, kind, size=size)
                annot = next(a for a in self.session.annots_on(0)
                             if a.uid == uid)
                self.assertEqual(annot.kind, E.TYPE_INK)
                # The annot RECT carries PyMuPDF's appearance padding, so the
                # pinned contract is the stroke geometry itself: every vertex
                # inside the size box, and the stroke bbox centred on the
                # point (every glyph in CHECK_GLYPHS is symmetric).
                with self.session.document._lock:
                    page_proxy, live = self.session._locate(0, uid)
                    points = [p for stroke in live.vertices for p in stroke]
                    del live, page_proxy
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                self.assertAlmostEqual((min(xs) + max(xs)) / 2.0, centre[0],
                                       delta=0.1)
                self.assertAlmostEqual((min(ys) + max(ys)) / 2.0, centre[1],
                                       delta=0.1)
                self.assertLessEqual(max(xs) - min(xs), size + 0.1)
                self.assertLessEqual(max(ys) - min(ys), size + 0.1)
        self.assertEqual(len(self.session._history.undo),
                         len(F.CHECK_GLYPHS),
                         "each glyph is its own undoable command")
        while self.session.can_undo:
            self.session.undo()
        self.assertEqual(self.session.annots_on(0), [])
        self._assert_source_untouched()

    def test_add_check_rejects_unknown_glyph_kinds(self) -> None:
        with self.assertRaises(ValueError):
            F.add_check(self.session, 0, (100.0, 100.0), "tick")
        self.assertFalse(self.session.can_undo)


if __name__ == "__main__":
    unittest.main()
