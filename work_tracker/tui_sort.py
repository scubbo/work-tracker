"""Client-side ordering of a complete cached PR view."""

from datetime import datetime, timezone

import work_activity as activity
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select


SORT_FIELDS = [('PR URL', 'pr'), ('Review priority', 'review'), ('Title', 'title'),
               ('Time since state change', 'state-age'), ('Opened date', 'opened')]
COLUMN_LABELS = {'review': 'Review', 'pr': 'PR', 'title': 'Title', 'state-age': 'Since change', 'opened': 'Opened'}


def ordered_rows(rows, field, descending=False, review_states=(), now=None):
    now = now or datetime.now(timezone.utc)
    def key(row):
        if field == 'review':
            state = row.get('review_state')
            return review_states.index(state) if state in review_states else None
        if field == 'title':
            return (row.get('github', {}).get('title') or row.get('task_title') or row['url']).casefold()
        if field in ('opened', 'state-age'):
            value = (row.get('github') or {}).get('createdAt') if field == 'opened' else (
                None if row.get('refresh_error') else row.get('state_changed_at') or row.get('state_first_seen_at'))
            try:
                timestamp = activity.parse_time(value)
                if field == 'opened':
                    return timestamp
                elapsed = (now - timestamp).total_seconds()
                return elapsed if elapsed >= 0 else None
            except (TypeError, ValueError, AttributeError, OverflowError):
                return None
        return row['url']
    stable = sorted(rows, key=lambda row: row['url'])
    known, unknown = [], []
    for row in stable:
        value = key(row)
        (unknown if value is None else known).append(row if value is None else (value, row))
    return [row for value, row in sorted(known, key=lambda item: item[0], reverse=descending)] + unknown


class SortScreen(ModalScreen[tuple | None]):
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]
    CSS = """
    SortScreen { align: center middle; background: $background 75%; }
    SortScreen > Vertical { width: 58; max-width: 96%; height: auto; padding: 1 2;
        border: round $accent; background: $surface; }
    SortScreen Label { height: auto; margin-bottom: 1; }
    SortScreen Select { margin-bottom: 1; }
    SortScreen Horizontal { height: 3; }
    SortScreen Button { margin-right: 1; }
    """

    def __init__(self, field, descending):
        super().__init__()
        self.field = field
        self.descending = descending

    def compose(self):
        with Vertical():
            yield Label('Sort PRs. Unknown dates stay last in both directions.')
            yield Select(SORT_FIELDS, value=self.field, allow_blank=False, id='sort-field')
            yield Select([('Ascending ↑', False), ('Descending ↓', True)], value=self.descending,
                         allow_blank=False, id='sort-direction')
            with Horizontal():
                yield Button('Apply', id='sort-apply', variant='primary')
                yield Button('Cancel', id='sort-cancel')

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'sort-apply':
            self.dismiss((self.query_one('#sort-field', Select).value,
                          self.query_one('#sort-direction', Select).value))
        else:
            self.action_cancel()
