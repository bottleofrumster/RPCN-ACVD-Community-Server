import re

import pytest

from client.configure_endpoint import configure


def template():
    rows = [f"- [ be64, 0x{0x01A8DE58 + offset:08x}, 0x0000000000000000 ]"
            for offset in range(0, 32, 8)]
    return "\n".join(rows + [
        '- [ cutf8, 0x01a8e2b0, "http://192.0.2.1:8080/regulation/contents_101.bin" ]',
        '- [ utf8, 0x01b6b130, "PUBLIC KEY UNCHANGED" ]',
    ])


@pytest.mark.parametrize("host", ["127.0.0.1", "203.0.113.12", "255.255.255.255"])
def test_login_and_cdn_addresses_agree_and_key_is_preserved(host):
    result = configure(template(), host, 9090)
    words = re.findall(r"be64, 0x[0-9a-f]+, 0x([0-9a-f]{16})", result)
    decoded = b"".join(bytes.fromhex(word) for word in words).decode("utf-16-be")
    assert decoded.rstrip("\0") == host
    assert f'http://{host}:9090/regulation/contents_101.bin' in result
    assert '"PUBLIC KEY UNCHANGED"' in result
    assert configure(result, host, 9090) == result


@pytest.mark.parametrize("host,port", [("bad", 8080), ("::1", 8080),
                                       ("192.0.2.1", 0), ("192.0.2.1", 65536)])
def test_invalid_endpoint_is_rejected(host, port):
    with pytest.raises(ValueError):
        configure(template(), host, port)


def test_missing_or_duplicate_patch_blocks_are_rejected():
    for text in ("", template() + "\n" + template()):
        with pytest.raises(ValueError):
            configure(text, "192.0.2.1")
