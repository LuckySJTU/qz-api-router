"""TUI Dashboard - real-time display of backend status, load, and response times."""
import asyncio
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static
from textual.containers import Vertical
from textual import work

from router import APIRouter


class StatsBar(Static):
    """Displays global router statistics."""
    def update_stats(self, stats: dict):
        self.update(
            f"[dim]github.com/LuckySJTU/qz-api-router[/]\n"
            f"[bold]Total Requests:[/] {stats['total_proxied']}  "
            f"[green]Success:[/] {stats['total_success']}  "
            f"[red]Fail:[/] {stats['total_fail']}  "
            f"[cyan]Sticky:[/] {stats['sticky_mappings']}"
        )


class BackendTable(DataTable):
    """Table showing per-backend status."""
    def on_mount(self):
        self.add_columns(
            "Backend", "URL", "Status", "Load",
            "Sent", "Received", "Success", "Fail", "Avg RT (ms)", "Consec. Failures"
        )
        self.cursor_type = "row"

    def refresh_data(self, backends: list):
        self.clear()
        for b in backends:
            status_display = b["status"]
            if status_display == "available":
                status_display = "[green]available[/]"
            elif status_display == "unavailable":
                status_display = "[red]unavailable[/]"
            else:
                status_display = "[yellow]recovering[/]"

            self.add_row(
                b["name"],
                b["url"],
                status_display,
                str(b["current_load"]),
                str(b["total_requests_sent"]),
                str(b["total_responses_received"]),
                str(b["total_success"]),
                str(b["total_fail"]),
                str(b["avg_response_time_ms"]),
                str(b["consecutive_failures"]),
            )


class ErrorLog(Static):
    """Displays recent errors from backends."""
    def update_errors(self, backends: list):
        lines = []
        for b in backends:
            for err in b.get("recent_errors", [])[-2:]:
                ts = err["time"]
                lines.append(f"[red]{b['name']}[/] @ {ts:.0f}: {err['error']}")
        if not lines:
            lines.append("[dim]No recent errors[/]")
        self.update("\n".join(lines[-6:]))


class RouterDashboard(App):
    """Textual TUI application for monitoring the API Router."""

    CSS = """
    Screen {
        layout: vertical;
    }
    StatsBar {
        height: 3;
        padding: 0 1;
        background: $surface;
        width: 100%;
    }
    BackendTable {
        height: 1fr;
    }
    ErrorLog {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: 0 1;
        background: $surface;
        border-top: solid $primary;
        width: 100%;
    }
    """

    TITLE = "API Router Dashboard"
    SUBTITLE = "github.com/LuckySJTU/qz-api-router"
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh Now")]

    def __init__(self, api_router: APIRouter, refresh_interval: float = 1.0, **kwargs):
        super().__init__(**kwargs)
        self.api_router = api_router
        self.refresh_interval = refresh_interval

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar(id="stats-bar")
        yield BackendTable(id="backend-table")
        yield ErrorLog(id="error-log")
        yield Footer()

    def on_mount(self):
        self.set_interval(self.refresh_interval, self._refresh)

    def _refresh(self):
        stats = self.api_router.get_stats()
        self.query_one("#stats-bar", StatsBar).update_stats(stats)
        self.query_one("#backend-table", BackendTable).refresh_data(stats["backends"])
        self.query_one("#error-log", ErrorLog).update_errors(stats["backends"])

    def action_refresh(self):
        self._refresh()

    def action_quit(self):
        self.exit()
