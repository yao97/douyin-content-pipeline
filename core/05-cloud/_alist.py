# -*- coding: utf-8 -*-
"""
alist 客户端（本地上传用，无第三方依赖，仅标准库）

要点（2026-09-28 实地核实 alist v3.63.0 源码 server/middlewares/auth.go）：
- 认证有两条路：JWT 登录，或直接带**全局 API 令牌**。
  令牌命中时 `Auth` 中间件直接以 admin 身份放行 —— 不需要密码、不影响已登录设备。
  令牌存在 alist 库 `data/data.db` 的 `x_setting_items(key='token')`。
  （自签 JWT 走不通：中间件还校验 client-id 设备会话 + pwd_ts，会报 token is invalidated）
- 上传：`PUT /api/fs/put`，路径放在 **Header `File-Path`**（URL 编码，服务端做 PathUnescape），
  body 是裸文件流；支持 `X-File-Md5`（夸克据此**秒传**）、`Last-Modified`（毫秒，保留原时间戳）、
  `Overwrite:false`（已存在则 403 file exists，可用于幂等）。
- guest 已禁用（`Guest user is disabled`），所以一切操作都必须带令牌。
"""
import http.client
import json
import pathlib
import sqlite3
import urllib.parse
import urllib.request

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
ALIST_DB = pathlib.Path(r"D:\alist\data\data.db")
ALIST_CONFIG = pathlib.Path(r"D:\alist\data\config.json")
SECRETS = ROOT / "_tools" / "_secrets" / "alist.json"


def load_conn():
    """返回 (base_url, token)。优先读 _secrets/alist.json，缺失则直接从 alist 库取。"""
    if SECRETS.exists():
        try:
            d = json.loads(SECRETS.read_text(encoding="utf-8"))
            if d.get("url") and d.get("token"):
                return d["url"].rstrip("/"), d["token"]
        except Exception:
            pass
    url = "http://127.0.0.1:5244"
    if ALIST_CONFIG.exists():
        try:
            cfg = json.loads(ALIST_CONFIG.read_text(encoding="utf-8-sig"))
            sc = cfg.get("scheme") or {}
            port = sc.get("http_port") or 5244
            addr = sc.get("address") or "127.0.0.1"
            host = "127.0.0.1" if addr in ("0.0.0.0", "::", "") else addr
            url = f"http://{host}:{port}"
        except Exception:
            pass
    token = ""
    if ALIST_DB.exists():
        con = sqlite3.connect(f"file:{ALIST_DB}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()
            token = r[0] if r else ""
        finally:
            con.close()
    return url, token


class AlistError(RuntimeError):
    pass


class Alist:
    def __init__(self, url=None, token=None, timeout=120):
        u, t = load_conn()
        self.base = (url or u).rstrip("/")
        self.token = token or t
        self.timeout = timeout
        if not self.token:
            raise AlistError("未取到 alist API 令牌（检查 D:\\alist\\data\\data.db 或 _tools/_secrets/alist.json）")
        p = urllib.parse.urlsplit(self.base)
        self.host = p.hostname or "127.0.0.1"
        self.port = p.port or 80
        # 本机直连，绕开 Clash 等代理
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------- JSON API ----------
    def api(self, path, body=None, method="POST", timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Authorization": self.token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                raise AlistError(f"HTTP {e.code} {path}: {raw[:200]}")
        except Exception as e:
            raise AlistError(f"{type(e).__name__} {path}: {str(e)[:200]}")

    def me(self):
        return self.api("/api/me", {}, "GET")

    def list_dir(self, path, refresh=False):
        d = self.api("/api/fs/list", {"path": path, "page": 1, "per_page": 0,
                                     "refresh": refresh, "password": ""})
        if d.get("code") != 200:
            raise AlistError(f"list {path}: {d.get('message')}")
        return (d.get("data") or {}).get("content") or []

    def mkdir(self, path):
        return self.api("/api/fs/mkdir", {"path": path})

    def stat(self, path):
        """返回 (size, is_dir)；不存在返回 (None, None)。"""
        d = self.api("/api/fs/get", {"path": path, "password": ""})
        data = d.get("data")
        if d.get("code") != 200 or not data:
            return None, None
        return data.get("size"), data.get("is_dir")

    def ensure_dir(self, path):
        """逐级建目录（幂等）。返回 True 表示本次新建过。"""
        parts = [p for p in path.split("/") if p]
        cur, created = "", False
        for p in parts:
            cur += "/" + p
            try:
                if not self.stat(cur)[1]:
                    r = self.mkdir(cur)
                    created = True
                    if r.get("code") not in (200, 409):
                        raise AlistError(f"mkdir {cur}: {r.get('message')}")
            except AlistError:
                raise
        return created

    # ---------- 上传 ----------
    def put_file(self, local, remote, md5=None, mtime_ms=None, timeout=3600, overwrite=True):
        """流式上传单个文件。返回 (ok, message)。

        用 http.client 手工拼请求：要精确控制 Content-Length 与 Header，
        且必须把整个文件以裸流送出（alist 的 fs/put 读 request body）。
        """
        local = pathlib.Path(local)
        size = local.stat().st_size
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.putrequest("PUT", "/api/fs/put", skip_accept_encoding=True)
            conn.putheader("Authorization", self.token)
            conn.putheader("File-Path", urllib.parse.quote(str(remote), safe="/"))
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.putheader("Overwrite", "true" if overwrite else "false")
            if md5:
                conn.putheader("X-File-Md5", md5)
            if mtime_ms:
                conn.putheader("Last-Modified", str(int(mtime_ms)))
            conn.endheaders()
            sent = 0
            with local.open("rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status != 200:
                return False, f"HTTP {resp.status} {raw[:200]}"
            try:
                d = json.loads(raw)
            except Exception:
                return False, f"响应非 JSON: {raw[:200]}"
            if d.get("code") == 200:
                return True, f"ok {sent}B"
            return False, f"code={d.get('code')} {d.get('message')}"
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            try:
                conn.close()
            except Exception:
                pass
