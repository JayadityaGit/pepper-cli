"""Rendering helpers for the CLI.

Rich handles markdown → terminal output. Two consoles: ``stdout`` for
the reply itself (so ``pepper chat ... | less`` and ``pepper chat ... >
file`` work), ``stderr`` for status / error chrome (so piping the
reply doesn't capture the "Logged in as ..." messages).

``NO_COLOR`` and non-tty downgrades are handled by Rich's
auto-detection — passing nothing extra here means a CI log file gets
plain ASCII without us doing anything.

The ``render_thought`` / ``render_tool_call`` / ``render_tool_result``
helpers are used by the REPL to surface the model's interim work as
each SSE frame arrives. They print to stderr so a hypothetical
future ``pepper chat --stream MESSAGE | jq`` (out of scope today but
plausible) wouldn't capture them in the piped output.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

if TYPE_CHECKING:
    from pepper_cli.api_client import (
        ArtifactListResponse,
        GroupList,
        GroupMessagePage,
        GroupRosterMember,
        GroupTopicSummary,
        ScheduledTaskDetail,
        ScheduledTaskMessage,
        ScheduledTaskRunEntry,
        ScheduledTaskSummary,
        TodoEntry,
        TrajectoryDetail,
        TrajectoryListEntry,
    )

# Lazy-ish: build the consoles once per process. ``no_color`` is left
# to Rich's auto-detection so ``NO_COLOR=1`` and piped output both
# produce plain text without explicit config here.
_stdout = Console()
_stderr = Console(stderr=True)

#: Cap on how much of a tool_result ``text`` we render inline. The
#: server's ``_sanitize_step_value`` already truncates to 2KB, but the
#: REPL transcript looks gnarly if we slap 2KB into the terminal for
#: every tool result. Keep the inline preview short; the user can pull
#: the full payload from the trajectory viewer if they need it.
_TOOL_RESULT_PREVIEW_CHARS = 200


def render_markdown(text: str) -> None:
    """Print markdown text to stdout: rendered when stdout is a TTY, raw and
    byte-exact otherwise.

    Shared by :func:`render_reply` and ``pepper notes show``. The non-tty
    branch writes ``text`` with nothing added or trimmed — ``notes show``'s
    job is to put a note's stored body on stdout byte-for-byte, so
    ``show > file.md`` followed by a later ``edit --file file.md`` round-trips
    cleanly instead of a no-op edit silently changing the stored body (and
    its etag) by one trailing newline.
    """
    if not sys.stdout.isatty():
        sys.stdout.write(text)
        return
    _stdout.print(Markdown(text))


def render_reply(markdown_text: str) -> None:
    """Print a chat reply to stdout, rendered as markdown.

    Non-tty stdout (piped, redirected) skips the markdown
    formatting and prints raw text — chat replies usually contain
    plain text the user wants to grep / pipe, not bold and headings.
    Ensures the output ends with a newline: a reply is a line of output,
    unlike a note body, which :func:`render_markdown` prints exactly as
    stored.
    """
    render_markdown(markdown_text)
    if not sys.stdout.isatty() and not markdown_text.endswith("\n"):
        sys.stdout.write("\n")


def info(message: str) -> None:
    """Print a status line to stderr. Used for the chrome around
    commands ('Logged in as ...', 'Removed credentials at ...') so the
    primary output channel stays clean for piping."""
    _stderr.print(message)


def error(message: str, *, soft_wrap: bool = False) -> None:
    """Print an error line to stderr in red. The CLI returns nonzero
    *and* prints this; the two signals together let scripts both
    detect failure (exit code) and surface the cause (message).

    ``soft_wrap=True`` is for a message that carries an id/uuid a person
    would want to copy-paste (an ambiguous-name match listing, say) —
    Rich would otherwise fold it across lines at the terminal width,
    which breaks a paste of exactly the thing being offered.
    """
    _stderr.print(f"[red]error:[/red] {message}", soft_wrap=soft_wrap)


def render_thought_delta(text: str, *, first: bool) -> None:
    """Append one ``model_thought`` fragment to the thought being rendered.

    A fragment is whatever the backend chunks at — a paragraph on Gemini, a
    token on OpenAI and Anthropic — so a line per fragment gives a column of
    single words on the token-level backends. Fragments are written
    without a newline instead, and :func:`end_thought` closes the line when the
    terminal ``model_thought_block`` says the thought ended. Nothing is
    buffered, so the text still appears at the backend's own pace.

    ``first`` marks the fragment that opens a thought and gets the ``·``
    bullet. Dimmed because thoughts are background context — the user's eye
    should land on the eventual reply, not the running commentary.

    ``markup=False`` matters here: a fragment is model text, and a stray
    ``[dim]``-shaped token in it must not be read as rich markup.
    """
    if not text:
        return
    if first:
        _stderr.print("· ", style="dim", end="", markup=False, highlight=False, soft_wrap=True)
    _stderr.print(text, style="dim", end="", markup=False, highlight=False, soft_wrap=True)


def end_thought(text: str, *, streamed: str) -> None:
    """Close the thought a ``model_thought_block`` frame terminates.

    ``streamed`` is everything the fragments put on screen for this thought,
    which is usually exactly ``text`` — then this only ends the line, because
    the thought is already rendered and reprinting it would double it.

    The two cases where it is not:

    - **Nothing streamed.** A backend that only emits whole thoughts, or a
      dropped fragment. Print the block as its own line.
    - **What streamed is not this thought.** A transient provider failure
      mid-stream abandons its attempt *without* a terminating block (see
      the server's generation loop); the server now sends
      a ``retry_attempt`` frame for that (:func:`discard_thought`).
      Otherwise the block is authoritative — the same rule the web composer
      applies by rewriting the row — so print it on its own line rather than
      leaving stale text as the record. A terminal cannot un-write what it
      already showed; what it can do is end with the correct paragraph.
    """
    if not streamed:
        if text and text.strip():
            render_thought_delta(text.strip(), first=True)
            _stderr.print("")
        return
    _stderr.print("")
    if text and text.strip() != streamed.strip():
        render_thought_delta(text.strip(), first=True)
        _stderr.print("")


def discard_thought() -> None:
    """End the line a ``retry_attempt`` frame abandons.

    A terminal cannot un-write the abandoned fragments, so end their line and
    let the retry's fragments start a fresh ``·`` thought instead of fusing
    onto them.
    """
    _stderr.print("")


def render_tool_call(payload: dict[str, Any]) -> None:
    """Render one ``tool_call`` SSE frame.

    Compact one-liner: ``→ name(arg=value, ...)``. Args dict is
    formatted with ``repr``-style truncation so a giant arg blob
    (e.g. a pasted email body) doesn't blow up the terminal."""
    name = payload.get("name") or "<unknown>"
    args = payload.get("args") or {}
    if isinstance(args, dict) and args:
        rendered_args = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        _stderr.print(f"[cyan]→[/cyan] [bold]{name}[/bold]({rendered_args})")
    else:
        _stderr.print(f"[cyan]→[/cyan] [bold]{name}[/bold]()")


def render_tool_result(payload: dict[str, Any]) -> None:
    """Render one ``tool_result`` SSE frame.

    Status-coloured glyph + name + truncated result text. ``ok`` gets
    a green check; non-ok gets a yellow warning so a glance at the
    transcript surfaces the failures."""
    name = payload.get("name") or "<unknown>"
    status = payload.get("status") or "ok"
    text = payload.get("text") or ""
    glyph = "[green]✓[/green]" if status == "ok" else "[yellow]⚠[/yellow]"
    if text:
        _stderr.print(f"{glyph} [bold]{name}[/bold] → {_short(text)}")
    else:
        _stderr.print(f"{glyph} [bold]{name}[/bold] ({status})")


#: Per-event payload preview budget for ``pepper trajectory show``.
#: Part of the rendered shape below, so changing it changes what
#: parsers of that output see.
_TRAJECTORY_EVENT_PREVIEW_CHARS = 300


def render_trajectory(detail: TrajectoryDetail) -> None:
    """Pretty-print a trajectory + its events to stdout.

    The output shape is kept stable, because agents and scripts
    parse it:

        trajectory <id> — user <user_id> — channel <channel>
        conversation <key>                (when the turn carries one)
        created <ts>  closed <ts|"(open)">
        code <12-char-sha>[ (dirty)]   (when meta carries it)
        N events:
        [seq] event_type: <payload truncated>
        ...

    Stdout, not stderr — so ``pepper trajectory show <id> > out.txt``
    captures the rendered view. The ``--json`` flag bypasses this
    function entirely.
    """
    traj = detail.trajectory
    closed = traj.closed_at or "(open)"
    _stdout.print(
        f"trajectory [bold]{traj.id}[/bold] — user {traj.user_id} — channel {traj.channel}"
    )
    # Its own line rather than appended to the header: a dated 1:1 key is
    # ~55 chars, and the point of printing it is that two trajectories'
    # keys can be compared at a glance.
    if traj.conversation_key:
        _stdout.print(f"conversation {traj.conversation_key}")
    _stdout.print(f"created {traj.created_at}  closed {closed}")
    code_sha = traj.meta.get("code_sha") if isinstance(traj.meta, dict) else None
    if isinstance(code_sha, str) and code_sha:
        dirty = " (dirty)" if traj.meta.get("code_dirty") else ""
        _stdout.print(f"code {code_sha[:12]}{dirty}")
    _stdout.print(f"{len(detail.events)} events:")
    for ev in detail.events:
        payload_str = _short_json(ev.payload, _TRAJECTORY_EVENT_PREVIEW_CHARS)
        _stdout.print(f"[{ev.seq}] {ev.event_type}: {payload_str}")


def render_trajectory_list(rows: list[TrajectoryListEntry]) -> None:
    """Pretty-print a trajectory-list response to stdout.

    Newest-first. Each row: id, channel, created_at, event count,
    optional group tag, and a snippet of the first user message.
    Plain text — no Rich tables — so the output stays grep-able.
    """
    if not rows:
        _stdout.print("(no trajectories)")
        return
    for row in rows:
        tag = f" [group:{row.group_name}]" if row.group_name else ""
        preview = ""
        if row.first_user_message:
            snippet = row.first_user_message.strip().replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:80] + "…"
            preview = f" — {snippet}"
        _stdout.print(
            f"{row.created_at}  [bold]{row.id}[/bold]  "
            f"{row.channel}{tag}  ({row.event_count} events){preview}"
        )


