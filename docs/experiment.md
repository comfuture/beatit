# First full-song experiment: Wake

실행일: 2026-10-05. 기준 악보와의 정확도 비교 전인 자동 채보 초안입니다.

## 입력과 실행

- [Wake | ONEDAY CONFERENCE LIVE | SOLA SCRIPTURA | 아이자야씩스티원](https://youtu.be/-6YAK8u9wiw).
- yt-dlp `bestaudio/best`로 format 251을 다운로드했습니다. Opus 48 kHz,
  스테레오, 약 126 kbps, 4,345,654 bytes, ffprobe 길이 275.121초입니다.
- 브라우저 파일 선택 및 시작 버튼으로 전체 파일을 실제 처리했습니다.
- Apple Silicon macOS, PyTorch 2.8 MPS에서 Demucs htdemucs → ADTOF Frame_RNN을
  실행했습니다. MusicXML은 MuseScore Studio 4.7.5로 A4 PDF/SVG로 조판했습니다.
- 요청은 BPM/phase 자동, 4/4, 16분 격자, 감도 1.0, 전체 믹스 입력입니다.
  4/4와 마디 시작 위치는 자동 판별 결과가 아닙니다. 타격을 수동 수정하지 않았습니다.

재현용 다운로드:

```sh
uvx --from yt-dlp yt-dlp --no-playlist -f 'bestaudio/best' \
  --write-info-json -o 'work/experiment/source.%(ext)s' \
  'https://youtu.be/-6YAK8u9wiw'
```

웹 앱에서 내려받은 파일을 올리고 기본 설정으로 시작합니다. 모델과 의존성은
`uv.lock`의 버전 및 `docs/architecture.md`의 upstream commit을 사용합니다.
장치와 수치 연산 차이로 타격 검출이 조금 달라질 수 있습니다.

## 결과

| 항목 | 값 |
| --- | --- |
| 분석 길이 | 275.11초 |
| BPM | 131.0 |
| Quarter-note phase | 0.4216초 |
| 마디 | 150 |
| 원시 타격 | 1,515 |
| 격자에 기록한 타격 | 1,509 |
| 같은 악기/격자 위치 중복 병합 | 6 |
| 킥 / 스네어 / 탐 / 하이햇 / 심벌 | 644 / 222 / 217 / 88 / 344 |
| 평균 타격-격자 시간차 | 40.4 ms |
| PDF / SVG | A4 4페이지 / 4페이지 |

Librosa의 hop 단위 초기 템포 129.199 BPM은 전체 비트 위치를 회귀한 뒤
131.0 BPM으로 보정했습니다. 비트 회귀 잔차는 24.4 ms입니다.
두 시간차는 채보 정확도나 F1 점수가 아닙니다.

최초 분석 실행은 약 145초였습니다. MuseScore가 유효한 파일을 만든 후 종료
오류(-6)를 내면서 처음에는 실패로 처리되었습니다. 변환물 검증을 추가한 후
저장된 타격/드럼 stem을 재사용하여 약 8초에 다시 조판했습니다. 재시도에서
모델을 다시 실행하지 않았습니다. 최종 출력은 종료 경고를 포함합니다.

## 출력 확인과 평가 범위

- PDF를 Poppler로 4페이지 모두 이미지화해 육안 확인했습니다. 페이지 잘림,
  누락된 글리프 및 겹친 보표는 관찰되지 않았습니다.
- MusicXML의 각 마디에서 두 성부 모두 48 divisions를 채우며,
  타악기 음표 1,509개를 확인했습니다. PDF/SVG 페이지 수도 일치합니다.
- macOS 미리보기에서 최종 PDF를 열고 4페이지 문서를 확인했습니다.
- 모델 검출의 맞고 틀림은 아직 평가하지 않았습니다. 사용자 기준 악보가
  제공되면 악기별 타격 precision/recall, 마디 시작 및 리듬 오류를 대조할 수 있습니다.
- 하이햇 88개와 심벌 344개 등 클래스 간 분류, 탐 검출 및 킥 과검출은
  원음과 확인해야 합니다. 5종 모델은 열린/닫힌 하이햇과 라이드/크래시를 구분하지 않습니다.

출력 묶음에는 PDF, SVG 4개, MusicXML, 원시/격자 MIDI, 타격 JSON,
분석 JSON, 입력 provenance 및 이 기록이 포함됩니다. 분리된 드럼 FLAC은
청음 검토를 위해 별도로 제공합니다. 원본 YouTube 매체는 Git에 추가하지 않았습니다.

## 다른 호스트 검증

Linux ARM64 CPU Docker에서 이 소스의 처음 8초를 검은 영상이 포함된 MP4로
만들어 FFmpeg → Demucs → ADTOF → MusicXML → Verovio SVG 전체 경로를
실제 실행했습니다. MuseScore 미설치 환경의 SVG 출력도 확인했습니다.
macOS와 최종 Linux 이미지에서 각각 테스트 17개가 통과했습니다.
Intel macOS 및 NVIDIA CUDA는 의존성과 선택 경로를 구성했으나 실제 장치에서
실행하지 않았습니다. MLX 추론 경로는 제공하지 않습니다.
