#!/usr/bin/env python3
"""Ventoy-UI - CachyOS (デスクトップ環境なし) 向け Ventoy Web フロントエンド.

- 3363番ポートで公開
- GUI相当の設定 (GPT/MBR, インストール/アップデート, SecureBoot, 予約領域, ラベル, 非破壊インストール) を Web-UI で操作
- /opt/ventoy にダウンロード・展開
- USBドライブ / イメージファイル (ventoy.qcow2含む) を対象に選択可能
- 標準ライブラリのみで動作
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

VERSION = "v0.1.1"
PORT = 3363
BASE_DIR = Path(__file__).resolve().parent
VENTOY_DIR = Path("/opt/ventoy")
GITHUB_API_LATEST = "https://api.github.com/repos/ventoy/Ventoy/releases/latest"

# 単一バックグラウンドタスク (ダウンロード / 書き込み共通)
_task_lock = threading.Lock()
_task = {"running": False, "kind": "", "log": [], "returncode": None, "done": True}


def task_log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _task_lock:
        _task["log"].append(line)
        # 肥大化防止
        if len(_task["log"]) > 2000:
            _task["log"] = _task["log"][-2000:]
    print(line, flush=True)


def task_start(kind: str):
    with _task_lock:
        if _task["running"]:
            return False
        _task.update({"running": True, "kind": kind, "log": [], "returncode": None, "done": False})
    return True


def task_finish(rc):
    with _task_lock:
        _task["running"] = False
        _task["returncode"] = rc
        _task["done"] = True


def task_snapshot():
    with _task_lock:
        return dict(_task)


def run_streaming(cmd, cwd=None, env=None):
    """コマンドを実行し、stdout/stderrをタスクログへ流す。戻り値はreturncode。"""
    task_log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        task_log(line.rstrip())
    proc.wait()
    return proc.returncode


def find_ventoy_script():
    """Ventoy2Disk.sh の実体を探す。

    v0.0.1 のダウンロード処理の不具合で /opt/ventoy/ventoy-x.y.zz/ 配下に
    ネストして展開されている場合があるため、直下→ネスト→再帰の順で探索する。
    """
    direct = VENTOY_DIR / "Ventoy2Disk.sh"
    if direct.is_file():
        return direct
    nested = sorted(VENTOY_DIR.glob("ventoy-*/Ventoy2Disk.sh"))
    if nested:
        return nested[0]
    try:
        for cand in sorted(VENTOY_DIR.rglob("Ventoy2Disk.sh")):
            if cand.is_file():
                return cand
    except OSError:
        pass
    return direct


def repair_ventoy_layout():
    """ネスト展開・残留アーカイブ等のレイアウト崩れを修復する。"""
    repaired = []
    if (VENTOY_DIR / "Ventoy2Disk.sh").is_file():
        base = str(VENTOY_DIR)
        # v0.0.1 の不具合で残った重複ネスト (ventoy-x.y.zz/) があれば除去
        for dup in sorted(VENTOY_DIR.glob("ventoy-*/Ventoy2Disk.sh")):
            shutil.rmtree(dup.parent, ignore_errors=True)
            repaired.append(f"重複した {dup.parent} を削除")
    else:
        nested = sorted(VENTOY_DIR.glob("ventoy-*/Ventoy2Disk.sh"))
        if not nested:
            return repaired
        src_dir = nested[0].parent
        for entry in os.listdir(src_dir):
            s = os.path.join(src_dir, entry)
            d = VENTOY_DIR / entry
            if os.path.isdir(s) and not os.path.islink(s):
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d)
            else:
                try:
                    if d.exists() or d.is_symlink():
                        if d.is_dir() and not d.is_symlink():
                            shutil.rmtree(d, ignore_errors=True)
                        else:
                            d.unlink()
                except OSError:
                    pass
                shutil.copy2(s, d)
        try:
            if (VENTOY_DIR / "Ventoy2Disk.sh").is_file():
                shutil.rmtree(src_dir, ignore_errors=True)
                repaired.append(f"重複した {src_dir} を削除")
            else:
                os.rmdir(src_dir)
        except OSError:
            pass
        repaired.append(f"nested dir {src_dir} を {VENTOY_DIR} 直下に昇格")
        base = str(VENTOY_DIR)
    # 展開時に混入したダウンロードアーカイブの残骸を除去
    for leftover in sorted(VENTOY_DIR.glob("*.tar.gz")):
        try:
            leftover.unlink()
            repaired.append(f"残留アーカイブ {leftover.name} を削除")
        except OSError:
            pass
    # 実行権限の確保
    for sh in VENTOY_DIR.glob("*.sh"):
        try:
            os.chmod(sh, 0o755)
        except OSError:
            pass
    return repaired


def get_ventoy_version():
    """インストール済みVentoyのバージョン推定。"""
    if not VENTOY_DIR.exists():
        return None
    # ダウンロード時に保存するタグファイル
    tag_file = VENTOY_DIR / "ventoy-ui-version.txt"
    if tag_file.exists():
        try:
            return tag_file.read_text().strip()
        except OSError:
            pass
    script = find_ventoy_script()
    home = script.parent if script.is_file() else VENTOY_DIR
    # tool内のバージョン情報ファイルを探索
    for cand in home.glob("tool/ventoy-*"):
        m = re.search(r"(\d+\.\d+\.\d+)", cand.name)
        if m:
            return f"v{m.group(1)}"
    # ディレクトリ直下に展開直後の名残がある場合
    names = [p.name for p in VENTOY_DIR.glob("ventoy-*")]
    if names:
        m = re.search(r"(\d+\.\d+\.\d+)", names[0])
        if m:
            return f"v{m.group(1)}"
    # Ventoy2Disk.sh が存在すれば「インストール済み(バージョン不明)」
    if script.is_file():
        return "installed (unknown version)"
    return None


def list_devices():
    """lsblk -J で物理ディスク一覧を取得。"""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PATH,SIZE,MODEL,TRAN,TYPE"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(out.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        return [], f"lsblk 取得失敗: {e}"
    devices = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") == "disk":
            devices.append({
                "name": dev.get("name", ""),
                "path": dev.get("path", f"/dev/{dev.get('name','')}"),
                "size": dev.get("size", ""),
                "model": (dev.get("model") or "").strip(),
                "tran": dev.get("tran") or "",
            })
    return devices, ""


def list_image_candidates():
    cands = []
    for pat in ("/opt/*.qcow2", "/opt/*.img", "/opt/ventoy.qcow2",
                "/var/lib/libvirt/images/*.qcow2", "/tmp/*.img", "/root/*.img"):
        try:
            cands.extend(glob.glob(pat))
        except Exception:  # noqa: BLE001
            pass
    # 重複除去・ソート
    seen = sorted(set(cands))
    out = []
    for p in seen:
        try:
            st = os.stat(p)
            out.append({"path": p, "size": st.st_size})
        except OSError:
            out.append({"path": p, "size": -1})
    return out


def download_ventoy_task():
    rc = 1
    try:
        VENTOY_DIR.mkdir(parents=True, exist_ok=True)
        task_log(f"{VENTOY_DIR} を作成/確認しました")
        task_log(f"最新リリース情報を取得中: {GITHUB_API_LATEST}")
        req = urllib.request.Request(
            GITHUB_API_LATEST, headers={"User-Agent": "Ventoy-UI", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
        tag = info.get("tag_name", "")
        assets = info.get("assets", [])
        url = ""
        for a in assets:
            name = a.get("name", "")
            if name.endswith("-linux.tar.gz"):
                url = a.get("browser_download_url", "")
                break
        if not url:
            task_log("linux.tar.gz アセットが見つかりませんでした")
            task_finish(1)
            return
        task_log(f"最新バージョン: {tag}")
        task_log(f"ダウンロードURL: {url}")
        tmp = tempfile.mkdtemp(prefix="ventoy-ui-")
        archive = os.path.join(tmp, "ventoy-linux.tar.gz")
        task_log("ダウンロード中...")
        req2 = urllib.request.Request(url, headers={"User-Agent": "Ventoy-UI"})
        with urllib.request.urlopen(req2, timeout=120) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        task_log(f"ダウンロード完了 ({os.path.getsize(archive)} bytes)")
        task_log(f"{VENTOY_DIR} に展開中...")
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(tmp)
        # 展開結果から Ventoy2Disk.sh を含むディレクトリを特定する
        # (tar の先頭メンバ名に依存しない。v0.0.1 では先頭メンバ判定の
        # 失敗によりネスト展開・アーカイブ混入が起きた)
        src = None
        for root, _dirs, files in os.walk(tmp):
            if "Ventoy2Disk.sh" in files:
                src = root
                break
        if src is None:
            task_log("展開結果に Ventoy2Disk.sh が見つかりませんでした")
            task_finish(1)
            return
        for entry in os.listdir(src):
            if os.path.join(src, entry) == archive:
                continue  # ダウンロードしたアーカイブ自体はコピーしない
            s = os.path.join(src, entry)
            d = VENTOY_DIR / entry
            if os.path.isdir(s) and not os.path.islink(s):
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d)
            else:
                try:
                    if d.exists() or d.is_symlink():
                        if d.is_dir() and not d.is_symlink():
                            shutil.rmtree(d, ignore_errors=True)
                        else:
                            d.unlink()
                except OSError:
                    pass
                shutil.copy2(s, d)
        for msg in repair_ventoy_layout():
            task_log(f"修復: {msg}")
        # 実行権限の確保
        for sh in VENTOY_DIR.glob("*.sh"):
            try:
                os.chmod(sh, 0o755)
            except OSError:
                pass
        (VENTOY_DIR / "ventoy-ui-version.txt").write_text(tag + "\n")
        task_log(f"展開完了: {tag} -> {VENTOY_DIR}")
        shutil.rmtree(tmp, ignore_errors=True)
        rc = 0
    except Exception as e:  # noqa: BLE001
        task_log(f"エラー: {e}")
        rc = 1
    task_finish(rc)


def ensure_image_file(path: str, create: bool, size_gb: float):
    """イメージファイル対象の準備。存在しなければ作成を試みる。"""
    p = Path(path)
    if p.exists():
        task_log(f"対象イメージファイルを確認: {p} ({p.stat().st_size} bytes)")
        return True
    if not create:
        task_log(f"対象ファイルが存在しません: {p}")
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    size_bytes = int(float(size_gb) * 1024 * 1024 * 1024)
    if p.suffix == ".qcow2" and shutil.which("qemu-img"):
        task_log(f"qcow2イメージを新規作成: {p} ({size_gb} GiB)")
        return run_streaming(["qemu-img", "create", "-f", "qcow2", str(p), str(size_bytes)]) == 0
    # qemu-img が無い場合や .img の場合は truncate/fallocate で raw 作成
    task_log(f"rawイメージを新規作成: {p} ({size_gb} GiB)")
    if shutil.which("fallocate"):
        if run_streaming(["fallocate", "-l", str(size_bytes), str(p)]) == 0:
            return True
    try:
        with open(p, "wb") as f:
            f.truncate(size_bytes)
        task_log("truncate で作成しました (raw形式)")
        return True
    except OSError as e:
        task_log(f"イメージ作成失敗: {e}")
        return False


def install_ventoy_task(params: dict):
    rc = 1
    try:
        script = find_ventoy_script()
        if not script.is_file():
            task_log(f"{VENTOY_DIR} に Ventoy2Disk.sh が見つかりません。先に「Ventoyをダウンロード」を実行してください。")
            task_finish(1)
            return
        for msg in repair_ventoy_layout():
            task_log(f"修復: {msg}")
        script = find_ventoy_script()
        target_type = params.get("target_type", "usb")
        mode = params.get("mode", "install")  # install | force | update
        part_style = params.get("part_style", "GPT")
        secure_boot = bool(params.get("secure_boot", True))
        preserve_mb = int(params.get("preserve_mb") or 0)
        label = (params.get("label") or "").strip()
        nondestructive = bool(params.get("nondestructive", False))

        if mode == "update":
            cmd_flag = "-u"
        elif mode == "force":
            cmd_flag = "-I"
        else:
            cmd_flag = "-i"

        if target_type == "image":
            image_path = (params.get("image_path") or "/opt/ventoy.qcow2").strip()
            if not os.path.isabs(image_path):
                task_log("イメージパスは絶対パスで指定してください")
                task_finish(1)
                return
            ok = ensure_image_file(image_path,
                                   bool(params.get("create_image", False)),
                                   float(params.get("image_size_gb") or 16))
            if not ok:
                task_finish(1)
                return
            target = image_path
        else:
            target = (params.get("device") or "").strip()
            if not target.startswith("/dev/"):
                task_log("USBドライブを選択してください (例: /dev/sdb)")
                task_finish(1)
                return

        # システムディスクへの誤書き込み防止の注意喚起 (実行はする)
        task_log(f"対象: {target} / モード: {mode} / パーティション: {part_style}")
        if target_type == "usb" and target in ("/dev/sda", "/dev/vda", "/dev/nvme0n1"):
            task_log("警告: システムディスクの可能性があります。選択を再確認してください。")

        cmd = ["bash", str(script), cmd_flag]
        if mode in ("install", "force"):
            if part_style.upper() == "GPT":
                cmd.append("-g")
            if secure_boot:
                cmd.append("-s")
            else:
                cmd.append("-S")
            if preserve_mb > 0:
                cmd.extend(["-r", str(preserve_mb)])
            if label:
                cmd.extend(["-L", label])
            if nondestructive:
                cmd.append("-n")
        else:  # update
            cmd.append("-s" if secure_boot else "-S")

        cmd.append(target)
        # Ventoy2Disk.sh は対話的に確認を求めるため、yes をパイプする
        task_log("Ventoy2Disk.sh を実行します (確認プロンプトには自動で yes と回答)")
        proc = subprocess.Popen(
            ["bash", "-c", f"yes | {' '.join(map(sh_quote, cmd))}"],
            cwd=str(script.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            task_log(line.rstrip())
        proc.wait()
        rc = proc.returncode
        task_log(f"終了コード: {rc}")
    except Exception as e:  # noqa: BLE001
        task_log(f"エラー: {e}")
        rc = 1
    task_finish(rc)


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ========== ISOイメージダウンロード (Ventoyパーティションへの保存) ==========
# USBメモリ / qcow2・img イメージ内の Ventoyデータパーティション (第2パーティション)
# を /mnt/ventoy-iso にマウントし、URL指定で ISO を直接保存する。
# URL入力・検証・進捗・キャンセルの流儀は cachy-UI の Limine編集
# 「または直接URLを入力」ダウンロードに合わせている。
ISO_MOUNT_POINT = Path("/mnt/ventoy-iso")
NBD_MAX = 8  # /dev/nbd0〜nbd7 まで探索

_iso_lock = threading.Lock()
_iso_mount = {"mounted": False, "target_type": "", "target": "",
              "part": "", "label": "", "backend": "", "backing_dev": ""}
_iso_dl = {"running": False, "filename": "", "received": 0, "total": -1,
           "done": False, "success": None, "message": "", "cancel": False}
_iso_files_cache = {"files": [], "at": 0.0}

# /api/iso/install-pkg で導入を許可するパッケージ (任意コマンド実行防止のallowlist)
ISO_INSTALL_ALLOWLIST = ("libguestfs", "qemu-img")
GUESTFISH_TIMEOUT = 600


def need_install(package: str, reason: str, message: str):
    return {"ok": False, "need_install": {"package": package, "reason": reason},
            "message": message}


def guest_env():
    env = dict(os.environ)
    # コンテナ等で libvirtd が無い環境でも直接起動する
    env.setdefault("LIBGUESTFS_BACKEND", "direct")
    return env


def guest_probe(image: str):
    """guestfish でイメージ内のVentoyデータパーティション (第2パーティション) を特定する。
    戻り値 (ok, dev, fstype_or_message)。"""
    try:
        r = subprocess.run(
            ["guestfish", "--ro", "-a", image, "run", ":", "list-filesystems"],
            capture_output=True, text=True, timeout=GUESTFISH_TIMEOUT,
            env=guest_env())
    except subprocess.TimeoutExpired:
        return False, "", "guestfish の応答がタイムアウトしました (初回はアプライアンス生成で数分かかります)。"
    except Exception as e:  # noqa: BLE001
        return False, "", f"guestfish 実行失敗: {e}"
    if r.returncode != 0:
        return False, "", f"イメージ解析失敗: {(r.stderr or '').strip()[-500:]}"
    parts = []
    for line in (r.stdout or "").splitlines():
        if ":" not in line:
            continue
        dev, fst = line.split(":", 1)
        parts.append((dev.strip(), fst.strip()))
    if not parts:
        return False, "", "パーティションが見つかりません。イメージ内にVentoyが書き込まれていますか?"
    # 第2パーティションを優先、無ければ最大の unknown 以外
    dev = ""
    for d, _fst in parts:
        if d.endswith("2"):
            dev = d
            break
    if not dev:
        dev = parts[-1][0]
    fst = dict(parts).get(dev, "")
    return True, dev, fst


def guest_list(image: str, dev: str, force: bool = False):
    """guestfish で / 直下の ISO一覧を取得 (15秒キャッシュ)。"""
    now = time.time()
    if not force and now - _iso_files_cache["at"] < 15 and _iso_files_cache["files"] is not None:
        return list(_iso_files_cache["files"])
    files = []
    try:
        r = subprocess.run(
            ["guestfish", "--ro", "-a", image, "-m", dev, "ll", "/"],
            capture_output=True, text=True, timeout=GUESTFISH_TIMEOUT,
            env=guest_env())
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                p = line.split()
                if len(p) < 9 or not p[0].startswith("-"):
                    continue
                name = " ".join(p[8:])
                if not name.lower().endswith(".iso"):
                    continue
                try:
                    size = int(p[4])
                except ValueError:
                    size = -1
                files.append({"name": name, "size": size})
            files.sort(key=lambda x: x["name"])
    except Exception:  # noqa: BLE001
        pass
    _iso_files_cache.update({"files": files, "at": now})
    return files


def guest_upload(image: str, dev: str, local: str, remote: str):
    """ローカルファイルをイメージ内パーティションへ書き込む。戻り値 (ok, message)。"""
    try:
        r = subprocess.run(
            ["guestfish", "--rw", "-a", image, "-m", dev,
             "upload", local, remote],
            capture_output=True, text=True, timeout=GUESTFISH_TIMEOUT,
            env=guest_env())
    except subprocess.TimeoutExpired:
        return False, "イメージへの書き込みがタイムアウトしました。"
    except Exception as e:  # noqa: BLE001
        return False, f"イメージへの書き込み失敗: {e}"
    if r.returncode != 0:
        return False, f"イメージへの書き込み失敗: {(r.stderr or '').strip()[-500:]}"
    _iso_files_cache["at"] = 0.0
    return True, "イメージへ保存しました。"


def second_partition(device: str) -> str:
    """ディスク全体のデバイス名から第2パーティション名を求める。"""
    base = device.rstrip("/")
    # /dev/nvme0n1 / /dev/mmcblk0 / /dev/vda 等の末尾数字系は p を挿入
    if re.search(r"[0-9]$", base):
        return f"{base}p2"
    return f"{base}2"


def part_label(part: str) -> str:
    try:
        r = subprocess.run(["blkid", "-o", "value", "-s", "LABEL", part],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def mountpoint_active() -> bool:
    try:
        r = subprocess.run(["findmnt", "-n", str(ISO_MOUNT_POINT)],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def wait_for_dev(path: str, timeout: int = 15) -> bool:
    for _ in range(timeout * 2):
        if os.path.exists(path):
            return True
        time.sleep(0.5)
    return False


def nbd_module_loaded() -> bool:
    if os.path.exists("/sys/module/nbd"):
        return True
    try:
        with open("/proc/devices") as f:
            return " nbd" in f.read()
    except OSError:
        return False


def acquire_nbd():
    """空き nbd デバイスを確保する。戻り値 (ok, dev_or_message)。

    /dev/nbd0 決め打ちだった v0.0.3 では、nbdモジュール未ロードや
    デバイスノード未作成の環境で
    "Failed to open /dev/nbd0: No such file or directory" となった。
    """
    modprobe_err = ""
    if not nbd_module_loaded():
        r = subprocess.run(["modprobe", "nbd", "max_part=8"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            modprobe_err = (r.stderr or "").strip()
    if not nbd_module_loaded():
        msg = "nbd カーネルモジュールが読み込めません。"
        if modprobe_err:
            msg += f" (modprobe: {modprobe_err})"
        msg += " ホスト側で 'sudo modprobe nbd' を試すか、raw (.img) 形式を使用してください。"
        return False, msg
    for i in range(NBD_MAX):
        dev = f"/dev/nbd{i}"
        if not os.path.exists(dev):
            # モジュールはあるが udev がノードを作っていない場合は自作する (major 43)
            try:
                os.mknod(dev, 0o660 | 0o060000, os.makedev(43, i))
            except OSError:
                continue
        try:
            with open(f"/sys/block/nbd{i}/pid") as f:
                if f.read().strip() != "0":
                    continue  # 使用中
        except OSError:
            pass  # pid 情報が無ければ空きとみなして試す
        return True, dev
    return False, f"/dev/nbd0〜nbd{NBD_MAX - 1} が全て使用中です。不要な接続を切断してください。"


def iso_mount(target_type: str, target: str):
    """Ventoyデータパーティションをマウントする。戻り値は dict(ok, message[, need_install])。"""
    with _iso_lock:
        if _iso_mount["mounted"] or mountpoint_active():
            return {"ok": False, "message": "既にマウントされています。先にアンマウントしてください。"}
    if task_snapshot()["running"]:
        return {"ok": False, "message": "他の処理 (ダウンロード/書き込み) が実行中です。完了後に操作してください。"}
    ISO_MOUNT_POINT.mkdir(parents=True, exist_ok=True)
    backend, backing, part = "direct", "", ""
    try:
        if target_type == "image":
            if not target or not os.path.isabs(target):
                return {"ok": False, "message": "イメージパスは絶対パスで指定してください。"}
            if not os.path.isfile(target):
                return {"ok": False, "message": f"イメージファイルが存在しません: {target}"}
            if target.endswith(".qcow2"):
                if not shutil.which("qemu-nbd"):
                    return need_install(
                        "qemu-img", "qcow2イメージの操作に必要です。",
                        "qemu-img が見つかりません。インストールして続行できます。")
                ok, nbd = acquire_nbd()
                if not ok:
                    # nbdが使えない環境では guestfish (libguestfs) にフォールバック
                    return iso_mount_guest(target, nbd)
                r = subprocess.run(["qemu-nbd", "--connect", nbd, target],
                                   capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    return {"ok": False,
                            "message": f"qemu-nbd 接続失敗 ({nbd}): {(r.stderr or '').strip()}"}
                backend, backing, part = "nbd", nbd, f"{nbd}p2"
            else:
                if not shutil.which("losetup"):
                    return {"ok": False, "message": "イメージのマウントには losetup が必要です (util-linux) 。"}
                r = subprocess.run(["losetup", "-f", "--show", "-P", target],
                                   capture_output=True, text=True, timeout=60)
                loop = (r.stdout or "").strip()
                if r.returncode != 0 or not loop:
                    # loopが使えない環境 (コンテナ等) では guestfish にフォールバック
                    return iso_mount_guest(
                        target, f"loop デバイスの割り当てに失敗: {(r.stderr or '').strip()}")
                backend, backing, part = "loop", loop, f"{loop}p2"
            if not wait_for_dev(part):
                iso_cleanup_backend(backend, backing)
                return {"ok": False,
                        "message": f"パーティション {part} が現れません。イメージ内にVentoyが書き込まれていますか?"}
        else:
            if not target.startswith("/dev/"):
                return {"ok": False, "message": "USBドライブを選択してください (例: /dev/sdb) 。"}
            part = second_partition(target)
            if not os.path.exists(part):
                return {"ok": False, "message": f"{part} が見つかりません。先に Ventoy を書き込んでください。"}
        r = subprocess.run(["mount", part, str(ISO_MOUNT_POINT)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            if backend in ("nbd", "loop"):
                iso_cleanup_backend(backend, backing)
            return {"ok": False, "message": f"マウント失敗 ({part}): {(r.stderr or '').strip()}"}
        label = part_label(part)
        with _iso_lock:
            _iso_mount.update({"mounted": True, "target_type": target_type,
                               "target": target, "part": part, "label": label,
                               "backend": backend, "backing_dev": backing})
        return {"ok": True,
                "message": f"{part} を {ISO_MOUNT_POINT} にマウントしました" + (f" (ラベル: {label})" if label else "")}
    except Exception as e:  # noqa: BLE001
        try:
            subprocess.run(["umount", str(ISO_MOUNT_POINT)],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
        if backend in ("nbd", "loop") and backing:
            iso_cleanup_backend(backend, backing)
        return {"ok": False, "message": f"マウント中にエラー: {e}"}


def iso_mount_guest(target: str, prior_err: str):
    """nbd/loop が使えない環境向けに guestfish (libguestfs) で接続する。"""
    if not shutil.which("guestfish"):
        return need_install(
            "libguestfs",
            "この環境ではnbdカーネルモジュールが使えないため、"
            "カーネルモジュール不要でイメージを操作できる guestfish が必要です。",
            f"libguestfs が見つかりません ({prior_err})。インストールして続行できます。")
    ok, dev, fst_or_msg = guest_probe(target)
    if not ok:
        return {"ok": False, "message": fst_or_msg}
    label = ""
    try:
        r = subprocess.run(
            ["guestfish", "--ro", "-a", target, "-m", dev, "vfs-label", dev],
            capture_output=True, text=True, timeout=GUESTFISH_TIMEOUT,
            env=guest_env())
        if r.returncode == 0:
            label = (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        pass
    with _iso_lock:
        _iso_mount.update({"mounted": True, "target_type": "image",
                           "target": target, "part": f"{dev} (guestfish)",
                           "label": label, "backend": "guest", "backing_dev": target})
    _iso_files_cache["at"] = 0.0
    msg = f"{target} 内の {dev} にguestfishで接続しました"
    if fst_or_msg:
        msg += f" ({fst_or_msg})"
    if label:
        msg += f" (ラベル: {label})"
    return {"ok": True, "message": msg}


def iso_cleanup_backend(backend: str, backing: str):
    try:
        if backend == "nbd" and backing:
            subprocess.run(["qemu-nbd", "--disconnect", backing],
                           capture_output=True, timeout=60)
        elif backend == "loop" and backing:
            subprocess.run(["losetup", "-d", backing],
                           capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001
        pass


def iso_unmount():
    """マウント解除 + バックエンド後始末。戻り値 dict(ok, message)。"""
    if task_snapshot()["running"]:
        return {"ok": False, "message": "他の処理 (ダウンロード/書き込み) が実行中です。完了後に操作してください。"}
    with _iso_lock:
        if _iso_dl["running"]:
            return {"ok": False, "message": "ISOダウンロード実行中です。先にキャンセルしてください。"}
        backend = _iso_mount["backend"]
        backing = _iso_mount["backing_dev"]
        was_mounted = _iso_mount["mounted"] or mountpoint_active()
    if not was_mounted:
        return {"ok": False, "message": "マウントされていません。"}
    if backend == "guest":
        # guestfish は常駐接続を持たないため状態クリアのみ
        with _iso_lock:
            _iso_mount.update({"mounted": False, "target_type": "", "target": "",
                               "part": "", "label": "", "backend": "", "backing_dev": ""})
        _iso_files_cache.update({"files": [], "at": 0.0})
        return {"ok": True, "message": "切断しました。"}
    r = subprocess.run(["umount", str(ISO_MOUNT_POINT)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        r = subprocess.run(["umount", "-l", str(ISO_MOUNT_POINT)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"ok": False, "message": f"アンマウント失敗: {(r.stderr or '').strip()}"}
    if backend in ("nbd", "loop") and backing:
        iso_cleanup_backend(backend, backing)
    with _iso_lock:
        _iso_mount.update({"mounted": False, "target_type": "", "target": "",
                           "part": "", "label": "", "backend": "", "backing_dev": ""})
    return {"ok": True, "message": "アンマウントしました。"}


def iso_list_files():
    with _iso_lock:
        backend = _iso_mount["backend"]
        image = _iso_mount["backing_dev"] if backend == "guest" else ""
        dev = _iso_mount["part"].split(" ")[0] if backend == "guest" else ""
    if backend == "guest" and image and dev:
        return guest_list(image, dev)
    files = []
    if not mountpoint_active():
        return files
    try:
        for name in sorted(os.listdir(ISO_MOUNT_POINT)):
            if not name.lower().endswith(".iso"):
                continue
            try:
                size = os.path.getsize(ISO_MOUNT_POINT / name)
            except OSError:
                size = -1
            files.append({"name": name, "size": size})
    except OSError:
        pass
    return files


def iso_status():
    with _iso_lock:
        m = dict(_iso_mount)
        d = dict(_iso_dl)
    if not m["mounted"]:
        # 前回起動時の残留マウント等を反映
        m["mounted"] = mountpoint_active()
    d.pop("cancel", None)
    return {"mounted": m["mounted"], "target_type": m["target_type"],
            "target": m["target"], "part": m["part"], "label": m["label"],
            "backend": m["backend"], "mountpoint": str(ISO_MOUNT_POINT),
            "files": iso_list_files() if m["mounted"] else [],
            "download": d}


def iso_download_worker(url: str, dest: str, filename: str, total: int, guest=None):
    """URLからdestへダウンロードする。guest=(image, dev)指定時は完了後に
    guestfishでイメージ内へ書き込む。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Ventoy-UI"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
            while True:
                with _iso_lock:
                    if _iso_dl["cancel"]:
                        break
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                with _iso_lock:
                    _iso_dl["received"] += len(chunk)
        with _iso_lock:
            cancelled = _iso_dl["cancel"]
        if cancelled:
            with _iso_lock:
                _iso_dl.update({"running": False, "done": True,
                                "success": False, "message": "キャンセルしました。"})
            try:
                os.unlink(dest)
            except OSError:
                pass
            return
        if guest:
            image, dev = guest
            with _iso_lock:
                _iso_dl["message"] = f"イメージへ書き込み中...: {filename}"
            ok, msg = guest_upload(image, dev, dest, f"/{filename}")
            try:
                os.unlink(dest)
            except OSError:
                pass
            with _iso_lock:
                _iso_dl.update({"running": False, "done": True,
                                "success": ok,
                                "message": f"ダウンロード完了: {filename} ({msg})" if ok else msg})
            return
        with _iso_lock:
            _iso_dl.update({"running": False, "done": True,
                            "success": True,
                            "message": f"ダウンロード完了: {filename}"})
    except Exception as e:  # noqa: BLE001
        with _iso_lock:
            _iso_dl.update({"running": False, "done": True,
                            "success": False, "message": f"ダウンロード失敗: {e}"})
        try:
            if os.path.exists(dest):
                os.unlink(dest)
        except OSError:
            pass


