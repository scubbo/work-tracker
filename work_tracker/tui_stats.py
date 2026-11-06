"""Read-only daily history client. The application owns collection and aggregation."""

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static

from .daily_stats import duration_text
from .tui_close import literal


class StatisticsScreen(ModalScreen):
    BINDINGS = [Binding('escape', 'close', 'Back'), Binding('r', 'reload', 'Reload stats')]
    CSS = """
    StatisticsScreen { align: center middle; background: $background 75%; }
    StatisticsScreen > Vertical { width: 98%; height: 94%; padding: 1 1;
        background: $surface; border: round $primary; }
    StatisticsScreen #stats-heading { height: 1; text-style: bold; }
    StatisticsScreen #stats-note { height: auto; max-height: 3; margin-bottom: 1; }
    StatisticsScreen #stats-controls { height: 3; }
    StatisticsScreen #stats-project { width: 1fr; margin-right: 1; }
    StatisticsScreen #stats-days { width: 20; }
    StatisticsScreen #stats-table { height: 1fr; }
    StatisticsScreen #stats-status { height: auto; max-height: 4; }
    StatisticsScreen #stats-buttons { height: 3; align-horizontal: right; }
    StatisticsScreen Button { margin-left: 1; }
    """

    def __init__(self):
        super().__init__()
        self.project = ''
        self.days = 30
        self.projects = None

    def compose(self):
        with Vertical():
            yield Static('Daily PR statistics', id='stats-heading', markup=False)
            yield Static('Mean and p90 use closures on that local calendar day. Today is provisional. '
                         'Unallocated is a separate project. Scroll sideways for all columns.', id='stats-note', markup=False)
            with Horizontal(id='stats-controls'):
                yield Select([('Overall', ''), ('Overall and all projects', '*')], value='', allow_blank=False, id='stats-project')
                yield Select([('Last 7 days', 7), ('Last 30 days', 30), ('Last 90 days', 90), ('Last year', 365)],
                             value=30, allow_blank=False, id='stats-days')
            yield DataTable(id='stats-table', cursor_type='row', zebra_stripes=True)
            yield Static('Loading saved snapshots...', id='stats-status', markup=False)
            with Horizontal(id='stats-buttons'):
                yield Button('Reload', id='stats-reload')
                yield Button('Back', id='stats-back')

    def on_mount(self):
        table = self.query_one('#stats-table', DataTable)
        for label, key in (('Day', 'day'), ('Project', 'project'), ('Tracked', 'tracked'), ('Closed', 'closed'),
                           ('Merged', 'merged'), ('Oldest open', 'oldest'), ('Mean close', 'mean'), ('P90 close', 'p90'),
                           ('Timed/all', 'timed'), ('Stale', 'stale'), ('Missing O/C', 'dates')):
            table.add_column(label, key=key)
        table.focus()
        self.load_stats()

    def action_reload(self):
        self.load_stats()

    def action_close(self):
        self.dismiss()

    @on(Select.Changed)
    def changed(self, event):
        if event.select.id == 'stats-project' and isinstance(event.value, str) and self.project != event.value:
            self.project = event.value
            self.load_stats()
        elif event.select.id == 'stats-days' and isinstance(event.value, int) and self.days != event.value:
            self.days = event.value
            self.load_stats()

    @work(group='statistics-read', exclusive=True, exit_on_error=False)
    async def load_stats(self):
        try:
            value = await self.app.call_api('statistics', {'days': self.days, 'project': self.project or None, 'limit': 5000})
            if not self.is_mounted:
                return
            projects = [(project['id'], project['name']) for project in value['projects']]
            if projects != self.projects:
                self.projects = projects
                selector = self.query_one('#stats-project', Select)
                selector.set_options([('Overall', ''), ('Overall and all projects', '*'),
                                      *[(Text(literal(name)), key) for key, name in projects]])
                selector.value = self.project
            table = self.query_one('#stats-table', DataTable)
            table.clear()
            for row in value['items']:
                count_prefix = '' if row['closure_counts_complete'] else '>='
                age = duration_text(row['oldest_open_age_seconds'])
                if not row['oldest_open_complete']:
                    age = '>='+age if row['oldest_open_age_seconds'] is not None else 'Unknown'
                table.add_row(Text(row['day']+(' *' if row['provisional'] else '')), Text(literal(row['project'])),
                              str(row['total_tracked']), count_prefix+str(row['closed_count']), count_prefix+str(row['merged_count']), age,
                              duration_text(row['mean_closure_seconds']), duration_text(row['p90_closure_seconds']),
                              f"{row['closure_sample_count']}/{row['completed_count']}", str(row['stale_pr_count']),
                              f"{row['missing_open_date_count']}/{row['missing_close_date_count']}")
            collection = value.get('collection') or {}
            latest = value['items'][0]['inventory_captured_at'] if value['items'] else 'not recorded'
            warning = ' Capture failed: '+literal(collection['last_error']) if collection.get('last_error') else ''
            self.query_one('#stats-status', Static).update(
                f"History {value['first_recorded_day'] or 'not recorded'}; last sample {latest}. "
                f"Collector {collection.get('status') or 'unknown'}.{warning} "
                f"{len(value['missing_days'])} missing dates, not zero activity. Rows {len(value['items'])}/{value['total']}. "
                '* Today is provisional. >= marks known lower bounds. Timed/all shows coverage; Missing O/C shows undated PRs.')
        except Exception:
            if self.is_mounted:
                self.query_one('#stats-status', Static).update('Statistics are unavailable. Any previously displayed rows remain. Reload after the application reconnects.')

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'stats-back':
            self.action_close()
        elif event.button.id == 'stats-reload':
            self.action_reload()
