# -*- coding: utf-8 -*-
"""
抖音自媒体视频库 → 夸克网盘（经 alist）每日增量上传器

设计
----
1. **本地状态库** `_upload_state.db`（SQLite）记录每个已上传文件：
   (folder, filename) 主键 + size/mtime/md5 + remote_path + status + attempts + 时间戳。
   增量判定：库里没有该文件，或 status != done，或 size/mtime 变了 → 需要上传。
   重复跑幂等，中断可续传（不必扫云端）。
2. **传输** 走 alist：`PUT /api/fs/put`（细节见 `_alist.py` 顶部注释）。
   本地算好 md5 放 `X-File-Md5` → 夸克侧可**秒传**；`Last-Modified` 保留原始时间戳。
3. 上传成功后**按目录批量强刷列表**比对 size（不能逐文件 fs/get，见 flush_verify 注释）。

产物布局：`/自媒体视频库/{账号昵称}/{原文件名}`（远端根可用 --remote-root 改）

用法
----
  python _upload_alist.py                      # 预演：只报告待传清单与体积
  python _upload_alist.py --apply              # 真实上传
  python _upload_alist.py --apply --jobs 3 --max-seconds 540
  python _upload_alist.py --stats              # 只看状态库统计
  python _upload_alist.py --apply --retry-failed
  python _upload_alist.py --apply --folder UID2862736318926412_巫师财经_发布作品
"""
import argparse
import concurrent.futures as futures
import datetime
import pathlib
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _alist  # noqa: E402

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
DB_PATH = ROOT / "_upload_state.db"
LOCK_PATH = ROOT / "_upload_state.lock"
LOG_PATH = ROOT / "_upload_log.txt"

REMOTE_ROOT = "/自媒体视频库"                        # alist 路径 → 夸克顶层「自媒体视频库」
EXTS = {".m4a", ".jpeg", ".jpg", ".webp", ".png"}    # m4a 音频 + 封面图
FOLDERS = ("UID*_发布作品", "MID*_合集作品")
MIN_FREE_GB = 5.0        # alist 上传会把整文件先落到 Volume/temp，留足余量
VERIFY_EVERY = 25        # 每传多少个做一次目录级校验

DDL = """
CREATE TABLE IF NOT EXISTS files (
  folder       TEXT    NOT NULL,
  account      TEXT    NOT NULL,
  filename     TEXT    NOT NULL,
  ext          TEXT    NOT NULL,
  size         INTEGER NOT NULL,
  mtime        INTEGER NOT NULL,
  md5          TEXT,
  remote_path  TEXT,
  remote_size  INTEGER,
  status       TEXT    NOT NULL,   -- done / failed
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  first_seen   TEXT    NOT NULL,
  uploaded_at  TEXT,
  PRIMARY KEY (folder, filename)
);
CREATE INDEX IF NOT EXISTS idx_files_status  ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_account ON files(account);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  duration_s  REAL,
  scanned     INTEGER DEFAULT 0,
  pending     INTEGER DEFAULT 0,
  uploaded    INTEGER DEFAULT 0,
  failed      INTEGER DEFAULT 0,
  bytes       INTEGER DEFAULT 0,
  jobs        INTEGER DEFAULT 1,
  note        TEXT
);
"""


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, echo=True):
    line = f"[upload {time.strftime('%H:%M:%S')}] {msg}"
    if echo:
        print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    # 轻量迁移：老库缺列时补上（CREATE TABLE IF NOT EXISTS 不会加列）
    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    if "jobs" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN jobs INTEGER DEFAULT 1")
        con.commit()
    return con


def account_of(folder_name):
    """UID2862736318926412_巫师财经_发布作品 -> 巫师财经；MID…_播客正片合集_合集作品 -> 播客正片合集"""
    parts = folder_name.split("_")
    return parts[1] if len(parts) >= 3 else folder_name


def md5_of(path, chunk=1 << 20):
    import hashlib
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def free_gb(p=r"D:\\"):
    import shutil
    return shutil.disk_usage(p).free / 1024 ** 3


def _pid_alive(pid):
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def acquire_lock():
    """防止定时任务与手动运行并发写同一个状态库 / 重复上传。"""
    import os
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            pid = 0
        if pid and _pid_alive(pid):
            log(f"已有上传进程在跑（PID {pid}），本次跳过以免并发")
            return False
        log(f"发现陈旧锁（PID {pid} 已不存在），接管")
    LOCK_PATH.write_text(f"{os.getpid()} {now()}", encoding="utf-8")
    return True