def iso_start_download(url: str):
    """cachy-UI同様の検証後にバックグラウンドダウンロードを開始する。"""
    if not re.match(r"^https?://", url):
        return {"ok": False, "message": "http:// または https:// で始まるURLを入力してください"}
    if any(ch in url for ch in ('"', "'", "`", "\\", "\n", "\r", "$", ";", "&", "|", "<", ">")):
        return {"ok": False, "message": "URLに使用できない文字が含まれています"}
    fname = os.path.basename(urlparse(url).path)
    if not fname.lower().endswith(".iso"):
        return {"ok": False, "message": "URLの末尾が .iso となっている直接リンクを指定してください"}
    if any(ch in fname for ch in ('"', "'", "`", "\\", "$", ";", "&", "|", "<", ">")):
        return {"ok": False, "message": "ファイル名に使用できない文字が含まれています"}
    with _iso_lock:
        mounted = _iso_mount["mounted"]
        backend = _iso_mount["backend"]
        host_mounted = mountpoint_active()
        if _iso_dl["running"]:
            return {"ok": False, "message": "ダウンロードが既に実行中です"}
        image = _iso_mount["backing_dev"] if backend == "guest" else ""
        dev = _iso_mount["part"].split(" ")[0] if backend == "guest" else ""
    if backend == "guest":
        if not mounted or not image or not dev:
            return {"ok": False, "message": "保存先が接続されていません。先にマウントしてください。"}
        names = {f["name"] for f in guest_list(image, dev, force=True)}
        if fname in names:
            return {"ok": False, "message": f"同名のファイルが既に存在します: {fname}"}
        # /tmp は tmpfs で小さい場合があるためディスク backed な /var/tmp を使用
        tmpdir = "/var/tmp" if os.path.isdir("/var/tmp") else tempfile.gettempdir()
        dest = os.path.join(tmpdir, f"ventoy-ui-iso-{fname}")
        guest = (image, dev)
    else:
        if not (mounted and host_mounted):
            return {"ok": False, "message": "保存先がマウントされていません。先にマウントしてください。"}
        dest = str(ISO_MOUNT_POINT / fname)
        if os.path.exists(dest):
            return {"ok": False, "message": f"同名のファイルが既に存在します: {fname}"}
        guest = None
        tmpdir = str(ISO_MOUNT_POINT)
    total = -1
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Ventoy-UI"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit():
                total = int(length)
    except Exception:  # noqa: BLE001
        total = -1
    if total > 0:
        try:
            free = shutil.disk_usage(tmpdir).free
            if free < total:
                return {"ok": False,
                        "message": f"空き容量不足です (必要: 約{total // 1024 // 1024}MB / 空き: 約{free // 1024 // 1024}MB @ {tmpdir})"}
        except OSError:
            pass
    with _iso_lock:
        _iso_dl.update({"running": True, "filename": fname, "received": 0,
                        "total": total, "done": False, "success": None,
                        "message": "ダウンロード中...", "cancel": False})
    threading.Thread(target=iso_download_worker,
                     args=(url, dest, fname, total, guest), daemon=True).start()
    return {"ok": True, "filename": fname}


