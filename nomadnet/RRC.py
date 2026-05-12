import os
import re
import time
import threading
import hashlib
from collections import deque

import RNS

from nomadnet.vendor import cbor


_MENTION_RE_CACHE = {}


def _mention_re(nick):
    if not isinstance(nick, str) or not nick:
        return None
    pat = _MENTION_RE_CACHE.get(nick)
    if pat is None:
        pat = re.compile(r"(?<![A-Za-z0-9_])@"+re.escape(nick)+r"(?![A-Za-z0-9_])", re.IGNORECASE)
        if len(_MENTION_RE_CACHE) > 32:
            _MENTION_RE_CACHE.clear()
        _MENTION_RE_CACHE[nick] = pat
    return pat

# https://github.com/kc1awv/rrcd/blob/main/rrcd/constants.py
RRC_VERSION = 1

K_V    = 0
K_T    = 1
K_ID   = 2
K_TS   = 3
K_SRC  = 4
K_ROOM = 5
K_BODY = 6
K_NICK = 7

T_HELLO   = 1
T_WELCOME = 2

T_JOIN   = 10
T_JOINED = 11
T_PART   = 12
T_PARTED = 13

T_MSG    = 20
T_NOTICE = 21

T_PING = 30
T_PONG = 31

T_ERROR = 40

T_RESOURCE_ENVELOPE = 50

B_HELLO_NAME = 0
B_HELLO_VER  = 1
B_HELLO_CAPS = 2

B_WELCOME_HUB    = 0
B_WELCOME_VER    = 1
B_WELCOME_CAPS   = 2
B_WELCOME_LIMITS = 3

L_MAX_NICK_BYTES            = 0
L_MAX_ROOM_NAME_BYTES       = 1
L_MAX_MSG_BODY_BYTES        = 2
L_MAX_ROOMS_PER_SESSION     = 3
L_RATE_LIMIT_MSGS_PER_MINUTE= 4

CAP_RESOURCE_ENVELOPE = 0

B_RES_ID       = 0
B_RES_KIND     = 1
B_RES_SIZE     = 2
B_RES_SHA256   = 3
B_RES_ENCODING = 4

RES_KIND_NOTICE = "notice"
RES_KIND_MOTD   = "motd"
RES_KIND_BLOB   = "blob"

DEFAULT_DEST_NAME       = "rrc.hub"
DEFAULT_MAX_NICK_BYTES  = 32
DEFAULT_MAX_ROOM_BYTES  = 64
DEFAULT_MAX_MSG_BYTES   = 350
DEFAULT_MAX_ROOMS       = 32
DEFAULT_RATE_PER_MINUTE = 240






def _now_ms():
    return int(time.time()*1000)


def _msg_id():
    return os.urandom(8)


def _parse_room_list_notice(text):
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped == "No public rooms registered":
        return {}
    lines = text.split("\n")
    if not lines or not lines[0].lstrip().startswith("Registered public rooms"):
        return None
    rooms = {}
    for line in lines[1:]:
        s = line.strip()
        if not s:
            continue
        if " - " in s:
            name, topic = s.split(" - ", 1)
            rooms[name.strip().lower()] = topic.strip() or None
        else:
            rooms[s.strip().lstrip("#").lower()] = None
    return rooms


def _make_envelope(msg_type, src, room=None, body=None, nick=None, mid=None, ts=None):
    env = {
        K_V:   RRC_VERSION,
        K_T:   int(msg_type),
        K_ID:  mid or _msg_id(),
        K_TS:  ts or _now_ms(),
        K_SRC: src,
    }
    if room is not None:
        env[K_ROOM] = room
    if body is not None:
        env[K_BODY] = body
    if nick is not None and nick != "":
        env[K_NICK] = nick
    return env


class RRCMessage:
    def __init__(self, kind, room, src, nick, text, ts):
        self.kind = kind
        self.room = room
        self.src  = src
        self.nick = nick
        self.text = text
        self.ts   = ts
        self.mention = False


