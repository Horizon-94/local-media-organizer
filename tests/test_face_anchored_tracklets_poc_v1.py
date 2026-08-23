from __future__ import annotations

import unittest

from apps.media_archive_image_video_ui.person_track_suggestions import (
    PersonDetection,
    build_tracklets,
    candidate_suggestions,
)


def detection(
    identifier: str,
    source: str,
    time_ms: int,
    bbox: tuple[float, float, float, float],
    face: str = "",
    shot: str = "",
    visual_id: str = "",
) -> PersonDetection:
    return PersonDetection(
        detection_id=identifier,
        visual_unit_id=visual_id or "visual_" + identifier,
        source_content_id=source,
        time_position_ms=time_ms,
        bbox=bbox,
        face_cluster_ids=(face,) if face else (),
        shot_id=shot,
    )


class FaceAnchoredTrackletTests(unittest.TestCase):
    def test_face_identity_propagates_to_same_continuous_back_view(self) -> None:
        result = build_tracklets([
            detection("front", "video-a", 0, (0.10, 0.10, 0.35, 0.90), "person-kang"),
            detection("side", "video-a", 3000, (0.12, 0.10, 0.37, 0.90)),
            detection("back", "video-a", 6000, (0.14, 0.10, 0.39, 0.90)),
        ])
        self.assertEqual(len(result.tracklets), 1)
        self.assertEqual(result.tracklets[0].identity_status, "FACE_ANCHORED_TRACK")
        self.assertEqual(result.tracklets[0].propagated_member_count, 2)

    def test_shot_boundary_prevents_identity_propagation(self) -> None:
        result = build_tracklets([
            detection("front", "video-a", 0, (0.10, 0.10, 0.35, 0.90), "person-a", "shot-1"),
            detection("new-shot", "video-a", 3000, (0.11, 0.10, 0.36, 0.90), "", "shot-2"),
        ])
        self.assertEqual(len(result.tracklets), 2)
        self.assertEqual(result.tracklets[0].propagated_member_count, 0)

    def test_conflicting_faces_are_not_reported_as_confirmed_identity(self) -> None:
        result = build_tracklets([
            detection("first", "video-a", 0, (0.10, 0.10, 0.35, 0.90), "person-a"),
            detection("second", "video-a", 3000, (0.11, 0.10, 0.36, 0.90), "person-b"),
        ])
        self.assertEqual(len(result.tracklets), 1)
        self.assertEqual(result.tracklets[0].identity_status, "CONFLICT_REQUIRES_REVIEW")
        self.assertEqual(result.tracklets[0].propagated_member_count, 0)

    def test_tracks_never_cross_source_videos(self) -> None:
        result = build_tracklets([
            detection("a", "video-a", 0, (0.10, 0.10, 0.35, 0.90), "person-a"),
            detection("b", "video-b", 3000, (0.11, 0.10, 0.36, 0.90)),
        ])
        self.assertEqual(len(result.tracklets), 2)

    def test_only_nearby_unfaced_members_become_suggestions(self) -> None:
        rows = [
            detection("front", "video-a", 0, (0.10, 0.10, 0.35, 0.90), "person-a"),
            detection("near-back", "video-a", 6000, (0.11, 0.10, 0.36, 0.90)),
            detection("far-back", "video-a", 18000, (0.12, 0.10, 0.37, 0.90)),
        ]
        suggestions = candidate_suggestions(rows, ["person-a"], max_anchor_distance_ms=12000)
        self.assertEqual([value.visual_unit_id for value in suggestions], ["visual_near-back"])

    def test_crowded_frame_is_kept_for_review_but_penalised(self) -> None:
        rows = [
            detection("front", "video-a", 0, (0.10, 0.10, 0.35, 0.90), "person-a"),
            detection("candidate", "video-a", 3000, (0.11, 0.10, 0.36, 0.90), visual_id="visual-crowd"),
            detection("other", "video-a", 3000, (0.60, 0.10, 0.85, 0.90), visual_id="visual-crowd"),
        ]
        suggestion = candidate_suggestions(rows, ["person-a"])[0]
        self.assertEqual(suggestion.frame_person_count, 2)
        self.assertIn("人工确认", suggestion.review_reason)


if __name__ == "__main__":
    unittest.main()
