"""HTTP framing for the regulation download; the listener owns I/O and file selection."""

_STATUS_REASONS = {
    200: "OK",
    404: "Not Found",
    405: "Method Not Allowed",
    503: "Service Unavailable",
}


def encode_response(method: str, status: int, body: bytes) -> bytes:
    """HEAD advertises the GET body length but never sends the body."""
    reason = _STATUS_REASONS.get(status, "Bad Request")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/octet-stream\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    return headers + (b"" if method == "HEAD" else body)