def iso_cancel_download():
    with _iso_lock:
        if not _iso_dl["running"]:
            return {"ok": False, "message": "実行中のダウンロードはありません"}
        _iso_dl["cancel"] = True
    return {"ok": True, "message": "キャンセル要求を送信しました"}


def install_pkg_task(package: str):
    task_log(f"{package} をインストールします (pacman -Sy --noconfirm {package})")
    rc = run_streaming(["pacman", "-Sy", "--noconfirm", package])
    if rc != 0:
        task_log(f"{package} のインストールに失敗しました。手動で 'sudo pacman -S {package}' を実行してください。")
    else:
        task_log(f"{package} のインストールが完了しました。")
    task_finish(rc)


# ========== Windows11用設定ファイル作成 ==========
# 下記の3コマンドと等価の処理をボタン一つで実行する。
#   curl -LO https://raw.githubusercontent.com/hirogura/ventoy-win/main/ventoy-win.sh
#   chmod +x ventoy-win.sh
#   ./ventoy-win.sh
# スクリプトは対話式 (確認プロンプト・複数ISO時の番号選択) のため、
# バックエンド側で stdin 応答を事前生成してパイプする。
WIN11_SCRIPT_URL = "https://raw.githubusercontent.com/hirogura/ventoy-win/main/ventoy-win.sh"
WIN11_WORKDIR = BASE_DIR / "win11"


