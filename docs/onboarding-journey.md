# Onboarding journey and Continue actions

The initial wizard order is About you, Customer inbox, Voice, then Knowledge.
The application's remaining tuning steps follow Knowledge. The server's journey
descriptor supplies the current step, observed saved state, next destination and
the effect of Continue. Knowledge uses the same first-open tuning destination as
its existing completion handler. Unknown saved state is not reported as complete.

Navigation requests on the first three steps receive a deterministic explanation
from that descriptor. Other Assistant answers receive the same authoritative
context. An answer cannot submit a form or connect an account.

The Assistant offers Continue whenever the visible application action is ready.
A review pause and the model's legacy ready_to_advance field do not determine
availability. Required fields, disabled actions, an active operation, pending
notes cards, unsaved Knowledge notes and pending Knowledge reviews retain their
existing safeguards. Pending reviews expose the explicit leave-for-later choice.
No choice is made on the owner's behalf.

Every click resolves the current page action again and checks readiness; an old
detached action cannot be executed. Input, page mutations and the
`blam8:onboarding-readiness` window event refresh the offer. Knowledge clients
should dispatch that event after their operation state changes. The shared
`window.Blam8OnboardingJourney.read(pendingCardCount)` helper reads the current
descriptor from `#onboarding-journey` and returns action, ready, reason and blocker.

Offline validation: `tests/test_onboarding_journey.py` checks server destinations,
verified mailbox state and navigation without a model call. Its Node replay
executes the actual readiness helper and widget action function, including
invalid-to-valid forms, review pauses, Knowledge blockers and a replaced action.
The capture replay verifies field suggestions remain unsubmitted. These checks
do not establish browser layout or production behaviour.