def release_lock():
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def scan(folders_filter=None):
    """返回 [(folder_name, account, pathlib.Path)]"""
    out = []
    for pat in FOLDERS:
        for folder in sorted(ROOT.glob(pat)):
            if not folder.is_dir():
                continue
            if folders_filter and folder.name not in folders_filter:
                continue
            acct = account_of(folder.name)
            for p in sorted(folder.iterdir()):
                if p.is_file() and p.suffix.lower() in EXTS:
                    out.append((folder.name, acct, p))
    return out


def pending_of(con, items, min_age_min=0):
    """增量核心：大小/mtime 一致且已 done 的跳过。

    min_age_min：跳过"刚写入不久"的文件。下载任务与上传任务可能并行，
    工具写封面是直接落盘（非原子），刚写的文件可能只写了一半 ——
    传上去就是一个截断的坏文件，且 size 校验也发现不了。
    冷却期满后自然会被下一轮捡起来。
    """
    todo, skipped, changed, young = [], 0, 0, 0
    cutoff = time.time() - min_age_min * 60
    for folder, acct, p in items:
        st = p.stat()
        if st.st_mtime > cutoff:
            young += 1
            continue
        r = con.execute("SELECT size, mtime, status FROM files WHERE folder=? AND filename=?",
                        (folder, p.name)).fetchone()
        if r is None:
            todo.append((folder, acct, p))
        elif r["status"] == "done" and r["size"] == st.st_size and r["mtime"] == int(st.st_mtime):
            skipped += 1
        else:
            if r["status"] == "done":
                changed += 1
            todo.append((folder, acct, p))
    return todo, skipped, changed, young


# ---------------------------------------------------------------- 上传

def put_and_record(al, con, dblock, folder, acct, p, remote_root, timeout):
    """上传一个文件；成功则写 done 并返回需校验的条目。

    注意：**不要**在此处用 `/api/fs/get` 判成败 —— alist 目录列表有缓存，
    刚上传完立刻查会拿不到文件，造成"假失败"（2026-09-28 实测，m4a 实际已落盘）。
    回查统一交给 flush_verify。
    """
    st = p.stat()
    remote = f"{remote_root}/{acct}/{p.name}"
    if free_gb() < MIN_FREE_GB:
        return False, f"磁盘不足（D: 剩 {free_gb():.1f}GB < {MIN_FREE_GB}GB）", None
    try:
        md5 = md5_of(p)
    except Exception as e:
        return False, f"md5 失败 {type(e).__name__}", None
    ok, msg = al.put_file(p, remote, md5=md5, mtime_ms=int(st.st_mtime * 1000), timeout=timeout)
    if not ok:
        return False, f"{msg} :: md5={md5[:12]}", None
    with dblock:
        con.execute("""INSERT INTO files (folder,account,filename,ext,size,mtime,md5,remote_path,
                         remote_size,status,attempts,last_error,first_seen,uploaded_at)
                       VALUES (?,?,?,?,?,?,?,?,NULL,'done',1,NULL,?,?)
                       ON CONFLICT(folder,filename) DO UPDATE SET
                         account=excluded.account, ext=excluded.ext, size=excluded.size,
                         mtime=excluded.mtime, md5=excluded.md5, remote_path=excluded.remote_path,
                         status='done', attempts=files.attempts+1, last_error=NULL,
                         uploaded_at=excluded.uploaded_at""",
                    (folder, acct, p.name, p.suffix.lower(), st.st_size, int(st.st_mtime), md5,
                     remote, now(), now()))
        con.commit()
    return True, f"ok {st.st_size/1024/1024:.1f}MB", (folder, acct, p, remote)


def record_fail(con, dblock, folder, acct, p, err):
    st = p.stat()
    with dblock:
        con.execute("""INSERT INTO files (folder,account,filename,ext,size,mtime,status,attempts,
                         last_error,first_seen)
                       VALUES (?,?,?,?,?,?,'failed',1,?,?)
                       ON CONFLICT(folder,filename) DO UPDATE SET
                         status='failed', attempts=files.attempts+1,
                         last_error=excluded.last_error, size=excluded.size, mtime=excluded.mtime""",
                    (folder, acct, p.name, p.suffix.lower(), st.st_size, int(st.st_mtime),
                     err[:400], now()))
        con.commit()


