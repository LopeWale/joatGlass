import pytest

from joat.agents.a2a_stream import iter_sse_data, parse_sse_payload
from joat.core.a2a import A2AError


async def chunks(*values):
    for value in values:
        yield value


async def test_data_lines_are_one_event_even_when_first_line_is_valid_json():
    events = [event async for event in iter_sse_data(chunks(b'data: {"ok":true}\n', b'data: trailing\n\n'))]
    assert events == ['{"ok":true}\ntrailing']
    with pytest.raises(A2AError):
        parse_sse_payload(events[0])


@pytest.mark.parametrize('ending', [b'\n', b'\r', b'\r\n'])
async def test_fragmented_utf8_crlf_and_comments_preserve_data(ending):
    stream = ending.join([b': keepalive', b'', 'data: {"text":"café  "}'.encode(), b'', b': done', b'', b''])
    events = [event async for event in iter_sse_data(chunks(*(stream[i:i+1] for i in range(len(stream)))))]
    assert events == ['{"text":"café  "}']


async def test_network_coalescing_does_not_apply_event_limit_to_whole_chunk():
    stream = b'data: {}\n\n' * 100
    assert [event async for event in iter_sse_data(chunks(stream), max_bytes=16)] == ['{}'] * 100


@pytest.mark.parametrize('stream', [b'data: {}', b'data: {}\n', b'data: \xff\n\n', b'data: 12345678901234567\n\n'])
async def test_truncated_invalid_or_oversized_event_is_rejected(stream):
    with pytest.raises(A2AError):
        _ = [event async for event in iter_sse_data(chunks(stream), max_bytes=16)]


async def test_multiline_event_byte_bound_includes_unicode_and_joining_newlines():
    stream = ('data: éé\n' * 4 + '\n').encode()
    with pytest.raises(A2AError):
        _ = [event async for event in iter_sse_data(chunks(stream), max_bytes=16)]
