"""Double-click launcher; frozen bundles include Python and all static assets."""
import os
import sys
import webbrowser


def main():
    diagnostic_log = os.environ.get("LJARCHIVE_DIAGNOSTIC_LOG")
    if diagnostic_log:
        import faulthandler
        sys.stdout = sys.stderr = open(diagnostic_log, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=sys.stderr)
        faulthandler.dump_traceback_later(60, file=sys.stderr)
    # Windows --windowed builds do not provide stdout/stderr.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if len(sys.argv) > 1:
        from .__main__ import main as cli
        return cli()
    from .paths import desktop_output
    from .server import serve
    import requests
    try:
        response = requests.get("http://127.0.0.1:8787/", timeout=1)
        if response.headers.get("Server", "").startswith("LJLocalArchive/"):
            webbrowser.open("http://127.0.0.1:8787/")
            return
    except requests.RequestException:
        pass
    try:
        serve(desktop_output(), 8787, True)
    except OSError:
        # Another local service may own the default port; choose a free loopback port.
        serve(desktop_output(), 0, True)