def flush_verify(al, con, dblock, queue, quiet=False):
    """按远端目录批量强刷（refresh=True）校验 size，纠正"假成功"。

    按目录分组后一次列表覆盖该目录下本轮所有文件，比逐文件 fs/get 省一个数量级请求。
    """
    if not queue:
        return 0, 0
    by_dir = {}
    for folder, acct, path, remote in queue:
        by_dir.setdefault(remote.rsplit("/", 1)[0], []).append((folder, acct, path, remote))
    ok = bad = 0
    for d, items in by_dir.items():
        try:
            listing = {c["name"]: c.get("size") for c in al.list_dir(d, refresh=True)}
        except Exception as e:
            log(f"  校验失败（目录列表拿不到）{d}: {e}")
            bad += len(items)
            continue
        with dblock:
            for folder, acct, path, remote in items:
                name = remote.rsplit("/", 1)[1]
                local_size = path.stat().st_size
                rsize = listing.get(name)
                if rsize == local_size:
                    con.execute("UPDATE files SET remote_size=? WHERE folder=? AND filename=?",
                                (rsize, folder, name))
                    ok += 1
                else:
                    bad += 1
                    err = "远端不存在" if rsize is None else f"远端 size {rsize} != {local_size}"
                    con.execute("UPDATE files SET status='failed', last_error=? "
                                "WHERE folder=? AND filename=?", (err, folder, name))
                    log(f"  校验不过 {acct}/{name[:48]}: {err}")
            con.commit()
    if not quiet:
        log(f"  校验 {ok} 通过 / {bad} 不过")
    return ok, bad


def show_stats(con):
    print("=== 上传状态库统计 ===")
    tot = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    print(f"  记录总数 {tot}")
    for r in con.execute("SELECT status, COUNT(*) c, SUM(size)/1024/1024 mb FROM files GROUP BY status"):
        print(f"  {r['status']:<8} {r['c']:>6} 个  {r['mb'] or 0:.1f} MB")
    print("\n  按账号（已上传 / 失败 / 体积）:")
    for r in con.execute("""SELECT account,
                              SUM(status='done') d, SUM(status='failed') f,
                              SUM(CASE WHEN status='done' THEN size ELSE 0 END)/1024/1024 mb
                            FROM files GROUP BY account ORDER BY account"""):
        print(f"    {r['account']:<20} done {r['d']:>5}  failed {r['f']:>4}  {r['mb']:.1f} MB")
    print("\n  最近 5 次运行:")
    for r in con.execute("SELECT started_at,duration_s,scanned,uploaded,failed,bytes,jobs,note "
                         "FROM runs ORDER BY id DESC LIMIT 5"):
        print(f"    {r['started_at']}  {r['duration_s'] or 0:.0f}s  扫描{r['scanned']} "
              f"上传{r['uploaded']} 失败{r['failed']} {(r['bytes'] or 0)/1024/1024:.1f}MB "
              f"jobs={r['jobs']} {r['note'] or ''}")


# ---------------------------------------------------------------- 主流程

