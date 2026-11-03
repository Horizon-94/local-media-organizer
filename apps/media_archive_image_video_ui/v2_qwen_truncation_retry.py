"""One bounded, contract-declared retry; no validator or sampling relaxation."""
from dataclasses import asdict
import json

POLICY = 'confirmed_output_truncation_384_to_768_once_v1'

def generate(runner, *, candidate_id, image_path, prompt):
    original = runner.max_tokens
    attempts = []
    try:
        outcome = runner.generate_one(candidate_id=candidate_id, image_path=image_path, prompt=prompt)
        attempts.append({'max_output_tokens': original, 'outcome': asdict(outcome)})
        finish = str(outcome.raw_finish_reason or '').lower()
        confirmed = finish == 'length' if finish else (
            outcome.generation_tokens is not None and outcome.generation_tokens >= original)
        if original == 384 and outcome.result_status != 'success' and outcome.missing_required_sections and confirmed:
            runner.max_tokens = 768
            outcome = runner.generate_one(candidate_id=candidate_id, image_path=image_path, prompt=prompt)
            attempts.append({'max_output_tokens': 768, 'outcome': asdict(outcome)})
        if outcome.result_status != 'success':
            from .v2_runtime import ContractError
            raise ContractError('SPECIAL_REVIEW_REQUIRED:' + json.dumps(
                {'policy': POLICY, 'candidate_id': candidate_id, 'attempts': attempts}, ensure_ascii=False))
        return outcome, {'policy': POLICY, 'attempts': attempts}
    finally:
        runner.max_tokens = original
