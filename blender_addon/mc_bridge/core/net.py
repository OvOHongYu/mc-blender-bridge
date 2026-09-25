# -*- coding: utf-8 -*-
"""HTTP API 客户端（纯 Python，无 bpy 依赖）。

线程安全：每线程一条 keep-alive 长连接（http.client），避免每请求
TCP 握手/断开的开销；连接失效自动重建并重试。"""
import http.client
import json
import threading
import time
import urllib.parse

from . import codec

TIMEOUT = 15.0


class ApiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class ApiClient:
    def __init__(self, host="127.0.0.1", port=8788, timeout=TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base = f"http://{host}:{port}"
        self.server_info = None
        self._tls = threading.local()      # 每线程独立 keep-alive 连接

    # ------------------------------------------------------------- 底层 ----
    def _conn(self):
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = http.client.HTTPConnection(self.host, self.port,
                                              timeout=self.timeout)
            self._tls.conn = conn
        return conn

    def _drop_conn(self):
        try:
            self._tls.conn.close()
        except Exception:
            pass
        self._tls.conn = None

    def _request(self, path, params=None, method="GET", data=None, retries=2):
        target = path
        if params:
            target += "?" + urllib.parse.urlencode(params)
        headers = {"Host": f"{self.host}:{self.port}"}
        if data is not None:
            headers["Content-Length"] = str(len(data))
        last = None
        for attempt in range(retries + 1):
            try:
                conn = self._conn()
                conn.request(method, target, body=data, headers=headers)
                r = conn.getresponse()
                body = r.read()
                return r.status, r.msg, body
            except (http.client.HTTPException, ConnectionError, TimeoutError,
                    OSError) as e:
                last = e
                self._drop_conn()          # 长连接可能被服务端关闭，重建重试
                if attempt < retries:
                    time.sleep(0.2 * (attempt + 1))
        raise ApiError(f"无法连接 {self.base}: {last}")

    def _binary(self, path, params):
        status, headers, data = self._request(path, params)
        if status != 200:
            raise ApiError(f"{path} -> {status}")
        enc = headers.get("X-MCB-Encoding")
        if enc and int(enc) == 2:
            data = codec.decompress(data)
        return data

    # ------------------------------------------------------------- API ----
    def ping(self):
        status, _, data = self._request("/api/ping")
        info = json.loads(data)
        self.server_info = info
        return info

    def blocks(self):
        status, _, data = self._request("/api/blocks")
        return json.loads(data)

    def player(self):
        """玩家状态（模组 >= 1.0.0-player）：{dim,x,y,z,yaw,pitch} 或 {player:None}。"""
        status, _, data = self._request("/api/player")
        return json.loads(data)

    def set_player(self, x, y, z, yaw, pitch):
        """把玩家传送到指定位置与视角（相机 -> 玩家 同步）。"""
        status, _, data = self._request(
            "/api/player", {"x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch},
            method="POST")
        return json.loads(data)

    def chunk(self, dim, cx, cz, ymin, ymax):
        buf = self._binary("/api/chunk", {"dim": dim, "cx": cx, "cz": cz,
                                          "ymin": ymin, "ymax": ymax})
        return codec.decode_mcc1(buf)

    def mesh(self, dim, cx, cz, ymin, ymax, lod=0, ao=None, leaves=None):
        params = {"dim": dim, "cx": cx, "cz": cz, "ymin": ymin, "ymax": ymax, "lod": lod}
        if ao is not None:
            ao_on = ao if isinstance(ao, bool) else str(ao) == "1"
            params["ao"] = 1 if ao_on else 0
        if leaves is not None:
            fast = (leaves is True) or (isinstance(leaves, str) and leaves == "fast")
            params["leaves"] = "fast" if fast else "fancy"
        buf = self._binary("/api/mesh", params)
        return codec.decode_mcm1(buf)

    def versions(self, dim, cx0, cz0, cx1, cz1):
        buf = self._binary("/api/versions",
                           {"dim": dim, "cx0": cx0, "cz0": cz0, "cx1": cx1, "cz1": cz1})
        return codec.decode_versions(buf)

    def entities(self, dim, cx, cz, ymin=-64, ymax=320):
        """区块内实体列表（R5）：{entities: [{id, Pos, ...}]}；JSON。"""
        status, _, data = self._request(
            "/api/entities", {"dim": dim, "cx": cx, "cz": cz,
                              "ymin": ymin, "ymax": ymax})
        if status != 200:
            raise ApiError(f"entities {dim}/{cx}/{cz} -> {status}")
        return json.loads(data).get("entities") or []

    def texture_png(self, block, face):
        status, headers, data = self._request(
            "/api/texture", {"block": block, "face": face})
        if status != 200:
            raise ApiError(f"texture {block}/{face} -> {status}")
        return data

    def setblock(self, dim, x, y, z, block):
        status, _, data = self._request(
            "/api/setblock", {"dim": dim, "x": x, "y": y, "z": z, "block": block},
            method="POST")
        return json.loads(data)
