"""Unit tests for shared transcription speaker helpers."""

from types import SimpleNamespace
import unittest

from src.transcription.speaker_attribution import (
    assign_speakers_to_aligned_result,
    assign_speakers_to_segments,
    repair_quality_gate_passed,
    speaker_assignment_metrics,
)
from src.transcription.diarize import assign_speaker
from src.transcription.speaker_review_state import (
    speaker_identity,
    speaker_review_update_fields,
    transcript_requires_speaker_review,
)


class TranscriptionSpeakerHelperTests(unittest.TestCase):
    def test_assign_speakers_to_segments_uses_shared_overlap_logic(self):
        segments = [
            SimpleNamespace(text="Hello world", start_time=0.0, end_time=1.0, words=None),
            SimpleNamespace(text="Goodbye now", start_time=1.2, end_time=2.3, words=None),
        ]

        reassigned = assign_speakers_to_segments(
            segments,
            [(0.0, 1.1, "SPEAKER_00"), (1.1, 2.4, "SPEAKER_01")],
            assign_speaker,
        )

        self.assertEqual([segment.speaker_cluster for segment in reassigned], ["SPEAKER_00", "SPEAKER_01"])

    def test_assign_speakers_to_aligned_result_backfills_short_gap_between_same_speaker(self):
        aligned_result = {
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "Hello"},
                {"start": 1.1, "end": 1.4, "text": "Uh huh"},
                {"start": 1.5, "end": 2.2, "text": "Continue"},
            ]
        }

        attributed = assign_speakers_to_aligned_result(
            aligned_result,
            [(0.0, 1.0, "SPEAKER_00"), (1.5, 2.2, "SPEAKER_00")],
            assign_speaker,
        )

        self.assertEqual(
            [segment.get("speaker_cluster") for segment in attributed["segments"]],
            ["SPEAKER_00", "SPEAKER_00", "SPEAKER_00"],
        )

    def test_speaker_metrics_and_review_state_share_generic_speaker_rules(self):
        unresolved_segments = [
            SimpleNamespace(speaker="SPEAKER_00", speaker_cluster="SPEAKER_00"),
            SimpleNamespace(speaker="SPEAKER_01", speaker_cluster="SPEAKER_01"),
        ]
        resolved_segments = [
            SimpleNamespace(speaker="Oscar", speaker_cluster="SPEAKER_00"),
            SimpleNamespace(speaker="Jillian", speaker_cluster="SPEAKER_01"),
        ]

        unresolved_metrics = speaker_assignment_metrics(unresolved_segments)
        self.assertEqual(unresolved_metrics["actual_speaker_count"], 2)
        self.assertTrue(transcript_requires_speaker_review(unresolved_segments))
        self.assertFalse(transcript_requires_speaker_review(resolved_segments))
        self.assertEqual(speaker_identity(resolved_segments[0]), "SPEAKER_00")

        update_fields = speaker_review_update_fields(resolved_segments)
        self.assertFalse(update_fields["speaker_review_required"])
        self.assertIsNotNone(update_fields["speaker_review_completed_at"])

    def test_repair_quality_gate_requires_expected_count_and_low_unassigned_ratio(self):
        self.assertTrue(
            repair_quality_gate_passed(
                {
                    "actual_speaker_count": 3,
                    "unassigned_segment_ratio": 0.0,
                },
                expected_speaker_count=3,
            )
        )
        self.assertFalse(
            repair_quality_gate_passed(
                {
                    "actual_speaker_count": 2,
                    "unassigned_segment_ratio": 0.0,
                },
                expected_speaker_count=3,
            )
        )


if __name__ == "__main__":
    unittest.main()
