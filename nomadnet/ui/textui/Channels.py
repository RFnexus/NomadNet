import collections
import os
import re
import time

import RNS
import urwid

import nomadnet
from nomadnet.RRC import RRCHub
from nomadnet.vendor.additional_urwid_widgets import IndicativeListBox
from nomadnet.ui.textui.MicronParser import LinkableText, LinkSpec
from RNS.Utilities.rngit.util import MarkdownToMicron
from RNS.Utilities.rngit.highlight import SyntaxHighlighter
from .MicronParser import markup_to_attrmaps, default_state, make_style
from .ReadlineEdit import ReadlineMixin, ReadlineEdit
from nomadnet.util import sanitize_name, strip_modifiers, strip_micron
from nomadnet.util import strip_escaped_micron, unescape_micron, strip_non_formatting_tags
from nomadnet.vendor.Scrollable import Scrollable, ScrollBar


theme_dark  = { "text": "ddd",
                "ts": "888",
                "nick_self": "6c5",
                "nick_peer": "3cd",
                "notice":    "fd3",
                "error":     "f55",
                "system":    "888",
                "mention":   "fb4",
                "link":      "79d",
                # colorgen.py --hue-step 18 --sat-start 25 --sat-steps 2 --sat-step 100 --light-step 30 --normalize --normalize-target 2.5 --perceptual-multiplier 1.4 --discard 1,5,7,11,13,14,17,27,31,33,37,39,3,16,18,36
                "nick_colors": ["f68787", "00c394", "d59e00", "62be00", "a1ac76", "95b600", "76a9ee", "81b385", "7eb1a1", "e89264", "7cb0b0", "00c0c0", "8cacbb", "32b4db", "98a8c3", "bbab00", "95a0fd", "a9a2ca", "ad98fe", "c58ffa", "df83f4", "c49abf", "f380c7", "f484a7"],
               }

theme_light = { "text": "111",
                "ts": "888",
                "nick_self": "3a0",
                "nick_peer": "077",
                "notice":    "a70",
                "error":     "a22",
                "system":    "888",
                "mention":   "c50",
                "link":      "79d",
                # colorgen.py --hue-step 18 --sat-start 25 --sat-steps 2 --sat-step 100 --light-step 30 --normalize --normalize-target 2.5 --perceptual-multiplier 0.2 --discard 1,5,7,11,13,14,17,27,31,33,37,39,3,16,18,36 > ~/.nomadnetwork/storage/pages/index.mu
                "nick_colors": ["ca0000", "008000", "9d1c00", "007800", "2c5200", "006800", "004ac0", "006100", "005d2c", "b70000", "005b5b", "007b7a", "005071", "0064a5", "004580", "714f00", "0026d3", "48318c", "5200d5", "8400cf", "aa00c8", "820079", "c60086", "c80043"],
               }


class _ChatLinkableText(LinkableText):
    def render(self, size, focus=False):
        c = urwid.Text.render(self, size, focus)
        if focus:
            c = urwid.CompositeCanvas(c)
            c.cursor = self.get_cursor_coords(size)
            if self.delegate is not None:
                self.peek_link()
        return c


_LINK_RE = re.compile(
    r"(?P<lxmf>(?<!\w)lxmf@[0-9a-fA-F]{32})(?!\w)"
    r"|(?P<page>(?<![@\w])[0-9a-fA-F]{32}(?::\S+)?)(?!\w)"
    r"|(?P<room>(?<!\w)#[A-Za-z0-9][A-Za-z0-9_\-]{0,62})"
)

def _link_attrs():
    return {
        "room": urwid.AttrSpec("light cyan,underline", "default", colors=256),
        "lxmf": urwid.AttrSpec("light magenta,underline", "default", colors=256),
        "page": urwid.AttrSpec("light blue,underline", "default", colors=256),
    }

_LINK_ATTRS = _link_attrs()

def _scan_links(text):
    for m in _LINK_RE.finditer(text):
        if m.group("lxmf"):
            yield m.start(), m.end(), "lxmf", m.group()[5:]
        elif m.group("page"):
            yield m.start(), m.end(), "page", m.group()
        elif m.group("room"):
            yield m.start(), m.end(), "room", m.group()[1:]


def _chunk_by_bytes(s, budget):
    chunks = []
    remaining = s
    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= budget:
            chunks.append(remaining)
            break
        cut = encoded[:budget]
        while cut and (cut[-1] & 0xC0) == 0x80:
            cut = cut[:-1]
        chunk = cut.decode("utf-8", errors="ignore")
        last_space = max(chunk.rfind(" "), chunk.rfind("\n"), chunk.rfind("\t"))
        if last_space > 0 and last_space >= len(chunk) // 2:
            chunk = chunk[:last_space]
        if not chunk:
            chunk = remaining[:1]
        chunks.append(chunk.rstrip())
        remaining = remaining[len(chunk):].lstrip()
    return chunks


def _split_message(text, max_bytes):
    if not text:
        return [text]
    parts = [text]
    for _attempt in range(10):
        K_guess = max(1, len(parts))
        prefix_bytes = len(("({}/{}) ".format(K_guess, K_guess)).encode("utf-8"))
        budget = max_bytes - prefix_bytes
        if budget <= 0:
            return None
        parts = _chunk_by_bytes(text, budget)
        if len(parts) == K_guess:
            break
    K = len(parts)
    return ["({}/{}) ".format(i+1, K) + p for i, p in enumerate(parts)]


def _scan_mentions(text, own_nick):
    if not own_nick or not text:
        return
    pat = re.compile(r"(?<![A-Za-z0-9_])@"+re.escape(own_nick)+r"(?![A-Za-z0-9_])", re.IGNORECASE) # @(....)
    for m in pat.finditer(text):
        yield m.start(), m.end(), "mention", None

def _scan_nick_mentions(text, own_nick):
    if not own_nick or not text: return
    pat = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)(?![A-Za-z0-9_])", re.IGNORECASE)
    for m in pat.finditer(text):
        nick = m.group(1)
        if nick.lower() != own_nick:
            yield m.start(), m.end(), f"nick_mention", nick

def _body_markup(body, body_attr="body_text", own_nick=None, check_links=True):
    spans = list(_scan_links(body))
    spans.extend(_scan_mentions(body, own_nick))
    spans.extend(_scan_nick_mentions(body, own_nick))
    spans.sort(key=lambda s: s[0])
    filtered = []
    last_end = 0
    for s in spans:
        if s[0] >= last_end:
            filtered.append(s)
            last_end = s[1]
    spans = filtered

    if not spans:
        return [(body_attr, body)], False

    out = []
    pos = 0
    has_links = False
    for start, end, kind, target in spans:
        if start > pos:
            out.append((body_attr, body[pos:start]))
        if kind == "mention":
            out.append(("irc_mention", body[start:end]))
        elif kind == "nick_mention":
            out.append(("nick_mention", body[start:end]))
        else:
            base = _LINK_ATTRS[kind]
            if check_links:
                out.append((LinkSpec(kind+":"+target, base, cm=256), body[start:end]))
                has_links = True
            else:
                out.append((f"link_{kind}", body[start:end]))
                has_links = True
        pos = end
    if pos < len(body):
        out.append((body_attr, body[pos:]))
    return out, has_links


def _short_hash(b, n=12):
    if isinstance(b, (bytes, bytearray)):
        return bytes(b).hex()[:n]
    return "?"


def _format_ts(ts_ms):
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts_ms/1000.0))
    except Exception:
        return ""


class ChannelsListShortcuts():
    def __init__(self, app):
        self.app = app
        self.widget = urwid.AttrMap(urwid.Text("[C-n] New Hub  [C-a] Add Room  [C-r] Connect  [C-w] Disconnect  [C-t] Auto-reconnect  [C-e] Edit Hub  [C-x] Remove"), "shortcutbar")


class ChannelsRoomShortcuts():
    def __init__(self, app):
        self.app = app
        self.widget = urwid.AttrMap(urwid.Text("[C-d] Send  [C-x] Leave  [F8] Collapse  [Tab] Complete Nick"), "shortcutbar")


class ChannelsRoomBodyShortcuts():
    def __init__(self, app):
        self.app = app
        self.widget = urwid.AttrMap(urwid.Text("[C-x] Leave  [C-u] Users  [C-y] Channels  [F8] Collapse Joins  [Tab] ↓ Editor"), "shortcutbar")


class ChannelsDialogLineBox(urwid.LineBox):
    def keypress(self, size, key):
        if key == "esc":
            if hasattr(self.delegate, "close_dialog"):
                self.delegate.close_dialog()
        else:
            return super(ChannelsDialogLineBox, self).keypress(size, key)


class ChannelListEntry(urwid.Text):
    _selectable = True
    signals = ["click"]

    def keypress(self, size, key):
        if self._command_map[key] != urwid.ACTIVATE:
            return key
        self._emit("click")

    def mouse_event(self, size, event, button, x, y, focus):
        if button != 1 or not urwid.util.is_mouse_press(event):
            return False
        self._emit("click")
        return True


class ChannelsExpandGutter(urwid.WidgetWrap):
    def __init__(self, app, delegate):
        self.app = app
        self.delegate = delegate
        glyph = app.ui.glyphs.get("arrow_r", ">")
        if len(glyph) > 1:
            glyph = ">"
        self._inner = urwid.SolidFill(glyph)
        super().__init__(urwid.AttrMap(self._inner, "shortcutbar", "list_focus"))

    def mouse_event(self, size, event, button, col, row, focus):
        if button == 1 and urwid.util.is_mouse_press(event):
            try:
                self.delegate.toggle_channel_list()
                return True
            except Exception:
                pass
        return False

    def selectable(self):
        return False


class UsersExpandGutter(urwid.WidgetWrap):
    def __init__(self, app, delegate):
        self.app = app
        self.delegate = delegate
        glyph = app.ui.glyphs.get("arrow_l", "<")
        if len(glyph) > 1:
            glyph = "<"
        self._inner = urwid.SolidFill(glyph)
        super().__init__(urwid.AttrMap(self._inner, "shortcutbar", "list_focus"))

    def mouse_event(self, size, event, button, col, row, focus):
        if button == 1 and urwid.util.is_mouse_press(event):
            try:
                self.delegate.toggle_users()
                return True
            except Exception:
                pass
        return False

    def selectable(self):
        return False


