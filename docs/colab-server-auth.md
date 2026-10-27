# Mac 없이 사용하는 서버 Colab 인증

2026-10-27부터 [이슈 #8](https://github.com/comfuture/beatit/issues/8)의 배포 목표는
MacBook을 꺼도 chunsik에서 분석을 시작할 수 있는 구성이다. 이전의
[수동 연결 모드](colab-deployment.md)는 선택 가능한 복구/실험 경로로 남긴다.

## 인증과 실행 구조

```text
최초 1회: chunsik의 로그인 컨테이너 → 사용자 Google 동의 → 서버 전용 OAuth 토큰
일상 실행: 웹 작업 큐 → 컨트롤러 → 서버 관리자 → Colab 할당/갱신/해제
                                ↑ 단기 런타임 상태만 전달
```

서버 전용 `beatit_colab-auth` 볼륨 안에서 공식 CLI의 새로운 OAuth 동의를 완료한다.
Mac의 `~/.config/colab-cli/token.json`은 복사하지 않는다. 발급된 access token과
refresh token은 서버 볼륨의 `.config/colab-cli/token.json`에 저장된다.
서로 다른 장치에서 발급한 토큰은 별도로 보관하며, 재인증할 때 기존 Mac 토큰을 철회하지 않는다.

관리자만 이 볼륨을 읽기 전용으로 마운트한다. 필요한 `client_id`, `client_secret`,
`refresh_token`을 `/tmp`의 0700 임시 디렉토리에 `authorized_user` ADC로 구성하고
`colab --auth adc`를 사용한다. 이 ADC 파일은 tmpfs에만 존재한다. 웹 앱·컨트롤러·이미지·
Git·Colab 작업 소스에는 Google 계정 인증을 넣지 않는다. 관리자 상태/CLI 디버그 로그는
별도 비공개 `colab-manager-state` 볼륨에만 보관한다.

관리자는 새 분석 요청이 있을 때 최대 한 개의 T4를 할당한다. 준비된 환경은 이어지는 작업에서
재사용하며 기본 10분 유휴 후 해제한다. 앱 시작만으로 GPU를 요청하지 않는다. 관리자가
정상인 대기 상태에서는 앱이 `분석 시 Colab GPU 자동 할당`을 표시한다.

할당 결과가 불확실하면 이전 계정 할당 목록과 비교하고 추가 VM을 만들지 않는다.
세션 이름뿐 아니라 저장된 endpoint가 일치해야 재사용/해제한다. 다른 장치의 런타임을
소유했다고 추정하거나 삭제하지 않는다. 인증 철회·할당 거절·관리자 연결 단절은 UI와
작업 오류에 반영하며 CPU로 조용히 대체하지 않는다.

## 최초 로그인과 재인증

전용 볼륨을 준비한다. 기존 볼륨은 삭제하지 않는다.

```sh
docker volume create beatit_colab-auth
sudo docker run --rm --user 0 \
  --mount type=volume,src=beatit_colab-auth,dst=/auth \
  --entrypoint python "$BEATIT_CONTROLLER_IMAGE" \
  -c "import os; os.chown('/auth',10001,10001); os.chmod('/auth',0o700)"
```

아래 명령은 서버에서 실행한다. 이미지 변수는 현재 배포한 컨트롤러 이미지 태그로 설정한다.

```sh
sudo docker run --rm -it --user 10001:10001 \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  -e HOME=/app/colab-auth \
  --mount type=volume,src=beatit_colab-auth,dst=/app/colab-auth \
  --entrypoint sh "$BEATIT_CONTROLLER_IMAGE" \
  -c 'umask 077; exec colab --auth oauth2 sessions'
```

표시된 Google URL에서 승인하고 코드를 해당 터미널에 입력한다. 비밀번호나 인증 코드·
토큰을 채팅/이슈/로그에 게시하지 않는다. `No active sessions found on server.`와 종료 코드
0은 인증 성공 후 활성 VM이 없다는 정상 결과다. 아직 GPU 할당을 요청한 것이 아니다.

재인증 시에는 관리자부터 중지해 자신의 VM을 해제한 다음 진행한다. 기존 서버 grant가
철회되어 새 로그인이 필요하면 서버 인증 파일만 보호된 백업 경로로 이동하고 위 흐름을
다시 실행한다. Mac 인증은 별도로 유지한다. 새 파일을 저장한 후 관리자를 재시작한다.

## 배포와 복구

기존 `.env`의 Tailscale bind와 내부 API 인증 파일을 유지한다.

```sh
docker compose -f compose.yaml -f compose.colab.yaml -f compose.colab-server.yaml build
sudo docker compose -f compose.yaml -f compose.colab.yaml -f compose.colab-server.yaml up -d --wait
```

관리자 컨테이너는 0.5 CPU/512 MiB 상한이고 외부 포트를 열지 않는다. 컨트롤러는 Google
인증 볼륨에 접근할 수 없다. 배포 후 `/api/system`의 `allocation_mode=server`,
`can_allocate=true`, 유휴 상태에서 활성 Colab 세션 0개를 확인한다. 로그인 볼륨은
`external`이므로 일반 앱 구성과 분리해 보존한다. `down -v`로 데이터를 삭제하지 않는다.

이전 수동 모드로 돌아가려면 먼저 관리자를 중지해 자신의 런타임을 해제한 뒤 서버 overlay를
빼고 실행한다. 데이터 볼륨은 유지한다.

```sh
sudo docker compose -f compose.yaml -f compose.colab.yaml -f compose.colab-server.yaml stop colab-manager
sudo docker compose -f compose.yaml -f compose.colab.yaml up -d --remove-orphans --wait
```

인증 독립성과 GPU 이용 자격은 별개다. 유료 결제는 설정하지 않았으며, Google의
[Colab 리소스/사용 제한](https://research.google.com/colaboratory/faq.html)은 그대로 적용된다.
할당을 거절하면 실패를 표시한다. 인위적 keep-alive, 계정 순환, 제한 우회는 구현하지 않는다.
Google은 refresh token을 철회할 수 있으므로 영구 무인증을 보장하지 않는다.
자세한 흐름은 [공식 CLI 인증 문서](https://github.com/googlecolab/google-colab-cli/blob/main/docs/04_automation_and_utility.md)와
[Google 토큰 수명 안내](https://developers.google.com/identity/protocols/oauth2#expiration)를 따른다.
