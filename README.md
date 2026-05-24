# StealKit

Red-DiscordBot v3용 CDN 기반 이모지/스티커 복사 Cog입니다.

## Commands

- `[p]steal emoji <items...>`
  - `<:name:id>`, `<a:name:id>`, 숫자 ID를 지원합니다.
  - 한 번에 최대 10개까지 처리합니다.
- `[p]steal sticker`
  - 명령 실행 후 60초 안에 같은 채널에 스티커 메시지를 보내면 첫 번째 스티커를 복사합니다.

## Required Permissions

- 명령어 실행자: `Manage Emojis and Stickers` / `Manage Expressions`
- 봇: `Manage Emojis and Stickers` / `Manage Expressions`, `Send Messages`, `View Channel`

## Limits and Formats

- 이모지는 정적/애니메이션 슬롯을 분리해 사전 검사합니다.
- 서버의 남은 슬롯보다 요청량이 많으면 생성 전에 중단합니다.
- 스티커는 `png`, `apng`, `gif`만 지원합니다.
- `lottie` 스티커는 Discord/discord.py 업로드 경로에서 지원하기 어렵기 때문에 실패 사유로 안내합니다.

## Asset Safety

원본 이모지/스티커는 절대 수정, 삭제, 이동하지 않습니다. 이 Cog는 Discord CDN URL에서 바이트를 다운로드한 뒤 현재 서버에 새 자산으로 생성만 합니다.

## Copyright Notice

모든 사용자-facing 결과 메시지에는 아래 고지를 표시합니다.

> 복사된 이모지/스티커의 사용 책임 및 저작권 책임은 명령어 실행자에게 있습니다.

## Components V2

모든 안내와 결과 메시지는 `discord.ui.LayoutView`, `Container`, `TextDisplay`, `Separator` 기반 Components V2 구조로 전송합니다. 일반 `content`/`embed` 출력은 사용자-facing UI로 사용하지 않습니다.
