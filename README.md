# Video Summary

오즈모·액션캠·iPhone으로 촬영한 여행 영상을 날짜별로 정리해, 여정과 재미있는 순간이 함께 보이는 YouTube용 요약 영상을 만드는 로컬 우선 CLI입니다.

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
  --prompt "날짜별 4~6분을 선호하되, 사건의 완결성과 이동 흐름을 우선하고 약한 장면으로 길이를 채우지 마." \
  --planner local
```

`--source-dir`는 읽기 전용 원본 경로이고, `--workspace`는 캐시와 최종 결과의 소유 경로입니다. 외장 드라이브의 원본을 복사하거나 수정하지 않습니다. 다만 manifest는 절대 원본 경로를 기억하므로 렌더가 끝날 때까지 드라이브 경로를 유지해야 합니다.

첫 인트로의 큰 제목은 내부 `--project` ID와 분리된 여행지 이름입니다. 기본값은 원본 폴더명에서 선행 날짜와 `trip/raw/videos` 같은 일반 토큰을 걷어내고, 더 구체적인 지명이 함께 있으면 국가 접두어도 제외해 추론합니다. 국가명만 있는 여행은 그 국가명을 유지하며, `DCIM`·`100MEDIA`처럼 목적지를 알 수 없는 카메라 폴더에서는 프로젝트명으로 돌아갑니다. 예를 들어 `2512_vietnam-phuquoc`, `2605_japan-okinawa`, `2605_usa-SF`는 각각 `Phu Quoc`, `Okinawa`, `San Francisco`가 됩니다. 원하는 표기가 있으면 `--destination "푸꾸옥"` 또는 `project.yaml`의 `project.destination`으로 명시할 수 있으며, `render --destination ...`만 다시 실행해도 source segment 캐시는 재사용됩니다.

여행기간은 폴더명의 숫자로 추측하지 않고 scan이 시간대·`day_start_hour`·`date_overrides`를 적용해 확정한 모든 `day_key`의 처음과 끝으로 계산합니다. trip 인트로에는 전체 범위, daily 인트로에는 해당 DAY 날짜가 표시됩니다. 날짜가 틀렸다면 표시 문자열을 덮어쓰는 대신 촬영일 설정을 바로잡고 scan부터 다시 실행하세요. 최종 추론값과 근거는 `render-report.json`의 `intro_metadata`에서 확인할 수 있습니다.

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

이동 거점이나 식사 사건 탐지 정책의 버전이 바뀌면 다음 `analyze`에서 후보 캐시를 자동으로 재생성하고, 후속 `plan`도 새 후보를 기준으로 다시 검증합니다. 원본과 ASR 설정이 같은 한 기존 클립별 전사·시각 신호는 재사용하므로 이 이유만으로 `--force`를 쓸 필요는 없습니다. 선택이 바뀐 경우에만 assembly를 다시 만들며, 렌더 형식과 실시간 구간이 같은 기존 source segment는 그대로 재사용하고 새로 선택된 구간만 추가로 렌더합니다. 이전 실행에서 전사를 건너뛰었거나 원본·ASR 설정이 바뀐 경우에는 해당 클립의 전사부터 다시 필요합니다.

## 시간순, 콜드 오픈과 카드

날짜별로 선택된 source 구간은 첫 장면부터 마지막 장면까지 실제 촬영 시간순을 지킵니다. `cold_open: true`는 선택된 source 중 가장 이른 장면만 `hook`으로 강조할 수 있다는 뜻이며, 뒷시점의 장면을 앞으로 이동시키지 않습니다. `hook`도 시각 카드 뒤에 나오는 첫 source입니다.

플래너는 **전체 원본 event accounting**을 먼저 수행하는 event-flow 방식으로 동작합니다. 점수가 높은 장면부터 고르는 대신, 각 DAY의 원본 시간축을 먼저 훑어 실제로 촬영된 활동을 시간순 `story_event_id`로 묶고 모든 후보와 후보로 덮이지 않은 원본 구간의 상태를 회계합니다. 후보가 없는 구간을 “사건이 없다”고 간주하지 않고 contact sheet와 원본 시간축에서 후보 hole로 검토합니다. 그런 다음에야 인터뷰·식사·놀이·만남·이동 거점·풍경·대화를 여정 흐름에 맞게 구성합니다.

각 사건은 촬영된 범위 안에서 `setup → body/action → reaction/outcome → closure`로 읽습니다. 모든 단계가 항상 필요한 것은 아니지만, 도착·주문·입장 같은 setup이나 퇴장·후기 같은 closure가 핵심 body/action을 대신했다고 판정하지 않습니다. 원본/contact sheet 감사에서 핵심 활동이 실제로 촬영되지 않았다고 판단하면 검수 메모에 `not_filmed`로 남깁니다. 이는 현재 파이프라인이 자동 생성하는 필드를 의미하지 않습니다. “식사를 하는 장면”을 조작해 낸 듯 연결하지 않으며 “식사 후”·“잠시 쉬어 간 뒤” 같은 정직한 카드·캡션·나레이션 브리지만 사용합니다.

길이는 사건을 누락시키는 선택 예산이 아니라 완성본 검토 guard입니다. `target_minutes_per_day`는 `edit-plan.json`의 `target_duration` 호환·표시값이며 fill quota가 아니고, 기본 `soft_max_minutes_per_day: 10`은 사건 흐름을 모두 구성한 뒤 속도와 밀도를 다시 보라는 DAY별 review guard입니다. 10분을 넘는다고 뒤의 사건을 자동 삭제하지 않고, 각 사건을 `full → compact → speed_up → omit`의 순서로 검토합니다. `compact`는 원속도로 setup/body/action/reaction/closure의 핵심 run을 짧게 다듬는 것이고, `speed_up`은 대사가 없고 화면 변화가 적은 이동·대기·반복 풍경에만 적용합니다. 식사·놀이(수영장, 야외 구경, 탈것 등)·인터뷰·사람의 합류/만남·핵심 반응은 `speed=1.0`으로 보호합니다. `omit`은 의미를 반복하거나 사적·기술적으로 사용할 수 없는 구간에 대한 마지막 선택입니다. 기대되는 결과가 guard를 넘어도 흐름이 완전하면 그 길이를 유지하며, 반대로 더 보여 줄 사건이 없으면 10분을 채우지 않습니다.

기본 편집 조합은 `pacing_profile: gentle`과 `tone_profile: playful`입니다. Gentle은 사건을 길게 늘이는 뜻이 아니라 인터뷰·식사·놀이·이동·대화·풍경의 맥락을 이해할 만큼 남기는 호흡입니다. Playful은 사건 수나 길이 envelope를 늘리지 않고 같은 사건 안에서 선택·행동·결과·반응, 가족 상호작용과 화면 변화가 있는 후보를 반복·정지 구간보다 우선합니다. 선택과 공개 사이의 실제 행동 및 공개 뒤 반응을 시간순으로 살리고, `allow_fast`인 변화 적은 브리지만 Gentle 상한 2배 안에서 조금 더 경쾌하게 처리합니다. `tone_profile: calm`은 동일한 사건과 필수 단계를 유지하면서 공간과 대화의 자연스러운 맥락을 우선합니다.

새 plan은 두 profile을 메타데이터에 기록합니다. profile 필드가 생기기 전에 만든 기존 plan은 당시 동작과 3배속 호환성을 보존하기 위해 `balanced + calm`으로 해석하므로, 새 기본값을 적용하려면 `plan`을 다시 실행해야 합니다.

원본 검색은 재귀적이며 폴더 이름과 무관하게 QuickTime 카메라 제조사·모델 메타데이터로 휴대폰, 액션캠, 공유본을 구분하고 익명 source stream을 만듭니다. 따라서 여러 사람의 iPhone 영상이 루트에 섞여 있거나 휴대폰 영상만 있는 여행도 모두 동등한 주 촬영본으로 같은 시간축에 합쳐집니다. 촬영 시각은 QuickTime original-date를 우선하고 `-1000` 같은 compact UTC offset도 해석합니다. 서로 다른 고신뢰 stream이 같은 사건에서 실제 시간상 겹치고 대표 화면까지 강하게 비슷하면 중복 시점으로 묶어 가장 좋은 한 컷을 우선하며, 전사 일치는 이 시각 근거를 보강할 뿐 단독 삭제 근거가 되지 않습니다. 따라서 같은 오디오를 담았어도 화면이 다른 행동·반응 앵글은 살아남아 Playful local plan이 2~8초, 45초 이내에서 사건당 최대 한 컷의 보조 시점으로 사용할 수 있습니다. 서로 다른 필수 인터뷰·이동·식사 계약을 각각 만족해야 하는 중복 그룹은 필요한 컷을 모두 보존합니다. 인터뷰에는 자동 cutaway를 추가하지 않고 필수 식사 body를 다른 각도로 대체하지 않습니다. 수동 추정 시각처럼 `low` confidence인 공유본은 근접 시각만으로 중복 그룹이나 자동 cutaway에 섞지 않고 독립적인 사건 흐름으로만 다룹니다. 세로 영상은 기본 `render.portrait_layout: blur`로 가로 캔버스에 배치하며 `pillarbox`와 `crop`도 선택할 수 있습니다. macOS에서는 iPhone HLG/PQ를 VideoToolbox로 BT.709 SDR에 맞추고 최종 H.264에도 BT.709 색상 태그를 기록합니다.

여행 중간이나 마지막에 가족이 한 명씩 여행 소감·가장 좋았던 순간 등을 묻고 답하는 인터뷰가 전사에서 확실하게 탐지되면, `preserve_family_interviews: true` 기본값이 그 답변 묶음을 필수 모먼트로 지정합니다. 10분 review guard를 넘거나 후보 점수가 낮아도 완결된 연속 구간을 모두 선택하며, local뿐 아니라 Codex/Claude/file 플랜도 누락하거나 배속하면 검증에서 거부합니다. 인터뷰가 탐지되지 않으면 기존 방식으로 정상 진행합니다. 얼굴 인식이나 화자 분리를 하지 않으므로 실제 가족 구성원 수를 판별하는 기능은 아니며, 질문·답변이 이어지는 source 구간을 개인 인터뷰 단위로 보존합니다. 이 보장을 사용할 때는 전사가 필요하므로 `--skip-transcribe`를 함께 쓸 수 없습니다.

`preserve_meal_events: true` 기본값은 전사와 인접 타임라인에서 탐지된 서로 다른 조식·점심·저녁·카페·디저트·간식 사건을 필수 모먼트로 보존합니다. 사건마다 실제 식탁·음식·먹는 반응인 body 후보를 하나의 `one_of` 그룹으로 묶어 최소 하나를 촬영시간순·원속도로 선택합니다. 연결된 식당 도착과 주문은 setup으로, 퇴장·감사 인사뿐 아니라 음식명 또는 식사명과 결합된 맛 반응·식사 회고는 closure로 묶고, 탐지된 각 맥락 그룹에서도 최소 한 장면을 선택합니다. 따라서 흐름은 가능한 경우 `setup → 실제 식사 body/reaction → closure`가 되며, setup이나 closure 같은 context는 body를 대신할 수 없습니다. local뿐 아니라 Codex/Claude/file 플랜도 탐지된 body나 연결 맥락 그룹을 건너뛸 수 없고, 필요한 완결 구간은 10분 review guard보다 우선합니다. 넓은 `food` role 전체를 강제로 넣지는 않으므로 요리책 대사, 막연한 식사 계획, 음식명과 연결되지 않은 과거 회고 같은 오탐으로 영상을 채우지 않습니다. 전사 근거가 없는 visual-only 식사는 자동 탐지 결과만으로 확정할 수 없으므로, 최종 렌더 전 DAY별 전체 후보 contact sheet를 검수해 실제 식사 사건별 body와 자연스러운 전후 서사를 plan에 보강해야 합니다. 원본에 body가 없다면 인접 무음 클립을 body로 오인하지 말고 검수 메모에 `not_filmed`로 남긴 뒤 정직한 브리지를 사용합니다. `not_detected`는 식사가 없었다는 뜻이 아니라 자동 탐지 근거가 없었다는 뜻입니다. 이 보장을 켠 상태에서는 `--skip-transcribe`를 함께 쓸 수 없습니다.

여정을 이해하는 데 필요한 고신뢰 이동 거점도 항상 mandatory-if-detected 정책을 따릅니다. 전사에서 가족·일행을 픽업하거나 합류하는 순간, 환승·경유, 렌터카 인수·반납, 숙소 체크인·체크아웃, 공항·역·터미널의 명시적인 출발·도착·승차·하차가 확인되면, `transition`으로 태그된 해당 연결 구간을 실제 촬영 시간순·원속도로 모두 선택합니다. 이 규칙은 DAY별 review guard보다 우선하며 local·Codex·Claude·file 플랜 모두에서 누락·배속·부적절한 role을 거부합니다. 단, DAY의 첫·마지막 source인 경우에는 기존 `hook`·`closing` role을 유지할 수 있고, 같은 후보가 필수 가족 인터뷰이기도 하면 더 엄격한 인터뷰 role·표시 규칙을 우선합니다.

단순히 “어디 가?”처럼 묻는 말, 구체적 연결점이 없는 일반적인 이동 언급, 공항·차량의 안내방송, 식당·관광지에 도착했다는 말만으로는 필수 이동 거점으로 지정하지 않습니다. 자동 탐지는 명시적인 전사만 사용하며, 알려진 위치는 후속 수동·플래너 검토의 맥락으로만 사용합니다. 실제 GPS 이동 경로나 영상 속 승하차를 자동 인식하지 않으므로 전사되지 않은 이동 거점은 보장할 수 없습니다.

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
  target_minutes_per_day: 4.0       # plan 표시 호환값이며 fill quota가 아님
  soft_max_minutes_per_day: 10.0    # 사건 선택 상한이 아닌 DAY 페이싱 review guard
  selection_strategy: event_flow
  pacing_profile: gentle            # gentle 또는 balanced
  tone_profile: playful             # playful 또는 calm
  adaptive_fast_forward: true       # allow_fast 브리지만 profile/tone 범위에서 배속
  max_fast_forward_speed: 3.0
  exclude_ranges:
    - match: "**/IMG_1234.MOV"     # source 상대경로 또는 basename glob
      start: 12.4                   # 클립 기준 초; 생략 시 0
      end: 28.0                     # 생략 시 클립 끝
      reason: "옷 갈아입는 사적 장면"
  preserve_family_interviews: true  # 탐지된 가족 회고 인터뷰는 반드시 원속도로 포함
  preserve_meal_events: true        # 탐지된 식사마다 setup/body/closure 서사를 원속도로 보존
  episode_mode: trip

render:
  portrait_layout: blur         # blur, pillarbox, crop 중 선택
  trip_intro_style: mosaic
  trip_intro_candidate_ids: []  # 비우면 DAY 커버리지 우선으로 자동 선택
  trip_intro_grid_size: 7       # 6, 7, 8 중 선택
  trip_intro_animation: flow    # flow 또는 static
  intro_seconds: 6.0            # 여러 날 여행 flow 권장값
  date_card_seconds: 3.0
  outro_seconds: 5.0
  transition_seconds: 0.18
```

