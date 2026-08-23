# Pipeline Contract

## One-shot flow

`run`은 다음 단계를 순서대로 실행합니다.

1. `scan`: 원본을 재귀 검색하고 ffprobe 메타데이터, 촬영시각, 여행일, 위치 규칙을 manifest로 정규화
2. `analyze/transcribe`: 자연어 `.srt/.vtt`, whisper.cpp 또는 faster-whisper로 클립별 전사
3. `analyze/visual`: 폭 96px 저해상도 프레임을 스트리밍하여 밝기·대비·움직임 신호 계산
4. `candidates`: 전사 구간과 무음 풍경/이동 구간을 함께 후보화하고, 고신뢰 여정 이동 거점과 식사 setup/body/post 구간을 사건으로 태그하며 대표 프레임 생성
5. `plan`: local 규칙 또는 Codex/Claude가 날짜별 후보 ID를 선택
6. `validate`: candidate ID, 날짜, 시간순, 중복·같은 원본 시간 겹침, 길이와 allowlist를 검증
7. `render`: 한 세그먼트씩 동일 규격으로 렌더하고 concat stream-copy로 날짜별 영상 조립
8. `package`: VTT, YouTube 설명란용 chapters, description 초안과 `moment_coverage`를 포함한 render report 생성

## Stable invariants

- 각 여행일의 모든 source 구간은 첫 장면부터 마지막 장면까지 실제 촬영 시간순입니다. `cold_open`도 이 순서를 깨지 않으며, 선택된 후보 중 가장 이른 하나만 첫 source인 `hook`이 될 수 있습니다.
- 로컬 plan은 각 DAY의 가장 이른 후보를 시작 앵커로 보존합니다. 후보 총량이 목표보다 짧은 날은 목표를 채우기 위해 전부 선택하지 않고 기본 80%의 adaptive ceiling을 적용하며, 의미 있는 마무리와 검증된 필수 사건이 우선합니다. 화면 품질이 충분한 무대사·저대사 `scenery` 후보를 시각 앵커로 검토하므로 전사량만으로 풍경이나 야외 장면을 탈락시키지 않으며, 고립된 점수 상위 조각보다 같은 원본에서 맞닿는 후보 run을 우선합니다. 외부 planner에도 목표가 할당량이 아닌 상한임을 명시합니다.
- `editing.preserve_family_interviews: true`이면 로컬 전사에서 고신뢰 여행 회고 질문·답변으로 탐지된 모든 인터뷰 source run을 필수 사건으로 고정합니다. 해당 후보는 원속도·촬영시간순으로 모두 선택하며 adaptive ceiling보다 우선합니다. 외부/file planner의 누락이나 배속은 validate에서 거부하고, 탐지 결과가 0개면 기존 선택을 그대로 수행합니다. 얼굴 인식·화자 분리는 하지 않으므로 사람 수가 아니라 별도의 Q&A run을 인터뷰 단위로 취급합니다.
- `editing.preserve_meal_events: true`이면 전사 직접 근거 또는 인접 setup/post 타임라인에서 탐지된 조식·점심·저녁·카페·간식의 body를 시간순 사건으로 묶습니다. 실제 식탁·음식·먹는 반응으로 추론된 body option 중 사건마다 하나 이상을 원속도로 선택하며 adaptive ceiling보다 우선합니다. broad `food` role 전체를 필수화하지 않고, 외부/file planner가 한 사건의 option을 모두 누락하거나 식사 전·후 설명만 남기면 validate에서 거부합니다. 완전 무전사 식사는 자동 탐지 범위 밖이므로, 최종 렌더 전 DAY별 전체 candidate contact sheet 시각 인벤토리와 file-plan 보강을 필수 검수 단계로 둡니다. `not_detected`는 자동 근거가 없음을 뜻할 뿐 식사가 없음을 보장하지 않습니다.
- 로컬 전사에서 고신뢰로 확인된 여정의 연결 거점은 항상 `transition`·`journey` role을 갖는 필수 후보입니다. 가족·일행 픽업/합류, 환승/경유, 렌터카 인수/반납, 숙소 체크인/체크아웃, 공항·역·터미널의 명시적 출발/도착/승차/하차가 해당합니다. 태그된 후보를 원속도·촬영시간순으로 모두 선택하고 soft ceiling보다 우선하며, 외부/file planner의 누락·배속·role 위반은 validate에서 거부합니다.
- 단순 질문, 구체적 연결점이 없는 generic 이동, 안내방송, 식당·관광지 도착은 필수 이동 거점으로 태그하지 않습니다. 자동 탐지는 명시적인 전사만 사용하며, 알려진 위치는 후속 수동·플래너 검토의 맥락으로만 사용합니다. 무전사 waypoint를 행동·GPS 경로·위치만으로 추론해 필수화하지는 않습니다.
- 카메라 달력이 초기화된 여행은 `date_overrides`의 폴더 glob, 현지 날짜와 IANA timezone으로 보정할 수 있습니다. 폴더 날짜를 회차 경계로 쓸 때는 `day_start_hour: 0`을 사용합니다.
- 인트로 표시명은 내부 project ID와 분리합니다. `project.destination`이 비어 있으면 source 폴더명에서 선행 날짜와 일반 영상 토큰을 제거하고 더 구체적인 지명이 남을 때만 국가 접두어도 제외해 여행지를 추론하며, 실패하면 project 이름을 사용합니다. 기간은 폴더명 숫자가 아니라 scan이 보정한 전체 `day_key`의 최솟값·최댓값이며, trip은 전체 범위, daily는 해당 DAY 날짜를 표시합니다.
- 원본 파일은 읽기만 하며 모든 파생물은 workspace 아래에 저장합니다.
- 전사, 시각 분석, 렌더는 기본 동시성 1입니다.
- 완료 artifact만 atomic rename으로 공개합니다.
- 같은 원본에서 겹치거나 맞닿은 후보 창은 후보 생성 때 union하고, planner가 같은 원본에서 1ms를 넘게 겹치는 실시간 구간을 선택하면 검증에서 거부합니다.
- source 조각은 목표 fps의 정수 프레임 수로 길이를 양자화하고 fps·PTS·영상/음성 길이를 정규화하며, 캐시 재사용 전에도 정확한 프레임 수와 길이를 확인합니다.
- 프롬프트 변경은 plan/render만, 렌더 설정 변경은 render만 무효화합니다. 필수 이동 거점 또는 식사 사건 탐지 정책 버전이 바뀌면 candidate와 plan을 재생성하지만, 원본·ASR 설정이 같으면 완료된 클립 전사·시각 신호와 실시간 구간/렌더 형식이 같은 source segment cache는 재사용합니다. 선택이 바뀌면 새 source 구간만 렌더하고 assembly를 다시 만듭니다. 이전에 전사를 건너뛰었거나 원본·ASR 설정이 바뀐 클립은 전사부터 다시 수행합니다.
- 외부 planner는 파일 경로나 FFmpeg 인자를 결정할 수 없습니다.
- 기본 출력은 날짜별 1080p/30fps H.264 + AAC입니다.

