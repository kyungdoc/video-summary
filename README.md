# Video Summary

오즈모·액션캠으로 촬영한 여행 영상을 날짜별로 정리해, 여정과 재미있는 순간이 함께 보이는 YouTube용 요약 영상을 만드는 로컬 우선 CLI입니다.

기본 결과는 여행 날짜별 MP4입니다. 인트로, 날짜 카드, 위치 lower-third, 아웃트로와 함께 `.vtt` 자막, YouTube 설명란에 붙일 `.chapters.txt`, 설명문 초안도 생성합니다. 업로드 자체는 자동으로 수행하지 않습니다.

## 설계 원칙

- 한 번에 한 클립, 한 렌더 세그먼트만 처리합니다.
- 모든 단계는 `.video-summary/`에 원자적으로 캐시되며 같은 명령으로 재개할 수 있습니다.
- 4K 전체 프록시를 만들지 않고, 전사와 저해상도 프레임 신호로 후보 구간만 찾습니다.
- 기본 출력은 1080p/30fps이며 Apple Silicon에서는 `h264_videotoolbox`를 우선합니다.
- 외부 모델 없이 `local` 플래너로 완주할 수 있고, 의미 기반 선별 품질이 필요할 때만 Codex/Claude를 선택합니다.

## 요구사항

- macOS 또는 Linux
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- FFmpeg와 ffprobe

```bash
uv sync --frozen
uv run --frozen video-summary doctor
```

현재 렌더 머신(M3, 16GB)에서는 한 작업만 순차 실행하고 1080p로 렌더하는 기본값을 권장합니다.

## 한 번에 실행

```bash
uv run --frozen video-summary run \
  --project "okinawa-2026" \
  --workspace "$PWD" \
  --source-dir "/Volumes/OSMO/Okinawa" \
  --prompt "날짜별 4~6분으로, 이동 흐름과 음식·대화·재미있는 반응을 균형 있게 살려줘." \
  --planner local
```

`--source-dir`는 읽기 전용 원본 경로이고, `--workspace`는 캐시와 최종 결과의 소유 경로입니다. 외장 드라이브의 원본을 복사하거나 수정하지 않습니다. 다만 manifest는 절대 원본 경로를 기억하므로 렌더가 끝날 때까지 드라이브 경로를 유지해야 합니다.

더 나은 의미 기반 선별을 원하면 다음처럼 실행합니다.

```bash
uv run --frozen video-summary run \
  --project "okinawa-2026" \
  --workspace "$PWD" \
  --source-dir "/Volumes/OSMO/Okinawa" \
  --prompt-file "./editing-prompt.md" \
  --planner codex \
  --strict-planner
```

축소 contact sheet까지 외부 플래너에 제공하려는 경우에만 `--planner-images`를 추가합니다. `--strict-planner`가 없으면 외부 플래너의 일시적 실패 시 로컬 플래너로 해당 실행을 완주하며, 다음 실행에서는 외부 플래너를 다시 시도합니다.

## 단계별 실행

```bash
uv run --frozen video-summary scan \
  --project "okinawa-2026" --workspace "$PWD" \
  --source-dir "/Volumes/OSMO/Okinawa"

# .video-summary/okinawa-2026/project.yaml 검토/수정

uv run --frozen video-summary analyze \
  --project "okinawa-2026" --workspace "$PWD"

uv run --frozen video-summary plan \
  --project "okinawa-2026" --workspace "$PWD" \
  --prompt-file "./editing-prompt.md" --planner codex

uv run --frozen video-summary render \
  --project "okinawa-2026" --workspace "$PWD" --draft

uv run --frozen video-summary status \
  --project "okinawa-2026" --workspace "$PWD"

uv run --frozen video-summary render \
  --project "okinawa-2026" --workspace "$PWD"
```

