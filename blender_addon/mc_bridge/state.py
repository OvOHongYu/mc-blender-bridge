# -*- coding: utf-8 -*-
"""运行时状态（单例）。bpy 侧与测试共用的轻量容器。"""
import threading

_lock = threading.Lock()
_client = None
_scheduler = None
_info = None            # /api/ping 结果
_blocks = None          # /api/blocks 结果
_timer_on = False
_frame_hook_on = False


def set_runtime(client, scheduler, info, blocks):
    global _client, _scheduler, _info, _blocks
    with _lock:
        _client, _scheduler = client, scheduler
        _info, _blocks = info, blocks


def scheduler():
    return _scheduler


def client():
    return _client


def info():
    return _info


def blocks():
    return _blocks


def set_timers(timer_on=False, frame_hook_on=False):
    global _timer_on, _frame_hook_on
    _timer_on, _frame_hook_on = timer_on, frame_hook_on


def timers():
    return _timer_on, _frame_hook_on


def clear():
    global _client, _scheduler, _info, _blocks, _timer_on, _frame_hook_on
    with _lock:
        _client = _scheduler = _info = _blocks = None
        _timer_on = _frame_hook_on = False
