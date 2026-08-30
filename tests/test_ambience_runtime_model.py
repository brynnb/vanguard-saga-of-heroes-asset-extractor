import unittest

from scripts.extractors.export_music_ambience_manifest import (
    AMBIENCE_RUNTIME_SCHEMA,
    build_ambience_bundle,
    simplify_sound_entry,
)


class AmbienceRuntimeModelTests(unittest.TestCase):
    def test_sound_event_preserves_weighted_sound_silence_and_spatial_routes(
        self,
    ) -> None:
        event = simplify_sound_entry(
            {
                "title": "DayOneShots",
                "signal_group": "Environment",
                "info": [1, 50, 1, 2, 1, 1],
                "silt": [31, 1, 6000, 10000, 1],
                "sndt_records": [
                    {
                        "record_index": 0,
                        "order": 1,
                        "chance": 2,
                        "path_index": 4,
                        "target_title": "CanyonBird01.wav",
                        "target_sample_index": 1,
                        "target_bank": "ambiencecanyon.isb",
                        "target_ref_mode": "packed-low16",
                    },
                    {
                        "record_index": 1,
                        "order": 2,
                        "chance": 97,
                        "path_index": 9,
                        "target_title": "CanyonBird18.wav",
                        "target_sample_index": 18,
                        "target_bank": "ambiencecanyon.isb",
                        "target_ref_mode": "packed-low16",
                    },
                ],
            }
        )

        self.assertEqual(event["selection"]["mode"], "weighted-random")
        self.assertEqual(event["selection"]["sound_weight_total_percent"], 99)
        self.assertEqual(event["selection"]["authored_weight_total_percent"], 100)
        self.assertEqual(
            event["silence_route"],
            {
                "order": 31,
                "weight_percent": 1,
                "duration_min_ms": 6000,
                "duration_max_ms": 10000,
                "flags": 1,
            },
        )
        self.assertEqual(event["spatial_path_indices"], [4, 9])
        self.assertEqual(
            event["sound_routes"][0]["target"]["title"], "CanyonBird01.wav"
        )

    def test_unresolved_background_profile_remains_an_executable_control_route(
        self,
    ) -> None:
        event = simplify_sound_entry(
            {
                "title": "DayAmbience",
                "info": [1, 50, 1, 0, 0, 0],
                "sndt_records": [
                    {
                        "record_index": 0,
                        "order": 1,
                        "chance": 50,
                        "path_index": 0,
                        "decoded_as": "control-window",
                        "control_window_kind": "sndt-dual-layer-profile",
                        "control_window_profile_kind": "front-pair",
                        "control_window_selector": 0,
                        "control_window_slot": 1,
                        "control_window_gain": 4.0,
                    }
                ],
            }
        )

        route = event["sound_routes"][0]
        self.assertEqual(route["decoded_as"], "control-window")
        self.assertNotIn("target", route)
        self.assertEqual(route["control_window"]["profile_kind"], "front-pair")
        self.assertEqual(route["control_window"]["gain"], 4.0)

    def test_bundle_declares_only_selectors_supported_by_its_channels(self) -> None:
        cue = {
            "file": "/client/Assets/Sounds/AmbienceCanyon.icb",
            "paired_isb": "/client/Assets/Sounds/ambiencecanyon.isb",
            "semantic_summary": {
                "list_summaries": [
                    {"list_type": "ento", "title": "CanyonAmbience"}
                ],
                "sound_entries": [
                    {"title": "DayAmbience", "info": [1], "sndt_records": []},
                    {"title": "NightAmbience", "info": [1], "sndt_records": []},
                    {"title": "DayOneShots", "info": [1], "sndt_records": []},
                    {"title": "NightOneshots", "info": [1], "sndt_records": []},
                    {"title": "DaySpecial", "info": [1], "sndt_records": []},
                    {"title": "NightSpecial", "info": [1], "sndt_records": []},
                    {
                        "title": "Silence",
                        "info": [1],
                        "silt": [1, 100, 3000, 3000, 1],
                    },
                ],
            },
        }

        bundle = build_ambience_bundle(cue)
        selectors = {
            item["name"]: item for item in bundle["runtime_model"]["selectors"]
        }

        self.assertEqual(bundle["runtime_model"]["schema"], AMBIENCE_RUNTIME_SCHEMA)
        self.assertEqual(selectors["TimeOfDay"]["values"], ["Day", "Night"])
        self.assertEqual(selectors["OneShots"]["phases"], ["day", "night"])
        self.assertEqual(selectors["SpecialAmbience"]["values"], ["Off", "On"])
        self.assertNotIn("Storms", selectors)


if __name__ == "__main__":
    unittest.main()
