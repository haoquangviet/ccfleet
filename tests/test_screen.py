"""Reading the console screen: busy detection and menu parsing must survive the noisy frames
`peek` returns while Claude Code animates its spinner. Deciding "busy" from the single string
"esc to interrupt" is not enough — the spinner redraw overwrites it — so a running session would
read as idle. Fixtures below are real frames captured from a thinking session plus hand-built
idle/menu frames.
"""
import unittest

import _setup  # noqa: F401  (isolated CCFLEET_HOME)
import fleet_web  # noqa: E402

# --- real noisy frames captured mid-spinner (only spinner rows survived the redraw) ---
BUSY_WRANGLING = (
    "  ✻  Wrangling…  36s · ↓   1.4k to\n"
    "  ✽  Wrangling…  5    ?25h\n"
    "  ✢  Wrangling…  1m 0s · ↓   1\n"
    "   Wrangling…  10s · ↓   1.6k tokens)  [2\n"
    "   Wrangling…    5h0;1H  ?25h\n"
)
BUSY_WHIRRING_WITH_HOOK = (
    "    ⎿   Updated  plans\\plan.md   (+1 -1\n"
    "  ✢  Whirring…  8m 48s · ↓   49.2k\n"
    "   running PostToolUse hook · 8m 51s · ↓ 49.3k tokens)    ?25h\n"
    "  ●   Bash (cp \"C:/Users/ADMINI~1/...\"\n"
)
# the classic marker still appears in some frames — must stay detected
BUSY_CLASSIC = (
    "  Some reply text being written\n"
    "  ✻  Crunching… (12s · esc to interrupt)\n"
)

# --- idle: a settled prompt, no spinner ---
IDLE_PROMPT = (
    "  ⎿  Done (3 tool uses · 12.4k tokens · 1m 2s)\n"
    "\n"
    "  I have updated the plan. Please take a look.\n"
    "\n"
    " ╭──────────────────────────────────────╮\n"
    " │ >                                    │\n"
    " ╰──────────────────────────────────────╯\n"
    "   ? for shortcuts\n"
)

# --- menu (AskUserQuestion-style), settled, no spinner ---
MENU_FRAME = (
    "  Which approach do you want?\n"
    "❯ 1. Filter the peek source\n"
    "  2. Stream over WebSocket\n"
    "  3. Transcript only\n"
    "\n"
    "  Enter to select · Esc to cancel\n"
)
# menu while a spinner frame is also in the buffer (must NOT be shown as a menu: still busy)
MENU_DURING_SPINNER = BUSY_WRANGLING + MENU_FRAME


class BusyDetection(unittest.TestCase):
    def test_spinner_frames_read_as_busy(self):
        for name, frame in [("wrangling", BUSY_WRANGLING),
                            ("whirring+hook", BUSY_WHIRRING_WITH_HOOK),
                            ("classic", BUSY_CLASSIC)]:
            with self.subTest(frame=name):
                self.assertTrue(fleet_web.is_busy(frame), f"{name} should be busy")

    def test_idle_and_menu_read_as_not_busy(self):
        self.assertFalse(fleet_web.is_busy(IDLE_PROMPT))
        self.assertFalse(fleet_web.is_busy(MENU_FRAME))

    def test_old_marker_alone_was_insufficient(self):
        # proves the regression: the real busy frames do NOT contain "esc to interrupt"
        self.assertNotIn("esc to interrupt", BUSY_WRANGLING.lower())
        self.assertNotIn("esc to interrupt", BUSY_WHIRRING_WITH_HOOK.lower())

    def test_no_false_positive_on_vietnamese_ellipsis(self):
        # a settled reply ending in "…" must NOT read as busy (common in replies, any language)
        for text in [
            "Đã cập nhật xong plan…\n Vui lòng xem lại.\n > ",   # Vietnamese reply
            "Đang chờ duyệt…\n > ",                # even "Đang…" alone is not a spinner
            "Bước tiếp: duyệt plan rồi code…\n > ",
            "Done. Updated the config…\n > ",
        ]:
            with self.subTest(text=text[:20]):
                self.assertFalse(fleet_web.is_busy(text), text)


class MenuParsing(unittest.TestCase):
    def test_menu_parsed_when_idle(self):
        menu = fleet_web.parse_choices(MENU_FRAME)
        self.assertIsNotNone(menu)
        self.assertEqual([o["n"] for o in menu["options"]], [1, 2, 3])
        self.assertEqual(menu["current"], 1)

    def test_question_header_kept_apart(self):
        # AskUserQuestion: header chip, blank line, (wrapped) question — never "Rate limit Where should…"
        frame = (" ☐ Rate limit\n\nWhere should the\nlimiter live?\n\n❯ 1. Middleware\n  2. Decorator\n"
                 "  3. Type something.\n\nEnter to select · Esc to cancel\n")
        menu = fleet_web.parse_choices(frame)
        self.assertEqual(menu["header"], "Rate limit")
        self.assertEqual(menu["question"], "Where should the limiter live?")
        self.assertEqual(fleet_web.parse_choices(MENU_FRAME)["question"], "Which approach do you want?")
        self.assertEqual(fleet_web.parse_choices(MENU_FRAME)["header"], "")

    def test_no_menu_while_busy(self):
        # even if menu-looking lines exist, a busy buffer must not surface a menu
        self.assertIsNone(fleet_web.menu_if_idle(MENU_DURING_SPINNER))

    def test_menu_if_idle_returns_menu_when_settled(self):
        self.assertIsNotNone(fleet_web.menu_if_idle(MENU_FRAME))
        self.assertIsNone(fleet_web.menu_if_idle(IDLE_PROMPT))


