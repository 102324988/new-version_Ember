"""Try distinct DNS addresses only when TCP/TLS fails before HTTP is sent."""
import socket
import threading
import time
import httpx


class ConnectFailoverTransport(httpx.BaseTransport):
    def __init__(self):
        self.direct = httpx.HTTPTransport(retries=0)
        self._routes, self._preferred = {}, {}
        self._lock = threading.RLock()

    def _route(self, origin):
        # Separate pools per original TLS origin, even when domains share an IP.
        # Otherwise a pooled connection could inherit another domain's SNI.
        with self._lock:
            if origin not in self._routes:
                self._routes[origin] = httpx.HTTPTransport(retries=0)
            return self._routes[origin]

    def _at_address(self, request, origin, address):
        routed = httpx.Request(request.method, request.url.copy_with(host=address),
            headers=request.headers, stream=request.stream,
            extensions={**request.extensions, 'sni_hostname': request.url.host})
        # The original Host header and certificate hostname remain unchanged.
        return self._route(origin).handle_request(routed)

    def handle_request(self, request):
        if request.url.scheme != 'https':
            return self.direct.handle_request(request)
        origin = (request.url.scheme, request.url.host, request.url.port or 443)
        tried, last_error = set(), None
        with self._lock:
            preferred = self._preferred.get(origin)
        if preferred and time.monotonic() - preferred[1] < 60:
            tried.add(preferred[0])
            try:
                return self._at_address(request, origin, preferred[0])
            except (httpx.ConnectTimeout, httpx.ConnectError) as error:
                last_error = error
        else:
            try:
                return self.direct.handle_request(request)
            except (httpx.ConnectTimeout, httpx.ConnectError) as error:
                last_error = error
        try:
            records = socket.getaddrinfo(origin[1], origin[2], type=socket.SOCK_STREAM)
        except OSError:
            raise last_error
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        for address in addresses:
            if address in tried:
                continue
            tried.add(address)
            if len(tried) > 4:
                break
            try:
                response = self._at_address(request, origin, address)
            except (httpx.ConnectTimeout, httpx.ConnectError) as error:
                last_error = error
                continue
            with self._lock:
                self._preferred[origin] = (address, time.monotonic())
            return response
        raise last_error

    def close(self):
        with self._lock:
            for transport in [self.direct, *self._routes.values()]:
                transport.close()
            self._routes.clear()
            self._preferred.clear()
