# Pipeline Contract

## One-shot flow

`run`은 다음 단계를 순서대로 실행합니다.

1. `scan`: 원본을 재귀 검색하고 ffprobe 메타데이터, 촬영시각, 여행일, 위치 규칙을 manifest로 정규화
2. `analyze/transcribe`: 자연어 `.srt/.vtt`, whisper.cpp 또는 faster-whisper로 클립별 전사
3. `analyze/visual`: 폭 96px 저해상도 프레임을 스트리밍하여 밝기·대비·움직임 신호 계산
4. `candidates`: 전사 구간과 무음 풍경/이동 구간을 함께 후보화하고, 원본 시간축의 모든 활동을 `story_event_id`와 `setup/body/action/reaction/outcome/closure/bridge` 단계로 회계하며 제외·후보 hole을 기록
5. `plan`: local 규칙 또는 Codex/Claude가 모든 사건의 시간순 스토리 스파인을 먼저 구성한 뒤 `full → compact → speed_up → omit` 순으로 압축
6. `validate`: candidate ID, 날짜, 시간순, 중복·같은 원본 시간 겹침, 길이와 allowlist를 검증
7. `render`: 한 세그먼트씩 동일 규격으로 렌더하고 concat stream-copy로 날짜별 영상 조립
8. `package`: VTT, YouTube 설명란용 chapters, description 초안과 `moment_coverage`를 포함한 render report 생성

## Stable invariants

