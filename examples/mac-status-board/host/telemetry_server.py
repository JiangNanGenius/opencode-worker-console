#!/usr/bin/env python3
"""Read-only LAN telemetry, mutually authenticated before any snapshot is sent.

NDJSON v1 on TCP 8642, UDP discovery on the same port. The existing local
OTA token is a key only: it never appears on the wire, argv or logs.
"""
import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import socket
import subprocess
import threading
import time

PORT = 8642
MAX_FRAME = 24576
EPOCH_FIELDS = frozenset("unix_time sampled_at valid_until expires_at resets_at started_at ended_at updated_at".split())


def proof(key, role, board_nonce, server_nonce, port=None):
    message = '%s:v1:%s:%s' % (role, board_nonce, server_nonce)
    if port is not None:
        message += ':' + str(port)
    return hmac.new(key, message.encode('ascii'), hashlib.sha256).hexdigest()


def nonce_ok(value):
    return isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def lan_peer(address):
    """Do not trust headers or ipaddress.is_private (which includes reserved nets)."""
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_loopback or (ip.version == 4 and any(ip in ipaddress.ip_network(n)
            for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
    except ValueError:
        return False


def fields(value, names, string_limit=288):
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in names.split():
        item = value.get(name)
        if isinstance(item, str):
            limit = min(string_limit, {'id':64,'title':288,'project':108,'model':96,'status':24,'state':24,'unit':16}.get(name,string_limit))
            result[name] = item.encode('utf-8')[:limit].decode('utf-8','ignore')
        elif isinstance(item, (bool, int, float)) and (not isinstance(item, float) or math.isfinite(item)):
            if name in EPOCH_FIELDS:
                # Integer seconds survive ArduinoJson float32 compaction and NDJSON re-encoding.
                result[name] = int(item) if not isinstance(item,bool) and 0 <= item <= 4102444800 else 0
            else:
                result[name] = item
    return result


def sanitize_snapshot(value):
    """Explicit schema whitelist: no transcripts, bodies, paths, credentials or extras."""
    if not isinstance(value, dict) or value.get('schema_version') != 2:
        raise ValueError('Expected schema 2')
    result = fields(value, 'unix_time utc_offset_seconds')
    result['schema_version'] = 2
    system = value.get('system', {})
    result['system'] = fields(system, 'host sampled_at cpu memory_percent memory_used_gb memory_total_gb cpu_temp_c gpu_temp_c uptime_hours', 64)
    result['system']['disks'] = [fields(d, 'name free_gb total_gb used_percent', 48)
        for d in system.get('disks', [])[:3] if isinstance(d, dict)] if isinstance(system, dict) and isinstance(system.get('disks', []), list) else []
    result['system']['fans'] = [fields(d, 'rpm') for d in system.get('fans', [])[:2]
        if isinstance(d, dict)] if isinstance(system, dict) and isinstance(system.get('fans', []), list) else []
    tasks = value.get('tasks', {})
    result['tasks'] = {}
    for name in ('codex', 'opencode'):
        source = tasks.get(name, {}) if isinstance(tasks, dict) else {}
        row = fields(source, 'state sampled_at total active', 24)
        items = source.get('items', []) if isinstance(source, dict) else []
        row['items'] = [fields(t, 'id title project model status started_at ended_at updated_at')
            for t in items[:12] if isinstance(t, dict)] if isinstance(items, list) else []
        result['tasks'][name] = row
    result['quotas'] = []
    quotas = value.get('quotas', [])
    for quota in quotas[:8] if isinstance(quotas, list) else []:
        if not isinstance(quota, dict):
            continue
        row = fields(quota, 'id name state sampled_at valid_until available exhausted stale expires_at', 64)
        windows = quota.get('windows', [])
        row['windows'] = [fields(w, 'name label remaining_percent resets_at expires_at remaining limit unit', 48)
            for w in windows[:4] if isinstance(w, dict)] if isinstance(windows, list) else []
        balances = quota.get('balances', [])
        row['balances'] = [fields(b, 'currency remaining', 8)
            for b in balances[:2] if isinstance(b, dict)] if isinstance(balances, list) else []
        row['snapshot'] = fields(quota.get('snapshot'), 'freshness expired expires_at', 24)
        row['billing'] = fields(quota.get('billing'), 'monthly_exhausted')
        result['quotas'].append(row)
    # A maximum-sized UTF-8 title set must not starve the entire telemetry feed.
    # Keep source totals while bounding the displayed task subset.
    while len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8')) > MAX_FRAME-128:
        rows = [row['items'] for row in result['tasks'].values() if row['items']]
        if not rows:
            raise ValueError('Snapshot exceeds limit')
        max(rows,key=len).pop()
    return result


def encode_frame(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_FRAME:
        raise ValueError('Frame exceeds limit')
    return encoded + b'\n'


class FrameReader:
    def __init__(self, connection):
        self.connection = connection
        self.buffer = bytearray()

    def read(self):
        while True:
            end = self.buffer.find(b'\n')
            if end >= 0:
                if end > MAX_FRAME:
                    raise ValueError('Frame exceeds limit')
                raw = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError('Expected object')
                return value
            if len(self.buffer) > MAX_FRAME:
                raise ValueError('Frame exceeds limit')
            chunk = self.connection.recv(min(4096, MAX_FRAME + 1 - len(self.buffer)))
            if not chunk:
                raise EOFError()
            self.buffer.extend(chunk)


class TelemetryServer:
    def __init__(self, snapshot, key, host='0.0.0.0', port=PORT, advertise=True, interval=1.0):
        if not isinstance(key, bytes) or len(key) < 24:
            raise ValueError('Missing provisioned key')
        self.snapshot = snapshot
        self.key = key
        self.host = host
        self.port = port
        self.advertise = advertise
        self.interval = interval
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.clients = set()
        self.threads = []
        self.last_ack = {}
        self.tcp = self.udp = self.mdns = None

    def active_client(self):
        now = time.monotonic()
        with self.lock:
            return any(now - ack < 5 for ack in self.last_ack.values())

    def start(self):
        try:
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp.bind((self.host, self.port))
            self.port = self.tcp.getsockname()[1]
            self.tcp.listen(3)
            self.tcp.settimeout(.5)
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp.bind((self.host, self.port))
            self.udp.settimeout(.5)
            for fn in (self._accept, self._discovery):
                thread = threading.Thread(target=fn, daemon=True)
                self.threads.append(thread)
                thread.start()
            if self.advertise:
                try:
                    self.mdns = subprocess.Popen(['/usr/bin/dns-sd', '-R', 'Mac Telemetry', '_macboard._tcp', 'local.', str(self.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass  # UDP and bounded firmware scan remain available.
            return self
        except Exception:
            self.close()
            raise

    def _accept(self):
        while not self.stop.is_set():
            try:
                connection, address = self.tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                allowed = not self.stop.is_set() and lan_peer(address[0]) and len(self.clients) < 3
                if allowed:
                    self.clients.add(connection)
                    self.threads = [thread for thread in self.threads if thread.is_alive()]
                    thread = threading.Thread(target=self._client, args=(connection,), daemon=True)
                    thread.start()
                    self.threads.append(thread)
            if not allowed:
                connection.close()
                continue

    def _client(self, connection):
        try:
            connection.settimeout(3)
            reader = FrameReader(connection)
            hello = reader.read()
            if hello.get('v') != 1 or hello.get('type') != 'hello' or not nonce_ok(hello.get('nonce')):
                return
            board_nonce = hello['nonce']
            server_nonce = secrets.token_hex(16)
            connection.sendall(encode_frame({'v': 1, 'type': 'challenge', 'nonce': server_nonce,
                'proof': proof(self.key, 'host', board_nonce, server_nonce)}))
            auth = reader.read()
            expected = proof(self.key, 'board', board_nonce, server_nonce)
            if auth.get('type') != 'auth' or not isinstance(auth.get('proof'), str) or not hmac.compare_digest(auth['proof'], expected):
                return
            seq = 0
            while not self.stop.is_set():
                started = time.monotonic()
                seq += 1
                frame = {'v': 1, 'type': 'snapshot', 'seq': seq, 'data': sanitize_snapshot(self.snapshot())}
                connection.sendall(encode_frame(frame))
                ack = reader.read()
                if ack.get('type') != 'ack' or type(ack.get('seq')) is not int or ack['seq'] != seq:
                    return
                with self.lock:
                    self.last_ack[connection] = time.monotonic()
                self.stop.wait(max(0, self.interval - (time.monotonic() - started)))
        except (OSError, EOFError, ValueError, TypeError, RecursionError):
            pass
        finally:
            with self.lock:
                self.last_ack.pop(connection, None)
                self.clients.discard(connection)
            connection.close()

    def _discovery(self):
        while not self.stop.is_set():
            try:
                data, address = self.udp.recvfrom(513)
                if not lan_peer(address[0]) or len(data) > 512:
                    continue
                message = json.loads(data)
                if not isinstance(message, dict) or message.get('v') != 1 or message.get('type') != 'probe' or not nonce_ok(message.get('nonce')):
                    continue
                server_nonce = secrets.token_hex(16)
                offer = {'v': 1, 'type': 'offer', 'nonce': server_nonce, 'port': self.port,
                    'proof': proof(self.key, 'offer', message['nonce'], server_nonce, self.port)}
                self.udp.sendto(encode_frame(offer), address)
            except socket.timeout:
                continue
            except (ValueError, TypeError, RecursionError):
                continue
            except OSError:
                break

    def close(self):
        self.stop.set()
        for listener in (self.tcp, self.udp):
            if listener is not None:
                listener.close()
        with self.lock:
            connections = list(self.clients)
            threads = list(self.threads)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self.mdns is not None:
            self.mdns.terminate()
            try:
                self.mdns.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.mdns.kill()
                self.mdns.wait()
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=3.5)  # macOS recv/select may finish on its 3-second socket deadline.