def win11_list_root_isos(mnt: str):
    """スクリプトの select_iso と同じ find|sort でルート直下ISOを列挙する。"""
    try:
        find = subprocess.run(["find", mnt, "-maxdepth", "1", "-type", "f",
                               "-iname", "*.iso"],
                              capture_output=True, text=True, timeout=30)
        if find.returncode != 0:
            return []
        # 番号選択の順序をスクリプトと完全一致させるため sort(1) を使用
        s = subprocess.run(["sort"], input=find.stdout,
                           capture_output=True, text=True, timeout=30)
        if s.returncode != 0:
            return []
        return [ln for ln in s.stdout.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def win11_task(iso_name: str):
    rc = 1
    try:
        WIN11_WORKDIR.mkdir(parents=True, exist_ok=True)
        script = WIN11_WORKDIR / "ventoy-win.sh"
        # 1. curl -LO (毎回取得し直して最新化)
        task_log(f"$ curl -LO {WIN11_SCRIPT_URL}")
        try:
            req = urllib.request.Request(WIN11_SCRIPT_URL,
                                         headers={"User-Agent": "Ventoy-UI"})
            with urllib.request.urlopen(req, timeout=120) as resp, \
                    open(script, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as e:  # noqa: BLE001
            task_log(f"スクリプトの取得に失敗: {e}")
            task_finish(1)
            return
        task_log(f"取得完了 ({script.stat().st_size} bytes)")
        # 2. chmod +x
        task_log("$ chmod +x ventoy-win.sh")
        os.chmod(script, 0o755)
        # 3. 実行対象の解決: カード5でホストマウント中ならそれを引数に、
        #    否则スクリプトのUSB自動検出に任せる (引数なし)
        with _iso_lock:
            mounted = _iso_mount["mounted"]
            backend = _iso_mount["backend"]
        args = []
        isos = []
        if mounted and backend in ("direct", "nbd", "loop") and mountpoint_active():
            args = [str(ISO_MOUNT_POINT)]
            isos = win11_list_root_isos(str(ISO_MOUNT_POINT))
            task_log(f"対象: {ISO_MOUNT_POINT} (カード5でマウント中)")
        else:
            task_log("対象: USB自動検出 (スクリプトに任せます)")
        # 4. stdin 応答の事前生成 (確認 y + 複数ISO時の番号)
        stdin_lines = ["y"]
        if isos and len(isos) > 1:
            if not iso_name:
                task_log("エラー: ISOが複数あるため対象ファイル名を指定してください: "
                         + ", ".join(os.path.basename(p) for p in isos))
                task_finish(1)
                return
            names = [os.path.basename(p) for p in isos]
            if iso_name not in names:
                task_log(f"エラー: {iso_name} は対象内にありません: " + ", ".join(names))
                task_finish(1)
                return
            stdin_lines.append(str(names.index(iso_name) + 1))
            task_log(f"対象ISO: {iso_name} (選択肢 {names.index(iso_name) + 1})")
        elif iso_name:
            task_log(f"指定ISO名はスクリプトの自動検出時に参照されません (注意): {iso_name}")
            task_log("※ ISO名の埋め込みはスクリプトが検出・選択したファイルで行われます。")
        task_log(f"$ ./ventoy-win.sh {' '.join(args)}")
        proc = subprocess.Popen(
            ["bash", str(script)] + args, cwd=str(WIN11_WORKDIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        try:
            proc.stdin.write("\n".join(stdin_lines) + "\n")
            proc.stdin.close()
        except BrokenPipeError:
            pass
        for line in proc.stdout:
            task_log(line.rstrip())
        proc.wait()
        rc = proc.returncode
        task_log(f"終了コード: {rc}")
    except Exception as e:  # noqa: BLE001
        task_log(f"エラー: {e}")
        rc = 1
    task_finish(rc)


class Handler(BaseHTTPRequestHandler):
    server_version = f"Ventoy-UI/{VERSION}"

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                body = (BASE_DIR / "index.html").read_bytes()
            except OSError:
                body = b"<h1>index.html not found</h1>"
            self._send(200, body)
        elif parsed.path == "/api/version":
            self._send_json({"version": VERSION})
        elif parsed.path == "/api/status":
            devices, err = list_devices()
            self._send_json({
                "version": VERSION,
                "ventoy_dir": str(VENTOY_DIR),
                "ventoy_installed": find_ventoy_script().is_file(),
                "ventoy_version": get_ventoy_version(),
                "devices": devices,
                "devices_error": err,
                "images": list_image_candidates(),
            })
        elif parsed.path == "/api/task":
            self._send_json(task_snapshot())
        elif parsed.path == "/api/iso/status":
            self._send_json(iso_status())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            params = json.loads(raw.decode() or "{}") if raw else {}
        except json.JSONDecodeError:
            params = {}

        if parsed.path == "/api/download":
            if not task_start("download"):
                self._send_json({"error": "他の処理が実行中です"}, 409)
                return
            threading.Thread(target=download_ventoy_task, daemon=True).start()
            self._send_json({"ok": True})
        elif parsed.path == "/api/install":
            if not task_start("install"):
                self._send_json({"error": "他の処理が実行中です"}, 409)
                return
            threading.Thread(target=install_ventoy_task, args=(params,), daemon=True).start()
            self._send_json({"ok": True})
        elif parsed.path == "/api/update-self":
            if not task_start("update-self"):
                self._send_json({"error": "他の処理が実行中です"}, 409)
                return
            threading.Thread(target=self_update_task, daemon=True).start()
            self._send_json({"ok": True})
        elif parsed.path == "/api/restart":
            self._send_json({"ok": True, "message": "再起動します"})
            threading.Thread(target=restart_self, daemon=True).start()
        elif parsed.path == "/api/iso/mount":
            ttype = params.get("target_type", "usb")
            tgt = (params.get("image_path") if ttype == "image"
                   else params.get("device")) or ""
            self._send_json(iso_mount(ttype, tgt.strip()))
        elif parsed.path == "/api/iso/unmount":
            self._send_json(iso_unmount())
        elif parsed.path == "/api/iso/download":
            self._send_json(iso_start_download((params.get("url") or "").strip()))
        elif parsed.path == "/api/iso/cancel":
            self._send_json(iso_cancel_download())
        elif parsed.path == "/api/iso/install-pkg":
            pkg = (params.get("package") or "").strip()
            if pkg not in ISO_INSTALL_ALLOWLIST:
                self._send_json({"ok": False, "message": "インストール対象外のパッケージです"})
            elif not task_start("install-pkg"):
                self._send_json({"ok": False, "message": "他の処理が実行中です"}, 409)
            else:
                threading.Thread(target=install_pkg_task, args=(pkg,), daemon=True).start()
                self._send_json({"ok": True, "message": f"{pkg} をインストールします"})
        elif parsed.path == "/api/win11":
            if not task_start("win11"):
                self._send_json({"error": "他の処理が実行中です"}, 409)
                return
            threading.Thread(target=win11_task,
                             args=((params.get("iso_name") or "").strip(),),
                             daemon=True).start()
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def self_update_task():
    rc = run_streaming(["git", "-C", str(BASE_DIR), "pull", "--ff-only"])
    if rc != 0:
        task_log("git pull に失敗しました。リモート未設定の場合は手動で git pull してください。")
    task_finish(rc)


def restart_self():
    time.sleep(1)
    # systemd 配下なら service 再起動を試みる
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "restart", "ventoy-ui"], capture_output=True, text=True)
        if r.returncode == 0:
            return
    # フォールバック: 自プロセスを再実行
    try:
        os.execv(sys.executable, [sys.executable, str(BASE_DIR / "app.py")])
    except Exception as e:  # noqa: BLE001
        print(f"restart failed: {e}", flush=True)


def main():
    for msg in repair_ventoy_layout():
        print(f"Ventoy-UI repair: {msg}", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Ventoy-UI {VERSION} listening on :{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
