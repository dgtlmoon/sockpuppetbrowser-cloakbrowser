#!/usr/bin/python3

import sys
import urllib.request
import argparse
from urllib.parse import urlparse
import datetime

def main():
    parser = argparse.ArgumentParser(description='Health check script')
    parser.add_argument(
        '--host',
        default='http://localhost',
        help='Hostname or URL to check (default: http://localhost)'
    )
    args = parser.parse_args()

    host_input = args.host
    parsed_url = urlparse(host_input)

    if not parsed_url.scheme:
        host_input = f'http://{host_input}'
        parsed_url = urlparse(host_input)

    scheme = parsed_url.scheme or 'http'
    netloc = parsed_url.netloc or 'localhost'
    base_url = f'{scheme}://{netloc}'

    log_file_path = '/tmp/healthcheck.log'
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Only check the HTTP stats endpoint — probing the WebSocket port with a raw
    # TCP connection causes the server to log spurious "opening handshake failed"
    # errors. The stats endpoint is sufficient: if it responds, both the HTTP
    # server and the main process are healthy.
    try:
        stats_url = f'{base_url}:8080/stats'
        response = urllib.request.urlopen(stats_url, timeout=5)
        if response.status != 200:
            with open(log_file_path, 'a') as log_file:
                log_file.write(f'[{timestamp}] HTTP request to {stats_url} returned status code {response.status}\n')
            sys.exit(1)
    except Exception as e:
        with open(log_file_path, 'a') as log_file:
            log_file.write(f'[{timestamp}] HTTP request to {stats_url} failed: {e}\n')
        sys.exit(1)

    sys.exit(0)

if __name__ == '__main__':
    main()