`--draft`는 720p 미리보기입니다. 최종 4K가 필요하면 `uv run --frozen video-summary render --project "okinawa-2026" --workspace "$PWD" --resolution 2160p`로 렌더합니다. `--force`는 해당 명령의 캐시를 다시 계산하므로 필요한 단계에만 사용하세요.

## 시간순, 콜드 오픈과 카드

날짜별로 선택된 source 구간은 첫 장면부터 마지막 장면까지 실제 촬영 시간순을 지킵니다. `cold_open: true`는 선택된 source 중 가장 이른 장면만 `hook`으로 강조할 수 있다는 뜻이며, 뒷시점의 장면을 앞으로 이동시키지 않습니다. `hook`도 시각 카드 뒤에 나오는 첫 source입니다.

로컬 플래너는 각 DAY의 가장 이른 장면을 여정 시작점으로 보존하고, 여유가 있으면 마지막 장면도 남깁니다. 대사가 적거나 없어도 `scenery`로 분류되거나 밝기·대비 등 화면 품질이 좋은 안정적인 장면은 날짜별 시각 앵커로 우선 검토하므로, 풍경과 야외 장면이 전사 점수 때문에 빠지지 않습니다.

`episode_mode: trip`의 MP4 시각 순서는 다음과 같습니다.

1. 여행 전체 인트로 모자이크(대표 프레임이 없으면 제목 카드)
2. 각 DAY의 날짜 카드
3. 해당 DAY의 모든 source 구간(`hook` 포함, 실제 촬영 시간순)
4. 다음 DAY의 날짜 카드와 source 구간 반복
5. 여행 전체 아웃트로 카드

카드를 조금 더 여유 있게 읽히는 권장값은 날짜 3초, 아웃트로 5초입니다. 여러 날을 합친 동적 모자이크는 타일이 채워지고 제목이 나타날 호흡을 위해 인트로 6초를 권장합니다.

```yaml
editing:
  cold_open: true
  episode_mode: trip

render:
  trip_intro_style: mosaic
  trip_intro_candidate_ids: []  # 비우면 DAY 커버리지 우선으로 자동 선택
  trip_intro_grid_size: 7       # 6, 7, 8 중 선택
  trip_intro_animation: flow    # flow 또는 static
  intro_seconds: 6.0            # 여러 날 여행 flow 권장값
  date_card_seconds: 3.0
  outro_seconds: 5.0
  transition_seconds: 0.18
```

`trip_intro_style: mosaic`는 최종 edit plan에 실제로 선택되고 JPEG를 정상적으로 읽을 수 있는 source만 사용합니다. 기본 `trip_intro_grid_size: 7`은 최대 49장의 7×7 인트로를 만들며, `6`은 최대 36장의 더 큰 타일, `8`은 최대 64장의 더 촘촘한 전체 조망을 제공합니다. 먼저 usable frame이 있는 각 DAY를 한 장씩 커버하고, DAY가 현재 grid 용량을 넘으면 첫날과 마지막 날을 포함해 전체 여정에서 용량만큼 DAY를 균등하게 고릅니다. 남은 칸은 검토한 후보와 DAY 균형을 따르면서 서로 다른 원본 clip을 한 장씩 우선 사용하고, 고유 원본이 부족할 때만 같은 clip의 추가 프레임을 사용합니다. 기본 후보 순서는 visual quality, 후보 점수, 촬영 시각 순입니다. 선택된 프레임이 grid 용량보다 적으면 남은 셀은 어두운 배경으로 유지합니다. 누락되거나 손상된 프레임은 같은 DAY의 다음 후보로 대체합니다.

