"""AZ Letter Proofer's tab in Glyphs ▸ Settings ▸ AZ Toolkit: show or hide the
3D dice and pick its colour, the same choices as the window's right-click menu.

Every control writes the window's own preferences (keys below), and
an open window follows at once; the window mirrors its right-click changes back
here through sync_pane(). The pane lands as a tab in the suite's shared "AZ
Toolkit" Settings entry (the hub below); if no hub can be built it registers on
its own.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

import objc
import vanilla
from AppKit import (
    NSApplication,
    NSAttributedString,
    NSColor,
    NSColorSpace,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageSymbolConfiguration,
    NSMakeRange,
    NSMakeRect,
    NSMakeSize,
    NSMutableAttributedString,
    NSNoTabsNoBorder,
    NSPopUpButton,
    NSTabView,
    NSTabViewItem,
    NSTextAttachment,
    NSThread,
    NSUserDefaults,
    NSView,
    NSViewController,
    NSViewHeightSizable,
    NSViewMaxXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
)
from GlyphsApp import Glyphs

# Preference keys and defaults, shared with the window (azletterproofer_ui).
PREFS = "com.hugojourdan.AZLetterProofer."

DEFAULTS = {
    "letters": "a",
    "results": 10,
    "positions": ["contain"],
    "capitalize": 0,
    "group": "none:",
    "diversity": 0.0,
    "letter_frequency": 0.0,
    "mark_frequency": 0.0,
    "texture": 0.0,
    "unique": False,
    "allLetters": True,  # several letters: words holding them all (off: as many as possible)
    "smart": True,
    "currentTab": False,
    "oneWordPerLine": False,
    "showExtraSettings": False,  # the Extra settings rows start folded
    "showDice": True,  # show the 3D dice canvas (hide -> window reflows up)
    "diceColor": "#9A9A9A",  # neutral grey (HSB 0/0/60): grey body, white-ish glows
}


def register_defaults() -> None:
    for key, value in DEFAULTS.items():
        Glyphs.registerDefault(PREFS + key, value)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    red, green, blue = (max(0, min(255, round(channel * 255))) for channel in rgb)
    return f"#{red:02X}{green:02X}{blue:02X}"


def hex_to_rgb(text: object) -> tuple[float, ...] | None:
    """'#RRGGBB' (or 'RRGGBB') -> (r, g, b) floats in 0..1, or None if junk."""
    digits = str(text).lstrip("#").strip()
    if len(digits) != 6:
        return None
    try:
        return tuple(int(digits[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return None


def save_dice_color(ns_color: Any) -> bool:
    """Persist a colour-well pick as #RRGGBB. A pattern / image swatch has no
    RGB: ignored, returns False."""
    converted = ns_color.colorUsingColorSpace_(NSColorSpace.genericRGBColorSpace())
    if converted is None:
        return False
    rgb = (converted.redComponent(), converted.greenComponent(), converted.blueComponent())
    Glyphs.defaults[PREFS + "diceColor"] = rgb_to_hex(rgb)
    return True


# Hold the controller + view for the app's lifetime so AppKit can't dealloc the
# pane out from under the Settings window.
_PANE_ADDED = False
_PANE_VC = None
_PANE_VIEW = None

_PANE_IDENTIFIER = "AZ Letter Proofer"


class AZLetterProoferSettingsView:
    """Builds the vanilla pane; edits go to the window's preferences."""

    WIDTH = 300
    HEIGHT = 84
    MARGIN = 14
    WELL_W = 44

    def __init__(self, live_window: Callable[[], Any]) -> None:
        self._live_window = live_window
        self._build()
        self.load()

    @property
    def view(self) -> object:
        return self.group.getNSView()

    def _build(self) -> None:
        margin = self.MARGIN
        group = vanilla.Group((0, 0, self.WIDTH, self.HEIGHT))
        # Frame first: vanilla lays children out against the parent's current height.
        group.getNSView().setFrame_(NSMakeRect(0, 0, self.WIDTH, self.HEIGHT))

        group.showDice = vanilla.CheckBox(
            (margin, 14, -margin, 20), "Show 3D Cube", callback=self._show_dice_changed
        )
        group.colorLabel = vanilla.TextBox((margin, 50, 120, 18), "Dice colour")
        group.color = vanilla.ColorWell(
            (self.WIDTH - margin - self.WELL_W, 46, self.WELL_W, 24),
            callback=self._color_changed,
        )
        self.group = group

    def load(self) -> None:
        """Controls from the saved preferences. The well is left alone while
        it drives the colour panel, or a drag would fight itself."""
        self.group.showDice.set(bool(Glyphs.defaults[PREFS + "showDice"]))
        well = self.group.color
        if not well.getNSColorWell().isActive():
            saved = Glyphs.defaults[PREFS + "diceColor"]
            rgb = hex_to_rgb(saved) or hex_to_rgb(DEFAULTS["diceColor"])  # junk -> default
            if rgb is not None:
                well.set(NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, 1.0))

    def _show_dice_changed(self, sender: Any) -> None:
        show = bool(sender.get())
        Glyphs.defaults[PREFS + "showDice"] = show
        window = self._live_window()
        if window is not None:
            window.set_show_dice(show)

    def _color_changed(self, sender: Any) -> None:
        try:
            if save_dice_color(sender.get()):
                window = self._live_window()
                if window is not None:
                    window.dice_color_saved()
        except Exception:
            traceback.print_exc()


