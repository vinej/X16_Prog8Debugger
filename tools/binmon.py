#!/usr/bin/env python3
"""VICE binary-monitor client for the Box16 fork (vinej/box16,
branch binary-monitor).

Transport and framing adapted from the fork's reference client
box16-src\test\binmon_test.py and X16_BasicDebugger's probes -- this is
the shared foundation the eventual DAP adapter will reuse.
"""

import socket
import struct
import time

STX = 0x02
API = 0x02
EVENT_ID = 0xFFFFFFFF

CMD_MEMORY_GET = 0x01
CMD_MEMORY_SET = 0x02
CMD_CHECKPOINT_SET = 0x12
CMD_CHECKPOINT_DELETE = 0x13
CMD_REGISTERS_GET = 0x31
CMD_ADVANCE = 0x71
CMD_UNTIL_RETURN = 0x73
CMD_PING = 0x81
CMD_EXIT = 0xAA
CMD_RESET = 0xCC
CMD_AUTOSTART = 0xDD

RESP_CHECKPOINT_INFO = 0x11
RESP_REGISTER_INFO = 0x31
RESP_STOPPED = 0x62
RESP_RESUMED = 0x63

REG_PC = 3  # register ids: 0=a 1=x 2=y 3=pc 4=sp 5=fl


class MonitorError(RuntimeError):
    pass


class Monitor:
    def __init__(self, host="127.0.0.1", port=6502, timeout=5):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(0.2)
        self.buffer = b""
        self.next_id = 1
        self.events = []

    def close(self):
        self.sock.close()

    # -- framing ---------------------------------------------------------

    def send(self, cmd, body=b""):
        req_id = self.next_id
        self.next_id += 1
        frame = struct.pack("<BBIIB", STX, API, len(body), req_id, cmd) + body
        self.sock.sendall(frame)
        return req_id

    def _pump(self, deadline):
        while len(self.buffer) < 12:
            if time.monotonic() > deadline:
                raise TimeoutError("timed out waiting for a frame")
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buffer += chunk
        stx, api, body_len = struct.unpack_from("<BBI", self.buffer, 0)
        if stx != STX or api != API:
            raise MonitorError(f"bad frame header {self.buffer[:2].hex()}")
        total = 12 + body_len
        while len(self.buffer) < total:
            if time.monotonic() > deadline:
                raise TimeoutError("timed out inside a frame")
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buffer += chunk
        rtype, err, rid = struct.unpack_from("<BBI", self.buffer, 6)
        body = self.buffer[12:total]
        self.buffer = self.buffer[total:]
        return rtype, err, rid, body

    def recv_response(self, req_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            rtype, err, rid, body = self._pump(deadline)
            if rid == req_id:
                return rtype, err, body
            if rid == EVENT_ID:
                self.events.append((rtype, err, body))

    def wait_event(self, rtype, timeout=30.0):
        deadline = time.monotonic() + timeout
        while True:
            for i, (etype, err, body) in enumerate(self.events):
                if etype == rtype:
                    del self.events[i]
                    return err, body
            etype, err, rid, body = self._pump(deadline)
            if rid == EVENT_ID:
                self.events.append((etype, err, body))

    def command(self, cmd, body=b"", expect=None, timeout=5.0):
        rtype, err, rbody = self.recv_response(self.send(cmd, body), timeout)
        if err != 0:
            raise MonitorError(f"command {cmd:#04x} -> error {err:#04x}")
        if expect is not None and rtype != expect:
            raise MonitorError(f"command {cmd:#04x} -> unexpected response "
                               f"type {rtype:#04x}")
        return rbody

    # -- operations ------------------------------------------------------

    def ping(self):
        self.command(CMD_PING)

    def memory_get(self, start, end, memspace=0, bank=0):
        body = struct.pack("<BHHBH", 0, start, end, memspace, bank)
        rbody = self.command(CMD_MEMORY_GET, body)
        (n,) = struct.unpack_from("<H", rbody, 0)
        return rbody[2:2 + n]

    def registers_get(self):
        rbody = self.command(CMD_REGISTERS_GET, b"\x00")
        return parse_registers(rbody)

    def pc(self):
        return self.registers_get()[REG_PC]

    def checkpoint_set(self, start, end=None, stop=True, enabled=True,
                       op=4, temporary=False, memspace=0, bank=None):
        """op: 1=load 2=store 4=exec. bank (u16) uses the fork's bank-aware
        extension; omit for plain 16-bit checkpoints (low-RAM code)."""
        end = start if end is None else end
        body = struct.pack("<HHBBBBB", start, end, int(stop), int(enabled),
                           op, int(temporary), memspace)
        if bank is not None:
            body += struct.pack("<H", bank)
        rbody = self.command(CMD_CHECKPOINT_SET, body,
                             expect=RESP_CHECKPOINT_INFO)
        (cp_num,) = struct.unpack_from("<I", rbody, 0)
        return cp_num

    def checkpoint_delete(self, cp_num):
        self.command(CMD_CHECKPOINT_DELETE, struct.pack("<I", cp_num))

    def resume(self):
        self.command(CMD_EXIT)

    def wait_stopped(self, timeout=30.0):
        """-> PC from the STOPPED event body."""
        err, body = self.wait_event(RESP_STOPPED, timeout)
        (pc,) = struct.unpack_from("<H", body, 0)
        return pc

    def advance(self, step_over=False, count=1, timeout=10.0):
        """Step instructions; -> PC after the step completes."""
        self.command(CMD_ADVANCE, struct.pack("<BH", int(step_over), count))
        self.wait_event(RESP_RESUMED, timeout)
        return self.wait_stopped(timeout)

    def until_return(self, timeout=10.0):
        self.command(CMD_UNTIL_RETURN)
        self.wait_event(RESP_RESUMED, timeout)
        return self.wait_stopped(timeout)

    def reset_paused(self):
        """RESET leaves the machine paused at the reset vector (fork
        behavior VS64 relies on); arm checkpoints before resuming."""
        self.command(CMD_RESET, b"\x00")

    def autostart(self, prg_path):
        name = str(prg_path).encode()
        self.command(CMD_AUTOSTART, bytes([1, 0, 0, len(name)]) + name)


def parse_registers(body):
    (count,) = struct.unpack_from("<H", body, 0)
    pos, regs = 2, {}
    for _ in range(count):
        size = body[pos]
        reg_id = body[pos + 1]
        (value,) = struct.unpack_from("<H", body, pos + 2)
        regs[reg_id] = value
        pos += 1 + size
    return regs
