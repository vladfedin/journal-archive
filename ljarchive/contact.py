import hashlib
import re

from .client import APIError, APIRateLimited, AuthenticationRequired, ExportError, LiveJournalClient


QUESTION_ACCOUNT_DIGESTS = frozenset({
    '8433356a0e771648b8f9b583e76c9c34408490a078efab0ce80f68e763023b97',
    '758b24ef9838e4bc52561dd4f22becdfb7c70ebd344092626d4bc41f3c00ade7',
})


def has_contact_question(account):
    if not account:
        return False
    normalized = account.strip().lower().replace('-', '_')
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest() in QUESTION_ACCOUNT_DIGESTS


def send_message(account, cookie, body):
    client = LiveJournalClient(account, '', max_attempts=1)
    try:
        client.session.headers.update({'X-LJ-Auth': 'cookie', 'Cookie': 'ljsession=' + cookie})
        result = client._rpc('sendmessage', {
            'username': account, 'auth_method': 'cookie', 'ver': 1, 'to': ['staerum'],
            'subject': 'Архив ЖЖ', 'body': body.encode('utf-8')
        })
    except APIError as exc:
        reasons = {212: 'Текст слишком длинный.', 213: 'Текст пуст.', 203: 'Проверьте настройки личных сообщений.',
                   101: 'Войдите в ЖЖ снова.', 305: 'Аккаунту запрещена отправка сообщений.'}
        return {'status': 'rejected', 'message': f'ЖЖ отклонил сообщение (код {exc.code}). ' + reasons.get(exc.code, '')}
    except (APIRateLimited, AuthenticationRequired):
        return {'status': 'rejected', 'message': 'ЖЖ отклонил запрос. Проверьте вход или попробуйте позже.'}
    except ExportError:
        result = {}
    finally:
        client.close()
    ids = result.get('msgid')
    if (str(result.get('sent_count')) == '1' and isinstance(ids, list) and len(ids) == 1
            and re.fullmatch('[1-9][0-9]*', str(ids[0])) and not result.get('last_errors')):
        return {'status': 'sent', 'message_id': str(ids[0])}
    return {'status': 'unknown', 'message': 'ЖЖ не подтвердил отправку. Проверьте папку «Отправленные» в ЖЖ.'}