- 각 여행일의 모든 source 구간은 첫 장면부터 마지막 장면까지 실제 촬영 시간순입니다. `cold_open`도 이 순서를 깨지 않으며, 선택된 후보 중 가장 이른 하나만 첫 source인 `hook`이 될 수 있습니다.
- plan은 coverage-first event-flow입니다. 선택 예산을 보기 전에 각 DAY의 전체 원본을 훑어 실제 활동을 시간순 `story_event_id`로 묶고, 모든 후보를 사건/단계/중요도/속도 정책에 할당합니다. 후보로 덮이지 않는 원본 길이는 `unassigned_source_seconds`로 드러내며, candidate hole이 사건 부재를 의미하지 않으므로 contact sheet/원본 시간축을 재검수합니다. 전체 사건 스파인을 구성한 뒤에야 편집 방법을 결정합니다.
- 사건의 서사 단위는 `setup → body/action → reaction/outcome → closure`입니다. 사건 성격과 실제 촬영 범위에 따라 단계가 생략될 수는 있지만 setup/closure로 body/action을 대체하지 않습니다. 원본/contact sheet 감사에서 body/action이 촬영되지 않았다고 판단하면 검수 메모에 `not_filmed`로 남기고, setup과 closure만으로 완결된 활동처럼 연출하지 않습니다. `not_filmed`는 현재 파이프라인이 자동 생성하는 필드가 아닙니다. 필요하면 “식사 후”·“잠시 쉬어 간 뒤”처럼 편집 공백을 명시하는 정직한 카드·캡션·나레이션 브리지만 사용합니다.
- 편집 압축 사다리는 `full → compact → speed_up → omit`입니다. 먼저 사건을 충분히 원속도로 담고, 길면 사건 내부의 중복만 잘라 핵심 run을 원속도로 `compact`하며, 그래도 길 때만 대사 없는 low-change 이동·대기·반복 풍경 브리지를 `speed_up`합니다. 식사·놀이·인터뷰·만남/합류·대화·핵심 행동과 반응은 `protected_1x`이며, `omit`은 의미 중복·사적 장면·사용 불가 구간에 대한 마지막 선택입니다. story-flow 보고서의 event treatment는 실제 결과를 `full`, `full_speed_up`, `compact`, `compact_speed_up`, `omit`으로 기록하며, `_speed_up`은 해당 사건의 허용된 브리지에만 배속이 쓰였음을 뜻합니다.
- `editing.pacing_profile`은 사건 자체를 삭제하는 길이 제한이 아니라 사건 내부 반복을 compact하는 source-time soft envelope입니다. `gentle`은 인터뷰/식사/놀이/이동 거점/대화/풍경에 각각 120/60/70/30/45/20초, `balanced`는 90/45/50/20/30/15초를 기준으로 삼습니다. 필수 서사 단계와 연속된 핵심 run은 envelope를 넘겨 보존할 수 있습니다. `gentle`의 배속 상한은 2배이며, `balanced`도 `protected_1x`는 건드리지 않고 `allow_fast` 브리지만 설정 상한 안에서 더 적극적으로 압축합니다.
- 기본 조합은 `pacing_profile: gentle`과 `tone_profile: playful`입니다. playful은 사건 수나 envelope를 늘리지 않고, 이미 대표하기로 한 사건 안에서 선택·행동·결과·리액션, 가족 상호작용과 화면 변화가 있는 후보를 반복·정지 구간보다 우선합니다. 선택과 공개 사이의 실제 행동, 공개 뒤 반응을 시간순 chain으로 보존합니다. 신뢰 가능한 다른 source stream의 2~8초 후보가 같은 사건의 선택 장면과 45초 이내이면 envelope 안에서 사건당 최대 한 컷의 보조 시점으로 사용할 수 있습니다. 인터뷰에는 자동 cutaway를 추가하지 않고, 필수 식사 option을 대체하지 않습니다. 수동 추정처럼 촬영시각 confidence가 `low`인 후보는 근접 시각만으로 자동 cutaway나 동시 앵글에 넣지 않습니다. `calm`은 같은 사건과 필수 단계를 유지하면서 자연스러운 공간·대화 맥락을 우선합니다.
- `target_minutes_per_day`는 episode `target_duration` 호환·표시값이지 fill quota나 실제 선택 상한이 아닙니다. `soft_max_minutes_per_day: 10`은 완성된 DAY의 페이싱과 압축 여지를 다시 보는 review guard입니다. guard 초과는 삭제 명령이 아니며, 고유한 사건을 뒤에서부터 자동으로 떨어뜨리지 않습니다. 전체 흐름이 필요하면 guard를 넘고, 반복 외에 더 보여 줄 것이 없으면 guard 아래에서 멈춥니다. 외부 planner에도 같은 계약을 제공합니다.
- `editing.preserve_family_interviews: true`이면 로컬 전사에서 고신뢰 여행 회고 질문·답변으로 탐지된 모든 인터뷰 source run을 필수 사건으로 고정합니다. 해당 후보는 원속도·촬영시간순으로 모두 선택하며 `soft_max_minutes_per_day`보다 우선합니다. 외부/file planner의 누락이나 배속은 validate에서 거부하고, 탐지 결과가 0개면 기존 선택을 그대로 수행합니다. 얼굴 인식·화자 분리는 하지 않으므로 사람 수가 아니라 별도의 Q&A run을 인터뷰 단위로 취급합니다.
- `editing.preserve_meal_events: true`이면 전사 직접 근거 또는 인접 타임라인에서 탐지된 조식·점심·저녁·카페·디저트·간식을 시간순 사건으로 묶습니다. 실제 식탁·음식·먹는 반응인 body option 중 사건마다 하나 이상을 원속도로 선택합니다. 식당 도착·주문은 setup, 퇴장·감사 또는 음식명/식사명과 결합된 맛 반응·회고는 closure context로 묶고, 탐지된 각 그룹도 하나 이상 선택합니다. 이 필수 흐름은 review guard보다 우선하며 setup/closure context만으로 body를 대신할 수 없습니다. broad `food` role 전체를 필수화하지 않고, 외부/file planner가 body나 탐지된 맥락 그룹을 누락하면 validate에서 거부합니다. 전사 근거가 없는 visual-only 식사는 자동 탐지 결과만으로 확정하지 않으므로, 최종 렌더 전 DAY별 전체 candidate contact sheet 시각 인벤토리와 file-plan 보강을 필수 검수 단계로 둡니다. body가 원본에 없다면 검수 메모에 `not_filmed`로 남기고 인접 무음 클립을 body로 오인하지 않으며, 정직한 브리지로만 다음 사건을 연결합니다. `not_detected`는 자동 근거가 없음을 뜻할 뿐 식사가 없음을 보장하지 않습니다.
- 로컬 전사에서 고신뢰로 확인된 여정의 연결 거점은 항상 `transition`·`journey` role을 갖는 필수 후보입니다. 가족·일행 픽업/합류, 환승/경유, 렌터카 인수/반납, 숙소 체크인/체크아웃, 공항·역·터미널의 명시적 출발/도착/승차/하차가 해당합니다. 태그된 후보를 원속도·촬영시간순으로 모두 선택하고 review guard보다 우선하며, 외부/file planner의 누락·배속·role 위반은 validate에서 거부합니다.
- 식사·놀이(수영장, 야외 구경, 탈것)·가족 인터뷰·사람의 만남/합류·핵심 반응과 대화는 항상 `speed=1.0`으로 보호합니다. 화면 변화가 적은 generic 이동·대기·반복 풍경만 `allow_fast`이며, 이동이라도 픽업/합류·승하차·렌터카 인수/반납·체크인/체크아웃 같은 사건 행동은 배속하지 않습니다.
- 단순 질문, 구체적 연결점이 없는 generic 이동, 안내방송, 식당·관광지 도착은 필수 이동 거점으로 태그하지 않습니다. 자동 탐지는 명시적인 전사만 사용하며, 알려진 위치는 후속 수동·플래너 검토의 맥락으로만 사용합니다. 무전사 waypoint를 행동·GPS 경로·위치만으로 추론해 필수화하지는 않습니다.
- `editing.exclude_ranges`는 옷 갈아입기·의료/위생·민감 정보·명시적 사용 금지 같은 구간을 source 상대경로/파일명 glob, 클립 기준 `start`/`end` 초, 필수 `reason`으로 지정합니다. 범위와 겹치는 candidate는 `exclusion_reason`과 `speed_policy=omit`을 가지고 plan/render에서 제외되며, story-flow 보고서에는 명시적 제외로 회계됩니다.
- 카메라 달력이 초기화된 여행은 `date_overrides`의 폴더 glob, 현지 날짜와 IANA timezone으로 보정할 수 있습니다. 폴더 날짜를 회차 경계로 쓸 때는 `day_start_hour: 0`을 사용합니다.
- 인트로 표시명은 내부 project ID와 분리합니다. `project.destination`이 비어 있으면 source 폴더명에서 선행 날짜와 일반 영상 토큰을 제거하고 더 구체적인 지명이 남을 때만 국가 접두어도 제외해 여행지를 추론하며, 실패하면 project 이름을 사용합니다. 기간은 폴더명 숫자가 아니라 scan이 보정한 전체 `day_key`의 최솟값·최댓값이며, trip은 전체 범위, daily는 해당 DAY 날짜를 표시합니다.
- 원본 파일은 읽기만 하며 모든 파생물은 workspace 아래에 저장합니다.
- 전사, 시각 분석, 렌더는 기본 동시성 1입니다.
- 완료 artifact만 atomic rename으로 공개합니다.
- 같은 원본에서 겹치거나 맞닿은 후보 창은 후보 생성 때 union하고, planner가 같은 원본에서 1ms를 넘게 겹치는 실시간 구간을 선택하면 검증에서 거부합니다.
- source 조각은 목표 fps의 정수 프레임 수로 길이를 양자화하고 fps·PTS·영상/음성 길이를 정규화하며, 캐시 재사용 전에도 정확한 프레임 수와 길이를 확인합니다.
- 프롬프트 변경은 plan/render만, 렌더 설정 변경은 render만 무효화합니다. 필수 이동 거점 또는 식사 사건 탐지 정책 버전이 바뀌면 candidate와 plan을 재생성하지만, 원본·ASR 설정이 같으면 완료된 클립 전사·시각 신호와 실시간 구간/렌더 형식이 같은 source segment cache는 재사용합니다. 선택이 바뀌면 새 source 구간만 렌더하고 assembly를 다시 만듭니다. 이전에 전사를 건너뛰었거나 원본·ASR 설정이 바뀐 클립은 전사부터 다시 수행합니다.
- 외부 planner는 파일 경로나 FFmpeg 인자를 결정할 수 없습니다.
- 새 plan은 pacing/tone profile을 명시합니다. 두 필드가 없던 기존 plan은 과거 동작과 3배속 호환성을 위해 `balanced + calm`으로 해석하며, 새 기본 `gentle + playful`을 적용하려면 plan을 다시 생성합니다.
- 기본 출력은 날짜별 1080p/30fps H.264 + AAC입니다.

