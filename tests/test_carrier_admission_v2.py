import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

DIGEST = "a" * 64


def _valid_verdict_dict(*, family: str, leg_name: str, route: str | None, attempt: int = 1) -> dict:
    return {
        "schema": "triad-leg-verdict.v2",
        "review_id": "rev-20261015-01",
        "family": family,
        "leg_name": leg_name,
        "route": route,
        "attempt": attempt,
        "content_digest": DIGEST,
        "verdict": "SAFE TO MERGE",
        "ApproveForMerge": True,
        "inspected_surfaces": ["bin/validate_v2.py"],
        "findings": [],
        "strengths": ["Clean boundaries"],
        "open_questions": [],
    }


class TestCarrierAdmissionV2(unittest.TestCase):
    def test_admit_carrier_output_rejects_nested_duplicate_keys_across_all_carriers(self):
        import verdict_v2

        agy_binding = {
            "review_id": "rev-20261015-01",
            "family": "google",
            "content_digest": DIGEST,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
        }
        bound_agy = verdict_v2.bound_verdict(agy_binding)
        body = json.dumps(_valid_verdict_dict(family="google", leg_name="google", route="agy"))
        dup_body = '{"review_id":"other",' + body[1:]

        # 1. agy-stream-json duplicate key inside structured_output
        agy_stream = (
            '{"type":"step_update","step":1}\n'
            + '{"type":"result","status":"SUCCESS","result":{"structured_output":'
            + dup_body
            + "}}\n"
        )
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_agy, carrier="agy-stream-json", stdout=agy_stream, exit_code=0, completed=True
            )

        # 2. gemini-json duplicate key inside response string
        gemini_binding = {**agy_binding, "route": "gemini"}
        bound_gemini = verdict_v2.bound_verdict(gemini_binding)
        gemini_body = json.dumps(_valid_verdict_dict(family="google", leg_name="google", route="gemini"))
        dup_gemini_body = '{"review_id":"other",' + gemini_body[1:]
        gemini_envelope = json.dumps({"response": dup_gemini_body})
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_gemini, carrier="gemini-json", stdout=gemini_envelope, exit_code=0, completed=True
            )

        # 3. claude-json duplicate key inside structured_output
        claude_binding = {**agy_binding, "family": "claude", "leg_name": "claude", "route": None}
        bound_claude = verdict_v2.bound_verdict(claude_binding)
        claude_body = json.dumps(_valid_verdict_dict(family="claude", leg_name="claude", route=None))
        dup_claude_body = '{"review_id":"other",' + claude_body[1:]
        claude_envelope = (
            '{"type":"result","subtype":"success","is_error":false,"result":"ok","structured_output":'
            + dup_claude_body
            + "}"
        )
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_claude, carrier="claude-json", stdout=claude_envelope, exit_code=0, completed=True
            )

    def test_admit_carrier_output_rejects_route_and_binding_swaps(self):
        import verdict_v2

        agy_binding = {
            "review_id": "rev-20261015-01",
            "family": "google",
            "content_digest": DIGEST,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
        }
        bound_agy = verdict_v2.bound_verdict(agy_binding)
        wrong_route_verdict = _valid_verdict_dict(family="google", leg_name="google", route="gemini")
        agy_stream = json.dumps(
            {"type": "result", "status": "SUCCESS", "result": {"structured_output": wrong_route_verdict}}
        )
        with self.assertRaises(ValueError):
            verdict_v2.admit_carrier_output(
                bound_agy, carrier="agy-stream-json", stdout=agy_stream, exit_code=0, completed=True
            )

    def test_agy_c23_bridge_extracts_original_span_and_preserves_duplicate_key_rejection(self):
        import antigravity_wrapper
        import verdict_v2

        agy_binding = {
            "review_id": "rev-20261015-01",
            "family": "google",
            "content_digest": DIGEST,
            "leg_name": "google",
            "attempt": 1,
            "route": "agy",
        }
        bound_agy = verdict_v2.bound_verdict(agy_binding)
        valid_body = json.dumps(_valid_verdict_dict(family="google", leg_name="google", route="agy"))
        c23_stream_valid = (
            '{"type":"result","status":"ERROR","result":{"structured_output":' + valid_body + "}}\n"
        )
        admitted = antigravity_wrapper._admit_v2_agy_output(
            bound_agy,
            stdout=c23_stream_valid,
            stderr="",
            vendor_exit_code=0,
            c23_recovered=True,
        )
        self.assertEqual(admitted["route"], "agy")

        dup_body = '{"review_id":"other",' + valid_body[1:]
        c23_stream_dup = (
            '{"type":"result","status":"ERROR","result":{"structured_output":' + dup_body + "}}\n"
        )
        with self.assertRaises(ValueError):
            antigravity_wrapper._admit_v2_agy_output(
                bound_agy,
                stdout=c23_stream_dup,
                stderr="",
                vendor_exit_code=0,
                c23_recovered=True,
            )