#: Status → leading glyph for the todo list. Open is an empty box; the
#: two closed states get distinct marks so a ``--status all`` listing
#: reads at a glance.
_TODO_MARKERS = {"open": "☐", "done": "✓", "dismissed": "✗"}


def render_todo_list(rows: list[TodoEntry]) -> None:
    """Pretty-print a todo list to stdout.

    One line per todo: status glyph, title, optional localized due date
    and priority, then a dim id (the handle for ``todos done <id>``).
    Plain text — no Rich tables — so the output stays grep-able, matching
    ``render_trajectory_list``. Due dates are localized to the machine's
    zone (the CLI runs on the user's own machine).
    """
    if not rows:
        _stdout.print("(no todos)")
        return
    for row in rows:
        marker = _TODO_MARKERS.get(row.status, "•")
        line = f"{marker} {row.title}"
        if row.due_date:
            line += f"  due {_format_due(row.due_date, row.due_is_date_only)}"
        if row.priority:
            line += f"  ({row.priority})"
        line += f"  [dim]{row.id}[/dim]"
        _stdout.print(line)


_IMPUTED = {"followed": "followed", "email": "suggested - from email"}


def _calendar_event_line(ev: dict[str, Any]) -> str:
    """One agenda line from an event's server-preformatted fields."""
    if ev.get("all_day"):
        when = "all day"
    else:
        when = f"{ev.get('start_label') or ''}-{ev.get('end_label') or ''}".strip("-")
    title = escape(ev.get("summary") or "(no title)")
    line = f"{'>' if ev.get('is_next') else ' '} {escape(when):<16} {title}"
    if ev.get("location"):
        line += f"  @ {escape(ev['location'])}"
    conf = ev.get("conference")
    if isinstance(conf, dict) and conf.get("url"):
        line += f"  {escape('[' + (conf.get('name') or 'call') + ']')}"
    if ev.get("my_response") in ("declined", "tentative"):
        line += f"  ({ev['my_response']})"
    if ev.get("kind") in _IMPUTED:
        line += f"  [dim]({_IMPUTED[ev['kind']]})[/dim]"
    return f"[bold]{line}[/bold]" if ev.get("is_next") else line