# Real live_text output captured while a session was thinking — escape-code residue plus torn
# spinner rows. live_status must ignore all of this and report a single clean status line.
NOISY_LIVE = (
    "5 [?25 ,  * r g 30 [? ,  ✛ i … 3 6 [28; . text fragment\n"
    "  ●  some real reply text that got torn[48\n"
    "   ng…  50    fixture (torn spinner)\n"
    "  ✳ i i 5 [?25\n"
    "  * r g 30 [?\n"
    "  ✛ i … 3 6 [28;\n"
    "   Whirring…  9m 7s · ↓ 50.7k tokens)  [30\n"
    "   h  m   5h  [H5h5h\n"
)


# A /model picker: 9 options, each with a trailing description, plus header/hint lines.
MODEL_MENU = (
    # scrollback above the menu
    "".join(f"  earlier reply line {i} of the conversation\n" for i in range(1, 15))
    + "  I have updated the plan.\n"
    "\n"
    "  Switch between Claude models. Your pick becomes the default for new sessions.\n"
    "\n"
    "    1. Default (recommended)        Opus 5 · Best for everyday\n"
    "    2. Sonnet 5                     Efficient for routine tasks\n"
    "    3. Haiku 4.5                    Fastest for simple tasks\n"
    "    4. Fable 5                      Most capable, longest-running\n"
    "    5. Opus 4.8                     Previous generation\n"
    "    6. Sonnet 5 (1M context)        Large context window\n"
    "    7. Opus 5 (1M context)          Large context window\n"
    "    8. Fable 5.1                    Newest, experimental\n"
    "  ❯ 9. Opus 4.8 ✓                   Currently selected\n"
    "\n"
    "  Enter to set as default · s to use in this session only · Esc to cancel\n"
)


class LongMenu(unittest.TestCase):
    """A tall picker (/model has 9 options) must survive parsing. The real-world truncation was the
    console window itself being only 30 rows — Claude Code then draws just the last few options, so
    ccfleet grows the console (console_io.resize) to the largest size Windows allows."""

    def test_all_nine_options_parsed(self):
        menu = fleet_web.parse_choices(MODEL_MENU)
        self.assertIsNotNone(menu)
        self.assertEqual([o["n"] for o in menu["options"]], list(range(1, 10)), "a long menu must keep all 9 options")

    def test_current_is_the_highlighted_row(self):
        self.assertEqual(fleet_web.parse_choices(MODEL_MENU)["current"], 9)

    def test_scroll_flag_set_only_when_the_picker_scrolls(self):
        # a fully drawn menu needs no arrows
        self.assertFalse(fleet_web.parse_choices(MODEL_MENU)["scroll"])
        # the real /model viewport: three rows with a ↓ marker → the UI must offer ↑/↓
        scrolling = (
            "  Switch between Claude models. Your pick becomes the default.\n"
            "  ❯ 7. Opus 5 ✓\n"
            "    8. Fable 5                 Most capable\n"
            "  ↓ 10. Opus 4.7               Best for everyday\n"
            "  Enter to set as default · Esc to cancel\n"
        )
        menu = fleet_web.parse_choices(scrolling)
        self.assertIsNotNone(menu)
        self.assertTrue(menu["scroll"], "a scrolling menu must set the scroll flag")

    def test_labels_keep_their_text(self):
        opts = fleet_web.parse_choices(MODEL_MENU)["options"]
        self.assertIn("Sonnet 5", opts[1]["label"])
        self.assertIn("Fable 5.1", opts[7]["label"])


class LiveStatus(unittest.TestCase):
    def test_status_from_spinner_line(self):
        s = fleet_web.live_status(NOISY_LIVE)
        self.assertIsNotNone(s)
        self.assertIn("50.7k", s)       # token count surfaced
        self.assertNotIn("[?25", s)     # no escape residue
        self.assertNotIn("[28", s)
        self.assertNotIn("✳", s)        # no raw throbber glyph

    def test_status_none_when_idle(self):
        self.assertIsNone(fleet_web.live_status(IDLE_PROMPT))

    def test_live_text_drops_escape_residue_lines(self):
        out = fleet_web.live_text(NOISY_LIVE)
        self.assertNotIn("[?25", out)
        self.assertNotIn("[28;", out)
        for frag in ("✳ i i", "* r g 30", "✛ i …"):
            self.assertNotIn(frag, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