`target_minutes_per_day`와 `soft_max_minutes_per_day`는 모두 0.1~180 사이의 유한한 숫자여야 합니다. `max_fast_forward_speed`는 1~4배이며, 이 값이 크더라도 `protected_1x`로 표시된 식사·놀이·인터뷰·만남·대화·핵심 반응은 배속하지 않습니다. `exclude_ranges`의 각 규칙은 `match`와 1~160자 `reason`이 필수이며, 지정 시간과 겹치는 후보는 plan·render에 들어가지 않고 event accounting에 “명시적 제외”로 남습니다.

```yaml
project:
  destination: ""  # 비우면 source 폴더명 → project 이름 순으로 자동 추론
```

`trip_intro_style: mosaic`는 최종 edit plan에 실제로 선택되고 JPEG를 정상적으로 읽을 수 있는 source만 사용합니다. 기본 `trip_intro_grid_size: 7`은 최대 49장의 7×7 인트로를 만들며, `6`은 최대 36장의 더 큰 타일, `8`은 최대 64장의 더 촘촘한 전체 조망을 제공합니다. 먼저 usable frame이 있는 각 DAY를 한 장씩 커버하고, DAY가 현재 grid 용량을 넘으면 첫날과 마지막 날을 포함해 전체 여정에서 용량만큼 DAY를 균등하게 고릅니다. 남은 칸은 검토한 후보와 DAY 균형을 따르면서 서로 다른 원본 clip을 한 장씩 우선 사용하고, 고유 원본이 부족할 때만 같은 clip의 추가 프레임을 사용합니다. 기본 후보 순서는 visual quality, 후보 점수, 촬영 시각 순입니다. 선택된 프레임이 grid 용량보다 적으면 남은 셀은 어두운 배경으로 유지합니다. 누락되거나 손상된 프레임은 같은 DAY의 다음 후보로 대체합니다.

