import psutil
from aiohttp import web
from loguru import logger
import os
import asyncio
from functools import partial

async def handle_http_request(request, stats):
    try:
        loop = asyncio.get_event_loop()
        TIMEOUT = 3

        try:
            svmem = await asyncio.wait_for(
                loop.run_in_executor(None, psutil.virtual_memory),
                timeout=TIMEOUT
            )

            parent = psutil.Process(os.getpid())
            get_child_count = lambda: len(parent.children(recursive=False))
            child_count = await asyncio.wait_for(
                loop.run_in_executor(None, get_child_count),
                timeout=TIMEOUT
            )

            mem_use_percent = svmem.percent

        except asyncio.TimeoutError:
            logger.warning("System monitoring calls timed out in stats endpoint")
            child_count = stats.get('last_child_count', 0)
            mem_use_percent = stats.get('last_mem_percent', 0)
        else:
            stats['last_child_count'] = child_count
            stats['last_mem_percent'] = mem_use_percent

        data = {
            'active_connections': stats['connection_count'],
            'child_count': child_count,
            'connection_count_total': stats['connection_count_total'],
            'dropped_threshold_reached': stats['dropped_threshold_reached'],
            'dropped_waited_too_long': stats['dropped_waited_too_long'],
            'mem_use_percent': mem_use_percent,
            'special_counter_len': len(stats['special_counter']),
            'chrome_start_failures': stats['chrome_start_failures']
        }

        return web.json_response(data, content_type='application/json')
    except asyncio.TimeoutError:
        logger.warning("Stats request timed out, returning partial data")
        return web.json_response({
            'active_connections': stats['connection_count'],
            'connection_count_total': stats['connection_count_total'],
            'error': 'request_timeout'
        }, content_type='application/json')
    except Exception as e:
        logger.error(f"Error in stats endpoint: {str(e)}")
        return web.json_response({
            'error': 'internal_error',
            'message': str(e)
        }, status=500, content_type='application/json')


async def start_http_server(host, port, stats):
    app = web.Application(client_max_size=1024)
    app.router.add_get('/stats', lambda req: handle_http_request(req, stats))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port, backlog=20)
    await site.start()
    logger.success(f"HTTP stats info server running at http://{host}:{port}/stats")