class UsersBox(urwid.LineBox):
    def mouse_event(self, size, event, button, col, row, focus):
        if button == 1 and urwid.util.is_mouse_press(event) and row == 0:
            try:
                self.delegate.toggle_users()
                return True
            except Exception:
                pass
        return super().mouse_event(size, event, button, col, row, focus)

    def keypress(self, size, key):
        if key == "tab":
            rw = getattr(self, "delegate", None)
            if rw is not None:
                try:
                    rw.columns.focus_position = 0
                    rw.frame.focus_position = "footer"
                    return None
                except Exception:
                    pass
        if key == "ctrl u":
            rw = getattr(self, "delegate", None)
            if rw is not None:
                try:
                    rw.toggle_users()
                    return None
                except Exception:
                    pass
        if key == "ctrl y":
            rw = getattr(self, "delegate", None)
            if rw is not None:
                try:
                    rw.display.toggle_channel_list()
                    return None
                except Exception:
                    pass
        return super().keypress(size, key)


class ChannelsListArea(urwid.LineBox):
    def mouse_event(self, size, event, button, col, row, focus):
        if button == 1 and urwid.util.is_mouse_press(event) and row == 0:
            try:
                self.delegate.toggle_channel_list()
                return True
            except Exception:
                pass
        return super().mouse_event(size, event, button, col, row, focus)

    def keypress(self, size, key):
        if key == "ctrl n":
            self.delegate.new_hub_dialog()
        elif key == "ctrl a":
            self.delegate.join_room_dialog()
        elif key == "ctrl r":
            self.delegate.connect_selected()
        elif key == "ctrl w":
            self.delegate.disconnect_selected()
        elif key == "ctrl t":
            self.delegate.toggle_auto_reconnect_selected()
        elif key == "ctrl e":
            self.delegate.edit_hub_dialog()
        elif key == "ctrl x":
            self.delegate.remove_selected_dialog()
        elif key == "ctrl y":
            self.delegate.toggle_channel_list()
            return None
        elif key == "f8":
            self.delegate.toggle_join_part_collapse()
            return None
        elif key == "tab":
            self.delegate.app.ui.main_display.frame.focus_position = "header"
        elif key == "up" and (self.delegate.ilb.first_item_is_selected() or self.delegate.ilb.body_is_empty()):
            self.delegate.app.ui.main_display.frame.focus_position = "header"
        else:
            return super(ChannelsListArea, self).keypress(size, key)


class HubInfoArea(urwid.LineBox):
    def keypress(self, size, key):
        if key == "ctrl n":
            self.delegate.new_hub_dialog()
            return None
        if key == "ctrl a":
            self.delegate.join_room_dialog()
            return None
        if key == "ctrl r":
            self.delegate.connect_selected()
            return None
        if key == "ctrl w":
            self.delegate.disconnect_selected()
            return None
        if key == "ctrl t":
            self.delegate.toggle_auto_reconnect_selected()
            return None
        if key == "ctrl e":
            self.delegate.edit_hub_dialog()
            return None
        if key == "ctrl x":
            self.delegate.remove_selected_dialog()
            return None
        if key == "ctrl y":
            self.delegate.toggle_channel_list()
            return None
        if key == "f8":
            self.delegate.toggle_join_part_collapse()
            return None
        return super(HubInfoArea, self).keypress(size, key)


