#!/usr/bin/env python3
"""Portable OTA upload; optionally pause only the current MacStatusBoard agent."""
import argparse
import base64
import contextlib
import os
import pathlib
import plistlib
import secrets
import signal
import subprocess
import time
import urllib.request

AGENT_LABEL = 'com.local.status-board'


def launchctl(*arguments):
    subprocess.run(['launchctl', *arguments], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=15)


@contextlib.contextmanager
def pause_agent():
    plist = pathlib.Path.home() / 'Library/LaunchAgents' / (AGENT_LABEL + '.plist')
    if not plist.is_file() or plistlib.loads(plist.read_bytes()).get('Label') != AGENT_LABEL:
        raise RuntimeError('Expected MacStatusBoard LaunchAgent plist is missing or mismatched')
    domain = 'gui/%d' % os.getuid()
    target = domain + '/' + AGENT_LABEL
    launchctl('print', target)  # Fail before any mutation when the service is absent.
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, terminate)
    stopped = False
    try:
        launchctl('bootout', target)
        stopped = True
        # The telemetry socket may remain in its receive deadline during shutdown.
        time.sleep(5)
        yield
    finally:
        try:
            if stopped:
                launchctl('bootstrap', domain, str(plist))
        finally:
            signal.signal(signal.SIGTERM, previous_handler)


def request_for_upload(firmware, token_file, url):
    key = pathlib.Path(token_file).read_text().strip()
    boundary = '----' + secrets.token_hex(12)
    body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="firmware"; '
            'filename="firmware.bin"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    body += pathlib.Path(firmware).read_bytes() + ('\r\n--' + boundary + '--\r\n').encode()
    auth = 'Basic ' + base64.b64encode(('admin:' + key).encode()).decode()
    request = urllib.request.Request(url.rstrip('/') + '/update', data=body,
        headers={'Authorization': auth, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
    return request, key


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('firmware')
    parser.add_argument('--url', default='http://mac-status-board.local')
    parser.add_argument('--token-file', default='private/ota-token')
    parser.add_argument('--pause-agent', action='store_true',
                        help='Pause and restore only the current user MacStatusBoard LaunchAgent during OTA')
    args = parser.parse_args(argv)
    # Prepare and validate local inputs before interrupting telemetry.
    request, key = request_for_upload(args.firmware, args.token_file, args.url)
    window = pause_agent() if args.pause_agent else contextlib.nullcontext()
    with window:
        with urllib.request.urlopen(request, timeout=45) as response:
            reply = response.read().decode()
            print(response.status, reply.replace(key, '[redacted]') if key else reply)


if __name__ == '__main__':
    main()
