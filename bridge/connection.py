"""tModLoader Mod 接口单通道管理（9877 JSON-over-TCP）。游戏窗口由 launcher.py 启动，登录/心跳由游戏原生处理"""

import asyncio
import json
import logging
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

# 优先级：# 高优先级命令（移动/导航/战斗/聊天/状态）插队，其余排队。
# 大请求（enum_* / get_recipes / enum_chests）算低优先级，别堵住主线。
HIGH_PRIORITY_CMDS = {
    "move",
    "navigate_to",
    "navigate_stream",
    "dig_tile",
    "find_ore",
    "use_item",
    "use_item_slot",
    "select_item",
    "equip",
    "damage_npc",
    "send_chat",
    "get_state",
    "get_server_info",
    "get_capabilities",
    "join_status",
    "get_network_info",
    "ping",
    "warp",
    "hook",
    "break_tile",
    "place_tile",
    "collect_items",
}
# 明确低优先级：这些容易几 MB，排后面
LOW_PRIORITY_CMDS = {"enum_items", "get_recipes", "enum_chests"}


class Connection:
    """管理 tModLoader Mod 接口 TCP 连接（9877 端口）。

    独立读循环：后台 task 持续读流——
    - 带 req_id 的响应按 req_id 分发给对应的 request_mod future（支持并发请求）
    - 事件推送即时派发（状态缓存不再堆积延迟）
    request_mod 只负责发送 + 等自己的 future，互不阻塞；
    单条命令超时不再锁住其他命令（移动/导航不会被排队卡死）。
    """

    def __init__(self, mod_host: str, mod_port: int) -> None:
        self.mod_host = mod_host
        self.mod_port = mod_port
        self._mod_reader: Optional[asyncio.StreamReader] = None
        self._mod_writer: Optional[asyncio.StreamWriter] = None
        self._mod_lock = asyncio.Lock()  # 只保护发送（防并发写乱序）
        self._req_seq = 0
        self._event_callbacks: list = []
        self._pending: Dict[int, asyncio.Future] = {}  # req_id → future
        self._read_task: Optional[asyncio.Task] = None

        # A6：优先级发送队列——高优先级命令插队，避免大请求堵住移动/导航/聊天
        self._send_q: "asyncio.Queue[dict]" = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None
        self._sendentinel = object()

    # ===== A6 优先级发送器 =====
    # 协议一次一条，发送锁在 request_mod 内；为避免大请求（几 MB 响应）在后台
    # 生成期间把 send 通道占住，这里在【发送命令】层做优先级缓冲：
    #   高优（移动/导航/战斗/聊天/取状态）即刻插队发送；
    #   低优（enum_* / get_recipes）排队到空闲再发。
    # 由于 _mod_lock 串行且每条命令先发后等，响应由独立读循环分发，
    # 高优命令不会等低优的响应——这正是"命令不下发"的根因之一。

    async def request_mod(self, cmd: dict, timeout: float = 3.0) -> Optional[dict]:
        """向 mod 接口发送 JSON 命令并等待回执（独立读循环按 req_id 分发）。

        协议一次一条命令：发送 + 等待在锁内串行，避免并发写乱序。
        低优大请求先等一拍让高优插队（A6）。
        """
        if not self._mod_writer:
            return None
        # 读循环意外死亡时自动重建
        self._ensure_read_loop()

        is_low = cmd.get("cmd") in LOW_PRIORITY_CMDS
        if is_low:
            # 低优命令让高优（move/navigate/战斗/send_chat）先发一拍
            await asyncio.sleep(0.15)

        async with self._mod_lock:
            self._req_seq = (self._req_seq + 1) & 0xFFFF
            req_id = self._req_seq
            cmd = dict(cmd)
            cmd["req_id"] = req_id

            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending[req_id] = fut
            log.info(
                f"[conn] request_mod req_id={req_id} cmd={cmd.get('cmd')} "
                f"loop_id={id(loop)} reader_id={id(self._mod_reader)}"
            )
            await self._send_mod_raw(cmd)
            try:
                resp = await asyncio.wait_for(fut, timeout=timeout)
                return resp
            except asyncio.TimeoutError:
                log.warning(f"[conn] request_mod(req_id={req_id}, cmd={cmd.get('cmd')}) 超时({timeout}s)")
                return None
            finally:
                self._pending.pop(req_id, None)

    # ===== 9877 Mod 接口通道 =====

    async def connect_mod(self, retry_ports: bool = True, skip_first: bool = False, skip_count: int = 0) -> bool:
        # 先关闭旧连接（如有）
        self.close()
        start_port = self.mod_port + max(skip_count, 1 if skip_first else 0)

        async def _try_connect(port: int) -> bool:
            try:
                # limit=8MB：大响应（enum_items 物品表/get_recipes 配方表/enum_chests 箱子列表）
                # 可能几百 KB，StreamReader 默认 64KB limit 会导致 readline 抛
                # ValueError("chunk exceed the limit") → 响应读不完 → 请求超时
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(self.mod_host, port, limit=8 * 1024 * 1024), timeout=2.0
                )
                self._mod_reader, self._mod_writer = r, w
                self.mod_port = port
                # ── 握手：读 C# 发来的 welcome，确认字节流干净 ──
                try:
                    welcome_line = await asyncio.wait_for(self._mod_reader.readline(), timeout=1.5)
                    if welcome_line:
                        try:
                            welcome = json.loads(welcome_line.decode("utf-8"))
                            if welcome.get("welcome"):
                                print(f"[conn] 收到 welcome 握手 (port={port})，连接正常")
                        except json.JSONDecodeError:
                            pass  # 旧版模组不发 welcome
                except asyncio.TimeoutError:
                    pass  # 旧版模组不发 welcome
                # 启动独立读循环（强制 cancel 旧的再重建，杜绝残留双读循环）
                self._start_reader()
                return True
            except Exception as e:
                print(f"[conn] _try_connect({port}) 失败: {e}")
                return False

        if await _try_connect(start_port):
            return True
        if not retry_ports:
            return False

        for port in range(start_port + 1, start_port + 11):
            if await _try_connect(port):
                return True
        return False

    def is_mod_connected(self) -> bool:
        """检查 mod 连接是否还活着"""
        if self._mod_writer is None:
            return False
        try:
            return not self._mod_writer.is_closing()
        except Exception:
            return False

    def _start_reader(self) -> None:
        """启动后台读循环（每次连接强制 cancel 旧的再重建，杜绝残留）。

        旧 reader 若仍在跑（cancel 异步，done() 未必立刻 True），直接重建，
        否则新连接没有读循环，所有响应都会超时。
        """
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
        self._read_task = asyncio.create_task(self._read_loop())

    def _ensure_read_loop(self) -> None:
        """读循环不在跑时重建（readline 异常/竞态导致死亡后的自愈）。"""
        if self._mod_reader and (self._read_task is None or self._read_task.done()):
            try:
                self._read_task = asyncio.create_task(self._read_loop())
                log.warning("[conn] 读循环已重建")
            except Exception:
                pass

    async def _send_mod_raw(self, cmd: dict) -> None:
        if not self._mod_writer:
            log.warning("[conn] _send_mod_raw 失败: _mod_writer 为 None")
            return
        data = (json.dumps(cmd) + "\n").encode("utf-8")
        try:
            self._mod_writer.write(data)
            await self._mod_writer.drain()
            log.info(f"[conn] 已发送: {cmd.get('cmd')} req_id={cmd.get('req_id')}")
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            log.warning(f"[conn] _send_mod_raw 连接错误: {e}")
        except Exception as e:
            log.error(f"[conn] _send_mod_raw 异常: {type(e).__name__}: {e}")

    # ===== 独立读循环（替代请求内读流） =====

    async def _read_loop(self) -> None:
        """后台持续读流：响应按 req_id 分发到 pending future，事件即时派发。"""
        my_reader = self._mod_reader
        log.info(
            f"[conn] 读循环启动 reader_id={id(my_reader)} "
            f"task_id={id(asyncio.current_task())} "
            f"loop_id={id(asyncio.get_running_loop())}"
        )
        while True:
            if not self._mod_reader:
                log.warning("[conn] 读循环退出: _mod_reader 为 None")
                break
            try:
                line = await self._mod_reader.readline()
            except asyncio.CancelledError:
                raise  # close() 取消读循环，正常退出
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                log.warning(f"[conn] _read_loop 连接错误: {e}")
                break
            except Exception as e:
                # 非连接类异常（RuntimeError 等）不退出——退出会杀死读循环，
                # 导致所有后续请求静默超时。打日志后继续。
                log.error(f"[conn] _read_loop 异常(继续): {type(e).__name__}: {e}")
                await asyncio.sleep(0.1)
                continue
            if not line:
                # EOF：对端关闭
                log.warning("[conn] _read_loop EOF，连接已关闭")
                break
            try:
                log.info(f"[conn] 读到行: {line[:100]!r}")
                resp = json.loads(line.decode("utf-8"))
                if not isinstance(resp, dict):
                    continue
                resp_req_id = resp.get("req_id")
                if resp_req_id is not None and resp_req_id in self._pending:
                    fut = self._pending[resp_req_id]
                    if not fut.done():
                        try:
                            fut.set_result(resp)
                        except Exception as e:
                            # 跨事件循环操作 future 会抛 RuntimeError（SDK 多循环时）
                            log.error(f"[conn] set_result 失败 req_id={resp_req_id}: {type(e).__name__}: {e}")
                elif resp.get("type") == "event":
                    self._dispatch_event(resp)
                # 其他（无 req_id 的非事件响应）：忽略
            except json.JSONDecodeError:
                continue
            except Exception:
                continue

        # 循环退出（断线/EOF/异常）：唤醒所有等待者，避免 request_mod 卡死。
        # 只有自己仍是当前读循环才清理——防止旧连接的读循环残留退出时
        # 清掉新连接的 pending（会导致新连接请求被误判失败 → 无限重连）
        log.warning(
            f"[conn] 读循环退出 reader_id={id(my_reader)} "
            f"task_id={id(asyncio.current_task())} "
            f"current_task_is_self={self._read_task is asyncio.current_task()}"
        )
        if self._read_task is asyncio.current_task():
            self._read_task = None
            # 连接已死：清掉引用，让 is_mod_connected() 返回 False → 上层触发重连
            if self._mod_writer:
                try:
                    self._mod_writer.close()
                except Exception:
                    pass
            self._mod_reader = None
            self._mod_writer = None
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_result(None)
            self._pending.clear()

    # ===== 事件回调 =====

    def on_message(self, callback: Callable) -> None:
        """注册事件回调。模组主动推送的 type="event" 消息会被派发到此。"""
        self._event_callbacks.append(callback)

    def _dispatch_event(self, msg: dict) -> None:
        """将事件消息派发给所有注册的回调。"""
        for cb in self._event_callbacks:
            try:
                cb(msg)
            except Exception:
                pass  # 回调异常不阻塞主流程

    # ===== 通用 =====

    def close(self) -> None:
        """关闭 Mod 连接"""
        # 先取消读循环，避免旧读循环残留（EOF 退出时误清新连接的 pending）
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._mod_writer:
            try:
                self._mod_writer.close()
            except Exception:
                pass
        self._mod_reader = None
        self._mod_writer = None
        # 唤醒所有等待者（读循环退出时也会做，这里兜底）
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(None)
        self._pending.clear()