class RRCHub:
    STATUS_DISCONNECTED = 0
    STATUS_CONNECTING   = 1
    STATUS_CONNECTED    = 2
    STATUS_FAILED       = 3

    def __init__(self, manager, hub_hash, dest_name=None, name=None):
        self.manager   = manager
        self.hub_hash  = hub_hash
        self.dest_name = dest_name or DEFAULT_DEST_NAME
        self.name      = name or RNS.prettyhexrep(hub_hash)

        self.link        = None
        self.status      = RRCHub.STATUS_DISCONNECTED
        self.status_text = "Disconnected"
        self.welcomed    = False
        self.hub_name    = None
        self.hub_version = None

        self.max_nick_bytes        = DEFAULT_MAX_NICK_BYTES
        self.max_room_name_bytes   = DEFAULT_MAX_ROOM_BYTES
        self.max_msg_body_bytes    = DEFAULT_MAX_MSG_BYTES
        self.max_rooms_per_session = DEFAULT_MAX_ROOMS
        self.rate_limit_msgs_per_minute = DEFAULT_RATE_PER_MINUTE

        self.rooms = set()
        self.messages = {}
        self.notices = []
        self.unread_rooms = set()
        self.mention_rooms = set()
        self.members = {}
        self.nicks = {}

        self.auto_reconnect = False
        self.auto_list = False

        self._lock = threading.RLock()
        self._resource_expectations = {}
        self._sent_ids = deque(maxlen=256)

        self._hello_thread = None
        self._stop_hello = threading.Event()
        self._manual_disconnect = False
        self._reconnect_attempts = 0
        self._reconnect_timer = None
        self._pending_pings = {}

        self.available_rooms = {}
        self._silent_list_pending = 0

        self.nick_override = None
        self._pending_joins = set()
        self._pending_parts = set()
        self._silent_joins = set()

    def _log(self, msg, level=None):
        if level is None:
            level = RNS.LOG_INFO
        RNS.log("[RRC "+self.name+"] "+msg, level)

    def add_room(self, room):
        room_n = self._normalize_room(room)
        with self._lock:
            self.rooms.add(room_n)
            if room_n not in self.messages:
                self.messages[room_n] = []
        self.manager.save()
        self.manager._notify_change(self)
        return room_n

    def remove_room(self, room):
        r = self._normalize_room(room)
        with self._lock:
            self.rooms.discard(r)
            self.messages.pop(r, None)
            self.unread_rooms.discard(r)
            self.mention_rooms.discard(r)
            self.members.pop(r, None)
        self.manager.save()
        self.manager._notify_change(self)

    def get_members(self, room):
        with self._lock:
            return list(self.members.get(room, set()))

    def display_name_for(self, peer):
        if not isinstance(peer, (bytes, bytearray)):
            return "<unknown>"
        ph = bytes(peer)
        with self._lock:
            nick = self.nicks.get(ph)
        if nick:
            return nick
        return ph.hex()[:10]

    def mark_read(self, room):
        r = self._normalize_room(room)
        with self._lock:
            self.unread_rooms.discard(r)
            self.mention_rooms.discard(r)
        self.manager._notify_change(self)

    def _normalize_room(self, room):
        r = (room or "").strip().lower()
        if not r:
            raise ValueError("room must not be empty")
        return r

    def _set_status(self, status, text=None):
        self.status = status
        if text is not None:
            self.status_text = text
        self.manager._notify_change(self)

    def connect(self):
        with self._lock:
            if self.status in (RRCHub.STATUS_CONNECTING, RRCHub.STATUS_CONNECTED):
                return
            self._manual_disconnect = False
            if self._reconnect_timer is not None:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
            self._set_status(RRCHub.STATUS_CONNECTING, "Connecting")

        t = threading.Thread(target=self._connect_worker, daemon=True)
        t.start()

    def _connect_worker(self):
        try:
            timeout_s = 20.0
            if not RNS.Transport.has_path(self.hub_hash):
                RNS.Transport.request_path(self.hub_hash)
                deadline = time.monotonic() + min(5.0, timeout_s)
                while time.monotonic() < deadline:
                    if RNS.Transport.has_path(self.hub_hash):
                        break
                    time.sleep(0.1)

            hub_identity = None
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                hub_identity = RNS.Identity.recall(self.hub_hash)
                if hub_identity is not None:
                    break
                time.sleep(0.2)

            if hub_identity is None:
                self._set_status(RRCHub.STATUS_FAILED, "Hub identity unknown")
                return

            app_name, aspects = RNS.Destination.app_and_aspects_from_name(self.dest_name)
            hub_dest = RNS.Destination(
                hub_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                app_name,
                *aspects,
            )

            if hub_dest.hash != self.hub_hash:
                self._set_status(RRCHub.STATUS_FAILED, "Hash/destination name mismatch")
                return

            self._stop_hello.clear()
            link = RNS.Link(hub_dest, established_callback=self._on_established, closed_callback=self._on_closed)
            link.set_packet_callback(lambda data, pkt: self._on_packet(data))
            with self._lock:
                self.link = link

        except Exception as e:
            self._set_status(RRCHub.STATUS_FAILED, "Connect error: "+str(e))

    def _on_established(self, link):
        try:
            link.set_resource_strategy(RNS.Link.ACCEPT_APP)
            link.set_resource_callback(self._resource_advertised)
            link.set_resource_started_callback(self._resource_advertised)
            link.set_resource_concluded_callback(self._resource_concluded)
        except Exception:
            pass

        try:
            link.identify(self.manager.identity)
        except Exception as e:
            self._log("identify failed: "+str(e), RNS.LOG_ERROR)
            try: link.teardown()
            except Exception: pass
            return

        self._set_status(RRCHub.STATUS_CONNECTING, "Identified, sending HELLO")

        def hello_loop():
            attempts = 0
            while not self._stop_hello.is_set() and not self.welcomed and attempts < 5:
                with self._lock:
                    cur_link = self.link
                if cur_link is None or cur_link.status != RNS.Link.ACTIVE:
                    return
                try:
                    self._send_hello(cur_link)
                except Exception as e:
                    self._log("HELLO send failed: "+str(e), RNS.LOG_ERROR)
                attempts += 1
                self._stop_hello.wait(timeout=3.0)
            if not self.welcomed and not self._stop_hello.is_set():
                self._set_status(RRCHub.STATUS_FAILED, "WELCOME timeout")
                try:
                    with self._lock:
                        if self.link is not None:
                            self.link.teardown()
                except Exception:
                    pass

        self._hello_thread = threading.Thread(target=hello_loop, daemon=True)
        self._hello_thread.start()

    def _send_hello(self, link):
        body = {
            B_HELLO_NAME: "nomadnet",
            B_HELLO_VER:  "0.1",
            B_HELLO_CAPS: {CAP_RESOURCE_ENVELOPE: True},
        }
        env = _make_envelope(T_HELLO, src=self.manager.identity.hash, body=body)
        nick = self.get_effective_nick()
        if nick:
            env[K_NICK] = nick
        payload = cbor.encode(env)
        RNS.Packet(link, payload).send()

    def _on_closed(self, link):
        self._stop_hello.set()
        with self._lock:
            self.link = None
            self.welcomed = False
            self.members.clear()
            self._resource_expectations.clear()
            self._pending_joins.clear()
            self._pending_parts.clear()
            self._silent_joins.clear()
            should_reconnect = self.auto_reconnect and not self._manual_disconnect
        self._set_status(RRCHub.STATUS_DISCONNECTED, "Disconnected")
        if should_reconnect:
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        with self._lock:
            self._reconnect_attempts += 1
            backoff = min(60.0, max(1.0, 2.0 ** min(self._reconnect_attempts, 6)))
            if self._reconnect_timer is not None:
                self._reconnect_timer.cancel()

            def fire():
                with self._lock:
                    self._reconnect_timer = None
                    if self._manual_disconnect or not self.auto_reconnect:
                        return
                self._set_status(RRCHub.STATUS_CONNECTING, "Reconnecting (attempt "+str(self._reconnect_attempts)+")")
                self.connect()

            self._reconnect_timer = threading.Timer(backoff, fire)
            self._reconnect_timer.daemon = True
            self._reconnect_timer.start()
            self._set_status(RRCHub.STATUS_DISCONNECTED, "Reconnect in "+str(int(backoff))+"s")

    

    def disconnect(self):
        self._stop_hello.set()
        with self._lock:
            self._manual_disconnect = True
            self._reconnect_attempts = 0
            if self._reconnect_timer is not None:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
            link = self.link
            self.link = None
        if link is not None:
            try: link.teardown()
            except Exception: pass
        self._set_status(RRCHub.STATUS_DISCONNECTED, "Disconnected")

    def set_auto_reconnect(self, enabled):
        with self._lock:
            self.auto_reconnect = bool(enabled)
            if not enabled and self._reconnect_timer is not None:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
        self.manager.save()
        self.manager._notify_change(self)

    def set_auto_list(self, enabled):
        with self._lock:
            self.auto_list = bool(enabled)
        self.manager.save()
        self.manager._notify_change(self)

    def get_effective_nick(self):
        if isinstance(self.nick_override, str) and self.nick_override:
            return self.nick_override
        return self.manager.get_nickname()

    def set_nick_override(self, nick):
        with self._lock:
            if nick is None or (isinstance(nick, str) and nick == ""):
                self.nick_override = None
            else:
                self.nick_override = str(nick)
        self.manager.save()
        self.manager._notify_change(self)

    def _packet_would_fit(self, link, payload):
        try:
            pkt = RNS.Packet(link, payload)
            pkt.pack()
            return True
        except Exception:
            return False

    def _send_env(self, env):
        with self._lock:
            link = self.link
        if link is None or link.status != RNS.Link.ACTIVE:
            raise RuntimeError("not connected")
        payload = cbor.encode(env)
        if not self._packet_would_fit(link, payload):
            raise RuntimeError("message exceeds link MTU")
        RNS.Packet(link, payload).send()

    def join_room(self, room, key=None, silent=False):
        r = self._normalize_room(room)
        body = key if (isinstance(key, str) and key) else None
        env = _make_envelope(T_JOIN, src=self.manager.identity.hash, room=r, body=body)
        nick = self.get_effective_nick()
        if nick:
            env[K_NICK] = nick
        with self._lock:
            self._pending_joins.add(r)
            if silent:
                self._silent_joins.add(r)
        self._send_env(env)
        with self._lock:
            if r not in self.messages:
                self.messages[r] = []
        self.manager._notify_change(self)

    def send_command(self, text, room=None):
        if not isinstance(text, str) or not text.startswith("/"):
            raise ValueError("command must start with /")
        env = _make_envelope(T_MSG, src=self.manager.identity.hash, room=room, body=text)
        nick = self.get_effective_nick()
        if nick:
            env[K_NICK] = nick
        self._send_env(env)

    def send_ping(self, room=None):
        body = os.urandom(8)
        env = _make_envelope(T_PING, src=self.manager.identity.hash, body=body)
        with self._lock:
            now_ms = _now_ms()
            self._pending_pings[body] = (now_ms, room)
            expired = [k for k, v in self._pending_pings.items() if now_ms - v[0] > 15000]
            for k in expired:
                self._pending_pings.pop(k, None)
        self._send_env(env)
        return body

    def part_room(self, room):
        room_n = self._normalize_room(room)
        env = _make_envelope(T_PART, src=self.manager.identity.hash, room=room_n)
        with self._lock:
            self._pending_parts.add(room_n)
        try:
            self._send_env(env)
        except Exception:
            pass
        with self._lock:
            self.rooms.discard(room_n)
        self.manager.save()
        self.manager._notify_change(self)

    def send_message(self, room, text):
        r = self._normalize_room(room)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text must be non-empty")
        if len(text.encode("utf-8")) > self.max_msg_body_bytes:
            raise ValueError("message too long for hub limit")
        env = _make_envelope(T_MSG, src=self.manager.identity.hash, room=r, body=text)
        nick = self.get_effective_nick()
        if nick:
            env[K_NICK] = nick
        mid = env[K_ID]
        if isinstance(mid, (bytes, bytearray)):
            self._sent_ids.append(bytes(mid))
        self._send_env(env)
        self._record_message(RRCMessage("msg", r, self.manager.identity.hash, nick, text, _now_ms()), local=True)
        return mid

    def _record_message(self, msg, local=False):
        with self._lock:
            buf = self.messages.setdefault(msg.room or "*", [])
            buf.append(msg)
            if len(buf) > 500:
                del buf[:len(buf)-500]
            if not local and msg.room:
                if msg.room != self.manager.active_room_for(self):
                    self.unread_rooms.add(msg.room)
                    if msg.mention:
                        self.mention_rooms.add(msg.room)
        self.manager._notify_messages(self, msg)

    def _record_system(self, room, text):
        if not room:
            return
        msg = RRCMessage("system", room, None, None, text, _now_ms())
        with self._lock:
            buf = self.messages.setdefault(room, [])
            buf.append(msg)
            if len(buf) > 500:
                del buf[:len(buf)-500]
        self.manager._notify_messages(self, msg)

    def _record_notice(self, msg):
        target_room = msg.room
        if not target_room:
            target_room = self.manager.active_room_for(self)
            if target_room:
                msg.room = target_room

        with self._lock:
            self.notices.append(msg)
            if len(self.notices) > 200:
                del self.notices[:len(self.notices)-200]
            if target_room:
                buf = self.messages.setdefault(target_room, [])
                buf.append(msg)
                if len(buf) > 500:
                    del buf[:len(buf)-500]
                if target_room != self.manager.active_room_for(self):
                    self.unread_rooms.add(target_room)
        self.manager._notify_messages(self, msg)

    def get_messages(self, room):
        with self._lock:
            buf = list(self.messages.get(room, []))
        return buf

    def _on_packet(self, data):
        try:
            env = cbor.decode(data)
        except Exception as e:
            self._log("decode failed: "+str(e), RNS.LOG_DEBUG)
            return
        if not isinstance(env, dict):
            return
        try:
            t = env.get(K_T)
        except Exception:
            return

        if t == T_PING:
            try:
                pong = _make_envelope(T_PONG, src=self.manager.identity.hash, body=env.get(K_BODY))
                self._send_env(pong)
            except Exception:
                pass
            return

        if t == T_PONG:
            body = env.get(K_BODY)
            if isinstance(body, (bytes, bytearray)):
                key = bytes(body)
                with self._lock:
                    pending = self._pending_pings.pop(key, None)
                if pending is not None:
                    sent_ms, room = pending
                    rtt_ms = max(0, _now_ms() - sent_ms)
                    self._record_system(room, "Pong from hub: "+str(rtt_ms)+" ms")
            return

        if t == T_WELCOME:
            self.welcomed = True
            body = env.get(K_BODY)
            if isinstance(body, dict):
                hub_name = body.get(B_WELCOME_HUB)
                if isinstance(hub_name, str):
                    self.hub_name = hub_name
                ver = body.get(B_WELCOME_VER)
                if isinstance(ver, str):
                    self.hub_version = ver
                limits = body.get(B_WELCOME_LIMITS)
                if isinstance(limits, dict):
                    if L_MAX_NICK_BYTES in limits:
                        self.max_nick_bytes = int(limits[L_MAX_NICK_BYTES])
                    if L_MAX_ROOM_NAME_BYTES in limits:
                        self.max_room_name_bytes = int(limits[L_MAX_ROOM_NAME_BYTES])
                    if L_MAX_MSG_BODY_BYTES in limits:
                        self.max_msg_body_bytes = int(limits[L_MAX_MSG_BODY_BYTES])
                    if L_MAX_ROOMS_PER_SESSION in limits:
                        self.max_rooms_per_session = int(limits[L_MAX_ROOMS_PER_SESSION])
                    if L_RATE_LIMIT_MSGS_PER_MINUTE in limits:
                        self.rate_limit_msgs_per_minute = int(limits[L_RATE_LIMIT_MSGS_PER_MINUTE])
            self._set_status(RRCHub.STATUS_CONNECTED, "Connected")
            with self._lock:
                self._reconnect_attempts = 0
            self.manager._on_welcome(self)
            if self.auto_list:
                try:
                    with self._lock:
                        self._silent_list_pending += 1
                    self.send_command("/list", room=None)
                except Exception:
                    with self._lock:
                        if self._silent_list_pending > 0:
                            self._silent_list_pending -= 1
            return

        if t == T_JOINED:
            room = env.get(K_ROOM)
            if isinstance(room, str) and room:
                r = room.strip().lower()
                body = env.get(K_BODY)
                own_hash = self.manager.identity.hash if self.manager.identity is not None else None

                body_hashes = []
                if isinstance(body, list):
                    body_hashes = [bytes(e) for e in body if isinstance(e, (bytes, bytearray))]

                with self._lock:
                    self_join = r in self._pending_joins
                    silent = r in self._silent_joins
                    if self_join:
                        self._pending_joins.discard(r)
                    if silent:
                        self._silent_joins.discard(r)

                    self.rooms.add(r)
                    if r not in self.messages:
                        self.messages[r] = []
                    members = self.members.setdefault(r, set())
                    for h in body_hashes:
                        members.add(h)
                    if own_hash is not None:
                        members.add(own_hash)

                if self_join:
                    if not silent:
                        self._record_system(r, "You joined #"+r)
                    self.manager.save()
                else:
                    joiner = None
                    if len(body_hashes) == 1 and (own_hash is None or body_hashes[0] != own_hash):
                        joiner = body_hashes[0]
                    if joiner is not None:
                        self._record_system(r, self.display_name_for(joiner)+" joined")
                self.manager._notify_change(self)
            return

        if t == T_PARTED:
            room = env.get(K_ROOM)
            if isinstance(room, str) and room:
                r = room.strip().lower()
                body = env.get(K_BODY)
                own_hash = self.manager.identity.hash if self.manager.identity is not None else None

                body_hashes = []
                if isinstance(body, list):
                    body_hashes = [bytes(e) for e in body if isinstance(e, (bytes, bytearray))]

                with self._lock:
                    self_part = r in self._pending_parts
                    if self_part:
                        self._pending_parts.discard(r)

                    members = self.members.get(r)
                    if members is not None:
                        for h in body_hashes:
                            members.discard(h)
                    if self_part:
                        self.rooms.discard(r)
                        self.members.pop(r, None)

                if self_part:
                    self.manager.save()
                else:
                    parter = None
                    if len(body_hashes) == 1 and (own_hash is None or body_hashes[0] != own_hash):
                        parter = body_hashes[0]
                    if parter is not None:
                        self._record_system(r, self.display_name_for(parter)+" left")
                self.manager._notify_change(self)
            return

        if t == T_MSG:
            body = env.get(K_BODY)
            room = env.get(K_ROOM)
            src  = env.get(K_SRC)
            nick = env.get(K_NICK)
            mid  = env.get(K_ID)
            own_hash = self.manager.identity.hash if self.manager.identity is not None else None
            if isinstance(src, (bytes, bytearray)) and own_hash is not None and bytes(src) == own_hash:
                if isinstance(mid, (bytes, bytearray)) and bytes(mid) in self._sent_ids:
                    return
            if isinstance(src, (bytes, bytearray)) and isinstance(nick, str) and nick:
                with self._lock:
                    self.nicks[bytes(src)] = nick
                    if isinstance(room, str) and room:
                        self.members.setdefault(room.strip().lower(), set()).add(bytes(src))
            if isinstance(body, str):
                msg = RRCMessage(
                    "msg",
                    room.strip().lower() if isinstance(room, str) else None,
                    bytes(src) if isinstance(src, (bytes, bytearray)) else None,
                    nick if isinstance(nick, str) else None,
                    body,
                    _now_ms(),
                )
                is_own = isinstance(src, (bytes, bytearray)) and own_hash is not None and bytes(src) == own_hash
                if not is_own:
                    own_nick = self.get_effective_nick()
                    pat = _mention_re(own_nick)
                    if pat is not None and pat.search(body):
                        msg.mention = True
                self._record_message(msg)
            return

        if t == T_NOTICE:
            body = env.get(K_BODY)
            room = env.get(K_ROOM)
            src  = env.get(K_SRC)
            if isinstance(body, str):
                parsed = _parse_room_list_notice(body)
                if parsed is not None:
                    with self._lock:
                        self.available_rooms = parsed
                        silent = self._silent_list_pending > 0
                        if silent:
                            self._silent_list_pending -= 1
                    self.manager._notify_change(self)
                    if silent:
                        return
                msg = RRCMessage(
                    "notice",
                    room.strip().lower() if isinstance(room, str) else None,
                    bytes(src) if isinstance(src, (bytes, bytearray)) else None,
                    None,
                    body,
                    _now_ms(),
                )
                self._record_notice(msg)
            return

        if t == T_ERROR:
            body = env.get(K_BODY)
            room = env.get(K_ROOM)
            text = body if isinstance(body, str) else "(error)"
            msg = RRCMessage(
                "error",
                room.strip().lower() if isinstance(room, str) else None,
                None,
                None,
                text,
                _now_ms(),
            )
            self._record_notice(msg)
            return

        if t == T_RESOURCE_ENVELOPE:
            body = env.get(K_BODY)
            if not isinstance(body, dict):
                return
            try:
                rid = body.get(B_RES_ID)
                kind = body.get(B_RES_KIND)
                size = body.get(B_RES_SIZE)
                sha256 = body.get(B_RES_SHA256)
                encoding = body.get(B_RES_ENCODING)
                if not isinstance(rid, (bytes, bytearray)): return
                if not isinstance(kind, str): return
                if not isinstance(size, int) or size <= 0: return
                room = env.get(K_ROOM)
                with self._lock:
                    self._resource_expectations[bytes(rid)] = {
                        "kind": kind,
                        "size": size,
                        "sha256": bytes(sha256) if isinstance(sha256, (bytes, bytearray)) else None,
                        "encoding": encoding if isinstance(encoding, str) else "utf-8",
                        "room": room.strip().lower() if isinstance(room, str) else None,
                        "expires": time.monotonic()+30.0,
                    }
            except Exception:
                pass
            return

    def _resource_advertised(self, resource):
        try:
            if hasattr(resource, "get_data_size"):
                size = resource.get_data_size()
            elif hasattr(resource, "total_size"):
                size = resource.total_size
            else:
                size = getattr(resource, "size", 0)
        except Exception:
            return False
        if size > 262144:
            return False
        return True

    def _resource_concluded(self, resource):
        try:
            if resource.status != RNS.Resource.COMPLETE:
                try:
                    if hasattr(resource, "data") and resource.data:
                        resource.data.close()
                except Exception:
                    pass
                return
            try:
                size = resource.total_size if hasattr(resource, "total_size") else getattr(resource, "size", 0)
            except Exception:
                size = 0
            data = None
            try:
                data = resource.data.read()
            finally:
                try:
                    if hasattr(resource, "data") and resource.data:
                        resource.data.close()
                except Exception:
                    pass
            if data is None:
                return

            now = time.monotonic()
            matched = None
            with self._lock:
                expired = [k for k, v in self._resource_expectations.items() if v["expires"] < now]
                for k in expired:
                    self._resource_expectations.pop(k, None)
                for k, exp in list(self._resource_expectations.items()):
                    if exp["size"] == len(data):
                        matched = exp
                        self._resource_expectations.pop(k, None)
                        break

            kind = matched["kind"] if matched else RES_KIND_BLOB
            room = matched["room"] if matched else None
            encoding = matched["encoding"] if matched else "utf-8"
            sha = matched["sha256"] if matched else None
            if sha is not None:
                if hashlib.sha256(data).digest() != sha:
                    return
            if kind in (RES_KIND_NOTICE, RES_KIND_MOTD):
                try:
                    text = data.decode(encoding, errors="replace")
                except Exception:
                    return
                msg = RRCMessage("notice", room, None, None, text, _now_ms())
                self._record_notice(msg)
        except Exception as e:
            self._log("resource handling failed: "+str(e), RNS.LOG_ERROR)


