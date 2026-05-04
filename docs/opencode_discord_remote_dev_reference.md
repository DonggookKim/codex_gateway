# Discord 원격 지시 → 로컬 개발 머신 실행 구조에서 OpenCode 적용 검토

## 목적

현재 구성은 로컬 개발 머신에서 Codex를 실행하고, Discord를 통해 원격으로 지시를 넣고 응답을 받는 구조이다.

현재 사용 중인 핵심 흐름은 다음과 같다.

```text
Discord 원격 지시
  → 로컬 개발 머신
  → codex resume --last
  → 기존 Codex 세션에 계속 명령 전달
  → Codex가 작업 수행
  → Discord로 응답 수신
```

이 문서는 같은 목적을 OpenCode를 중간 실행 도구로 사용해서 구성할 수 있는지 검토한 내용이다.

---

## 현재 Codex 기반 방식

현재 Codex 설정의 주요 특징은 다음과 같다.

```toml
model = "gpt-5.4"
approval_policy = "never"
sandbox_mode = "workspace-write"

[mcp_servers.discord]
command = "uvx"
args = ["discord-mcp-agent"]
env_vars = ["DISCORD_TOKEN"]
startup_timeout_sec = 30
tool_timeout_sec = 7500
```

Discord MCP 서버는 다음 환경값을 사용한다.

```toml
[mcp_servers.discord.env]
DISCORD_GUILD_ID = "1494463231647813736"
DISCORD_CHANNEL = "일반"
DISCORD_ASK_TIMEOUT = "7200"
```

Discord 관련 MCP tool 권한은 다음과 같이 설정되어 있다.

```toml
[mcp_servers.discord.tools.discord_ask]
approval_mode = "approve"

[mcp_servers.discord.tools.discord_notify]
approval_mode = "approve"

[mcp_servers.discord.tools.discord_send_file]
approval_mode = "approve"

[mcp_servers.discord.tools.discord_embed]
approval_mode = "approve"

[mcp_servers.discord.tools.discord_screenshot]
approval_mode = "auto"
```

현재 운영상 중요한 점은 `codex resume --last`를 통해 기존 세션에 계속 이어서 명령을 넣는다는 것이다.

---

## OpenCode에서도 같은 구조가 가능한가?

가능하다.

다만 OpenCode는 Codex의 `codex resume --last`와 완전히 같은 방식보다는 다음 기능들을 조합하는 형태가 더 적합하다.

- `opencode run --continue`
- `opencode run --session <session_id>`
- `opencode serve`
- `opencode run --attach http://127.0.0.1:<port>`
- `opencode session list`

즉 단순히 마지막 세션에 붙는 방식은 가능하지만, 장기 운영에서는 `--continue`보다 `--session <session_id>`를 명시적으로 사용하는 방식이 더 안정적이다.

---

## OpenCode에서 Codex resume --last에 해당하는 방식

### 1. 마지막 세션 계속 사용

Codex의 다음 명령과 비슷한 방식이다.

```bash
codex resume --last
```

OpenCode에서는 다음처럼 실행할 수 있다.

```bash
opencode run --continue "Discord에서 받은 지시 내용"
```

또는 축약해서:

```bash
opencode run -c "Discord에서 받은 지시 내용"
```

장점:

- 구현이 단순하다.
- 현재 Codex 방식과 개념적으로 가장 비슷하다.

단점:

- 여러 프로젝트나 여러 OpenCode 세션을 동시에 쓰면 마지막 세션이 꼬일 수 있다.
- Discord gateway가 의도하지 않은 세션에 명령을 넣을 위험이 있다.

따라서 테스트용으로는 괜찮지만, 장기 운영용으로는 추천하지 않는다.

---

### 2. 특정 세션 ID에 명령 전달

운영용으로는 이 방식이 가장 적합하다.

```bash
opencode run --session "$SESSION_ID" "Discord에서 받은 지시 내용"
```

세션 목록은 다음으로 확인한다.

```bash
opencode session list
```

JSON으로 받으려면 다음을 사용한다.

```bash
opencode session list --format json
```

프로젝트마다 세션 ID를 저장해두면 안정적으로 동일한 OpenCode 세션에 계속 명령을 전달할 수 있다.

예시:

```text
/home/boor123/work/codex_sandbox/.opencode-session
```

이 파일에 해당 프로젝트에서 사용할 OpenCode session id를 저장한다.

---

### 3. OpenCode server에 attach해서 사용

