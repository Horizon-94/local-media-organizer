from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))

from media_archive_image_video_ui.local_person_annotations import (  # noqa: E402
    add_visual_membership,
    annotation_path,
    create_identity,
    detach_cluster,
    grouped_catalog,
    load_annotations,
    merge_identity,
    name_identity,
    resolve_clusters,
    save_annotations,
)


class LocalPersonAnnotationsTests(unittest.TestCase):
    def test_name_merge_and_detach_are_local_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "library.sqlite"
            path = annotation_path(root / "state", db)
            payload = load_annotations(path)
            first = name_identity(payload, "cluster-a", "张三", ["家人", "常客"])
            target = merge_identity(payload, "cluster-b", first)
            save_annotations(path, payload)
            restored = load_annotations(path)

            self.assertEqual(target, first)
            self.assertEqual(resolve_clusters(restored, first), ["cluster-a", "cluster-b"])
            self.assertEqual(restored["identities"][first]["display_name"], "张三")
            self.assertEqual(restored["identities"][first]["tags"], ["家人", "常客"])

            detached = detach_cluster(restored, "cluster-b")
            self.assertNotEqual(detached, first)
            self.assertEqual(resolve_clusters(restored, first), ["cluster-a"])
            self.assertEqual(resolve_clusters(restored, detached), ["cluster-b"])

    def test_grouped_catalog_collapses_machine_clusters(self) -> None:
        payload = load_annotations(Path("/does/not/exist"))
        local_id = merge_identity(payload, "cluster-b", "cluster-a")
        name_identity(payload, local_id, "人物甲", ["本地标签"])
        rows = [
            {"person_cluster_id": "cluster-a", "member_count": 5, "distinct_source_count": 2},
            {"person_cluster_id": "cluster-b", "member_count": 7, "distinct_source_count": 3},
        ]
        result = grouped_catalog(rows, payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["person_cluster_id"], local_id)
        self.assertEqual(result[0]["machine_cluster_ids"], ["cluster-a", "cluster-b"])
        self.assertEqual(result[0]["member_count"], 12)
        self.assertEqual(result[0]["display_name"], "人物甲")

    def test_manual_people_accept_frames_without_changing_machine_groups(self) -> None:
        payload = load_annotations(Path("/does/not/exist"))
        first = create_identity(payload, "康康", ["家人"])
        add_visual_membership(payload, first, "visual-back", "source-video")
        second = create_identity(payload, "临时人物")
        add_visual_membership(payload, second, "visual-front", "source-image")
        merged = merge_identity(payload, second, first)

        self.assertEqual(merged, first)
        self.assertEqual(resolve_clusters(payload, first), [])
        self.assertEqual(
            [row["visual_unit_id"] for row in payload["visual_memberships"][first]],
            ["visual-back", "visual-front"],
        )
        self.assertEqual(payload["identities"][first]["display_name"], "康康")


if __name__ == "__main__":
    unittest.main()
