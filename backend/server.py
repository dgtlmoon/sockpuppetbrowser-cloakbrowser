#!/usr/bin/env python3

# Auto-scaling WebSocket proxy for Chrome CDP — CloakBrowser edition
# Uses the cloakbrowser pip package (https://github.com/CloakHQ/CloakBrowser)
# which ships a patched Chromium with C++-level fingerprint spoofing for
# canvas, WebGL, audio, navigator, and automation signals.

def strtobool(val):
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError(f"invalid truth value {val!r}")

from http_server import start_http_server
from ports import PortSelector
from loguru import logger
import aiohttp
import argparse
import asyncio
import orjson
import os
import psutil
import shlex
import shutil
import sys
import tempfile
import time
import websockets

stats = {
    'confirmed_data_received': 0,
    'connection_count': 0,
    'connection_count_total': 0,
    'dropped_threshold_reached': 0,
    'dropped_waited_too_long': 0,
    'special_counter': [],
    'chrome_start_failures': 0,
}


connection_count_max = int(os.getenv('MAX_CONCURRENT_CHROME_PROCESSES', 10))
# asyncio.Semaphore is used from async code — no polling, proper backpressure
connection_semaphore = asyncio.Semaphore(connection_count_max)
port_selector = PortSelector()
# Minimum free RAM (in MB) required before accepting a new browser connection.
# Uses psutil.virtual_memory().available which counts free + reclaimable page
# cache — a much better signal than a raw usage % which Linux inflates via
# aggressive file caching. Swap is intentionally excluded: a Chrome that has
# to swap is already unusable.
min_available_memory_mb = int(os.getenv('MIN_AVAILABLE_MEMORY_MB', 500))
stats_refresh_time = int(os.getenv('STATS_REFRESH_SECONDS', 3))
STARTUP_DELAY = int(os.getenv('STARTUP_DELAY', 0))
DROP_EXCESS_CONNECTIONS = strtobool(os.getenv('DROP_EXCESS_CONNECTIONS', 'False'))
# Kill a proxied connection (and its Chrome/tab) if no CDP messages flow for
# this many seconds.  Set to 0 to disable.
IDLE_TIMEOUT_SECONDS = int(os.getenv('IDLE_TIMEOUT_SECONDS', 60))
# Delete auto-created /tmp/cloak-proxy* profile dirs on disconnect.
# OFF by default so Chrome's disk cache is reused across sessions, which
# speeds up page loads.  Enable if you want a clean slate every time or
# need to reclaim disk space.
CLEANUP_PARALLEL_USER_DATA_DIR = strtobool(os.getenv('CLEANUP_PARALLEL_USER_DATA_DIR', 'False'))


def get_cloak_binary():
    """Resolve the CloakBrowser patched Chromium binary path.

    Resolution order:
    1. CLOAKBROWSER_BINARY_PATH env var (explicit override)
    2. cloakbrowser.binary_info() Python API
    3. CHROME_BIN env var fallback
    """
    explicit = os.getenv('CLOAKBROWSER_BINARY_PATH')
    if explicit:
        return explicit

    try:
        from cloakbrowser import binary_info
        info = binary_info()
        if isinstance(info, str):
            return info
        if isinstance(info, dict):
            path = (info.get('executable') or info.get('path') or
                    info.get('binary') or info.get('binary_path'))
            if path:
                return path
    except Exception as e:
        logger.warning(f"cloakbrowser.binary_info() failed: {e}")

    fallback = os.getenv('CHROME_BIN', '/usr/bin/google-chrome')
    logger.warning(f"Falling back to CHROME_BIN={fallback}")
    return fallback


def build_fingerprint_args():
    """Build CloakBrowser fingerprint CLI flags from environment variables."""
    args = []

    seed = os.getenv('FINGERPRINT_SEED', '').strip()
    if seed:
        args.append(f"--fingerprint={seed}")
    # No seed → CloakBrowser picks a random seed per launch (recommended)

    platform = os.getenv('FINGERPRINT_PLATFORM', 'windows').strip()
    if platform:
        args.append(f"--fingerprint-platform={platform}")

    extra = os.getenv('EXTRA_FINGERPRINT_ARGS', '').strip()
    if extra:
        args.extend(shlex.split(extra))

    return args


