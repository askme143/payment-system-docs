# Subscription Cancel Expiration Design

## Goal

해지 예약된 구독이 이용 기간 종료 시점을 지난 뒤 최종 종료되는 문서 플로우를 추가한다. 이 플로우의 "구독 만료"는 결제 실패 재시도나 결제 시도 `expired`가 아니라, `cancel_scheduled` 구독이 `currentPeriodEnd` 또는 `cancelAt` 이후 `canceled`로 확정되는 상태 전이를 뜻한다.

## Scope

새 시퀀스 페이지를 만들지 않고 기존 `subscription-cancel` 문서에 두 번째 다이어그램을 추가한다. 기존 해지 예약 다이어그램은 회원 요청으로 `active -> cancel_scheduled`가 되는 흐름을 유지하고, 새 다이어그램은 스케줄러가 만료 대상 구독을 찾아 `cancel_scheduled -> canceled`로 마감하는 흐름을 설명한다.

문서 원천은 `docs-data/documentation.json`이며, HTML과 D2 산출물은 기존 `scripts/generate_docs.py`로 재생성한다.

## Flow

스케줄러가 `status=cancel_scheduled`이고 `cancelAt` 또는 `currentPeriodEnd`가 현재 시각 이하인 구독을 조회한다. 서버는 대상 구독을 잠그고, 기간 종료 전이거나 해지 예약이 철회되어 `active`로 돌아간 구독은 제외한다.

검증을 통과한 구독은 최종 상태를 `canceled`로 저장한다. 이때 `canceledAt`, `accessUntil`, `cancelReason`, 감사 로그를 남기고 `nextBillingDate`는 계속 `null`로 유지해 정기 과금 대상에 다시 포함되지 않게 한다.

만료 처리 후 회원의 `subscriptions-me` 조회에서는 종료된 구독을 `canceled`로 반환한다. 기간 종료 전에는 재개가 가능하지만, 만료 후에는 기존 `subscriptions-resume`으로 되돌릴 수 없고 신규 구독 또는 재구독 CTA로 안내한다.

## Error Handling

만료 배치는 멱등해야 한다. 이미 `canceled`인 구독은 상태 변경 없이 건너뛰고, `cancel_scheduled`가 아닌 구독은 처리 대상에서 제외한다.

상태 전이와 권한 회수는 같은 트랜잭션 경계에서 처리한다. 알림 발송이나 이메일 큐 적재 실패는 구독 상태를 되돌리지 않고, 후속 재시도 작업이나 운영 로그로 남긴다.

## Testing

생성 테스트는 기존 실제 문서 테스트 스타일을 따른다. `subscription-cancel-sequence.html`에 새 다이어그램 ID, `cancel_scheduled -> canceled`, `currentPeriodEnd <= now`, 재구독 안내 문구가 포함되는지 확인한다. 생성된 D2 원본에도 만료 대상 조회, 최종 종료 저장, 이후 조회 단계가 포함되어야 한다.