def _sort_key(all_day: bool, iso: str | None) -> tuple[bool, datetime]:
    return (not all_day, datetime.fromisoformat(iso) if iso else datetime.min.replace(tzinfo=UTC))


def _day_rows(day: dict[str, Any], mode: str) -> list[tuple[tuple[bool, datetime], str]]:
    """A day's lines for one view, as (sort key, markup) pairs.

    ``calendar`` is what is on the user's calendars; ``schedule`` adds timed
    todo deadlines and planned meals; ``planning`` adds what Pepper imputes
    (followed events, email suggestions, meal windows), marked as such.
    """
    rows = []
    for ev in day.get("events") or []:
        if ev.get("kind") in _IMPUTED and mode != "planning":
            continue
        rows.append((_sort_key(bool(ev.get("all_day")), ev.get("start")), _calendar_event_line(ev)))
    if mode == "schedule":
        for todo in day.get("deadlines") or []:
            due = escape(f"due {todo.get('time') or ''}")
            line = f"  {due:<16} {escape(todo.get('title') or '')}"
            rows.append((_sort_key(False, todo.get("sort_key")), line))
        for meal in day.get("meals") or []:
            if meal.get("name"):
                slot = escape(str(meal.get("slot") or "meal"))
                rows.append((_sort_key(False, None), f"  {slot:<16} {escape(meal['name'])}"))
    if mode == "planning":
        for win in day.get("meal_windows") or []:
            when = f"{win.get('start_label') or ''}-{win.get('end_label') or ''}"
            dish = f" - {escape(win['dish'])}" if win.get("dish") else ""
            name = escape(win.get("label") or "Meal")
            line = f"  {escape(when):<16} {name} [dim](meal window){dish}[/dim]"
            rows.append((_sort_key(False, win.get("start")), line))
    for leg in day.get("travel") or []:
        facts = leg.get("facts") or {}
        if facts.get("ok"):
            slack = facts.get("spare_minutes")
            tight = f", {-slack} min late" if isinstance(slack, int) and slack < 0 else ""
            drive = escape(f"drive {facts['minutes']} min")
            line = (
                f"  [dim]{drive:<16} to {escape(leg.get('to') or '')}, "
                f"arrive {escape(facts.get('arrive_at') or '')}{tight}[/dim]"
            )
            rows.append((_sort_key(False, leg.get("after")), line))
    return sorted(rows, key=lambda r: r[0])


def _proposed_rows(plan: dict[str, Any]) -> list[tuple[tuple[bool, datetime], str]]:
    """Pepper's proposed todo blocks from the active schedule plan.

    Blocks already held on the calendar are real events in the range payload, so
    only the unheld ones are added here.
    """
    chosen = next(
        (p for p in plan.get("plans") or [] if plan.get("active") in p["philosophies"]), None
    )
    rows = []
    for b in (chosen or {}).get("blocks") or []:
        if b.get("type") != "todo" or b.get("done") or b.get("on_calendar"):
            continue
        when = escape(f"{b['start_label']}-{b['end_label']}")
        why = f" - {escape(b['reason'])}" if b.get("reason") else ""
        line = f"  {when:<16} {escape(b['title'])}  [dim](proposed todo block{why})[/dim]"
        rows.append((_sort_key(False, b["start"]), line))
    return rows