`trip_intro_animation: flow`는 선택된 프레임을 최종 edit plan의 시간순으로 정렬한 뒤, 첫째 줄은 왼쪽에서 오른쪽으로, 다음 줄은 오른쪽에서 왼쪽으로 번갈아 가는 serpentine 흐름으로 바둑판을 채웁니다. 각 타일은 짧은 수평 flip/slide로 자리를 잡고, 중앙에는 자동 추론하거나 명시한 여행지와 보정된 전체 여행기간이 모자이크가 충분히 드러난 후반에 반투명한 어두운 패널과 함께 나타납니다. `static`과 classic title-card fallback도 같은 여행지·기간을 사용합니다.

특정 컷을 쓰려면 `trip_intro_candidate_ids`에 이미 plan-selected된 candidate ID를 전역 선택 우선순위로 적습니다. 같은 DAY의 ID를 여러 개 지정할 수 있지만 DAY 커버리지를 먼저 확보하며, 이 목록은 타일 위치를 정하지 않습니다. 오래됐거나 최종 plan에 선택되지 않은 ID는 무시합니다. 실제 타일은 설정 목록의 순서와 무관하게 최종 edit plan의 시간순으로 배치됩니다. `trip_intro_grid_size`는 `6`, `7`, `8`만 허용하며, `trip_intro_style: card`로 모자이크를 끌 수도 있습니다.