class AZLetterProoferPreferencesController(NSViewController):
    """NSViewController wrapper that gives the pane a fixed size."""

    def initWithSettingsView_(self, settings_view: AZLetterProoferSettingsView) -> object:
        self = objc.super(AZLetterProoferPreferencesController, self).init()
        if self is None:
            return None
        self._settings_view = settings_view
        # In init, not only loadView: the hub reads the size BEFORE it asks for the view.
        self._apply_size()
        return self

    def loadView(self) -> None:
        try:
            self.setView_(self._settings_view.view)
            self._apply_size()
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _apply_size(self) -> None:
        try:
            view = self._settings_view
            self.setPreferredContentSize_(NSMakeSize(float(view.WIDTH), float(view.HEIGHT)))
        except Exception:
            traceback.print_exc()


# ---- AZ Toolkit shared Settings hub (protocol v1) ----------------------
# One "AZ Toolkit" entry in Glyphs > Settings, each AZ plugin's pane a tab in
# it (a popup over a tabless NSTabView). The first plugin to load creates the
# hub and parks it in the main thread's dictionary under _HUB_KEY.
_HUB_KEY = "AZToolkit.SettingsHub.v1"
_HUB_TITLE = "AZ Toolkit"
_HUB_BADGE = "\U001001fb"  # SF Symbol checkmark seal, one astral codepoint
_POPUP_BAR_H = 41.0  # 10 top + 25 popup + 6 gap