def _conditions_line(env: dict[str, Any], date: str) -> str | None:
    """One dim line of weather and daylight for ``date`` from the environment payload."""
    info = (env.get("days") or {}).get(date) or {}
    wx, solar = info.get("weather") or {}, info.get("solar") or {}
    parts = []
    if wx:
        parts.append(f"{wx.get('label')}, {wx.get('low')}-{wx.get('high')}F")
        if wx.get("precip"):
            parts.append(f"{wx['precip']['kind']} {wx['precip']['inches']}in")
        if wx.get("humidity"):
            parts.append(f"humidity {wx['humidity']['low']}-{wx['humidity']['high']}%")
        if wx.get("severe"):
            parts.append("severe weather")
    parts += [solar[k] for k in ("sunrise_label", "sunset_label") if solar.get(k)]
    return f"  [dim]{escape(' | '.join(parts))}[/dim]" if parts else None


def render_calendar(
    payload: dict[str, Any], *, mode: str = "calendar", cards: list[dict[str, Any]] | None = None
) -> None:
    """Print a calendar range payload as a per-day agenda.

    Uses the server's preformatted labels. Every ``window`` day prints,
    quiet ones included; ``days`` entries outside it don't. Multi-day
    ``spanning`` items come first. ``mode`` picks the layer (see
    ``_day_rows``); Planning's detector ``cards`` print after the days.
    Plain text, grep-able, like the other list renderers.
    """
    # ``window`` names every requested date; ``days`` only those with activity. A connected
    # calendar prints the whole window, so a quiet day still gets its heading.
    window_rows = payload.get("window") or []
    window = {w["date"] for w in window_rows}
    by_date = {d.get("date"): d for d in payload.get("days") or []}
    connected = payload.get("state") in (None, "connected")
    days = [{**w, **by_date.get(w["date"], {})} for w in window_rows if connected] or [
        d for d in by_date.values() if not window or d.get("date") in window
    ]
    # Followed and email suggestions can span days too: same layer rule as day events.
    spanning = [
        ev
        for ev in payload.get("spanning") or []
        if ev.get("kind") not in _IMPUTED or mode == "planning"
    ]

    def out(text: str = "") -> None:
        # Piped output is read by agents and scripts: never hard-wrap it at 80 columns.
        _stdout.print(text, soft_wrap=not _stdout.is_terminal)

    if spanning:
        out("[bold]Multi-day[/bold]")
        for ev in spanning:
            tag = " (ongoing)" if ev.get("ongoing") else ""
            if ev.get("kind") in _IMPUTED:
                tag += f"  [dim]({_IMPUTED[ev['kind']]})[/dim]"
            out(f"  {escape(ev.get('range_label') or '')}  {escape(ev.get('summary') or '')}{tag}")
        out()
    for day in days:
        label, date_label = day.get("label") or "", day.get("date_label") or ""
        extra = f" [dim]{escape(date_label)}[/dim]" if date_label != label else ""
        out(f"[bold]{escape(label)}[/bold]{extra}")
        env = payload.get("environment")
        if env and (conditions := _conditions_line(env, day.get("date") or "")):
            out(conditions)
        if env and day.get("date") == (window and min(window)):
            out_bands = [b.get("label") for b in env.get("precipitation") or []]
            if out_bands:
                out(f"  [dim]{escape('precipitation: ' + '; '.join(out_bands))}[/dim]")
        rows = _day_rows(day, mode)
        plan = payload.get("schedule_plan")
        if mode == "schedule" and plan and plan.get("date") == day.get("date"):
            rows = sorted(rows + _proposed_rows(plan), key=lambda r: r[0])
        for _, line in rows:
            out(line)
        if not rows:
            out("  [dim](nothing scheduled)[/dim]")
    plan = payload.get("schedule_plan")
    if mode == "schedule" and plan and plan.get("date") not in {d.get("date") for d in days}:
        # No calendar days (not connected) but todos still get a plan.
        proposed = _proposed_rows(plan)
        if proposed:
            out(f"[bold]{escape(plan['date'])}[/bold]")
            for _, line in sorted(proposed, key=lambda r: r[0]):
                out(line)
    if not days and payload.get("state") not in (None, "connected"):
        out("[dim](calendar not connected)[/dim]")
    if cards:
        out("\n[bold]Suggestions[/bold] [dim](not on your calendar)[/dim]")
        for card in cards:
            why = f" - {escape(card['rationale'])}" if card.get("rationale") else ""
            kind = escape(f"[{card.get('kind')}]")
            out(f"  {kind} {escape(card.get('title') or '')}{why}")
    if payload.get("truncated"):
        out("[dim](more events than shown; try fewer days)[/dim]")


#: Human labels for the destination names the server stores. Mirrors
#: ``pepper.agent.scheduled_tasks.destinations.LABELS`` — a name the CLI
#: doesn't know falls through verbatim rather than being dropped, so a
#: destination added server-side still renders honestly here.
_DESTINATION_LABELS = {
    "main_chat": "main chat",
    "resend_email": "email",
    "twilio_sms": "SMS",
    "whatsapp": "WhatsApp",
    "client_api": "app",
    "http": "task thread only",
}


def _describe_destinations(destinations: list[str] | None) -> str:
    """One-line summary of where a task announces its results.

    The three states are rendered as three different sentences on
    purpose: ``None`` (inherit) and ``[]`` (silent) look the same in a
    naive renderer and mean opposite things to the user — one follows
    their notification default, the other deliberately tells nobody.
    """
    if destinations is None:
        return "your default"
    if not destinations:
        return "nobody (silent)"
    return ", ".join(_DESTINATION_LABELS.get(d, d) for d in destinations)


