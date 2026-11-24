"""Direct HTTP for the locally owned model server and synthetic platform."""
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import (HTTPDefaultErrorHandler, HTTPErrorProcessor, HTTPHandler,
                            OpenerDirector, UnknownHandler)


class _LoopbackHTTPHandler(HTTPHandler):
    def http_open(self, request):
        target = urlsplit(request.full_url)
        if target.hostname != '127.0.0.1' or target.username is not None or request.has_proxy():
            raise URLError('Expected direct HTTP to 127.0.0.1')
        return super().http_open(request)


def loopback_http_opener():
    # Install only the direct HTTP transport. Redirects remain HTTP errors and
    # the only socket destination is the caller's numeric loopback address.
    opener = OpenerDirector()
    for handler in (_LoopbackHTTPHandler(), HTTPDefaultErrorHandler(), HTTPErrorProcessor(), UnknownHandler()):
        opener.add_handler(handler)
    return opener