## Failure and resume

- 손상된 영상, 잘못된 날짜/설정, 후보 없음, out-of-bounds plan은 즉시 실패합니다.
- 가족 인터뷰 또는 식사 사건 보존을 켠 상태에서 `--skip-transcribe`를 요청하면 조용히 놓치지 않고 즉시 실패합니다. 이동 거점 탐지 자체는 별도 fail-fast를 추가하지 않지만, 전사를 건너뛰면 이 보장을 제공할 수 없습니다.
- 성공한 클립 전사·신호·프레임·렌더 세그먼트는 보존합니다.
- 같은 명령을 다시 실행하면 `state.sqlite3`와 artifact cache key를 이용해 미완료 지점부터 이어갑니다.
- 외부 planner의 실행·JSON·검증이 실패하고 strict 모드가 아니면 그 실행만 local fallback으로 완주하되, fallback을 영구 cache hit로 취급하지 않습니다.

## Planner boundary

Planner가 반환할 수 있는 것은 프로젝트/후보 해시, 날짜별 제목·요약, candidate ID와 편집 메타데이터(role/reason 및 선택적 location/caption/speed)뿐입니다. `role=hook`은 해당 DAY의 첫 번째 source이자 선택된 후보 중 실제 촬영 시각이 가장 이른 후보에만 허용됩니다. 탐지된 필수 인터뷰 후보는 `role=interview`(첫 hook 예외), 필수 이동 거점 후보는 기본 `role=transition`으로, 모두 `speed=1.0`과 실제 촬영 시간순을 지켜 포함해야 합니다. 이동 거점이 DAY의 첫·마지막 source이면 각각 `hook`·`closing`을 예외로 허용하고, 필수 인터뷰와 겹치면 인터뷰 계약을 우선합니다. 일반 `journey` 후보는 그 이유만으로 필수가 아닙니다. 외부 planner도 전체 사건과 촬영된 서사 단계를 먼저 시간순으로 구성하고, review guard를 넘을 때는 `full → compact → speed_up → omit` 순으로 압축 여지를 검토합니다. 중복 외의 사건을 guard 때문에 누락하거나 `target_duration`을 채우기 위해 반복 장면을 추가해서는 안 됩니다. `speed>1.0`은 `speed_policy=allow_fast`인 low-change 무대사 브리지에만 허용하고 `exclusion_reason`이 있는 후보는 선택할 수 없습니다. 원본 경로·시간 범위·FFmpeg 인자는 반환할 수 없으며 renderer가 검증된 catalog에서 다시 조회합니다. Codex/Claude planner는 이 판단을 위해 사건·단계·중요도·속도 정책·제외 이유와 제한된 전사 발췌를 받을 수 있습니다.

