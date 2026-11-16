"""Double-click launcher; frozen bundles include Python and all static assets."""
import os
import sys
import webbrowser


def main():
    diagnostic_log = os.environ.get("LJARCHIVE_DIAGNOSTIC_LOG")
    if diagnostic_log:
        import faulthandler
        sys.stderr = open(diagnostic_log, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=sys.stderr)
        faulthandler.dump_traceback_later(60, file=sys.stderr)
    # Windows --windowed builds do not provide stdout/stderr.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # A parent process can supply console handles even to a windowed executable.
    # Their legacy Windows encoding must not prevent startup or error reporting.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if len(sys.argv) > 1:
        from .__main__ import main as cli
        return cli()
    from .paths import desktop_output
    try:
        launch(desktop_output())
    except Exception as exc:
        show_startup_error(str(exc))
        raise SystemExit(1) from exc


def launch(output, port=8787, open_browser=True):
    from .launcher import prepare_server
    from .server import run_server
    server, url = prepare_server(output, port)
    if server is None:
        if open_browser:
            webbrowser.open(url)
        return
    run_server(server, open_browser)


def show_startup_error(message):
    """Windowed executables need a visible error, not an invisible stderr traceback."""
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'Архив ЖЖ', 0x10)
        return
    # A local static page also works in frozen macOS/Linux builds without Tk.
    import html
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp(prefix='journal-archive-')) / 'startup.html'
    path.write_text('<!doctype html><html lang="ru"><meta charset="utf-8">'
                    '<title>Архив ЖЖ</title><h1>Не удалось запустить приложение</h1><p>'
                    + html.escape(message) + '</p>', encoding='utf-8')
    webbrowser.open(path.as_uri())