## Failure and resume

- 손상된 영상, 잘못된 날짜/설정, 후보 없음, out-of-bounds plan은 즉시 실패합니다.
- 가족 인터뷰 또는 식사 사건 보존을 켠 상태에서 `--skip-transcribe`를 요청하면 조용히 놓치지 않고 즉시 실패합니다. 이동 거점 탐지 자체는 별도 fail-fast를 추가하지 않지만, 전사를 건너뛰면 이 보장을 제공할 수 없습니다.
- 성공한 클립 전사·신호·프레임·렌더 세그먼트는 보존합니다.
- 같은 명령을 다시 실행하면 `state.sqlite3`와 artifact cache key를 이용해 미완료 지점부터 이어갑니다.
- 외부 planner의 실행·JSON·검증이 실패하고 strict 모드가 아니면 그 실행만 local fallback으로 완주하되, fallback을 영구 cache hit로 취급하지 않습니다.

## Planner boundary

Planner가 반환할 수 있는 것은 프로젝트/후보 해시, 날짜별 제목·요약, candidate ID와 편집 메타데이터(role/reason 및 선택적 location/caption/speed)뿐입니다. `role=hook`은 해당 DAY의 첫 번째 source이자 선택된 후보 중 실제 촬영 시각이 가장 이른 후보에만 허용됩니다. 탐지된 필수 인터뷰 후보는 `role=interview`(첫 hook 예외), 필수 이동 거점 후보는 기본 `role=transition`으로, 모두 `speed=1.0`과 실제 촬영 시간순을 지켜 포함해야 합니다. 이동 거점이 DAY의 첫·마지막 source이면 각각 `hook`·`closing`을 예외로 허용하고, 필수 인터뷰와 겹치면 인터뷰 계약을 우선합니다. 일반 `journey` 후보는 그 이유만으로 필수가 아닙니다. 원본 경로·시간 범위·FFmpeg 인자는 반환할 수 없으며 renderer가 검증된 catalog에서 다시 조회합니다. Codex/Claude planner는 이 판단을 위해 필수 이동 거점을 드러내는 제한된 전사 발췌와 메타데이터를 받을 수 있습니다.

## Output modes

- `daily` (기본): 여행일마다 MP4 하나
- `trip`: 여행 인트로 모자이크(실패 시 제목 카드) → DAY 날짜 카드 → 해당 DAY의 모든 source(`hook` 포함, 시간순) → 다음 DAY 반복 → 여행 아웃트로 카드 순서의 합본 MP4