OpenCode는 headless server 방식으로 실행할 수 있다.

```bash
opencode serve --port 4096 --hostname 127.0.0.1
```

이후 다른 프로세스에서 다음처럼 붙을 수 있다.

```bash
opencode run   --attach http://127.0.0.1:4096   --session "$SESSION_ID"   "Discord에서 받은 지시 내용"
```

이 방식의 장점:

- OpenCode 서버를 상시 실행할 수 있다.
- MCP 서버 cold boot 비용을 줄일 수 있다.
- Discord gateway와 OpenCode 실행부를 분리하기 쉽다.
- 장기적으로 여러 프로젝트/세션을 라우팅하기 좋다.

---

## 권장 아키텍처

가장 안정적인 구조는 다음과 같다.

```text
Discord
  ↓
Discord Gateway Bot / Listener
  ↓
Project Router
  ↓
opencode run --attach http://127.0.0.1:4096 --session <project-session-id>
  ↓
OpenCode
  ↓
Local filesystem / git / test / build
  ↓
stdout 또는 JSON event stream
  ↓
Gateway가 Discord 메시지로 변환하여 응답
```

이 구조에서는 Discord 입출력은 gateway bot이 담당하고, OpenCode는 로컬 개발 작업을 수행하는 agent 역할만 맡는다.

---

## OpenCode 내부에 Discord MCP를 넣는 방식과의 비교

두 가지 구성이 가능하다.

### A안: Discord Gateway가 입출력을 담당

```text
Discord Bot
  → OpenCode CLI / OpenCode Server
  → 작업 결과를 Bot이 Discord로 전송
```

장점:

- 역할이 명확하다.
- Discord 인증, 채널, 메시지 분할, 파일 전송 등을 gateway에서 통제할 수 있다.
- OpenCode는 개발 agent 역할에 집중한다.
- 여러 agent(Codex, OpenCode, Claude Code)를 gateway에서 라우팅하기 쉽다.

단점:

- gateway bot 구현이 필요하다.

### B안: OpenCode 내부에 Discord MCP를 붙임

```text
OpenCode
  → Discord MCP
  → Discord read/write
```

장점:

- agent가 Discord tool을 직접 사용할 수 있다.
- Codex에서 쓰던 MCP 구조와 유사하다.

단점:

- Discord gateway 역할과 OpenCode agent 역할이 섞인다.
- 장기 운영 시 메시지 라우팅/세션 관리가 복잡해질 수 있다.
- 외부 Discord bot과 OpenCode 내부 Discord MCP가 중복될 수 있다.

현재 목표가 “Discord 원격 지시 → 로컬 개발 머신 실행”이라면 A안이 더 안정적이다.

---

## OpenCode 권한 설정 검토

OpenCode는 Codex의 `approval_policy = "never"`와 같은 설정을 그대로 쓰지는 않는다.

대신 `permission` 설정에서 각 도구에 대해 다음 값을 지정한다.

- `allow`: 승인 없이 실행
- `ask`: 실행 전 확인
- `deny`: 차단

Codex의 권한 개념과 대략 대응하면 다음과 같다.

| Codex 설정 | OpenCode 대응 |
|---|---|
| `approval_policy = "never"` | 대부분의 permission을 `"allow"` |
| `approval_mode = "approve"` | `"ask"` |
| `approval_mode = "auto"` | `"allow"` |
| writable roots | `external_directory` |

원격 실행 구조에서는 `bash: allow`가 가장 위험하다.

따라서 초기에는 다음 구성을 추천한다.

```jsonc
{
  "permission": {
    "read": "allow",
    "edit": "allow",
    "grep": "allow",
    "glob": "allow",
    "lsp": "allow",
    "webfetch": "allow",
    "websearch": "allow",

    "bash": "ask",

    "external_directory": {
      "/home/boor123/work/codex_sandbox/**": "allow",
      "/tmp/**": "allow"
    }
  }
}
```

Codex의 `approval_policy = "never"`에 더 가깝게 하려면 다음처럼 할 수 있다.

```jsonc
{
  "permission": {
    "bash": "allow",
    "read": "allow",
    "edit": "allow",
    "grep": "allow",
    "glob": "allow",
    "lsp": "allow",
    "webfetch": "allow",
    "websearch": "allow"
  }
}
```

하지만 Discord 원격 실행에서는 실수 명령도 로컬 머신에서 바로 실행될 수 있으므로 주의가 필요하다.

