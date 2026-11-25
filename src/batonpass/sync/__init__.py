"""Tiered, append-only sync between machines.

A channel is a folder: a shared mount, a git remote, a USB stick, or a relay
mailbox. Each device writes only its own stream, so there is no lock, no
leader, and no merge. Pull is a resumable tail read, and the library stays
fully queryable while the channel is offline.
"""

from .channel import device_name, init_channel, is_channel
from .pull import pull
from .push import push
from .status import status

__all__ = ["device_name", "init_channel", "is_channel",
           "push", "pull", "status"]
