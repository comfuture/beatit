# chunsik의 수동 연결 Colab 백엔드

2026-10-27 서버 독립 실행 구성은 [서버 전용 인증 문서](colab-server-auth.md)를 참고한다.
이 문서는 선택 가능한 수동 연결 모드와 최초 배포 검증 기록이다.

[구현 이슈 #8](https://github.com/comfuture/beatit/issues/8)은 멀티유저 전환
[#7](https://github.com/comfuture/beatit/issues/7)과 별개다. 최초 배포는 계정별 분리가 없는
기존 개인용 앱을 Tailscale 안에서 사용하며, Google 계정 인증은 로컬 운영자 CLI에 둔다.

## 접속과 연결

- Tailscale: <http://100.71.242.57:18000>
- SSH 접속: `ssh -N -L 18000:100.71.242.57:18000 chunsik` 후 <http://127.0.0.1:18000>
- 앱과 컨트롤러: `/home/ubuntu/services/beatit`
- 컨트롤러 API는 `127.0.0.1:18001` 및 Docker 내부에서만 접근 가능하다.

로컬 Mac에서 기존 공식 Colab CLI 로그인을 사용한다.

```sh
python3 scripts/connect_colab.py --host chunsik
```

이 명령은 전경에서 실행한다. 자신의 `beatit-chunsik` T4 런타임을 한 개 연결하고,
정상 런타임이 이미 있으면 재사용한다. 연결 후 앱에서 분석할 수 있다. 첫 작업은
코드·가상환경·모델을 준비하고, 이후 작업은 동일한 준비 버전이면 이 과정을 생략한다.
노트북 문서를 생성하지 않고 Python 코드와 개별 작업 디렉토리를 사용한다.

작업이 없으면 기본 10분 후 런타임을 해제한다. `Ctrl-C`로 연결을 끝내도 자신의 VM을
해제한다. 원격 계정 한도로 할당을 거부하거나 런타임을 회수하면 새 VM을 무한 재시도하지
않으며, 운영자가 명시적으로 다시 연결한다. Colab 할당 응답이 불확실하면 개인 운영 로그와
`colab --config work/colab-operator/sessions.json sessions`를 확인한다.

연결 전에는 앱이 **Colab 연결 대기**를 표시하고 새 분석을 비활성화한다. 서버가 몰래
GPU를 할당하거나 CPU 분석으로 대체하지 않는다. 이미 완료한 악보의 재생·다운로드·편집·
재조판은 연결 없이도 사용할 수 있다. 오래된 캐시의 비트 재분석처럼 추론이 필요한 재시도는
Colab 연결이 필요하다. Colab 입력 한도는 120 MiB이며, 전송 묶음은 128 MiB를 넘지 않아야 한다.

이 모드는 유료 계획이 없는 현 단계의 **수동 연결 구조와 제한된 검증**을 위한 것이다.
무료 Colab을 상시 무인 웹/분산 워커로 운영할 수 있다는 의미는 아니다. 장기 자동 운영은
[Google의 플랜·사용 제한](https://research.google.com/colaboratory/faq.html)을 충족하는
계정과 별도 수명 관리 정책을 갖춘 후 진행한다.

## 인증과 상태

Google OAuth 파일, refresh token, gcloud 설정, 서비스 계정 키는 서버에 복사하지 않는다.
로컬 운영자가 SSH로 해당 VM의 단기 프록시 상태만 전송하며, 20초마다 연결 lease를 갱신한다.
서버는 90초 동안 갱신이 없으면 실행 중인 원격 작업을 중지하고 연결 대기로 돌아간다.
단기 토큰 자체도 비밀이므로 로컬 작업 경로와 서버 `colab-state` 볼륨은 비공개다.

앱과 컨트롤러 간 API 인증 파일은 서버의
`/home/ubuntu/services/beatit-secrets/colab-api-token`에 둔다. 이는 Google 인증이 아닌
Beatit 내부 API용 난수이며 UID 10001만 읽을 수 있다. 컨트롤러 상태 볼륨은 웹 컨테이너에
마운트하지 않는다. 기본 CLI의 민감한 디버그 로그도 이 비공개 볼륨 안에만 남는다.

원격 프로세스 종료를 확인하지 못한 작업은 정리 대기로 기록한다. 재연결/재시작 후 해당
작업을 정리하기 전에는 새 GPU 작업을 실행하지 않는다. 로컬 CLI가 VM 해제 성공을 확인한
경우에만 서버의 정리 대기를 해소한다. 연결이 물리적으로 끊어져 해제할 수 없다면 운영자가
Colab에서 자신의 VM을 확인해야 하며, 서버가 다른 계정의 할당을 지우거나 keep-alive로
수명을 강제로 연장하지 않는다.

준비된 환경은 런타임 안에 남기지만 작업별 오디오·분석·악보 파일은 결과 회수 후 정리한다.
회수한 ZIP은 경로·허용 파일·크기·CRC·MusicXML/SVG/MIDI를 검증하고 결과 포인터를 마지막에
교체한다. 실행 런타임 식별 해시와 환경 재사용 여부를 결과의 `execution_runtime`에 기록한다.

## 서버 설치 및 업데이트

기본 `.env`의 Tailscale bind/Host 설정은 유지한다. 다음 항목을 추가한다.

```dotenv
BEATIT_EXECUTION_BACKEND=colab
BEATIT_CONTROLLER_IMAGE=beatit-colab-controller:local
BEATIT_COLAB_API_TOKEN_FILE=/home/ubuntu/services/beatit-secrets/colab-api-token
BEATIT_COLAB_IDLE_SECONDS=600
```

API 키는 `secrets.token_hex(32)`처럼 생성하며 이미지·Git·환경 변수 값에 넣지 않는다.
파일 소유자는 10001:10001, 파일 모드는 0400, 부모 디렉토리는 0700으로 설정한다.
소스를 갱신한 후 다음 명령으로 두 이미지를 빌드한다.

```sh
docker compose -f compose.yaml -f compose.colab.yaml build
sudo docker compose -f compose.yaml -f compose.colab.yaml up -d --wait
sudo docker compose -f compose.yaml -f compose.colab.yaml ps
sudo docker compose -f compose.yaml -f compose.colab.yaml logs --tail 100
```

키 파일 경로의 보호 때문에 서비스 시작에는 `sudo`를 사용한다. 내부 API 키를 교체할 때는
파일을 안전하게 갱신하고 두 컨테이너를 재생성한다. Google 인증을 바꾸려면 서버 키 파일이
아니라 로컬 Colab CLI 로그인부터 갱신한다.

앱 데이터는 기존 `beatit_data` 볼륨을 유지한다. `beatit_colab-state`는 컨트롤러의 비공개
상태와 복구 기록이다. 컨테이너 재생성 시에도 두 볼륨을 삭제하지 않는다. 배포 전 백업은
`backups/before-colab.tar.gz`, 기존 이미지 스냅샷은 `beatit:before-colab`에 보관했다.

## 롤백

먼저 로컬 운영자 연결을 종료해 GPU를 해제한다. 그런 다음 서버에서 실행한다.

```sh
sudo env BEATIT_IMAGE=beatit:before-colab docker compose -f compose.yaml \
  up -d --no-build --remove-orphans --wait
```

이전 CPU 서버가 같은 데이터 볼륨으로 돌아온다. `down -v`는 사용하지 않는다.

## 검증

Python 테스트는 비공개 API 인증, 잘못된 runtime URL/토큰, 준비 버전 재사용/변경,
중복 요청, 취소·재시작의 미확인 원격 프로세스 정리, ZIP 경로/무결성, 포인터 발행 순서,
Google 계정 인증 격리, 운영자 할당 실패 시 해제를 다룬다. 프런트엔드 검증은 연결 대기와
복구, 분석 버튼 제어, 준비/회수 진행률, 취소, 상태 조회 빈도를 확인한다.

### 2026-10-26 실제 배포 검증

chunsik의 웹 → 비공개 컨트롤러 → 수동 연결 T4 → 결과 회수 경로로 12초 오디오를 검증했다.
다음 시간은 전송·준비·추론·결과 회수를 포함한 작업 시간이다.

| 작업 | 환경 재사용 | 시간 |
| --- | --- | ---: |
| 최초 분석 | 아니요 | 187.6초 |
| 같은 런타임의 다음 분석 | 예 | 38.6초 |
| 원격 실행 중 취소한 작업의 재시도 | 예 | 31.9초 |

세 작업의 런타임 식별 해시는 `3fb303f976fd960b`로 같았다. 실제 GPU 프로세스 취소 후
정리 대기가 해소된 다음 재시도했으며, MusicXML/MIDI/SVG/ZIP과 원본·드럼·스템 7개 미디어의
Range 응답을 확인했다. 메타데이터 수정·전체 악보 재조판은 서버에서 완료했고 브라우저에서
파형과 수정한 제목의 악보를 확인했다. 검증에 사용한 입력과 비공개 CLI 로그는 Git에 넣지 않는다.

배포 전부터 있던 프로젝트와 위 결과는 컨테이너 재생성 후에도 유지한다. 검증 종료 후 로컬
운영자 연결을 종료했고, 공식 CLI의 활성 세션 목록이 비어 있음을 확인했다. 일상 배포는 연결
대기이며 유료 잔액 추가나 자동 할당은 설정하지 않았다.

최종 코드의 Python 테스트 115개가 통과했고, 프런트엔드 타입 검사·빌드·단위 테스트 40개·
브라우저 테스트 40개가 통과했다. 위 측정은 짧은 입력의 연결·회수 검증이며 일반 곡의
속도 비교는 [별도 275초 곡 벤치마크](colab-feasibility.md)를 참고한다.
배포 Git revision과 이미지 식별자는 PR 및 서버 `deployment.json`에 별도로 기록한다.
