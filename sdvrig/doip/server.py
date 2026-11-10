"""Serve the simulated vehicle on a real DoIP port.

The gateway listens on TCP for one tester connection and answers vehicle
identification requests on UDP, both on port 13400 by default. The simulation
runs in real time (or `speed` times faster): every tick, the clock advances by the
wall time that passed, bytes from the socket go into the gateway, and bytes the
gateway produced go back out. Any DoIP tester can then drive the vehicle; the test
suite uses scapy's DoIP socket as an independent client.
"""

from __future__ import annotations

import selectors
import socket
import threading
import time

from sdvrig import doip
from sdvrig.doip import decode
from sdvrig.vehicle import Vehicle


class DoIPServer:
    def __init__(self, vehicle: Vehicle, host: str = "127.0.0.1", port: int = 13400,
                 speed: float = 1.0, tick_ms: float = 2.0, udp: bool = True):
        self.v = vehicle
        self.host = host
        self.speed = speed
        self.tick_s = tick_ms / 1000
        self._outgoing = bytearray()
        vehicle.link.on_a_receive(self._outgoing.extend)   # the socket replaces the built-in tester
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((host, port))
        self._tcp.listen(1)
        self._tcp.setblocking(False)
        self.port = self._tcp.getsockname()[1]
        self._udp = None
        if udp:
            self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp.bind((host, self.port))
            self._udp.setblocking(False)
        self._client: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self.bytes_in = 0
        self.bytes_out = 0

    # ---------------------------------------------------------------- control
    def start(self) -> "DoIPServer":
        self._running = True
        self._thread = threading.Thread(target=self.serve_forever, name="doip-server", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        for s in (self._client, self._tcp, self._udp):
            if s:
                s.close()

    def __enter__(self) -> "DoIPServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------- loop
    def serve_forever(self) -> None:
        self._running = True
        sel = selectors.DefaultSelector()
        sel.register(self._tcp, selectors.EVENT_READ, "accept")
        if self._udp:
            sel.register(self._udp, selectors.EVENT_READ, "udp")
        last = time.monotonic()
        while self._running:
            for key, _ in sel.select(timeout=self.tick_s):
                if key.data == "accept":
                    self._accept(sel)
                elif key.data == "udp":
                    self._on_udp()
                elif key.data == "client":
                    self._on_client(sel)
            now = time.monotonic()
            self.v.sim.run_for(int((now - last) * 1_000_000 * self.speed))
            last = now
            self._flush()
        sel.close()

    def _accept(self, sel: selectors.BaseSelector) -> None:
        conn, _ = self._tcp.accept()
        if self._client is not None:
            conn.close()     # one tester at a time on this edge node
            return
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._client = conn
        self.v.gateway.activated_source = None
        sel.register(conn, selectors.EVENT_READ, "client")

    def _on_client(self, sel: selectors.BaseSelector) -> None:
        assert self._client is not None
        try:
            data = self._client.recv(65536)
        except BlockingIOError:
            return
        except ConnectionResetError:
            data = b""
        if not data:
            sel.unregister(self._client)
            self._client.close()
            self._client = None
            self.v.gateway.activated_source = None
            return
        self.bytes_in += len(data)
        self.v.link.a_send(data)

    def _on_udp(self) -> None:
        assert self._udp is not None
        try:
            data, addr = self._udp.recvfrom(4096)
        except BlockingIOError:
            return
        try:
            message, _ = decode(data)
        except doip.DoIPError:
            return
        if message and message.payload_type == doip.PT_VEHICLE_ID_REQ:
            self._udp.sendto(self.v.gateway.vehicle_announcement().encode(), addr)

    def _flush(self) -> None:
        if self._client is None:
            self._outgoing.clear()    # nobody is connected to receive it
            return
        if not self._outgoing:
            return
        try:
            sent = self._client.send(bytes(self._outgoing))
        except (BlockingIOError, BrokenPipeError, ConnectionResetError):
            return
        self.bytes_out += sent
        del self._outgoing[:sent]
