import unittest

from app_core.douyin_commerce_setup_state import (
    CollectorDiagnosticEvent,
    CollectorState,
    CollectorType,
    SetupGenerationState,
    SetupGenerationStateError,
    new_setup_generation,
)


class SetupGenerationTest(unittest.TestCase):
    def test_closed_generation_rejects_late_collector_result(self):
        generation = new_setup_generation(account_id=31)
        generation.transition(SetupGenerationState.COLLECTING)
        generation.activate_collector(
            CollectorType.DOMESTIC_LOCATION,
            instance_id="domestic-1",
            session_id="session-a",
        )
        generation.transition(SetupGenerationState.CANCELLING)
        generation.close()

        self.assertFalse(
            generation.accepts_result(
                generation.generation_id,
                CollectorType.DOMESTIC_LOCATION,
                "domestic-1",
            )
        )

    def test_illegal_closed_to_collecting_transition_is_rejected(self):
        generation = new_setup_generation(account_id=31)
        generation.close()

        with self.assertRaises(SetupGenerationStateError):
            generation.transition(SetupGenerationState.COLLECTING)

    def test_illegal_publishing_to_ready_transition_is_rejected(self):
        generation = new_setup_generation(account_id=31)
        generation.transition(SetupGenerationState.COLLECTING)
        generation.transition(SetupGenerationState.CLOSING_COLLECTORS)
        generation.transition(SetupGenerationState.PUBLISHING)

        with self.assertRaises(SetupGenerationStateError):
            generation.transition(SetupGenerationState.READY)

    def test_mismatched_generation_or_instance_rejects_collector_result(self):
        generation = new_setup_generation(account_id=31)
        generation.transition(SetupGenerationState.COLLECTING)
        generation.activate_collector(
            CollectorType.DOMESTIC_LOCATION,
            instance_id="domestic-1",
            session_id="session-a",
        )

        self.assertFalse(
            generation.accepts_result(
                "another-generation",
                CollectorType.DOMESTIC_LOCATION,
                "domestic-1",
            )
        )
        self.assertFalse(
            generation.accepts_result(
                generation.generation_id,
                CollectorType.DOMESTIC_LOCATION,
                "domestic-2",
            )
        )

    def test_non_active_collectors_reject_results(self):
        generation = new_setup_generation(account_id=31)
        generation.transition(SetupGenerationState.COLLECTING)
        generation.activate_collector(
            CollectorType.DOMESTIC_LOCATION,
            instance_id="domestic-1",
            session_id="session-a",
        )
        slot = generation.collectors[CollectorType.DOMESTIC_LOCATION]

        for state in (CollectorState.FAILED, CollectorState.CLOSING, CollectorState.CLOSED):
            slot.state = state
            self.assertFalse(
                generation.accepts_result(
                    generation.generation_id,
                    CollectorType.DOMESTIC_LOCATION,
                    "domestic-1",
                )
            )

    def test_close_closes_every_collector_and_removes_sessions(self):
        generation = new_setup_generation(account_id=31)
        generation.transition(SetupGenerationState.COLLECTING)
        for collector_type in CollectorType:
            generation.activate_collector(
                collector_type,
                instance_id=f"{collector_type.value}-1",
                session_id="session-a",
            )

        generation.close()

        self.assertEqual(generation.state, SetupGenerationState.CLOSED)
        for slot in generation.collectors.values():
            self.assertEqual(slot.state, CollectorState.CLOSED)
            self.assertIsNone(slot.session_id)


class CollectorDiagnosticEventTest(unittest.TestCase):
    def test_public_diagnostic_does_not_expose_sensitive_values(self):
        event = CollectorDiagnosticEvent(
            request_id="request-1",
            setup_generation_id="generation-1",
            collector_type=CollectorType.LOCAL_LOCATION,
            collector_instance_id="local-1",
            account_masked_id="account-31",
            phase="search",
            action="search_locations",
            scope="local",
            keyword="夜南香",
            attempt=1,
            candidate_count=0,
            duration_ms=1200,
            outcome="failed",
            error_code="candidate_panel_missing",
            cleanup_result="closed",
        )
        public = event.to_public_dict()

        self.assertEqual(public["errorCode"], "candidate_panel_missing")
        self.assertNotIn("cookie", str(public).casefold())
        self.assertNotIn("session-a", str(public))

    def test_public_diagnostic_normalizes_sensitive_or_unknown_fields(self):
        event = CollectorDiagnosticEvent(
            request_id="request-1",
            setup_generation_id="generation-1",
            collector_type=CollectorType.LOCAL_LOCATION,
            collector_instance_id="local-1",
            account_masked_id="private-account-file.json",
            phase="search",
            action="search_locations",
            scope="local",
            keyword="a" * 100,
            attempt=1,
            candidate_count=0,
            duration_ms=1200,
            outcome="failed",
            error_code="raw exception with cookie=session-a",
            cleanup_result="closed",
        )
        public = event.to_public_dict()

        self.assertEqual(public["accountMaskedId"], "account-unknown")
        self.assertEqual(public["errorCode"], "collector_unknown")
        self.assertEqual(len(public["keyword"]), 80)


if __name__ == "__main__":
    unittest.main()