class AZLetterProoferToolkitHub(NSViewController):
    def init(self) -> object:
        self = objc.super(AZLetterProoferToolkitHub, self).init()
        if self is None:
            return None
        self._panes = []
        self.setTitle_(_HUB_TITLE)
        self.setRepresentedObject_(_HUB_TITLE)
        self.setPreferredContentSize_(NSMakeSize(360.0, 240.0))
        return self

    def loadView(self) -> None:
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 240))
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(12, 240 - 35, 360 - 24, 25), False
        )
        popup.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        popup.setTarget_(self)
        popup.setAction_("azToolkitPopupChanged:")
        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 240 - _POPUP_BAR_H))
        tabs.setTabViewType_(NSNoTabsNoBorder)
        tabs.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        root.addSubview_(popup)
        root.addSubview_(tabs)
        self._popup = popup
        self._tabs = tabs
        self.setView_(root)

    def azToolkitPopupChanged_(self, sender: Any) -> None:
        try:
            self._tabs.selectTabViewItemAtIndex_(sender.indexOfSelectedItem())
        except Exception:
            pass

    # Glyphs 4's Settings sidebar reads GSPreferenceViewControllerProtocol
    # off each pane: required -imageName, optional -image (both guarded).
    def imageName(self) -> str:
        return "wrench.and.screwdriver"

    def image(self) -> object:
        symbol_ctor = getattr(NSImage, "imageWithSystemSymbolName_accessibilityDescription_", None)
        if symbol_ctor is None:  # pre-macOS 11: no SF Symbols
            return None
        for name in ("wrench.and.screwdriver", "gearshape.2", "hammer"):
            image = symbol_ctor(name, _HUB_TITLE)
            if image is not None:
                image.setTemplate_(True)
                return image
        return None

    def addSettingsPane_(self, pane: Any) -> None:
        try:
            self.view()  # force loadView so _popup/_tabs exist
            tabs = self._tabs
            ident = pane.representedObject() or pane.title() or ""
            for index in range(tabs.numberOfTabViewItems()):
                if tabs.tabViewItemAtIndex_(index).identifier() == ident:
                    return  # already registered (e.g. two document windows)
            title = pane.title() or str(ident)
            label = title[3:] if title.startswith("AZ ") else title
            size = pane.preferredContentSize()
            view = pane.view()
            if size.width < 1 or size.height < 1:
                size = view.frame().size
            # Pin the pane at its designed size, top-left; the flexible
            # right/bottom margins absorb the stretch NSTabView applies.
            container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, size.width, size.height))
            view.setFrame_(NSMakeRect(0, 0, size.width, size.height))
            view.setAutoresizingMask_(NSViewMaxXMargin | NSViewMinYMargin)
            container.addSubview_(view)
            item = NSTabViewItem.alloc().initWithIdentifier_(ident)
            item.setLabel_(label)
            item.setView_(container)
            insert_at = tabs.numberOfTabViewItems()
            for index in range(tabs.numberOfTabViewItems()):
                if label < str(tabs.tabViewItemAtIndex_(index).label()):
                    insert_at = index
                    break
            tabs.insertTabViewItem_atIndex_(item, insert_at)
            # Popup shows the FULL plugin name ("AZ Colorflow"), not the
            # stripped tab label — the sort above still uses the label.
            self._popup.insertItemWithTitle_atIndex_(title, insert_at)
            if self._pane_licensed(pane):
                text = NSMutableAttributedString.alloc().initWithString_attributes_(
                    title + "  ",
                    {NSFontAttributeName: NSFont.menuFontOfSize_(0)},
                )
                text.appendAttributedString_(self._badge_string())
                self._popup.itemAtIndex_(insert_at).setAttributedTitle_(text)
            self._panes.append(pane)  # retain: AppKit must not dealloc it
            self._popup.selectItemAtIndex_(tabs.indexOfTabViewItem_(tabs.selectedTabViewItem()))
            self._recalc_preferred_size()
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _badge_string(self) -> object:
        """The licensed badge, appended after the plugin name.

        Preferred: checkmark.seal.fill as a PALETTE symbol image (white
        checkmark inside an accent-colored seal, macOS 12+) attached
        inline via NSTextAttachment — plain text runs render SF symbols
        monochrome, so the checkmark can't be white there. Fallback:
        the accent-colored 􀇻 glyph as text (checkmark = knocked out).
        """
        accent = getattr(NSColor, "controlAccentColor", None)
        color = accent() if accent is not None else NSColor.systemBlueColor()
        try:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "checkmark.seal.fill", "licensed"
            )
            size_conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                12.0, NSFontWeightRegular
            )
            palette = NSImageSymbolConfiguration.configurationWithPaletteColors_(
                [NSColor.whiteColor(), color]
            )
            image = image.imageWithSymbolConfiguration_(
                size_conf.configurationByApplyingConfiguration_(palette)
            )
            attachment = NSTextAttachment.alloc().init()
            attachment.setImage_(image)
            size = image.size()
            font = NSFont.menuFontOfSize_(0)
            # center the seal on the cap height of the menu font
            offset_y = round((font.capHeight() - size.height) / 2.0)
            attachment.setBounds_(NSMakeRect(0, offset_y, size.width, size.height))
            return NSAttributedString.attributedStringWithAttachment_(attachment)
        except Exception:
            pass
        text = NSMutableAttributedString.alloc().initWithString_attributes_(
            _HUB_BADGE, {NSFontAttributeName: NSFont.menuFontOfSize_(0)}
        )
        text.addAttribute_value_range_(
            NSForegroundColorAttributeName, color, NSMakeRange(0, text.length())
        )
        return text

    @objc.python_method
    def _pane_licensed(self, pane: Any) -> bool:
        """Protocol v1.1: a pane MAY expose azLicensed (bought, not trial).
        Debug: the AZToolkitFakeLicensed defaults key badges EVERY pane."""
        try:
            if NSUserDefaults.standardUserDefaults().boolForKey_("AZToolkitFakeLicensed"):
                return True
            if pane.respondsToSelector_(b"azLicensed"):
                return bool(pane.valueForKey_("azLicensed"))
        except Exception:
            pass
        return False

    def showSettingsPaneWithIdentifier_(self, identifier: object) -> None:
        try:
            self.view()
            tabs = self._tabs
            for index in range(tabs.numberOfTabViewItems()):
                if tabs.tabViewItemAtIndex_(index).identifier() == identifier:
                    tabs.selectTabViewItemAtIndex_(index)
                    self._popup.selectItemAtIndex_(index)
                    break
        except Exception:
            pass
        try:
            delegate = NSApplication.sharedApplication().delegate()
            if delegate is not None and delegate.respondsToSelector_(
                b"showMainPreferencesWithIdentifier:"
            ):
                delegate.showMainPreferencesWithIdentifier_(_HUB_TITLE)
        except Exception:
            pass

    @objc.python_method
    def _recalc_preferred_size(self) -> None:
        try:
            max_width, max_height = 300.0, 200.0
            for pane in self._panes:
                size = pane.preferredContentSize()
                if size.width < 1 or size.height < 1:
                    size = pane.view().frame().size
                max_width = max(max_width, size.width)
                max_height = max(max_height, size.height)
            self.setPreferredContentSize_(NSMakeSize(max_width, max_height + _POPUP_BAR_H))
        except Exception:
            traceback.print_exc()


