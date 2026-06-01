# Agent Context — 모든 워커 prompt 진입 시 자동 주입

> 풀 SoT (`docs/*`) 를 매번 읽지 않도록, 핵심만 한 페이지로 요약.
> 워커는 이 문서를 먼저 본 후 필요시 `docs/` 의 해당 섹션 펼침.

---

## Palette 한 줄 요약

**지인 네트워크 기반 데이팅 앱**. 8가지 컬러 타입으로 자기표현, 2단계 승인 매칭. Kotlin/Spring Boot + React TS. 베타 운영 중 (https://www.palette.ai.kr).

---

## 절대 룰 (어기면 reviewer 차단)

1. **frontend 에 raw hex 금지** (`#xxx` / `#xxxxxx`). 디자인 토큰 (`bg-primary` 등) 또는 `frontend/src/styles/theme.css` 만 허용.
2. **DDD import 방향**: domain ← persistence/application, application ← presentation. `domain/*` 에서 Spring/JPA import 금지.
3. **`@AuthenticationPrincipal AuthUser` non-null**. SecurityConfig 의 permitAll 매처가 그 path 잡으면 NPE — `**` 사용 시 반드시 ADR (`0002` 참조).
4. **main 직접 push 금지**, `data/` / `.env` / 시크릿 commit 금지.
5. **사용자 노출 한국어** ("팔레트" 사용, "Palette" 영문 금지).
6. **테스트 통과 후만 push**. 백엔드 `./gradlew test`, 프론트 `cd frontend && npx vitest run`.
7. **시드 격리** — `SeedUserPolicy` 통과 (ADR `0003`).
8. **`ResponseEntity.badRequest().build()` 금지** — 구체 예외 throw.

## 패키지 위치 cheat sheet

| 작업 | 위치 |
|---|---|
| API 추가 | `src/main/kotlin/kr/ai/palette/presentation/{domain}/` |
| 도메인 모델 | `src/main/kotlin/kr/ai/palette/domain/{domain}/` |
| Entity | `src/main/kotlin/kr/ai/palette/persistence/{domain}/` |
| 외부 연동 | `src/main/kotlin/kr/ai/palette/infrastructure/` |
| 화면 | `frontend/src/app/components/` |
| API 클라이언트 | `frontend/src/lib/api/` |
| 디자인 토큰 | `frontend/src/styles/theme.css` |

→ 상세 매핑: `docs/ARCHITECTURE.md` §3, §4

---

## 워커 흐름 cheat sheet

### Issue 받으면 (executor)
1. 본문의 `의도 / 영향 범위 / 완료 기준 / non-goal` 읽기
2. scope > 10 파일 또는 도메인 2개 이상 → `agent:needs_human` 으로 escalate
3. `auto/{issue-number}` 브랜치
4. 코드 변경 (위 절대 룰 준수)
5. 테스트 실행 — 실패 시 push 안 함, 코멘트 + `needs_human`
6. PR 생성 (본문에 `Closes #{issue-number}` + Test plan + SoT 갱신)
7. PR 에 `agent:ready`, 원 issue close + `in_progress` 라벨 제거

### PR 받으면 (reviewer)
1. diff 가져옴
2. 5 차원 점검: 정확성 / 보안 / 베타 안정성 / 코드 품질 / 테스트
3. SoT 갱신 누락 체크 (`CONVENTIONS.md` §6.3 표)
4. inline 코멘트 + 요약 코멘트 (`🟢/🟡/🔴`)
5. `agent:needs_human` 부여 — approve/request-changes 안 함 (사람만)

---

## SoT 갱신 강제 룰 (PR 시)

| 변경 | 갱신 필수 |
|---|---|
| 새 컨트롤러 / API path | `docs/ARCHITECTURE.md §4` |
| 새 Aggregate / Entity | `docs/ARCHITECTURE.md §3.3` |
| 새 외부 연동 | `docs/ARCHITECTURE.md §7` |
| SecurityConfig 매처 | `docs/ARCHITECTURE.md §5.1` + ADR |
| 디자인 토큰 변경 | ADR |
| 새 컨벤션/룰 | `docs/CONVENTIONS.md` + ADR |
| 운영 변경 (swap/DB/시크릿) | `docs/RUNBOOK.md` |
| 큰 변경 (>10 파일) | ADR |

ADR 위치: `docs/DECISIONS/NNNN-{slug}.md` (현재 `0001` ~ `0005`)

---

## 현재 운영 상태 (요약)

- prod: EC2 t3.small, swap 2GB, 베타 가동 (ADR `0004`)
- 시드 25명 활성, 신규 가입자에게 격리 (ADR `0003`)
- 토글 (소개받기/주선받기) 백엔드 미적용 — P1 fix 필요 (ADR `0005`)
- AI: OpenAI dummy key 시 stub fallback (`OpenAIService.kt`)
- 채팅 / 알림 화면 / 매칭 상세 — frontend mock (MVP 제한)

상세: `docs/RUNBOOK.md`

---

## 라벨 / 워커 권한

| 라벨 | 의미 |
|---|---|
| `agent:ready` | 워커 작업 가능 |
| `agent:in_progress` | 워커 점유 중 (다른 워커 들어오지 마라) |
| `agent:needs_human` | 사람 결정 대기 — 워커는 손대지 마라 |

| 워커 | 권한 |
|---|---|
| `palette-pm` | read + 라벨 |
| `palette-executor` | write (브랜치/commit/PR), main 직접 push 금지 |
| `palette-reviewer` | read + comment only, approve/request-changes 금지 |

---

## 모든 자동 코멘트 prefix

```
🤖 {agent-name} · {timestamp KST} · {short summary}
```

예: `🤖 palette-executor · 2026-05-31 00:15 · PR #42 생성, 테스트 ✅`