이 모자이크는 `.video-summary/<project>/frames/`의 로컬 분석 JPEG를 한 번에 한 장씩 처리하는 저메모리 render-only 작업이며, 원본 고해상도 영상을 한꺼번에 메모리에 올리지 않습니다. 모자이크 style·grid 크기·animation·선택 ID·대표 JPEG·제목 패널·intro 길이만 바뀌면 전사·분석·source segment 캐시는 재사용되므로 `--force`는 필요하지 않습니다. 반면 해상도·fps·encoder·bitrate 같은 출력 형식, font/audio 설정 또는 정확 프레임 정책이 바뀌면 source segment를 다시 렌더합니다. `daily` 인트로는 계속 기존 제목 카드이고, `trip`에서만 모자이크 인트로·아웃트로가 한 번씩, 날짜 카드는 DAY마다 적용됩니다. 세 카드 시간 값은 0.1~30초 사이의 유한한 숫자여야 합니다.

같은 원본에서 겹치거나 맞닿은 후보 창은 후보 생성 때 하나의 연속 구간으로 합치고, planner 검증에서도 같은 원본의 실시간 구간이 1ms를 넘게 겹치는 선택을 거부합니다. 렌더러는 각 source 길이를 목표 fps의 가장 가까운 정수 프레임으로 맞춘 뒤 fps·PTS를 정규화하고 부족한 끝 프레임을 보충한 다음 정확한 프레임 수로 자릅니다. 완성된 캐시도 fps·프레임 수·영상/음성 길이를 다시 확인하므로, concat에서 마지막 프레임이 붙잡혀 반복처럼 보이는 현상을 막습니다.

