"""How Annie reaches Aliyun and the Room: directly, never through a desktop proxy.

websockets 15 follows the system proxy by default. On 2026-10-21 the brain host
had Clash/V2Ray on: Realtime and TTS sockets went through HTTPProxyConnection
and dropped ("no close frame received or sent", 21 times in 20 minutes), timed
out in the opening handshake, and hit websockets' own proxy race
(InvalidStateError). Aliyun is in mainland China and the Room is on the LAN, so
neither needs a proxy. ANNIE_CLOUD_PROXY (e.g. http://127.0.0.1:7890) restores
one for Aliyun only, for a network that truly needs it.
"""

import functools
import os

from websockets.asyncio.client import connect


def cloud_proxy():
    """The proxy for Aliyun sockets: None (direct) unless ANNIE_CLOUD_PROXY is set."""
    return os.environ.get("ANNIE_CLOUD_PROXY") or None


# The Room is always on the LAN.
room_connect = functools.partial(connect, proxy=None)
