"""Terminal-to-terminal chat. A small WebSocket relay with rooms and direct
messages, history saved to disk.

How it connects:
  * LIVENEWS_CHAT_URL=ws://host:8765  join someone else's relay (LAN, VPN, tunnel)
  * otherwise the first terminal on this machine starts a relay on
    127.0.0.1:8765 and the rest join it
  * LIVENEWS_CHAT_SERVE=0.0.0.0:8765 hosts for other machines. Anything not on
    localhost requires LIVENEWS_CHAT_TOKEN, and clients must send the same token.
  * python -m livenews.chat runs a relay on its own (for a small server)

Messages: "#room" rooms and "@name" direct messages. $TICKER in a message is
highlighted and selectable.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
except ImportError:  # pragma: no cover
    from websockets import connect, serve  # type: ignore

DEFAULT_PORT = 8765
TICKER_RX = re.compile(r"\$([A-Za-z][A-Za-z.]{0,5})\b")
NAME_RX = re.compile(r"^[A-Za-z0-9_.-]{1,24}$")
ROOM_RX = re.compile(r"^#[A-Za-z0-9_-]{1,24}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dm_room(a: str, b: str) -> str:
    x, y = sorted([a.lower(), b.lower()])
    return f"@{x}+{y}"


class Relay:
    """The server side. Keeps the last 500 messages per room in memory and
    appends everything to a JSONL file so history survives restarts."""

    def __init__(self, history: Path | None = None, token: str = "", max_per_room: int = 500):
        self.history_path = history
        self.token = token
        self.rooms: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_per_room))
        self.clients: dict[object, str] = {}
        self.joined: dict[object, set[str]] = defaultdict(set)
        if history and history.exists():
            for line in history.read_text().splitlines()[-20000:]:
                try:
                    m = json.loads(line)
                    self.rooms[m["room"]].append(m)
                except (ValueError, KeyError):
                    continue

    def users(self) -> list[str]:
        return sorted(set(self.clients.values()))

    async def _send(self, ws, obj) -> None:
        try:
            await ws.send(json.dumps(obj))
        except Exception:
            pass

    async def _broadcast_presence(self) -> None:
        msg = {"type": "presence", "users": self.users()}
        await asyncio.gather(*(self._send(ws, msg) for ws in list(self.clients)))

    def _visible(self, room: str, user: str) -> bool:
        if room.startswith("@"):
            return user.lower() in room[1:].split("+")
        return True

    async def handler(self, ws) -> None:
        user = None
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if hello.get("type") != "hello" or not NAME_RX.match(str(hello.get("user", ""))):
                await self._send(ws, {"type": "error", "text": "bad hello"})
                return
            if self.token and hello.get("token") != self.token:
                await self._send(ws, {"type": "error", "text": "wrong or missing LIVENEWS_CHAT_TOKEN"})
                return
            user = hello["user"]
            self.clients[ws] = user
            await self._send(ws, {"type": "welcome", "user": user, "rooms": sorted(r for r in self.rooms if self._visible(r, user))})
            await self._join(ws, user, "#general")
            await self._broadcast_presence()
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                t = m.get("type")
                if t == "join" and ROOM_RX.match(str(m.get("room", ""))):
                    await self._join(ws, user, m["room"])
                elif t == "msg":
                    await self._message(ws, user, m)
        except Exception:
            pass
        finally:
            if ws in self.clients:
                del self.clients[ws]
                self.joined.pop(ws, None)
                await self._broadcast_presence()

    async def _join(self, ws, user, room) -> None:
        self.joined[ws].add(room)
        hist = [m for m in self.rooms.get(room, ()) if self._visible(room, user)][-200:]
        await self._send(ws, {"type": "history", "room": room, "msgs": hist})

    async def _message(self, ws, user, m) -> None:
        text = str(m.get("text", ""))[:2000].strip()
        to = str(m.get("to", "#general"))
        if not text:
            return
        if to.startswith("@"):
            peer = to[1:]
            if not NAME_RX.match(peer):
                return
            room = dm_room(user, peer)
        elif ROOM_RX.match(to):
            room = to
        else:
            return
        msg = {"type": "msg", "room": room, "from": user, "text": text, "ts": now_iso()}
        self.rooms[room].append(msg)
        if self.history_path:
            with self.history_path.open("a") as f:
                f.write(json.dumps(msg) + "\n")
        targets = []
        for c, u in list(self.clients.items()):
            if room.startswith("@"):
                if u.lower() in room[1:].split("+"):
                    targets.append(c)
            elif room in self.joined.get(c, ()):
                targets.append(c)
        await asyncio.gather(*(self._send(c, msg) for c in targets))

    async def serve_forever(self, host: str, port: int, ready: asyncio.Event | None = None, bound: list | None = None):
        async with serve(self.handler, host, port) as server:
            if bound is not None:
                bound.append(server.sockets[0].getsockname()[1])
            if ready:
                ready.set()
            await asyncio.Future()


@dataclass
class ChatState:
    """Client side state the UI reads."""

    user: str
    room: str = "#general"
    rooms: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=500)))
    users: list[str] = field(default_factory=list)
    unread: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    status: str = "connecting"
    url: str = ""
    hosting: bool = False


class ChatClient:
    def __init__(self, user: str | None = None, url: str | None = None, serve_at: str | None = None,
                 token: str | None = None, history: Path | None = None, on_message=None):
        self.state = ChatState(user=user or os.getenv("LIVENEWS_CHAT_NAME") or _default_name())
        self.url = url if url is not None else os.getenv("LIVENEWS_CHAT_URL", "")
        self.serve_at = serve_at if serve_at is not None else os.getenv("LIVENEWS_CHAT_SERVE", "")
        self.token = token if token is not None else os.getenv("LIVENEWS_CHAT_TOKEN", "")
        self.history = history
        self.on_message = on_message
        self.ws = None
        self._relay_task = None
        self.viewing = False  # UI sets this while the MSG screen is open

    async def _start_relay(self, host: str, port: int) -> int:
        if host not in ("127.0.0.1", "localhost", "::1") and not self.token:
            raise RuntimeError("hosting chat beyond localhost needs LIVENEWS_CHAT_TOKEN")
        relay = Relay(self.history, self.token)
        ready, bound = asyncio.Event(), []
        self._relay_task = asyncio.create_task(relay.serve_forever(host, port, ready, bound))
        await asyncio.wait_for(ready.wait(), 5)
        self.state.hosting = True
        return bound[0]

    async def _connect_once(self, url: str) -> None:
        async with connect(url, open_timeout=5, ping_interval=20) as ws:
            await ws.send(json.dumps({"type": "hello", "user": self.state.user, "token": self.token}))
            self.ws = ws
            self.state.url = url
            self.state.status = "connected"
            if self.state.room != "#general" and not self.state.room.startswith("@"):
                await ws.send(json.dumps({"type": "join", "room": self.state.room}))
            async for raw in ws:
                self.handle(json.loads(raw))

    def handle(self, m: dict) -> None:
        t = m.get("type")
        st = self.state
        if t == "welcome":
            st.user = m.get("user", st.user)
        elif t == "presence":
            st.users = m.get("users", [])
        elif t == "history":
            room = m["room"]
            st.rooms[room].clear()
            st.rooms[room].extend(m.get("msgs", []))
        elif t == "msg":
            room = m["room"]
            st.rooms[room].append(m)
            if m.get("from") != st.user and (room != st.room or not self.viewing):
                st.unread[room] += 1
            if self.on_message and m.get("from") != st.user:
                self.on_message(m)
        elif t == "error":
            st.status = "error: " + m.get("text", "")

    async def run(self) -> None:
        delay = 1.0
        while True:
            url = self.url
            try:
                if not url:
                    if self.serve_at:
                        host, _, port = self.serve_at.rpartition(":")
                        p = await self._start_relay(host or "0.0.0.0", int(port))
                        url = f"ws://127.0.0.1:{p}"
                    else:
                        url = f"ws://127.0.0.1:{DEFAULT_PORT}"
                        try:
                            await self._connect_once(url)
                            continue
                        except OSError:
                            # nobody hosting on this machine yet, so we do
                            p = await self._start_relay("127.0.0.1", DEFAULT_PORT)
                            url = f"ws://127.0.0.1:{p}"
                    self.url = url
                await self._connect_once(url)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state.status = f"offline ({str(e)[:60]})"
            self.ws = None
            if not self.state.status.startswith("error"):
                self.state.status = "reconnecting"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def send(self, text: str, to: str | None = None) -> None:
        if self.ws is None:
            raise RuntimeError(f"chat is {self.state.status}")
        target = to or self.state.room
        if target.startswith("@") and "+" in target:  # a DM room key, send to the other person
            names = target[1:].split("+")
            other = next((n for n in names if n != self.state.user.lower()), names[0])
            target = "@" + other
        await self.ws.send(json.dumps({"type": "msg", "to": target, "text": text}))

    async def switch(self, room: str) -> None:
        if room.startswith("@") and "+" not in room:
            room = dm_room(self.state.user, room[1:])
        self.state.room = room
        self.state.unread[room] = 0
        if self.ws is not None and room.startswith("#"):
            await self.ws.send(json.dumps({"type": "join", "room": room}))

    async def aclose(self) -> None:
        if self._relay_task:
            self._relay_task.cancel()


def _default_name() -> str:
    try:
        n = getpass.getuser()
    except Exception:
        n = "trader"
    n = re.sub(r"[^A-Za-z0-9_.-]", "", n)[:24]
    return n or "trader"


def tickers_in(text: str) -> list[str]:
    return [t.upper() for t in TICKER_RX.findall(text)]


def main() -> None:
    import argparse

    from .userdata import home

    ap = argparse.ArgumentParser(prog="python -m livenews.chat", description="run a chat relay")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    token = os.getenv("LIVENEWS_CHAT_TOKEN", "")
    if a.host not in ("127.0.0.1", "localhost") and not token:
        raise SystemExit("set LIVENEWS_CHAT_TOKEN before hosting beyond localhost")
    print(f"chat relay on ws://{a.host}:{a.port}")
    asyncio.run(Relay(home() / "chat.jsonl", token).serve_forever(a.host, a.port))


if __name__ == "__main__":
    main()
