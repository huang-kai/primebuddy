"""模型调用错误类型；可由各 provider 复用。"""
import errno
import http.client
import socket
import ssl
import urllib.error


class AsrError(Exception):
    pass


class TtsError(Exception):
    pass


class ProviderNetworkError(Exception):
    """请求因网络传输失败；不包含 HTTP 权限、配额或服务端错误。"""


class AsrNetworkError(AsrError, ProviderNetworkError):
    pass


class TtsNetworkError(TtsError, ProviderNetworkError):
    pass


def is_network_error(error):
    # HTTPError 继承 URLError，但收到 HTTP 响应不能据此判断用户断网。
    if isinstance(error, urllib.error.HTTPError):
        return False
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (TimeoutError, ConnectionError, socket.gaierror, socket.herror,
                           http.client.IncompleteRead, ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return True
    return isinstance(reason, OSError) and reason.errno in {
        errno.ENETDOWN, errno.ENETUNREACH, errno.ENETRESET, errno.ECONNABORTED,
        errno.ECONNRESET, errno.ECONNREFUSED, errno.EHOSTDOWN, errno.EHOSTUNREACH,
        errno.ETIMEDOUT, errno.EPIPE,
    }
