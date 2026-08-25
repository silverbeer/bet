"""The command tree from BET_IMPLEMENTATION_PLAN.md section 5.

Registered as stubs so ``bet --help`` shows the shape of the finished product
and each command reports the ticket that will implement it. A stub that names
its ticket is more useful than a command that does not exist: it answers "is
this planned?" without a trip to the backlog.

``config`` and ``doctor`` are real and are registered separately.
"""

from __future__ import annotations

import typer

from bet.errors import BetError

# group -> help text -> {command: (help, ticket)}
TREE: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    "import": (
        "Import from trusted sportsbook exports.",
        {
            "detect": ("Identify which profile matches a file.", "SB-715"),
            "validate": ("Validate a file without committing it.", "SB-715"),
            "review": ("Review records held for manual attention.", "SB-712"),
            "history": ("List previous import runs.", "SB-715"),
            "rollback": ("Undo an import run.", "SB-715"),
        },
    ),
    "ingest": (
        "Ingest lower-confidence document channels.",
        {
            "pdf": ("Extract bets from a PDF statement.", "SB-709"),
            "screenshot": ("Extract bets from a screenshot.", "SB-759"),
        },
    ),
    "sportsbook": (
        "Inspect sportsbooks and accounts.",
        {
            "list": ("List supported sportsbooks.", "SB-744"),
            "status": ("Show per-sportsbook import status.", "SB-744"),
            "summary": ("Performance by sportsbook.", "SB-744"),
            "accounts": ("List sportsbook accounts.", "SB-701"),
        },
    ),
    "bets": (
        "Query individual bets.",
        {
            "list": ("List bets.", "SB-812"),
            "show": ("Show one bet in full.", "SB-745"),
            "open": ("List unsettled bets.", "SB-745"),
            "search": ("Search bets.", "SB-745"),
            "correct": ("Record a correction to a bet.", "SB-703"),
        },
    ),
    "sport": (
        "Performance by sport.",
        {
            "list": ("List sports seen in the warehouse.", "SB-744"),
            "summary": ("Performance for one sport.", "SB-744"),
        },
    ),
    "team": (
        "Performance by team.",
        {
            "search": ("Find a team by name.", "SB-744"),
            "summary": ("Performance for one team.", "SB-744"),
        },
    ),
    "player": (
        "Performance by player.",
        {
            "search": ("Find a player by name.", "SB-744"),
            "summary": ("Performance for one player.", "SB-744"),
        },
    ),
    "strategy": (
        "Saved cohort rules.",
        {
            "list": ("List strategies.", "SB-746"),
            "create": ("Create a strategy.", "SB-746"),
            "show": ("Show a strategy definition.", "SB-746"),
            "evaluate": ("Evaluate a strategy over settled bets.", "SB-746"),
            "compare": ("Compare strategies.", "SB-746"),
            "archive": ("Archive a strategy.", "SB-746"),
        },
    ),
    "analyze": (
        "Run analytical work.",
        {
            "summary": ("Overall performance summary.", "SB-744"),
            "trends": ("Performance change over time.", "SB-746"),
            "opportunity": ("Compare a candidate bet to history.", "SB-746"),
            "cohort": ("Analyse an ad-hoc cohort.", "SB-746"),
            "refresh": ("Refresh derived tables.", "SB-742"),
        },
    ),
    "review": (
        "Curated periodic reviews.",
        {
            "daily": ("Daily review.", "SB-746"),
            "weekly": ("Weekly review.", "SB-746"),
            "monthly": ("Monthly review.", "SB-746"),
            "period": ("Review an arbitrary period.", "SB-746"),
        },
    ),
    "sports": (
        "Independent sports data.",
        {
            "sync": ("Sync sports data from a provider.", "SB-747"),
            "events": ("List events.", "SB-747"),
            "teams": ("List teams.", "SB-747"),
            "players": ("List players.", "SB-747"),
            "resolve": ("Resolve unmatched entities.", "SB-747"),
        },
    ),
    "market": (
        "Market and line intelligence.",
        {
            "import": ("Import market snapshots.", "SB-748"),
            "lines": ("Show line snapshots.", "SB-748"),
            "movements": ("Show line movement.", "SB-748"),
            "promotions": ("Show offered promotions.", "SB-748"),
        },
    ),
    "agent": (
        "Audited agent workflows.",
        {
            "daily-review": ("Agent daily review.", "SB-749"),
            "weekly-review": ("Agent weekly review.", "SB-749"),
            "monthly-review": ("Agent monthly review.", "SB-749"),
            "opportunity-analysis": ("Agent opportunity analysis.", "SB-749"),
            "lessons-learned": ("Agent lessons learned.", "SB-749"),
            "runs": ("List agent runs.", "SB-749"),
            "show": ("Show one agent run.", "SB-749"),
        },
    ),
    "report": (
        "Exported report artifacts.",
        {
            "export": ("Export a report.", "SB-744"),
            "list": ("List exported reports.", "SB-744"),
        },
    ),
}

# Commands that sit at the root rather than inside a group.
ROOT_STUBS: dict[str, tuple[str, str]] = {
    "roi": ("Overall return on investment.", "SB-743"),
    "wager-type": ("Performance by wager type.", "SB-744"),
    "promotion": ("Performance by promotion.", "SB-744"),
    "league": ("Performance by league.", "SB-744"),
    "lessons": ("Durable findings from settled history.", "SB-746"),
    "watch": ("Watch an inbox directory for new exports.", "SB-758"),
}


class NotImplementedYetError(BetError):
    """A planned command that has not been built."""

    exit_code = 69  # sysexits.h EX_UNAVAILABLE


def _stub(name: str, ticket: str) -> None:
    raise NotImplementedYetError(
        f"`bet {name}` is not implemented yet.",
        remediation=f"Tracked by {ticket}.",
    )


def _add_stub(app: typer.Typer, name: str, display: str, help_text: str, ticket: str) -> None:
    """Register ``name`` on ``app``; ``display`` is the full path shown to users."""

    @app.command(name=name, help=f"{help_text}  [not yet implemented \u2014 {ticket}]")
    def _command() -> None:
        _stub(display, ticket)


def register(app: typer.Typer) -> None:
    """Attach every planned group and command to ``app`` as stubs."""
    for group, (group_help, commands) in TREE.items():
        sub = typer.Typer(name=group, help=group_help, no_args_is_help=True)
        for command, (command_help, ticket) in commands.items():
            _add_stub(sub, command, f"{group} {command}", command_help, ticket)
        app.add_typer(sub, name=group)

    for name, (help_text, ticket) in ROOT_STUBS.items():
        _add_stub(app, name, name, help_text, ticket)
