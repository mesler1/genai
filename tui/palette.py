"""hoplon-tui's colours: "bronze on iron". One place for every curses screen and the tmux chrome.

Each token has its design colour (hex, used by tmux, which converts it as the terminal allows), a 256-colour
code for curses, and a 16-colour fallback. NO_COLOR turns colour off (bold and dim stay). The terminal's own
background is kept: the panes sit beside your shell, so hoplon-tui doesn't paint over it.
"""
import os

# token: (hex, 256-colour code, 16-colour fallback by curses name)
TOKENS = {
    "dim":    ("#7E858E", 245, "WHITE"),     # hints, timestamps, quiet text (8 on 16-colour terminals)
    "muted":  ("#9AA0A8", 247, "WHITE"),     # labels
    "line":   ("#2A3036", 236, "BLACK"),     # pane borders, unfocused
    "bronze": ("#D4A05A", 179, "YELLOW"),    # the brand: accents, key hints, the focused border
    "brass":  ("#E3B341", 178, "YELLOW"),    # waiting: pending approvals, warnings
    "patina": ("#7FC1B8", 109, "CYAN"),      # ok, running, done
    "ember":  ("#E8776A", 167, "RED"),       # failed, denied, risky
    "iron":   ("#0E1113", 233, "BLACK"),     # text on a bronze badge
}

# The screens' roles (conversation's Q, A, W, D, R) and the token each one uses.
ROLES = {"quiet": "dim", "accent": "bronze", "waiting": "brass", "done": "patina", "risk": "ember"}


def hexof(token):
    return TOKENS[token][0]


def enabled(environ=None):
    """False under NO_COLOR (https://no-color.org: set and not empty)."""
    return not (environ if environ is not None else os.environ).get("NO_COLOR")


def roles(curses, environ=None):
    """curses attributes for each role, plus "" (plain). Call after initscr; it sets up colour pairs 1 to 5."""
    out = {"": 0, "quiet": curses.A_DIM}
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    if not enabled(environ) or not curses.has_colors():
        out["accent"] = curses.A_BOLD
        out["risk"] = curses.A_BOLD
        out.update({r: out.get(r, 0) for r in ROLES})
        return out
    for i, (role, token) in enumerate(ROLES.items(), 1):
        _, code, name = TOKENS[token]
        if curses.COLORS >= 256:
            fg = code
        elif token == "dim" and curses.COLORS >= 16:
            fg = 8                                  # bright black
        else:
            fg = getattr(curses, "COLOR_" + name)
        curses.init_pair(i, fg, -1)
        out[role] = curses.color_pair(i)
    return out


def tmux(fg=None, bg=None, bold=False, environ=None):
    """A tmux style for status-line text, e.g. "#[fg=#D4A05A,bold]"; under NO_COLOR, only the bold."""
    parts = []
    if enabled(environ):
        parts += [f"fg={hexof(fg)}"] if fg else []
        parts += [f"bg={hexof(bg)}"] if bg else []
    parts += ["bold"] if bold else []
    return f"#[{','.join(parts)}]" if parts else ""