`transition_seconds`의 기본값은 `0.18`초이고 허용 범위는 0~1초입니다. 같은 원본에서 정확히 맞닿고 편집 속도·표시가 같은 후보들은 먼저 하나의 연속 source로 합칩니다. 같은 사건 ID라도 실제 촬영 간격이 30초를 넘으면 hard cut으로 붙이지 않고, 합쳐지지 않은 DAY 내부 장면 경계와 날짜 카드 앞뒤에는 짧은 블랙·무음 전환을 적용합니다. 조립 단계에서 여러 4K 영상을 동시에 디코드하는 실제 crossfade 대신 각 조각의 길이를 보존하는 저메모리 fade-through-black 방식입니다. 값을 `0`으로 두면 모든 경계 페이드를 끕니다.

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

가족 인터뷰, 필수 이동 거점과 식사 사건으로 탐지된 후보도 같은 개인정보 경계를 따릅니다. Codex/Claude에는 답변, 픽업·환승·승하차 또는 식사 맥락을 드러내는 제한된 전사 발췌가 전달될 수 있고, `--planner-images`를 켜면 가족 얼굴과 식탁이 축소 contact sheet에 포함될 수 있습니다. 이 탐지와 필수 포함 여부 검증 자체는 항상 로컬에서 수행합니다.

외부 플래너가 생성한 JSON은 그대로 실행되지 않습니다. 후보 ID, 날짜, 시간 범위, 순서, 중복, 길이, 허용 필드, 각 DAY의 가장 이른 후보가 첫 장면인지, 탐지된 필수 인터뷰·이동 거점과 각 식사 사건의 body 및 setup/closure `one_of` 그룹이 누락·배속되지 않았는지를 검증한 뒤 프로그램이 안전한 FFmpeg 인자를 다시 생성합니다.

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
│   └── render-report.json       # intro metadata와 필수 모먼트 포함 여부
└── exports/okinawa-2026/
    ├── 2026-08-19-day-01.mp4
    ├── 2026-08-19-day-01.vtt
    ├── 2026-08-19-day-01.chapters.txt
    ├── 2026-08-19-day-01.timeline.txt
    └── 2026-08-19-day-01.description.md
