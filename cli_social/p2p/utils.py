# I thought for reusability , I should put those functions here and import them in other files

from __future__ import annotations
import asyncio
import struct

MAX_FRAME_SIZE = 64 * 1024

async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    length = struct.unpack(">I", header)[0]
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"frame is large: {length} bytes")
    return await reader.readexactly(length)

async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()