두 모드 모두 인트로, 날짜 카드, 위치 lower-third, 아웃트로와 YouTube 보조 파일을 생성합니다. 날짜 카드와 아웃트로의 권장 길이는 각각 `date_card_seconds: 3.0`, `outro_seconds: 5.0`입니다. 여러 날 여행의 flow 모자이크는 `intro_seconds: 6.0`을 권장하며, 각 카드 길이는 0.1~30초 사이의 유한한 숫자여야 합니다.

`trip_intro_style: mosaic`는 plan-selected source 중 정상적으로 읽을 수 있는 candidate JPEG만 대상으로 합니다. `trip_intro_grid_size`는 `6`, `7`, `8`만 허용하고 기본값 `7`은 최대 49장의 7×7 모자이크를 만듭니다. `6`은 최대 36장의 더 큰 타일, `8`은 최대 64장의 조밀한 타일 옵션입니다. 먼저 usable frame이 있는 DAY마다 한 장을 확보합니다. usable DAY가 grid 용량보다 많으면 첫날·마지막 날을 포함해 용량만큼 전체 기간에서 균등 선택합니다. 남은 칸에는 `trip_intro_candidate_ids`와 DAY별 균형을 반영하되, 서로 다른 원본 clip을 한 장씩 먼저 사용하고 고유 원본이 부족할 때만 같은 clip을 반복합니다. 자동 후보 순서는 `visual_quality desc → score desc → captured_at asc → candidate_id`입니다. 용량보다 적은 프레임은 어두운 빈 셀을 남기며, 손상·누락 JPEG는 같은 DAY의 다음 후보로 넘어갑니다.

`trip_intro_animation`은 `flow` 또는 `static`만 허용합니다. 기본 `flow`는 선택 프레임을 edit-plan 시간순으로 정렬하고, 행마다 좌→우와 우→좌를 번갈아 진행하는 serpentine 순서로 cell을 채웁니다. 각 tile은 짧은 horizontal flip/slide 뒤 자리를 잡으며, 중앙에는 추론된 여행지와 전체 여행기간이 반투명 어두운 panel로 후반에 나타납니다. `static`과 classic title-card fallback도 동일한 표시 메타데이터를 사용합니다. 추론 결과와 provenance는 `render-report.json.intro_metadata`에 기록하며 변경 시 카드와 assembly만 무효화하고 source segment 캐시는 재사용합니다.

`trip_intro_candidate_ids`는 이미 plan-selected된 ID의 전역 선택 우선순위이며 타일 위치 목록이 아닙니다. 같은 DAY의 ID를 여러 개 포함할 수 있지만 DAY 커버리지를 먼저 확보합니다. 오래됐거나 최종 plan에 선택되지 않은 ID는 무시하며, 선택된 타일은 최종 edit plan의 시간순으로 배치합니다. `daily` 인트로에는 이 설정이 영향을 주지 않습니다.

모자이크는 기존 candidate JPEG를 한 번에 한 장씩 로컬에서 합성하는 저메모리 render-only 파생물입니다. 모자이크를 켠 것만으로 외부 전송이 생기지는 않지만, 별도로 `--planner-images`를 쓰면 candidate frame 기반의 축소 contact sheet가 외부 플래너에 전달됩니다. style·grid·animation·선택 ID·프레임 지문·제목·기간·렌더 형식은 render cache에 포함합니다. 모자이크 레이아웃·animation·선택·제목 패널·intro 길이만 바뀌면 transcript/analyze/source segment cache를 재사용합니다. 반면 source 출력 형식이나 정확 프레임 정책이 바뀌면 source segment를 다시 렌더합니다.

`transition_seconds`는 기본 `0.18`초이며 0~1초만 허용합니다. 정확히 맞닿고 speed/location/caption이 같은 동일 원본 후보는 먼저 하나의 source로 합칩니다. 합쳐지지 않은 모든 DAY 내부 group 경계와 날짜 카드 경계에서는 앞 조각의 영상·음성이 블랙·무음으로 fade-out하고 다음 조각이 fade-in합니다. 실제 crossfade filter graph 없이 각 piece 길이를 그대로 보존하는 저메모리 fade-through-black이며, `0`이면 경계 fade를 끕니다.

MP4의 카드는 영상 프레임에 렌더되는 시각 요소입니다. `.chapters.txt`는 MP4 embedded chapter가 아니라 YouTube 설명란에 복사할 timestamp 텍스트이고, 자동 업로드되지 않습니다. `trip`에서는 DAY마다 챕터 하나를 생성하며 DAY 1은 여행 인트로를 포함한 `00:00`, 이후 DAY는 해당 날짜 카드의 시작 시각을 사용합니다. timestamp의 오름차순·영상 범위를 검증하고, YouTube에 사용할 때는 `00:00` 시작·최소 3개·각 챕터 10초 이상 조건을 확인합니다.

카드와 모든 source 후보의 상세 누적 시각은 별도 `.timeline.txt`에 보존하며 YouTube 챕터와 섞지 않습니다.