`trip_intro_animation: flow`는 선택된 프레임을 최종 edit plan의 시간순으로 정렬한 뒤, 첫째 줄은 왼쪽에서 오른쪽으로, 다음 줄은 오른쪽에서 왼쪽으로 번갈아 가는 serpentine 흐름으로 바둑판을 채웁니다. 각 타일은 짧은 수평 flip/slide로 자리를 잡고, 중앙 제목과 기간은 모자이크가 충분히 드러난 후반에 반투명한 어두운 패널과 함께 나타납니다. `static`은 같은 선택 프레임으로 완성된 모자이크를 처음부터 보여주는 저동작 옵션입니다. `flow` 생성이 실패하면 static 모자이크로, usable frame이 없거나 static 생성도 실패하면 기존 제목 카드로 안전하게 fallback합니다.

특정 컷을 쓰려면 `trip_intro_candidate_ids`에 이미 plan-selected된 candidate ID를 전역 선택 우선순위로 적습니다. 같은 DAY의 ID를 여러 개 지정할 수 있지만 DAY 커버리지를 먼저 확보하며, 이 목록은 타일 위치를 정하지 않습니다. 오래됐거나 최종 plan에 선택되지 않은 ID는 무시합니다. 실제 타일은 설정 목록의 순서와 무관하게 최종 edit plan의 시간순으로 배치됩니다. `trip_intro_grid_size`는 `6`, `7`, `8`만 허용하며, `trip_intro_style: card`로 모자이크를 끌 수도 있습니다.

이 모자이크는 `.video-summary/<project>/frames/`의 로컬 분석 JPEG를 한 번에 한 장씩 처리하는 저메모리 render-only 작업이며, 원본 고해상도 영상을 한꺼번에 메모리에 올리지 않습니다. 모자이크 style·grid 크기·animation·선택 ID·대표 JPEG·제목 패널·intro 길이만 바뀌면 전사·분석·source segment 캐시는 재사용되므로 `--force`는 필요하지 않습니다. 반면 해상도·fps·encoder·bitrate 같은 출력 형식, font/audio 설정 또는 정확 프레임 정책이 바뀌면 source segment를 다시 렌더합니다. `daily` 인트로는 계속 기존 제목 카드이고, `trip`에서만 모자이크 인트로·아웃트로가 한 번씩, 날짜 카드는 DAY마다 적용됩니다. 세 카드 시간 값은 0.1~30초 사이의 유한한 숫자여야 합니다.

같은 원본에서 겹치거나 맞닿은 후보 창은 후보 생성 때 하나의 연속 구간으로 합치고, planner 검증에서도 같은 원본의 실시간 구간이 1ms를 넘게 겹치는 선택을 거부합니다. 렌더러는 각 source 길이를 목표 fps의 가장 가까운 정수 프레임으로 맞춘 뒤 fps·PTS를 정규화하고 부족한 끝 프레임을 보충한 다음 정확한 프레임 수로 자릅니다. 완성된 캐시도 fps·프레임 수·영상/음성 길이를 다시 확인하므로, concat에서 마지막 프레임이 붙잡혀 반복처럼 보이는 현상을 막습니다.

`transition_seconds`의 기본값은 `0.18`초이고 허용 범위는 0~1초입니다. 각 DAY의 날짜 카드 뒤 첫 source는 블랙 화면·무음에서 짧게 들어오고, 마지막 source는 다음 날짜 카드나 아웃트로를 향해 블랙·무음으로 빠집니다. DAY 내부의 source끼리는 원본 호흡을 보존하는 clean cut이며 실제 crossfade는 하지 않습니다. 값을 `0`으로 두면 이 경계 페이드도 끕니다.

## 로컬 전사

`analysis.asr_backend: auto`의 선택 순서는 다음과 같습니다.

1. `whisper-cli`와 `WHISPER_CPP_MODEL` 또는 프로젝트 모델 파일이 있으면 whisper.cpp
2. 그 외에는 포함된 faster-whisper `small`, CPU int8, worker 1

`auto`에서 whisper.cpp 모델 로드·실행·JSON 검증이 실패하면 해당 실행에 한해 faster-whisper로 한 번 재시도합니다. `--asr-backend whisper.cpp`를 명시하면 자동 fallback 없이 실패합니다.