def render_scheduled_task_list(rows: list[ScheduledTaskSummary]) -> None:
    """Pretty-print a scheduled-task listing to stdout.

    One line per task: state glyph, name, schedule, next fire (localized),
    delivery, then a dim id. Plain text — no Rich tables — so the output
    stays grep-able, matching the trajectory and todo listings.
    """
    if not rows:
        _stdout.print("(no scheduled tasks)")
        return
    for row in rows:
        glyph = "[green]●[/green]" if row.active else "[dim]○[/dim]"
        schedule = "one-off" if row.one_shot else f"cron {row.cron}"
        line = f"{glyph} {row.name}  {schedule}"
        # A paused row's next_run_at is whatever it held when it stopped —
        # printing it as "next" would be a lie, so say what it is instead.
        line += f"  {'next' if row.active else 'was due'} {_format_stamp(row.next_run_at)}"
        # A room's row carries the server's own sentence: its ``None`` is the
        # room's default, which "your default" would misdescribe.
        # (``getattr``: this module imports the wire types for checking only.)
        delivery = getattr(row, "delivery", None)
        if delivery is not None:
            line += f"  → {escape(delivery)}"
            last_run_status = getattr(row, "last_run_status", None)
            if last_run_status:
                line += f"  last run {escape(last_run_status)}"
        else:
            line += f"  → {_describe_destinations(row.destinations)}"
        line += f"  [dim]{row.id}[/dim]"
        _stdout.print(line)


def render_scheduled_task(detail: ScheduledTaskDetail) -> None:
    """Pretty-print one task's configuration and its most recent fire."""
    state = "active" if detail.active else "paused"
    _stdout.print(f"[bold]{detail.name}[/bold] ({state})  [dim]{detail.id}[/dim]")
    if detail.description:
        _stdout.print(detail.description)
    schedule = "one-off" if detail.one_shot else f"cron {detail.cron}"
    if detail.timezone:
        schedule += f" ({detail.timezone})"
    _stdout.print(f"schedule {schedule}")
    _stdout.print(f"{'next' if detail.active else 'was due'} {_format_stamp(detail.next_run_at)}")
    _stdout.print(f"delivered to {_describe_destinations(detail.destinations)}")
    if detail.latest_run is None:
        _stdout.print("last run (never fired)")
        return
    _stdout.print("last run:")
    render_scheduled_task_runs([detail.latest_run])


def render_scheduled_task_runs(rows: list[ScheduledTaskRunEntry]) -> None:
    """Pretty-print a task's fire history to stdout, newest first."""
    if not rows:
        _stdout.print("(no runs yet)")
        return
    for row in rows:
        line = f"{_format_stamp(row.fired_at)}  {row.status}"
        if row.delivered_channels:
            line += f"  → {_describe_destinations(row.delivered_channels)}"
        if row.error:
            line += f"  [red]{_short(row.error)}[/red]"
        elif row.reply_preview:
            line += f"  {_short(row.reply_preview)}"
        if row.trajectory_id:
            line += f"  [dim]{row.trajectory_id}[/dim]"
        _stdout.print(line)


def render_scheduled_task_thread(rows: list[ScheduledTaskMessage]) -> None:
    """Pretty-print a task's own conversation thread, oldest first."""
    if not rows:
        _stdout.print("(no messages in this task's thread)")
        return
    for row in rows:
        _stdout.print(f"[dim]{_format_stamp(row.created_at)}[/dim] [bold]{row.role}[/bold]:")
        _stdout.print(row.text)


#: Server-recognized ``kind`` values (mirrors
#: ``pepper.features.groups.contract`` / ``CreateGroupRequest``'s docstring)
#: mapped to the label a person recognizes: a named, persistent room is a
#: "channel"; an unnamed room that is exactly its member set is a "room".
_KIND_LABELS = {"open": "channel", "sealed": "room"}


def render_group_list(gl: GroupList) -> None:
    """Pretty-print a rooms listing to stdout.

    One line per room: name, member count, the server-rendered relative
    activity label, an archived tag, then a dim id. Plain text — no Rich
    tables — matching every other listing in this CLI.

    ``kind`` and unread counts are deliberately absent: ``GET
    /api/v1/groups`` doesn't carry either (only ``GET
    /api/v1/groups/{id}`` — ``groups show`` — has ``kind``), and fetching
    it per room here would turn one list call into N+1. Left as a
    follow-up rather than guessed at.

    ``degraded`` means "could not ask", not "nothing new" — printed as a
    note so a client reading this list doesn't mistake a stale answer for
    a complete one.
    """
    if gl.degraded:
        _stdout.print("[yellow](groups service degraded — this list may be incomplete)[/yellow]")
    if not gl.groups:
        _stdout.print("(no rooms)")
        return
    for g in gl.groups:
        tag = " [dim](archived)[/dim]" if g.archived else ""
        # ``display_name`` is member-settable (a room rename) and interpolated
        # alongside our own ``[dim]``-style chrome, so it's escaped —
        # otherwise a room named e.g. "[bold red]Trip[/]" would inject
        # formatting, or a stray unmatched tag could raise. Everything else
        # on this line (count, activity label, id) is server-computed, not
        # free text.
        # soft_wrap: the id at the end of the line is the one thing here a
        # person is likely to copy-paste, and Rich's default wrapping would
        # otherwise fold it across two lines at the terminal width.
        _stdout.print(
            f"{escape(g.display_name)}{tag}  {g.member_count} members  "
            f"{g.sub}  [dim]{g.group_id}[/dim]",
            soft_wrap=True,
        )