```yaml
editing:
  target_minutes_per_day: 4.0     # plan target_duration 호환·표시값
  soft_max_minutes_per_day: 10.0  # 선택 상한이 아닌 페이싱 review guard
  selection_strategy: event_flow
  pacing_profile: gentle          # gentle 또는 balanced
  tone_profile: playful           # playful 또는 calm
  adaptive_fast_forward: true
  max_fast_forward_speed: 3.0
  exclude_ranges:
    - match: "**/IMG_1234.MOV"
      start: 12.4
      end: 28.0
      reason: "사적 장면"
```

두 값은 0.1~180 사이의 유한한 숫자여야 합니다.

### Pacing A/B comparison

같은 전체 candidate set에서 한 날짜의 호흡만 비교할 때는 각 profile의 plan을 별도 파일로 보존한 뒤 날짜 범위 렌더를 사용합니다. `--day-key`는 전체 plan을 먼저 검증한 다음 해당 DAY만 렌더하며, `--output-tag` 결과는 `exports/<project>/comparisons/`에 서로 덮어쓰지 않고 저장됩니다.

```bash
video-summary plan --project sample-trip --planner local --pacing-profile gentle
cp .video-summary/sample-trip/edit-plan.json .video-summary/sample-trip/edit-plan-gentle.json

video-summary plan --project sample-trip --planner local --pacing-profile balanced
cp .video-summary/sample-trip/edit-plan.json .video-summary/sample-trip/edit-plan-balanced.json

video-summary render --project sample-trip --day-key 2025-02-05 \
  --plan-file .video-summary/sample-trip/edit-plan-gentle.json --output-tag gentle --draft
video-summary render --project sample-trip --day-key 2025-02-05 \
  --plan-file .video-summary/sample-trip/edit-plan-balanced.json --output-tag balanced --draft
```