16GB Apple Silicon 권장 조합은 `whisper.cpp`의 `large-v3-turbo-q5_0` 모델과 Silero VAD입니다.

```bash
uv run --frozen video-summary run ... \
  --asr-backend whisper.cpp \
  --whisper-cpp-model "/absolute/models/ggml-large-v3-turbo-q5_0.bin" \
  --whisper-cpp-vad-model "/absolute/models/ggml-silero-v6.2.0.bin"
```

설치한 모델과 VAD까지 사전 점검하려면 다음처럼 실행하고 출력의 `ready`와 `asr.whisper_cpp_ready`를 확인하세요.

```bash
uv run --frozen video-summary doctor \
  --whisper-cpp-model "/absolute/models/ggml-large-v3-turbo-q5_0.bin" \
  --whisper-cpp-vad-model "/absolute/models/ggml-silero-v6.2.0.bin"
```

faster-whisper는 최초 실행에 모델 다운로드가 필요할 수 있지만 영상이나 음성을 업로드하지 않습니다. 클립별 16kHz mono 임시 WAV는 전사 직후 삭제됩니다. 같은 이름의 `.srt`/`.vtt`가 자연어 자막이면 우선 재사용하고, Osmo 텔레메트리 SRT는 자동으로 제외합니다.

## 위치와 날짜 보정

첫 scan 뒤 생성된 `project.yaml`에서 규칙을 추가할 수 있습니다.

```yaml
locations:
  - label: 인천국제공항
    match: ["*DJI_0001*", "airport/*"]
    keywords: ["공항", "탑승"]
  - label: 오키나와 · 나하
    day_key: "2026-08-20"
    match: ["*"]

date_overrides:
  # 카메라 날짜만 초기화된 경우: 폴더 날짜를 쓰고 메타데이터 시각은 유지
  - match: "0517-kr/*"
    date: "2026-05-17"
    timezone: "Asia/Seoul"
  - match: "0517-us/*"
    date: "2026-05-17"
    timezone: "America/Los_Angeles"
  # 한 파일의 날짜·시각을 모두 지정해야 하는 경우
  - match: "broken-clock/*.MP4"
    captured_at: "2026-08-20T09:30:00+09:00"
```

`match`는 `--source-dir` 기준 상대 경로이며 먼저 일치한 규칙을 사용합니다. `date`는 폴더가 알려주는 현지 날짜를 고정하고 QuickTime 메타데이터 → 지원 파일명 → 파일 수정시각 순으로 시·분·초를 가져옵니다. offset이 있는 QuickTime 시각은 지정한 `timezone`과 실제 날짜의 DST를 반영해 다시 계산합니다. `timezone`을 생략하면 `project.timezone`을 사용합니다. 날짜와 시각이 모두 틀렸다면 파일별로 offset이 포함된 `captured_at`을 지정하세요. 한 규칙에 `date`와 `captured_at`을 함께 쓸 수는 없습니다.

폴더 날짜 자체를 영상 회차로 쓸 때는 `day_start_hour: 0`을 권장합니다. 기본값 `4`에서는 보정 후에도 새벽 4시 이전 영상이 전날 여행일에 포함됩니다. 규칙이 없는 영상은 기존처럼 QuickTime 메타데이터 → 지원 파일명 → 파일 수정시각 순으로 촬영시각을 결정합니다.

## 외부 플래너와 개인정보

| 설정 | 외부 전송/접근 |
|---|---|
| `--planner local` | 없음 |
| faster-whisper 최초 실행 | 모델 파일 다운로드만 수행 |
| `--planner codex` / `claude` | 편집 프롬프트, 후보별 최대 240자 전사 발췌, 시간·역할·위치 메타데이터 |
| `--planner-images` | 위 항목과 축소 contact sheet |