def build_webrtc_args():
    """Build Chrome flags to prevent WebRTC-based IP address leaks."""
    args = []

    if strtobool(os.getenv('BLOCK_WEBRTC', 'true')):
        args.append('--disable-webrtc')
        args.append('--enforce-webrtc-ip-permission-check')
    else:
        policy = os.getenv('WEBRTC_IP_HANDLING_POLICY', 'disable_non_proxied_udp').strip()
        if policy:
            args.append(f"--force-webrtc-ip-handling-policy={policy}")

    return args


def is_profile_in_use(udd: str) -> bool:
    """Return True if a Chrome user-data-dir is locked by a running process.

    Chrome creates a SingletonLock symlink (hostname-PID) on startup and
    removes it on clean exit.  We verify the embedded PID is still alive so
    stale locks left by a crash don't permanently block the slot.

    On any ambiguity (unreadable lock, unexpected format, permission error)
    we conservatively return True so the caller skips to the next slot rather
    than risk two Chrome processes colliding on the same profile.
    """
    lock_path = os.path.join(udd, 'SingletonLock')
    # No lock file at all → definitely free
    if not os.path.islink(lock_path) and not os.path.exists(lock_path):
        return False
    try:
        target = os.readlink(lock_path)       # e.g. "myhostname-12345"
        pid = int(target.rsplit('-', 1)[-1])
        os.kill(pid, 0)                       # signal 0 = existence check only
        return True
    except OSError as e:
        import errno as errno_mod
        if e.errno == errno_mod.ESRCH:
            # PID is gone → stale lock. Delete it so Chrome can start cleanly.
            # This also handles cross-hostname stale locks (e.g. left by a
            # previous container run) — Chrome refuses to start if the hostname
            # in SingletonLock doesn't match the current machine, even when the
            # PID is dead, so we must remove the file rather than just skip it.
            try:
                os.unlink(lock_path)
                logger.info(f"Removed stale SingletonLock {lock_path} (PID {pid} no longer running)")
            except OSError:
                pass
            return False
        return True       # EPERM (pid exists, wrong owner) or EINVAL (not a symlink) → skip slot
    except (ValueError, IndexError):
        return True       # couldn't parse hostname-PID format → skip slot to be safe


def find_available_udd(base_udd: str, limit: int = 100) -> str:
    """Return the first user-data-dir variant not currently in use.

    Tries base_udd, then base_udd-2, base_udd-3 … base_udd-{limit}.
    Chrome creates the directory automatically if it does not exist yet,
    so there is no need to pre-create variants.
    """
    if not is_profile_in_use(base_udd):
        return base_udd
    for n in range(2, limit + 1):
        candidate = f"{base_udd}-{n}"
        if not is_profile_in_use(candidate):
            return candidate
    logger.warning(f"All {limit} user-data-dir slots for {base_udd} are in use, reusing slot {limit}")
    return f"{base_udd}-{limit}"


def getBrowserArgsFromQuery(query, dashdash=True):
    if dashdash:
        extra_args = []
    else:
        extra_args = {}
    from urllib.parse import urlparse, parse_qs
    parsed_url = urlparse(query)
    for k, v in parse_qs(parsed_url.query).items():
        if dashdash:
            if k.startswith('--'):
                extra_args.append(f"{k}={v[0]}")
        else:
            if not k.startswith('--'):
                extra_args[k] = v[0]
    return extra_args


