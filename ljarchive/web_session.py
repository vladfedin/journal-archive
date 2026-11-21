"""Obtain server-issued browser cookies for protected LJ images.

The XML-RPC sessiongenerate value is a master token, not the signed
ScrapBook ljsession cookie. Only a normal web login can issue the latter.
"""
from html.parser import HTMLParser

import requests

AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
AUTH_COOKIES = frozenset(('ljsession', 'ljmastersession', 'ljloggedin'))


def cookie_header(client, url):
    jar = requests.cookies.RequestsCookieJar()
    for cookie in getattr(getattr(client, 'session', None), 'cookies', ()):
        if cookie.name in AUTH_COOKIES:
            jar.set_cookie(cookie)
    return requests.cookies.get_cookie_header(jar, requests.Request('GET', url).prepare()) or ''


def ready(client, url='https://ic.pics.livejournal.com/'):
    values = dict(part.strip().split('=', 1) for part in cookie_header(client, url).split(';') if '=' in part)
    return bool(values.get('ljsession') and values.get('ljloggedin'))


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside, self.token = False, None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form':
            self.inside = values.get('action') in ('/login.bml', 'https://www.livejournal.com/login.bml')
        elif self.inside and tag == 'input' and values.get('name') == 'lj_form_auth':
            self.token = values.get('value')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.inside = False


def establish(client, url='https://ic.pics.livejournal.com/'):
    from .client import Cancelled, ExportError, RequestLimitReached, WebLoginRequired
    if ready(client, url):
        return True
    if client._web_login_attempted or not client._web_password:
        return False
    client.budget.announce_wait(url='https://www.livejournal.com/login.bml')
    client._web_login_attempted = True
    client.progress(phase='login', message='Входим в ЖЖ через веб-форму…')
    headers = {'User-Agent': AGENT, 'Accept': 'text/html', 'Referer': 'https://www.livejournal.com/login.bml'}
    form_data = {}
    try:
        # Never synthesize signed image cookies from an API token or follow a
        # login redirect carrying credentials. Let LJ set its own domain cookies.
        for cookie in list(client.session.cookies):
            if cookie.name in AUTH_COOKIES:
                client.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        page = client._request('GET', '/login.bml', headers=headers)
        form = LoginForm(); form.feed(page.decode('utf-8', 'replace')); form.close()
        if not form.token:
            raise WebLoginRequired()
        form_data = {'user': client.account, 'password': client._web_password,
                     'lj_form_auth': form.token, 'returnto': '/'}
        client._request('POST', '/login.bml', data=form_data,
                        headers={**headers, 'Origin': 'https://www.livejournal.com'},
                        accepted_statuses=(200, 302, 303))
        if not ready(client, url):
            raise WebLoginRequired()
        if client.session_ready:
            client.session_ready(client)
        return True
    except (Cancelled, RequestLimitReached):
        raise
    except ExportError:
        raise WebLoginRequired() from None
    finally:
        form_data.clear()
        client._web_password = ''
