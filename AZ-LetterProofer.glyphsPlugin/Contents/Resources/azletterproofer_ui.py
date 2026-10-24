"""AZ Letter Proofer window: vanilla UI and all Glyphs-side glue.

The scoring itself lives in azletterproofer_engine; this module gathers the
controls into a SearchConfig, feeds the engine the font's available
characters, and sends the results to an Edit tab.
"""

from __future__ import annotations

import contextlib
import random
import traceback
import unicodedata
from itertools import zip_longest
from pathlib import Path

import azletterproofer_dice as dice
from AppKit import (
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSAttributedString,
    NSBoxCustom,
    NSColor,
    NSCompositingOperationSourceAtop,
    NSCompositingOperationSourceOver,
    NSControlSizeMini,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSEvent,
    NSEventMaskKeyDown,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSFloatingWindowLevel,
    NSFont,
    NSFontAttributeName,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageSymbolConfiguration,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSNoTitle,
    NSPopUpButtonWillPopUpNotification,
    NSRectFillUsingOperation,
    NSSwitch,
    NSText,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSZeroRect,
)
from azletterproofer_engine import (
    Case,
    Position,
    RatingsDatabase,
    ScoreWeights,
    SearchConfig,
    WordSource,
    case_of,
    chars_for_tag,
    find_words,
    fold_char,
    font_tag_titles,
    script_of,
    unicode_char,
)
from azletterproofer_settings import (
    PREFS,
    hex_to_rgb,
    register_defaults,
    save_dice_color,
    sync_pane,
)
from Foundation import (
    NSNotificationCenter,
    NSOperationQueue,
    NSProcessInfo,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
)
from GlyphsApp import (
    UPDATEINTERFACE,
    GetOpenFile,
    Glyphs,
    Message,
)
from vanilla import (
    Box,
    Button,
    CheckBoxListCell,
    ColorWell,
    EditText,
    Group,
    ImageButton,
    List,
    Popover,
    PopUpButton,
    SegmentedButton,
    Slider,
    Stepper,
    TextBox,
    Window,
)
from vanilla.vanillaBase import VanillaBaseObject, VanillaCallbackWrapper


def _sync_settings_pane():
    """Mirror a window-side dice change into the AZ Toolkit Settings pane."""
    with contextlib.suppress(Exception):
        sync_pane()


def kill_focus_ring(view):
    """NSFocusRingTypeNone — the ring adds noise on these small controls."""
    view.setFocusRingType_(1)


class Switch(VanillaBaseObject):
    """A mini on/off NSSwitch (vanilla ships none). get()/set() take a bool,
    like a CheckBox; the window reads get() when it needs the state."""

    nsClass = NSSwitch

    def __init__(self, posSize: tuple) -> None:
        self._setupView(self.nsClass, posSize)
        kill_focus_ring(self._nsObject)
        self._nsObject.setControlSize_(NSControlSizeMini)
        self._nsObject.sizeToFit()

    def set(self, value: object) -> None:
        self._nsObject.setState_(NSControlStateValueOn if value else NSControlStateValueOff)

    def get(self) -> bool:
        return self._nsObject.state() == NSControlStateValueOn


DATA_DIR = Path(__file__).parent / "data"
ASSETS_DIR = Path(__file__).parent / "assets"

TABLE_LAST_COLUMN_AUTORESIZE = 4  # NSTableViewLastColumnOnlyAutoresizingStyle
TABLE_STYLE_PLAIN = 4  # NSTableViewStylePlain: flat, edge-to-edge rows
DICE_ICON_BOX = 13  # freeze-button icon size in pt — baked to this exact box
SYMBOL_WEIGHT_REGULAR = 5  # NSImageSymbolWeightRegular
NS_IMAGE_SCALE_NONE = 2  # draw the icon at its own size, never stretched to the button
LIST_ROW_HEIGHT = 18  # fits the unified small system font

SLIDER_NAMES = (
    "diversity",
    "letter_frequency",
    "mark_frequency",
    "texture",
)
# The rarer toggles, folded under the "Extra settings" header row.
EXTRA_SETTINGS = (
    "one_per_line_label",
    "one_per_line",
    "current_tab_label",
    "current_tab",
    "unique_label",
    "unique",
    "all_letters_label",
    "all_letters",
)
MAX_PROOF_LETTERS = 26  # distinct glyphs taken from a selection (select-all stays sane)
DICE_HEIGHT = 169  # dice card height (CARD_MARGIN on every side)
CARD_RADIUS = 10  # rounded corners on the dice + settings cards
CARD_PAD = 10  # inner padding between a card's edge and its content
CARD_GAP = 6  # gap between stacked cards, the suite's AZ_CARD_GAP
WINDOW_WIDTH = 240  # single column throughout, so it also fits the G4 sidebar via AZ Docker
# Word lists beside the bundled languages: the macOS dictionary (off until
# ticked) and one of the user's own (see add_wordlist).
MAC_DICTIONARY = "macOS Dictionary"
MAC_DICTIONARY_PATH = Path("/usr/share/dict/words")
LANG_LIST_HEIGHT = 150  # Languages list sits full-width under the settings

# Layout metrics, shared by the floating window and the docked sidebar.
CARD_MARGIN = 10
BUTTON_BEZEL = 6  # vanilla grows a Button's painted frame by this per side
# A Box grows too (measured: x -3 / width +6, top -2 / height +6); a Group doesn't.
BOX_BEZEL_X = 3
BOX_BEZEL_TOP = 2
BOX_BEZEL_H = 6
BUTTON_BEZEL_MINI = 1  # a mini Button barely grows (measured): 1px per side
PAD = 12
ROW_GAP = 6
SECTION_GAP = 12
LABEL_W = 64
CONTROL_X = CARD_MARGIN + LABEL_W + 4
LABEL_H = 15  # TextBox height; labels are centred on their control
HINT_H = 12  # one line of mini text (the grey hint above each slider)
DICE_FALLBACK_CHARS = "ABCDEFGHKMOPQRSWXZgaej0123456789@&#$£%?€¥"
DICE_SYMBOLS = frozenset("@&#$£%?€¥")  # symbols allowed on dice faces


def dice_char_allowed(char: str) -> bool:
    """Dice faces show only plain letters (no accents/diacritics), digits and a
    small set of symbols (@&#$£%?€¥)."""
    if char in DICE_SYMBOLS:
        return True
    category = unicodedata.category(char)
    if category.startswith("N"):
        return True
    if category.startswith("L"):
        # any combining mark in the decomposition = accented -> excluded
        return all(not unicodedata.combining(c) for c in unicodedata.normalize("NFD", char))
    return False


def glyph_char(glyph) -> str | None:
    """The character a glyph encodes, or None (no glyph, unencoded)."""
    return unicode_char(getattr(glyph, "unicode", None))


def font_characters(font) -> frozenset[str] | None:
    """Every character the font encodes: proof words use only these, so none shows
    a missing glyph. None without a font (no limit)."""
    if font is None:
        return None
    return frozenset(
        char
        for glyph in font.glyphs
        for code in glyph.unicodes or ()
        if (char := unicode_char(code))
    )


def glyph_tokens(raw: str) -> list[str]:
    """The Letter field's entries, separated by spaces (commas and semicolons
    too); NFC, so a decomposed é stays one character."""
    normalized = unicodedata.normalize("NFC", str(raw))
    return normalized.replace(",", " ").replace(";", " ").split()


def _token_char(token: str, char_for_name) -> str | None:
    """One character is that glyph; anything longer is a glyph name ("/name" too)."""
    if len(token) == 1:
        return token
    return char_for_name(token.removeprefix("/")) if char_for_name else None


def parse_letters(raw: str, char_for_name=None) -> tuple[str, ...]:
    """The glyphs to test, in typed order, repeats dropped, at most
    MAX_PROOF_LETTERS: "a b c", "eacute b", "/eacute b". Several proof
    TOGETHER, like a selection: words holding them all. A name char_for_name
    can't turn into a character adds nothing (see unknown_glyph_names)."""
    chars = (_token_char(token, char_for_name) for token in glyph_tokens(raw))
    return tuple(dict.fromkeys(char for char in chars if char))[:MAX_PROOF_LETTERS]


def unknown_glyph_names(raw: str, char_for_name=None) -> list[str]:
    """Entries naming no encoded glyph: Show Words reports them rather than
    proofing something else ("abc" is a name now, not a, b, c)."""
    return [token for token in glyph_tokens(raw) if not _token_char(token, char_for_name)]


def glyph_name_char(font, name: str) -> str | None:
    """Character of a glyph name: the font's own glyph first (custom names),
    else Glyphs' glyph database (eacute, uni00E9; no font needed). None when
    unknown or unencoded (a.ss01)."""
    code = None
    with contextlib.suppress(Exception):
        code = font.glyphs[name].unicode
    if not code:
        with contextlib.suppress(Exception):
            code = Glyphs.glyphInfoForName(name).unicode
    return unicode_char(code)


def current_layer_letters(font) -> tuple[str, ...] | None:
    """The characters being edited, the fallback for an empty Letter field: the
    caret's glyph or a selection's glyphs, in order, no repeats, at most
    MAX_PROOF_LETTERS. None when only separators (space glyphs, the Edit view's
    newline) are selected: walking the caret through proof words keeps the letter."""
    if font is None:
        return ()
    letters: list[str] = []
    separators = 0
    try:
        for layer in font.selectedLayers or ():
            glyph = getattr(layer, "parent", None)
            char = glyph_char(glyph)
            if glyph is None or (char and unicodedata.category(char).startswith("Z")):
                separators += 1
            elif char and char not in letters:
                letters.append(char)
                if len(letters) == MAX_PROOF_LETTERS:
                    break
    except Exception:
        return ()
    if not letters and separators:
        return None
    return tuple(letters)