async def launch_chrome(port=19222, url_query="", headful=False, websocket=None):
    query_args = getBrowserArgsFromQuery(url_query)
    chrome_location = get_cloak_binary()

    chrome_args = [
        "--allow-pre-commit-input",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-blink-features=AutomationControlled",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
        "--disable-dev-shm-usage",
        # Suppress UA-CH so sites fall back to the classic User-Agent header
        "--disable-features=AutofillServerCommunication,Translate,AcceptCHFrame,"
        "MediaRouter,OptimizationHints,Prerender2,UserAgentClientHint",
        "--disable-gpu",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-remote-fonts",
        "--disable-renderer-backgrounding",
        "--disable-search-engine-choice-screen",
        "--disable-sync",
        "--disable-web-security=true",
        "--enable-blink-features=IdleDetection",
        "--enable-features=NetworkServiceInProcess2",
        "--enable-logging=stderr",
        "--export-tagged-pdf",
        "--force-color-profile=srgb",
        "--hide-scrollbars",
        "--log-level=2",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
        "--no-sandbox",
        "--password-store=basic",
        "--use-mock-keychain",
        "--v1=1",
        f"--remote-debugging-port={port}",
        "about:blank",
    ]

    if not headful:
        chrome_args.append("--headless")
    else:
        chrome_args.extend([
            "--disable-infobars",
            "--disable-default-apps",
            "--disable-extensions-file-access-check",
            "--disable-plugins-discovery",
            "--disable-translate",
            "--disable-plugins",
            "--disable-geolocation",
        ])
        for flag in ["--disable-blink-features=AutomationControlled",
                     "--enable-blink-features=IdleDetection"]:
            if flag in chrome_args:
                chrome_args.remove(flag)

    chrome_args += build_fingerprint_args()
    chrome_args += build_webrtc_args()

    # If the caller supplied --user-data-dir, find the first available slot
    # (base, base-2, base-3 …) so concurrent sessions never collide on the
    # same profile directory and trigger Chrome's ProcessSingleton error.
    resolved_query_args = []
    caller_udd = None   # the resolved caller-supplied UDD, if any
    for arg in query_args:
        if arg.startswith('--user-data-dir='):
            base_udd = arg.split('=', 1)[1]
            caller_udd = find_available_udd(base_udd)
            if caller_udd != base_udd:
                logger.info(f"user-data-dir {base_udd!r} in use, using slot {caller_udd!r}")
            resolved_query_args.append(f'--user-data-dir={caller_udd}')
        else:
            resolved_query_args.append(arg)
    chrome_args += resolved_query_args

    if '--window-size' not in url_query:
        w, h = os.getenv('SCREEN_WIDTH'), os.getenv('SCREEN_HEIGHT')
        if w and h:
            chrome_args.append(f"--window-size={int(w)},{int(h)}")
        else:
            logger.warning("No --window-size in query, and no SCREEN_HEIGHT + SCREEN_WIDTH env vars found :-(")

    # Track which UDD this Chrome is using so cleanup can remove SingletonLock
    # after a SIGKILL (Chrome won't clean it up itself in that case).
    # For auto-created temp dirs we also optionally delete the whole dir.
    user_data_dir = caller_udd  # None if no --user-data-dir in query
    if caller_udd is None:
        try:
            user_data_dir = tempfile.mkdtemp(prefix="cloak-proxy", dir="/tmp")
            chrome_args.append(f"--user-data-dir={user_data_dir}")
            logger.debug(f"No user-data-dir in query, using {user_data_dir}")
        except Exception:
            logger.warning("Could not create temp user-data-dir, using default")
            chrome_args.append("--user-data-dir=/tmp/cloak-proxy-default")

    chrome_env = os.environ.copy()

    # asyncio.create_subprocess_exec reads stdout/stderr natively — no threads
    # are consumed for log reading regardless of how many browsers are running.
    try:
        if headful:
            xvfb_args = [
                "-a", "-s",
                "-screen 0 1920x1080x24 -ac +extension GLX +extension RANDR "
                "+extension RENDER +extension DAMAGE +extension XINERAMA "
                "+extension MIT-SHM +extension XTEST +extension SYNC "
                "-dpi 96 -fbdir /var/tmp",
            ]
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "xvfb-run", *xvfb_args, chrome_location, *chrome_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=chrome_env,
                ),
                timeout=20.0,
            )
        else:
            logger.debug(f"{chrome_location} {' '.join(chrome_args)}")
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    chrome_location, *chrome_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=chrome_env,
                ),
                timeout=20.0,
            )
    except asyncio.TimeoutError:
        logger.critical("Chrome process creation timed out after 20 seconds")
        raise RuntimeError("Chrome startup timed out")
    except FileNotFoundError:
        logger.critical(f"Chrome binary was not found at {chrome_location}, aborting!")
        raise
    except Exception as e:
        logger.critical(f"Unexpected error launching Chrome: {e}")
        raise RuntimeError(f"Chrome startup failed: {e}")

    # Attach the user_data_dir so cleanup_chrome_by_pid can remove it
    process.user_data_dir = user_data_dir

    async def log_stream(stream, log_func, prefix):
        try:
            async for line in stream:
                websocket_id = websocket.id if websocket else "unknown"
                log_func(f"WebSocket ID: {websocket_id} {prefix} PID {process.pid}: {line.decode(errors='replace').strip()}")
        except Exception as e:
            logger.warning(f"Error in log_stream for {prefix}: {e}")

    process.logging_tasks = [
        asyncio.create_task(log_stream(process.stdout, logger.debug, "Chrome stdout")),
        asyncio.create_task(log_stream(process.stderr, logger.critical, "Chrome stderr")),
    ]

    # Yield briefly so the event loop can detect an immediate exit
    await asyncio.sleep(0)
    if process.returncode is not None:
        logger.critical(f"Chrome process exited immediately with code {process.returncode}")

    return process