---

## Claude Code Auto Mode와의 비교

OpenCode도 자동 실행에 가까운 구성을 만들 수 있다.

하지만 Claude Code의 Auto Mode처럼 “위험도에 따라 자동으로 승인 여부를 판단하는 공식 기능”과 완전히 같다고 보기는 어렵다.

OpenCode에서는 현재 다음처럼 수동 정책 기반으로 구성하는 것이 현실적이다.

```jsonc
{
  "permission": {
    "read": "allow",
    "edit": "allow",
    "grep": "allow",
    "glob": "allow",
    "lsp": "allow",
    "webfetch": "allow",
    "websearch": "allow",
    "bash": "ask"
  }
}
```

완전 자동에 가깝게 하려면:

```jsonc
{
  "permission": {
    "bash": "allow"
  }
}
```

또는 실행 시 다음 옵션을 사용할 수 있다.

```bash
opencode run --dangerously-skip-permissions "..."
```

이 옵션은 명시적으로 deny되지 않은 권한을 자동 승인하는 방식에 가깝다.

다만 원격 Discord 기반 실행에서는 매우 강한 권한이므로, 테스트 환경이나 격리된 workspace에서만 사용하는 것이 좋다.

---

## OpenCode 설정 예시

아래는 현재 Codex 설정을 OpenCode용으로 옮긴 예시이다.

파일 위치 예시:

```bash
~/.config/opencode/opencode.jsonc
```

```jsonc
{
  "$schema": "https://opencode.ai/config.json",

  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama Local",
      "options": {
        "baseURL": "http://127.0.0.1:11434/v1"
      },
      "models": {
        "qwen3:8b": {
          "name": "Qwen3 8B"
        }
      }
    }
  },

  "mcp": {
    "discord": {
      "type": "local",
      "command": ["uvx", "discord-mcp-agent"],
      "enabled": true,
      "timeout": 7500000,
      "environment": {
        "DISCORD_TOKEN": "{env:DISCORD_TOKEN}",
        "DISCORD_GUILD_ID": "1494463231647813736",
        "DISCORD_CHANNEL": "일반",
        "DISCORD_ASK_TIMEOUT": "7200"
      }
    }
  },

  "permission": {
    "bash": "ask",
    "read": "allow",
    "edit": "allow",
    "grep": "allow",
    "glob": "allow",
    "lsp": "allow",
    "task": "allow",
    "skill": "allow",
    "webfetch": "allow",
    "websearch": "allow",
    "question": "allow",

    "external_directory": {
      "/home/boor123/**": "allow",
      "/tmp/**": "allow",
      "/mnt/d/Study_Things/codex_sandbox/**": "allow",
      "/mnt/d/study_things/codex_sandbox/**": "allow"
    },

    "discord*ask": "ask",
    "discord*notify": "ask",
    "discord*send_file": "ask",
    "discord*embed": "ask",
    "discord*screenshot": "allow"
  }
}
```

단, 권장 아키텍처에서는 Discord MCP를 OpenCode 내부에 넣지 않고 gateway bot이 Discord 입출력을 담당하는 편이 더 안정적이다.

그 경우 OpenCode 설정에서 `mcp.discord`는 제거해도 된다.

---

## 권장 실행 방식

### 1. OpenCode 서버 실행

```bash
cd /home/boor123/work/codex_sandbox

opencode serve --port 4096 --hostname 127.0.0.1
```

필요 시 서버 비밀번호를 설정한다.

```bash
OPENCODE_SERVER_PASSWORD='강한비밀번호' opencode serve --port 4096 --hostname 127.0.0.1
```

---

### 2. 세션 생성 또는 확인

```bash
opencode session list --format json
```

프로젝트별로 사용할 session id를 파일에 저장한다.

```bash
echo "<session_id>" > /home/boor123/work/codex_sandbox/.opencode-session
```

---

### 3. Discord gateway에서 명령 전달

```bash
PROJECT_DIR="/home/boor123/work/codex_sandbox"
SESSION_ID="$(cat "$PROJECT_DIR/.opencode-session")"

cd "$PROJECT_DIR"

opencode run   --attach http://127.0.0.1:4096   --session "$SESSION_ID"   --format json   "현재 git diff를 확인하고, 실패하는 테스트를 고쳐줘."
```

---

## Gateway wrapper 예시

