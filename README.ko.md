# agy-triad-dispatch

**Google Antigravity (`agy`)** 호스트를 위한 교차 패밀리 Triad 디스패치 및 v2 포멀 리뷰 플러그인입니다. 고정된 `triad-dispatch-lib 0.1.0.dev3` (`2d1e62e7de6f33fa13a918a39579c6b5e74def00`) 및 원본 캐리어 `admit_output` 검증을 기반으로 동작합니다.

## 개요

`agy-triad-dispatch`는 Antigravity 네이티브 플러그인 매니페스트(`plugin.json`, `.antigravity-plugin/plugin.json`), 5개의 관리형 프로바이더/선택기 래퍼, v2 포멀 리뷰 오케스트레이션 및 오프라인 검증 번들 라이브러리를 패키징합니다.

Antigravity 리더는 3개의 독립적인 모델 패밀리를 조율합니다:
- **Claude**: Anthropic Claude Code CLI (`claude`), `bin/claude_wrapper.py` 경유.
- **Google**: Google Antigravity CLI (`agy`), `bin/antigravity_wrapper.py` 경유 (우선 사용, agy 부재 시 Gemini Enterprise OAuth 폴백 `bin/gemini_wrapper.py` 지원).
- **Codex**: OpenAI Codex CLI (`codex`), `bin/codex_wrapper.py` 경유.

## 핵심 아키텍처

### 1. 번들 공유 라이브러리 (`triad-dispatch-lib 0.1.0.dev3`)
- `bin/triad_dispatch_lib/` 경로에 번들링되며, 커밋 `2d1e62e7de6f33fa13a918a39579c6b5e74def00` (Wheel SHA-256 `df1e84b56a7238bab2a019064c850c30bde5b6b15268a81a1e74d76df0c187b8`)에 고정됩니다.
- `bin/library_adoption.py`를 통해 `contracts/library-adoption.json`을 기준으로 오프라인 무결성을 검증합니다. 외부 환경(`PYTHONPATH` 등)에서 주입된 라이브러리는 즉시 거부(fail-closed)됩니다.

### 2. 관리형 래퍼 및 런처
부트스트랩은 `~/.local/bin` (`TRIAD_BOOTSTRAP_BIN_DIR`) 경로에 5개의 관리형 명령 래퍼를 설치합니다:
- `claude_wrapper.py`: stdin 프롬프트 전달, 네이티브 Plan Mode, `claude-json` 캐리어 검증을 적용한 Claude 디스패치.
- `codex_wrapper.py`: `--sandbox {read-only,workspace-write}`, `--model`, `--effort`, `--web`, 엄격한 OpenAI 구조화 스키마, `native-final` 캐리어 검증을 적용한 Codex 디스패치.
- `antigravity_wrapper.py`: 네이티브 plan mode, `--sandbox read-only`, 기본 컴포넌트 제외 프로파일, `agy-stream-json` 캐리어 검증을 적용한 Antigravity 디스패치.
- `gemini_wrapper.py`: 모드 독립적 읽기/검색 정책 및 `gemini-json` 캐리어 검증을 적용한 독립형 Gemini CLI 호환 래퍼.
- `review_round.py`: `bin/review_round_v2.py`를 호출하는 교차 패밀리 리뷰 오케스트레이터.

### 3. 원본 캐리어 `admit_output` 검증 강제
모든 v2 포멀 리뷰 레그는 `triad_dispatch_lib.output.admit_output`을 통해 프로바이더 원본 캐리어 페이로드에서 직접 평결을 승인합니다:
- `claude-json`: Claude stdout의 마크다운 펜스 또는 순수 JSON 검증.
- `native-final`: Codex의 `-o <last_msg_path>` 원본 바이트 검증.
- `agy-stream-json`: AGY plan mode stdout의 스트리밍 이벤트 라인 검증.
- `gemini-json`: Gemini stdout의 원본 응답 JSON 검증.

중첩된 캐리어 구조(예: `structured_output` 또는 `response` 내부)에 중복된 JSON 키가 존재할 경우, 조용히 덮어쓰지 않고 즉시 종료 코드 66 (`schema-fail`)으로 실패 처리(fail-closed)됩니다.

### 4. 6대 필수 리뷰 바인딩
모든 포멀 리뷰 평결은 다음 6가지 필드를 엄격하게 바인딩합니다:
1. `review_id`: 할당된 라운드 리뷰 ID와 일치.
2. `family`: `"claude"`, `"google"`, `"codex"` 중 하나.
3. `content_digest`: 준비된 리뷰 디렉터리의 SHA-256 다이제스트.
4. `leg_name`: 표준 레그 식별자 (`"claude"`, `"google"`, `"codex"`).
5. `attempt`: 시도 횟수 정수 (1부터 시작).
6. `route`: 명시적 라우트 문자열 (Antigravity는 `"agy"`, Gemini 폴백은 `"gemini"`, Claude 및 Codex는 `null`).

### 5. C23 Plan-Mode 복구
AGY plan mode 실행 중 `status == "ERROR"`, `vendor_exit_code == 0`이고 허용된 읽기가 관찰되었으며 금지된 도구 실행이 없는 정상적인 완료 후 거부 이벤트(`denied_post_completion` 또는 `_agy_finish_resubmitted`)가 발생한 경우:
- 래퍼는 `json_member_span`을 사용해 터미널 라인에서 원본 `structured_output` 텍스트 스팬을 추출합니다.
- 추출된 스팬을 `admit_output`의 `native-final` 캐리어로 안전하게 검증합니다.
- 허용된 읽기가 없거나 금지된 도구가 실행된 경우 복구를 거부합니다.

## 부트스트랩 설치 프로그램

작업 공간 외부의 인증된 로그인 환경에서 부트스트랩을 실행합니다:

```bash
# 관리형 런처 설치 및 라이브러리 검증
./scripts/bootstrap.sh --install

# 관리형 런처 제거 및 레거시 아티팩트 정리
./scripts/bootstrap.sh --remove
```

- 설치 변형 전 `library_adoption.verify_library()`를 사전 검증합니다.
- SHA-256 출처 검증과 함께 원자적 명령 그룹 단위로 설치됩니다.
- 학습된 분류기 패치는 `~/.config/agy-triad-dispatch/classifier-patches.json`에 보존됩니다.

## 패키징된 스킬

플러그인은 `skills/` 하위에 5개의 스킬을 등록합니다:
- `triad-claude-dispatch`: 승인된 Claude 호출 및 포멀 Claude 리뷰 레그 실행.
- `triad-codex-dispatch`: 승인된 Codex 호출 및 포멀 Codex 리뷰 레그 실행.
- `triad-antigravity-dispatch`: 승인된 Antigravity 호출 및 포멀 Google 리뷰 레그 실행.
- `triad-gemini-dispatch`: 독립형 Gemini CLI 호환성 및 Enterprise OAuth 폴백.
- `triad-cross-family-review`: Claude, Google, Codex 3개 패밀리를 조율하는 포멀 리뷰 오케스트레이터.

## 검증

오프라인에서 전체 테스트 스위트를 실행합니다:

```bash
python3 -m unittest discover -s tests -v
```
