"""Bound the bytes decoded and retained from provider responses."""
import zlib

from providers.base import ProviderConnectionError

MAX_RESPONSE_BYTES = 5_000_000
MAX_STREAM_BYTES = 20_000_000
MAX_SSE_LINE_BYTES = 65_536
DECODE_CHUNK_BYTES = 16_384


class BoundedDecoder:
    def __init__(self, encoding=None, limit=MAX_STREAM_BYTES):
        encoding = (encoding or "identity").strip().lower()
        if encoding in ("identity", ""):
            self.decoder = None
        elif encoding in ("gzip", "deflate"):
            self.decoder = zlib.decompressobj(
                16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
            )
        else:
            raise ProviderConnectionError("Unsupported provider content encoding.")
        self.limit = limit
        self.total = 0

    def feed(self, chunk):
        if self.decoder is None:
            for offset in range(0, len(chunk), DECODE_CHUNK_BYTES):
                yield self._count(chunk[offset:offset + DECODE_CHUNK_BYTES])
            return
        try:
            while chunk:
                decoded = self.decoder.decompress(chunk, DECODE_CHUNK_BYTES)
                chunk = self.decoder.unconsumed_tail
                if decoded:
                    yield self._count(decoded)
                if self.decoder.unused_data:
                    raise ProviderConnectionError("Unexpected trailing compressed data.")
        except zlib.error as error:
            raise ProviderConnectionError("Invalid provider compression.") from error

    def _count(self, chunk):
        self.total += len(chunk)
        if self.total > self.limit:
            raise ProviderConnectionError("Provider response exceeded the byte limit.")
        return chunk

    def finish(self):
        if self.decoder is not None and not self.decoder.eof:
            raise ProviderConnectionError("Incomplete compressed provider response.")


async def decoded_chunks(response, limit):
    # Some mock transports supply an already-read body; real calls use stream=True.
    if response.is_stream_consumed:
        if len(response.content) > limit:
            raise ProviderConnectionError("Provider response exceeded the byte limit.")
        for offset in range(0, len(response.content), DECODE_CHUNK_BYTES):
            yield response.content[offset:offset + DECODE_CHUNK_BYTES]
        return
    decoder = BoundedDecoder(response.headers.get("content-encoding"), limit)
    async for raw in response.aiter_raw():
        for chunk in decoder.feed(raw):
            yield chunk
    decoder.finish()


async def read_bounded_response(response, limit=MAX_RESPONSE_BYTES):
    body = bytearray()
    async for chunk in decoded_chunks(response, limit):
        body.extend(chunk)
    return bytes(body)


class BoundedLines:
    def __init__(self):
        self.buffer = b""
        self.skip_lf = False

    def feed(self, chunk):
        offset = 0
        while offset < len(chunk):
            if self.skip_lf:
                self.skip_lf = False
                if chunk[offset:offset + 1] == b"\n":
                    offset += 1
                    continue
            cr = chunk.find(b"\r", offset)
            lf = chunk.find(b"\n", offset)
            boundaries = [position for position in (cr, lf) if position >= 0]
            end = min(boundaries) if boundaries else len(chunk)
            if len(self.buffer) + end - offset > MAX_SSE_LINE_BYTES:
                raise ProviderConnectionError("Provider SSE line exceeded the byte limit.")
            self.buffer += chunk[offset:end]
            if end == len(chunk):
                return
            line, self.buffer = self.buffer, b""
            self.skip_lf = chunk[end:end + 1] == b"\r"
            offset = end + 1
            yield line


async def bounded_sse_lines(response):
    lines = BoundedLines()
    try:
        async for chunk in decoded_chunks(response, MAX_STREAM_BYTES):
            for line in lines.feed(chunk):
                yield line.decode("utf-8")
        if lines.buffer:
            yield lines.buffer.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProviderConnectionError("Invalid provider SSE encoding.") from error