Discord bot에서 직접 호출하기 위한 wrapper 예시이다.

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/boor123/work/codex_sandbox}"
SESSION_FILE="$PROJECT_DIR/.opencode-session"
OPENCODE_SERVER_URL="${OPENCODE_SERVER_URL:-http://127.0.0.1:4096}"

PROMPT="${1:?prompt required}"

cd "$PROJECT_DIR"

if [[ ! -f "$SESSION_FILE" ]]; then
  echo "No OpenCode session file found: $SESSION_FILE" >&2
  echo "Create an OpenCode session first and write the session id into this file." >&2
  exit 1
fi

SESSION_ID="$(cat "$SESSION_FILE")"

opencode run   --attach "$OPENCODE_SERVER_URL"   --session "$SESSION_ID"   --format json   "$PROMPT"
```

파일명 예시:

```bash
~/bin/opencode-discord-run
```

권한 부여:

```bash
chmod +x ~/bin/opencode-discord-run
```

실행 예시:

```bash
~/bin/opencode-discord-run "현재 브랜치 상태를 확인하고 다음 작업을 제안해줘."
```

---

## 세션 라우팅 전략

여러 프로젝트를 다룰 경우, 프로젝트별로 session id를 분리하는 것이 좋다.

예시:

```text
/home/boor123/work/codex_sandbox/.opencode-session
/mnt/d/study_things/codex_sandbox/codex_gateway/.opencode-session
/mnt/d/Study_Things/android_mail_arranger/.opencode-session
```

Discord에서 다음처럼 프로젝트 prefix를 붙여 라우팅할 수 있다.

```text
!oc codex_gateway 현재 테스트 실패 원인 확인해줘
!oc android_mail_arranger git diff 리뷰해줘
!oc mkm yolo 학습 스크립트 상태 확인해줘
```

Gateway는 prefix에 따라 project directory와 session id를 결정한다.

---

## OpenCode 사용 시 주의점

### 1. `--continue`는 운영용으로 불안할 수 있음

`opencode run --continue`는 마지막 세션에 붙기 때문에 여러 세션을 동시에 쓰면 의도하지 않은 세션에 명령이 들어갈 수 있다.

운영용으로는 `--session <session_id>`를 추천한다.

### 2. `bash: allow`는 강력하지만 위험함

Discord 원격 실행 구조에서 `bash`를 allow하면 다음 작업이 승인 없이 실행될 수 있다.

- 파일 삭제
- git push
- credential 출력
- 외부 네트워크 호출
- 시스템 설정 변경

초기에는 `bash: ask`를 추천한다.

### 3. workspace 범위를 좁히는 것이 좋음

`external_directory`를 너무 넓게 열면 실수로 홈 디렉토리 전체나 민감 파일을 수정할 수 있다.

가능하면 프로젝트 디렉토리 단위로 제한하는 것이 좋다.

### 4. OpenCode 내부 Discord MCP와 외부 Gateway Bot을 동시에 쓰면 역할이 중복될 수 있음

원격 명령 수신과 응답 전송은 gateway가 담당하고, OpenCode는 로컬 개발 agent로 사용하는 구성이 더 깔끔하다.

---

## 최종 추천

현재 목표에는 다음 구조를 추천한다.

```text
Discord Gateway Bot
  → 프로젝트 라우팅
  → opencode run --attach --session
  → OpenCode가 로컬 작업 수행
  → JSON/stdout 결과를 gateway가 Discord로 전송
```

초기 테스트는 다음 순서로 진행한다.

1. OpenCode를 설치하고 `opencode run` 단독 실행 확인
2. `opencode session list --format json`로 세션 ID 확인
3. `.opencode-session` 파일에 session id 저장
4. `opencode run --session <id>`로 기존 세션에 이어서 명령이 들어가는지 확인
5. `opencode serve` 실행
6. `opencode run --attach http://127.0.0.1:4096 --session <id>` 테스트
7. Discord gateway에서 wrapper script 호출
8. stdout/json 응답을 Discord 메시지로 변환

Codex의 `codex resume --last`를 그대로 대체하고 싶다면 `opencode run --continue`를 쓸 수 있다.

하지만 실제 운영에서는 다음이 더 안전하다.

```bash
opencode run   --attach http://127.0.0.1:4096   --session "$SESSION_ID"   --format json   "$PROMPT"
```

이 방식이 Codex의 `resume --last`보다 세션 제어가 명확하고, Discord 원격 개발 프로세스에도 더 적합하다.