def render_group_topics(rows: list[GroupTopicSummary]) -> None:
    """Pretty-print a room's topic list to stdout, busiest first (the order
    the server already answers them in)."""
    if not rows:
        _stdout.print("(no topics)")
        return
    for t in rows:
        tag = " [dim](archived)[/dim]" if t.archived_at is not None else ""
        # soft_wrap: same reasoning as render_group_list — don't fold the id.
        _stdout.print(
            f"{escape(t.name)}{tag}  active {_format_stamp(t.last_activity_at)}  "
            f"[dim]{t.topic_id}[/dim]",
            soft_wrap=True,
        )


def render_group_roster(rows: list[GroupRosterMember]) -> None:
    """Pretty-print a room's ``@``-mention roster to stdout, one member per
    line with the person id a ``--mention`` takes."""
    if not rows:
        _stdout.print("(no members)")
        return
    for m in rows:
        tag = " [dim](Pepper)[/dim]" if m.is_pepper else " [dim](agent)[/dim]" if m.is_agent else ""
        # soft_wrap: the id is what a person copies — same as the lists above.
        _stdout.print(f"{escape(m.display_name)}{tag}  [dim]{m.person_id}[/dim]", soft_wrap=True)


def render_group_detail(detail: dict[str, Any]) -> None:
    """Pretty-print one room's info to stdout: kind, members + roles,
    topics, installed apps.

    ``detail`` is the raw ``GET /api/v1/groups/{id}`` JSON (a dict on
    purpose — see ``ApiClient.get_group``'s docstring): thirty-odd nested
    fields nothing else in this package reads would be a second
    description to drift from, so this reads it defensively with
    ``.get()`` rather than assuming every key is present.

    Every member-settable string (room name, member display names, topic
    names) is ``rich.markup.escape``'d before it's interpolated alongside
    this function's own ``[bold]``/``[dim]`` chrome — a display name or a
    room rename is exactly as attacker-controlled as a message body, and
    unescaped it could inject formatting or abort the render on a stray
    unmatched tag.
    """
    kind = detail.get("kind")
    kind_label = _KIND_LABELS.get(kind, kind or "unknown")
    _stdout.print(
        f"[bold]{escape(detail.get('display_name') or '(unnamed)')}[/bold]  ({kind_label})  "
        f"[dim]{detail.get('group_id', '')}[/dim]"
    )
    members = detail.get("members") or []
    _stdout.print(f"members ({len(members)}):")
    for m in members:
        you = " [dim](you)[/dim]" if m.get("is_you") else ""
        role = m.get("role") or "member"
        agent = ""
        if m.get("is_agent"):
            sponsor = m.get("sponsor_display_name")
            run_by = f", run by {escape(sponsor)}" if sponsor else ""
            agent = f" [dim](agent{run_by})[/dim]"
        _stdout.print(f"  {escape(m.get('display_name') or 'Someone')}{you}{agent}  — {role}")
    topics = detail.get("topics") or []
    _stdout.print(f"topics ({len(topics)}):")
    for t in topics:
        tag = " [dim](archived)[/dim]" if t.get("archived") else ""
        _stdout.print(f"  {escape(t.get('name') or '')}{tag}")
    apps = detail.get("apps") or {}
    if apps.get("unavailable"):
        _stdout.print("apps: [dim](unavailable — couldn't ask)[/dim]")
    else:
        installed = apps.get("installed") or []
        if installed:
            _stdout.print("apps:")
            for a in installed:
                _stdout.print(f"  {escape(a.get('title') or a.get('app_key') or '')}")
        else:
            _stdout.print("apps: (none installed)")


def render_note_list(result: ArtifactListResponse) -> None:
    """Pretty-print a ``pepper notes list``/``search`` page to stdout.

    One line per note: name, labels (if any), ``updated`` timestamp
    (localized to the machine's own zone via :func:`_format_stamp` — same
    convention as the todo and scheduled-task listings), then a dim id.
    Plain text — no Rich tables — matching every other listing here.

    A truncated page says so: the server's ``truncated`` flag means there
    are more rows than fit in this page, and silently showing 50 of some
    larger total without a word would look like the whole answer.
    """
    if not result.items:
        _stdout.print("(no notes)")
        return
    for note in result.items:
        # The whole bracketed literal is escaped, not just each label: an
        # unescaped ``[recipe]`` is itself a Rich tag and silently vanishes,
        # and a label like ``[/dim]`` would close our own markup early.
        labels = "[" + ", ".join(note.labels) + "]"
        tag = f"  [dim]{escape(labels)}[/dim]" if note.labels else ""
        # soft_wrap: same reasoning as render_group_list — don't fold the id.
        _stdout.print(
            f"{escape(note.name)}{tag}  updated {_format_stamp(note.updated_at)}  "
            f"[dim]{note.id}[/dim]",
            soft_wrap=True,
        )
    if result.truncated:
        _stdout.print("[dim](more notes than shown — narrow with --label or --room)[/dim]")


