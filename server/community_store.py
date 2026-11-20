"""커뮤니티 데이터(추천 도서·후기·하트·참가자) 영구 저장소.

무료 HF Space는 재시작·재배포 때 디스크가 초기화되므로, 데이터를 메모리 + 로컬 파일에
두고 변경분을 주기적으로 비공개 HF Dataset 저장소에 커밋한다. 시작 시 그 저장소에서 복원.

- HF_TOKEN               : Dataset 저장소 쓰기 권한 토큰 (없으면 로컬 파일만 사용)
- COMMUNITY_DATA_REPO    : 기본 "dongle0516/mbtibooktalk-data"
- COMMUNITY_DATA_DIR     : 로컬 저장 경로 (기본 server/../data)
- COMMUNITY_SYNC_SECONDS : 업로드 주기 (기본 60초)
- GITHUB_TOKEN           : 있으면 같은 데이터를 GitHub 저장소의 별도 브랜치에도 사본으로 올린다
- GITHUB_DATA_REPO       : 기본 "marvinloveyou12-rgb/mbtibooktalk"
- GITHUB_DATA_BRANCH     : 기본 "data" (main에 쓰면 코드 push가 충돌하므로 분리)
- GITHUB_SYNC_SECONDS    : GitHub 업로드 주기 (기본 300초)
"""
import json
import os
import threading
import time
import uuid

import httpx
from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))

