"""Comment observations, distinct from missing responses and retained content."""
from datetime import datetime, timezone

DELETED = frozenset(('D', 'X'))


def merge_comment(previous, incoming, *, evidence='getcomments', observed_at=None):
    previous = previous or {}
    current = dict(incoming)
    if current.get('state') in DELETED:
        # A tombstone often has empty author/date/subject fields. Keep what was
        # actually archived, including the selected userpic and earlier text.
        current = {**previous, **incoming}
        for field in ('posterid', 'date', 'subject', 'props', 'userpic', 'poster_userpic_url', 'datepostunix'):
            if not incoming.get(field) and field in previous:
                current[field] = previous[field]
        if 'body' in previous:
            current['body'] = previous['body']
        current.pop('source_status', None)
        current['deletion'] = previous.get('deletion') or {
            'observed_at': observed_at or datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'after_archive': previous.get('state') not in DELETED and 'body' in previous,
            'evidence': evidence + ':' + incoming['state'],
        }
    elif previous.get('state') in DELETED and previous.get('deletion') and not current.get('body'):
        # XML-RPC sometimes keeps returning an approved, unloaded stub after
        # the web page has explicitly marked this very comment as deleted.
        return dict(previous)
    elif 'body' not in current:
        current = {**previous, **current, 'source_status': 'body_not_returned'}
    return current


def comment_view(item, settled=False):
    """One presentation policy for offline pages and the live preview.

    settled: the discussion was requested several times; a missing text is final, not pending.
    """
    deleted = item.get('state') in DELETED
    later = deleted and item.get('deletion', {}).get('after_archive', False)
    label = ('Удалён впоследствии' if later else 'Удалён') if deleted else {
        'S': 'Скрыт', 'F': 'Заморожен', 'B': 'Спам', 'A': ''}.get(item.get('state'), '')
    retained = isinstance(item.get('body'), str)
    return {'state_label': label, 'deleted': deleted,
            'retained_text': retained and (deleted or bool(item.get('source_status'))),
            'display_body': item.get('body', 'Комментарий удалён.' if deleted else
                                     'ЖЖ не отдал текст этого комментария.' if settled else 'Текст пока не получен.')}