## Output modes

- `daily` (기본): 여행일마다 MP4 하나
- `trip`: 여행 인트로 모자이크(실패 시 제목 카드) → DAY 날짜 카드 → 해당 DAY의 모든 source(`hook` 포함, 시간순) → 다음 DAY 반복 → 여행 아웃트로 카드 순서의 합본 MP4

두 모드 모두 인트로, 날짜 카드, 위치 lower-third, 아웃트로와 YouTube 보조 파일을 생성합니다. 날짜 카드와 아웃트로의 권장 길이는 각각 `date_card_seconds: 3.0`, `outro_seconds: 5.0`입니다. 여러 날 여행의 flow 모자이크는 `intro_seconds: 6.0`을 권장하며, 각 카드 길이는 0.1~30초 사이의 유한한 숫자여야 합니다.

`trip_intro_style: mosaic`는 plan-selected source 중 정상적으로 읽을 수 있는 candidate JPEG만 대상으로 합니다. `trip_intro_grid_size`는 `6`, `7`, `8`만 허용하고 기본값 `7`은 최대 49장의 7×7 모자이크를 만듭니다. `6`은 최대 36장의 더 큰 타일, `8`은 최대 64장의 조밀한 타일 옵션입니다. 먼저 usable frame이 있는 DAY마다 한 장을 확보합니다. usable DAY가 grid 용량보다 많으면 첫날·마지막 날을 포함해 용량만큼 전체 기간에서 균등 선택합니다. 남은 칸에는 `trip_intro_candidate_ids`와 DAY별 균형을 반영하되, 서로 다른 원본 clip을 한 장씩 먼저 사용하고 고유 원본이 부족할 때만 같은 clip을 반복합니다. 자동 후보 순서는 `visual_quality desc → score desc → captured_at asc → candidate_id`입니다. 용량보다 적은 프레임은 어두운 빈 셀을 남기며, 손상·누락 JPEG는 같은 DAY의 다음 후보로 넘어갑니다.

`trip_intro_animation`은 `flow` 또는 `static`만 허용합니다. 기본 `flow`는 선택 프레임을 edit-plan 시간순으로 정렬하고, 행마다 좌→우와 우→좌를 번갈아 진행하는 serpentine 순서로 cell을 채웁니다. 각 tile은 짧은 horizontal flip/slide 뒤 자리를 잡으며, 중앙에는 추론된 여행지와 전체 여행기간이 반투명 어두운 panel로 후반에 나타납니다. `static`과 classic title-card fallback도 동일한 표시 메타데이터를 사용합니다. 추론 결과와 provenance는 `render-report.json.intro_metadata`에 기록하며 변경 시 카드와 assembly만 무효화하고 source segment 캐시는 재사용합니다.

`trip_intro_candidate_ids`는 이미 plan-selected된 ID의 전역 선택 우선순위이며 타일 위치 목록이 아닙니다. 같은 DAY의 ID를 여러 개 포함할 수 있지만 DAY 커버리지를 먼저 확보합니다. 오래됐거나 최종 plan에 선택되지 않은 ID는 무시하며, 선택된 타일은 최종 edit plan의 시간순으로 배치합니다. `daily` 인트로에는 이 설정이 영향을 주지 않습니다.

