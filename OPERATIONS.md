# MBTI Book Talk 운영 가이드

사이트를 운영하면서 알아야 할 것들을 정리한 문서입니다. (2026-11-20 기준)

> 이 저장소는 공개되어 있으므로 비밀번호·토큰 같은 비밀 값은 이 문서에 적지 않습니다.

---

## 1. 주요 주소

| 용도 | 주소 |
|---|---|
| 사이트 (학생·사용자용) | https://dongle0516-mbtibooktalk.hf.space |
| 관리자 페이지 | https://dongle0516-mbtibooktalk.hf.space/admin.html |
| HF Space 설정 (시크릿·재시작·로그) | https://huggingface.co/spaces/dongle0516/mbtibooktalk/settings |
| 데이터 원본 백업 (비공개) | https://huggingface.co/datasets/dongle0516/mbtibooktalk-data |
| 데이터 사본 (GitHub `data` 브랜치, 공개) | https://github.com/marvinloveyou12-rgb/mbtibooktalk/tree/data |
| 코드 (GitHub `main` 브랜치) | https://github.com/marvinloveyou12-rgb/mbtibooktalk |
| 저장 상태 확인 | https://dongle0516-mbtibooktalk.hf.space/api/community/status |

---

## 2. 관리자 페이지

- **들어가는 법**: 사이트 상단 **MBTI Book Talk 로고를 3초 안에 5번** 누르거나, 관리자 페이지 주소로 바로 접속
- **할 수 있는 일**: 추천 도서·후기 목록 보기, 검색·유형별 필터, **삭제**(삭제 → 정말 삭제 두 번 눌러야 실행)
- **비밀번호 변경**: 관리자 페이지 오른쪽 위 **비밀번호 변경** 버튼 (4자 이상)
- 비밀번호를 10분 안에 5번 틀리면 그 접속은 10분간 차단됩니다.
- 삭제는 되돌리기 버튼이 없지만, 지운 내용 전체가 `log.jsonl`에 남아 있어 복구할 수 있습니다.
- **비밀번호를 잊었을 때**: HF 데이터셋의 `admin.json` 파일을 삭제하고 Space를 재시작하면 HF 시크릿 `ADMIN_PASSWORD` 값으로 돌아갑니다.

---

## 3. 데이터 저장 방식

사용자가 저장한 추천 도서·후기·하트·참가자 기록은 **서버에 저장되어 모두가 함께 보고, 다시 접속해도 남아 있습니다.**

```
사용자 저장 ─▶ 서버 ─┬─(1분 안)─▶ HF 비공개 데이터셋   ← 원본·복원용
                    └─(5분 안)─▶ GitHub data 브랜치  ← 공개 사본
```

| 파일 | 내용 | HF 데이터셋 | GitHub data |
|---|---|---|---|
| `community.json` | 현재 데이터 (추천·후기·하트·참가자) | ✅ | ✅ |
| `log.jsonl` | 모든 저장·삭제 활동 기록 (시간순) | ✅ | ✅ |
| `admin.json` | 관리자 비밀번호(해시) | ✅ | ❌ 올리지 않음 |

- 서버가 재시작·재배포되어도 HF 데이터셋에서 자동 복원됩니다.
- 서버가 비정상 종료되면 **마지막 1분 이내** 저장분은 잃을 수 있습니다. (정상 재배포 때는 직전에 백업)
- 무료 Space는 오래 접속이 없으면 잠듭니다. 다시 깨어날 때 데이터는 그대로이고 첫 접속만 조금 느립니다.
- **GitHub data 브랜치는 공개**입니다. 후기 내용과 익명 사용자 ID가 보입니다. (사이트에서도 원래 모두에게 보이는 내용)
- data 브랜치 파일은 서버가 덮어쓰므로 **직접 수정하지 마세요.**

---

## 4. HF Space 시크릿 (Settings → Variables and secrets)

| 이름 | 용도 | 없으면 |
|---|---|---|
| `YES24_API_KEY` | 도서 검색·표지·책 소개 (YES24 Open API) | 검색·표지가 비어 나옴 |
| `HF_TOKEN` | 데이터를 HF 데이터셋에 백업·복원 | 재시작 때 데이터 유실 위험 |
| `GITHUB_TOKEN` | 데이터를 GitHub data 브랜치에 사본 업로드 | GitHub 사본만 멈춤 (사이트는 정상) |
| `ADMIN_PASSWORD` | 관리자 초기·복구용 비밀번호 | 관리자 페이지 사용 불가 |
| `NL_API_KEY` | 국립중앙도서관 API | 국중도 검색 불가 |
| `DATA4LIB_API_KEY` | 정보나루 API | 사서 Q&A 보강 불가 |
| `SEMANTIC_ENABLED=0` (변수) | 무료 CPU 과부하 방지용 시맨틱 검색 끄기 | 500/503 오류 위험 |

시크릿을 바꾸면 Space가 자동 재시작되고, 1~3분 뒤 반영됩니다.

---

## 5. 토큰·키 만료 대비

| 항목 | 만료되면 생기는 일 | 해결 |
|---|---|---|
| GitHub 토큰 | 코드 push 실패, GitHub data 사본 중단 | 새 fine-grained 토큰(저장소 `mbtibooktalk`, Contents: Read and write) 발급 → ① 로컬 git remote 주소 ② Space 시크릿 `GITHUB_TOKEN` 둘 다 교체 |
| HF 토큰 | 데이터 백업 중단, HF push 실패 | 새 Write 토큰 발급 → Space 시크릿 `HF_TOKEN`·로컬 git remote 교체 |
| YES24 API 키 | 도서 검색·표지 안 나옴 | [예스24 개발자센터](https://developers.yes24.com)에서 재발급 → `YES24_API_KEY` 교체 |

YES24 API 한도: 초당 10회, 하루 20,000회.

---

## 6. 코드 수정·배포

```bash
cd ~/mbtibooktalk
git add .
git commit -m "변경 내용"
git push origin main     # GitHub + HF Space에 한 번에 배포
```

- push 후 HF가 다시 빌드하는 데 **몇 분** 걸립니다.
- 페이지는 매번 새로 확인하도록 설정되어 있지만, 오래된 화면이 보이면 **Cmd+Shift+R**(Windows: Ctrl+Shift+R)로 새로고침하세요.

---

## 7. 문제가 생겼을 때

| 증상 | 먼저 확인할 것 |
|---|---|
| 표지·검색이 안 나옴 | 시크릿 `YES24_API_KEY` 존재 여부 → Space 로그에 `[yes24]` 메시지 |
| 저장한 글이 사라짐 | `/api/community/status`에서 `"remote": true`, `"ready": true`, `last_error` 확인 |
| GitHub data가 갱신 안 됨 | status의 `github.last_error` (대개 토큰 만료) |
| 사이트 500/503 오류 | Space 페이지 Running 배지 → CPU/RAM, Logs → Container 탭 |
| 관리자 로그인 안 됨 | 10분 차단 여부, 비밀번호 분실 시 2번 항목 참고 |