외부 요청은 실행별 격리 폴더에서 동작하며, `--planner-images`가 없는 요청 폴더에는 contact sheet를 만들지 않습니다. 모자이크 모드를 켠 것만으로 candidate JPEG가 전송되지는 않습니다. 다만 `--planner-images`를 명시하면 candidate frame에서 만든 축소 contact sheet의 시각 내용이 외부 플래너에 전달됩니다. 파이프라인은 원본 MP4를 플래너에 직접 첨부하지 않습니다. 외부 CLI 프로세스 자체의 파일 접근 범위는 각 도구의 sandbox에 따르므로, 처리와 보관 정책은 사용 중인 Codex/Claude 계정 정책을 따릅니다. 민감한 여행에는 `local` 플래너를 사용하세요.

외부 플래너가 생성한 JSON은 그대로 실행되지 않습니다. 후보 ID, 날짜, 시간 범위, 순서, 중복, 길이, 허용 필드뿐 아니라 각 DAY의 가장 이른 후보가 첫 장면인지 검증한 뒤 프로그램이 안전한 FFmpeg 인자를 다시 생성합니다.

## 출력

```text
WORKSPACE/
├── .video-summary/okinawa-2026/
│   ├── project.yaml
│   ├── manifest.json
│   ├── state.sqlite3
│   ├── transcripts/
│   ├── signals/
│   ├── frames/
│   ├── candidates.json
│   ├── planner/
│   ├── edit-plan.json
│   ├── render/
│   └── render-report.json
└── exports/okinawa-2026/
    ├── 2026-08-19-day-01.mp4
    ├── 2026-08-19-day-01.vtt
    ├── 2026-08-19-day-01.chapters.txt
    ├── 2026-08-19-day-01.timeline.txt
    └── 2026-08-19-day-01.description.md
```

기본 `episode_mode: daily`는 날짜별 파일을 만들고, `trip`은 `trip-summary.mp4` 하나와 같은 이름의 `.vtt`, `.chapters.txt`, `.timeline.txt`, `.description.md`를 만듭니다. `render.music_file`에 합법적으로 사용할 수 있는 음악을 지정하면 대화 구간에서 자동 ducking합니다. YouTube 저작권 확인 책임은 사용자에게 있습니다.

MP4의 인트로·날짜·아웃트로 카드는 영상 프레임에 직접 렌더되는 시각 요소입니다. 반면 `.chapters.txt`는 YouTube 설명란에 복사할 timestamp 텍스트이며 MP4에 embedded chapter metadata로 넣지 않습니다. 파일이 자동으로 YouTube에 업로드되지도 않습니다.

`trip-summary.chapters.txt`는 source 조각이나 내부 `role`마다가 아니라 DAY마다 하나의 챕터를 만듭니다. DAY 1은 여행 인트로를 포함하도록 `00:00`에서 시작하고, DAY 2부터는 해당 날짜 카드의 시작 시각을 사용합니다. timestamp는 오름차순이고 영상 길이 범위 안인지 검증합니다. YouTube 챕터로 사용하려면 첫 항목이 `00:00`이고, timestamp가 최소 3개이며, 각 챕터가 10초 이상인지 업로드 전에 확인하세요.

`.timeline.txt`에는 카드와 모든 source 후보의 상세 누적 시각을 따로 기록합니다. 편집 검증용이며 YouTube 설명란에는 `.chapters.txt`만 사용하세요.

CLI stdout은 기본적으로 clip/candidate 수와 출력 경로만 요약합니다. 디버깅을 위해 전체 내부 payload가 필요하면 하위 명령 앞에 `--full-json`을 둡니다(예: `video-summary --full-json scan ...`).

## 현재 범위

- 의미 기반 전체 영상 비전 모델이나 온라인 GPS 역지오코딩은 포함하지 않습니다.
- 위치는 파일/날짜/전사 키워드 규칙 또는 외부 플래너의 label을 사용합니다.
- D-Log M/HLG 자동 색보정은 추측하지 않습니다. 필요한 LUT/color pipeline은 후속 기능입니다.