def run_upload(a, con, items, todo):
    dblock = threading.Lock()
    log(f"alist 连接测试 ...")
    probe = _alist.Alist(timeout=60)
    log(f"alist OK，远端根 {a.remote_root}，并发 {a.jobs}")

    # 先把每个账号的远端目录建好（串行，避免并发建目录打架）
    accts = sorted({acct for _f, acct, _p in todo})
    for acct in accts:
        try:
            probe.ensure_dir(f"{a.remote_root}/{acct}")
        except Exception as e:
            log(f"建目录失败 {acct}: {e}")

    rid = con.execute("INSERT INTO runs (started_at,scanned,pending,dry_run,jobs) "
                      "VALUES (?,?,?,0,?)", (now(), len(items), len(todo), a.jobs)).lastrowid
    con.commit()

    t0 = time.time()
    stat = {"ok": 0, "fail": 0, "bytes": 0, "done": False}
    budget = a.max_seconds or 0

    def work(idx, chunk):
        al = _alist.Alist(timeout=max(120, a.timeout))
        vq = []
        for folder, acct, p in chunk:
            if budget and time.time() - t0 > budget:
                break
            if a.max_files and stat["ok"] + stat["fail"] >= a.max_files:
                break
            tb = time.time()
            good, msg, mint = put_and_record(al, con, dblock, folder, acct, p,
                                             a.remote_root, a.timeout)
            with dblock:
                if good:
                    stat["ok"] += 1
                    stat["bytes"] += p.stat().st_size
                else:
                    stat["fail"] += 1
            if good:
                vq.append(mint)
                log(f"[{stat['ok']+stat['fail']}/{len(todo)}] ✔ {acct}/{p.name[:52]}  "
                    f"{msg}  {time.time()-tb:.1f}s")
                if a.verify and len(vq) >= VERIFY_EVERY:
                    flush_verify(al, con, dblock, vq)
                    vq.clear()
            else:
                record_fail(con, dblock, folder, acct, p, msg)
                log(f"[{stat['ok']+stat['fail']}/{len(todo)}] ✘ {acct}/{p.name[:52]}  {msg}")
        if a.verify and vq:
            flush_verify(al, con, dblock, vq)

    if a.jobs > 1:
        # 轮转切分：让每个 worker 都拿到各账号的文件，避免一个 worker 卡在超大文件上
        chunks = [[] for _ in range(a.jobs)]
        for i, it in enumerate(todo):
            chunks[i % a.jobs].append(it)
        with futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            list(ex.map(lambda t: work(*t), list(enumerate(chunks))))
    else:
        work(0, todo)

    dur = time.time() - t0
    con.execute("UPDATE runs SET finished_at=?,duration_s=?,uploaded=?,failed=?,bytes=? WHERE id=?",
                (now(), round(dur, 1), stat["ok"], stat["fail"], stat["bytes"], rid))
    con.commit()
    log(f"本轮完成：成功 {stat['ok']}，失败 {stat['fail']}，共 {stat['bytes']/1024/1024:.1f}MB，"
        f"耗时 {dur/60:.1f} 分")
    left = len(pending_of(con, scan(a.folder), a.min_age_min)[0])
    print(f"\n成功 {stat['ok']}  失败 {stat['fail']}  {stat['bytes']/1024/1024:.1f}MB  "
          f"{dur/60:.1f}分  剩余待传 {left}")
    return 0 if stat["fail"] == 0 and left == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="不加则只预演")
    ap.add_argument("--remote-root", default=REMOTE_ROOT)
    ap.add_argument("--folder", nargs="*", default=None, help="只处理指定本地目录名")
    ap.add_argument("--jobs", type=int, default=1, help="并发上传数（默认 1）")
    ap.add_argument("--max-files", type=int, default=0, help="本轮最多上传几个（0=不限）")
    ap.add_argument("--max-seconds", type=int, default=0, help="本轮最多跑多少秒（0=不限）")
    ap.add_argument("--retry-failed", action="store_true", help="把 failed 的也纳入本轮")
    ap.add_argument("--timeout", type=int, default=3600, help="单文件上传超时秒")
    ap.add_argument("--min-age-min", type=int, default=15,
                    help="跳过 mtime 在 N 分钟内的文件（防传到下载任务正在写的半成品，默认 15）")
    ap.add_argument("--no-verify", action="store_false", dest="verify",
                    help="跳过上传后的远端校验（默认开启）")
    ap.add_argument("--stats", action="store_true", help="只打印状态库统计")
    a = ap.parse_args()

    con = db()
    if a.stats:
        show_stats(con)
        return 0

    items = scan(a.folder)
    todo, skipped, changed, young = pending_of(con, items, a.min_age_min)
    if a.retry_failed:
        have = {(f, p.name) for f, _ac, p in todo}
        for f, acct, p in items:
            if (f, p.name) in have:
                continue
            if p.stat().st_mtime > time.time() - a.min_age_min * 60:
                continue
            r = con.execute("SELECT status FROM files WHERE folder=? AND filename=?",
                            (f, p.name)).fetchone()
            if r and r["status"] == "failed":
                todo.append((f, acct, p))
        todo, _s, _c, _y = pending_of(con, todo, a.min_age_min)

    size_mb = sum(p.stat().st_size for _f, _a, p in todo) / 1024 / 1024
    log(f"本地 {len(items)} 文件；已上传 {skipped}；变更 {changed}；待传 {len(todo)}"
        f"（{size_mb:.1f}MB）；冷却中 {young}；D: 剩 {free_gb():.1f}GB", echo=False)
    print(f"本地文件 {len(items)}  已上传 {skipped}  变更 {changed}  待传 {len(todo)}"
          f"  ({size_mb:.1f}MB)  冷却中 {young}")

    if not todo:
        log("无新增/变更，跳过上传（0 网络请求）")
        print("无新增/变更，跳过上传")
        con.execute("INSERT INTO runs (started_at,finished_at,duration_s,scanned,pending,note) "
                    "VALUES (?,?,0,?,0,'no-change')", (now(), now(), len(items)))
        con.commit()
        return 0

    if not a.apply:
        for _f, ac, p in todo[:25]:
            print(f"  待传> {ac}\\{p.name}")
        if len(todo) > 25:
            print(f"  ... 其余 {len(todo) - 25} 个")
        print("\n（预演模式，未发任何请求；加 --apply 执行）")
        return 0

    if not acquire_lock():
        print("已有上传进程在运行，本次跳过")
        return 0
    try:
        return run_upload(a, con, items, todo)
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
