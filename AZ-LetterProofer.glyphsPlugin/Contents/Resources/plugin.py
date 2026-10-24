"""AZ Letter Proofer — digs up optimal test words for type design.

General Plugin shell: adds a Window ▸ AZ Toolkit item (with the bundled
assets/az-icon.pdf art) that opens the AZ Letter Proofer window. All real work lives
in azletterproofer_ui (window/Glyphs glue) and azletterproofer_engine (pure
scoring logic).
"""

import traceback

import objc
from AppKit import NSMenuItem
from GlyphsApp import WINDOW_MENU, Glyphs
from GlyphsApp.plugins import GeneralPlugin

MENU_ICON_HEIGHT = 16.0  # points; standard macOS menu-item icon height


# ──────────────────────────────────────────────────────────────────────────────
# The plugin's face
# ──────────────────────────────────────────────────────────────────────────────
# Template image: the black art is inked with the text colour, so it reads in
# light mode, dark mode and on a highlighted row.

AZ_ICON_FILE = "assets/az-icon.pdf"
_AZ_ICON_CACHE: dict = {}


def _az_icon(point_size: float = 14.0):
    """The bundled plugin icon as a template NSImage `point_size` tall, or None.

    Cached per size: an NSImage is mutable, so two sizes can't share one object.
    """
    cached = _AZ_ICON_CACHE.get(point_size)
    if cached is not None:
        return cached
    try:
        import os

        from AppKit import NSImage

        # The art sits in the bundle's Resources/, but the module asking for it
        # may be a package DEEPER than that (azkeyring/sidebar.py), so walk up.
        folder = os.path.dirname(os.path.abspath(__file__))
        path = None
        for _ in range(3):
            candidate = os.path.join(folder, AZ_ICON_FILE)
            if os.path.exists(candidate):
                path = candidate
                break
            folder = os.path.dirname(folder)
        if path is None:
            return None
        image = NSImage.alloc().initByReferencingFile_(path)
        if image is None or not image.isValid():
            return None
        # size() is an NSSize struct, not a tuple.
        natural = image.size()
        if natural.height:
            image.setSize_((natural.width * point_size / natural.height, point_size))
        image.setTemplate_(True)
        _AZ_ICON_CACHE[point_size] = image
        return image
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Window ▸ AZ Toolkit
# ──────────────────────────────────────────────────────────────────────────────
# One "AZ Toolkit" item in the Window menu, every AZ plugin a child ("AZ "
# dropped), the site link last. The first plugin to load builds it; the rest
# insert themselves alphabetically.

AZ_TOOLKIT_MENU_TITLE = "AZ Toolkit"
AZ_TOOLKIT_SITE_TITLE = "More AZ plugins…"
AZ_TOOLKIT_SITE_URL = "https://azerty-typefaces.com"
# NSMenuItem doesn't retain its target: the link's opener lives on the main
# thread's dictionary, which lasts as long as the app.
AZ_TOOLKIT_OPENER_KEY = "AZToolkit.WindowMenu.SiteOpener.v1"


def _az_toolkit_title(name: str) -> str:
    """A child item's label: the plugin name without its "AZ " prefix."""
    return name[3:] if name.startswith("AZ ") else name


def _az_toolkit_site_opener():
    """The retained object behind "More AZ plugins…". One per app session."""
    import objc
    from AppKit import NSWorkspace
    from Foundation import NSObject, NSThread, NSURL

    store = NSThread.mainThread().threadDictionary()
    opener = store.objectForKey_(AZ_TOOLKIT_OPENER_KEY)
    if opener is not None:
        return opener
    try:
        # Another AZ plugin already defined it: ObjC class names are global to
        # the process and all Python plugins share one interpreter, so
        # re-declaring the class raises instead of shadowing.
        opener_class = objc.lookUpClass("AZToolkitSiteOpener")
    except objc.error:

        class AZToolkitSiteOpener(NSObject):
            def openAZSite_(self, sender) -> None:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(AZ_TOOLKIT_SITE_URL))

        opener_class = AZToolkitSiteOpener
    opener = opener_class.alloc().init()
    store.setObject_forKey_(opener, AZ_TOOLKIT_OPENER_KEY)
    return opener


