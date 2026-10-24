"""Real local WebSocket test. Requires websockets installed."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from relay import Relay

async def main():
    relay = Relay("s"*32, "r"*32)
    async with serve(relay.handler, "127.0.0.1", 0, compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as sender, connect(f"ws://127.0.0.1:{port}") as receiver:
            await sender.send(json.dumps({"role":"sender","key":"s"*32}))
            await sender.recv()
            await receiver.send(json.dumps({"role":"receiver","key":"r"*32}))
            await receiver.recv()
            await sender.recv()
            await receiver.send(json.dumps({"type":"enabled","value":True}))
            assert json.loads(await sender.recv())["enabled"]
            await receiver.recv()
            audio = b"\0" + b"\0"*1920
            await sender.send(audio)
            assert await asyncio.wait_for(receiver.recv(), 2) == audio
            for data in [{"type":"say","text":"Test announcement"}, {"type":"stop"}]:
                await sender.send(json.dumps(data))
                assert json.loads(await asyncio.wait_for(receiver.recv(), 2)) == data
    print("PASS: real WebSocket audio, announcement and stop relay")

asyncio.run(main())