```

기본 `episode_mode: daily`는 날짜별 파일을 만들고, `trip`은 `trip-summary.mp4` 하나와 같은 이름의 `.vtt`, `.chapters.txt`, `.timeline.txt`, `.description.md`를 만듭니다. `render.music_file`에 합법적으로 사용할 수 있는 음악을 지정하면 대화 구간에서 자동 ducking합니다. YouTube 저작권 확인 책임은 사용자에게 있습니다.

`render-report.json`의 `moment_coverage.family_interviews`는 인터뷰가 있으면 `satisfied`, 없으면 `not_detected`를 기록하고, 필수·선택 candidate 수와 개인정보를 최소화한 탐지 근거를 남깁니다. `moment_coverage.meals`도 같은 상태와 함께 자동 탐지된 식사 사건 수, body option 수, 탐지된 setup/closure 맥락 그룹 수, 각 그룹의 실제 선택 ID를 기록합니다. body와 탐지된 맥락 그룹이 모두 선택되어야 `satisfied`입니다. 이때 `not_detected`는 수동 시각 검수를 대체하지 않습니다. 명시적으로 각 보존 기능을 끈 경우에는 `disabled`입니다. 이동 거점은 별도 coverage schema 대신 후보의 `transition` role과 최종 plan으로 검증합니다.

`render-report.json.moment_coverage.story_flow`는 전체 원본 길이, 후보로 회계된 길이와 후보 hole, 이벤트 수/대표 수, 명시적 제외 수, 원본 선택 길이와 배속 후 출력 길이, 배속으로 줄인 시간을 DAY별로 기록합니다. 이벤트별 실제 treatment는 `full`, `full_speed_up`, `compact`, `compact_speed_up`, `omit`으로 기록합니다. DAY 출력이 review guard를 넘으면 실패가 아니라 `review`로 표시합니다. 핵심 사건 누락이나 `protected_1x` 배속은 `unsatisfied`이며, 수상한 후보 hole이 있으면 원본/contact sheet를 다시 보고 사건과 후보를 복구한 뒤 draft를 재생성합니다.

MP4의 인트로·날짜·아웃트로 카드는 영상 프레임에 직접 렌더되는 시각 요소입니다. 반면 `.chapters.txt`는 YouTube 설명란에 복사할 timestamp 텍스트이며 MP4에 embedded chapter metadata로 넣지 않습니다. 파일이 자동으로 YouTube에 업로드되지도 않습니다.

`trip-summary.chapters.txt`는 source 조각이나 내부 `role`마다가 아니라 DAY마다 하나의 챕터를 만듭니다. DAY 1은 여행 인트로를 포함하도록 `00:00`에서 시작하고, DAY 2부터는 해당 날짜 카드의 시작 시각을 사용합니다. timestamp는 오름차순이고 영상 길이 범위 안인지 검증합니다. YouTube 챕터로 사용하려면 첫 항목이 `00:00`이고, timestamp가 최소 3개이며, 각 챕터가 10초 이상인지 업로드 전에 확인하세요.

`.timeline.txt`에는 카드와 모든 source 후보의 상세 누적 시각을 따로 기록합니다. 편집 검증용이며 YouTube 설명란에는 `.chapters.txt`만 사용하세요.

CLI stdout은 기본적으로 clip/candidate 수와 출력 경로만 요약합니다. 디버깅을 위해 전체 내부 payload가 필요하면 하위 명령 앞에 `--full-json`을 둡니다(예: `video-summary --full-json scan ...`).

## 현재 범위

- 의미 기반 전체 영상 비전 모델이나 온라인 GPS 역지오코딩은 포함하지 않습니다.
- 위치는 파일/날짜/전사 키워드 규칙 또는 외부 플래너의 label을 사용합니다.
- 필수 이동 거점의 자동 탐지는 명시적인 전사에 의존합니다. 위치는 후속 검토에만 도움을 주며, 무전사 장면을 행동·경로·위치만으로 추론해 필수화하지는 않습니다.
- D-Log M 색보정은 추측하지 않습니다. iPhone HLG/PQ → BT.709 변환은 macOS VideoToolbox 경로에서만 지원하며, 필요한 변환 필터가 없으면 잘못된 색으로 계속하지 않고 중단합니다.
