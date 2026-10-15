"""Picker: groups (left) · sessions of the group (right) · detail (bottom). Rescans every 5 s; ● = running."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .. import config, groups as G, labeler as L
from ..digests import title_of
from ..i18n import t
from ..links import Link, handoff_cmd, is_url, md_section, resume_cmd, session_cwd
from ..model import DAYS, RELOAD_SEC, SOURCES, Model
from ..util import fmt_date, fmt_dt, fmt_size, fold, rel_path, rel_time

ROLE_COLOR = {"research": "blue", "code": "green", "test": "magenta", "docs": "cyan", "ops": "red", "env": "yellow",
              "qa": "bright_black"}  # ANSI names ⇒ the terminal adapts them to light and dark themes
STATUS_ICON = {"done": ("✓", "green"), "in_progress": ("…", "yellow"), "waiting": ("⏳", "blue"), "abandoned": ("✗", "bright_black")}
KIND_STYLE = {"ticket": "bold blue", "pr": "bold green", "artifact": "magenta", "doc": "cyan", "backlink": "cyan", "plan": "green", "memory": "yellow",
              "code": ""}
PREVIEW_MAX = 300 * 1024
MD_RENDER_MAX = 100 * 1024  # bigger .md ⇒ TextArea (rendering 230 KB of Markdown takes ~5 s and delays Esc)
CODE_ROW_MAX = 12  # other files in the detail: the rest is in the `o` list


def group_cols():
    return (("dot", " ", 1), ("key", t("col.key"), 9), ("st", " ", 2), ("name", t("col.group"), 0), ("n", "n", 3),
            ("when", t("col.when"), 8))


def session_cols():
    return (("dot", " ", 1), ("when", t("col.when"), 8), ("st", " ", 2), ("role", t("col.role"), 10),
            ("name", t("col.summary"), 0), ("tool", " ", 1), ("size", t("col.size"), 5))


def cut(s, w, style=""):
    x = Text(" ".join((s or "").split()), style=style, no_wrap=True)
    x.truncate(max(1, w), overflow="ellipsis")
    return x


def dot(state):
    """● the agent is working · ◐ the session is open and waiting for you."""
    if state == "busy":
        return Text("●", style="bold green")
    if state == "open":
        return Text("◐", style="bold yellow")
    return Text(" ")


def key_cell(key):
    if G.is_ticket(key):
        return Text(key, style="bold blue")
    if key == G.UNGROUPED:
        return Text("—", style="dim")
    return Text("◆", style="magenta")  # LLM topic or user-made group


def role_label(role):
    return t("role." + role) if role in ROLE_COLOR else ""


def role_cell(role):
    return Text(role_label(role), style=ROLE_COLOR.get(role or "", "dim"))


def status_cell(lb):
    """LLM-judged status; no status yet ⇒ blank, unknown value ⇒ ?."""
    if "status" not in lb:
        return Text("  ")
    icon, color = STATUS_ICON.get(lb["status"], ("?", "dim"))
    return Text(icon, style=color)


def status_text(lb):
    if "status" not in lb:
        return None
    icon, color = STATUS_ICON.get(lb["status"], ("?", "dim"))
    x = Text.assemble((icon + " " + (t("status." + lb["status"]) if lb["status"] in STATUS_ICON else t("status.unknown")),
                       "bold " + color))
    if lb.get("reason"):  # a hard signal (merged PR), not the model's opinion
        x.append(" — " + lb["reason"])
        return x
    if lb.get("next"):
        x.append(" — " + lb["next"])
    x.append("   " + t("detail.llm_judged"), style="dim italic")
    return x


def link_text(lk, i, src=False):
    """Clickable text ⇒ app.open_link(i, target). A file that is gone: struck through, dim."""
    style = Style.parse(KIND_STYLE.get(lk.kind) or "default") if lk.exists else Style(dim=True, strike=True)
    x = Text(lk.label, style=style + Style(meta={"@click": "app.open_link(%d, %r)" % (i, lk.target)}))
    if src and lk.src:
        x.append(" ·" + lk.src[:8], style="dim")
    return x


def stack(*renderables):
    g = Table.grid()
    g.add_column()
    for r in renderables:
        g.add_row(r)
    return g


class PaneTable(DataTable):
    BINDINGS = [
        Binding("left", "app.focus_pane('groups')", show=False),
        Binding("right", "app.focus_pane('sessions')", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("tab,shift+tab", "app.switch_pane", show=False),
    ]
    armed = False  # ≥ 1 click inside this table since it last got focus

    def on_blur(self, event):
        self.armed = False

    async def _on_click(self, event):
        """The stock DataTable treats a click on the highlighted cell as a selection, so one click coming from the
        other pane would open a session. Here a click only moves the cursor; clicking the highlighted row again
        (after an earlier click in this table) is Enter."""
        row = event.style.meta.get("row")
        if row is None or row < 0 or row >= self.row_count:
            return  # header click…: leave it to DataTable._on_click (Textual calls handlers along the MRO)
        event.prevent_default()  # stop DataTable._on_click
        event.stop()
        again = self.armed and row == self.cursor_row
        self.armed = True
        self.move_cursor(row=row)
        if again:
            self.post_message(DataTable.RowSelected(self, row, self.ordered_rows[row].key))

    def setup(self, cols):
        """Rebuild columns for the current width; width 0 takes the rest. → that flexible width."""
        fixed = sum(w for _, _, w in cols)
        avail = (self.size.width or 80) - 2 * self.cell_padding * len(cols) - 1  # size excludes the border; 1 for the scrollbar
        flex = max(8, avail - fixed)
        self.clear(columns=True)
        for key, label, w in cols:
            self.add_column(label, width=w or flex, key=key)
        return flex


# ---------- dialogs ----------
class Confirm(ModalScreen):
    BINDINGS = [Binding("enter", "pick('fork')", "fork"), Binding("r", "pick('resume')", "resume"),
                Binding("escape", "pick('')", "cancel")]

    def __init__(self, text):
        super().__init__()
        self.text = text

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.text)
            yield Static(Text.assemble(("Enter", "bold"), " " + t("confirm.fork") + " · ", ("r", "bold"), " " + t("confirm.resume")
                                       + " · ", ("Esc", "bold"), " " + t("cancel")), classes="hint")

    def action_pick(self, v):
        self.dismiss(v or None)


class PickAgent(ModalScreen):
    """Which agent picks up the work item."""
    BINDINGS = [Binding("c", "pick('claude')", "Claude"), Binding("x", "pick('codex')", "Codex"),
                Binding("enter", "pick('claude')", show=False), Binding("escape", "pick('')", "cancel")]

    def __init__(self, title):
        super().__init__()
        self.title_ = title

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_), classes="dlg-title")
            yield Static(Text.assemble(("c", "bold"), " Claude Code · ", ("x", "bold"), " Codex · ", ("Esc", "bold"),
                                       " " + t("cancel")), classes="hint")

    def action_pick(self, v):
        self.dismiss(v or None)


class AskLabeler(ModalScreen):
    """First run: ask before any session data goes to an LLM. The answer is kept for this workspace."""
    BINDINGS = [Binding("y", "answer(True)", "yes"), Binding("n,escape", "answer(False)", "no")]

    def __init__(self, n):
        super().__init__()
        self.n = n

    def compose(self):
        minutes, usd = L.estimate(self.n)
        with Vertical(classes="dialog wide"):
            yield Static(t("consent.title"), classes="dlg-title")
            yield Static(Text(t("consent.body", what=t("consent.what"), n=self.n, min=minutes, usd="%.2f" % usd)))
            yield Static(Text.assemble(("y", "bold"), " " + t("consent.yes") + " · ", ("n", "bold"), " " + t("consent.no")),
                         classes="hint")

    def action_answer(self, yes):
        self.dismiss(yes)


class AskText(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title, value=""):
        super().__init__()
        self.title_, self.value = title, value

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_), classes="dlg-title")
            yield Input(self.value, id="ask")
            yield Static(t("ask.hint"), classes="hint")

    def on_input_submitted(self, e):
        e.stop()
        self.dismiss(e.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


class PickGroup(ModalScreen):
    """Choose a target group: type to filter; '+ New group' is always last so Enter picks the matching group."""
    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("down", "move(1)", show=False), Binding("up", "move(-1)", show=False)]

    def __init__(self, title, groups):
        super().__init__()
        self.title_, self.groups = title, groups

    def compose(self):
        with Vertical(classes="dialog wide"):
            yield Static(Text(self.title_), classes="dlg-title")
            yield Input(placeholder=t("pick.placeholder"), id="pg-q")
            yield OptionList(id="pg-list")
            yield Static(t("pick.hint"), classes="hint")

    def on_mount(self):
        self.fill("")

    def fill(self, q):
        ol = self.query_one(OptionList)
        ol.clear_options()
        rows = [g for g in self.groups if not q or Model.match(fold(g["key"] + " " + g["title"]), q)]
        for g in rows[:200]:
            ol.add_option(Option(Text.assemble(key_cell(g["key"]), "  ", g["title"], ("  (%d)" % len(g["sessions"]), "dim")), id=g["key"]))
        if q.strip():
            ol.add_option(Option(Text.assemble((t("pick.new") + " ", "bold green"), q.strip()), id="+"))
        ol.highlighted = 0 if ol.option_count else None

    def on_input_changed(self, e):
        e.stop()
        self.fill(e.value)

    def action_move(self, step):
        ol = self.query_one(OptionList)
        if ol.option_count:
            ol.highlighted = max(0, min(ol.option_count - 1, (ol.highlighted or 0) + step))

    def choose(self, oid):
        q = self.query_one(Input).value.strip()
        self.dismiss((G.new_group_key(q), q) if oid == "+" else (oid, None))

    def on_input_submitted(self, e):
        e.stop()
        ol = self.query_one(OptionList)
        if ol.highlighted is not None:
            self.choose(ol.get_option_at_index(ol.highlighted).id)

    def on_option_list_option_selected(self, e):
        self.choose(e.option.id)

    def action_cancel(self):
        self.dismiss(None)


class LinkList(ModalScreen):
    """Links of a session / group. Enter previews (.md, text) or opens a URL · e editor · c copy."""
    BINDINGS = [Binding("escape,q", "cancel", "close"), Binding("e", "act('edit')", "editor"), Binding("c", "act('copy')", "copy")]

    def __init__(self, title, items, show_src=False):
        super().__init__()
        self.title_, self.items, self.show_src = title, items, show_src

    def compose(self):
        with Vertical(classes="dialog wide"):
            yield Static(Text(self.title_), classes="dlg-title")
            ol = OptionList(id="ll-list")
            for i, lk in enumerate(self.items):
                kind = Text("%-9s" % t("kind." + lk.kind), style=KIND_STYLE.get(lk.kind) or "dim")
                label = Text(lk.label, style="" if lk.exists else "dim strike")
                src = Text(" ·" + lk.src[:8], style="dim") if self.show_src and lk.src else ""
                ol.add_option(Option(Text.assemble(kind, " ", label, src), id=str(i)))
            yield ol
            yield Static(t("links.hint"), classes="hint")

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.highlighted = 0
        ol.focus()

    def current(self):
        ol = self.query_one(OptionList)
        return self.items[ol.highlighted] if ol.highlighted is not None else None

    def on_option_list_option_selected(self, e):
        e.stop()
        self.app.open_target(self.items[int(e.option.id)])

    def action_act(self, what):
        lk = self.current()
        if lk:
            (self.app.edit_target if what == "edit" else self.app.copy_target)(lk)

    def action_cancel(self):
        self.dismiss(None)


class Preview(ModalScreen):
    """File preview: .md rendered (a backlink shows only its entry), other text in a read-only TextArea; ≤ 300 KB."""
    BINDINGS = [Binding("escape,q", "close", "close"), Binding("e", "edit", "editor"), Binding("c", "copy", "copy")]

    def __init__(self, link):
        super().__init__()
        self.link = link

    def read(self):
        try:
            with open(self.link.target, "rb") as f:
                raw = f.read(PREVIEW_MAX + 1)
        except OSError as e:
            return None, t("preview.unreadable", err=e)
        if b"\0" in raw[:4096]:
            return None, t("preview.binary")
        note = " · " + t("preview.truncated") if len(raw) > PREVIEW_MAX else ""
        return raw[:PREVIEW_MAX].decode("utf-8", "replace"), note

    def compose(self):
        lk = self.link
        text, note = self.read()
        title = rel_path(lk.target) + (" › " + lk.label.split(" › ", 1)[1] if lk.kind == "backlink" else "")
        with Vertical(classes="dialog preview"):
            yield Static(Text(title), classes="dlg-title")
            if text is None:
                yield Static(Text(note, style="yellow"))
                note = ""
            elif lk.target.endswith(".md") and (lk.line or len(text) <= MD_RENDER_MAX):
                heading = lk.label.split(" › ", 1)[1] if lk.kind == "backlink" else None
                with VerticalScroll(id="pv-md"):
                    yield Markdown(md_section(text, lk.line, heading) if lk.line else text, open_links=False)
            else:
                yield TextArea(text, read_only=True, show_line_numbers=True, id="pv-text")
            yield Static(t("preview.hint", at_line=t("preview.at_line") if lk.line else "") + note, classes="hint")

    def on_markdown_link_clicked(self, e):
        """Links in the previewed file: URL ⇒ browser; #anchor ⇒ scroll; relative path ⇒ preview that file.
        Never let Markdown handle them itself (MarkdownViewer reads 'https:/…' as a file and crashes the app)."""
        e.stop()
        href = e.href.strip()
        if is_url(href):
            self.app.open_url(href)
            self.notify(t("note.browser", url=href), markup=False)
            return
        if href.startswith("#"):
            self.query_one(Markdown).goto_anchor(href[1:])
            return
        path = os.path.normpath(os.path.join(os.path.dirname(self.link.target), href.split("#", 1)[0]))
        if os.path.isfile(path):
            self.app.push_screen(Preview(Link("code", rel_path(path), path, True, None, None)))
        else:
            self.notify(t("note.bad_link", href=href), severity="warning", markup=False)

    def action_edit(self):
        self.app.edit_target(self.link)

    def action_copy(self):
        self.app.copy_target(self.link)

    def action_close(self):
        self.dismiss(None)


class Help(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark,enter", "close", "close")]

    def compose(self):
        cfg = config.current()
        x = Table.grid(padding=(0, 2))
        x.add_column(style="bold cyan", no_wrap=True)
        x.add_column()
        rows = [("/", "help.filter"), ("← → Tab", "help.panes"), ("↑ ↓ j k PgUp PgDn", "help.move"), ("Enter", "help.enter"),
                ("click", "help.click"), ("f", "help.fork"), ("y", "help.print"), ("n", "help.new"), ("m", "help.mv"),
                ("g", "help.merge"), ("r", "help.rename"), ("o", "help.links"), ("p", "help.pin"), ("v", "help.prompts"),
                ("s", "help.source"), ("d", "help.days"), ("q", "help.quit"), ("", ""),
                ("●", "help.legend_running"), ("◐", "help.legend_open"), ("◆", "help.legend_topic"), ("✓ … ⏳ ✗ ?", "help.legend_status"),
                ("⚑", "help.legend_key"), ("click link", "help.legend_link")]
        for k, v in rows:
            x.add_row(k, t(v, mb=cfg.large_kb // 1024) if v else "")
        x.add_row("", t("help.files", groups=rel_path(cfg.groups_file), labels=rel_path(cfg.labels_file)))
        with Vertical(classes="dialog wide"):
            yield Static(t("help.title"), classes="dlg-title")
            yield Static(x)
            yield Static(t("help.close"), classes="hint")

    def action_close(self):
        self.dismiss(None)


# ---------- app ----------
class Picker(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; }
    #topbar { height: 1; padding: 0 1; background: $boost; }
    #filterbar { height: 1; padding: 0 1; margin-bottom: 1; }
    #filterbar Static { width: auto; color: $text-muted; }
    #q { width: 1fr; }
    #panes { height: 55%; }
    PaneTable { height: 1fr; border: round $panel-lighten-2; border-title-color: $text-muted; scrollbar-size-vertical: 1; }
    PaneTable:focus { border: round $accent; border-title-color: $accent; border-title-style: bold; }
    #groups { width: 9fr; }
    #sessions { width: 11fr; }
    #detail-body { link-style: none; link-style-hover: underline; }
    #detail { height: 1fr; border: round $panel-lighten-2; border-title-color: $text-muted; padding: 0 1; }
    ModalScreen { align: center middle; }
    .dialog { width: 72; height: auto; max-height: 80%; padding: 1 2; border: thick $accent; background: $surface; }
    .dialog.wide { width: 96; }
    .dlg-title { text-style: bold; margin-bottom: 1; }
    .hint { color: $text-muted; margin-top: 1; }
    #pg-list { height: auto; max-height: 20; margin-top: 1; }
    #ll-list { height: auto; max-height: 24; }
    .dialog.preview { width: 90%; height: 90%; max-height: 90%; }
    .dialog.preview #pv-md, .dialog.preview TextArea { height: 1fr; }
    """
    LETTER_ACTIONS = {"fork", "print", "new", "move", "merge", "rename", "source", "days", "help", "quit", "focus_filter",
                      "links", "pin", "toggle_prompts"}
    SESSION_ONLY = {"fork", "print", "move", "pin", "toggle_prompts"}

    def __init__(self, model, query=""):
        super().__init__()
        self.title = t("app.title")
        self.model = model
        self.initial_query = query
        self.cur_group = None
        self.cur_session = None
        self.detail_mode = "group"  # groups pane focused ⇒ group summary; sessions pane ⇒ session detail
        self.all_prompts = False
        self.link_list = []  # links drawn in the detail; @click app.open_link(i, target) points here
        for key, action, desc, show in [
            ("slash", "focus_filter", "key.filter", True), ("f", "fork", "key.fork", True), ("y", "print", "key.print", True),
            ("n", "new", "key.new", True), ("m", "move", "key.move", True), ("g", "merge", "key.merge", True),
            ("r", "rename", "key.rename", True), ("o", "links", "key.links", True), ("p", "pin", "key.pin", True),
            ("v", "toggle_prompts", "key.prompts", True), ("s", "source", "key.source", False),  # shown on the top bar badge
            ("d", "days", "key.days", False), ("question_mark", "help", "key.help", True), ("q", "quit", "key.quit", True),
        ]:
            self.bind(key, action, description=t(desc), show=show)
        self.bind("escape", "escape", show=False)
        self.bind("down", "leave_filter", show=False)

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="filterbar"):
            yield Static("🔍 ")
            yield Input(placeholder=t("filter.placeholder"), id="q", compact=True)
        with Horizontal(id="panes"):
            yield PaneTable(id="groups", cursor_type="row", zebra_stripes=True, show_row_labels=False)
            yield PaneTable(id="sessions", cursor_type="row", zebra_stripes=True, show_row_labels=False)
        with VerticalScroll(id="detail", can_focus=False):
            yield Static(id="detail-body")
        yield Footer(compact=True)

    def on_mount(self):
        self.query_one("#detail").border_title = t("detail.title")
        self.model.query = self.initial_query
        self.query_one("#q", Input).value = self.initial_query
        self.query_one("#groups").focus()
        self.refresh_all()  # fill now so early key presses never hit an empty table
        self.call_after_refresh(self.refresh_all)  # and again once the real width is known (flexible name column)
        self.set_interval(RELOAD_SEC, self.tick)
        if self.model.should_ask_llm():
            self.push_screen(AskLabeler(self.model.queue), self.on_llm_answer)
        else:
            self.model.maybe_spawn()

    def on_llm_answer(self, yes):
        self.model.answer_llm(bool(yes))
        self.model.maybe_spawn()
        self.render_topbar()

    # --- data ---
    loading = False

    def tick(self):
        if self.loading:  # previous scan still running (threads can't be cancelled) ⇒ don't stack another
            return
        self.loading = True
        self.run_worker(self.load_bg, thread=True, group="reload")

    def load_bg(self):
        try:
            st = self.model.load()
        except Exception as e:  # noqa: BLE001 — keep old data, report on the top bar
            self.call_from_thread(self.on_load_error, str(e))
            return
        self.call_from_thread(self.on_loaded, st)

    def on_loaded(self, st):
        self.loading = False
        if self.model.apply(st):
            self.model.maybe_spawn()
            self.refresh_all()

    def on_load_error(self, msg):
        self.loading = False
        self.model.error = t("scan_error", err=msg)
        self.render_topbar()

    # --- drawing ---
    def refresh_all(self):
        self.render_topbar()
        self.fill_groups()

    def render_topbar(self):
        m = self.model
        n_lab = sum(1 for d in m.sessions if d["id"] in m.labels)
        if m.no_llm:
            lab = t("top.no_llm")
        elif m.llm_state != L.ON:
            lab = t("top.llm_" + m.llm_state, cmd=config.current().labeler_command)
        else:
            lab = t("top.labels", n=n_lab, total=len(m.sessions)) + (
                " · " + t("top.labeling") if m.running else (" · " + t("top.queue", n=m.queue) if m.queue else ""))
        days = DAYS[m.days]
        x = Text.assemble((t("app.title"), "bold"),
                          "  " + t("top.counts", sessions=len(m.sessions), groups=len(m.groups)) + " · " + lab + "   ",
                          ("s", "bold"), (" %s " % t("source." + SOURCES[m.source]), "reverse"), " ",
                          ("d", "bold"), (" %s " % (t("top.days", n=days) if days else t("top.all_days")), "reverse"))
        for err in (m.ov_error, m.error):
            if err:
                x.append("   ⚠ " + err, style="bold red")
        self.query_one("#topbar", Static).update(x)

    def fill_groups(self):
        m = self.model
        tb = self.query_one("#groups", PaneTable)
        gs = m.visible_groups()
        keys = [g["key"] for g in gs]
        if self.cur_group not in keys:
            self.cur_group = keys[0] if keys else None
        with self.prevent(DataTable.RowHighlighted):
            self._fill_groups(tb, gs, keys)
        self.fill_sessions()

    def _fill_groups(self, tb, gs, keys):
        m = self.model
        name_w = tb.setup(group_cols())
        for g in gs:
            tb.add_row(dot(m.group_live(g["vis"])), key_cell(g["key"]), status_cell(m.work_status(g["key"])),
                       cut(g["title"], name_w, "bold" if g["hit"] else ""),
                       Text(str(len(g["vis"])), justify="right", style="dim"), Text(rel_time(g["last"], m.now), style="dim"),
                       key=g["key"])
        tb.border_title = t("pane.groups", n=len(gs))
        if self.cur_group:
            tb.move_cursor(row=keys.index(self.cur_group), scroll=True)

    def fill_sessions(self):
        m = self.model
        tb = self.query_one("#sessions", PaneTable)
        sids = m.visible_sessions(self.cur_group) if self.cur_group else []
        if self.cur_session not in sids:
            self.cur_session = sids[0] if sids else None
        with self.prevent(DataTable.RowHighlighted):
            self._fill_sessions(tb, sids)
        self.render_detail()

    def _fill_sessions(self, tb, sids):
        m = self.model
        large_kb = config.current().large_kb
        name_w = tb.setup(session_cols())
        key_sid = (m.key_of.get(self.cur_group) or (None,))[0]
        for sid in sids:
            d = m.by_id[sid]
            lb = m.label(sid)
            name = ("★ " if d.get("title_custom") else "") + (lb.get("summary") or title_of(d))
            name = cut(name, name_w - 2 if sid == key_sid else name_w)
            if sid == key_sid:
                name = Text.assemble(("⚑ ", "bold yellow"), name)
            tb.add_row(dot(m.live_state(sid)), Text(rel_time(d["mtime"], m.now), style="dim"), status_cell(lb),
                       role_cell(lb.get("role")), name,
                       Text("C", style="bold") if d["tool"] == "claude" else Text("X", style="bold cyan"),
                       Text(fmt_size(d["size_kb"]), justify="right", style="bold yellow" if d["size_kb"] > large_kb else "dim"),
                       key=sid)
        g = m.groups.get(self.cur_group)
        tb.border_title = escape(t("pane.sessions_of", title=g["title"], n=len(sids)) if g else t("pane.sessions"))
        if self.cur_session:
            tb.move_cursor(row=sids.index(self.cur_session), scroll=True)

    def render_detail(self):
        if self.detail_mode == "group" and self.model.groups.get(self.cur_group):
            body, title, ident = self.group_detail()
        else:
            body, title, ident = self.session_detail()
        self.query_one("#detail-body", Static).update(body)
        box = self.query_one("#detail")
        box.border_title = title
        if getattr(self, "detail_ident", None) != ident:  # a 5 s rescan must not scroll the reader back to the top
            self.detail_ident = ident
            box.scroll_home(animate=False)

    def link_rows(self, row, items, src=False):
        """One row per link kind, links joined by ' · ', clickable; other files beyond CODE_ROW_MAX point to `o`."""
        by_kind = {}
        for lk in items:
            by_kind.setdefault(lk.kind, []).append(lk)
        for kind, lks in by_kind.items():
            shown = lks[:CODE_ROW_MAX] if kind == "code" else lks
            x = Text()
            for lk in shown:
                if x:
                    x.append("\n" if kind in ("backlink", "plan", "artifact") or src else " · ", style="dim")
                self.link_list.append(lk)
                x.append_text(link_text(lk, len(self.link_list) - 1, src))
            if len(lks) > len(shown):
                x.append("  " + t("detail.more", n=len(lks) - len(shown)), style="dim")
            row(t("kind." + kind) + (" (%d)" % len(lks) if len(lks) > 1 else ""), x)

    def grid(self):
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan", no_wrap=True)  # as wide as the longest label
        grid.add_column(ratio=1)

        def row(label, value, style=""):
            if value:
                grid.add_row(label, value if isinstance(value, Text) else Text(value, style=style))
        return grid, row

    def session_detail(self):
        m = self.model
        self.link_list = []
        d = m.by_id.get(self.cur_session)
        if not d:
            return Text(t("detail.none"), style="dim"), t("detail.title"), None
        lb = m.label(self.cur_session)
        head = Text(("★ " if d.get("title_custom") else "") + title_of(d), style="bold")
        ks, pinned = m.key_of.get(self.cur_group) or (None, False)
        if ks == d["id"]:
            head.append("   ⚑ " + t("detail.key_session") + (" 📌" if pinned else ""), style="yellow")
        meta = Text("%s · %s · %s · %s → %s · %s%s" % (
            d["id"], d["tool"], t("detail.n_prompts", n=d["n_prompts"]), fmt_dt(d.get("first_ts")), fmt_dt(d.get("last_ts")),
            rel_path(d.get("cwd")) or ".",
            " · " + d["branch"] if d.get("branch") and d["branch"] != "HEAD" else ""), style="dim")
        grid, row = self.grid()
        row(t("detail.summary"), lb.get("summary"))
        row(t("detail.status"), status_text(lb))
        lks = m.links(self.cur_session)
        self.link_rows(row, [lk for lk in lks if lk.kind not in ("memory", "code")])
        ps = d["prompts"]
        if self.all_prompts and ps:
            x = Text()
            for i, p in enumerate(ps, 1):
                x.append("%s%2d. " % ("\n" if i > 1 else "", i), style="dim")
                x.append(fmt_dt(p.get("ts")) + "  ", style="dim")
                x.append(p["text"])
            row(t("detail.prompts", n=len(ps)), x)
        elif ps:
            row(t("detail.first"), ps[0]["text"])
            if len(ps) > 1:
                row(t("detail.latest"), ps[-1]["text"] + ("   " + t("detail.see_all", n=len(ps)) if len(ps) > 2 else ""))
        concl = [re.sub(r"\*\*|`", "", ln).strip() for ln in (d.get("last_assistant") or "").strip().splitlines() if ln.strip()]
        row(t("detail.conclusion"), "\n".join(concl[:6] + (["…"] if len(concl) > 6 else [])), "dim")
        self.link_rows(row, [lk for lk in lks if lk.kind in ("memory", "code")])
        row(t("detail.related"), m.related_text(self.cur_session), "yellow")
        cmd = Text("$ cd %s && %s" % (shlex.quote(session_cwd(d)), shlex.join(resume_cmd(d))), style="dim")
        return stack(head, meta, Text(""), grid, Text(""), cmd), t("detail.session_title", id=d["id"][:8]), ("s", d["id"])

    def group_detail(self):
        m = self.model
        self.link_list = []
        g = m.groups[self.cur_group]
        sids = m.visible_sessions(self.cur_group) or g["sessions"]
        ds = [m.by_id[s] for s in sids]
        head = Text.assemble(key_cell(g["key"]), "  ", (g["title"], "bold"))
        meta = Text("%s · %s → %s%s" % (
            t("detail.n_sessions", n=len(ds)), fmt_date(min(d.get("first_ts") or d.get("last_ts") or "" for d in ds) or None),
            fmt_date(max(d.get("last_ts") or "" for d in ds) or None),
            " · " + t("detail.tickets", keys=", ".join(g["tickets"])) if g["tickets"] else ""), style="dim")
        grid, row = self.grid()

        def sess_line(d, x):
            """One session per line; click ⇒ jump to that session in the right pane."""
            lb = m.label(d["id"])
            icon, color = STATUS_ICON.get(lb.get("status"), ("?", "dim")) if "status" in lb else (" ", "")
            click = Style(meta={"@click": "app.goto_session(%r)" % d["id"]})
            x.append(fmt_date(d.get("last_ts")) + "  ", style=Style(dim=True) + click)
            x.append("%-2s" % icon, style=Style.parse(color or "default") + click)
            x.append("%-9s " % role_label(lb.get("role")), style=Style.parse(ROLE_COLOR.get(lb.get("role") or "", "dim")) + click)
            x.append(lb.get("summary") or title_of(d), style=click)
            return x

        ks, pinned = m.key_of.get(self.cur_group) or (None, False)
        if ks in m.by_id:
            row(t("detail.key"), sess_line(m.by_id[ks], Text("⚑ " + ("📌 " if pinned else ""), style="yellow")))
        newest = m.newest(sids)  # same source as the icon in the groups pane: never fall back to an older session's label
        live = m.live_state(newest)
        row(t("detail.now"), status_text(m.work_status(self.cur_group)) or Text(
            t("detail.running_unjudged") if live == "busy" else t("detail.open_unjudged") if live == "open"
            else t("detail.unjudged"), style="dim"))
        tl = Text()
        for d in sorted(ds, key=lambda d: d["mtime"]):
            if tl:
                tl.append("\n")
            sess_line(d, tl)
        row(t("detail.history"), tl)
        self.link_rows(row, m.group_links(self.cur_group), src=True)
        hint = Text(t("detail.group_hint"), style="dim")
        return stack(head, meta, Text(""), grid, Text(""), hint), t("detail.group_title", key=escape(g["key"])), ("g", g["key"])

    # --- events ---
    def on_data_table_row_highlighted(self, e):
        if e.row_key is None or e.row_key.value is None:
            return
        if e.data_table.id == "groups":
            if e.row_key.value != self.cur_group and e.row_key.value in self.model.groups:
                self.cur_group = e.row_key.value
                self.cur_session = None
                self.fill_sessions()
        elif e.row_key.value != self.cur_session and e.row_key.value in self.model.by_id:
            self.cur_session = e.row_key.value
            self.render_detail()

    def on_data_table_row_selected(self, e):
        key = e.row_key.value if e.row_key is not None else None
        if e.data_table.id == "groups":
            self.query_one("#sessions").focus()
        elif key in self.model.by_id:
            self.cur_session = key  # open exactly the selected row, whatever state was rendered last
            self.action_open()

    def on_input_changed(self, e):
        if e.input.id == "q":
            self.model.query = e.value
            if e.value:  # typing ⇒ jump to the first matching group; clearing ⇒ stay on the group just found
                self.cur_group = None
            self.fill_groups()

    def on_input_submitted(self, e):
        if e.input.id == "q":
            self.query_one("#groups").focus()

    def on_resize(self, e):
        self.call_after_refresh(self.fill_groups)

    def check_action(self, action, parameters):
        focused = self.focused
        if isinstance(focused, Input) and action in self.LETTER_ACTIONS:
            return False  # typing in the filter box ⇒ letters are letters
        if action == "leave_filter":
            return isinstance(focused, Input)
        if action in self.SESSION_ONLY and getattr(focused, "id", None) != "sessions":
            return None  # dimmed in the footer outside the sessions pane
        return True

    def on_descendant_focus(self, e):
        self.refresh_bindings()
        mode = {"groups": "group", "sessions": "session"}.get(getattr(e.widget, "id", None))
        if mode and mode != self.detail_mode:
            self.detail_mode = mode
            self.render_detail()

    # --- actions ---
    def action_focus_pane(self, which):
        self.query_one("#" + which).focus()

    def action_switch_pane(self):
        self.query_one("#sessions" if getattr(self.focused, "id", None) == "groups" else "#groups").focus()

    def action_focus_filter(self):
        self.query_one("#q").focus()

    def action_leave_filter(self):
        self.query_one("#groups").focus()

    def action_escape(self):
        q = self.query_one("#q", Input)
        if q.value:
            q.value = ""
        if self.focused is q:
            self.query_one("#groups").focus()

    def action_source(self):
        self.model.source = (self.model.source + 1) % len(SOURCES)
        self.refresh_all()

    def action_days(self):
        self.model.days = (self.model.days + 1) % len(DAYS)
        self.refresh_all()

    def action_help(self):
        self.push_screen(Help())

    def cur(self):
        return self.model.by_id.get(self.cur_session)

    def action_open(self):
        d = self.cur()
        if not d:
            return
        if d["size_kb"] <= config.current().large_kb:
            self.exit(("exec", resume_cmd(d), session_cwd(d)))
            return

        def done(v):
            if v:
                self.exit(("exec", resume_cmd(d, v == "fork"), session_cwd(d)))
        self.push_screen(Confirm(Text.assemble(t("confirm.session") + " ", (fmt_size(d["size_kb"]), "bold yellow"),
                                               " — " + t("confirm.large"))), done)

    def action_fork(self):
        d = self.cur()
        if d:
            self.exit(("exec", resume_cmd(d, True), session_cwd(d)))

    def action_print(self):
        d = self.cur()
        if d:
            self.exit(("print", resume_cmd(d, d["size_kb"] > config.current().large_kb), session_cwd(d)))

    def action_new(self):
        g = self.model.groups.get(self.cur_group)
        if g and g["key"] == G.UNGROUPED:
            self.notify(t("note.new_ungrouped"), severity="warning")
        elif g:
            key = self.cur_group
            members = self.model.members(key)
            key_sid = (self.model.key_of.get(key) or (None,))[0]

            def go(agent):
                if agent:
                    self.exit(("exec", handoff_cmd(g, members, self.model.labels, key_sid, agent), config.current().root))
            agents = [a for a in ("claude", "codex") if shutil.which(a)]
            if len(agents) == 2:
                self.push_screen(PickAgent(t("handoff.title", title=g["title"])), go)
            else:
                go(agents[0] if agents else "claude")

    def save(self, fn, ok_msg):
        err = self.model.apply_ov(fn)
        if err:
            self.notify(t("note.ov_failed", err=err), severity="error", timeout=8, markup=False)
        else:
            self.notify(ok_msg, markup=False)
        self.refresh_all()

    def other_groups(self, exclude, allow_ungrouped=False):
        skip = {exclude} | (set() if allow_ungrouped else {G.UNGROUPED})
        return sorted((g for k, g in self.model.groups.items() if k not in skip), key=lambda g: g["last"], reverse=True)

    def action_move(self):
        sid, gkey = self.cur_session, self.cur_group
        base = self.model.base.get(sid)
        if not base:
            return

        def done(r):
            if r:
                dst, title = r
                self.cur_group = dst
                self.save(lambda ov: G.ov_move(ov, sid, dst, base, title), t("note.moved", id=sid[:8], dst=title or dst))
        # the natural group must stay selectable to undo a move — even when it is empty (this was its only session)
        groups = self.other_groups(gkey, allow_ungrouped=base == G.UNGROUPED)
        if base != gkey and base not in self.model.groups:
            title = t("ungrouped") if base == G.UNGROUPED else base
            groups.append({"key": base, "title": title + " " + t("pick.natural"), "sessions": [], "last": 0})
        self.push_screen(PickGroup(t("pick.move_title", id=sid[:8]), groups), done)

    def action_merge(self):
        gkey = self.cur_group
        if not gkey:
            return
        if gkey == G.UNGROUPED:
            self.notify(t("note.merge_ungrouped"), severity="warning")
            return
        g = self.model.groups.get(gkey)
        if not g:
            return

        def done(r):
            if r:
                dst, title = r

                def merge(ov):
                    G.ov_merge(ov, gkey, dst)
                    if title:
                        G.ov_rename(ov, dst, title)
                self.cur_group = dst
                self.save(merge, t("note.merged", src=g["title"], dst=title or dst))
        self.push_screen(PickGroup(t("pick.merge_title", title=g["title"]), self.other_groups(gkey)), done)

    def action_rename(self):
        gkey = self.cur_group
        g = self.model.groups.get(gkey)
        if not g:
            return

        def done(x):
            if x:
                self.save(lambda ov: G.ov_rename(ov, gkey, x), t("note.renamed", title=x))
        self.push_screen(AskText(t("ask.rename", key=gkey), g["title"]), done)

    # --- links ---
    def action_open_link(self, i, target):
        """Index + target: a 5 s rescan rebuilds link_list, so the index may be stale ⇒ check the target first."""
        lk = self.link_list[i] if 0 <= i < len(self.link_list) else None
        if not lk or lk.target != target:
            lk = next((x for x in self.link_list if x.target == target), None)
        if lk:
            self.open_target(lk)

    def open_target(self, lk):
        if not lk.exists:
            self.notify(t("note.gone", path=lk.target), severity="warning", markup=False)
        elif is_url(lk.target):
            self.open_url(lk.target)
            self.notify(t("note.browser", url=lk.target), markup=False)
        else:
            self.push_screen(Preview(lk))

    def edit_target(self, lk):
        if is_url(lk.target):
            self.open_target(lk)
            return
        if not lk.exists:
            self.notify(t("note.gone", path=lk.target), severity="warning", markup=False)
            return
        argv = [a.format(path=lk.target, line=lk.line or 1) for a in shlex.split(config.current().editor)]
        if not argv or not shutil.which(argv[0]):
            self.notify(t("note.no_editor", cmd=argv[0] if argv else "open.editor"), severity="error")
            return
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)  # a list, never a shell: paths cannot inject anything
        self.notify(t("note.editor", path=rel_path(lk.target)), markup=False)

    def copy_target(self, lk):
        pb = shutil.which("pbcopy")
        ok = False
        if pb and not os.environ.get("SSH_CONNECTION"):  # over SSH pbcopy copies on the remote ⇒ OSC 52 instead
            try:
                ok = subprocess.run([pb], input=lk.target, text=True, timeout=2).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
        if not ok:
            self.copy_to_clipboard(lk.target)
        self.notify(t("note.copied", text=lk.target), markup=False)

    def action_links(self):
        m = self.model
        if self.detail_mode == "group" and self.cur_group in m.groups:
            items, title, src = m.group_links(self.cur_group), t("links.group", title=m.groups[self.cur_group]["title"]), True
        elif self.cur_session in m.by_id:
            items, title, src = m.links(self.cur_session), t("links.session", id=self.cur_session[:8]), False
        else:
            return
        if not items:
            self.notify(t("note.no_links"), severity="warning")
            return
        self.push_screen(LinkList(title, items, show_src=src))

    def action_goto_session(self, sid):
        if sid not in self.model.visible_sessions(self.cur_group):
            self.notify(t("note.hidden", id=sid[:8]), severity="warning")
        else:
            self.cur_session = sid
            self.query_one("#sessions").focus()
            self.fill_sessions()

    def action_pin(self):
        sid, gkey = self.cur_session, self.cur_group
        if not sid or not gkey:
            return
        if gkey == G.UNGROUPED:
            self.notify(t("note.pin_ungrouped"), severity="warning")
            return
        unpin = self.model.key_of.get(gkey) == (sid, True)
        self.save(lambda ov: G.ov_pin(ov, gkey, sid), t("note.unpinned" if unpin else "note.pinned", id=sid[:8]))

    def action_toggle_prompts(self):
        self.all_prompts = not self.all_prompts
        self.render_detail()

    def action_quit(self):
        self.exit(("quit",))