def render_group_messages(page: GroupMessagePage) -> None:
    """Pretty-print one page of a topic's messages to stdout, oldest first.

    Timestamps use ``created_at_display`` verbatim — server-preformatted in
    the user's own timezone and date format (``GroupMessage``'s docstring:
    "show them; never re-derive them"), which is what makes this render the
    repo's "always render times in the user's local timezone" rule without
    this client owning any zone logic of its own.

    Every participant-controlled string (display name, "via" name,
    attachment filename) is ``rich.markup.escape``'d before it's
    interpolated alongside this function's own ``[dim]``/``[bold]`` chrome.
    The message body gets a stronger treatment: it's printed with
    ``markup=False`` on its own, unadorned call rather than escaped and
    merged into a chrome string, since it's the one field here with no
    formatting of ours around it to preserve.

    A day header (``day_label`` — the same server-preformatted string the
    web room fragment's own day divider prints) is inserted wherever
    ``day_key`` changes, so a transcript spanning more than one day says
    which day each run of messages is from instead of only a bare
    ``created_at_display`` clock time. A message with an empty ``day_key``
    or ``day_label`` — the server's broken-message placeholder, when its
    ``created_at`` couldn't be parsed — neither prints a header nor resets
    the current day, so it sits inside the surrounding day's run rather
    than opening a blank one (the same guard as the web room's
    ``group_room.js``).
    """
    if not page.messages:
        _stdout.print("(no messages)")
    last_day_key: str | None = None
    for m in page.messages:
        if m.day_key and m.day_label and m.day_key != last_day_key:
            if last_day_key is not None:
                _stdout.print()
            _stdout.print(f"[bold]{m.day_label}[/bold]")
            last_day_key = m.day_key
        display_name = escape(m.display_name)
        # "*agent* · for *requester*" — on a deleted
        # post too, as the web's badge is: who wrote it doesn't change.
        agent_tag = ""
        if m.is_agent:
            agent_tag = (
                f" [dim](agent, for {escape(m.for_display_name)})[/dim]"
                if m.for_display_name
                else " [dim](agent)[/dim]"
            )
        if m.is_deleted:
            _stdout.print(
                f"[dim]{m.created_at_display}[/dim] [bold]{display_name}[/bold]{agent_tag}: "
                "[dim](message deleted)[/dim]"
            )
            continue
        via = f" [dim](via {escape(m.via_display_name)})[/dim]" if m.via_display_name else ""
        via += agent_tag
        edited = " [dim](edited)[/dim]" if m.is_edited else ""
        _stdout.print(
            f"[dim]{m.created_at_display}[/dim] [bold]{display_name}[/bold]{via}{edited}:"
        )
        if m.is_broken:
            _stdout.print("[dim](message could not be rendered)[/dim]")
        else:
            # Rich's markup parser would otherwise interpret any
            # ``[...]``-shaped text a participant typed into the message —
            # misrendering it at best, aborting the whole ``groups read``
            # on a malformed/unmatched tag at worst. The body is untrusted
            # in a way nothing else printed here is, so it gets its own
            # plain, unparsed print rather than being escaped into a
            # chrome string like the fields above.
            _stdout.print(m.raw_body, markup=False, highlight=False)
        if m.attachments:
            names = ", ".join(
                escape(str(a.get("filename") or a.get("id") or "attachment")) for a in m.attachments
            )
            _stdout.print(f"  [dim]📎 {names}[/dim]")
        if m.reactions:
            chips = " ".join(f"{r.get('emoji', '')}{r.get('count', '')}" for r in m.reactions)
            _stdout.print(f"  {chips}")
    if page.next_before:
        # soft_wrap: the cursor is meant to be copy-pasted into the next
        # --before, and Rich's default wrapping would otherwise fold it
        # across two lines at the terminal width.
        _stdout.print(f"[dim](more — pass --before {page.next_before})[/dim]", soft_wrap=True)


def _format_stamp(iso: str) -> str:
    """Render a UTC ISO-8601 timestamp in the machine's local zone, labelled.

    The CLI runs on the user's own machine, so its zone is the right one
    per the repo's "always render times in the user's local timezone"
    rule — the server deliberately hands out raw UTC and leaves this to
    the client.

    The zone label is not decoration. A cron task carries its own pinned
    timezone, which ``show`` prints beside the schedule, and the machine's
    zone is frequently not that one — an unlabelled ``next 14:00`` under a
    ``(America/New_York)`` pin reads as 14:00 New York and is off by the
    offset between them. Naming the zone the stamp is actually in is what
    makes the two lines consistent instead of contradictory, and it costs
    nothing on the common case where they agree.

    Falls back to the raw string when it can't be parsed: better a
    verbatim timestamp than a crashed listing.
    """
    import datetime as dt

    try:
        parsed = dt.datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return iso
    rendered = parsed.strftime("%Y-%m-%d %H:%M")
    # %Z is empty on a platform that can't name the local zone; fall back
    # to the numeric offset so the stamp is never silently unanchored.
    label = parsed.strftime("%Z") or parsed.strftime("%z")
    return f"{rendered} {label}" if label else rendered