def reap_zombies():
    """Reap any zombie children of this process.

    Called after each Chrome cleanup and periodically from stats_thread_func.
    We only call waitpid on processes already in ZOMBIE state so we never
    accidentally steal an exit status from a live process asyncio is managing.
    On Python 3.12 + Linux >=5.3 asyncio uses PidfdChildWatcher (pidfd-based,
    not SIGCHLD/waitpid), so there is no interference. On older kernels asyncio
    uses ThreadedChildWatcher which calls waitpid(-1), but since we target only
    known zombies rather than calling waitpid(-1) ourselves, the risk is minimal.
    """
    reaped = 0
    try:
        me = psutil.Process(os.getpid())
        for child in me.children():
            try:
                if child.status() == psutil.STATUS_ZOMBIE:
                    os.waitpid(child.pid, os.WNOHANG)
                    reaped += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, ChildProcessError, OSError):
                pass
    except Exception:
        pass
    if reaped:
        logger.debug(f"reap_zombies: reaped {reaped} stray zombie(s)")


async def close_socket(websocket=None):
    logger.debug(f"WebSocket: {websocket.id} Closing websocket to puppeteer")
    try:
        await websocket.close()
    except Exception as e:
        logger.error(f"WebSocket: {websocket.id} - While closing - error: {e}")


async def stats_disconnect(time_at_start=0.0, websocket=None):
    global stats
    connection_semaphore.release()
    stats['connection_count'] -= 1
    logger.debug(f"Websocket {websocket.id} - Connection ended, processed in {time.time() - time_at_start:.3f}s")


async def _kill_and_reap_chrome(chrome_process, websocket_id: str):
    """Kill a Chrome process tree, reap all children, and clean up its user-data-dir.

    Does NOT touch any websocket — callers handle that themselves.
    """
    logger.debug(f"WebSocket ID: {websocket_id} Cleaning up Chrome subprocess PID {chrome_process.pid}")

    # Cancel log readers before killing — they hold stream references
    for task in getattr(chrome_process, 'logging_tasks', []):
        if not task.done():
            task.cancel()

    # Collect child PIDs *before* killing. Chrome uses the Zygote process
    # model: renderer, GPU, and crashpad processes are spawned as grandchildren
    # but get reparented directly to Python when the main Chrome exits.
    # We must call waitpid() on every one of them or they stay as zombies.
    child_pids = set()
    try:
        parent_process = psutil.Process(chrome_process.pid)
        procs = [parent_process] + parent_process.children(recursive=True)
        child_pids = {p.pid for p in procs if p.pid != chrome_process.pid}
        if procs:
            logger.debug(f"WebSocket ID: {websocket_id} - Killing {len(procs)} Chrome processes")
        for proc in procs:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        try:
            chrome_process.kill()
        except OSError:
            pass

    # Reap the asyncio-managed root process first
    try:
        await asyncio.wait_for(chrome_process.wait(), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"WebSocket ID: {websocket_id} - Error reaping main Chrome process: {e}")

    # Reap each reparented child. os.waitpid() blocks until exit, but since
    # we just sent SIGKILL this should return almost immediately.
    loop = asyncio.get_running_loop()
    for pid in child_pids:
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda p=pid: os.waitpid(p, 0)),
                timeout=3.0
            )
        except (asyncio.TimeoutError, ChildProcessError, OSError):
            pass  # already reaped or never existed as our child

    logger.debug(f"WebSocket ID: {websocket_id} - Reaped {len(child_pids) + 1} Chrome processes")

    # Sweep for late-spawned zombies. Chrome spawns crashpad processes lazily
    # and some get reparented to us after our psutil snapshot above — they
    # won't be in child_pids so the targeted loop above misses them.
    # We only call waitpid on processes already in ZOMBIE state, so we won't
    # accidentally steal an exit status from a process asyncio is still tracking.
    reap_zombies()

    udd = getattr(chrome_process, 'user_data_dir', None)
    if udd and os.path.isdir(udd):
        # Always remove SingletonLock after a SIGKILL — Chrome can't clean it
        # up itself, and a stale lock blocks the next session from using this slot.
        lock_path = os.path.join(udd, 'SingletonLock')
        try:
            os.unlink(lock_path)
            logger.debug(f"WebSocket ID: {websocket_id} - Removed SingletonLock from {udd}")
        except FileNotFoundError:
            pass  # already gone, that's fine
        except OSError as e:
            logger.warning(f"WebSocket ID: {websocket_id} - Could not remove SingletonLock from {udd}: {e}")

        # Delete the entire dir only for auto-created temp dirs and only when
        # explicitly enabled — keeping them lets Chrome reuse its disk cache.
        if udd.startswith('/tmp/cloak-proxy') and CLEANUP_PARALLEL_USER_DATA_DIR:
            try:
                shutil.rmtree(udd, ignore_errors=True)
                logger.debug(f"WebSocket ID: {websocket_id} - Removed auto-created user-data-dir {udd}")
            except Exception as e:
                logger.warning(f"WebSocket ID: {websocket_id} - Could not remove user-data-dir {udd}: {e}")


