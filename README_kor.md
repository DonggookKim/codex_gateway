# codex_gateway

**codex** (기본) 및 **opencode** 백엔드 위에서 프로젝트 단위 로컬 세션을
구동하는 Discord 컨트롤 플레인 게이트웨이. 하나의 게이트웨이 프로세스가
두 백엔드를 모두 운용하며, 세션은 생성 시점에 프로젝트와 백엔드에
바인딩됩니다. opencode 세션의 권한 승인은 Discord 버튼으로 원격 처리할 수
있습니다.

> English version: [README.md](./README.md)

- [빠른 시작](#빠른-시작)
- [일상 사용 흐름](#일상-사용-흐름)
- [채널 모델](#채널-모델) · [슬래시 명령어 레퍼런스](#슬래시-명령어-레퍼런스) · [백엔드](#백엔드)
- [환경 설정](#환경-설정) · [로컬 Ollama를 opencode 프로바이더로](#로컬-ollama를-opencode-프로바이더로)
- [저장소 레이아웃](#저장소-레이아웃) · [운영 메모](#운영-메모) · [리포지토리 구조](#리포지토리-구조)

개발 히스토리·이전 노트는 [docs/HISTORY.md](./docs/HISTORY.md),
아키텍처와 영속 상태 모델은 [docs/DESIGN.md](./docs/DESIGN.md),
`mistral-nemo:latest` 기본값을 정한 로컬 Ollama tool-call 평가 매트릭스는
[docs/local-ollama-toolcall-evaluation.md](./docs/local-ollama-toolcall-evaluation.md)
를 참고하세요.

## 빠른 시작

```bash
# 1. 누락된 항목 점검(읽기 전용) 또는 자동 설치 + bin 심볼릭 등록
bash codex_gateway/scripts/install.sh             # 점검만
bash codex_gateway/scripts/install.sh --install   # 설치 + ~/.local/bin/codex-gateway 생성

# 2. .env 작성 (--install 모드는 .env.example을 복사해 줍니다)
$EDITOR .env

# 3. 실행
codex-gateway                                     # bin 심볼릭이 $PATH에 있을 때
# 또는:
bash codex_gateway/scripts/run_gateway.sh
```

codex만 사용하는 최소 `.env` 예시:

```dotenv
DISCORD_GATEWAY_TOKEN=...           # 또는 DISCORD_TOKEN
CONTROL_GUILD_ID=...                # 또는 DISCORD_GUILD_ID
CONTROL_CHANNEL_ID=...
ALLOWED_USER_IDS=123,456            # 쉼표로 구분된 Discord user ID
CODEX_BIN=codex
CODEX_CWD=/path/to/repo
STATE_ROOT=~/.codex_gateway/state
RUNTIME_ROOT=~/.codex_gateway/runtime
PROJECTS_FILE=~/.codex_gateway/projects.json
```

opencode 백엔드도 함께 켜려면 `OPENCODE_GATEWAY_ENABLED=1`과
`OPENCODE_PROVIDER_ID` / `OPENCODE_MODEL_ID`를 추가하세요. 자세한 내용은
[환경 설정 → OpenCode 런타임](#opencode-런타임-옵션)을 참고하세요.

`install.sh`에서 `BIN_DIR=` (빈 값) 으로 호출하면 bin 심볼릭을 생성하지
않고, `BIN_DIR=...`로 지정하면 다른 위치에 둘 수 있습니다. `~/.local/bin`이
`$PATH`에 없으면 스크립트가 rc 파일에 추가할 줄을 출력해 줍니다.

## 일상 사용 흐름

게이트웨이가 떠 있고 프로젝트가 등록된 상태에서, 전형적인 Discord 사용
흐름은 다음과 같습니다.

| 단계 | 명령 | 위치 |
|---|---|---|
| 프로젝트 선택 (글로벌) | `/project_select <project_id>` | 컨트롤 채널 |
| 세션 시작 | `/session_new <label> [codex\|opencode]` | 컨트롤 또는 프로젝트 채널 |
| (opencode만) 모델 먼저 지정 | `/model_select <provider/model>` | 컨트롤 또는 프로젝트 채널 |
| 프롬프트 실행 | `/ask <prompt>` | 컨트롤 또는 프로젝트 채널 |
| 현재 상태 확인 | `/status`, `/current`, `/last` | 같은 채널 |
| 장시간 작업 감시 | `/watch on [interval]` | 컨트롤 채널 |
| 권한 요청 승인 (opencode) | ✅ / ♾️ / ❌ 버튼, 또는 `/perm_allow` / `/perm_reject` | 프로젝트 채널 |
| 로컬 TUI 연결 | `/tui` → 출력된 명령 실행 | 같은 채널 |
| 중지 | `/stop` | 같은 채널 |

전체 명령 목록과 채널 바인딩 규칙은
[슬래시 명령어 레퍼런스](#슬래시-명령어-레퍼런스)를 참고하세요.

## 채널 모델

두 종류의 Discord 채널이 사용됩니다.

- **컨트롤 채널** (`CONTROL_CHANNEL_ID`): 프로젝트 간 횡단 명령용. 게이트웨이
  글로벌 선택 상태에 작용합니다.
- **프로젝트 채널** (`ProjectDefinition.project_channel_id`): 프로젝트 단위
  스코프. 이 채널에서 호출되는 명령은 해당 채널이 가리키는 프로젝트를
  자동으로 대상 삼으며, 세션은 `ProjectDefinition.active_session_id`를
  사용합니다. 게이트웨이 글로벌 선택 상태는 건드리지 않습니다.

채널 인식 명령: `/ask`, `/session_select`, `/session_list`,
`/session_new`, `/model_select`, `/current`, `/status`, `/last`,
`/tui`, `/stop`, `/perms`, `/perm_allow`, `/perm_reject`.
나머지 명령(`/project_select`, `/project_list`, `/watch`)은 컨트롤
채널 전용입니다. `/watch`는 설계상 단일 글로벌 대상을 유지합니다.

## 슬래시 명령어 레퍼런스

프로젝트 / 세션 선택:

- `/project_select <project_id>`, `/project_list`
- `/session_select <session_id>`, `/session_list`
- `/session_new <label> [backend?]` — `backend`는 `codex` (기본) 또는
  `opencode`
- `/model_select <model_profile>` — 프로젝트별로 저장되며, 다음 번
  **opencode** 세션 생성 시 적용됩니다. codex 세션은 이 값을 무시하고
  항상 `ProjectDefinition.default_model_profile`을 사용합니다.

실행 / 관찰:

- `/ask <prompt>` — 채널/컨트롤에 바인딩된 세션에서 실행
- `/status`, `/current`, `/last`, `/tui`
- `/watch <on|off> [interval]` — 활성 실행이 idle 상태가 될 때까지 상태
  스냅샷을 반복 게시. 기본 간격 `10m`. 형식: `30s`, `1m`, `10m`.
- `/stop`

OpenCode 권한 승인 (opencode 세션에서만 의미 있음):

- `/perms` — 대기 중인 권한 요청 목록
- `/perm_allow [permission_id?] [scope=once|always]`
- `/perm_reject [permission_id?]`

opencode가 `permission.asked`를 발생시키면 게이트웨이가 프로젝트 채널에
3개 버튼 메시지(`✅ Once / ♾️ Always / ❌ Reject`)도 함께 게시합니다.
위 슬래시 명령은 그 대안으로 사용할 수 있는 텍스트 입력 경로입니다.

## 백엔드

`codex` (기본):
- `codex exec resume <thread_ref>`을 서브프로세스로 구동합니다.
- 각 세션은 `STATE_ROOT` 아래 자기만의 `codex-home/.codex`를 가지며,
  `auth.json`은 `~/.codex/auth.json`에서 링크됩니다.
- `model_profile`은 생성 시점에
  `ProjectDefinition.default_model_profile`에서 결정되어 고정됩니다.

`opencode`:
- 게이트웨이가 단일·장기 `opencode serve` 프로세스를 띄우고, HTTP + SSE
  API를 통해 세션을 운용합니다. 권한 요청은 Discord 버튼으로 전달되며,
  운영자 응답이 없을 경우 세션을 중단시키는 타임아웃이 설정되어 있습니다.
- `model_profile`은 opencode의 `provider/model` 표기를 따릅니다
  (예: `model-connect/Qwen3.5-...`). 기본값은 `OPENCODE_PROVIDER_ID` /
  `OPENCODE_MODEL_ID`에서 오고, `/model_select`로 프로젝트 단위 오버라이드
  가능합니다.

같은 게이트웨이 프로세스가 두 백엔드를 모두 처리합니다. 분기는 세션의
`backend` 필드 단위이며, 백엔드별 게이트웨이를 따로 띄우지 않습니다.

## 환경 설정

### 필수

| 변수 | 용도 |
|---|---|
| `DISCORD_GATEWAY_TOKEN` (또는 `DISCORD_TOKEN`) | 게이트웨이 봇 토큰 |
| `CONTROL_GUILD_ID` (또는 `DISCORD_GUILD_ID`) | 컨트롤용 Discord 길드 |
| `CONTROL_CHANNEL_ID` | 컨트롤 채널 ID |
| `ALLOWED_USER_IDS` | 허용된 Discord 사용자 ID (쉼표 구분) |
| `CODEX_BIN` | `codex` 바이너리 경로 (기본 `codex`) |
| `CODEX_CWD` | codex 세션의 작업 디렉터리 |
| `STATE_ROOT` | 영속 프로젝트/세션 메타데이터 경로 |
| `RUNTIME_ROOT` | 세션별 런타임 아티팩트 경로 |
| `PROJECTS_FILE` | 등록된 프로젝트 목록 파일 |

추가 저장소 옵션: `STATE_FILE`, `TMP_DIR`, `LAST_RESPONSE_FILE`,
`CODEX_HOME_PARENT`, `CODEX_HOME_SEED_FROM`, `CODEX_STATUS_HOME`.

### OpenCode 런타임 (옵션)

`opencode` 백엔드를 사용하려면 다음 변수를 설정하세요. 설정되지 않은
상태에서 `backend=opencode` 세션을 디스패치하면 명확한 에러로 실패합니다.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `OPENCODE_GATEWAY_ENABLED` | `0` | `1`/`true`/`yes`이면 런타임 활성화 |
| `OPENCODE_PROVIDER_ID` | (필수) | 예: `ollama`, `model-connect`, `openai`, `anthropic` |
| `OPENCODE_MODEL_ID` | (필수) | 예: `qwen3:14b`, `Qwen3.5-397B-A17B-FP8` |
| `OPENCODE_BIN` | `opencode` | 바이너리 경로 |
| `OPENCODE_SERVER_PORT` | `14096` | 리스닝 포트 |
| `OPENCODE_SERVER_HOSTNAME` | `127.0.0.1` | 리스닝 호스트 |
| `OPENCODE_SERVER_PASSWORD` | (없음) | 설정 시 서버에 Basic auth 활성화 |
| `OPENCODE_DEFAULT_AGENT` | `build` | opencode 에이전트 이름 |
| `OPENCODE_IDLE_TIMEOUT_SECONDS` | `7200` | 권한 요청에 응답이 없을 때 중단 시한(초) |

### 로컬 Ollama를 opencode 프로바이더로

opencode는 로컬에서 동작 중인 Ollama를 자동 인식하지 않습니다. 내장
카탈로그(models.dev)에는 `ollama-cloud`(호스티드 Turbo 서비스)와
`lmstudio`만 등록되어 있으므로, 로컬 `:11434` 데몬은 프로바이더로 명시
등록해야 합니다.

바로 사용 가능한 템플릿이
[`scripts/opencode.json.example`](./scripts/opencode.json.example)에
있습니다 — `@ai-sdk/openai-compatible` 위에 `ollama` 프로바이더를 정의하고
`http://localhost:11434/v1`을 향하도록 되어 있습니다.

- `~/.config/opencode/opencode.json`이 없다면:
  ```bash
  mkdir -p ~/.config/opencode
  cp codex_gateway/scripts/opencode.json.example ~/.config/opencode/opencode.json
  # 그 다음 모델 목록을 `ollama list` 결과에 맞게 편집
  ```
- 이미 존재한다면 **덮어쓰지 마세요**. 템플릿을 열어 `provider.ollama`
  블록을 기존 `"provider"` 키 아래로 직접 병합하세요.

> ### ⚠ 직관적이지 않은 두 가지 함정
>
> **1. 모델 메타데이터를 최소만 채우면 조용히 누락됩니다.**
> 모델 항목에 `"tool_call": true` 하나만 있어도 옆에서 보면 충분해
> 보이지만, opencode는 이 값으로 채팅 컴플리션 요청에 `tools[]` 배열을
> 실어 보낼지 결정하지 **않습니다**. 카탈로그 형태의 전체 필드 셋
> ―― `id`, `name`, `family`, `attachment`, `reasoning`, `tool_call`,
> `temperature`, `release_date`, `last_updated`, `modalities`,
> `open_weights`, `cost`, `limit` ―― 이 모두 함께 있을 때 비로소
> 스위치가 켜집니다. 누락 시 에이전트는 도구를 전달받지 못하고,
> 모델은 *"I don't have a bash function"* 같은 응답으로 거절합니다.
> Ollama의 `/v1/chat/completions`를 직접 호출하면 `tool_calls`가 정상
> 동작하더라도 opencode 경로에서는 이렇게 됩니다. **새 로컬 모델을
> 추가할 때는 템플릿의 필드 셋을 그대로 복사하세요.**
>
> **2. Gemma 계열은 Ollama 자체에서 tool calling 미지원.**
> Ollama가 Gemma 모델에 대한 tool-calling chat completion을
> 거부합니다 (`Error: registry.ollama.ai/library/gemma3:4b does not
> support tools`). `gemma2:*`, `gemma3:*`, `gemma4:*` 모두 해당합니다.
> 도구 호출이 가능한 로컬 백엔드가 필요하다면 `mistral-nemo:latest`
> (권장), `qwen2.5:14b` 같은 tool 학습 instruct 모델을 사용하세요.
> 전체 매트릭스와 기본값 선정 근거는
> [docs/local-ollama-toolcall-evaluation.md](./docs/local-ollama-toolcall-evaluation.md)
> 를 참고하세요.

`bash codex_gateway/scripts/install.sh --check`를 실행하면 현재 설정
상태(설정 파일 없음 / `provider.ollama` 누락 / 모델 메타데이터 부족 /
완전 등록됨)에 따라 다음 단계를 정확히 안내해 줍니다.

등록을 마치고 게이트웨이 환경 변수에
`OPENCODE_PROVIDER_ID="ollama"`,
`OPENCODE_MODEL_ID="mistral-nemo:latest"`
(또는 위 매트릭스에서 tool-capable로 분류된 `ollama list` 모델)을
설정하면, `/session_new <label> opencode` 세션이 로컬 데몬을 경유합니다.

### 옵션: opencode에서 ChatGPT 구독 인증 사용

opencode를 codex가 사용하는 ChatGPT Plus/Pro 계정에 붙이려면(별도
OpenAI API 키 대신) 게이트웨이 호스트에 커뮤니티 플러그인을 한 번
설치해 두세요:

```bash
npx -y opencode-openai-codex-auth@latest
opencode auth login
```

플러그인: <https://github.com/numman-ali/opencode-openai-codex-auth>.
OpenAI ToS 상 개인 사용 한정입니다.

## 저장소 레이아웃

- `PROJECTS_FILE` — 등록된 프로젝트 목록과 프로젝트별 `active_session_id`.
- `STATE_ROOT` — `gateway_state.json` (선택 상태, 프로젝트별 펜딩 모델,
  최근 실행) 그리고 `projects/<id>/sessions/<id>/session.json`.
- `RUNTIME_ROOT` — 세션별 런타임 tmp + 마지막 응답 아티팩트.

codex 세션은 추가로 자신의 세션 루트 아래에 `codex-home/.codex`를
구성합니다.

## 운영 메모

- `/ask`는 프로젝트와 세션이 모두 결정 가능한 경우(채널 바인딩 또는 명시
  선택)에만 실행됩니다.
- `/last`로 받는 첨부는 `utf-8-sig`로 기록되어 안드로이드 모바일
  Discord에서도 텍스트가 깨끗하게 열립니다.
- `/watch on`은 활성 실행이 idle 상태가 될 때까지 상태 스냅샷을 반복
  게시하고, 대상 프로젝트/세션이 바뀌면 자동으로 꺼집니다.
- `/tui`는 현재 선택된 세션을 로컬 터미널에서 이어 받을 수 있는 명령을
  돌려줍니다:
  - codex 세션 → `bash codex_gateway/scripts/attach-gateway-session.sh ...`
    형태로, 게이트웨이의 런타임 `HOME`을 재사용해 TUI가 같은 세션별
    `.codex` 설정을 보도록 합니다.
  - opencode 세션 →
    `opencode attach http://<host>:<port> --session ses_...` 형태로,
    게이트웨이가 구동 중인 동일한 `opencode serve` 인스턴스에 로컬
    `opencode` TUI를 연결합니다. `OPENCODE_SERVER_PASSWORD`가 설정되어
    있으면 `--password`가 자동 추가되고, 원격 호스트일 경우 SSH 터널링
    안내도 함께 출력됩니다.
- 선택 상태와 프로젝트별 펜딩 모델 값은 영속 상태 파일을 통해 게이트웨이
  재시작 후에도 유지됩니다.
- `OPENCODE_GATEWAY_ENABLED`가 켜지면 `opencode serve`가 백그라운드로
  실행되고, 게이트웨이 종료 시 함께 정리됩니다. 설정된 포트에 이미 서버가
  떠 있다면 새로 띄우지 않고 그 서버를 재사용합니다.

## 리포지토리 구조

```
codex_gateway/
├── README.md                # 영문 문서
├── README_kor.md            # 본 문서
├── __init__.py / __main__.py
├── config.py                # env → GatewayConfig
├── state.py                 # 게이트웨이 글로벌 선택, 프로젝트별 펜딩 모델, 실행 이력
├── execution_env.py
├── runner.py                # codex 백엔드 구현 (subprocess)
├── bot.py                   # Discord 클라이언트 + 슬래시 명령 표면
├── formatter.py             # Discord 친화 요약
├── notification_router.py   # 프로젝트 채널 알림
├── permission_router.py     # opencode 권한 UX (버튼 + 슬래시 폴백)
├── tui_attach.py            # codex 세션용 `/tui` CLI
├── backend/                 # 백엔드 ABC + codex / opencode 어댑터
│   ├── codex.py
│   ├── opencode.py          # OpencodeClient + OpencodeBackend
│   ├── opencode_runtime.py  # server + client + backend 보유자
│   └── opencode_server.py   # `opencode serve` 라이프사이클
├── storage/                 # 영속 JSON 저장소
│   ├── session_store.py
│   ├── project_registry.py
│   └── last_response_store.py
├── inspectors/              # 라이브 로컬 상태 읽기 전용 인스펙터
│   ├── process_inspector.py
│   └── session_inspector.py
├── ollama_pull.py           # `/model_select`이 호출하는 온디맨드 `ollama pull` 드라이버
├── scripts/                 # 셸 진입점
│   ├── install.sh           # 환경 점검 / 자동 설치 / bin 심볼릭
│   ├── run_gateway.sh       # 메인 게이트웨이 런처
│   └── attach-gateway-session.sh   # codex 세션용 `/tui` 헬퍼
├── docs/
│   ├── DESIGN.md
│   ├── HISTORY.md
│   └── opencode_discord_remote_dev_reference.md
└── tests/                   # 162개 유닛 테스트
```