def _format_due(iso: str, date_only: bool | None = None) -> str:
    """Render a UTC ISO-8601 due date for display.

    When ``date_only`` is ``True`` the server stored midnight UTC on a
    *named day*, not an instant — show that day's ``YYYY-MM-DD`` straight
    off the UTC value with no zone conversion, exactly as the web
    applet's ``fmtDue`` does, or a client east of UTC renders a clock
    that was never there and a client west of UTC shows the wrong day.
    Otherwise localize to the machine's own zone: ``YYYY-MM-DD`` for a
    deadline that happens to land on local midnight, ``YYYY-MM-DD HH:MM``
    otherwise. Falls back to the raw string if it can't be parsed —
    better a verbatim timestamp than a crashed listing.
    """
    import datetime as dt

    try:
        parsed_utc = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if date_only:
        return parsed_utc.strftime("%Y-%m-%d")
    parsed = parsed_utc.astimezone()
    if (parsed.hour, parsed.minute) == (0, 0):
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%d %H:%M")


def _short_json(value: Any, budget: int) -> str:
    """JSON-serialize ``value`` and truncate to ``budget`` chars.

    The equivalent of jq's ``payload | tostring | .[0:budget]``.
    ``default=str`` rather than raising on
    a non-serializable value: a payload dict from the server is JSON
    by definition, but defensiveness here costs nothing and keeps
    the renderer from dying mid-event if a future field slips
    through as something exotic.
    """
    try:
        rendered = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > budget:
        return rendered[:budget] + "…"
    return rendered


def _short(value: Any) -> str:
    """Format a value for inline display, truncating long strings.

    Strings get a ``…`` suffix past the budget; other types pass
    through ``repr`` (so ``{}`` / ``True`` / numbers all render
    sensibly) and then the same truncation rule applies."""
    s = value if isinstance(value, str) else repr(value)
    if len(s) > _TOOL_RESULT_PREVIEW_CHARS:
        return s[:_TOOL_RESULT_PREVIEW_CHARS] + "…"
    return s


def _next_item_line(n: int | None, item: dict[str, Any]) -> str:
    """One Do next / Due soon / Later row from an item's own fields (plan item or todo row)."""
    due = item.get("due") or {}
    due_label = due.get("label") or (
        _format_due(item["due_date"], item.get("due_is_date_only"))
        if item.get("due_date")
        else None
    )
    if due_label == "No due date":
        due_label = None
    elif due_label and not due_label.lower().startswith("due"):
        due_label = f"due {due_label}"
    bits = [
        item.get("estimate_label"),
        due_label,
        item.get("source_label") or item.get("room"),
        f"room {item['principal']}" if item.get("principal") else None,
    ]
    meta = f"  [dim]({escape(', '.join(b for b in bits if b))})[/dim]" if any(bits) else ""
    head = f"{n:>2}. " if n else "  - "
    line = f"{head}{escape(item.get('title') or '(untitled)')}{meta}"
    if item.get("reason"):
        line += f"\n      [dim]{escape(item['reason'])}[/dim]"
    return line


def render_next(result: dict[str, Any]) -> None:
    """Print a ``pepper next`` result: Now, a numbered Do next, Due soon, Later."""

    def out(text: str = "") -> None:
        _stdout.print(text, soft_wrap=not _stdout.is_terminal)

    out("[bold]Now[/bold]")
    for ev in result["now"]:
        if "countdown" in ev:  # schedule-plan's next_up
            when = f"{ev.get('start_label')}-{ev.get('end_label')}"
            title, countdown = escape(ev.get("title") or ""), escape(ev["countdown"])
            out(f"  {title}  [dim]{escape(when)}, {countdown}[/dim]")
            if ev.get("leave"):
                out(f"  [dim]leave by {escape(str(ev['leave']))}[/dim]")
        else:
            out(_calendar_event_line(ev))
    if not result["now"]:
        out("  [dim](nothing on the calendar)[/dim]")
    do_next = result["do_next"]
    out("\n[bold]Do next[/bold]")
    info = result.get("plan")
    if info and info.get("name"):
        mark = ", Pepper's choice today" if info["chosen_by_pepper"] else ""
        sub = escape(info.get("subtitle") or "")
        out(f"  [bold]{escape(info['name'])}[/bold]{mark}  [dim]{sub}[/dim]")
        diff = [o for o in info["others"] if o["differs"]]
        if diff:
            parts = []
            for o in diff:
                n = o["blocks"]
                fits = f"fits {n} block{'' if n == 1 else 's'}"
                head = f"starts with {o['first_block']} and " if o["first_block"] else ""
                parts.append(f"{o['name']} {head}{fits}")
            out(f"  [dim]{escape('; '.join(parts))}[/dim]")
        elif info.get("agree_note"):
            out(f"  [dim]{escape(info['agree_note'])}[/dim]")
    if do_next["note"]:
        out(f"  [dim]{escape(do_next['note'])}[/dim]")
    for n, item in enumerate(do_next["items"], 1):
        out(_next_item_line(n, item))
    if not do_next["items"]:
        out("  [dim](nothing)[/dim]")
    if result["due_soon"]:
        out("\n[bold]Due soon[/bold] [dim](next 48 hours)[/dim]")
        for item in result["due_soon"]:
            out(_next_item_line(None, item))
    later = result["later"]
    if later["count"]:
        out(f"\n[bold]Later[/bold] [dim]({later['count']})[/dim]")
        for item in later["items"]:
            out(_next_item_line(None, {"title": item.get("title")}))
        if later["count"] > len(later["items"]):
            out(f"  [dim]... {later['count'] - len(later['items'])} more (--all)[/dim]")