async def cleanup_chrome_by_pid(chrome_process, time_at_start=0.0, websocket=None):
    try:
        await _kill_and_reap_chrome(chrome_process, websocket_id=websocket.id)
    except Exception as e:
        logger.error(f"WebSocket ID: {websocket.id} - Error in Chrome cleanup: {e}")
    finally:
        await close_socket(websocket)


async def _request_retry(url, num_retries=20, websocket_id='unknown'):
    """Poll Chrome's /json/version endpoint until it responds.

    Uses aiohttp (already a dependency) instead of requests+executor so no
    thread pool slots are consumed while waiting for Chrome to start.
    """
    start_time = time.time()

    async with aiohttp.ClientSession() as session:
        for retry_count in range(num_retries):
            if time.time() - start_time > 60:
                logger.error(f"WebSocket ID: {websocket_id} - _request_retry exceeded 60s for {url}")
                raise aiohttp.ClientError("Overall retry timeout exceeded")

            sleep_time = min(0.05 * (2 ** retry_count), 3.0)
            await asyncio.sleep(sleep_time)

            current_timeout = aiohttp.ClientTimeout(total=min(5 + retry_count * 0.5, 15))
            logger.debug(f"WebSocket ID: {websocket_id} - attempt {retry_count+1}/{num_retries} for {url}")

            try:
                async with session.get(url, timeout=current_timeout) as resp:
                    if resp.status == 200:
                        elapsed = time.time() - start_time
                        logger.debug(f"WebSocket ID: {websocket_id} - connected after {retry_count+1} attempts in {elapsed:.2f}s")
                        return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            except Exception as e:
                logger.warning(f"WebSocket ID: {websocket_id} - unexpected error: {e}")
                continue

    raise aiohttp.ClientError(f"Failed to connect to {url} after {num_retries} attempts")



