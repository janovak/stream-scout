#!/usr/bin/env python3
"""Patch installed pyTwitchAPI's Chat.leave_room() to bound its wait with a
timeout, mirroring the leave_room() sibling join_room()'s existing
join_timeout. We depend on plain PyPI twitchAPI, not a fork, so this is
applied to the installed package at image build time instead.

Without this, leave_room() waits on _room_leave_locks with no deadline: if
the underlying connection dies mid-wait, the PART confirmation that would
clear the lock never arrives and the call hangs forever, permanently
blocking poll_top_streams (max_instances=1) on every future scheduled run.

Upstream PR with the same fix: https://github.com/Teekeks/pyTwitchAPI/pull/364
(closed, unmerged -- patching locally instead of waiting on it or forking).
"""
import sys
from pathlib import Path

TARGET = Path("/usr/local/lib/python3.11/site-packages/twitchAPI/chat/__init__.py")

text = TARGET.read_text()

if "leave_timeout" in text:
    print("twitchAPI already patched, skipping")
    sys.exit(0)

old_attr = (
    '        self.join_timeout: int = 10\n'
    '        """Time in seconds till a channel join attempt times out"""\n'
)
new_attr = old_attr + (
    '        self.leave_timeout: int = 10\n'
    '        """Time in seconds till a channel leave attempt times out"""\n'
)

old_method = '''    async def leave_room(self, chat_rooms: Union[List[str], str]):
        """leave one or more chat rooms\\n
        Will only exit once all given chat rooms where successfully left

        :param chat_rooms: The room or rooms you want to leave"""
        if isinstance(chat_rooms, str):
            chat_rooms = [chat_rooms]
        room_str = ','.join([f'#{c}'.lower() if c[0] != '#' else c.lower() for c in chat_rooms])
        target = [c[1:].lower() if c[0] == '#' else c.lower() for c in chat_rooms]
        for r in target:
            self._room_leave_locks.append(r)
        await self._send_message(f'PART {room_str}')
        for x in target:
            if x in self._join_target:
                self._join_target.remove(x)
        # wait to leave all rooms
        while any([r in self._room_leave_locks for r in target]):
            await asyncio.sleep(0.01)
'''

new_method = '''    async def leave_room(self, chat_rooms: Union[List[str], str]):
        """leave one or more chat rooms\\n
        Will only exit once all given chat rooms where successfully left or :const:`twitchAPI.chat.Chat.leave_timeout` run out.

        :param chat_rooms: The room or rooms you want to leave
        :returns: list of channels that could not be left
        """
        if isinstance(chat_rooms, str):
            chat_rooms = [chat_rooms]
        room_str = ','.join([f'#{c}'.lower() if c[0] != '#' else c.lower() for c in chat_rooms])
        target = [c[1:].lower() if c[0] == '#' else c.lower() for c in chat_rooms]
        for r in target:
            self._room_leave_locks.append(r)
        await self._send_message(f'PART {room_str}')
        for x in target:
            if x in self._join_target:
                self._join_target.remove(x)
        # wait to leave all rooms
        timeout = datetime.datetime.now() + datetime.timedelta(seconds=self.leave_timeout)
        while any([r in self._room_leave_locks for r in target]) and timeout > datetime.datetime.now():
            await asyncio.sleep(0.01)
        failed_to_leave = [r for r in self._room_leave_locks if r in target]
        for r in failed_to_leave:
            self._room_leave_locks.remove(r)
        return failed_to_leave
'''

if old_attr not in text:
    print("ERROR: expected join_timeout attribute block not found; twitchAPI version may have changed", file=sys.stderr)
    sys.exit(1)
if old_method not in text:
    print("ERROR: expected leave_room method body not found; twitchAPI version may have changed", file=sys.stderr)
    sys.exit(1)

text = text.replace(old_attr, new_attr, 1)
text = text.replace(old_method, new_method, 1)
TARGET.write_text(text)
print("Patched Chat.leave_room() with leave_timeout")