class RRCManager:
    def __init__(self, app):
        self.app = app
        self.hubs = []
        self._lock = threading.RLock()
        self._change_callback = None
        self._message_callback = None
        self._active_hub = None
        self._active_room = None
        self._loaded = False
        self._loading = False
        self._save_lock = threading.Lock()

    @property
    def identity(self):
        return self.app.identity

    def get_nickname(self):
        try:
            n = self.app.peer_settings.get("display_name")
            if isinstance(n, str):
                return n
        except Exception:
            pass
        return None

    def set_change_callback(self, cb):
        self._change_callback = cb

    def set_message_callback(self, cb):
        self._message_callback = cb

    def _notify_change(self, hub=None):
        try:
            if self._change_callback is not None:
                self._change_callback(hub)
        except Exception:
            pass

    def _notify_messages(self, hub, msg):
        try:
            if self._message_callback is not None:
                self._message_callback(hub, msg)
        except Exception:
            pass

    def _on_welcome(self, hub):
        for r in list(hub.rooms):
            try:
                hub.join_room(r, silent=True)
            except Exception:
                pass

    def set_active(self, hub, room):
        self._active_hub = hub
        self._active_room = room
        if hub is not None and room is not None:
            hub.mark_read(room)

    def active_room_for(self, hub):
        if self._active_hub is hub:
            return self._active_room
        return None

    def has_unread(self):
        with self._lock:
            for hub in self.hubs:
                if hub.unread_rooms:
                    return True
        return False

    def add_hub(self, hub_hash, dest_name=None, name=None):
        with self._lock:
            for h in self.hubs:
                if h.hub_hash == hub_hash and (h.dest_name == (dest_name or DEFAULT_DEST_NAME)):
                    return h
            hub = RRCHub(self, hub_hash, dest_name=dest_name, name=name)
            self.hubs.append(hub)
        self.save()
        self._notify_change()
        return hub

    def remove_hub(self, hub):
        with self._lock:
            if hub in self.hubs:
                self.hubs.remove(hub)
        try:
            hub.disconnect()
        except Exception:
            pass
        self.save()
        self._notify_change()

    def find_hub(self, hub_hash, dest_name=None):
        dn = dest_name or DEFAULT_DEST_NAME
        with self._lock:
            for h in self.hubs:
                if h.hub_hash == hub_hash and h.dest_name == dn:
                    return h
        return None

    def _store_path(self):
        return os.path.join(self.app.storagepath, "rrc_hubs")

    def load(self):
        if self._loaded:
            return
        self._loaded = True
        path = self._store_path()
        if not os.path.isfile(path):
            return
        self._loading = True
        try:
            with open(path, "rb") as f:
                data = f.read()
            obj = cbor.decode(data)
            if not isinstance(obj, dict):
                return
            entries = obj.get("hubs")
            if not isinstance(entries, list):
                return
            for e in entries:
                if not isinstance(e, dict):
                    continue
                hh = e.get("hash")
                if not isinstance(hh, (bytes, bytearray)):
                    continue
                dn = e.get("dest_name")
                nm = e.get("name")
                hub = self.add_hub(bytes(hh), dest_name=dn if isinstance(dn, str) else None, name=nm if isinstance(nm, str) else None)
                rooms = e.get("rooms")
                if isinstance(rooms, list):
                    for r in rooms:
                        if isinstance(r, str):
                            hub.add_room(r)
                parted = e.get("parted_rooms")
                if isinstance(parted, list):
                    for r in parted:
                        if isinstance(r, str):
                            try:
                                rn = hub._normalize_room(r)
                                with hub._lock:
                                    hub.messages.setdefault(rn, [])
                            except Exception:
                                pass
                ar = e.get("auto_reconnect")
                if isinstance(ar, bool):
                    hub.auto_reconnect = ar
                al = e.get("auto_list")
                if isinstance(al, bool):
                    hub.auto_list = al
                no = e.get("nick")
                if isinstance(no, str) and no:
                    hub.nick_override = no
        except Exception as e:
            RNS.log("Failed to load RRC hubs: "+str(e), RNS.LOG_ERROR)
        finally:
            self._loading = False

    def save(self):
        if self._loading:
            return
        path = self._store_path()
        tmp_path = path + ".tmp"
        with self._save_lock:
            try:
                entries = []
                with self._lock:
                    for h in self.hubs:
                        joined = set(h.rooms)
                        parted = set(h.messages.keys()) - joined
                        entry = {
                            "hash":           h.hub_hash,
                            "dest_name":      h.dest_name,
                            "name":           h.name,
                            "rooms":          sorted(joined),
                            "parted_rooms":   sorted(parted),
                            "auto_reconnect": bool(h.auto_reconnect),
                            "auto_list":      bool(h.auto_list),
                        }
                        if isinstance(h.nick_override, str) and h.nick_override:
                            entry["nick"] = h.nick_override
                        entries.append(entry)
                data = cbor.encode({"hubs": entries})
                with open(tmp_path, "wb") as f:
                    f.write(data)
                    f.flush()
                    try: os.fsync(f.fileno())
                    except Exception: pass
                os.replace(tmp_path, path)
            except Exception as e:
                # 
                #
                #
                #
                try: os.unlink(tmp_path)
                except Exception: pass

    def shutdown(self):
        for h in list(self.hubs):
            try:
                h.disconnect()
            except Exception:
                pass