def _az_toolkit_menu():
    """Find-or-create ``Window ▸ AZ Toolkit`` and return its NSMenu, or None if
    the Window menu is not the shape we expect."""
    from AppKit import NSImage, NSMenu, NSMenuItem

    try:
        window_menu = Glyphs.menu[WINDOW_MENU].submenu()
        existing_items = list(window_menu.itemArray())
    except Exception:
        return None
    for item in existing_items:
        if str(item.title() or "") == AZ_TOOLKIT_MENU_TITLE:
            return item.submenu()

    submenu = NSMenu.alloc().initWithTitle_(AZ_TOOLKIT_MENU_TITLE)
    parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(AZ_TOOLKIT_MENU_TITLE, None, "")
    parent.setSubmenu_(submenu)
    # The same symbol the AZ Toolkit pane carries in Settings, so the two
    # surfaces read as one thing. Guarded: a host without SF Symbols has no
    # such selector, and an icon is never worth losing the whole menu over.
    try:
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "wrench.and.screwdriver", AZ_TOOLKIT_MENU_TITLE
        )
        if icon is not None:
            icon.setTemplate_(True)
            icon.setSize_((14.0, 14.0))  # menu-item icon metrics
            parent.setImage_(icon)
    except Exception:
        pass

    # Same reasoning as the icon: if the opener cannot be built the plugin items
    # still belong in the menu, so only the link is dropped.
    try:
        site_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            AZ_TOOLKIT_SITE_TITLE, "openAZSite:", ""
        )
        site_item.setTarget_(_az_toolkit_site_opener())
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(site_item)
    except Exception:
        pass

    window_menu.addItem_(NSMenuItem.separatorItem())
    window_menu.addItem_(parent)
    return submenu


def _install_az_toolkit_item(menu_item) -> None:
    """Put a plugin's item in the AZ Toolkit submenu, alphabetically, above the
    trailing separator + site link.

    IDEMPOTENT BY TITLE: reloading a plugin while Glyphs runs calls ``start``
    again on a menu that already holds the item, and AppKit will happily show
    two of them.
    """
    from AppKit import NSMenuItem

    submenu = _az_toolkit_menu()
    if submenu is None:  # unexpected menu shape — do not lose the item
        Glyphs.menu[WINDOW_MENU].append(NSMenuItem.separatorItem())
        Glyphs.menu[WINDOW_MENU].append(menu_item)
        return
    title = str(menu_item.title() or "")
    for existing in list(submenu.itemArray()):
        if str(existing.title() or "") == title:
            submenu.removeItem_(existing)
    items = list(submenu.itemArray())
    # The trailing separator is the end of the plugin block; the site link sits
    # below it and always stays last.
    end = next((i for i, item in enumerate(items) if item.isSeparatorItem()), len(items))
    insert_at = next((i for i in range(end) if str(items[i].title() or "") > title), end)
    submenu.insertItem_atIndex_(menu_item, insert_at)


class AZLetterProofer(GeneralPlugin):
    @objc.python_method
    def settings(self):
        self.name = "AZ Letter Proofer"
        self.window = None

    @objc.python_method
    def start(self):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            _az_toolkit_title(self.name), self.showWindow_, ""
        )
        item.setTarget_(self)
        icon = _az_icon(MENU_ICON_HEIGHT)
        if icon is not None:
            item.setImage_(icon)
        # Shared, single-separator AZ group in the Window menu.
        _install_az_toolkit_item(item)

        # Tab in Glyphs ▸ Settings ▸ AZ Toolkit: show/hide the dice, its colour.
        try:
            from azletterproofer_settings import register_preferences_pane

            register_preferences_pane(
                lambda: self.window if self.window is not None and self.window.is_open() else None
            )
        except Exception:
            print(f"AZ Letter Proofer settings error:\n{traceback.format_exc()}")

        # Bridge: other plugins (AZ Classifier) can ask AZ Letter Proofer to proof a tag
        # group by posting this notification with {"tag": <title>}.
        from AppKit import NSNotificationCenter

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "digTagNotification:", "com.hugojourdan.AZLetterProofer.dig", None
        )

    def digTagNotification_(self, notification):
        try:
            info = notification.userInfo() or {}
            tag = str(info.get("tag", ""))
            self.showWindow_(None)
            if self.window is not None and tag:
                self.window.dig_tag(tag)
        except Exception:
            Glyphs.showMacroWindow()
            print(f"AZ Letter Proofer dig-tag error:\n{traceback.format_exc()}")

    def showWindow_(self, sender):
        try:
            # Lazy import keeps app launch fast and surfaces import errors
            # in the Macro panel instead of silently disabling the plugin.
            from azletterproofer_ui import AZLetterProoferWindow

            if self.window is None or not self.window.is_open():
                self.window = AZLetterProoferWindow()
            else:
                self.window.bring_to_front()
        except Exception:
            Glyphs.showMacroWindow()
            print(f"AZ Letter Proofer error:\n{traceback.format_exc()}")

    @objc.python_method
    def __file__(self):
        """Please leave this method unchanged"""
        return __file__