HF_TOKEN = os.getenv("HF_TOKEN", "")
DATA_REPO = os.getenv("COMMUNITY_DATA_REPO", "dongle0516/mbtibooktalk-data")
LOCAL_DIR = os.getenv("COMMUNITY_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
SYNC_SECONDS = int(os.getenv("COMMUNITY_SYNC_SECONDS", "60"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_DATA_REPO", "marvinloveyou12-rgb/mbtibooktalk")
GITHUB_BRANCH = os.getenv("GITHUB_DATA_BRANCH", "data")
GITHUB_SYNC_SECONDS = int(os.getenv("GITHUB_SYNC_SECONDS", "300"))

DATA_FILE = "community.json"
LOG_FILE = "log.jsonl"

_lock = threading.RLock()
_data = {"recs": {}, "reviews": {}, "hearts": {}, "participants": {}}
_log_lines: list[str] = []
_dirty = False
_gh_dirty = False
_gh_last = 0.0
# 원격 복원에 실패한 상태에서 업로드하면 원격 데이터를 빈 값으로 덮어쓰게 되므로,
# 복원이 확인되기 전에는 업로드하지 않는다.
_remote_ready = False
_status = {"remote": bool(HF_TOKEN), "repo": DATA_REPO, "loaded_from": None,
           "last_sync": None, "last_error": None,
           "github": {"enabled": bool(GITHUB_TOKEN), "repo": GITHUB_REPO, "branch": GITHUB_BRANCH,
                      "last_sync": None, "last_error": None}}

_GH_README = """# MBTI Book Talk 커뮤니티 데이터

서버가 자동으로 올리는 사본입니다. 직접 수정하지 마세요(다음 동기화 때 덮어써집니다).
원본·복원용 백업은 Hugging Face 비공개 데이터셋에 있습니다.

- `community.json` — 현재 데이터 (recs: 유형별 추천 도서, reviews: 도서별 후기, hearts: 하트 누른 사용자, participants: 유형별 참가자)
- `log.jsonl` — 모든 저장 활동 기록 (시간순, 한 줄에 한 건)
"""


def _now() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


def _local_path(name: str) -> str:
    return os.path.join(LOCAL_DIR, name)


def _write_local():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    tmp = _local_path(DATA_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, ensure_ascii=False)
    os.replace(tmp, _local_path(DATA_FILE))
    with open(_local_path(LOG_FILE), "w", encoding="utf-8") as f:
        f.writelines(_log_lines)


def _apply_loaded(data: dict, log_text: str):
    for k in _data:
        _data[k] = data.get(k) or {}
    _log_lines[:] = [ln if ln.endswith("\n") else ln + "\n" for ln in log_text.splitlines() if ln.strip()]


def _load_remote() -> bool:
    """원격에서 복원. 성공(또는 원격이 아직 비어 있음 확인) 시 True."""
    from huggingface_hub import HfApi, hf_hub_download
    from huggingface_hub import errors as hf_errors

    # 1.x에서는 RemoteEntryNotFoundError가 EntryNotFoundError를 상속하지 않으므로 둘 다 잡는다
    missing = tuple(c for c in (getattr(hf_errors, "EntryNotFoundError", None),
                                getattr(hf_errors, "RemoteEntryNotFoundError", None)) if c)

    api = HfApi(token=HF_TOKEN)
    try:
        api.create_repo(DATA_REPO, repo_type="dataset", private=True, exist_ok=True)
    except Exception as e:
        print(f"[community] create_repo 실패(무시하고 진행): {e}")
    try:
        p = hf_hub_download(DATA_REPO, DATA_FILE, repo_type="dataset", token=HF_TOKEN,
                            force_download=True, local_dir=os.path.join(LOCAL_DIR, ".remote"))
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except missing + (hf_errors.RepositoryNotFoundError,):
        print("[community] 원격 데이터 없음 — 새로 시작")
        return True
    log_text = ""
    try:
        p = hf_hub_download(DATA_REPO, LOG_FILE, repo_type="dataset", token=HF_TOKEN,
                            force_download=True, local_dir=os.path.join(LOCAL_DIR, ".remote"))
        with open(p, encoding="utf-8") as f:
            log_text = f.read()
    except missing:
        pass
    with _lock:
        _apply_loaded(data, log_text)
        _write_local()
    _status["loaded_from"] = "remote"
    print(f"[community] 원격 복원 완료: 추천 {sum(len(v) for v in _data['recs'].values())}건, "
          f"후기 {sum(len(v) for v in _data['reviews'].values())}건")
    return True


def _load_local():
    try:
        with open(_local_path(DATA_FILE), encoding="utf-8") as f:
            data = json.load(f)
        log_text = ""
        if os.path.exists(_local_path(LOG_FILE)):
            with open(_local_path(LOG_FILE), encoding="utf-8") as f:
                log_text = f.read()
        with _lock:
            _apply_loaded(data, log_text)
        _status["loaded_from"] = "local"
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[community] 로컬 파일 읽기 실패: {e}")


def _upload():
    global _dirty
    from huggingface_hub import HfApi, CommitOperationAdd

    with _lock:
        if not _dirty:
            return
        payload = json.dumps(_data, ensure_ascii=False, indent=1).encode("utf-8")
        log_bytes = "".join(_log_lines).encode("utf-8")
        _dirty = False
    try:
        HfApi(token=HF_TOKEN).create_commit(
            DATA_REPO, repo_type="dataset",
            operations=[CommitOperationAdd(DATA_FILE, payload), CommitOperationAdd(LOG_FILE, log_bytes)],
            commit_message=f"community data {_now()}",
        )
        _status["last_sync"] = _now()
        _status["last_error"] = None
    except Exception as e:
        with _lock:
            _dirty = True
        _status["last_error"] = f"{_now()} {e}"
        print(f"[community] 업로드 실패(다음 주기에 재시도): {e}")


def _github_upload():
    """data 브랜치에 community.json·log.jsonl을 한 커밋으로 올린다(브랜치에는 데이터 파일만 둔다)."""
    global _gh_dirty, _gh_last
    with _lock:
        if not _gh_dirty:
            return
        payload = json.dumps(_data, ensure_ascii=False, indent=1)
        log_text = "".join(_log_lines)
        _gh_dirty = False
    _gh_last = time.time()
    api = f"https://api.github.com/repos/{GITHUB_REPO}/git"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        with httpx.Client(timeout=30.0, headers=headers) as c:
            r = c.get(f"{api}/ref/heads/{GITHUB_BRANCH}")
            parent = r.json()["object"]["sha"] if r.status_code == 200 else None
            if r.status_code not in (200, 404):
                r.raise_for_status()
            files = {"README.md": _GH_README, DATA_FILE: payload, LOG_FILE: log_text}
            tree = c.post(f"{api}/trees", json={"tree": [
                {"path": k, "mode": "100644", "type": "blob", "content": v} for k, v in files.items()]})
            tree.raise_for_status()
            commit = c.post(f"{api}/commits", json={
                "message": f"community data {_now()}", "tree": tree.json()["sha"],
                "parents": [parent] if parent else []})
            commit.raise_for_status()
            sha = commit.json()["sha"]
            if parent:
                c.patch(f"{api}/refs/heads/{GITHUB_BRANCH}", json={"sha": sha}).raise_for_status()
            else:
                c.post(f"{api}/refs", json={"ref": f"refs/heads/{GITHUB_BRANCH}", "sha": sha}).raise_for_status()
        _status["github"]["last_sync"] = _now()
        _status["github"]["last_error"] = None
    except Exception as e:
        with _lock:
            _gh_dirty = True
        detail = getattr(getattr(e, "response", None), "text", "")[:200]
        _status["github"]["last_error"] = f"{_now()} {e} {detail}".strip()
        print(f"[community] GitHub 업로드 실패(다음 주기에 재시도): {e} {detail}")


def _sync_loop():
    global _remote_ready, _gh_dirty
    while True:
        if not _remote_ready:
            try:
                _remote_ready = _load_remote()
                _gh_dirty = _remote_ready  # 복원 직후 GitHub 사본도 최신으로 맞춘다
            except Exception as e:
                _status["last_error"] = f"{_now()} 복원 실패: {e}"
                print(f"[community] 원격 복원 실패(재시도 예정): {e}")
        else:
            _upload()
            if GITHUB_TOKEN and time.time() - _gh_last >= GITHUB_SYNC_SECONDS:
                _github_upload()
        time.sleep(SYNC_SECONDS if _remote_ready else 15)


def start():
    _load_local()
    if HF_TOKEN:
        threading.Thread(target=_sync_loop, daemon=True).start()
    else:
        print("[community] HF_TOKEN 미설정 — 로컬 파일에만 저장(재시작 시 유실될 수 있음)")


def flush():
    """종료 직전 호출: 남은 변경분 업로드."""
    if HF_TOKEN and _remote_ready:
        _upload()
        if GITHUB_TOKEN:
            _github_upload()


def is_ready() -> bool:
    return not HF_TOKEN or _remote_ready


def status() -> dict:
    with _lock:
        counts = {
            "recs": sum(len(v) for v in _data["recs"].values()),
            "reviews": sum(len(v) for v in _data["reviews"].values()),
            "hearts": sum(len(v) for v in _data["hearts"].values()),
            "participants": len({u for v in _data["participants"].values() for u in v}),
            "log_lines": len(_log_lines),
        }
    return {**_status, "ready": is_ready(), "pending_upload": _dirty, "counts": counts}


def _changed(event: dict):
    global _dirty, _gh_dirty
    _log_lines.append(json.dumps({"at": _now(), **event}, ensure_ascii=False) + "\n")
    _dirty = True
    _gh_dirty = True
    try:
        _write_local()
    except OSError as e:
        print(f"[community] 로컬 파일 저장 실패(원격 백업은 계속): {e}")


def _clip(v, n: int) -> str:
    return str(v or "")[:n]


def _add_participant(mbti: str, user_key: str):
    if not mbti or not user_key:
        return
    lst = _data["participants"].setdefault(mbti, [])
    if user_key not in lst:
        lst.append(user_key)


# ── 조회 ─────────────────────────────────────────────────────
def snapshot() -> dict:
    """추천 도서(하트 수 포함), 하트 수, 유형별 참가자 수."""
    with _lock:
        hearts = {k: len(v) for k, v in _data["hearts"].items()}
        recs = {t: [{**r, "hearts": hearts.get(f"rc_{t}_{r['id']}", 0)} for r in lst]
                for t, lst in _data["recs"].items()}
        participants = {t: len(v) for t, v in _data["participants"].items()}
    # persistent=False면 재시작 시 유실될 수 있으므로 브라우저는 자기 사본을 덮어쓰지 않는다
    return {"recs": recs, "hearts": hearts, "participants": participants,
            "persistent": bool(HF_TOKEN) and _remote_ready}


def get_reviews(bkey: str) -> list[dict]:
    with _lock:
        return list(_data["reviews"].get(bkey, []))


# ── 쓰기 ─────────────────────────────────────────────────────
def add_rec(mbti: str, rec: dict) -> dict:
    item = {
        "id": uuid.uuid4().hex[:12],
        "title": _clip(rec.get("title"), 300), "author": _clip(rec.get("author"), 200),
        "isbn": _clip(rec.get("isbn"), 20), "cover": _clip(rec.get("cover"), 500),
        "publisher": _clip(rec.get("publisher"), 200), "review": _clip(rec.get("review"), 2000),
        "userKey": _clip(rec.get("userKey"), 64), "createdAt": _now(),
    }
    with _lock:
        _data["recs"].setdefault(mbti, []).insert(0, item)
        _add_participant(mbti, item["userKey"])
        _changed({"action": "add_rec", "mbti": mbti, **item})
    return {**item, "hearts": 0}


def add_review(bkey: str, rev: dict) -> dict:
    item = {
        "id": uuid.uuid4().hex[:12],
        "text": _clip(rev.get("text"), 2000), "mbti": _clip(rev.get("mbti") or "MBTI", 8),
        "userKey": _clip(rev.get("userKey"), 64),
        "dateStr": datetime.now(_KST).strftime("%Y. %-m. %-d."), "createdAt": _now(),
    }
    with _lock:
        _data["reviews"].setdefault(bkey, []).insert(0, item)
        _add_participant(item["mbti"], item["userKey"])
        _changed({"action": "add_review", "bkey": bkey, **item})
    return item


def set_heart(key: str, user_key: str, on: bool, mbti: str = "") -> int:
    with _lock:
        users = _data["hearts"].setdefault(key, [])
        if on and user_key not in users:
            users.append(user_key)
        elif not on and user_key in users:
            users.remove(user_key)
        else:
            return len(users)
        _add_participant(mbti, user_key)
        _changed({"action": "heart_on" if on else "heart_off", "key": key, "userKey": user_key})
        return len(users)


def participate(mbti: str, user_key: str):
    with _lock:
        before = len(_data["participants"].get(mbti, []))
        _add_participant(mbti, user_key)
        if len(_data["participants"].get(mbti, [])) != before:
            _changed({"action": "participate", "mbti": mbti, "userKey": user_key})


def import_local(user_key: str, recs: dict, reviews: dict, hearts: list) -> dict:
    """브라우저 localStorage에만 있던 기존 데이터를 한 번 옮겨 온다. 같은 항목은 건너뜀."""
    added = {"recs": 0, "reviews": 0, "hearts": 0}
    with _lock:
        for mbti, lst in (recs or {}).items():
            existing = _data["recs"].setdefault(mbti, [])
            seen = {(r.get("userKey"), r.get("createdAt"), r.get("title")) for r in existing}
            for r in lst or []:
                sig = (r.get("userKey") or user_key, r.get("createdAt"), r.get("title"))
                if sig in seen or not r.get("title"):
                    continue
                existing.append({
                    "id": _clip(r.get("id"), 32) or uuid.uuid4().hex[:12],
                    "title": _clip(r.get("title"), 300), "author": _clip(r.get("author"), 200),
                    "isbn": _clip(r.get("isbn"), 20), "cover": _clip(r.get("cover"), 500),
                    "publisher": _clip(r.get("publisher"), 200), "review": _clip(r.get("review"), 2000),
                    "userKey": sig[0], "createdAt": _clip(r.get("createdAt"), 40) or _now(),
                })
                seen.add(sig)
                _add_participant(mbti, sig[0])
                added["recs"] += 1
            existing.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
        for bkey, lst in (reviews or {}).items():
            existing = _data["reviews"].setdefault(bkey, [])
            seen = {(r.get("userKey"), r.get("createdAt")) for r in existing}
            for r in lst or []:
                sig = (r.get("userKey") or user_key, r.get("createdAt"))
                if sig in seen or not r.get("text"):
                    continue
                existing.append({
                    "id": _clip(r.get("id"), 32) or uuid.uuid4().hex[:12],
                    "text": _clip(r.get("text"), 2000), "mbti": _clip(r.get("mbti") or "MBTI", 8),
                    "userKey": sig[0], "dateStr": _clip(r.get("dateStr"), 20),
                    "createdAt": _clip(r.get("createdAt"), 40) or _now(),
                })
                seen.add(sig)
                added["reviews"] += 1
            existing.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
        for key in hearts or []:
            users = _data["hearts"].setdefault(_clip(key, 200), [])
            if user_key not in users:
                users.append(user_key)
                added["hearts"] += 1
        if any(added.values()):
            _changed({"action": "import_local", "userKey": user_key, **added})
    return added