async def _idle_watchdog(last_activity: list, client_websocket, websocket_id: str,
                         idle_timeout: int, proxy_tasks: list):
    """Cancel proxy tasks and close the client websocket after *idle_timeout* seconds
    of no inbound or outbound CDP traffic.

    *last_activity* is a single-element list containing the epoch float of the
    most recent message (mutable so both proxy coroutines can update it).
    *proxy_tasks* is a list of asyncio.Task objects to cancel on idle expiry.
    """
    if idle_timeout <= 0:
        return
    poll_interval = min(10, idle_timeout // 2 or 1)
    while True:
        await asyncio.sleep(poll_interval)
        elapsed = time.time() - last_activity[0]
        if elapsed >= idle_timeout:
            logger.critical(
                f"WebSocket ID: {websocket_id} - No CDP activity for {elapsed:.0f}s "
                f"(threshold {idle_timeout}s, remote={getattr(client_websocket, 'remote_address', '?')}). "
                f"Closing idle connection."
            )
            for task in proxy_tasks:
                if not task.done():
                    task.cancel()
            try:
                await client_websocket.close()
            except Exception:
                pass
            return


async def debug_log_line(logfile_path, text):
    if logfile_path is None:
        return
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _write_log_line(logfile_path, text)),
            timeout=1.0
        )
    except asyncio.TimeoutError:
        logger.warning(f"Log file write timed out for {logfile_path}")
    except Exception as e:
        logger.warning(f"Error writing to log file {logfile_path}: {e}")


def _write_log_line(logfile_path, text):
    with open(logfile_path, 'a') as f:
        f.write(f"{time.time()} - {text}\n")