class RoomMessageEdit(ReadlineMixin, urwid.Edit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tab_state = None

    def keypress(self, size, key):
        if key == "tab":
            if self._try_tab_complete():
                return None
            return key
        self._tab_state = None
        if key == "ctrl d":
            self.delegate.send_message()
        elif key == "ctrl x":
            self.delegate.leave_room()
        elif key == "f8":
            self.delegate.display.toggle_join_part_collapse()
        elif key == "up":
            y = self.get_cursor_coords(size)[1]
            if y == 0:
                self.delegate.frame.focus_position = "body"
            else:
                return super(RoomMessageEdit, self).keypress(size, key)
        else:
            return super(RoomMessageEdit, self).keypress(size, key)

    def _candidates(self, prefix_lower):
        delegate = getattr(self, "delegate", None)
        if delegate is None or delegate.hub is None or delegate.room is None:
            return []
        members = delegate.hub.get_members(delegate.room)
        own_hash = None
        try:
            if delegate.app.identity is not None:
                own_hash = delegate.app.identity.hash
        except Exception:
            pass
        names = set()
        for m in members:
            if own_hash is not None and m == own_hash:
                continue
            names.add(delegate.hub.display_name_for(m))
        return sorted([n for n in names if n.lower().startswith(prefix_lower)],
                      key=str.lower)

    def _try_tab_complete(self):
        text = self.get_edit_text()
        pos = self.edit_pos
        state = self._tab_state

        if state is not None and state.get("cursor_after") == pos:
            prefix_lower = state["prefix"]
            token_start = state["token_start"]
            has_at = state["has_at"]
            matches = self._candidates(prefix_lower)
            if not matches:
                self._tab_state = None
                return False
            idx = (state["idx"] + 1) % len(matches)
        else:
            start = pos
            while start > 0 and (text[start-1].isalnum() or text[start-1] in "_-"):
                start -= 1
            has_at = start > 0 and text[start-1] == "@"
            token_start = start - 1 if has_at else start
            token = text[start:pos]
            if not token:
                return False
            prefix_lower = token.lower()
            matches = self._candidates(prefix_lower)
            if not matches:
                return False
            idx = 0

        selected = matches[idx]
        if has_at:
            replacement = "@" + selected
        elif token_start == 0:
            replacement = selected + ": "
        else:
            replacement = selected

        new_text = text[:token_start] + replacement + text[pos:]
        new_cursor = token_start + len(replacement)
        self.set_edit_text(new_text)
        self.set_edit_pos(new_cursor)
        self._tab_state = {
            "prefix":       prefix_lower,
            "token_start":  token_start,
            "has_at":       has_at,
            "cursor_after": new_cursor,
            "idx":          idx,
        }
        return True


class RoomFrame(urwid.Frame):
    @property
    def focus_position(self):
        return urwid.Frame.focus_position.fget(self)

    @focus_position.setter
    def focus_position(self, part):
        urwid.Frame.focus_position.fset(self, part)
        try:
            nomadnet.NomadNetworkApp.get_shared_instance().ui.main_display.update_active_shortcuts()
        except Exception:
            pass

    def keypress(self, size, key):
        if key in ("ctrl u", "ctrl x", "ctrl y", "f8", "tab"):
            result = super(RoomFrame, self).keypress(size, key)
            if result != key:
                return result
            if key == "ctrl u":
                self.delegate.toggle_users()
                return None
            if key == "ctrl x":
                self.delegate.leave_room()
                return None
            if key == "ctrl y":
                self.delegate.display.toggle_channel_list()
                return None
            if key == "f8":
                self.delegate.display.toggle_join_part_collapse()
                return None
            if self.focus_position != "footer":
                self.focus_position = "footer"
            return None
        elif self.focus_position == "body":
            if key == "down" and getattr(self.delegate, "messagelist", None) is not None and self.delegate.messagelist.bottom_is_visible:
                self.focus_position = "footer"
            elif key == "up" and getattr(self.delegate, "messagelist", None) is not None and self.delegate.messagelist.top_is_visible:
                nomadnet.NomadNetworkApp.get_shared_instance().ui.main_display.frame.focus_position = "header"
            else:
                return super(RoomFrame, self).keypress(size, key)
        else:
            return super(RoomFrame, self).keypress(size, key)


class _StickyMessageListBox(IndicativeListBox):
    # Tracks whether the user is scrolled to the bottom and re-asserts that
    # position on resize. urwid.ListBox stores an inset computed at the prior
    # size, so without re-asserting, the focused widget can render only partly
    # visible at narrower widths.
    def __init__(self, body, **kwargs):
        self.sticky_bottom = True
        self._last_render_size = None
        super().__init__(body, on_selection_change=self._track_sticky, **kwargs)

    def _track_sticky(self, old_pos, new_pos):
        if new_pos is None:
            self.sticky_bottom = True
        else:
            try:
                self.sticky_bottom = (new_pos == self.rearmost_position())
            except Exception:
                pass

    def render(self, size, focus=False):
        if (self._last_render_size is not None
                and self._last_render_size != size
                and self.sticky_bottom):
            try:
                inner_body = self._listbox.body
                if len(inner_body) > 0:
                    self._listbox.set_focus(len(inner_body)-1)
                    self._listbox.set_focus_valign("bottom")
            except Exception:
                pass
        self._last_render_size = size
        canvas = super().render(size, focus=focus)
        if getattr(self, "bottom_is_visible", False):
            self.sticky_bottom = True
        return canvas


class RoomWidget(urwid.WidgetWrap):
    USERS_PANE_WIDTH = 22

    def __init__(self, display, hub, room):
        self.display = display
        self.hub = hub
        self.room = room
        self.app = nomadnet.NomadNetworkApp.get_shared_instance()
        self.theme = theme_dark if self.app.config["textui"]["theme"] == nomadnet.ui.TextUI.THEME_DARK else theme_light

        self.messagelist = None
        self.last_history_clean = 0
        self.peer_info_widget = urwid.AttrMap(urwid.Text(""), "msg_header_sent")
        self._update_peer_info()

        editor = RoomMessageEdit(caption="", edit_text="", multiline=True)
        editor.delegate = self
        self.editor = editor
        urwid.connect_signal(editor, "postchange", self._on_editor_change)
        editor_attr = urwid.AttrMap(editor, "msg_editor")

        self.link_delegate = _ChatLinkDelegate(self.display, self.hub)
        self.update_messages()

        self.frame = RoomFrame(
            self.messagelist,
            header=self.peer_info_widget,
            footer=editor_attr,
            focus_part="footer",
        )
        self.frame.delegate = self

        self.chat_box = urwid.LineBox(self.frame)
        self.users_walker = urwid.SimpleFocusListWalker([urwid.Text("")])
        self.users_listbox = urwid.ListBox(self.users_walker)
        self.users_box = UsersBox(self.users_listbox, title="Users")
        self.users_box.delegate = self
        self.users_gutter = UsersExpandGutter(self.app, self)
        self.show_gutters = self.app.rrc_show_gutters
        self._refresh_users_pane()

        self.users_visible = self.display.users_visible
        self.columns = urwid.Columns([(urwid.WEIGHT, 1, self.chat_box)], dividechars=0, focus_column=0)
        self._apply_users_visibility()
        super().__init__(self.columns)

    def toggle_users(self):
        self.users_visible = not self.users_visible
        self.display.users_visible = self.users_visible
        self._apply_users_visibility()

    def _apply_users_visibility(self):
        if self.users_visible:
            self.columns.contents = [
                (self.chat_box,  self.columns.options(urwid.WEIGHT, 1)),
                (self.users_box, self.columns.options(urwid.GIVEN, RoomWidget.USERS_PANE_WIDTH)),
            ]
        else:
            if self.show_gutters:
                self.columns.contents = [
                    (self.chat_box,     self.columns.options(urwid.WEIGHT, 1)),
                    (self.users_gutter, self.columns.options(urwid.GIVEN, 1)),
                ]
            else:
                self.columns.contents = [
                    (self.chat_box,     self.columns.options(urwid.WEIGHT, 1))
                ]

        self.columns.focus_position = 0

    def _refresh_users_pane(self):
        g = self.app.ui.glyphs
        walker = self.users_walker
        if self.hub is None or self.room is None:
            walker[:] = [urwid.Text("")]
            return
        members = self.hub.get_members(self.room)
        own_hash = self.app.identity.hash if self.app.identity is not None else None
        def _safe_name(raw):
            if not raw: return ""
            try:
                if self.app.config["textui"]["sanitize_names"]:
                    return sanitize_name(str(raw)) or ""
                return strip_modifiers(str(raw)) or ""
            except Exception:
                return str(raw or "")

        entries = []
        for m in members:
            safe_name = _safe_name(self.hub.display_name_for(m))
            full_name = safe_name
            safe_name = safe_name[:15]+"…" if len(safe_name) > 16 else safe_name
            entries.append((safe_name, m, own_hash is not None and m == own_hash, full_name))
        entries.sort(key=lambda x: x[0].lower())

        prev_focus_key = None
        try:
            prev_idx = walker.focus
            if prev_idx is not None and 0 <= prev_idx < len(walker):
                prev_focus_key = getattr(walker[prev_idx], "user_hash", None)
        except Exception:
            prev_focus_key = None

        rows = [urwid.Text(" "+str(len(entries))+" user"+("s" if len(entries) != 1 else ""))]
        for name, peer_hash, is_self, full_name in entries:
            if self.app.rrc_nick_colors:
                style_state = default_state()
                style_state["fg_color"] = get_nick_color(peer_hash, self.theme, self.app)
                if is_self:
                    label = " "+g["arrow_r"]+" "+name
                    style = make_style(style_state)
                else:
                    label = " "+g["peer"]+" "+name
                    style = make_style(style_state)

            else:
                if is_self:
                    label = " "+g["arrow_r"]+" "+name
                    style = "list_trusted"
                else:
                    label = " "+g["peer"]+" "+name
                    style = "connected_status"
            entry = ChannelListEntry(label)
            urwid.connect_signal(entry, "click", self.display.show_user_info, (self.hub, peer_hash, full_name))
            row = urwid.AttrMap(entry, style, "list_focus")
            row.user_hash = peer_hash
            rows.append(row)
        if not entries:
            rows.append(urwid.Text(" (no members)"))

        walker[:] = rows

        new_focus = None
        if prev_focus_key is not None:
            for idx, w in enumerate(walker):
                if getattr(w, "user_hash", None) == prev_focus_key:
                    new_focus = idx
                    break
        if new_focus is None:
            for idx, w in enumerate(walker):
                if hasattr(w, "user_hash"):
                    new_focus = idx
                    break
        if new_focus is not None:
            try: walker.set_focus(new_focus)
            except Exception: pass

    def _update_peer_info(self):
        if self.hub is None or self.room is None:
            self.peer_info_widget.original_widget.set_text("")
            return

        status_label = {
            RRCHub.STATUS_DISCONNECTED: "Disconnected",
            RRCHub.STATUS_CONNECTING:   "Connecting",
            RRCHub.STATUS_CONNECTED:    "Connected",
            RRCHub.STATUS_FAILED:       "Failed",
        }.get(self.hub.status, "")

        server = ""
        if self.hub.hub_name:
            server = " "+self.app.ui.glyphs["divider1"]+" "+self.hub.hub_name
            if self.hub.hub_version:
                server += " v"+self.hub.hub_version
        left  = " #"+self.room+server+"  ("+self.hub.name+")"
        right = status_label+" "
        self.peer_info_widget.original_widget.set_text(left+" | "+right)

    def update_messages(self, replace=False):
        msgs = self.hub.get_messages(self.room) if (self.hub is not None and self.room is not None) else []
        widgets = []
        collapse = getattr(self.display, "collapse_join_part", False)
        run = []

        def flush_run():
            if not run:
                return
            widgets.append(_collapsed_joinpart_widget(self.app, len(run)))
            run.clear()

        for m in msgs:
            if collapse and _is_joinpart_system(m):
                run.append(m)
                continue
            flush_run()
            widgets.append(_message_widget(self.app, self.hub, self.room, m, link_delegate=self.link_delegate))
        flush_run()

        if not widgets:
            widgets = [urwid.Text([("irc_system", " "+self.app.ui.glyphs["info"]+"  No messages yet")])]
            self._empty_placeholder = True
        else:
            self._empty_placeholder = False

        self.messagelist = _StickyMessageListBox(widgets, position=len(widgets)-1)
        self.messagelist.name = "messagelist"
        try:
            self.messagelist._listbox.set_focus_valign("bottom")
        except Exception:
            pass
        if replace and hasattr(self, "frame"):
            self.frame.contents["body"] = (self.messagelist, None)
        if hasattr(self, "users_walker"):
            self._refresh_users_pane()

    def append_message(self, msg):
        if self.messagelist is None:
            self.update_messages(replace=True)
            return
        try:
            widget = _message_widget(self.app, self.hub, self.room, msg, link_delegate=self.link_delegate)
            wrapped = urwid.AttrMap(widget, None)
            body = self.messagelist.get_body()
            was_at_bottom = (self.messagelist.sticky_bottom
                             or getattr(self.messagelist, "bottom_is_visible", True))
            self.messagelist.sticky_bottom = was_at_bottom
            if getattr(self, "_empty_placeholder", False):
                del body[:]
                self._empty_placeholder = False

            if self.hub.clean_last_removed > self.last_history_clean:
                try:
                    with self.hub._lock:
                        hub_msgs = self.hub.get_messages(self.room, take_lock=False) if (self.hub is not None and self.room is not None) else []
                        self.last_history_clean = time.time()
                        c = self.messagelist.body_len()
                        old = set()
                        for i in range(0, c):
                            msg = None
                            w = self.messagelist.get_item(i)
                            if hasattr(w, "_original_widget"): o = w._original_widget
                            else: o = None
                            if hasattr(o, "msg"): msg = o.msg
                            elif hasattr(w, "msg"): msg = w.msg
                            if msg and not msg in hub_msgs: old.add(w)

                        list_body = self.messagelist.get_body()
                        for w in list(old):
                            try: list_body.remove(w)
                            except: RNS.log(f"Could not remove expired message widget {w}: {e}", RNS.LOG_DEBUG)

                except Exception as e:
                    RNS.log("Error while cleaning room history", RNS.LOG_ERROR)
                    RNS.trace_exception(e)

            body.append(wrapped)
            cap = getattr(self.app, "rrc_history_per_room_cap", 0)
            if cap and cap > 0:
                while len(body) > cap:
                    del body[0]
            if was_at_bottom:
                try:
                    self.messagelist._listbox.set_focus(len(body)-1)
                    self.messagelist._listbox.set_focus_valign("bottom")
                except Exception:
                    pass
        except Exception as e:
            RNS.log("Incremental append failed, falling back: "+str(e), RNS.LOG_DEBUG)
            self.update_messages(replace=True)
        if hasattr(self, "users_walker"):
            self._refresh_users_pane()

    def _on_editor_change(self, editor, old_text):
        if self.messagelist is None:
            return
        try:
            body = self.messagelist._listbox.body
            if len(body) > 0:
                self.messagelist._listbox.set_focus(len(body)-1)
                self.messagelist._listbox.set_focus_valign("bottom")
                self.messagelist.sticky_bottom = True
        except Exception:
            pass

    def send_message(self):
        text = self.editor.get_edit_text()
        if not text.strip():
            return
        self.messagelist.sticky_bottom = True
        if text.lstrip().startswith("/"):
            self._handle_slash_command(text.lstrip())
            self.editor.set_edit_text("")
            return
        if self.hub.status != RRCHub.STATUS_CONNECTED:
            try:
                self.hub.connect()
            except Exception:
                pass
            return
        limit = self.hub.max_msg_body_bytes or 350
        if len(text.encode("utf-8")) > limit:
            self._open_split_dialog(text, limit)
            return
        try:
            self.hub.send_message(self.room, text)
            self.editor.set_edit_text("")
        except Exception as e:
            RNS.log("Failed to send RRC message: "+str(e), RNS.LOG_ERROR)

    def _open_split_dialog(self, text, limit):
        body_bytes = len(text.encode("utf-8"))
        parts = _split_message(text, limit)
        if not parts:
            self._local_message("error",
                "Message is "+str(body_bytes)+" bytes but per-message limit is too small to split.")
            return
        K = len(parts)
        preview = parts[0]
        if len(preview) > 70:
            preview = preview[:70] + "…"
        preview = preview.replace("\n", " ").replace("\t", " ")

        error_text = urwid.Text("")

        def cancel(sender):
            self.display.close_dialog()

        def send_split(sender):
            try:
                self.messagelist.sticky_bottom = True
                for p in parts:
                    self.hub.send_message(self.room, p)
                self.editor.set_edit_text("")
                self.display.close_dialog()
            except Exception as e:
                error_text.set_text(("error_text", "Send failed: "+str(e)))

        dialog = ChannelsDialogLineBox(
            urwid.Pile([
                urwid.Text(""),
                urwid.Text("  Message is "+str(body_bytes)+" bytes."),
                urwid.Text("  Hub limit  : "+str(limit)+" bytes per message."),
                urwid.Text(""),
                urwid.Text("  Split into "+str(K)+" message"+("s" if K != 1 else "")+"."),
                urwid.Text("  Preview of part 1:"),
                urwid.AttrMap(urwid.Text("    "+preview), "irc_system"),
                urwid.Text(""),
                error_text,
                urwid.Columns([
                    (urwid.WEIGHT, 0.45, urwid.Button("Send Split", on_press=send_split)),
                    (urwid.WEIGHT, 0.1, urwid.Text("")),
                    (urwid.WEIGHT, 0.45, urwid.Button("Cancel", on_press=cancel)),
                ])
            ]), title="Message Too Long"
        )
        dialog.delegate = self.display
        self.display._show_dialog_overlay(dialog)

    def _local_message(self, kind, text):
        from nomadnet.RRC import RRCMessage
        msg = RRCMessage(kind, self.room, None, None, text, int(time.time()*1000))
        with self.hub._lock:
            buf = self.hub.messages.setdefault(self.room, [])
            buf.append(msg)
            if len(buf) > 500:
                del buf[:len(buf)-500]
        self.hub.manager._notify_messages(self.hub, msg)
    # printed /help
    SLASH_HELP = [
        "/help                                - show this list",
        "/ping                                - measure round-trip to hub",
        "/list                                - list public rooms on this hub",
        "/join <room>                         - join a room on this hub",
        "/part [room]                         - leave a room (default: current)",
        "/leave [room]                        - alias for /part",
        "/me <text>                           - send an action (e.g. /me waves)",
        "/nick <name>                         - set your nick on this hub only",
        "/who [room]                          - list users (current room if omitted)",
        "/names [room]                        - alias for /who",
        "/clear                               - clear local messages in this room",
        "/connect                             - connect this hub",
        "/disconnect                          - disconnect this hub",
        "/quit                                - alias for /disconnect",
        "",
        "Server-side commands (auth enforced by hub):",
        "/topic <room> [text]                 - view or set room topic",
        "/mode <room> [+-flags] [arg]         - view or set room modes",
        "/register <room>                     - register the current room",
        "/unregister <room>                   - unregister the current room",
        "/kick <room> <target>                - remove user from room",
        "/ban <room> add|del|list [target]    - room ban list",
        "/invite <room> add|del|list [target] - room invite list",
        "/op <room> <target>                  - grant op",
        "/deop <room> <target>                - revoke op",
        "/voice <room> <target>               - grant voice",
        "/devoice <room> <target>             - revoke voice",
        "/kline add|del|list [target]         - global ban",
        "/stats                               - server statistics",
        "/reload                              - reload server config",
    ]

    # commands that we forward to the server verbatim
    SERVER_SLASH_COMMANDS = {
        "who", "names",
        "topic", "mode", "kick", "kline",
        "ban", "invite", "kline",
        "op", "deop", "voice", "devoice",
        "register", "unregister",
        "stats", "reload",
    }

    def _require_connected(self):
        if self.hub.status != RRCHub.STATUS_CONNECTED:
            self._local_message("error", "Not connected to hub")
            return False
        return True

    def _handle_slash_command(self, text):
        parts = text[1:].split(None, 1)
        if not parts or not parts[0]:
            self._local_message("error", "Empty command")
            return
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "help":
            for line in self.SLASH_HELP:
                self._local_message("system", line)
            return

        if cmd == "ping":
            if not self._require_connected():
                return
            try:
                self.hub.send_ping(room=self.room)
                self._local_message("system", "Ping sent")
            except Exception as e:
                self._local_message("error", "Ping failed: "+str(e))
            return

        if cmd == "list":
            if not self._require_connected():
                return
            try:
                self.hub.send_command("/list", room=self.room)
            except Exception as e:
                self._local_message("error", "/list failed: "+str(e))
            return

        if cmd in ("join", "j"):
            if not arg:
                self._local_message("error", "Usage: /join <room>")
                return
            target = arg.lstrip("#").strip()
            try:
                self.hub.add_room(target)
                if self.hub.status == RRCHub.STATUS_CONNECTED:
                    self.hub.join_room(target)
                self.display.update_list()
                self.display._select_room(None, (self.hub, target.lower()))
            except Exception as e:
                self._local_message("error", "Join failed: "+str(e))
            return

        if cmd in ("part", "leave"):
            target = (arg.lstrip("#").strip().lower()) if arg else self.room
            try:
                self.hub.part_room(target)
                self.display.update_list()
                if target == self.room:
                    self.display.show_placeholder()
            except Exception as e:
                self._local_message("error", "Part failed: "+str(e))
            return

        if cmd == "me":
            if not self._require_connected():
                return
            if not arg:
                self._local_message("error", "Usage: /me <text>")
                return
            limit = self.hub.max_msg_body_bytes or 350
            if len(arg.encode("utf-8")) > limit:
                self._local_message("error", "Action too long (max "+str(limit)+" bytes)")
                return
            try:
                self.hub.send_action(self.room, arg)
            except Exception as e:
                self._local_message("error", "/me failed: "+str(e))
            return

        if cmd == "nick":
            if not arg:
                cur = self.hub.get_effective_nick() or " unset"
                src = "nick: " if (isinstance(self.hub.nick_override, str) and self.hub.nick_override) else "global"
                self._local_message("system", "Nick on this hub: "+cur+" ("+src+")")
                return
            limit = self.hub.max_nick_bytes or 32
            if len(arg.encode("utf-8")) > limit:
                self._local_message("error", "Nick too long (max "+str(limit)+" bytes)")
                return
            try:
                self.hub.set_nick_override(arg)
                self._local_message("system", "Nick on this hub set to "+arg+
                                    " (use /nick with no argument to view)")
            except Exception as e:
                self._local_message("error", "Nick change failed: "+str(e))
            return

        if cmd == "clear":
            self.hub.clear_messages(self.room)
            self.update_messages(replace=True)
            return

        if cmd == "connect":
            try:
                self.hub.connect()
                self._local_message("system", "Connecting...")
            except Exception as e:
                self._local_message("error", "Connect failed: "+str(e))
            return

        if cmd in ("disconnect", "quit"):
            try:
                self.hub.disconnect()
            except Exception as e:
                self._local_message("error", "Disconnect failed: "+str(e))
            return

        if cmd in self.SERVER_SLASH_COMMANDS:
            if not self._require_connected():
                return
            try:
                self.hub.send_command("/"+cmd+(" "+arg if arg else ""), room=self.room)
            except Exception as e:
                self._local_message("error", "/"+cmd+" failed: "+str(e))
            return

        self._local_message("error", "Unknown command: /"+cmd+"  (try /help)")

    def leave_room(self):
        try:
            self.hub.part_room(self.room)
        except Exception:
            pass
        self.display.update_list()
        self.display.show_placeholder()


def _ts_prefix(ts_ms):
    t = _format_ts(ts_ms) if ts_ms else "        "
    return ("irc_ts", " ["+t+"] ")

def _ts_prefix_raw(ts_ms):
    t = _format_ts(ts_ms) if ts_ms else "        "
    return "["+t+"] "


class _ChatLinkDelegate:
    def __init__(self, display, hub):
        self.display = display
        self.hub = hub
        self.app = display.app
        self.last_keypress = 0

    def marked_link(self, target, fields=None):
        pass

    def micron_released_focus(self):
        pass

    def handle_link(self, target, fields=None):
        if target is None: return
        try:
            components = target.split("://")
            if len(components) < 2: return
            kind = components[0]
            payload = components[1]
            if kind == "room": self._open_room(payload.lstrip("#"))
            elif kind == "lxmf": self._open_lxmf(payload.lstrip("lxmf@"))
            elif kind == "page":
                final_url = payload
                if fields:
                    final_url += "`"
                    for f in fields: final_url += f"{f}|"
                    final_url.rstrip("|")
                self._open_page(final_url)
            else: RNS.log(f"Invalid URL: {target}", RNS.LOG_WARNING)
        except Exception as e: RNS.log("Chat link handler failed: "+str(e), RNS.LOG_ERROR)

    def _open_room(self, room):
        room = (room or "").strip().lower()
        if not room:
            return
        if room not in self.hub.rooms and self.hub.status == RRCHub.STATUS_CONNECTED:
            try: self.hub.join_room(room)
            except Exception: pass
        self.hub.add_room(room)
        self.display.update_list()
        self.display._select_room(None, (self.hub, room))

    def _open_lxmf(self, hash_hex):
        try:
            bytes.fromhex(hash_hex)
        except Exception:
            return
        from nomadnet.Directory import DirectoryEntry
        existing = [c[0] for c in nomadnet.Conversation.conversation_list(self.app)]
        if hash_hex not in existing:
            display_name = None
            try:
                data = RNS.Identity.recall_app_data(bytes.fromhex(hash_hex))
                if data is not None:
                    import LXMF
                    display_name = LXMF.display_name_from_app_data(data)
            except Exception:
                pass
            try:
                self.app.directory.remember(DirectoryEntry(bytes.fromhex(hash_hex), display_name=display_name))
            except Exception:
                pass
            try:
                nomadnet.Conversation(hash_hex, self.app, initiator=True)
            except Exception:
                pass
        conversations = self.app.ui.main_display.sub_displays.conversations_display
        try:
            trust_level = self.app.directory.trust_level(bytes.fromhex(hash_hex))
        except Exception:
            trust_level = DirectoryEntry.UNKNOWN
        target_filter = (conversations.LIST_FILTER_TRUSTED if trust_level == DirectoryEntry.TRUSTED
                         else conversations.LIST_FILTER_UNTRUSTED)
        if conversations.list_filter != target_filter:
            conversations._set_filter(target_filter)
        else:
            conversations.update_conversation_list()
        conversations.display_conversation(None, hash_hex)
        self.app.ui.main_display.show_conversations(None)

    def _open_page(self, url):
        if not url:
            return
        self.app.ui.main_display.show_network(None)
        try:
            self.app.ui.main_display.sub_displays.network_display.browser.retrieve_url(url)
        except Exception as e:
            RNS.log("Could not open page link: "+str(e), RNS.LOG_ERROR)


_MOTD_ROOM_RE = re.compile(r"(?<!\[)(?<!\w)#([A-Za-z0-9][A-Za-z0-9_\-]{0,62})")

def _linkify_motd(text):
    if not text:
        return text or ""
    def repl(m):
        name = m.group(1)
        return "`["+m.group(0)+"`room://"+name+"]"
    return _MOTD_ROOM_RE.sub(repl, text)


def _is_joinpart_system(m):
    if getattr(m, "kind", None) != "system":
        return False
    text = (getattr(m, "text", "") or "").strip()
    if not text:
        return False
    if text.startswith("You "):
        return False
    return text.endswith(" joined") or text.endswith(" left")

def _collapsed_joinpart_widget(app, n):
    label = "  ⋯  "+str(n)+" join/leave event"+("" if n == 1 else "s")+"  ⋯"
    return urwid.Padding(urwid.AttrMap(urwid.Text(label, align=urwid.CENTER), "irc_system"), left=1)

def get_nick_color(sender_hash, theme, app, shift=15):
    if app.rrc_nick_colors_theme: nick_colors = app.rrc_nick_colors_theme
    else: nick_colors = theme["nick_colors"]
    if type(sender_hash) == str:
        try: sender_hash = sender_hash.encode("utf-8")
        except: pass
    if not type(sender_hash) == bytes: return theme["nick_peer"]
    return nick_colors[(int.from_bytes(sender_hash, "big")+shift)%len(nick_colors)]

room_nick_src_cache = {}
def get_nick_src(hub, room, nick):
    try:
        if not nick in room_nick_src_cache.get(str(room), {}):
            if not str(room) in room_nick_src_cache: room_nick_src_cache[str(room)] = {}
            for nh in hub.nicks.copy():
                hubnick = hub.nicks[nh]
                room_nick_src_cache[str(room)][hubnick] = nh

        if nick in room_nick_src_cache.get(str(room), {}): return room_nick_src_cache[str(room)][str(nick)]
        else: return None

    except Exception as e:
        RNS.log(f"Couldn't get hash for nick {nick}: {e}", RNS.LOG_DEBUG)
        return None

invalid_span_starts = [">", "#"]
mdc = MarkdownToMicron(max_width=80, syntax_highlighter=SyntaxHighlighter(), url_scope=None)
def _message_widget(app, hub, room, m, link_delegate=None):
    t = theme_dark if app.config["textui"]["theme"] == nomadnet.ui.TextUI.THEME_DARK else theme_light
    g = app.ui.glyphs
    own_nick = None
    try:
        if hub is not None:
            own_nick = hub.get_effective_nick()
        else:
            own_nick = app.rrc.get_nickname()
    except Exception:
        pass

    if m.kind == "system":
        evt_icon = g["arrow_l"] if m.text.endswith(" left") else g["arrow_r"]
        spans, has_links = _body_markup(m.text or "", body_attr="irc_system", own_nick=own_nick)
        if m.text.endswith(" left") or m.text.endswith(" joined"): spans = [(s[0], sanitize_name(s[1])) for s in spans]
        markup = [_ts_prefix(m.ts), ("irc_system", evt_icon+" ")] + spans
        final_widget = _wrap_text(markup, link_delegate if has_links else None)
        final_widget.msg = m
        return final_widget

    if m.kind == "notice":
        spans, has_links = _body_markup(m.text or "", body_attr="irc_notice", own_nick=own_nick)
        markup = [_ts_prefix(m.ts), ("irc_notice", g["info"]+" ")] + spans
        final_widget = _wrap_text(markup, link_delegate if has_links else None)
        final_widget.msg = m
        return final_widget

    if m.kind == "error":
        spans, has_links = _body_markup(m.text or "", body_attr="irc_error", own_nick=own_nick)
        markup = [_ts_prefix(m.ts), ("irc_error", g["warning"]+" ")] + spans
        final_widget = _wrap_text(markup, link_delegate if has_links else None)
        final_widget.msg = m
        return final_widget

    own = False
    try:
        if hub is not None and m.src is not None and app.identity is not None:
            own = bytes(m.src) == app.identity.hash
    except Exception:
        pass

    if m.nick:                                  sender = sanitize_name(m.nick)
    elif isinstance(m.src, (bytes, bytearray)): sender = _short_hash(m.src)
    else:                                       sender = "?"

    if isinstance(m.src, (bytes, bytearray)):
        if not str(room) in room_nick_src_cache: room_nick_src_cache[str(room)] = {}
        room_nick_src_cache[str(room)][str(m.nick)] = m.src

    nick_attr = "irc_nick_self" if own else "irc_nick_peer"
    body = m.text or ""
    spans, has_links = _body_markup(body, body_attr="body_text", own_nick=own_nick, check_links=False)
    ld = link_delegate if has_links else None

    irc_ts = f"`F{t['ts']}"
    message_body = ""
    for span in spans:
        ms = span[0]
        mb = span[1]
        if ms.startswith("irc_mention"):
            if not app.rrc_nick_colors:
                if app.rrc_mention_color: mention_color = f"`FT{app.rrc_mention_color}"
                else: mention_color = f"`F{t['mention']}"
                message_body += f"`!{mention_color}{mb}`f`!"
                if app.rrc_color_mention_timestamps: irc_ts = f"`F{t['mention']}"

            else:
                try:
                    if not app.rrc_mention_color: own_nick_color = get_nick_color(app.identity.hash, t, app)
                    else: own_nick_color = app.rrc_mention_color
                    message_body += f"`!`FT{own_nick_color}{mb}`f`!"
                    if app.rrc_color_mention_timestamps: irc_ts = f"`FT{own_nick_color}"

                except: message_body += f"`!`F{t['mention']}{mb}`f`!"

        elif ms.startswith("nick_mention"):
            if not app.rrc_nick_colors: message_body += f"{mb}"
            else:
                mentioned_nick = mb[1:]
                nick_src = get_nick_src(hub, room, mentioned_nick)
                if not nick_src: message_body += f"{mb}"
                else:
                    nick_color = get_nick_color(nick_src, t, app)
                    message_body += f"`!`FT{nick_color}{mb}`f`!"

        elif ms.startswith("link_"):
            kind = ms[len("link_"):]
            comps = mb.split("`")
            label = comps[0]
            fields = "|"+comps[1].replace("`", "") if len(comps) > 1 else ""
            url = f"{kind}://{mb}"
            link_mu = f"`_`F{t['link']}`[{label}{fields}`{url}]`f`_"
            link_md = f"[{label}]({url})"
            message_body += link_mu

        else:
            # This is a hack for now to avoid start-of-span
            # being interpreted as start-of-line by the micron
            # parser, because the link-detection logic splits
            # up lines into spans for link detection. Since
            # We're now using the micron engine, we could
            # probably avoid this alltogether. But it needs
            # another kind of link handling, and I'm not going
            # to write that right now, so little hack it is.
            if mb[0:1] in invalid_span_starts: yanked = mb[0:1]; mb = mb[1:]
            else:                              yanked = ""

            if app.rrc_ui_render_micron:
                mbo = mdc.format_block(mb) if app.rrc_ui_render_markdown else mb
                mbo = unescape_micron(mbo)
                message_body += yanked+strip_non_formatting_tags(mbo)

            else:
                mbo = mdc.format_block(strip_escaped_micron(mb)) if app.rrc_ui_render_markdown else strip_escaped_micron(mb)
                message_body += yanked+strip_non_formatting_tags(mbo)

    if app.rrc_nick_colors: nick_attr = f"`FT{get_nick_color(m.src, t, app)}"
    else:                   nick_attr = f"`F{t['nick_self']}" if own else f"`F{t['nick_peer']}"

    prefix_micron = f"{irc_ts}{_ts_prefix_raw(m.ts)}`f"
    if m.kind == "action":
        nick_micron = f" `*`f{nick_attr}{sender}`f`* "
        message_body = f"`*`f{nick_attr}{strip_micron(message_body)}`f`*"
    else:
        nick_micron = f"`f{nick_attr}<{sender}>`f "

    if app.rrc_ui_justify_msgs:
        prefix_rendered = _render_body(f"{prefix_micron}", fg=t["text"])
        body_rendered   = _render_body(f"{nick_micron}{message_body}", link_delegate=ld, fg=t["text"])
        if app.rrc_ui_space_msgs: body_rendered.append(urwid.Text(""))
        columns         = urwid.Columns([(urwid.PACK, urwid.Pile(prefix_rendered)), urwid.Pile(body_rendered)], dividechars=1)
        final_widget    = urwid.Padding(columns, left=1)

    else:
        rendered = _render_body(f"{prefix_micron}{nick_micron}{message_body}", link_delegate=ld, fg=t["text"])
        if app.rrc_ui_space_msgs: rendered.append(urwid.Text(""))
        final_widget = urwid.Padding(urwid.Pile(rendered), left=1)

    final_widget.msg = m
    return final_widget

def _render_body(markup, link_delegate=None, fg="bbb"):
    try: return markup_to_attrmaps(strip_modifiers(markup), url_delegate=link_delegate, link_class=_ChatLinkableText, fg_color=fg)
    except Exception as e:
        RNS.trace_exception(e)
        return []

def _wrap_text(markup, link_delegate):
    if link_delegate is not None:
        return _ChatLinkableText(markup, align="left", delegate=link_delegate)
    return urwid.Text(markup)


class ChannelsDisplay():
    list_width = 0.33
    given_list_width = 36

    def __init__(self, app):
        self.app = app
        self.dialog_open = False
        self.list_widgets = []
        self.selected_key = None
        self.current_room_widget = None
        self.users_visible = True
        self.channel_list_visible = True
        self.collapse_join_part = False
        self._room_drafts = {}

        self._build_listbox()
        self.gutter = ChannelsExpandGutter(self.app, self)
        self.show_gutters = self.app.rrc_show_gutters

        self.list_shortcuts = ChannelsListShortcuts(self.app)
        self.room_shortcuts = ChannelsRoomShortcuts(self.app)
        self.room_body_shortcuts = ChannelsRoomBodyShortcuts(self.app)
        self.shortcuts_display = self.list_shortcuts

        self.placeholder = urwid.LineBox(urwid.Filler(urwid.Text("\n  Select or add a hub to begin", align=urwid.CENTER), "top"))
        self.right = self.placeholder

        self.columns_widget = urwid.Columns(
            [
                (ChannelsDisplay.given_list_width, self.listbox),
                (urwid.WEIGHT, 1, self.right),
            ],
            dividechars=0, focus_column=0, box_columns=[0],
        )
        self.widget = urwid.WidgetPlaceholder(self.columns_widget)

        self._pending_actions = collections.deque()
        self._wake_fd = None
        try:
            self._wake_fd = self.app.ui.loop.watch_pipe(self._process_pending)
        except Exception:
            pass

        self._mention_bell_last = {}

        self.app.rrc.set_change_callback(self._on_rrc_change)
        self.app.rrc.set_message_callback(self._on_rrc_message)

    def _set_right_widget(self, widget):
        prev = getattr(self, "right", None)
        if isinstance(prev, RoomWidget) and prev is not widget:
            self._save_room_draft(prev)
        self.right = widget
        if widget is self.placeholder:
            # The placeholder has nothing to interact with, so always keep the
            # hub/channel pane visible (and focused) beside it.
            self.channel_list_visible = True
            self._apply_channel_list_visibility(focus_right=False)
            try:
                self.columns_widget.focus_position = 0
            except Exception:
                pass
        else:
            self._apply_channel_list_visibility(focus_right=True)

    def _draft_key(self, hub, room):
        try:
            return (hub.hub_hash, hub.dest_name, room)
        except Exception:
            return None

    def _save_room_draft(self, room_widget):
        try:
            key = self._draft_key(room_widget.hub, room_widget.room)
            if key is None:
                return
            text = room_widget.editor.get_edit_text()
            if text:
                self._room_drafts[key] = text
            else:
                self._room_drafts.pop(key, None)
        except Exception:
            pass

    def _restore_room_draft(self, room_widget):
        try:
            key = self._draft_key(room_widget.hub, room_widget.room)
            if key is None:
                return
            text = self._room_drafts.get(key)
            if text:
                room_widget.editor.set_edit_text(text)
                room_widget.editor.set_edit_pos(len(text))
        except Exception:
            pass

    def toggle_channel_list(self):
        if self.channel_list_visible and self.right is self.placeholder:
            return
        self.channel_list_visible = not self.channel_list_visible
        self._apply_channel_list_visibility()

    def toggle_join_part_collapse(self):
        self.collapse_join_part = not self.collapse_join_part
        if self.current_room_widget is not None:
            try:
                self.current_room_widget.update_messages(replace=True)
            except Exception:
                pass

    def _apply_channel_list_visibility(self, focus_right=False):
        list_opts   = self.columns_widget.options(urwid.GIVEN, ChannelsDisplay.given_list_width)
        gutter_opts = self.columns_widget.options(urwid.GIVEN, 1)
        right_opts  = self.columns_widget.options(urwid.WEIGHT, 1)
        if self.channel_list_visible:
            self.columns_widget.contents = [
                (self.listbox, list_opts),
                (self.right,   right_opts),
            ]
            if focus_right:
                try: self.columns_widget.focus_position = 1
                except Exception: pass
        else:
            if self.show_gutters:
                self.columns_widget.contents = [
                    (self.gutter, gutter_opts),
                    (self.right,  right_opts),
                ]
            else:
                self.columns_widget.contents = [
                    (self.right,  right_opts),
                ]
            try: self.columns_widget.focus_position = 1
            except Exception: pass

    def start(self):
        self.update_list()

    def shortcuts(self):
        try:
            focus_path = self.columns_widget.get_focus_path()
        except Exception:
            focus_path = None
        if focus_path and focus_path[0] == 1 and self.current_room_widget is not None:
            try:
                frame = self.current_room_widget.frame
                if frame is not None and frame.focus_position == "body":
                    return self.room_body_shortcuts
            except Exception:
                pass
            return self.room_shortcuts
        return self.list_shortcuts

    def _build_listbox(self):
        self._compose_list_widgets()
        self.ilb = IndicativeListBox(
            self.list_widgets,
            on_selection_change=lambda a, b: None,
            initialization_is_selection_change=False,
            highlight_offFocus="list_off_focus",
        )
        self.listbox = ChannelsListArea(urwid.Filler(self.ilb, height=urwid.RELATIVE_100), title="Channels")
        self.listbox.delegate = self

    def _compose_list_widgets(self):
        widgets = []
        manager = self.app.rrc

        if not manager.hubs:
            entry = urwid.AttrMap(urwid.Text("\n  No hubs yet. Press Ctrl-N to add one."), "list_unknown")
            widgets.append(entry)
            self.list_widgets = widgets
            return

        g = self.app.ui.glyphs
        for hub_idx, hub in enumerate(manager.hubs):
            if hub_idx > 0:
                spacer = urwid.Text("")
                spacer.row_kind = "spacer"
                widgets.append(spacer)
            if hub.status == RRCHub.STATUS_CONNECTED:
                status_glyph = g["check"]
                style = "list_trusted"
            elif hub.status == RRCHub.STATUS_CONNECTING:
                status_glyph = g["info"]
                style = "list_unresponsive"
            elif hub.status == RRCHub.STATUS_FAILED:
                status_glyph = g["cross"]
                style = "list_untrusted"
            else:
                status_glyph = " "
                style = "list_unknown"

            entry = ChannelListEntry(status_glyph+" "+hub.name)
            urwid.connect_signal(entry, "click", self._select_hub, hub)
            attr = urwid.AttrMap(entry, style, "list_focus")
            attr.row_kind = "hub"
            attr.hub = hub
            attr.room = None
            widgets.append(attr)

            for room in sorted(list(hub.rooms | set(hub.messages.keys()))):
                if not room:
                    continue
                is_joined = room in hub.rooms
                mentioned = room in hub.mention_rooms
                unread = room in hub.unread_rooms
                if mentioned:
                    marker = g["warning"]
                    room_style = "irc_mention"
                elif unread:
                    marker = g["unread"]
                    room_style = "list_unresponsive"
                elif not is_joined:
                    marker = " "
                    room_style = "list_unknown"
                else:
                    marker = " "
                    room_style = "list_trusted" if hub.status == RRCHub.STATUS_CONNECTED else "list_unknown"
                room_entry = ChannelListEntry("   "+marker+" #"+room)
                urwid.connect_signal(room_entry, "click", self._select_room, (hub, room))
                room_attr = urwid.AttrMap(room_entry, room_style, "list_focus")
                room_attr.row_kind = "room"
                room_attr.hub = hub
                room_attr.room = room
                widgets.append(room_attr)

        self.list_widgets = widgets

    def update_list(self):
        prev_key = self.selected_key
        self._compose_list_widgets()
        self.ilb = IndicativeListBox(
            self.list_widgets,
            on_selection_change=lambda a, b: None,
            initialization_is_selection_change=False,
            highlight_offFocus="list_off_focus",
        )
        self.listbox = ChannelsListArea(urwid.Filler(self.ilb, height=urwid.RELATIVE_100), title="Channels")
        self.listbox.delegate = self

        if not self.dialog_open and self.channel_list_visible:
            options = self.columns_widget.options(urwid.GIVEN, ChannelsDisplay.given_list_width)
            self.columns_widget.contents[0] = (self.listbox, options)

        if prev_key is not None:
            for idx, w in enumerate(self.list_widgets):
                key = self._row_key(w)
                if key == prev_key:
                    try: self.ilb.select_item(idx)
                    except Exception: pass
                    break

        self._refresh_active_header()
        try:
            self.app.ui.loop.draw_screen()
        except Exception:
            pass

    def _row_key(self, w):
        if not hasattr(w, "row_kind"):
            return None
        if w.row_kind == "hub":
            return ("hub", w.hub.hub_hash, w.hub.dest_name)
        if w.row_kind == "room":
            return ("room", w.hub.hub_hash, w.hub.dest_name, w.room)
        return None

    def _refresh_active_header(self):
        if self.current_room_widget is not None:
            try:
                self.current_room_widget._update_peer_info()
            except Exception:
                pass
            return
        if self.selected_key and self.selected_key[0] == "hub":
            for h in self.app.rrc.hubs:
                if h.hub_hash == self.selected_key[1] and h.dest_name == self.selected_key[2]:
                    self._show_hub_info(h)
                    break

    def _select_hub(self, sender, hub):
        self.selected_key = ("hub", hub.hub_hash, hub.dest_name)
        self.app.rrc.set_active(hub, None)
        self._maybe_autoconnect(hub)
        self._show_hub_info(hub)

    def _select_room(self, sender, payload):
        hub, room = payload
        self.selected_key = ("room", hub.hub_hash, hub.dest_name, room)
        self.app.rrc.set_active(hub, room)
        self._maybe_autoconnect(hub)
        if room not in hub.rooms:
            if hub.status == RRCHub.STATUS_CONNECTED:
                try: hub.join_room(room)
                except Exception as e: RNS.log("Auto-join failed: "+str(e), RNS.LOG_ERROR)
            else:
                try: hub.add_room(room)
                except Exception as e: RNS.log("Pending join queue failed: "+str(e), RNS.LOG_ERROR)
        self._show_room(hub, room)

    def _maybe_autoconnect(self, hub):
        if hub.status in (RRCHub.STATUS_DISCONNECTED, RRCHub.STATUS_FAILED):
            try:
                hub.connect()
            except Exception as e:
                RNS.log("Auto-connect failed: "+str(e), RNS.LOG_ERROR)

    def _show_hub_info(self, hub):
        g = self.app.ui.glyphs
        status_label = {
            RRCHub.STATUS_DISCONNECTED: "Disconnected",
            RRCHub.STATUS_CONNECTING:   "Connecting",
            RRCHub.STATUS_CONNECTED:    "Connected",
            RRCHub.STATUS_FAILED:       "Failed",
        }.get(hub.status, "")
        status_attr = {
            RRCHub.STATUS_DISCONNECTED: "list_unknown",
            RRCHub.STATUS_CONNECTING:   "list_unresponsive",
            RRCHub.STATUS_CONNECTED:    "connected_status",
            RRCHub.STATUS_FAILED:       "list_untrusted",
        }.get(hub.status, "list_unknown")

        lines = [
            urwid.Text(""),
            urwid.Text("  Hub      : "+hub.name),
            urwid.Text("  Address  : "+hub.hub_hash.hex()),
            urwid.AttrMap(urwid.Text("  Status   : "+status_label+" ("+hub.status_text+")"), status_attr),
        ]
        if hub.hub_name:
            ver = " v"+str(hub.hub_version) if hub.hub_version else ""
            lines.append(urwid.Text("  Server   : "+str(hub.hub_name)+ver))

        ar_glyph = g["check"] if hub.auto_reconnect else g["cross"]
        ar_attr  = "list_trusted" if hub.auto_reconnect else "list_unknown"
        ar_text  = "On" if hub.auto_reconnect else "Off"
        lines.append(urwid.AttrMap(urwid.Text("  AutoRcn  : "+ar_glyph+" "+ar_text+"  (Ctrl-T to toggle)"), ar_attr))

        al_glyph = g["check"] if hub.auto_list else g["cross"]
        al_attr  = "list_trusted" if hub.auto_list else "list_unknown"
        al_text  = "On" if hub.auto_list else "Off"
        lines.append(urwid.AttrMap(urwid.Text("  AutoList : "+al_glyph+" "+al_text+"  (Ctrl-E to edit)"), al_attr))

        aw_glyph = g["check"] if hub.auto_who else g["cross"]
        aw_attr  = "list_trusted" if hub.auto_who else "list_unknown"
        aw_text  = "On" if hub.auto_who else "Off"
        lines.append(urwid.AttrMap(urwid.Text("  AutoWho  : "+aw_glyph+" "+aw_text+"  (Ctrl-E to edit)"), aw_attr))

        lines.append(urwid.Divider(g["divider1"]))

        if hub.status == RRCHub.STATUS_CONNECTED:
            lines.append(urwid.Text("  Connected. Use Ctrl-A to add a room."))
        elif hub.status == RRCHub.STATUS_CONNECTING:
            lines.append(urwid.AttrMap(urwid.Text("  Connecting..."), "list_unresponsive"))
        else:
            lines.append(urwid.Text("  Use Ctrl-R to connect."))

        if hub.motd:
            lines.append(urwid.Divider(g["divider1"]))
            lines.append(urwid.Text("  MOTD:"))
            motd_delegate = _ChatLinkDelegate(self, hub)
            try:
                motd_widgets = markup_to_attrmaps(_linkify_motd(hub.motd), url_delegate=motd_delegate)
            except Exception:
                motd_widgets = [urwid.Text(hub.motd)]
            for w in motd_widgets:
                lines.append(urwid.Padding(w, left=2))

        if hub.rooms:
            lines.append(urwid.Divider(g["divider1"]))
            lines.append(urwid.Text("  Joined rooms:"))
            for r in sorted(hub.rooms):
                entry = ChannelListEntry("    #"+r)
                urwid.connect_signal(entry, "click", self._select_room, (hub, r))
                lines.append(urwid.AttrMap(entry, "list_trusted", "list_focus"))

        available = sorted(
            (name, topic) for name, topic in hub.available_rooms.items()
            if name and name not in hub.rooms
        )
        if available:
            lines.append(urwid.Divider(g["divider1"]))
            lines.append(urwid.Text("  Available rooms:"))
            for name, topic in available:
                label = "    #"+name
                if topic:
                    label += "  "+g["arrow_r"]+" "+topic
                entry = ChannelListEntry(label)
                urwid.connect_signal(entry, "click", self._select_room, (hub, name))
                lines.append(urwid.AttrMap(entry, "list_unknown", "list_focus"))

        body = ScrollBar(Scrollable(urwid.Pile(lines)), thumb_char="┃", trough_char=" ")
        info = HubInfoArea(urwid.AttrMap(body, "scrollbar"), title=hub.name)
        info.delegate = self
        self.current_room_widget = None
        self._set_right_widget(info)
        self.shortcuts_display = self.list_shortcuts
        self.app.ui.main_display.update_active_shortcuts()

    def show_placeholder(self):
        self.current_room_widget = None
        self.selected_key = None
        self._set_right_widget(self.placeholder)
        self.shortcuts_display = self.list_shortcuts
        self.app.ui.main_display.update_active_shortcuts()

    def _show_room(self, hub, room):
        widget = RoomWidget(self, hub, room)
        self.current_room_widget = widget
        self._set_right_widget(widget)
        self._restore_room_draft(widget)
        self.columns_widget.focus_position = len(self.columns_widget.contents)-1
        self.shortcuts_display = self.room_shortcuts
        self.app.ui.main_display.update_active_shortcuts()

    def _selected_row(self):
        item = self.ilb.get_selected_item()
        if item is None:
            return None
        return item

    def connect_selected(self):
        item = self._selected_row()
        if item is None or not hasattr(item, "hub"):
            return
        try:
            item.hub.connect()
        except Exception as e:
            RNS.log("Connect failed: "+str(e), RNS.LOG_ERROR)

    def disconnect_selected(self):
        item = self._selected_row()
        if item is None or not hasattr(item, "hub"):
            return
        try:
            item.hub.disconnect()
        except Exception:
            pass

    def toggle_auto_reconnect_selected(self):
        item = self._selected_row()
        if item is None or not hasattr(item, "hub"):
            return
        item.hub.set_auto_reconnect(not item.hub.auto_reconnect)
        if self.current_room_widget is None:
            self._show_hub_info(item.hub)

    def remove_selected_dialog(self):
        item = self._selected_row()
        if item is None or not hasattr(item, "hub"):
            return
        hub = item.hub
        room = getattr(item, "room", None)

        def confirmed(sender):
            self.close_dialog()
            if room is not None:
                try: hub.part_room(room)
                except Exception: pass
                hub.remove_room(room)
            else:
                self.app.rrc.remove_hub(hub)
            self.update_list()
            self.show_placeholder()

        def dismiss(sender):
            self.close_dialog()

        if room is not None:
            prompt = "Leave and remove room\n#"+room+"\non hub "+hub.name+"?"
        else:
            prompt = "Remove hub\n"+hub.name+"\nfrom this client?\n All Message history will be discarded."

        dialog = ChannelsDialogLineBox(
            urwid.Pile([
                urwid.Text(prompt+"\n", align=urwid.CENTER),
                urwid.Columns([
                    (urwid.WEIGHT, 0.45, urwid.Button("Yes", on_press=confirmed)),
                    (urwid.WEIGHT, 0.1, urwid.Text("")),
                    (urwid.WEIGHT, 0.45, urwid.Button("No",  on_press=dismiss)),
                ])
            ]), title="?"
        )
        dialog.delegate = self
        self._show_dialog_overlay(dialog)

    def new_hub_dialog(self):
        e_hash = ReadlineEdit(caption="Hub address : ", edit_text="")
        e_name = ReadlineEdit(caption="Display name: ", edit_text="")
        error_text = urwid.Text("")

        def dismiss(sender):
            self.close_dialog()

        def confirmed(sender):
            try:
                hh_text = e_hash.get_edit_text().strip().lower()
                if hh_text.startswith("0x"):
                    hh_text = hh_text[2:]
                hh = bytes.fromhex(hh_text)
                if len(hh) != RNS.Reticulum.TRUNCATED_HASHLENGTH//8:
                    raise ValueError("Hash length must be "+str(RNS.Reticulum.TRUNCATED_HASHLENGTH//8)+" bytes")
                nm = e_name.get_edit_text().strip() or None
                self.app.rrc.add_hub(hh, name=nm)
                self.close_dialog()
                self.update_list()
            except Exception as e:
                error_text.set_text(("error_text", "Could not add hub: "+str(e)))

        dialog = ChannelsDialogLineBox(
            urwid.Pile([
                e_hash,
                e_name,
                urwid.Text(""),
                error_text,
                urwid.Columns([
                    (urwid.WEIGHT, 0.45, urwid.Button("Add",  on_press=confirmed)),
                    (urwid.WEIGHT, 0.1, urwid.Text("")),
                    (urwid.WEIGHT, 0.45, urwid.Button("Back", on_press=dismiss)),
                ])
            ]), title="New Hub"
        )
        dialog.delegate = self
        self._show_dialog_overlay(dialog)

    def confirm_new_hub_dialog(self, hub_hash, dest_name, room):
        error_text = urwid.Text("")

        def dismiss(sender):
            self.close_dialog()

        def confirmed(sender):
            try:
                hub = self.app.rrc.add_hub(hub_hash, dest_name=dest_name)
                self.close_dialog()
                self.update_list()
                if room:
                    self._select_room(None, (hub, room))
                else:
                    self._select_hub(None, hub)
            except Exception as e:
                error_text.set_text(("error_text", "Could not add hub: "+str(e)))

        dialog = ChannelsDialogLineBox(
            urwid.Pile([
                urwid.Text(""),
                urwid.Text("  A page is requesting to open an RRC hub."),
                urwid.Text(""),
                urwid.Text("  Address : "+hub_hash.hex()),
                urwid.Text("  Aspect  : "+(dest_name or "rrc.hub")),
                urwid.Text("  Room    : "+("#"+room if room else "(none)")),
                urwid.Text(""),
                urwid.AttrMap(urwid.Text(
                    "  Opening will add this hub to your client,"), "list_unknown"),
                urwid.AttrMap(urwid.Text(
                    "  and reveal your identity hash to the hub"), "list_unknown"),
                urwid.AttrMap(urwid.Text(
                    "  to the hub operator."), "list_unknown"),
                urwid.Text(""),
                error_text,
                urwid.Columns([
                    (urwid.WEIGHT, 0.45, urwid.Button("Open",   on_press=confirmed)),
                    (urwid.WEIGHT, 0.1,  urwid.Text("")),
                    (urwid.WEIGHT, 0.45, urwid.Button("Cancel", on_press=dismiss)),
                ])
            ]), title="Open RRC hub?"
        )
        dialog.delegate = self
        self._show_dialog_overlay(dialog)

    def edit_hub_dialog(self):
        item = self._selected_row()
        if item is None or not hasattr(item, "hub"):
            return
        hub = item.hub

        e_name = ReadlineEdit(caption="Display name : ", edit_text=hub.name or "")
        cb_autorcn  = urwid.CheckBox("Auto-reconnect on disconnect", state=hub.auto_reconnect)
        cb_autolist = urwid.CheckBox("Auto-fetch room list on connect", state=hub.auto_list)
        cb_autowho  = urwid.CheckBox("Auto-fetch members on room join", state=hub.auto_who)
        error_text = urwid.Text("")

        def dismiss(sender):
            self.close_dialog()

        def confirmed(sender):
            try:
                nm = e_name.get_edit_text().strip() or hub.name
                hub.name = nm
                hub.set_auto_reconnect(cb_autorcn.get_state(), save=False)
                hub.set_auto_list(cb_autolist.get_state(), save=False)
                hub.set_auto_who(cb_autowho.get_state(), save=False)
                self.app.rrc.save()
                self.close_dialog()
                self.update_list()
                if self.selected_key and self.selected_key[0] == "hub" and self.selected_key[1] == hub.hub_hash:
                    self._show_hub_info(hub)
            except Exception as e:
                error_text.set_text(("error_text", "Could not save: "+str(e)))

        dialog = ChannelsDialogLineBox(
            urwid.Pile([
                urwid.Text(" Address : "+hub.hub_hash.hex()),
                urwid.Text(" Server  : "+(hub.hub_name or "(unknown until connected)")),
                urwid.Divider(self.app.ui.glyphs["divider1"]),
                e_name,
                urwid.Text(""),
                cb_autorcn,
                cb_autolist,
                cb_autowho,
                urwid.Text(""),
                error_text,
                urwid.Columns([
                    (urwid.WEIGHT, 0.45, urwid.Button("Save", on_press=confirmed)),
                    (urwid.WEIGHT, 0.1, urwid.Text("")),
                    (urwid.WEIGHT, 0.45, urwid.Button("Back", on_press=dismiss)),
                ])
            ]), title="Edit Hub"
        )
        dialog.delegate = self
        self._show_dialog_overlay(dialog)

    def join_room_dialog(self):
        item = self._selected_row()
        hub = None
        if item is not None and hasattr(item, "hub"):
            hub = item.hub
        if hub is None:
            if self.app.rrc.hubs:
                hub = self.app.rrc.hubs[0]
            else:
                return

        e_room = ReadlineEdit(caption="Room : #", edit_text="")
        e_key  = ReadlineEdit(caption="Key  : ",  edit_text="", mask="*")
        error_text = urwid.Text("")

        key_section_placeholder = urwid.WidgetPlaceholder(urwid.Text(""))

        def update_key_visibility(checkbox, state):
            if state:
                key_section_placeholder.original_widget = e_key
            else:
                key_section_placeholder.original_widget = urwid.Text("")

        cb_key = urwid.CheckBox("Keyed room (+k)", state=False, on_state_change=update_key_visibility)

        def dismiss(sender):
            self.close_dialog()

        def confirmed(sender):
            try:
                room = e_room.get_edit_text().strip()
                if not room:
                    raise ValueError("Room name is required")
                key = e_key.get_edit_text().strip() if cb_key.get_state() else None
                key = key or None
                hub.add_room(room)
                if hub.status == RRCHub.STATUS_CONNECTED:
                    hub.join_room(room, key=key)
                self.close_dialog()
                self.update_list()
                self._select_room(None, (hub, room.lower()))
            except Exception as e:
                error_text.set_text(("error_text", "Could not join: "+str(e)))

        dialog = ChannelsDialogLineBox(
            urwid.Pile([
                urwid.Text(" Hub : "+hub.name),
                e_room,
                cb_key,
                key_section_placeholder,
                urwid.Text(""),
                error_text,
                urwid.Columns([
                    (urwid.WEIGHT, 0.45, urwid.Button("Join", on_press=confirmed)),
                    (urwid.WEIGHT, 0.1, urwid.Text("")),
                    (urwid.WEIGHT, 0.45, urwid.Button("Back", on_press=dismiss)),
                ])
            ]), title="Add Room"
        )
        dialog.delegate = self
        self._show_dialog_overlay(dialog)

    def show_user_info(self, sender, payload):
        try:
            hub, peer_hash, display_name = payload
        except Exception:
            return
        if not isinstance(peer_hash, (bytes, bytearray)):
            return

        peer_hash    = bytes(peer_hash)
        identity_hex = RNS.hexrep(peer_hash, delimit=False)
        own_hash     = self.app.identity.hash if self.app.identity is not None else None
        is_self      = (own_hash is not None and peer_hash == own_hash)

        lxmf_hex = None
        try:
            peer_identity = RNS.Identity.recall(peer_hash, from_identity_hash=True)
            if peer_identity is not None:
                lxmf_dest = RNS.Destination.hash_from_name_and_identity("lxmf.delivery", peer_identity)
                lxmf_hex  = RNS.hexrep(lxmf_dest, delimit=False)
        except Exception:
            pass

        def on_close(_b):
            self.close_dialog()

        def on_open(_b):
            self.close_dialog()
            if lxmf_hex is None:
                return
            try:
                _ChatLinkDelegate(self, hub)._open_lxmf(lxmf_hex)
            except Exception as e:
                RNS.log("Could not open conversation: "+str(e), RNS.LOG_ERROR)

        safe_name = ""
        try:
            if display_name:
                if self.app.config["textui"]["sanitize_names"]:
                    safe_name = sanitize_name(str(display_name)) or ""
                else:
                    safe_name = strip_modifiers(str(display_name)) or ""
        except Exception:
            safe_name = ""

        lines = [
            urwid.Text(""),
            urwid.Text(" Nick     : "+safe_name),
            urwid.Text(" Identity : "+identity_hex),
        ]
        if lxmf_hex:
            lines.append(urwid.Text(" LXMF     : "+lxmf_hex))

        if is_self:
            lines.append(urwid.Text(""))
            lines.append(urwid.Text(" (This is you)", align=urwid.CENTER))
            lines.append(urwid.Text(""))
            lines.append(urwid.Columns([
                (urwid.WEIGHT, 1, urwid.Button("Close", on_press=on_close)),
            ]))
        else:
            if lxmf_hex is None:
                lines.append(urwid.Text(""))
                lines.append(urwid.Text(" Identity not in local cache;", align=urwid.CENTER))
                lines.append(urwid.Text(" conversation can't be opened until", align=urwid.CENTER))
                lines.append(urwid.Text(" the peer announces.", align=urwid.CENTER))
                lines.append(urwid.Text(""))
                lines.append(urwid.Columns([
                    (urwid.WEIGHT, 1, urwid.Button("Close", on_press=on_close)),
                ]))
            else:
                lines.append(urwid.Text(""))
                lines.append(urwid.Columns([
                    (urwid.WEIGHT, 0.55, urwid.Button("Open Conversation", on_press=on_open)),
                    (urwid.WEIGHT, 0.05, urwid.Text("")),
                    (urwid.WEIGHT, 0.40, urwid.Button("Close", on_press=on_close)),
                ]))

        dialog = ChannelsDialogLineBox(urwid.Pile(lines), title="User Info")
        dialog.delegate = self
        self._show_dialog_overlay(dialog)

    def _show_dialog_overlay(self, dialog):
        self.dialog_open = True
        overlay = urwid.Overlay(
            dialog,
            self.columns_widget,
            align=urwid.CENTER,
            width=(urwid.RELATIVE, 60),
            min_width=40,
            valign=urwid.MIDDLE,
            height=urwid.PACK,
        )
        self.widget.original_widget = overlay

    def close_dialog(self):
        self.dialog_open = False
        self.widget.original_widget = self.columns_widget

    def _process_pending(self, data):
        while True:
            try:
                action = self._pending_actions.popleft()
            except IndexError:
                break
            try:
                action()
            except Exception as e:
                RNS.log("RRC UI action failed: "+str(e), RNS.LOG_ERROR)
        return True

    def _wake(self, action):
        self._pending_actions.append(action)
        if self._wake_fd is not None:
            try:
                os.write(self._wake_fd, b".")
                return
            except Exception:
                pass
        try:
            self.app.ui.loop.set_alarm_in(0.0, lambda l, d: self._process_pending(None))
        except Exception:
            pass

    def _on_rrc_change(self, hub):
        def action():
            self.update_list()
            if (self.current_room_widget is not None
                    and self.current_room_widget.hub is hub):
                try:
                    self.current_room_widget._refresh_users_pane()
                except Exception:
                    pass
            elif (self.selected_key
                    and self.selected_key[0] == "hub"
                    and self.selected_key[1] == hub.hub_hash
                    and self.selected_key[2] == hub.dest_name):
                try:
                    self._show_hub_info(hub)
                except Exception:
                    pass
        self._wake(action)

    def _on_rrc_message(self, hub, msg):
        def action():
            is_active = (self.current_room_widget is not None
                         and self.current_room_widget.hub is hub
                         and self.current_room_widget.room == msg.room)
            if getattr(msg, "mention", False) and not is_active:
                self._ring_mention_bell(hub, msg.room)
            if is_active:
                self.current_room_widget.append_message(msg)
            self.update_list()
        self._wake(action)

    def _ring_mention_bell(self, hub, room):
        key = (hub.hub_hash, room or "")
        now = time.monotonic()
        last = self._mention_bell_last.get(key, 0.0)
        if now - last < 5.0:
            return
        self._mention_bell_last[key] = now
        try:
            import sys
            sys.stdout.write("\x07")
            sys.stdout.flush()
        except Exception:
            pass