def _toolkit_hub() -> Any:
    """Find-or-create the shared AZ Toolkit hub (None if it can't be hosted)."""
    try:
        pool = NSThread.mainThread().threadDictionary()
        hub = pool.objectForKey_(_HUB_KEY)
        if hub is not None:
            return hub
        delegate = NSApplication.sharedApplication().delegate()
        if delegate is None or not delegate.respondsToSelector_(b"addViewToPreferences:"):
            return None
        hub = AZLetterProoferToolkitHub.alloc().init()
        if hub is None:
            return None
        pool.setObject_forKey_(hub, _HUB_KEY)  # retained here for the app's lifetime
        delegate.addViewToPreferences_(hub)
        return hub
    except Exception:
        traceback.print_exc()
        return None


def sync_pane() -> None:
    """Re-read the preferences into the pane (the window changed them)."""
    if _PANE_VIEW is not None:
        _PANE_VIEW.load()


def register_preferences_pane(live_window: Callable[[], Any]) -> None:
    """Add the AZ Letter Proofer pane to Glyphs > Settings, once per app session.

    The pane lands as a tab in the shared "AZ Toolkit" entry (see the hub above);
    if the hub can't be built, fall back to registering a standalone pane.
    ``live_window`` returns the open proofer window, or None.
    """
    global _PANE_ADDED, _PANE_VC, _PANE_VIEW
    if _PANE_ADDED:
        return
    try:
        delegate = NSApplication.sharedApplication().delegate()
    except Exception:
        return
    if delegate is None or not delegate.respondsToSelector_(b"addViewToPreferences:"):
        return
    try:
        register_defaults()  # the pane reads them before any window registers them
        settings_view = AZLetterProoferSettingsView(live_window)
        controller = AZLetterProoferPreferencesController.alloc().initWithSettingsView_(
            settings_view
        )
        if controller is None:
            return
        controller.setTitle_(_PANE_IDENTIFIER)
        controller.setRepresentedObject_(_PANE_IDENTIFIER)
        _PANE_ADDED = True
        _PANE_VIEW = settings_view
        _PANE_VC = controller
        hub = _toolkit_hub()
        if hub is not None and hub.respondsToSelector_(b"addSettingsPane:"):
            hub.addSettingsPane_(controller)
        else:
            delegate.addViewToPreferences_(controller)  # solo fallback
    except Exception:
        traceback.print_exc()
