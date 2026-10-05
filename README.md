# Drum Score

오디오와 비디오에서 드럼을 분리하고 **MusicXML + 인쇄 가능한 SVG**로 채보하는 로컬 웹 앱입니다.
MuseScore가 있으면 PDF도 생성합니다. 브라우저에 파일을 놓고 **채보 시작하기**를 누르세요.

## 실행

Python 3.11 또는 3.12, [uv](https://docs.astral.sh/uv/getting-started/installation/), FFmpeg가 필요합니다.
`.python-version`은 3.12를 선택합니다.

macOS:

```sh
brew install ffmpeg
brew install --cask musescore
uv sync --frozen
uv run drum-score serve
```

Linux CPU:

```sh
sudo apt-get install ffmpeg git
uv sync --frozen --extra cpu
uv run --extra cpu drum-score serve
```

Linux x86_64 NVIDIA GPU:

```sh
uv sync --frozen --extra cuda
uv run --extra cuda drum-score serve
```

CPU와 CUDA extra는 동시에 선택할 수 없습니다. CUDA 빌드는 CUDA 12.8을 사용하므로 호스트 NVIDIA
드라이버가 호환되어야 합니다. `uv run`에도 선택한 extra를 지정하세요. Linux ARM64는 `sphn`을
소스에서 빌드하므로 `build-essential pkg-config libopus-dev`가 추가로 필요합니다. Rust toolchain도
패키지 빌드 과정에서 필요합니다.

브라우저: **http://127.0.0.1:8000**

```sh
uv run drum-score doctor
uv run drum-score download-model
uv run drum-score serve --port 8080 --data-dir /path/to/local/data
```

Linux 호스트를 원격으로 사용한다면 SSH 포워딩으로 접속할 수 있습니다:
`ssh -L 8000:127.0.0.1:8000 user@host`.

## Docker (Linux CPU)

```sh
docker build -t drum-score:local .
docker run --rm -p 127.0.0.1:8000:8000 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/work/model-cache:/root/.cache/huggingface" \
  drum-score:local
```

기본 컨테이너는 CPU용이며 Verovio로 SVG를 생성합니다. Apple Silicon에서 실행하는 Docker도
Linux CPU를 사용합니다. MPS를 쓰려면 macOS에서 직접 실행하세요.

## 처리 흐름

1. FFmpeg: 비디오 첫 오디오 트랙/음원을 44.1 kHz 스테레오 PCM WAV로 변환.
2. Demucs 4.1 `htdemucs`: 드럼 stem 추출. 드럼 전용 입력이면 이 단계 생략.
3. ADTOF PyTorch Frame_RNN: 100 fps 타격 분석, 클래스별 peak picking.
4. Librosa 비트 추적 + 전체 비트 위치 회귀: BPM 및 quarter-note phase 추정.
5. 리듬 격자: 8분, 16분, 8분 셋잇단음표 및 4/4·3/4·6/8·7/8.
6. MusicXML 4.0: 타악기 보표, 악기별 GM 매핑, 킥/나머지 악기 성부, 동시 타격.
7. MuseScore CLI: MusicXML → SVG 페이지 및 A4 PDF. 미설치 시 Verovio → SVG.

가중치는 ADTOF 포트에 포함되어 있고 Demucs는 첫 실행에 다운로드합니다. 미리 내려받으면
이후 파일 분석은 로컬에서 진행합니다.

## 결과와 편집

- SVG 미리보기·페이지별 다운로드·브라우저 인쇄·전체 ZIP.
- 편집용 MusicXML, 격자 MIDI, 원시 타격 MIDI, 이벤트 JSON, 분석 JSON.
- 원음/분리된 드럼 대조 재생.
- BPM·첫 박 위치·박자·격자 수정과 마디별 타격 추가/제거.
- 악보 재생성은 검출된 타격을 재사용합니다. 감도를 바꾸려면 새 분석을 시작하세요.
- 단일 작업 큐, 진행률, 취소, 재시도, 영구 기록 및 작업 삭제.
- 렌더링 실패 시 분석 데이터를 재사용하여 조판만 다시 시도합니다.
- 수정한 악보가 성공적으로 만들어질 때까지 이전 출력 파일을 유지합니다.

데이터는 기본 `data/` 아래에 저장됩니다. 원본 업로드, PCM, drum stem, 모델 활성값,
원시 이벤트, 각 렌더링 세대, SQLite 작업 기록이 포함됩니다. 웹 화면에서 작업을 삭제하면
해당 입력과 결과도 삭제됩니다. 서버는 한 프로세스로 실행해야 합니다. 서버 중단 당시
실행 중인 작업은 실패로 표시하고 대기 작업은 다시 실행합니다.

## 장치 지원과 한계

| 환경 | 실행 경로 |
| --- | --- |
| Apple Silicon macOS | PyTorch MPS, 지원되지 않는 연산은 CPU fallback |
| Intel macOS | PyTorch 2.2.2 + NumPy 1.26, CPU |
| Linux CPU | PyTorch CPU wheel |
| Linux x86_64 NVIDIA | CUDA extra + 자동 CUDA 선택 |
| MLX 설치 환경 | MLX 존재 여부를 확인하지만 추론은 PyTorch 사용 |

선택한 Demucs·ADTOF 모델에는 검증된 MLX 백엔드가 없습니다. MLX 설치를 가속 지원으로
간주하지 않습니다. 자동 GPU 실행 실패 시 CPU에서 다시 시도하고 실제 장치를 기록합니다.
사용자가 장치를 명시하면 작업 실패로 표시합니다.

ADTOF는 **킥(35), 스네어(38), 탐(47), 하이햇(42), 심벌(49)** 5종만 구분합니다.
열린/닫힌 하이햇, 탐별 높이, 라이드/크래시, 고스트 노트 및 연주법 구분은 지원하지 않습니다.
검출 점수는 보정된 확률이 아닙니다.

박자 기본값은 **4/4**이며 마디의 downbeat는 자동 판별하지 않습니다. 자동 phase는
quarter-note 비트 위치입니다. 고정 템포/격자로 근사하므로 라이브 템포 변화, 스윙,
복잡한 셋잇단 리듬은 수동 검토가 필요합니다. **출력 성공과 채보 정확도는 별개**이며
원음 또는 기준 악보와 대조해야 합니다.

MuseScore 4.7.5의 일부 macOS 실행은 변환을 마친 뒤 종료 오류를 냅니다. 매번 새 출력
디렉토리에서 SVG XML 구조와 PDF 페이지 수를 검증한 뒤 유효한 출력만 제공하고,
종료 경고와 `engraver.log`를 남깁니다.

## 설정

| 환경 변수 | 기본값/용도 |
| --- | --- |
| `MUSESCORE_BIN` | MuseScore 실행 파일 경로; PATH 및 표준 macOS 위치 자동 탐색 |
| `DRUM_DATA_DIR` | `data` |
| `MAX_UPLOAD_BYTES` | 1 GiB |
| `MAX_DURATION_SECONDS` | 1800 (30분) |
| `ALLOWED_HOSTS` | localhost 외 Host 헤더 허용 목록, 쉼표 구분 |

## 검증

```sh
uv sync --frozen --extra dev
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
```

CI 정의는 macOS 및 Ubuntu에서 의미 검증·API 검증·Verovio SVG 검증을 수행합니다.
실제 모델·FFmpeg 통합 확인은 `scripts/smoke_pipeline.py`를 사용할 수 있습니다.
첫 전체 곡 실험은 [실험 기록](docs/experiment.md)에 기록합니다.

오픈 소스 선택 근거, upstream commit과 라이선스 조건은
[아키텍처 검토](docs/architecture.md)에 있습니다. ADTOF 원본의 CC BY-NC-SA 조건과
포트의 명시적 라이선스 부재를 확인해야 하며 상용 배포 권한을 가정하면 안 됩니다.
