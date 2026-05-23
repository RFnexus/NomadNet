import urwid


_KILL_KEYS = frozenset(("ctrl u", "ctrl k", "ctrl w", "ctrl l"))


class _KillRing:
    """Module-global kill buffer shared across all ReadlineMixin widgets.

    Mirrors GNU readline: consecutive kills accumulate into the same entry
    (forward kills append, backward kills prepend); any non-kill keypress
    breaks the chain so the next kill replaces the buffer.
    """
    text = ""
    last_was_kill = False

    @classmethod
    def reset_chain(cls):
        cls.last_was_kill = False

    @classmethod
    def kill(cls, killed, direction):
        if not killed:
            return
        if cls.last_was_kill:
            cls.text = cls.text + killed if direction == "forward" else killed + cls.text
        else:
            cls.text = killed
        cls.last_was_kill = True


class ReadlineMixin:
    """Mixin adding readline-style editing keys to an urwid.Edit-derived widget.

    Bindings (GNU readline defaults, plus ctrl-l as kill-whole-buffer):
        ctrl-a        beginning-of-line
        ctrl-e        end-of-line
        ctrl-u        unix-line-discard      (kill from cursor to beginning of line)
        ctrl-k        kill-line              (kill from cursor to end of line)
        ctrl-w        unix-word-rubout       (kill previous whitespace-delimited word)
        ctrl-l        kill-whole-buffer      (kill the entire edit buffer)
        ctrl-y        yank                   (insert most-recently-killed text)
        ctrl-left     backward-word          (alphanumeric boundary)
        ctrl-right    forward-word           (alphanumeric boundary)

    "Line" is the current logical line within the edit buffer, delimited by
    newlines -- so on multiline Edits these act on the line under the cursor,
    not the entire buffer.

    The kill buffer is shared across all widgets using this mixin, so text
    killed in one Edit can be yanked into another.
    """

    def keypress(self, size, key):
        if   key == "ctrl a":     self._rl_beg_of_line()
        elif key == "ctrl e":     self._rl_end_of_line()
        elif key == "ctrl u":     self._rl_kill_to_beg()
        elif key == "ctrl k":     self._rl_kill_to_end()
        elif key == "ctrl w":     self._rl_kill_word_back()
        elif key == "ctrl l":     self._rl_kill_whole_buffer()
        elif key == "ctrl y":     self._rl_yank()
        elif key == "ctrl left":  self._rl_backward_word()
        elif key == "ctrl right": self._rl_forward_word()
        else:
            result = super().keypress(size, key)
            if key not in _KILL_KEYS:
                _KillRing.reset_chain()
            return result
        if key not in _KILL_KEYS:
            _KillRing.reset_chain()
        return None

    def _rl_line_bounds(self):
        text, pos = self.edit_text, self.edit_pos
        bol = text.rfind("\n", 0, pos)
        bol = 0 if bol == -1 else bol + 1
        eol = text.find("\n", pos)
        eol = len(text) if eol == -1 else eol
        return bol, eol

    def _rl_delete(self, start, end, kill_direction=None):
        if start == end:
            return
        text = self.edit_text
        if kill_direction is not None:
            _KillRing.kill(text[start:end], kill_direction)
        self.set_edit_text(text[:start] + text[end:])
        self.set_edit_pos(start)

    @staticmethod
    def _rl_is_word_char(ch):
        return ch.isalnum() or ch == "_"

    def _rl_beg_of_line(self):
        bol, _ = self._rl_line_bounds()
        self.set_edit_pos(bol)

    def _rl_end_of_line(self):
        _, eol = self._rl_line_bounds()
        self.set_edit_pos(eol)

    def _rl_kill_to_beg(self):
        bol, _ = self._rl_line_bounds()
        self._rl_delete(bol, self.edit_pos, kill_direction="backward")

    def _rl_kill_to_end(self):
        _, eol = self._rl_line_bounds()
        self._rl_delete(self.edit_pos, eol, kill_direction="forward")

    def _rl_kill_word_back(self):
        text, pos = self.edit_text, self.edit_pos
        p = pos
        while p > 0 and text[p - 1].isspace():
            p -= 1
        while p > 0 and not text[p - 1].isspace():
            p -= 1
        self._rl_delete(p, pos, kill_direction="backward")

    def _rl_kill_whole_buffer(self):
        self._rl_delete(0, len(self.edit_text), kill_direction="forward")

    def _rl_yank(self):
        if not _KillRing.text:
            return
        pos = self.edit_pos
        text = self.edit_text
        self.set_edit_text(text[:pos] + _KillRing.text + text[pos:])
        self.set_edit_pos(pos + len(_KillRing.text))

    def _rl_backward_word(self):
        text, pos = self.edit_text, self.edit_pos
        while pos > 0 and not self._rl_is_word_char(text[pos - 1]):
            pos -= 1
        while pos > 0 and self._rl_is_word_char(text[pos - 1]):
            pos -= 1
        self.set_edit_pos(pos)

    def _rl_forward_word(self):
        text, pos = self.edit_text, self.edit_pos
        n = len(text)
        while pos < n and not self._rl_is_word_char(text[pos]):
            pos += 1
        while pos < n and self._rl_is_word_char(text[pos]):
            pos += 1
        self.set_edit_pos(pos)


class ReadlineEdit(ReadlineMixin, urwid.Edit):
    """Drop-in urwid.Edit replacement with readline-style editing keys."""
    pass