async def launchPuppeteerChromeProxy(websocket):
    '''Called whenever a new connection is made to the server. Launches a
    CloakBrowser instance and proxies CDP between the client and that browser.'''
    global stats
    global connection_count_max

    path = websocket.request.path  # websockets >=14 API

    now = time.time()
    stats['connection_count_total'] += 1
    logger.debug(
        f"WebSocket ID: {websocket.id} Got new incoming connection from "
        f"{websocket.remote_address[0]}:{websocket.remote_address[1]} ({path})")

    # Enforce memory floor before even queuing the connection.
    # available = free + reclaimable page cache; a much better signal than %.
    available_mb = psutil.virtual_memory().available // (1024 * 1024)
    if available_mb < min_available_memory_mb:
        logger.warning(
            f"WebSocket ID: {websocket.id} - Only {available_mb} MB available "
            f"(floor {min_available_memory_mb} MB), rejecting connection")
        stats['dropped_threshold_reached'] += 1
        await close_socket(websocket)
        return

    # Semaphore: asyncio-native, no polling loop needed
    if stats['connection_count'] >= connection_count_max:
        if DROP_EXCESS_CONNECTIONS:
            logger.warning(f"WebSocket ID: {websocket.id} - At capacity ({connection_count_max}), waiting for slot...")
            try:
                await asyncio.wait_for(connection_semaphore.acquire(), timeout=120.0)
            except asyncio.TimeoutError:
                logger.critical(f"WebSocket ID: {websocket.id} - Waited too long for a slot, dropping")
                stats['dropped_waited_too_long'] += 1
                await close_socket(websocket)
                return
        else:
            logger.warning(f"WebSocket ID: {websocket.id} - Rejecting connection, at capacity ({connection_count_max})")
            stats['dropped_threshold_reached'] += 1
            await close_socket(websocket)
            return
    else:
        await connection_semaphore.acquire()

    stats['connection_count'] += 1
    closed = asyncio.create_task(websocket.wait_closed())
    closed.add_done_callback(lambda task: asyncio.create_task(stats_disconnect(time_at_start=now, websocket=websocket)))

    now_before_chrome_launch = time.time()

    args = getBrowserArgsFromQuery(path, dashdash=False)
    headful_mode = (
        args.get('headful', '').lower() in ['true', '1'] or
        os.getenv('CHROME_HEADFUL', 'false').lower() in ['true', '1']
    )
    debug_log = args.get('log-cdp') if args.get('log-cdp') and strtobool(os.getenv('ALLOW_CDP_LOG', 'False')) else None

    if debug_log and os.path.isfile(debug_log):
        os.unlink(debug_log)

    port = next(port_selector)

    try:
        chrome_process = await launch_chrome(port=port, url_query=path, headful=headful_mode, websocket=websocket)
    except Exception as e:
        logger.critical(f"WebSocket ID: {websocket.id} - Chrome launch failed: {e}")
        stats['chrome_start_failures'] += 1
        # close_socket triggers wait_closed() → stats_disconnect callback, which
        # releases the semaphore and decrements connection_count.  Do NOT do it
        # here again or we get a double-release / negative counter.
        await close_socket(websocket)
        return

    closed.add_done_callback(lambda task: asyncio.create_task(
        cleanup_chrome_by_pid(chrome_process=chrome_process, time_at_start=now, websocket=websocket))
    )

    chrome_json_info_url = f"http://localhost:{port}/json/version"
    try:
        chrome_info = await _request_retry(chrome_json_info_url, websocket_id=websocket.id)
    except aiohttp.ClientError as e:
        logger.critical(f"WebSocket ID: {websocket.id} - Chrome did not start! Need --cap-add=SYS_ADMIN? Disk full?")
        logger.critical(f"WebSocket ID: {websocket.id} - While connecting to {chrome_json_info_url} - {e}")
        stats['chrome_start_failures'] += 1
        for task in getattr(chrome_process, 'logging_tasks', []):
            if not task.done():
                task.cancel()
        chrome_process.kill()
        try:
            await asyncio.wait_for(chrome_process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass
        # close_socket triggers wait_closed() → stats_disconnect callback which
        # handles semaphore release and counter decrement.  Same for
        # cleanup_chrome_by_pid — both are registered callbacks on `closed`.
        await close_socket(websocket)
        return

    logger.trace(f"WebSocket ID: {websocket.id} time to launch browser {time.time() - now_before_chrome_launch:.3f}s")

    chrome_websocket_url = chrome_info.get("webSocketDebuggerUrl")
    logger.debug(f"WebSocket ID: {websocket.id} proxying to local Chrome instance via CDP {chrome_websocket_url}")

    last_activity = [time.time()]
    try:
        await debug_log_line(text=f"Attempting connection to {chrome_websocket_url}", logfile_path=debug_log)
        async with websockets.connect(chrome_websocket_url, max_size=None, max_queue=None, ping_interval=20, ping_timeout=10) as ws:
            await debug_log_line(text=f"Connected to {chrome_websocket_url}", logfile_path=debug_log)
            taskA = asyncio.create_task(hereToChromeCDP(puppeteer_ws=ws, chrome_websocket=websocket, debug_log=debug_log, last_activity=last_activity))
            taskB = asyncio.create_task(puppeteerToHere(puppeteer_ws=ws, chrome_websocket=websocket, debug_log=debug_log, last_activity=last_activity))
            watchdog = asyncio.create_task(_idle_watchdog(last_activity, websocket, websocket.id, IDLE_TIMEOUT_SECONDS, [taskA, taskB]))
            try:
                await taskA
                await taskB
            finally:
                if not watchdog.done():
                    watchdog.cancel()
    except Exception as e:
        for task in getattr(chrome_process, 'logging_tasks', []):
            if not task.done():
                task.cancel()
        chrome_process.kill()
        try:
            await asyncio.wait_for(chrome_process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass
        txt = f"Something bad happened connecting to Chrome CDP at {chrome_websocket_url} - '{e}'"
        logger.error(f"WebSocket ID: {websocket.id} - " + txt)
        await debug_log_line(text="Exception: " + txt, logfile_path=debug_log)

    logger.success(f"Websocket {websocket.id} - Connection done!")
    await debug_log_line(text=f"Websocket {websocket.id} - Connection done!", logfile_path=debug_log)


async def hereToChromeCDP(puppeteer_ws, chrome_websocket, debug_log=None, last_activity=None):
    try:
        async for message in puppeteer_ws:
            if last_activity is not None:
                last_activity[0] = time.time()
            if debug_log:
                await debug_log_line(text=f"Chrome -> Puppeteer: {message[:1000]}", logfile_path=debug_log)
            logger.trace(message[:1000])

            if 'SOCKPUPPET.specialcounter' in message[:200] and puppeteer_ws.id not in stats['special_counter']:
                stats['special_counter'].append(puppeteer_ws.id)

            await chrome_websocket.send(message)
    except websockets.exceptions.ConnectionClosed:
        logger.debug(f"WebSocket ID: {puppeteer_ws.id} - Connection closed normally while sending")
    except Exception as e:
        logger.error(f"WebSocket ID: {puppeteer_ws.id} - Error in hereToChromeCDP: {e}")


async def puppeteerToHere(puppeteer_ws, chrome_websocket, debug_log=None, last_activity=None):
    try:
        async for message in chrome_websocket:
            if last_activity is not None:
                last_activity[0] = time.time()
            if debug_log:
                await debug_log_line(text=f"Puppeteer -> Chrome: {message[:1000]}", logfile_path=debug_log)
            logger.trace(message[:1000])

            if message.startswith("{") and message.endswith("}") and 'Page.navigate' in message:
                try:
                    m = orjson.loads(message)
                    logger.debug(f"{chrome_websocket.id} Page.navigate request called to '{m['params']['url']}'")
                except (orjson.JSONDecodeError, KeyError):
                    pass

            await puppeteer_ws.send(message)
    except websockets.exceptions.ConnectionClosed:
        logger.debug(f"WebSocket ID: {chrome_websocket.id} - Connection closed normally while receiving")
    except Exception as e:
        logger.error(f"WebSocket ID: {chrome_websocket.id} - Error in puppeteerToHere: {e}")


async def stats_thread_func():
    while True:
        try:
            vm = psutil.virtual_memory()
            available_mb = vm.available // (1024 * 1024)
            total_mb = vm.total // (1024 * 1024)
            logger.info(
                f"Connections: Active {stats['connection_count']} of max {connection_count_max}, "
                f"Total processed: {stats['connection_count_total']}. "
                f"RAM: {available_mb} MB available of {total_mb} MB total "
                f"(floor {min_available_memory_mb} MB)"
            )
            if available_mb < min_available_memory_mb:
                logger.warning(f"Available RAM {available_mb} MB below floor {min_available_memory_mb} MB — new connections will be rejected")

            # Periodic safety-net sweep for any zombies missed by cleanup
            reap_zombies()

            try:
                parent = psutil.Process(os.getpid())
                child_count = len(parent.children(recursive=False))
                logger.info(f"Process info: {child_count} child processes")
            except Exception as e:
                logger.warning(f"Process count check failed: {e}")

        except Exception as e:
            logger.error(f"Unexpected error in stats thread: {e}")

        await asyncio.sleep(stats_refresh_time)


if __name__ == '__main__':
    logger_level = os.getenv('LOG_LEVEL', 'DEBUG')
    logger.remove()

    try:
        log_level_for_stdout = {'DEBUG', 'SUCCESS'}
        logger.configure(handlers=[
            {"sink": sys.stdout, "level": logger_level,
             "filter": lambda record: record['level'].name in log_level_for_stdout},
            {"sink": sys.stderr, "level": logger_level,
             "filter": lambda record: record['level'].name not in log_level_for_stdout},
        ])
    except ValueError:
        print("Available log level names: TRACE, DEBUG(default), INFO, SUCCESS, WARNING, ERROR, CRITICAL")
        sys.exit(2)

    parser = argparse.ArgumentParser(description='CloakBrowser CDP WebSocket proxy.')
    parser.add_argument('--host', help='Host to bind to.', default='0.0.0.0')
    parser.add_argument('--port', help='Port to bind to.', default=3000, type=int)
    parser.add_argument('--sport', help='Port for HTTP statistics /stats endpoint.', default=8080, type=int)
    args = parser.parse_args()

    if STARTUP_DELAY:
        logger.info(f"Start-up delay {STARTUP_DELAY} seconds...")
        time.sleep(STARTUP_DELAY)

    try:
        from cloakbrowser import ensure_binary
        ensure_binary()
        cloak_binary = get_cloak_binary()
        logger.success(f"CloakBrowser binary: {cloak_binary}")
    except Exception as e:
        logger.warning(f"Could not pre-check CloakBrowser binary: {e}")
        cloak_binary = get_cloak_binary()

    async def main():
        await start_http_server(host=args.host, port=args.sport, stats=stats)
        asyncio.create_task(stats_thread_func())
        async with websockets.serve(launchPuppeteerChromeProxy, args.host, args.port, ping_interval=20, ping_timeout=10):
            logger.success(f"Starting CloakBrowser CDP proxy, listening on ws://{args.host}:{args.port} -> {cloak_binary}")
            logger.success(f"WebRTC blocking: {os.getenv('BLOCK_WEBRTC', 'true')} | Fingerprint platform: {os.getenv('FINGERPRINT_PLATFORM', 'windows')} | Backend: {os.getenv('CLOAKBROWSER_BACKEND', 'patchright')}")
            await asyncio.Future()  # run forever

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.success("Got CTRL+C/interrupt, shutting down.")