class AZLetterProoferWindow:
    def __init__(self):
        self._dice_node = None  # the live SceneKit dice; None while hidden or off
        self._dice_background = None
        self._dice_face_chars = []
        self._tracked_master_id = None
        self._morph_closed = False  # the window is gone: late callbacks do nothing
        self._morph_gen = {}  # face -> newest frame-build token
        self._morph_timer = None  # face 0's playback
        self._morph_build_queue = None
        self._landings = {}  # face -> (timer, finish) of its playback in flight
        self._feature_timers = []  # staggered face-morph timers
        self._letters_refresh_timer = None
        self._recolor_timer = None
        self._name_chars = {}
        self._tag_members = ()
        self._base_languages = None  # set once the Languages list is built
        self._popup_observer = None
        self._key_monitor = None
        self._interface_callback = None
        self.source = WordSource(DATA_DIR)
        if MAC_DICTIONARY_PATH.exists():
            self.source.add_wordlist(MAC_DICTIONARY, MAC_DICTIONARY_PATH)
        self._custom_list = None  # the name of the user's own list, if any
        custom = Glyphs.defaults[PREFS + "customWordlist"]
        if custom and Path(str(custom)).exists():
            self._custom_list = Path(str(custom)).name
            self.source.add_wordlist(self._custom_list, Path(str(custom)))
        self.ratings = RatingsDatabase.from_file(DATA_DIR / "ratings.json")
        self.group_names = self.ratings.group_names("Latin", "lowercase")
        register_defaults()
        self._show_dice = bool(Glyphs.defaults[PREFS + "showDice"])
        self._load_dice_color()  # apply the saved glow colour before the dice builds
        # Seed the dice before it builds, so the first bake is final.
        saved_letters = self._typed_letters(str(Glyphs.defaults[PREFS + "letters"] or ""))
        self._tracked_letters = current_layer_letters(Glyphs.font) or ()
        letters = saved_letters or self._tracked_letters
        self._dice_focus = tuple(letters[:12])  # one focus face per letter
        self._fronted_letters = saved_letters  # no glide for the restored letter
        self._build_window()
        self._setup_dice()  # early-returns (no canvas) when _show_dice is False
        self._load_prefs()
        self._start_glyph_tracking()
        self.w.bind("close", self._window_closed)
        self.w.open()
        self.w.makeKey()
        self._install_key_monitor()
        self.w.getNSWindow().setLevel_(NSFloatingWindowLevel)

    # ------------------------------------------------------------------ UI

    def _build_window(self):
        self.w = Window((WINDOW_WIDTH, 540), "AZ Letter Proofer")

        # The dice card on top; hidden, the body card starts at the top margin.
        if self._show_dice:
            self._add_dice_card()
            y = CARD_MARGIN + DICE_HEIGHT + CARD_GAP + CARD_PAD  # first card below the dice
        else:
            y = CARD_MARGIN + CARD_PAD  # no dice: body card starts at the top margin
        # Right-click anywhere: subviews without a menu hand the click up to here.
        self._cube_menu_target = VanillaCallbackWrapper(self.toggle_show_dice)  # held weakly
        menu = NSMenu.alloc().initWithTitle_("")
        self._cube_menu_item = menu.addItemWithTitle_action_keyEquivalent_(
            "Hide 3D Cube" if self._show_dice else "Show 3D Cube", "action:", ""
        )
        self._cube_menu_item.setTarget_(self._cube_menu_target)
        self._color_menu_target = VanillaCallbackWrapper(self.pick_dice_color)
        menu.addItemWithTitle_action_keyEquivalent_("Dice Colour…", "action:", "").setTarget_(
            self._color_menu_target
        )
        self.w.getNSWindow().contentView().setMenu_(menu)
        # Dice Colour… activates this hidden well: it takes the shared colour
        # panel and lets go of it as soon as any other well is activated.
        self.w.dice_color = ColorWell(
            (0, 0, 0, 0), color=self._current_dice_color(), callback=self.dice_color_changed
        )
        self.w.dice_color.show(False)

        def label(text, row_y, height, width=LABEL_W, x=CARD_MARGIN):
            """A row label, vertically centred on a control of `height`."""
            top = row_y + round((height - LABEL_H) / 2)
            return TextBox((x, top, width, LABEL_H), text, sizeStyle="small")

        # One card holds everything below the dice: created first so it sits behind.
        card_top = y - CARD_PAD
        card_x = CARD_MARGIN + CARD_PAD
        card_ctrl_x = CONTROL_X + CARD_PAD
        card_right = -(CARD_MARGIN + CARD_PAD)
        self.w.body_card = Box(
            (
                CARD_MARGIN + BOX_BEZEL_X,
                card_top + BOX_BEZEL_TOP,
                -(CARD_MARGIN + BOX_BEZEL_X),
                10,
            )
        )
        self._style_card(self.w.body_card.getNSBox())

        # Letter field + the word-count stepper (1..10) at the row's right end:
        # the locked field shows the count, the up/down stepper changes it.
        step_w, words_w, gap = 15, 30, 3
        step_x = -(CARD_MARGIN + CARD_PAD) - step_w
        words_x = step_x - gap - words_w
        self.w.letters_label = label("Letter", y, 22, x=card_x)
        self.w.letters = EditText(
            (card_ctrl_x, y, words_x - gap, 22), "", callback=self.letters_changed
        )
        self.w.results = EditText((words_x, y, words_w, 22), "10", sizeStyle="small")
        self.w.results.getNSTextField().setEditable_(False)  # stepper-only, no typing
        self.w.results.getNSTextField().setAlignment_(1)  # NSTextAlignmentCenter (iOS value)
        self.w.results_stepper = Stepper(
            (step_x, y, step_w, 22),
            value=10,
            minValue=1,
            maxValue=10,
            callback=self.words_stepped,
            sizeStyle="small",
        )
        y += 22 + ROW_GAP

        # 22 tall so the ~21 pt segmented control keeps a gap below it.
        self.w.position_label = label("Position", y, 22, x=card_x)
        self.w.position = SegmentedButton(
            (card_ctrl_x, y, card_right, 21),
            [
                self._position_segment("icon-pos-start.pdf"),
                self._position_segment("icon-pos-contain.pdf"),
            ],
            selectionStyle="one",  # exactly one of start/contain
            sizeStyle="small",
        )
        y += 22 + ROW_GAP
        # Capitalize off -> case follows the typed letter (see _current_case).
        sw_box = 30  # a mini Switch really is 30 wide (ink 25.5, measured)
        self.w.capitalize_label = label("Capitalize", y, 18, x=card_x)
        self.w.capitalize = Switch((card_ctrl_x, y + 1, sw_box, 16))
        y += 18 + ROW_GAP

        self.w.group_label = label("Feature", y, 18, x=card_x)
        self._feature_options, feature_items = self._feature_items(Glyphs.font)
        self.w.group = PopUpButton(
            (card_ctrl_x, y, card_right, 18),
            feature_items,
            sizeStyle="small",
            callback=self.feature_changed,
        )
        # Refresh the tag options as the popup opens, so tags added since show up.
        self._popup_observer = (
            NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(  # noqa: E501
                NSPopUpButtonWillPopUpNotification,
                self.w.group.getNSPopUpButton(),
                None,
                lambda notification: self._update_feature_popup(),
            )
        )
        y += 18 + SECTION_GAP  # gap to the Languages section (same card)

        # Mini buttons: their bezel grows by only BUTTON_BEZEL_MINI, so they sit flush.
        self.w.lang_label = label("Languages", y, 18, width=80, x=card_x)
        toggle_w = 56
        self.w.lang_toggle = Button(
            (card_right - BUTTON_BEZEL_MINI - toggle_w, y + 1, toggle_w, 16),
            "Clear all",
            sizeStyle="mini",
            callback=self.toggle_languages,
        )
        self.w.lang_add = Button(
            (card_right - BUTTON_BEZEL_MINI - 2 * toggle_w - 4, y + 1, toggle_w, 16),
            "Add list…",
            sizeStyle="mini",
            callback=self.add_wordlist,
        )
        self.w.lang_add.getNSButton().setToolTip_(
            "Add your own word list: a .txt file with one word per line."
        )
        y += 18 + ROW_GAP
        # The full roster; the list shows the languages covering the tested letter.
        self._base_languages = list(self.source.languages)
        self._lang_use = dict.fromkeys(self._base_languages, True)
        self._lang_use[MAC_DICTIONARY] = False
        self._lang_filter_names = None  # forces the first filter to build the rows
        items = [{"use": self._lang_use[name], "language": name} for name in self._base_languages]
        self.w.languages = List(
            (card_x, y, card_right, LANG_LIST_HEIGHT),
            items,
            columnDescriptions=[
                {"title": "use", "cell": CheckBoxListCell(), "width": 14},
                {"title": "language"},
            ],
            showColumnTitles=False,
            drawFocusRing=False,
            rowHeight=LIST_ROW_HEIGHT,
        )
        table = self.w.languages.getNSTableView()
        # Same font size as every other control.
        for column in table.tableColumns():
            column.dataCell().setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        # The name column absorbs width changes, so row backgrounds follow a resize.
        table.setColumnAutoresizingStyle_(TABLE_LAST_COLUMN_AUTORESIZE)
        table.sizeLastColumnToFit()
        # Plain style: in a sidebar, macOS 11+ turns the default into inset capsules.
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setStyle_(TABLE_STYLE_PLAIN)
        y += LANG_LIST_HEIGHT + SECTION_GAP  # gap to the action row (same card)

        # Scoring options live in a popover behind the gear button. Same PAD /
        # ROW_GAP / SECTION_GAP metrics as the window.
        pop_w = 300
        pop_label_w = 108
        pop_control_x = PAD + pop_label_w + 4
        self.pop = Popover((pop_w, 196))
        slider_specs = [
            ("diversity", "Diversity", "How many different letter shapes each word mixes."),
            (
                "letter_frequency",
                "Letter frequency",
                "How often your letter repeats within a word.",
            ),
            ("mark_frequency", "Marks frequency", "Accents per word, for a letter that has one."),
            ("texture", "Texture", "How common each word's letter pairs are."),
        ]
        hint_color = NSColor.secondaryLabelColor()
        pop_y = PAD
        self.pop.intro = TextBox(
            (PAD, pop_y, -PAD, 0),
            "Smart ranking scores every word that fits your settings and shows the "
            "best ones to proof with: frequent, long words full of demanding letters. "
            "The sliders tune that score; at 0 they are left out.",
            sizeStyle="small",
        )
        intro_h = (
            self.pop.intro.getNSTextField()
            .cell()
            .cellSizeForBounds_(((0, 0), (pop_w - 2 * PAD, 1000)))
            .height
        )
        self.pop.intro.setPosSize((PAD, pop_y, -PAD, intro_h))
        pop_y += round(intro_h) + SECTION_GAP
        for name, title, hint in slider_specs:
            hint_box = TextBox((PAD, pop_y, -PAD, HINT_H), hint, sizeStyle="mini")
            hint_box.getNSTextField().setTextColor_(hint_color)
            setattr(self.pop, name + "_hint", hint_box)
            pop_y += HINT_H + 2
            setattr(
                self.pop,
                name + "_label",
                TextBox((PAD, pop_y + 1, pop_label_w, LABEL_H), title, sizeStyle="small"),
            )
            setattr(
                self.pop,
                name,
                Slider(
                    (pop_control_x, pop_y, -PAD, 16),
                    minValue=-2,
                    maxValue=2,
                    value=0,
                    tickMarkCount=5,
                    stopOnTickMarks=True,
                    sizeStyle="mini",
                ),
            )
            pop_y += 16 + SECTION_GAP
        self.pop.resize(pop_w, pop_y - SECTION_GAP + PAD)

        def switch_row(name, title, row_y, tip=None):
            """A label + right-edge Switch row, both named after `name`."""
            setattr(self.w, f"{name}_label", label(title, row_y, 18, width=150, x=card_x))
            setattr(self.w, name, Switch((card_right - sw_box, row_y + 1, sw_box, 16)))
            if tip:
                getattr(self.w, f"{name}_label").getNSTextField().setToolTip_(tip)
                getattr(self.w, name)._nsObject.setToolTip_(tip)
            return row_y + 18 + ROW_GAP

        # Smart ranking and its scoring gear; off, random fitting words like word-o-mat.
        smart_y = y
        y = switch_row(
            "smart",
            "Smart ranking",
            y,
            "On: the best words to proof come first. "
            "Off: random words that fit your settings, like word-o-mat.",
        )
        text_w = self.w.smart_label.getNSTextField().attributedStringValue().size().width
        gear_w = 20
        self.w.sliders_gear = Button(
            (card_x + round(text_w) + 4, smart_y - 3, gear_w, gear_w),
            "⚙",
            callback=self.open_score_popover,
        )
        gear_button = self.w.sliders_gear.getNSButton()
        gear_button.setBordered_(False)  # just the icon, no bezel
        gear_button.setFont_(NSFont.systemFontOfSize_(15))
        gear_button.setToolTip_("Scoring options")
        # The Extra settings header folds the toggles below it (built unfolded).
        self.w.extra_toggle = ImageButton(
            (card_x + 2, y, card_right, 18),  # +2: a TextBox's text inset, so it lines up
            title="Extra settings",
            bordered=False,
            imagePosition="right",
            callback=self.toggle_extra_settings,
            sizeStyle="small",
        )
        extra_button = self.w.extra_toggle.getNSButton()
        extra_button.setAlignment_(0)  # NSTextAlignmentLeft
        # A borderless button draws its title dimmer than a label: match "Languages".
        extra_button.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                "Extra settings",
                {
                    NSFontAttributeName: NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()),
                    NSForegroundColorAttributeName: NSColor.labelColor(),
                },
            )
        )
        y += 18 + ROW_GAP
        extra_top = y
        y = switch_row("one_per_line", "Split lines", y)
        y = switch_row("current_tab", "Use current tab", y)
        y = switch_row("unique", "No repeated letter", y)
        y = switch_row(
            "all_letters",
            "All letters in each word",
            y,
            "For several letters. On: every word holds all of them. "
            "Off: words holding as many of them as possible.",
        )
        self._extra_height = y - extra_top

        # Action row: Show Words filling the width (same card). Stock system
        # colours — no custom tinting.
        run_h = 26
        # Inset by BUTTON_BEZEL so the painted edges land on the card column.
        self.w.run = Button(
            (
                card_x + BUTTON_BEZEL,
                y,
                card_right - BUTTON_BEZEL,
                run_h,
            ),
            "Show Words",
            callback=self.run,
        )
        self.w.setDefaultButton(self.w.run)
        # The single card wraps everything from the settings rows to here.
        self.w.body_card.setPosSize(
            (
                CARD_MARGIN + BOX_BEZEL_X,
                card_top + BOX_BEZEL_TOP,
                -(CARD_MARGIN + BOX_BEZEL_X),
                (y + run_h + CARD_PAD) - card_top - BOX_BEZEL_H,
            )
        )
        y += run_h + CARD_PAD + CARD_MARGIN  # bottom margin matches the side margins

        for control in (
            self.w.letters,
            self.w.results,
            self.w.group,
            self.w.position,
            self.w.lang_toggle,
            self.w.lang_add,
            self.w.run,
            self.w.sliders_gear,
            self.w.extra_toggle,
        ):
            kill_focus_ring(control._nsObject)
        kill_focus_ring(self.w.languages.getNSTableView())

        self.w.resize(WINDOW_WIDTH, y)
        # Can't be dragged narrower than the single column or shorter than the
        # stack: nothing reflows below that, controls would just get clipped.
        self.w.getNSWindow().setContentMinSize_((WINDOW_WIDTH, y))
        self._content_height = y
        self._show_extra = True  # as built
        self._set_extra_settings(bool(Glyphs.defaults[PREFS + "showExtraSettings"]))

    def _add_dice_card(self):
        """The dice card at the top of the window (its SCNView: _setup_dice)."""
        self._tumbling = True  # a fresh dice always tumbles
        self.w.dice = Group((CARD_MARGIN, CARD_MARGIN, -CARD_MARGIN, DICE_HEIGHT))
        # Freeze/resume, top-right over the canvas; +2: vanilla lifts the centre 2 pt.
        self.w.dice_stop = Button(
            (-(CARD_MARGIN + 20), CARD_MARGIN + 2, 20, 20), "", callback=self.toggle_tumble
        )
        self._style_dice_overlay_button(self.w.dice_stop.getNSButton())
        self._sync_tumble_button()

    def words_stepped(self, sender=None):
        """The up/down stepper drives the (locked) word-count field, 1..10."""
        self.w.results.set(str(round(self.w.results_stepper.get())))

    @staticmethod
    def _position_segment(name):
        """A Position segment: the bundled icon, 10 pt tall (a touch under the small
        segmented control), a template so the control tints it."""
        image = NSImage.alloc().initWithContentsOfFile_(str(ASSETS_DIR / name))
        source = image.size()
        image.setSize_(NSMakeSize(source.width * 10.0 / source.height, 10.0))
        image.setTemplate_(True)
        return {"title": "", "imageObject": image}

    # -------------------------------------------------------- dice colour

    def _current_dice_color(self):
        """NSColor for the well's initial value: the body colour."""
        red, green, blue = dice.SURFACE_ALBEDO
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 1.0)

    @staticmethod
    def _apply_dice_color(rgb):
        """The colour well themes the body (a solid albedo) AND the letter
        glows: the pick's hue at each glow's tuned brightness, so a dark pick
        still gets a readable glow. Bloom and the key/fill lights stay tuned."""
        dice.SURFACE_ALBEDO = rgb
        peak = max(rgb)
        if peak <= 0:  # black has no hue: leave the glows as tuned
            return
        hue = tuple(channel / peak for channel in rgb)
        for name in ("GLOW_COLOR", "HIGHLIGHT_GLOW_COLOR", "IDLE_GLOW_COLOR"):
            # max() of a derived glow is the same brightness again, so re-applying is stable
            brightness = max(getattr(dice, name))
            setattr(dice, name, tuple(channel * brightness for channel in hue))

    def _load_dice_color(self):
        """Apply the saved dice colour to the dice module BEFORE the dice builds,
        so the first bake + the first light pass already wear it. Unset -> the
        registered grey default; junk -> keep the code defaults."""
        raw = Glyphs.defaults[PREFS + "diceColor"]
        rgb = hex_to_rgb(raw) if raw else None
        if rgb is not None:
            self._apply_dice_color(rgb)

    def pick_dice_color(self, sender=None):
        """Right-click > Dice Colour…: the colour panel, on the hidden well."""
        self.w.dice_color.set(self._current_dice_color())
        self.w.dice_color.getNSColorWell().activate_(True)  # shows the panel

    def dice_color_changed(self, sender=None):
        """Hidden colour well (Dice Colour…): persist the pick, then theme the die."""
        try:
            if save_dice_color(sender.get()):
                self.dice_color_saved()
        except Exception:
            print(f"AZ Letter Proofer dice colour error:\n{traceback.format_exc()}")

    def dice_color_saved(self):
        """Theme the die to the saved colour, picked here or in the Settings
        pane. Wells fire continuously while dragging, so the re-bake waits for
        a 0.15 s pause."""
        self._load_dice_color()
        _sync_settings_pane()

        previous = self._recolor_timer
        if previous is not None:
            previous.invalidate()
        self._recolor_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.15, False, lambda timer: self._recolor_dice()
        )

    def _recolor_dice(self):
        """Re-bake the body and every face in the current colours (letters kept)."""
        self._recolor_timer = None
        if self._dice_node is None or self._morph_closed:
            return
        try:
            self._cancel_morphs()
            self._dice_background = dice.make_background()
            self._reskin_faces(Glyphs.font, fade=0.0)
            self._set_aura()  # crack lights take the new glow colour
        except Exception:
            print(f"AZ Letter Proofer dice colour error:\n{traceback.format_exc()}")

    def toggle_extra_settings(self, sender=None):
        """The Extra settings header: fold / unfold its rows. Persisted."""
        self._set_extra_settings(not self._show_extra)
        Glyphs.defaults[PREFS + "showExtraSettings"] = self._show_extra

    def _set_extra_settings(self, show):
        """Show or hide the Extra settings rows IN PLACE: Show Words and the
        card's bottom slide by their height, and the window follows from the
        bottom (top edge fixed), like the dice card."""
        if show != self._show_extra:
            self._show_extra = show
            for name in EXTRA_SETTINGS:
                getattr(self.w, name).show(show)
            shift = self._extra_height if show else -self._extra_height
            left, top, width, height = self.w.run.getPosSize()
            self.w.run.setPosSize((left, top + shift, width, height))
            left, top, width, height = self.w.body_card.getPosSize()
            self.w.body_card.setPosSize((left, top, width, height + shift))
            self._content_height += shift
            self.w.getNSWindow().setContentMinSize_((WINDOW_WIDTH, self._content_height))
            _left, _top, width, height = self.w.getPosSize()
            self.w.resize(width, height + shift, animate=False)
        chevron = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "chevron.down" if show else "chevron.right", None
        )
        config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            9, NSFontWeightSemibold
        )
        self.w.extra_toggle.getNSButton().setImage_(chevron.imageWithSymbolConfiguration_(config))

    def set_show_dice(self, show):
        """Settings pane: show or hide the dice to match (no-op when it does)."""
        if bool(show) != self._show_dice:
            self.toggle_show_dice()

    def toggle_show_dice(self, sender=None):
        """Right-click menu: show/hide the dice card IN PLACE. The cards below
        slide up/down by its height and the window shrinks/grows from the
        bottom (top edge fixed). Persisted."""
        self._show_dice = not self._show_dice
        Glyphs.defaults[PREFS + "showDice"] = self._show_dice
        shift = DICE_HEIGHT + CARD_GAP
        if not self._show_dice:
            self._cancel_morphs()
            self._dice_node = None
            del self.w.dice_stop
            del self.w.dice
            shift = -shift
        for name, item in list(vars(self.w).items()):
            if not name.startswith("_") and hasattr(item, "getPosSize"):
                left, top, width, height = item.getPosSize()
                item.setPosSize((left, top + shift, width, height))
        self._content_height += shift
        window = self.w.getNSWindow()
        window.setContentMinSize_((WINDOW_WIDTH, self._content_height))
        _left, _top, width, height = self.w.getPosSize()
        self.w.resize(width, height + shift, animate=False)
        if self._show_dice:
            self._add_dice_card()
            self._setup_dice()
        self._cube_menu_item.setTitle_("Hide 3D Cube" if self._show_dice else "Show 3D Cube")
        _sync_settings_pane()

    # ------------------------------------------------------------- dice

    def _setup_dice(self):
        """Attach the SceneKit dice to its banner group; hide on any failure."""
        self._dice_node = None
        if not self._show_dice:  # canvas hidden -> no group to attach to
            return
        try:
            faces, glows, boosts = self._dice_images()
            group_view = self.w.dice.getNSView()
            view, dice_node = dice.make_dice_view(
                WINDOW_WIDTH, DICE_HEIGHT, faces, emission_images=glows, emission_boosts=boosts
            )
            view.setFrame_(group_view.bounds())
            view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            # Round the card: the opaque scene background gets clipped to a
            # rounded rect (masksToBounds clips the Metal drawable sublayer too).
            view.setWantsLayer_(True)
            view.layer().setCornerRadius_(CARD_RADIUS)
            view.layer().setMasksToBounds_(True)
            group_view.addSubview_(view)
            self._dice_node = dice_node
            self._set_aura()
        except Exception:
            print(f"AZ Letter Proofer: dice disabled\n{traceback.format_exc()}")

    def _feature_dice_chars(self, font):
        """Characters the active Feature mode points at — these get first
        claim on dice faces (random glyphs only fill what's left)."""
        kind, value = self._feature()
        if kind == "group":
            return sorted(self.ratings.group_chars(value))
        if kind == "tag" and font is not None:
            return chars_for_tag(font, value)
        return []

    def _dice_images(self):
        """Twelve faces: (diffuse maps, emission maps, emission boosts). The focus
        letters first (or a stand-in on face 0), then the Feature's characters, then
        random font glyphs round-robin across scripts, then fallback letters."""
        font = Glyphs.font
        # Remember which master these faces were drawn at, so a later master
        # switch is detected (see _check_master_change) and re-skins the faces.
        self._tracked_master_id = self._selected_master_id(font)
        self._dice_background = dice.make_background()  # cached for single-face updates
        try:
            feature_chars = [
                char for char in self._feature_dice_chars(font) if dice_char_allowed(char)
            ]
        except Exception:
            feature_chars = []
        chars = list(self._dice_focus) or [
            feature_chars[0] if feature_chars else random.choice(DICE_FALLBACK_CHARS)
        ]
        drawable = self._drawable_chars(font)
        drawable_set = {char for char, _script in drawable}
        for char in feature_chars:  # no font: the system font draws them
            if char not in chars and (font is None or char in drawable_set):
                chars.append(char)
        buckets: dict[object, list] = {}
        for char, script in drawable:
            if char not in chars:
                buckets.setdefault(script, []).append(char)
        shuffled = list(buckets.values())
        for bucket in shuffled:
            random.shuffle(bucket)
        random.shuffle(shuffled)
        chars += [char for tier in zip_longest(*shuffled) for char in tier if char is not None]
        fallback = [char for char in DICE_FALLBACK_CHARS if char not in chars]
        random.shuffle(fallback)
        chars = (chars + fallback)[:12]
        self._dice_face_chars = chars
        roles = [self._face_role(index) for index in range(12)]
        pairs = [self._face_pair(font, char, role) for char, role in zip(chars, roles, strict=True)]
        boosts = [self._glow(role)[1] for role in roles]
        return [face for face, _glow in pairs], [glow for _face, glow in pairs], boosts

    @staticmethod
    def _drawable_chars(font):
        """(char, script) for every encoded glyph the dice can show: an allowed
        character whose outline in the selected master is at least x-height tall."""
        if font is None or not font.glyphs:
            return []
        master = font.selectedFontMaster or font.masters[0]
        min_height = (master.xHeight or 0) * 0.98  # tolerance for flat lowercase
        found = []
        for glyph in font.glyphs:
            char = glyph_char(glyph)
            if char is None or not dice_char_allowed(char):
                continue
            layer = glyph.layers[master.id]
            box = layer.bounds.size if layer is not None else None
            if box is not None and box.width > 0 and box.height >= min_height:
                found.append((char, glyph.script))
        return found

    def _note_face_char(self, index, char):
        """Track which character a face shows (morph sources)."""
        chars = self._dice_face_chars
        if chars and 0 <= index < len(chars):
            chars[index] = char

    @staticmethod
    def _auto_letter(chars, modifier_blocked, editing):
        """The character a keystroke puts in the Letter field, or None to let it
        pass: one printable non-space character, no Cmd/Ctrl, no field in edit."""
        if modifier_blocked or editing:
            return None
        if not chars or len(chars) != 1:
            return None
        if not chars.isprintable() or chars.isspace():
            return None
        return chars

    def _install_key_monitor(self):
        """Type-to-set: while the window is focused, any printable keystroke
        (when not already editing a field) fills the Letter field, so the
        user never has to click into it first."""
        try:
            nswindow = self.w.getNSWindow()
            shortcut_mask = NSEventModifierFlagCommand | NSEventModifierFlagControl

            def handler(event):
                try:
                    if event.window() is not nswindow:
                        return event
                    # characters(), NOT charactersIgnoringModifiers(): a dead-key
                    # sequence (^ then a -> â) reports "" while composing and the
                    # composed "â" on commit, whereas ignoringModifiers would hand
                    # us the bare "^" and then "a" — inserting the wrong glyph and
                    # killing the accent. Empty -> mid-compose, let AppKit finish.
                    editing = isinstance(nswindow.firstResponder(), NSText)
                    blocked = bool(int(event.modifierFlags()) & shortcut_mask)
                    char = self._auto_letter(event.characters(), blocked, editing)
                    if char is None:
                        return event
                    field = self.w.letters.getNSTextField()
                    # Replace, not append: a keystroke while the field isn't being
                    # edited starts a new proof; once editing, typing adds letters.
                    self.w.letters.set(char)
                    nswindow.makeFirstResponder_(field)  # now editing, cursor in place
                    self.letters_changed()
                    return None  # swallow: the char is already placed
                except Exception:
                    return event

            self._key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, handler
            )
        except Exception:
            self._key_monitor = None
            print(f"AZ Letter Proofer: key monitor failed\n{traceback.format_exc()}")

    def _remove_key_monitor(self):
        monitor = self._key_monitor
        if monitor is not None:
            with contextlib.suppress(Exception):
                NSEvent.removeMonitor_(monitor)
            self._key_monitor = None

    # ------------------------------------------- current-glyph fallback

    def _effective_letters(self):
        """The typed letters, else the edited glyph from the tracked cache."""
        return self._typed_letters() or self._tracked_letters

    def _typed_letters(self, raw=None):
        """The Letter field's glyphs (raw: other text, e.g. the saved field)."""
        text = self.w.letters.get() if raw is None else raw
        return parse_letters(str(text or ""), self._char_for_name)

    def _char_for_name(self, name):
        """glyph_name_char, cached per name until the field changes or Show
        Words runs: UPDATEINTERFACE re-reads the field often, and Glyphs.font
        is a WindowServer round-trip."""
        cache = self._name_chars
        if name not in cache:
            cache[name] = glyph_name_char(Glyphs.font, name)
        return cache[name]

    def _start_glyph_tracking(self):
        """Follow the edited glyph so an empty Letter field proofs whatever is
        on screen (the dice and Show Words read _effective_letters)."""
        try:
            Glyphs.addCallback(self._interface_updated, UPDATEINTERFACE)
            self._interface_callback = self._interface_updated
        except Exception:
            self._interface_callback = None
            print(f"AZ Letter Proofer glyph tracking off:\n{traceback.format_exc()}")
        self._interface_updated()

    def _current_glyph_letters(self, font):
        """The edited glyph, read fresh; on a separator, the last tracked glyph."""
        letters = current_layer_letters(font)
        return self._tracked_letters if letters is None else letters

    def _stop_glyph_tracking(self):
        if self._interface_callback is None:
            return
        with contextlib.suppress(Exception):
            Glyphs.removeCallback(self._interface_callback)
        self._interface_callback = None

    def _interface_updated(self, sender=None):
        """UPDATEINTERFACE: re-skin the dice to the selected master, and re-read
        the current glyph when nothing is typed. The typed path stays free of
        the Glyphs.font XPC hit unless a live dice needs the master check."""
        try:
            if self._morph_closed:
                return
            typed = bool(self._typed_letters())
            # Glyphs.font only when a live dice needs the master check or nothing is typed.
            dice_live = self._dice_node is not None
            font = Glyphs.font if (dice_live or not typed) else None
            if dice_live:
                self._check_master_change(font)
            if typed:
                self._set_letters_placeholder("")
                return
            letters = current_layer_letters(font)
            if letters is None:  # caret on a separator: keep the current glyph
                return
            # Show it greyed in the empty field: the fallback is invisible
            # otherwise, and "why did it proof that?" is a bad surprise.
            self._set_letters_placeholder(" ".join(letters))  # the format to type
            if letters == self._tracked_letters:
                return
            self._tracked_letters = letters
            self._update_dig_enabled()
            self._update_language_filter()  # follow the edited glyph, not just typing
            self._update_dice_highlight()
        except Exception:
            print(f"AZ Letter Proofer glyph tracking error:\n{traceback.format_exc()}")

    def _set_letters_placeholder(self, text):
        with contextlib.suppress(Exception):
            self.w.letters.getNSTextField().setPlaceholderString_(text)

    def letters_changed(self, sender=None):
        self._name_chars = {}  # names resolve afresh as they're typed
        self._interface_updated()  # emptying the field brings the glyph back
        self._update_dig_enabled()
        self._update_language_filter()  # narrow the list to letters we can proof
        self._schedule_letters_refresh()

    def _schedule_letters_refresh(self):
        """Coalesce the heavy per-keystroke work (dice morphs) behind a short
        timer: fast typing costs one pass, not one per key."""
        if self._letters_refresh_timer is not None:
            self._letters_refresh_timer.invalidate()

        def fire(_timer):
            self._letters_refresh_timer = None
            if self._morph_closed:
                return
            self._update_dice_highlight()  # face 0 now carries the focus letter
            letters = self._typed_letters()
            if letters != self._fronted_letters:
                self._fronted_letters = letters
                if self._dice_node is not None and letters:
                    # glide the new focus letter to the front; the tumble carries on
                    dice.face_front(self._dice_node, resume=lambda: self._tumbling)

        self._letters_refresh_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.18, False, fire
        )

    def _write_letters(self, text):
        """Replace the Letter field's text, caret at the end. Writes through the
        live field editor when the field is mid-edit (setStringValue_ alone can
        fail to refresh an active editor), else a plain set."""
        editor = None
        try:
            field = self.w.letters.getNSTextField()
            editor = field.currentEditor() if field is not None else None
        except Exception:
            editor = None
        if editor is not None:
            editor.setString_(text)
            with contextlib.suppress(Exception):
                editor.setSelectedRange_((len(text), 0))
        else:
            self.w.letters.set(text)

    def _update_dig_enabled(self):
        """Disable Show Words when there's nothing to proof — no letters and no
        feature/group selected (a feature-only dig is still allowed)."""
        try:
            letters = self._effective_letters()
            has_feature = self._selected_feature() != "none:"
            self.w.run.enable(bool(letters) or has_feature)
            self.w.all_letters.enable(len(letters) > 1)  # only means something for several
        except Exception:
            self.w.run.enable(True)  # never leave the button stuck off

    def _update_dice_highlight(self):
        """One focus face per letter being proofed (faces 0..N-1, violet glow);
        with nothing to proof every face glows the idle purple. Only changed
        faces morph: a face that gains a letter, one that loses it (back to a
        fresh plain glyph), and plain faces that duplicate a focus letter."""
        new = tuple(self._effective_letters()[:12])
        old = self._dice_focus
        if new == old:
            return
        self._dice_focus = new
        if self._dice_node is None or self._dice_background is None:
            return
        try:
            font = Glyphs.font
            chars = self._dice_face_chars
            if not new:  # going idle: a stand-in on face 0, the rest re-lit purple
                self._cancel_morphs()  # in-flight morphs would land in their old role
                stand_in = random.choice(DICE_FALLBACK_CHARS)
                self._morph_face(font, 0, chars[0], stand_in, from_role="focus")
                self._reskin_faces(font, faces=range(1, 12), fade=0.4)
                self._set_aura()
                return
            pool = [char for char, _script in self._drawable_chars(font)]
            pool += [char for char in DICE_FALLBACK_CHARS if char not in pool]
            pool = [char for char in pool if char not in new]  # one face per letter
            random.shuffle(pool)
            for index in range(12):
                if index < len(new):  # a focus face
                    if index < len(old) and chars[index] == new[index]:
                        continue
                    was = "focus" if index < len(old) else ("plain" if old else "idle")
                    self._morph_face(font, index, chars[index], new[index], from_role=was)
                elif not old or index < len(old) or chars[index] in new:
                    # leaving idle, lost its focus, or now duplicates a focus letter
                    was = "focus" if index < len(old) else None
                    fresh = pool.pop() if pool else "a"
                    self._morph_face(font, index, chars[index], fresh, from_role=was)
        except Exception:
            print(f"AZ Letter Proofer highlight error:\n{traceback.format_exc()}")

    def _window_closed(self, sender=None):
        """Window teardown: persist the current UI options, then kill the
        repeating timers so they release us."""
        try:
            self._save_prefs(self._current_positions())
        except Exception:
            print(f"AZ Letter Proofer: saving options on close failed\n{traceback.format_exc()}")
        self._morph_closed = True
        self.w.dice_color.getNSColorWell().deactivate()  # free the shared colour panel
        # The menu targets wrap bound methods: an ObjC<->Python cycle the GC can't
        # break, which would keep every closed window (and its dice) alive.
        self.w.getNSWindow().contentView().setMenu_(None)
        self._cube_menu_target = self._color_menu_target = None
        observer = self._popup_observer
        if observer is not None:
            NSNotificationCenter.defaultCenter().removeObserver_(observer)
            self._popup_observer = None
        self._stop_glyph_tracking()
        self._remove_key_monitor()
        self._cancel_morphs()
        refresh_timer = self._letters_refresh_timer
        if refresh_timer is not None:
            with contextlib.suppress(Exception):
                refresh_timer.invalidate()
            self._letters_refresh_timer = None
        queue = self._morph_build_queue
        if queue is not None:
            queue.cancelAllOperations()  # drop any in-flight frame builds

    # --------------------------------------------- off-main frame building

    def _frame_build_queue(self):
        """Serial background queue for morph frames: the ~0.5 s CoreImage build
        never freezes typing."""
        queue = self._morph_build_queue
        if queue is None:
            queue = NSOperationQueue.alloc().init()
            queue.setMaxConcurrentOperationCount_(1)
            self._morph_build_queue = queue
        return queue

    def _build_frames_async(self, face_index, build, on_ready):
        """Run `build()` on the background queue and hand its frames (None if it
        failed) to `on_ready` on the main thread. A newer build for the same face
        supersedes this one: its result is dropped."""
        token = self._morph_gen.get(face_index, 0) + 1
        self._morph_gen[face_index] = token

        def work():
            try:
                frames = build()
            except Exception:
                frames = None
                print(f"AZ Letter Proofer async build error:\n{traceback.format_exc()}")

            def deliver():
                if self._morph_closed:
                    return  # window gone
                if self._morph_gen.get(face_index) != token:
                    return  # superseded by a newer morph on this face
                on_ready(frames)

            NSOperationQueue.mainQueue().addOperationWithBlock_(deliver)

        self._frame_build_queue().addOperationWithBlock_(work)

    @staticmethod
    def _frame_at(fraction, total):
        """Map an elapsed-time fraction (0..1) to a frame index, or None when
        the morph is over (fraction >= 1). Clamps to the last frame so the
        boundary never indexes past the end."""
        if fraction >= 1.0:
            return None
        return min(total - 1, int(fraction * total))

    def _play_emission_frames(self, face_index, frames, blank, seconds, boosts, on_done, register):
        """Play pre-built emission frames on a face: a common-modes timer (scrolling
        can't stall it) shows the frame for the elapsed time on a monotonic clock.
        `boosts` = (from, to); `on_done` lands the exact pair; `register` owns the timer."""
        total = len(frames)
        boost_a, boost_b = boosts
        clock = NSProcessInfo.processInfo()
        start = clock.systemUptime()
        shown = {"last": -1}

        def finish():
            landings = self._landings
            if landings.get(face_index, (None,))[0] is timer:
                del landings[face_index]
            on_done()

        def tick(timer):
            fraction = (clock.systemUptime() - start) / seconds if seconds > 0 else 1.0
            index = self._frame_at(fraction, total)
            if index is None:
                timer.invalidate()
                finish()
                return
            if index != shown["last"]:
                boost = boost_a + (boost_b - boost_a) * ((index + 1) / (total + 1))
                self._update_face(face_index, blank if index == 0 else None, frames[index], boost)
                shown["last"] = index

        interval = max(1.0 / 120.0, seconds / (total * 2.0))
        timer = NSTimer.timerWithTimeInterval_repeats_block_(interval, True, tick)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        register(timer)
        # One playback per face: the newest replaces one still running.
        previous = self._landings.get(face_index)
        if previous is not None:
            previous[0].invalidate()
        self._landings[face_index] = (timer, finish)

    # ------------------------------------------------- feature face morphs

    def feature_changed(self, sender=None):
        """Feature popup callback: morph the other dice faces over to the
        picked group's glyphs."""
        self._refresh_tag_members()
        self._update_language_filter()
        self._morph_dice_to_feature()
        self._update_dig_enabled()

    def _refresh_tag_members(self):
        """Cache the picked tag's glyphs (a whole-font walk) for the
        per-keystroke language filter."""
        kind, value = self._feature()
        font = Glyphs.font
        self._tag_members = (
            tuple(chars_for_tag(font, value)) if kind == "tag" and font is not None else ()
        )

    def _cancel_morphs(self):
        """Stop every pending or playing face morph and drop in-flight frame
        builds, so a full re-bake isn't overwritten by a late landing."""
        self._cancel_feature_morphs()
        timer = self._morph_timer
        if timer is not None:
            timer.invalidate()
            self._morph_timer = None
        generations = self._morph_gen
        for index in generations:
            generations[index] += 1  # _build_frames_async drops the stale result
        self._landings = {}  # every caller re-skins or tears down next

    def _cancel_feature_morphs(self):
        for timer in self._feature_timers:
            with contextlib.suppress(Exception):
                timer.invalidate()
        self._feature_timers = []
        # A playback stopped mid-way would leave its face blank and half-morphed:
        # land it on its final pair instead. Face 0's lead morph is still valid.
        landings = self._landings
        for timer, finish in list(landings.values()):
            if not timer.isValid() and not self._morph_closed:
                finish()

    def _register_feature_timer(self, timer):
        """Track a playback timer, pruning spent ones — hours of idle churn
        would otherwise retain thousands of dead timer proxies."""
        alive = []
        for old in self._feature_timers:
            try:
                if old.isValid():
                    alive.append(old)
            except Exception:
                pass
        alive.append(timer)
        self._feature_timers = alive

    def _register_lead_timer(self, timer):
        """Face 0 plays one morph at a time: a new one stops the previous."""
        previous = self._morph_timer
        if previous is not None:
            previous.invalidate()
        self._morph_timer = timer

    def _morph_dice_to_feature(self):
        """Morph the non-focus faces to glyphs of the picked tag/group —
        staggered, one face starting after another, not all at once."""
        if self._dice_node is None or self._dice_background is None:
            return
        try:
            targets = [
                char for char in self._feature_dice_chars(Glyphs.font) if dice_char_allowed(char)
            ]
        except Exception:
            targets = []
        current = self._dice_face_chars
        if not targets or not current:
            return
        first_free = max(1, len(self._dice_focus))  # the focus faces stay
        missing = [char for char in targets if char not in current][: 12 - first_free]
        replaceable = [index for index in range(first_free, 12) if current[index] not in targets]
        if not missing or not replaceable:
            return  # the dice already shows the group (e.g. right after open)
        self._schedule_face_morphs(
            [
                (face_index, current[face_index], new_char)
                for face_index, new_char in zip(replaceable, missing, strict=False)
            ]
        )

    def _schedule_face_morphs(self, assignments):
        """Run a batch of (face_index, old_char, new_char) morphs in waves:
        dice.FEATURE_MORPH_BATCH faces start together, waves dice.FEATURE_MORPH_STAGGER
        apart, faces inside a wave nudged dice.FEATURE_MORPH_INWAVE apart so the
        frame generation doesn't stall the UI in one burst."""
        self._cancel_feature_morphs()
        for position, (face_index, old_char, new_char) in enumerate(assignments):
            self._note_face_char(face_index, new_char)
            wave, slot = divmod(position, dice.FEATURE_MORPH_BATCH)
            delay = 0.05 + wave * dice.FEATURE_MORPH_STAGGER + slot * dice.FEATURE_MORPH_INWAVE

            def fire(timer, face=face_index, old=old_char, new=new_char):
                self._morph_face(Glyphs.font, face, old, new, seconds=dice.FEATURE_MORPH_SECONDS)

            self._register_feature_timer(
                NSTimer.scheduledTimerWithTimeInterval_repeats_block_(delay, False, fire)
            )

    def _morph_face(self, font, index, old_char, new_char, from_role=None, seconds=None):
        """Morph face `index` from old_char to new_char in its current role
        (_face_role): glow-only frames built off the main thread, the colour and
        boost lerping from `from_role` (default: the same role), then the exact
        engraved pair lands. Same letter, or no old one, = a short crossfade.
        Face 0 re-lights its aura when it lands."""
        try:
            role = self._face_role(index)
            color_b, boost_b, _organic = self._glow(role)
            color_a, boost_a, _organic = self._glow(from_role or role)
            face, glow = self._face_pair(font, new_char, role)
            self._note_face_char(index, new_char)

            def land(fade=0.0):
                self._update_face(index, face, glow, boost_b, fade=fade)
                if index == 0:
                    self._set_aura()

            if not old_char or old_char == new_char:
                land(0.4)
                return
            stamp_a = self._char_stamp(font, old_char, dice.MORPH_SIZE)
            stamp_b = self._char_stamp(font, new_char, dice.MORPH_SIZE)
            # Blank the diffuse while the glow morphs, so the old letter's
            # engraving doesn't ghost through the transition.
            blank = self._dice_background
            register = self._register_lead_timer if index == 0 else self._register_feature_timer

            def build():
                return dice.morph_frames(stamp_a, stamp_b, color_a, color_to=color_b)

            def on_ready(frames):
                if not frames:
                    land(0.4)
                    return
                self._play_emission_frames(
                    index,
                    frames,
                    blank,
                    seconds or dice.MORPH_SECONDS,
                    (boost_a, boost_b),
                    land,
                    register,
                )

            self._build_frames_async(index, build, on_ready)
        except Exception:
            print(f"AZ Letter Proofer morph error:\n{traceback.format_exc()}")

    def _char_stamp(self, font, char, size):
        """Stamp callable for a character: the font's own outline in the
        selected master when it has one, the bold system font otherwise."""
        glyph = font.glyphs[char] if font is not None and font.glyphs else None
        if glyph is not None:
            master = font.selectedFontMaster or font.masters[0]
            layer = glyph.layers[master.id]
            path = layer.completeBezierPath if layer is not None else None
            if path is not None and path.elementCount():
                box = layer.bounds
                bounds = (box.origin.x, box.origin.y, box.size.width, box.size.height)
                return dice.bezier_stamp(path, bounds, font.upm, size)
        return dice.text_stamp(char, size)

    def _face_pair(self, font, char, role):
        """One face's (diffuse, emission) for a character in a glow role."""
        stamp = self._char_stamp(font, char, dice.TEXTURE_SIZE)
        color, _boost, organic = self._glow(role)
        return (
            dice.face_image(stamp, self._dice_background),
            dice.emission_image(stamp, color=color, organic=organic),
        )

    @staticmethod
    def _glow(role):
        """(colour, boost, organic) of a glow role: "focus" = the letter being
        proofed, "idle" = nothing to proof (every face), "plain" = the rest.
        Read live: the colour well rewrites the dice module's glow colours."""
        if role == "focus":
            return dice.HIGHLIGHT_GLOW_COLOR, dice.HIGHLIGHT_BOOST, True
        if role == "idle":
            return dice.IDLE_GLOW_COLOR, dice.IDLE_BOOST, True
        return dice.GLOW_COLOR, dice.NON_FOCUS_BOOST, False

    def _face_role(self, index):
        if not self._dice_focus:
            return "idle"
        return "focus" if index < len(self._dice_focus) else "plain"

    def _reskin_faces(self, font, faces=None, fade=0.25):
        """Re-render faces in place, letters kept, each in its current glow role:
        after a master switch (new outlines) or a colour change (new body/glow)."""
        if self._dice_node is None or self._dice_background is None:
            return
        chars = self._dice_face_chars
        for index in range(12) if faces is None else faces:
            if index < len(chars):
                role = self._face_role(index)
                face, glow = self._face_pair(font, chars[index], role)
                self._update_face(index, face, glow, self._glow(role)[1], fade=fade)

    def _update_face(self, index, diffuse, emission, boost, fade=0.0):
        """Swap one face's textures on the dice (None leaves that map as is)."""
        if self._dice_node is not None:
            dice.update_face(self._dice_node, index, diffuse, emission, boost, fade=fade)

    def _set_aura(self):
        """Crack lights + steady glow: face 0 when focused, every face when idle."""
        if self._dice_node is not None:
            dice.set_highlight_aura(self._dice_node, idle=not self._dice_focus)

    @staticmethod
    def _selected_master_id(font):
        """Id of the font's selected master (first master as a fallback), or
        None when there's no font/master."""
        if font is None:
            return None
        master = font.selectedFontMaster or (font.masters[0] if font.masters else None)
        return master.id if master is not None else None

    def _check_master_change(self, font):
        """Re-skin the dice when the selected master changes, so the letters on
        the faces take the new master's outline. A no-op while the master is
        unchanged (the common case), so it's cheap to call every UPDATEINTERFACE."""
        master_id = self._selected_master_id(font)
        if master_id == self._tracked_master_id:
            return
        self._tracked_master_id = master_id
        if font is not None and font.glyphs:
            self._cancel_morphs()
            self._reskin_faces(font)
            self._set_aura()

    def _style_card(self, box):
        """Style a vanilla Box as a flat, rounded, filled card (no title/border),
        matching the dice card. The fill follows the appearance: CARD_BACKGROUND
        (RGB 40) in dark mode, CARD_BACKGROUND_LIGHT (RGB 228) in light mode. A
        dynamic NSColor resolves at draw time, so a theme switch repaints itself."""
        with contextlib.suppress(Exception):
            box.setBoxType_(NSBoxCustom)
            box.setTitlePosition_(NSNoTitle)
            box.setBorderWidth_(0.0)
            box.setCornerRadius_(CARD_RADIUS)
            box.setContentViewMargins_((0, 0))
            box.setFillColor_(self._card_fill())

    @staticmethod
    def _card_fill():
        """The card fill as an appearance-aware NSColor."""

        def provider(appearance):
            names = [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
            dark = appearance.bestMatchFromAppearancesWithNames_(names) == NSAppearanceNameDarkAqua
            rgb = dice.CARD_BACKGROUND if dark else dice.CARD_BACKGROUND_LIGHT
            return NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0)

        return NSColor.colorWithName_dynamicProvider_(None, provider)

    def _style_dice_overlay_button(self, button):
        """Borderless, image-only — a HUD control floating on the canvas. Inked
        with the dynamic label colour: white on the dark canvas, black on the
        light one (the baked icon is a template, so the tint applies)."""
        with contextlib.suppress(Exception):
            button.setBordered_(False)
            button.setImagePosition_(2)  # NSImageOnly
            # NONE: any scaling stretches the template SF Symbol to fill the button.
            button.setImageScaling_(NS_IMAGE_SCALE_NONE)
            button.setContentTintColor_(NSColor.labelColor())

    @staticmethod
    def _overlay_icon(name):
        """SF Symbol `name` (pause / play) baked into a DICE_ICON_BOX bitmap: a
        template symbol would be stretched to fill the button."""
        config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            float(DICE_ICON_BOX), SYMBOL_WEIGHT_REGULAR
        )
        symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        symbol = symbol.imageWithSymbolConfiguration_(config)
        box = DICE_ICON_BOX
        glyph = symbol.size()
        origin_x, origin_y = (box - glyph.width) / 2, (box - glyph.height) / 2
        image = NSImage.alloc().initWithSize_((box, box))
        image.lockFocus()
        symbol.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            NSMakeRect(origin_x, origin_y, glyph.width, glyph.height),
            NSZeroRect,
            NSCompositingOperationSourceOver,
            1.0,
            True,
            None,
        )
        NSColor.whiteColor().set()  # flatten the drawn alpha to one colour
        NSRectFillUsingOperation(NSMakeRect(0, 0, box, box), NSCompositingOperationSourceAtop)
        image.unlockFocus()
        image.setTemplate_(True)  # only the alpha counts; the button tints it
        return image

    def _sync_tumble_button(self):
        """Point the button icon (and its tooltip) at the CURRENT state."""
        button = self.w.dice_stop.getNSButton()
        button.setImage_(self._overlay_icon("pause.fill" if self._tumbling else "play.fill"))
        button.setToolTip_("Freeze the dice" if self._tumbling else "Resume tumbling")

    def _set_tumbling(self, on):
        """Freeze the dice where it is (on=False) or resume the tumble from
        there, and sync the button. Idempotent."""
        self._tumbling = bool(on)
        if self._dice_node is not None:
            (dice.start_tumble if self._tumbling else dice.stop_tumble)(self._dice_node)
        self._sync_tumble_button()

    def toggle_tumble(self, sender=None):
        """Freeze / resume the idle rotation on every live dice node, in place."""
        try:
            self._set_tumbling(not self._tumbling)
        except Exception:
            print(f"AZ Letter Proofer tumble toggle error:\n{traceback.format_exc()}")

    def is_open(self) -> bool:
        return getattr(self.w, "_window", None) is not None

    def bring_to_front(self):
        self.w.show()
        self.w.makeKey()

    # -------------------------------------------------------- position

    def _one_per_line(self) -> bool:
        """Tab layout: one word per line, else a running line of words."""
        return bool(self.w.one_per_line.get())

    def _current_case(self) -> Case:
        """Capitalize ticked -> Capitalized words. Otherwise Case.AUTO, which the
        engine already resolves PER typed letter (case_of): a lowercase letter
        proofs lowercase words, an uppercase one UPPERCASE."""
        return Case.CAPITALIZE if self.w.capitalize.get() else Case.AUTO

    def _current_positions(self):
        """The Position segment (0 start, 1 contain) as a one-item list."""
        if self.w.position.get() == 0:
            return [Position.START]
        self.w.position.set(1)  # nothing picked: contain
        return [Position.CONTAIN]

    # ------------------------------------------------------------ prefs

    def _load_prefs(self):
        def get(key):
            return Glyphs.defaults[PREFS + key]

        def number(key, cast, fallback):
            with contextlib.suppress(TypeError, ValueError):
                return cast(get(key))
            return fallback  # hand-edited junk: the default, not a dead window

        self.w.letters.set(str(get("letters") or ""))
        words = max(1, min(10, number("results", int, 10)))  # stepper range is 1..10
        self.w.results.set(str(words))
        self.w.results_stepper.set(words)
        self.w.position.set(0 if "start" in (get("positions") or ()) else 1)
        self.w.capitalize.set(bool(number("capitalize", int, 0)))
        self._rebuild_feature_popup(Glyphs.font)
        self._set_selected_feature(get("group"))
        self._refresh_tag_members()
        for name in SLIDER_NAMES:
            getattr(self.pop, name).set(number(name, float, 0.0))
        self.w.smart.set(bool(get("smart")))
        self.w.unique.set(bool(get("unique")))
        self.w.all_letters.set(bool(get("allLetters")))
        self.w.current_tab.set(bool(get("currentTab")))
        self.w.one_per_line.set(bool(number("oneWordPerLine", int, 0)))
        # Bundled languages are on unless unticked; outside lists off unless ticked.
        unchecked = set(Glyphs.defaults[PREFS + "uncheckedLanguages"] or ())
        checked_lists = set(Glyphs.defaults[PREFS + "checkedWordlists"] or ())
        outside = {MAC_DICTIONARY, self._custom_list}
        for name in self._lang_use:  # hidden languages' state too
            self._lang_use[name] = (
                name in checked_lists if name in outside else name not in unchecked
            )
        for item in self.w.languages:
            item["use"] = self._lang_use[item["language"]]
        self._update_lang_toggle()
        self._update_language_filter()  # apply the filter for the restored letter
        self._update_dice_highlight()
        self._update_dig_enabled()

    def smart_changed(self, sender=None):
        """The sliders only steer Smart ranking: grey them out without it."""
        smart = bool(self.w.smart.get())
        for name in SLIDER_NAMES:
            getattr(self.pop, name).enable(smart)

    def _save_prefs(self, positions):
        values = {
            "letters": self.w.letters.get(),
            "results": self._int_field(self.w.results, 10),
            "positions": [position.value for position in positions],
            "capitalize": int(bool(self.w.capitalize.get())),
            "group": self._selected_feature(),
            **{name: getattr(self.pop, name).get() for name in SLIDER_NAMES},
            "unique": bool(self.w.unique.get()),
            "allLetters": bool(self.w.all_letters.get()),
            "smart": bool(self.w.smart.get()),
            "currentTab": bool(self.w.current_tab.get()),
            "oneWordPerLine": int(bool(self.w.one_per_line.get())),
        }
        for key, value in values.items():
            Glyphs.defaults[PREFS + key] = value
        # Persist from _lang_use (the whole roster), so a language currently
        # filtered out of the visible list keeps its unchecked state.
        for item in self.w.languages:
            self._lang_use[item["language"]] = item["use"]
        unchecked = [name for name, use in self._lang_use.items() if not use]
        Glyphs.defaults[PREFS + "uncheckedLanguages"] = unchecked
        Glyphs.defaults[PREFS + "checkedWordlists"] = [
            name for name in (MAC_DICTIONARY, self._custom_list) if self._lang_use.get(name)
        ]

    # -------------------------------------------------------- callbacks

    def toggle_languages(self, sender=None):
        any_unchecked = any(not item["use"] for item in self.w.languages)
        for item in self.w.languages:
            item["use"] = any_unchecked
        self._update_lang_toggle()

    def add_wordlist(self, sender=None):
        """Add the user's own word list beside the languages, ticked. One at a time:
        a new pick replaces the previous one."""
        picked = GetOpenFile(
            message="Choose a word list: a .txt file with one word per line.",
            filetypes=["txt"],
        )
        if not picked:
            return
        path = Path(str(picked))
        if self._custom_list:
            self.source.remove_wordlist(self._custom_list)
            self._lang_use.pop(self._custom_list, None)
        self.source.add_wordlist(path.name, path)
        if not self.source.words(path.name):
            self.source.remove_wordlist(path.name)
            self._custom_list = None
            Glyphs.defaults[PREFS + "customWordlist"] = None
            Message(title="AZ Letter Proofer", message=f"No words found in {path.name}.")
        else:
            self._custom_list = path.name
            self._lang_use[path.name] = True
            Glyphs.defaults[PREFS + "customWordlist"] = str(path)
        self._base_languages = list(self.source.languages)
        self._lang_filter_names = None  # rebuild the rows
        self._update_language_filter()

    def _update_lang_toggle(self):
        any_unchecked = any(not item["use"] for item in self.w.languages)
        self.w.lang_toggle.setTitle("Select all" if any_unchecked else "Clear all")

    def open_score_popover(self, sender=None):
        self.smart_changed()  # the popover is transient: toggling Smart closes it
        self.pop.open(parentView=self.w.sliders_gear.getNSButton(), preferredEdge="right")

    def _int_field(self, field, fallback):
        try:
            return int(str(field.get()).strip())
        except ValueError:
            field.set(str(fallback))
            return fallback

    def _selected_languages(self):
        return tuple(item["language"] for item in self.w.languages if item["use"])

    def _language_charset(self, name):
        """Case-folded characters a language uses: its hyperglot orthography (rare
        diacritics the top-300 words miss) plus the corpus letters of the list's own
        script (Serbian: Cyrillic orthography, Latin list); else the corpus alone."""
        corpus = self.source.charset_for(name)
        ortho = self.source.orthography_chars(name)
        if ortho is None:
            return {char.casefold() for char in corpus}
        script = self.source.dominant_script((name,))
        return ortho | {char.casefold() for char in corpus if script_of(char) == script}

    def _covering_languages(self, names, letters):
        """From `names`, keep the languages whose alphabet covers EVERY tested
        letter (case-folded). No letters -> all; an empty result -> all (a letter
        no bundled language has must not blank the whole list)."""
        targets = {char.casefold() for char in letters if char.isalpha()}
        if not targets:
            return list(names)
        covering = [name for name in names if targets <= self._language_charset(name)]
        return covering or list(names)

    def _update_language_filter(self):
        """Show only the languages that cover the tested letter(s); the full
        (font-supported) roster returns when the Letter field clears. Each
        language's checkbox survives the rebuild via _lang_use. Cheap per
        keystroke: charset_for is cached, and the rows only rebuild when the
        covering set actually changes."""
        if self._base_languages is None:
            return
        # Snapshot the visible checkboxes before we (maybe) rebuild the rows.
        for item in self.w.languages:
            self._lang_use[item["language"]] = item["use"]
        members = self._tag_members
        if members and not self._typed_letters():
            # A tag proofs each member on its own: keep languages that have ANY.
            targets = {member.casefold() for member in members if member.isalpha()}
            names = [
                name for name in self._base_languages if targets & self._language_charset(name)
            ] or list(self._base_languages)
        else:
            names = self._covering_languages(self._base_languages, self._effective_letters())
        if names == self._lang_filter_names:
            return
        self._lang_filter_names = names
        self.w.languages.set(
            [{"use": self._lang_use.get(name, True), "language": name} for name in names]
        )
        self._update_lang_toggle()

    # ------------------------------------------------------ feature popup

    def _feature_items(self, font):
        """(kind, value) options + display items for the Feature popup.
        Two data sources: the hand-encoded groups from ratings.json, and
        the font's own glyph tags (AZ Classifier writes those, so its curated
        clusters show up here; any custom tag does too)."""
        options = [("none", "")]
        items = ["None"]
        for name in self.group_names:
            options.append(("group", name))
            items.append(name[:1].upper() + name[1:])  # "DNA" stays DNA
        if font is not None:
            for title in font_tag_titles(font):
                options.append(("tag", title))
                items.append(f"Tag: {title}")
        return options, items

    def _rebuild_feature_popup(self, font):
        """Refresh the popup when the font's tag set changed; keeps the
        current selection when it still exists."""
        options, items = self._feature_items(font)
        if options == self._feature_options:
            return
        selected = self._selected_feature()
        self._feature_options = options
        self.w.group.setItems(items)
        self._set_selected_feature(selected)

    def _feature(self):
        """The Feature popup's pick as (kind, value); kind is none, group or tag."""
        index = self.w.group.get()
        if 0 <= index < len(self._feature_options):
            return self._feature_options[index]
        return ("none", "")

    def _selected_feature(self):
        """The pick as a stable "kind:value" token, for prefs."""
        return ":".join(self._feature())

    def _set_selected_feature(self, token):
        kind, _, value = str(token).partition(":")
        for index, (option_kind, option_value) in enumerate(self._feature_options):
            if option_kind == kind and option_value == value:
                self.w.group.set(index)
                return
        self.w.group.set(0)

    def _tag_feature_chars(self, title, chars):
        """Folded characters of the tag's member glyphs (`chars`)."""
        if not chars:
            Message(
                title="AZ Letter Proofer",
                message=f"No encoded letters carry the tag '{title}'.",
            )
            return frozenset()
        return frozenset(fold_char(char) for char in chars)

    def _update_feature_popup(self, sender=None):
        """Keep the Feature popup's options in step with the current font's tags.
        Runs when the popup opens (see _build_window), on prefs load and on dig —
        not per keystroke: the tag walk visits every glyph."""
        try:
            self._rebuild_feature_popup(Glyphs.font)
        except Exception:
            print(f"AZ Letter Proofer feature popup error:\n{traceback.format_exc()}")

    # -------------------------------------------------------------- run

    def run(self, sender=None):
        try:
            self._run()
        except Exception:
            Glyphs.showMacroWindow()
            print(f"AZ Letter Proofer error:\n{traceback.format_exc()}")

    def _run(self):
        font = Glyphs.font
        self._name_chars = {}  # a dig reads names fresh, like the glyph below
        unknown = unknown_glyph_names(str(self.w.letters.get() or ""), self._char_for_name)
        if unknown:
            Message(
                title="AZ Letter Proofer",
                message=(
                    f"No encoded glyph named {', '.join(unknown)}. Type letters or "
                    "glyph names separated by spaces, like “a b” or “eacute b”."
                ),
            )
            return
        typed = self._typed_letters()
        # Nothing typed: the edited glyph, read fresh (a dig is one-shot).
        letters = typed or self._current_glyph_letters(font)

        languages = self._selected_languages()
        if not languages:
            Message(
                title="AZ Letter Proofer",
                message="No language selected — check at least one in the list.",
            )
            return

        positions = self._current_positions()
        self._rebuild_feature_popup(font)
        kind, value = self._feature()
        preferred_groups = ()
        preferred_chars = frozenset()
        tag_members = []
        if kind == "group":
            preferred_groups = (value,)
        elif kind == "tag":
            if font is None:
                Message(title="AZ Letter Proofer", message="No font open — tag feature skipped.")
            else:
                tag_members = chars_for_tag(font, value)
                preferred_chars = self._tag_feature_chars(value, tag_members)
                if not typed and tag_members:
                    # Empty Letter field + tag: proof the whole group.
                    letters = tuple(tag_members)
        # A tag proofs one bucket per member; several typed glyphs proof together.
        group = tuple(tag_members) if not typed else ()
        together = len(letters) > 1 and not group

        config = SearchConfig(
            letters=letters,
            all_letters=together,
            most_letters=not self.w.all_letters.get(),
            languages=languages,
            results_per_letter=max(1, self._int_field(self.w.results, 10)),
            positions=tuple(positions),
            case=self._current_case(),
            preferred_groups=preferred_groups,
            preferred_chars=preferred_chars,
            unique_letters=bool(self.w.unique.get()),
            charset=font_characters(font),
            smart=bool(self.w.smart.get()),
            weights=ScoreWeights(**{name: getattr(self.pop, name).get() for name in SLIDER_NAMES}),
        )
        self._save_prefs(positions)

        results = find_words(config, self.source, self.ratings)
        if together and not results[0].words:
            Message(
                title="AZ Letter Proofer",
                message=f"No word holds all of {' '.join(letters)}. Turn off “All letters "
                "in each word” for words holding as many as possible, or proof fewer glyphs.",
            )
            return
        extra_words = []
        note = ""
        if group:
            empty_members = [
                member
                for member in group
                if not any(bucket.words for bucket in results if bucket.letter == member)
            ]
            if empty_members:
                extra_words = [
                    string for member in empty_members for string in self._spacing_strings(member)
                ]
                note = f"no words for {' '.join(empty_members)} — spacing strings appended"
        self._render(results, extra_words=extra_words, note=note, focus=letters or group)

    def _spacing_strings(self, member):
        """Classic control strings for letters the wordlists can't cover."""
        if case_of(member) == "uppercase":
            return [f"HH{member}HH", f"OO{member}OO"]
        return [f"nn{member}nn", f"oo{member}oo"]

    def _render(self, results, extra_words=(), note="", focus=()):
        """Send the found words straight to an Edit tab (no output box)."""
        separator = "\n" if self._one_per_line() else " "
        words = list(dict.fromkeys(word for bucket in results for word in bucket.words))
        if not words and not extra_words:
            Message(
                title="AZ Letter Proofer",
                message="No words found. Try loosening the settings.",
            )
            return
        font = Glyphs.font
        if font is None:
            Message(title="AZ Letter Proofer", message="Open a font first.")
            return
        if note:
            print(f"AZ Letter Proofer: {note}")
        text = separator.join(words + list(extra_words))
        # The proofed glyph(s), read BEFORE the tab changes the selection.
        focus = tuple(focus) or self._typed_letters() or self._current_glyph_letters(font)
        if self.w.current_tab.get() and font.currentTab is not None:
            tab = font.currentTab
            tab.text = text
        else:
            tab = font.newTab(text) or font.currentTab
        self._cursor_onto(tab, font, focus)

    @staticmethod
    def _cursor_onto(tab, font, chars):
        """Put the Edit-view caret, nothing selected, on the first proofed glyph so
        it is the active layer. Silent when nothing matches."""
        names = set()
        for char in chars:
            with contextlib.suppress(Exception):
                glyph = font.glyphs[char]
                if glyph is not None:
                    names.add(glyph.name)
        if tab is None or not names:
            return
        try:
            # G4: tab.layers rebuilds the whole list on every index; cachedLayers is O(1).
            entries = tab.graphicView().layoutManager().cachedLayers()
            layers = [entries.objectAtIndex_(i) for i in range(entries.count())]
        except Exception:
            layers = list(tab.layers)
        for index, layer in enumerate(layers):
            glyph = getattr(layer, "parent", None)
            if glyph is not None and getattr(glyph, "name", None) in names:
                with contextlib.suppress(Exception):
                    tab.graphicView().setSelectedRange_((index, 0))
                return

    def dig_tag(self, title):
        """AZ Classifier's bridge: arm a tag group in the Feature popup and leave the
        dig to the user. The Letter field is emptied: typed members would proof
        together, while an empty field fans out one bucket per member."""
        font = Glyphs.font
        self._rebuild_feature_popup(font)
        chars = chars_for_tag(font, title) if font is not None else []
        if not chars:
            Message(
                title="AZ Letter Proofer",
                message=(
                    f"No encoded glyphs carry the tag '{title}' yet — lock the "
                    "cluster in AZ Classifier (or write its tags) first."
                ),
            )
            return
        self._set_selected_feature(f"tag:{title}")
        self._write_letters("")
        self.letters_changed()
        # group.set() doesn't fire the popup callback: run it so the dice morphs.
        self.feature_changed()
