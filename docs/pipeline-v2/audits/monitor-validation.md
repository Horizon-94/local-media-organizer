# Benchmark monitor validation

No model, database, or media was used. Each child was bounded to about two seconds.

## cpu

- exit: `0`
- samples: `9`
- process RSS samples (non-zero): `0/9`
- CPU samples (non-zero): `0/9`
- system available observations: `9`

- Expected signal: CPU rises while the loop runs.

## memory

- exit: `0`
- samples: `9`
- process RSS samples (non-zero): `0/9`
- CPU samples (non-zero): `0/9`
- system available observations: `9`

- Expected signal: child RSS rises while the allocation is resident.

## Result

- vm_stat and `memory_pressure -Q` system-direction sampling is available in this shell.
- Process-tree RSS/CPU are not reliable here: `ps` visibility is denied by the Codex sandbox, so the observed process counts/RSS are empty or zero.
- swap and iostat are also unavailable in this sandbox.
- Child exit detection and bounded completion work.
- **Monitor validation status: PARTIAL / HOST TERMINAL REQUIRED for process RSS, CPU, swap, and disk metrics.**