모자이크는 기존 candidate JPEG를 한 번에 한 장씩 로컬에서 합성하는 저메모리 render-only 파생물입니다. 모자이크를 켠 것만으로 외부 전송이 생기지는 않지만, 별도로 `--planner-images`를 쓰면 candidate frame 기반의 축소 contact sheet가 외부 플래너에 전달됩니다. style·grid·animation·선택 ID·프레임 지문·제목·기간·렌더 형식은 render cache에 포함합니다. 모자이크 레이아웃·animation·선택·제목 패널·intro 길이만 바뀌면 transcript/analyze/source segment cache를 재사용합니다. 반면 source 출력 형식이나 정확 프레임 정책이 바뀌면 source segment를 다시 렌더합니다.

원본 검색은 하위 폴더를 재귀 탐색하고, 폴더 이름이 아니라 QuickTime 카메라 제조사·모델 메타데이터로 `phone`·`action_camera`·`shared` source kind와 익명 stream ID를 정합니다. 여러 사람의 휴대폰 영상이 루트에 섞이거나 휴대폰 영상만 있어도 모두 동등한 주 촬영본으로 같은 시간축에 합쳐집니다. 촬영 시각은 QuickTime original-date를 일반 creation time보다 우선하며 `-1000` 같은 compact UTC offset도 해석합니다. 서로 다른 고신뢰 stream이 같은 사건에서 실시간으로 충분히 겹치고 대표 화면까지 강하게 비슷할 때만 중복 앵글 그룹으로 묶어 일반 선택은 가장 좋은 한 시점을 남깁니다. 전사 일치는 이 시각 근거의 threshold를 보강할 뿐 단독 억제 근거가 되지 않으므로, 같은 오디오를 담은 시각적으로 다른 행동·반응 앵글은 playful 보조 컷 후보로 남습니다. 각 필수 인터뷰·이동 all-of 또는 서로 다른 식사 one-of 계약을 개별적으로 만족해야 하는 중복 앵글만 함께 남길 수 있습니다. 추정 시각 confidence가 `low`인 공유본은 자동 중복 억제와 alternate-angle cutaway에서 제외하고 독립 event-flow로만 다룹니다. 외부 planner에는 로컬 경로나 원본 카메라 식별자를 노출하지 않고 익명 stream과 confidence만 제공합니다. `render.portrait_layout`은 세로 영상을 가로 프레임에 넣는 `blur`(기본), `pillarbox`, `crop` 중 하나입니다. iPhone HLG/PQ HDR source는 macOS VideoToolbox의 `scale_vt`로 BT.709 SDR 출력에 맞추며, 최종 H.264에도 BT.709 색상 태그를 기록합니다. HDR 변환 필터가 없는 환경에서는 색이 틀린 결과를 조용히 만들지 않고 오류로 중단합니다.

`transition_seconds`는 기본 `0.18`초이며 0~1초만 허용합니다. 정확히 맞닿고 speed/location/caption이 같은 동일 원본 후보는 먼저 하나의 source로 합칩니다. 같은 사건의 조각도 실제 촬영 간격이 30초 이내일 때만 hard cut으로 연결하며, 더 멀리 떨어진 잘못된 동일 event ID나 다른 source group 및 날짜 카드 경계는 짧은 블랙·무음 fade를 사용합니다. 실제 crossfade filter graph 없이 각 piece 길이를 그대로 보존하는 저메모리 fade-through-black이며, `0`이면 경계 fade를 끕니다.

MP4의 카드는 영상 프레임에 렌더되는 시각 요소입니다. `.chapters.txt`는 MP4 embedded chapter가 아니라 YouTube 설명란에 복사할 timestamp 텍스트이고, 자동 업로드되지 않습니다. `trip`에서는 DAY마다 챕터 하나를 생성하며 DAY 1은 여행 인트로를 포함한 `00:00`, 이후 DAY는 해당 날짜 카드의 시작 시각을 사용합니다. timestamp의 오름차순·영상 범위를 검증하고, YouTube에 사용할 때는 `00:00` 시작·최소 3개·각 챕터 10초 이상 조건을 확인합니다.

카드와 모든 source 후보의 상세 누적 시각은 별도 `.timeline.txt`에 보존하며 YouTube 챕터와 섞지 않습니다.
